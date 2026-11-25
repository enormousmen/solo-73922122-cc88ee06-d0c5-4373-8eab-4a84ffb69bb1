"""切片调度器：常规底图两名额分层调度 + 应急单进程插队。

常规底图规则（需求逐条落地）：
- 两个渲染名额，“正在渲染”与“算完待出图（持有）”两类合计最多两张；
- 按缩放层 z0→z3 推进：同一层瓦片算完立即发布 PNG 并更新进度；
- 本层最后一张还在算、出现空位时，预渲染下一级的一张瓦片（算完先持有不发布），
  等本层全部出齐后才发布它、再进入下一层；
- 本层任一瓦片失败：立即停止派发本层新瓦片，已在算的成功后照常发布，
  下一级预渲染（在算或已持有）一律作废；在算瓦片清空后任务判失败；
- 预渲染瓦片先失败：本层照常收尾，等本层出齐后才报告下一级的错误；
- 应急受理：中止常规全部 worker、舍弃所有未发布/持有瓦片（staging 全清），
  应急全程只运行一个渲染子进程；应急结束后常规自动续做，且只算缺片；
- 失败重试同样只算缺片：已发布 PNG 留在磁盘，重建计划时自动跳过。

实现要点：
- worker.py 为常驻子进程，stdin 行协议派单，stdout 行 JSON 上报；
- worker 只写 ``.staging/<id>.png`` 暂存文件，发布（os.replace 原子改名）或作废
  （删除）全部由本调度器决定，“持有/作废/延迟发布”因此只在父进程一侧实现；
- 所有状态变更都在同一个调度协程里串行处理（事件经 asyncio.Queue 汇总），无需加锁。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from collections import deque
from pathlib import Path

TILE_SIZE = 256
ZOOMS = [0, 1, 2, 3]
KINDS = ("regular", "emergency")
ACTIVE = ("queued", "running", "interrupted")
REGULAR_SLOTS = 2   # 常规底图并发名额（在算 + 待出图合计上限）
ABORT_RC = 42       # worker 被中止时的约定退出码
FAIL_RC = 3         # worker 模拟失败退出码


def all_tiles() -> list[tuple[int, int, int]]:
    return [(z, x, y) for z in ZOOMS for x in range(2 ** z) for y in range(2 ** z)]


TOTAL = len(all_tiles())  # 1 + 4 + 16 + 64 = 85


class Job:
    """单个切片任务的持久态（已发布集合以磁盘为准）。"""

    def __init__(self, kind: str, data_dir: Path):
        self.kind = kind
        self.src = data_dir / "sources" / f"{kind}.png"
        self.out_dir = data_dir / "tiles" / kind
        self.staging_dir = self.out_dir / ".staging"
        self.log_file = data_dir / "logs" / f"{kind}.log"
        self.status = "idle"  # idle/queued/running/interrupted/failed/done
        self.published: set[tuple[int, int, int]] = set()
        self.error: str | None = None
        self.failed_zoom: int | None = None  # 最近一次失败所在/上报的缩放层（快照用）
        self.simulate_fail = False       # emergency: --fail-after
        self.fail_tile: tuple[int, int, int] | None = None  # regular: --fail-tile
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.rescan()

    def tile_file(self, t: tuple[int, int, int]) -> Path:
        z, x, y = t
        return self.out_dir / str(z) / str(x) / f"{y}.png"

    def rescan(self) -> None:
        """以磁盘为准重建已发布集合（启动时、每轮运行结束、被抢占后调用）。"""
        self.published = {t for t in all_tiles() if self.tile_file(t).is_file()}
        if not self.published:
            self.status = "idle"
        elif len(self.published) == TOTAL:
            self.status = "done"

    def remaining(self) -> list[tuple[int, int, int]]:
        return [t for t in all_tiles() if t not in self.published]

    def layer_count(self, z: int) -> int:
        return sum(1 for t in self.published if t[0] == z)

    def log(self, msg: str) -> None:
        try:
            with self.log_file.open("a") as f:
                f.write(f"{time.strftime('%F %T')} [{self.kind}] {msg}\n")
        except OSError:
            pass

    def snapshot(self, inflight: list[dict] | None = None,
                 held: list[str] | None = None, cur_z: int | None = None,
                 layer_failed: set[int] | None = None) -> dict:
        done = len(self.published)
        layer_failed = layer_failed or set()
        layers = []
        for z in ZOOMS:
            total_z = 4 ** z
            pub_z = self.layer_count(z)
            if pub_z == total_z:
                st = "done"
            elif z in layer_failed:
                st = "failed"
            else:
                st = "open"
            layers.append({"z": z, "total": total_z, "published": pub_z, "status": st})
        return {
            "kind": self.kind,
            "status": self.status,
            "published": done,
            "total": TOTAL,
            "percent": round(done * 100.0 / TOTAL, 1),
            "current_z": cur_z,
            "inflight": inflight or [],
            "held": held or [],
            "layers": layers,
            "error": self.error,
            "tiles": [f"{z}/{x}/{y}" for (z, x, y) in sorted(self.published)],
        }


class Assignment:
    """一张瓦片的派单记录。"""

    def __init__(self, worker: "Worker", tile: tuple[int, int, int],
                 prerender: bool, staging: Path):
        self.id = uuid.uuid4().hex
        self.worker = worker
        self.tile = tile
        self.z = tile[0]
        self.prerender = prerender
        self.staging = staging
        self.discard = False  # 本层失败后，在算的预渲染完成时作废而非持有


class RunState:
    """常规任务一次运行（start→done/failed/interrupted）的易失调度态。"""

    def __init__(self, job: Job):
        self.job = job
        self.pending: dict[int, deque[tuple[int, int, int]]] = {z: deque() for z in ZOOMS}
        missing = set(job.remaining())
        for t in all_tiles():  # all_tiles 已按 z/x/y 排序，保证同层按序派发
            if t in missing:
                self.pending[t[0]].append(t)
        self.cur_z = next(z for z in ZOOMS if self.pending[z])
        self.inflight: dict[str, Assignment] = {}   # assignment id -> Assignment
        self.held: dict[tuple[int, int, int], Path] = {}  # 预渲染完成待发布
        self.layer_open: dict[int, bool] = {z: True for z in ZOOMS}
        self.deferred_error: dict[int, str] = {}    # 预渲染失败 → 本层出齐后上报
        self.finalized = False
        self._advancing = False  # 层收尾/推进重入保护（仅在主协程内访问，无需锁）

    def layer_complete(self, z: int) -> bool:
        return self.job.layer_count(z) == 4 ** z

    def cur_inflight(self) -> list[Assignment]:
        return [a for a in self.inflight.values() if not a.prerender]


class Worker:
    """一个常驻渲染子进程及其读线协程。"""

    def __init__(self, wid: str, proc: asyncio.subprocess.Process):
        self.wid = wid
        self.proc = proc
        self.busy = False
        self.current: Assignment | None = None
        self.reader: asyncio.Task | None = None
        self.dead = False

    async def send(self, payload: dict | str) -> bool:
        """写一行命令。只 write 不 drain：保证调度主循环永不因管道背压阻塞，
        背压由 Scheduler._drain() 在主循环每次迭代统一排空。"""
        try:
            data = payload if isinstance(payload, str) else json.dumps(payload)
            self.proc.stdin.write((data + "\n").encode())
            return True
        except (BrokenPipeError, ConnectionResetError, ProcessLookupError, RuntimeError):
            return False

    async def drain(self) -> None:
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ProcessLookupError, RuntimeError):
            pass

    async def abort(self) -> None:
        """stdin ABORT 指令 + SIGTERM 双通道中止（沙箱禁信号时前者仍生效）。"""
        try:
            self.proc.stdin.write(b"ABORT\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ProcessLookupError, RuntimeError, ConnectionResetError):
            pass
        try:
            self.proc.terminate()
        except (PermissionError, ProcessLookupError):
            pass


class Scheduler:
    def __init__(self, data_dir: Path, tile_delay: float):
        self.data_dir = data_dir
        self.tile_delay = tile_delay
        self.jobs = {k: Job(k, data_dir) for k in KINDS}
        self._worker_py = Path(__file__).resolve().parent / "worker.py"
        self._q: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._tick: asyncio.Task | None = None
        # 常规 worker 池（最多 REGULAR_SLOTS 个）
        self.pool: dict[str, Worker] = {}
        self.run: RunState | None = None
        # 应急单 worker
        self.em_worker: Worker | None = None
        self.em_pending: deque[tuple[int, int, int]] = deque()
        self.em_total = 0
        self.preempting = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="tile-scheduler")
        # 心跳：持有瓦片等本层出齐等场景下可能没有新事件，靠 tick 驱动再次派发
        self._tick = asyncio.create_task(self._tick_loop(), name="scheduler-tick")

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            self._q.put_nowait(("tick",))

    async def shutdown(self) -> None:
        for w in list(self.pool.values()) + ([self.em_worker] if self.em_worker else []):
            await w.abort()
        for w in list(self.pool.values()) + ([self.em_worker] if self.em_worker else []):
            if w.proc.returncode is None:
                try:
                    await asyncio.wait_for(w.proc.wait(), 5)
                except asyncio.TimeoutError:
                    w.proc.kill()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if getattr(self, "_tick", None):
            self._tick.cancel()
            try:
                await self._tick
            except asyncio.CancelledError:
                pass

    # ---------- 值班员操作 ----------

    def request_start(self, kind: str, simulate_fail: bool = False,
                      fail_tile: tuple[int, int, int] | None = None) -> Job:
        job = self.jobs[kind]
        if job.status in ACTIVE:
            return job  # 幂等
        if not job.remaining():
            job.status = "done"
            job.error = None
            return job
        job.status = "queued"
        job.error = None
        job.started_at = time.time()
        job.finished_at = None
        if kind == "emergency":
            job.simulate_fail = simulate_fail
        else:
            job.fail_tile = fail_tile
        self._q.put_nowait(("wake",))
        return job

    def reset(self, kind: str) -> Job:
        job = self.jobs[kind]
        if job.status in ACTIVE:
            raise RuntimeError("任务进行中，无法重置")
        shutil.rmtree(job.out_dir, ignore_errors=True)
        job.out_dir.mkdir(parents=True, exist_ok=True)
        job.staging_dir.mkdir(parents=True, exist_ok=True)
        job.published.clear()
        job.status = "idle"
        job.error = None
        job.failed_zoom = None
        return job

    # ---------- 子进程管理 ----------

    async def _spawn(self, kind: str, fail_after: int = 0,
                     fail_tile: tuple[int, int, int] | None = None) -> Worker:
        job = self.jobs[kind]
        cmd = [
            sys.executable, "-P", str(self._worker_py),
            "--src", str(job.src),
            "--out", str(job.out_dir),
            "--tile", str(TILE_SIZE),
            "--delay", str(self.tile_delay),
        ]
        if fail_after:
            cmd += ["--fail-after", str(fail_after)]
        if fail_tile:
            cmd += ["--fail-tile", "/".join(map(str, fail_tile))]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        wid = uuid.uuid4().hex
        w = Worker(wid, proc)
        w.reader = asyncio.create_task(self._read_worker(w, kind, proc.stderr))
        return w

    async def _read_worker(self, w: Worker, kind: str,
                           stderr: asyncio.StreamReader) -> None:
        """读线协程：worker 全部事件汇总进主队列，状态只在主协程改。"""
        try:
            while True:
                line = await w.proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                self._q.put_nowait(("ev", kind, w.wid, ev))
        finally:
            err = await stderr.read()
            if err:
                self.jobs[kind].log(f"worker stderr: {err.decode('utf-8', 'replace').strip()}")
            rc = w.proc.returncode if w.proc.returncode is not None else ABORT_RC
            self._q.put_nowait(("exit", kind, w.wid, rc))

    async def _retire(self, w: Worker) -> None:
        if w.proc.returncode is None:
            await w.send("quit")
            try:
                await asyncio.wait_for(w.proc.wait(), 3)
            except asyncio.TimeoutError:
                await w.abort()
                try:
                    await asyncio.wait_for(w.proc.wait(), 3)
                except asyncio.TimeoutError:
                    w.proc.kill()
        if w.reader:
            try:
                await w.reader
            except asyncio.CancelledError:
                pass

    # ---------- 主循环 ----------

    async def _loop(self) -> None:
        while True:
            ev = await self._q.get()
            try:
                if ev[0] == "wake":
                    await self._on_wake()
                elif ev[0] == "ev":
                    await self._on_event(ev[1], ev[2], ev[3])
                elif ev[0] == "exit":
                    await self._on_exit(ev[1], ev[2], ev[3])
                # ev[0] == "tick"：无状态变更，仅靠迭代末尾的派发逻辑驱动
            except Exception as e:  # 兜底：调度循环不能死
                import traceback
                self.jobs["regular"].log(
                    f"scheduler loop error: {e!r}\n{traceback.format_exc()}")
            # 统一排空所有 worker 的 stdin 背压（派发只 write 不 drain）
            for w in list(self.pool.values()):
                await w.drain()
            if self.em_worker is not None:
                await self.em_worker.drain()
            # 每次迭代（含 tick 心跳）都尝试派发与收尾；无空位/无空闲 worker 时立即返回，天然幂等
            if self.jobs["regular"].status == "running" and not self.preempting:
                self._after_regular_progress()  # 持有时无新事件，靠 tick 推进层收尾
                await self._dispatch_regular()
                for w in list(self.pool.values()):
                    await w.drain()

    async def _on_wake(self) -> None:
        em, rg = self.jobs["emergency"], self.jobs["regular"]
        # 应急优先：必要时先抢占常规
        if em.status == "queued":
            if rg.status in ("queued", "running") and (self.pool or self.run):
                await self._preempt_regular()
            if self.em_worker is None:
                await self._start_emergency()
            return
        if (rg.status == "queued" and self.run is None and not self.preempting
                and em.status not in ("queued", "running")):
            await self._start_regular()

    # ---------- 常规：启动 / 抢占 / 派发 ----------

    async def _start_regular(self) -> None:
        job = self.jobs["regular"]
        job.rescan()  # 以磁盘为准：续做/重试只排缺片
        if not job.remaining():
            job.status = "done"
            return
        self.run = RunState(job)
        job.status = "running"
        job.failed_zoom = None
        job.log(f"开始常规切片，缺片 {len(job.remaining())}/{TOTAL}，"
                f"起始层 z{self.run.cur_z}，名额 {REGULAR_SLOTS}")
        self.pool.clear()
        for _ in range(REGULAR_SLOTS):
            w = await self._spawn("regular", fail_tile=job.fail_tile)
            self.pool[w.wid] = w

    async def _preempt_regular(self) -> None:
        """应急受理：中止常规全部 worker，舍弃所有未发布/持有瓦片。"""
        self.preempting = True
        job = self.jobs["regular"]
        job.status = "interrupted"
        job.log("应急受理：中止常规 worker，舍弃全部未发布/持有瓦片")
        workers = list(self.pool.values())
        for w in workers:
            await w.abort()
        for w in workers:
            if w.proc.returncode is None:
                try:
                    await asyncio.wait_for(w.proc.wait(), 5)
                except asyncio.TimeoutError:
                    w.proc.kill()
        # 等读线协程收尾（其 exit 事件到达时 wid 已不在 pool，会被忽略）
        for w in workers:
            if w.reader:
                try:
                    await asyncio.wait_for(w.reader, 5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    w.reader.cancel()
        self.pool.clear()
        self.run = None
        shutil.rmtree(job.staging_dir, ignore_errors=True)  # 舍弃全部暂存/持有瓦片
        job.staging_dir.mkdir(parents=True, exist_ok=True)
        job.rescan()
        self.preempting = False

    async def _dispatch_regular(self) -> None:
        run = self.run
        job = self.jobs["regular"]
        # 应急活动期间（排队/运行）绝不派发常规瓦片
        em = self.jobs["emergency"]
        if em.status in ("queued", "running") or self.em_worker is not None:
            return
        if run is None or run.finalized or job.status != "running":
            return
        z = run.cur_z
        while True:
            if not run.layer_open[z]:
                return  # 本层已失败：停止派发任何新瓦片（在算的照常收尾）
            # 名额 = 在算 + 待出图（持有）合计不超过 REGULAR_SLOTS
            used = len(run.inflight) + len(run.held)
            if used >= REGULAR_SLOTS:
                return
            w = next((w for w in self.pool.values() if not w.busy and not w.dead), None)
            if w is None:
                return
            if run.pending[z]:
                tile = run.pending[z].popleft()
                await self._assign(w, tile, prerender=False)
                continue
            # 本层待派已空：最后一张还在算时，用空位预渲染下一级一张
            cur_inflight = run.cur_inflight()
            pre_exists = any(a.prerender for a in run.inflight.values()) or bool(run.held)
            nz = z + 1
            if (len(cur_inflight) == 1 and not pre_exists and nz in run.pending
                    and run.pending[nz] and not run.deferred_error.get(nz)
                    and run.layer_open[z]):
                tile = run.pending[nz].popleft()
                await self._assign(w, tile, prerender=True)
            return

    async def _assign(self, w: Worker, tile: tuple[int, int, int], prerender: bool) -> None:
        run = self.run
        staging = run.job.staging_dir / f"{uuid.uuid4().hex}.png"
        a = Assignment(w, tile, prerender, staging)
        w.busy = True
        w.current = a
        run.inflight[a.id] = a
        run.job.log(f"派发 z{tile[0]}/{tile[1]}/{tile[2]}"
                    f"{'（下一级预渲染，算完先持有）' if prerender else ''} -> {w.wid[:8]}")
        await w.send({
            "cmd": "render", "id": a.id,
            "z": tile[0], "x": tile[1], "y": tile[2],
            "staging": str(staging),
        })

    # ---------- 常规：事件处理 ----------

    async def _on_regular_event(self, w: Worker, ev: dict) -> None:
        run = self.run
        job = self.jobs["regular"]
        if run is None:
            return
        name = ev.get("event")
        if name == "start":
            return
        if name == "tile":
            a = w.current
            if a is None or a.id != ev.get("id"):
                Path(ev.get("staging", "")).unlink(missing_ok=True)
                return
            w.busy = False
            w.current = None
            run.inflight.pop(a.id, None)
            if a.prerender:
                if a.discard or not run.layer_open[run.cur_z] or run.finalized:
                    a.staging.unlink(missing_ok=True)  # 本层已失败：预渲染作废
                    job.log(f"预渲染 {a.tile} 作废（本层失败）")
                else:
                    run.held[a.tile] = a.staging  # 算完待出图，占名额，等本层出齐
                    job.log(f"预渲染 {a.tile} 完成并持有，等待本层 z{run.cur_z} 出齐")
            else:
                self._publish(job, a)  # 同层算完立即发布
            self._after_regular_progress()
        elif name == "error":
            a = w.current
            msg = ev.get("message", "render error")
            w.busy = False
            w.current = None
            if a is not None:
                run.inflight.pop(a.id, None)
                a.staging.unlink(missing_ok=True)
                if a.prerender:
                    # 预渲染先失败：记录错误，本层照常收尾，出齐后再上报
                    run.deferred_error[a.z] = msg
                    job.log(f"预渲染 {a.tile} 失败，错误延迟到本层 z{run.cur_z} 出齐后上报：{msg}")
                else:
                    run.layer_open[run.cur_z] = False
                    job.error = msg
                    job.log(f"本层 z{run.cur_z} 瓦片 {a.tile} 失败：停止派发新瓦片，"
                            f"在算的照常发布，预渲染作废")
                    for other in run.inflight.values():
                        if other.prerender:
                            other.discard = True
                    for t, p in list(run.held.items()):
                        p.unlink(missing_ok=True)
                        job.log(f"已持有预渲染 {t} 作废")
                    run.held.clear()
            self._after_regular_progress()

    def _publish(self, job: Job, a: Assignment) -> None:
        final = job.tile_file(a.tile)
        final.parent.mkdir(parents=True, exist_ok=True)
        a.staging.replace(final)  # staging -> 正式 PNG，原子发布
        job.published.add(a.tile)
        job.log(f"发布 {a.tile[0]}/{a.tile[1]}/{a.tile[2]}（累计 {len(job.published)}/{TOTAL}）")

    def _after_regular_progress(self) -> None:
        run = self.run
        job = self.jobs["regular"]
        if run is None or run.finalized or run._advancing:
            return
        z = run.cur_z
        # 本层失败：在算瓦片清空后判失败（不再派发）
        if not run.layer_open[z] and not run.inflight:
            self._fail_regular(job.error or f"zoom {z} 层渲染失败")
            return
        if not run.layer_complete(z):
            return
        # 本层已出齐
        if run.deferred_error.get(z + 1):
            self._fail_regular(run.deferred_error[z + 1])  # 预渲染先失败：此刻才上报
            return
        pre_inflight = [a for a in run.inflight.values() if a.prerender]
        if pre_inflight:
            # 本层出齐但预渲染还在算：等它落地（持有/失败）后再推进
            return
        # 此时本层出齐、无预渲染在算。先把持有瓦片全部发布落盘，再切换层。
        run._advancing = True  # 一次性推进保护，防止 tick 重入
        try:
            for tile, staging in list(run.held.items()):
                if staging.is_file():  # 幂等：已被发布过则跳过
                    final = job.tile_file(tile)
                    final.parent.mkdir(parents=True, exist_ok=True)  # os.replace 不自动建目录
                    staging.replace(final)
                    job.published.add(tile)
                    job.log(f"本层 z{z} 出齐，发布持有的预渲染 {tile}")
            run.held.clear()
            nz = z + 1
            while nz <= max(ZOOMS) and run.layer_complete(nz):
                nz += 1
            if nz > max(ZOOMS):
                self._finish_regular()  # 内部会把 run 置 None
                return
            run.cur_z = nz
            job.log(f"进入缩放层 z{nz}")
        finally:
            if self.run is run:
                run._advancing = False

    def _fail_regular(self, msg: str) -> None:
        run = self.run
        job = self.jobs["regular"]
        if run is None or run.finalized:
            return
        run.finalized = True
        job.status = "failed"
        job.error = msg
        # 失败层：层内失败取当前层；预渲染延迟失败取 deferred_error 对应的下一级层
        deferred = {z for z in run.deferred_error}
        job.failed_zoom = min(deferred) if deferred else run.cur_z
        job.finished_at = time.time()
        job.log(f"常规任务失败：{msg}")
        for a in run.inflight.values():
            a.staging.unlink(missing_ok=True)
        for p in run.held.values():
            p.unlink(missing_ok=True)
        run.held.clear()
        workers = list(self.pool.values())
        self.pool.clear()
        self.run = None
        shutil.rmtree(job.staging_dir, ignore_errors=True)
        job.staging_dir.mkdir(parents=True, exist_ok=True)
        job.rescan()
        asyncio.create_task(self._retire_workers(workers))

    def _finish_regular(self) -> None:
        run = self.run
        job = self.jobs["regular"]
        if run is None:
            return
        run.finalized = True
        job.status = "done"
        job.error = None
        job.finished_at = time.time()
        job.log(f"常规切片完成 {len(job.published)}/{TOTAL}")
        workers = list(self.pool.values())
        self.pool.clear()
        self.run = None
        shutil.rmtree(job.staging_dir, ignore_errors=True)
        job.rescan()
        asyncio.create_task(self._retire_workers(workers))

    async def _retire_workers(self, workers: list[Worker]) -> None:
        for w in workers:
            await self._retire(w)

    # ---------- 应急 ----------

    async def _start_emergency(self) -> None:
        job = self.jobs["emergency"]
        job.rescan()
        missing = job.remaining()
        if not missing:
            job.status = "done"
            return
        self.em_pending = deque(missing)
        self.em_total = len(missing)
        job.status = "running"
        job.log(f"应急开始，缺片 {self.em_total}/{TOTAL}，单渲染子进程")
        self.em_worker = await self._spawn(
            "emergency", fail_after=10 if job.simulate_fail else 0
        )
        await self._pump_emergency()

    async def _pump_emergency(self) -> None:
        w = self.em_worker
        if w is None or w.busy or w.dead:
            return
        if not self.em_pending:
            return
        tile = self.em_pending.popleft()
        staging = self.jobs["emergency"].staging_dir / f"{uuid.uuid4().hex}.png"
        a = Assignment(w, tile, prerender=False, staging=staging)
        w.busy = True
        w.current = a
        await w.send({
            "cmd": "render", "id": a.id,
            "z": tile[0], "x": tile[1], "y": tile[2],
            "staging": str(staging),
        })

    async def _on_emergency_event(self, w: Worker, ev: dict) -> None:
        job = self.jobs["emergency"]
        name = ev.get("event")
        if name == "start":
            return
        if name == "tile":
            a = w.current
            if a is None or a.id != ev.get("id"):
                Path(ev.get("staging", "")).unlink(missing_ok=True)
                return
            w.busy = False
            w.current = None
            self._publish(job, a)  # 应急瓦片算完即发布
            if not self.em_pending:
                # 全部缺片已发布：让唯一的应急 worker 正常退出，exit 事件负责收尾
                await w.send({"cmd": "quit"})
                return
            await self._pump_emergency()
        elif name == "error":
            a = w.current
            w.busy = False
            w.current = None
            if a is not None:
                a.staging.unlink(missing_ok=True)
            job.error = ev.get("message", "emergency render error")
            job.log(f"应急渲染失败：{job.error}")

    async def _finish_emergency(self, failed: bool) -> None:
        job = self.jobs["emergency"]
        w = self.em_worker
        self.em_worker = None
        if w is not None:
            await self._retire(w)
        shutil.rmtree(job.staging_dir, ignore_errors=True)
        job.staging_dir.mkdir(parents=True, exist_ok=True)
        job.rescan()
        job.finished_at = time.time()
        if failed:
            job.status = "failed"
        else:
            job.status = "done" if not job.remaining() else "failed"
        job.log(f"应急结束（{'失败' if failed else '完成'}），已发布 {len(job.published)}/{TOTAL}")
        # 应急无论成功/失败，常规若处于待续/排队且仍有缺片则自动续做（只算缺片）
        rg = self.jobs["regular"]
        if rg.status in ("interrupted", "queued"):
            rg.rescan()
            if rg.remaining():
                rg.status = "queued"
                rg.error = None
                rg.log("应急结束，常规自动续做（只算缺片）")
                self._q.put_nowait(("wake",))
            else:
                rg.status = "done"

    # ---------- 事件分发 ----------

    async def _on_event(self, kind: str, wid: str, ev: dict) -> None:
        if kind == "emergency":
            w = self.em_worker
            if w is not None and w.wid == wid:
                await self._on_emergency_event(w, ev)
        else:
            w = self.pool.get(wid)
            if w is not None:
                await self._on_regular_event(w, ev)

    async def _on_exit(self, kind: str, wid: str, rc: int) -> None:
        if kind == "emergency":
            w = self.em_worker
            if w is None or w.wid != wid:
                return
            w.dead = True
            job = self.jobs["emergency"]
            interrupted = rc == ABORT_RC or rc < 0
            failed = bool(interrupted or rc != 0 or job.error)
            # 未处理完的派单一律作废
            if w.current is not None:
                w.current.staging.unlink(missing_ok=True)
                w.current = None
            for st in job.staging_dir.glob("*.png"):
                st.unlink(missing_ok=True)
            await self._finish_emergency(failed=failed)
            return

        w = self.pool.get(wid)
        if w is None:
            return  # 抢占/收退后的滞后事件
        w.dead = True
        self.pool.pop(wid, None)  # 死亡 worker 移出池子，避免残留影响快照与派发
        run = self.run
        job = self.jobs["regular"]
        a = w.current
        if a is not None and run is not None and not run.finalized:
            # 没有 error 事件就退出（被信号杀死等）：按对应失败策略处理
            w.busy = False
            w.current = None
            run.inflight.pop(a.id, None)
            a.staging.unlink(missing_ok=True)
            msg = f"worker 异常退出 (rc={rc})，瓦片 {a.tile[0]}/{a.tile[1]}/{a.tile[2]} 未发布"
            if a.prerender:
                run.deferred_error[a.z] = msg
                job.log(f"预渲染 {a.tile} 异常退出，错误延迟上报：{msg}")
            else:
                run.layer_open[run.cur_z] = False
                job.error = msg
                for other in run.inflight.values():
                    if other.prerender:
                        other.discard = True
                for t, p in list(run.held.items()):
                    p.unlink(missing_ok=True)
                run.held.clear()
                job.log(msg)
            self._after_regular_progress()

    # ---------- 快照 ----------

    def snapshot(self) -> dict:
        rg, em = self.jobs["regular"], self.jobs["emergency"]
        # 常规快照
        rg_inflight, rg_held, rg_cur, rg_fail = [], [], None, set()
        if self.run is not None and rg.status in ("queued", "running"):
            rg_inflight = [
                {"tile": list(a.tile), "prerender": a.prerender}
                for a in sorted(self.run.inflight.values(), key=lambda a: a.tile)
            ]
            rg_held = ["/".join(map(str, t)) for t in sorted(self.run.held)]
            rg_cur = self.run.cur_z
            rg_fail = {z for z, ok in self.run.layer_open.items() if not ok}
            rg_fail |= {z for z in self.run.deferred_error}
        if rg.failed_zoom is not None:
            rg_fail.add(rg.failed_zoom)  # RunState 已销毁后的持久失败层标记
        # 应急快照（单 worker，最多一张在渲）
        em_inflight = []
        if self.em_worker is not None and self.em_worker.current is not None:
            em_inflight = [{
                "tile": list(self.em_worker.current.tile),
                "prerender": False,
            }]
        return {
            "regular_slots": REGULAR_SLOTS,
            "tile_delay": self.tile_delay,
            "emergency_worker": self.em_worker is not None,
            "regular_workers": sum(1 for w in self.pool.values() if not w.dead),
            "jobs": {
                "regular": rg.snapshot(rg_inflight, rg_held, rg_cur, rg_fail),
                "emergency": em.snapshot(em_inflight),
            },
        }
