"""切片调度器。

约束：
- 同一时刻最多运行一个渲染子进程（worker.py）；
- 应急任务受理后立即中止正在渲染的常规子进程（stdin 中止指令 + SIGTERM 双通道），
  等它退出后再开应急；
- 被中止时正在渲染、尚未发布的常规瓦片直接舍弃，恢复后重算；
- 应急无论成功/失败，常规切片都会自动续做；已发布瓦片不重算、照常可取。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

TILE_SIZE = 256
ZOOMS = [0, 1, 2, 3]
KINDS = ("regular", "emergency")
ACTIVE = ("queued", "running", "interrupted")
ABORT_RC = 42  # worker 被父进程中止时的约定退出码


def all_tiles() -> list[tuple[int, int, int]]:
    return [(z, x, y) for z in ZOOMS for x in range(2 ** z) for y in range(2 ** z)]


TOTAL = len(all_tiles())  # 1 + 4 + 16 + 64 = 85


class Job:
    """单个切片任务（常规底图 / 应急航片）的运行态。"""

    def __init__(self, kind: str, data_dir: Path):
        self.kind = kind
        self.src = data_dir / "sources" / f"{kind}.png"
        self.out_dir = data_dir / "tiles" / kind
        self.log_file = data_dir / "logs" / f"{kind}.log"
        self.status = "idle"  # idle/queued/running/interrupted/done/failed
        self.published: set[tuple[int, int, int]] = set()
        self.current_tile: tuple[int, int, int] | None = None
        self.error: str | None = None
        self.simulate_fail = False
        self.plan_info: dict | None = None  # 子进程最近一次汇报的 {total, remaining}
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.rescan()

    def tile_file(self, t: tuple[int, int, int]) -> Path:
        z, x, y = t
        return self.out_dir / str(z) / str(x) / f"{y}.png"

    def rescan(self) -> None:
        """以磁盘为准重建已发布集合（启动时、每轮子进程结束后调用）。"""
        self.published = {t for t in all_tiles() if self.tile_file(t).is_file()}
        if not self.published:
            self.status = "idle"
        elif len(self.published) == TOTAL:
            self.status = "done"

    def remaining(self) -> list[tuple[int, int, int]]:
        return [t for t in all_tiles() if t not in self.published]

    def snapshot(self) -> dict:
        done = len(self.published)
        return {
            "kind": self.kind,
            "status": self.status,
            "published": done,
            "total": TOTAL,
            "percent": round(done * 100.0 / TOTAL, 1),
            "current_tile": list(self.current_tile) if self.current_tile else None,
            "error": self.error,
            "plan": self.plan_info,
            "tiles": [f"{z}/{x}/{y}" for (z, x, y) in sorted(self.published)],
        }


class Scheduler:
    def __init__(self, data_dir: Path, tile_delay: float):
        self.data_dir = data_dir
        self.tile_delay = tile_delay
        self.jobs = {k: Job(k, data_dir) for k in KINDS}
        self.proc: asyncio.subprocess.Process | None = None
        self.running_kind: str | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._worker_py = Path(__file__).resolve().parent / "worker.py"

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="tile-scheduler")

    async def shutdown(self) -> None:
        if self.proc and self.proc.returncode is None:
            self._stop_proc(self.proc)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _stop_proc(proc: asyncio.subprocess.Process) -> None:
        """立即中止渲染子进程：先写 stdin 中止指令（worker 的 select 会被立即唤醒），
        再补一发 SIGTERM 双保险。某些沙箱环境禁止发信号，两种通道各自容错。"""
        try:
            proc.stdin.write(b"ABORT\n")
        except (BrokenPipeError, ProcessLookupError, RuntimeError):
            pass
        try:
            proc.terminate()
        except (PermissionError, ProcessLookupError):
            pass

    # ---------- 值班员操作 ----------

    def request_start(self, kind: str, simulate_fail: bool = False) -> Job:
        job = self.jobs[kind]
        if job.status in ACTIVE:
            return job  # 幂等：已排队/在跑/被抢占待续，不重复受理
        if not job.remaining():
            job.status = "done"
            return job
        job.status = "queued"
        job.error = None
        job.plan_info = None
        job.started_at = time.time()
        job.finished_at = None
        if kind == "emergency":
            job.simulate_fail = simulate_fail
            if self.running_kind == "regular" and self.proc and self.proc.returncode is None:
                # 立即中止正在渲染的常规瓦片；当前瓦片尚未发布，直接舍弃。
                # 主循环 await proc.wait() 之后才会启动应急子进程。
                self._stop_proc(self.proc)
        self._wake.set()
        return job

    def reset(self, kind: str) -> Job:
        job = self.jobs[kind]
        if job.status in ACTIVE:
            raise RuntimeError("任务进行中，无法重置")
        shutil.rmtree(job.out_dir, ignore_errors=True)
        job.out_dir.mkdir(parents=True, exist_ok=True)
        job.published.clear()
        job.status = "idle"
        job.error = None
        job.current_tile = None
        job.plan_info = None
        return job

    # ---------- 调度主循环 ----------

    def _pick(self) -> str | None:
        em, rg = self.jobs["emergency"], self.jobs["regular"]
        if em.status == "queued" and em.remaining():
            return "emergency"  # 应急优先
        if rg.status in ("queued", "interrupted") and rg.remaining():
            return "regular"
        return None

    async def _loop(self) -> None:
        while True:
            kind = self._pick()
            if kind is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._run(kind)  # 串行执行 => 同时最多一个子进程
            except Exception as e:  # 子进程spawn失败等意外：标记失败，调度循环存活
                job = self.jobs[kind]
                job.status = "failed"
                job.error = f"scheduler error: {e!r}"
                self.proc = None
                self.running_kind = None

    async def _run(self, kind: str) -> None:
        job = self.jobs[kind]
        job.status = "running"
        # -P：不把脚本目录/当前目录放进 sys.path，但保留 site-packages（Pillow 在其中）
        cmd = [
            sys.executable, "-P", str(self._worker_py),
            "--src", str(job.src),
            "--out", str(job.out_dir),
            "--tile", str(TILE_SIZE),
            "--zooms", ",".join(map(str, ZOOMS)),
            "--delay", str(self.tile_delay),
        ]
        if kind == "emergency" and job.simulate_fail:
            cmd += ["--fail-after", "10"]  # 自测钩子：发布 10 张后模拟崩溃
        logf = open(job.log_file, "ab")
        logf.write(f"\n===== run {time.strftime('%F %T')} =====\n".encode())
        logf.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,   # 中止指令通道
            stdout=asyncio.subprocess.PIPE,  # 进度事件
            stderr=logf,
        )
        self.proc = proc
        self.running_kind = kind
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                self._apply(job, ev)
        finally:
            rc = await proc.wait()  # 等原进程退出，之后主循环才可能开下一个
            logf.close()
            self.proc = None
            self.running_kind = None
            job.current_tile = None
            job.finished_at = time.time()
            job.rescan()  # 以磁盘为准对齐已发布集合
        if rc == 0:
            job.status = "done"
        elif rc == ABORT_RC or rc < 0:
            # 被应急任务中止（stdin 指令或 SIGTERM）；若恰好已全部发布则视为完成
            job.status = "done" if not job.remaining() else "interrupted"
        else:
            job.status = "failed"
            job.error = job.error or self._log_tail(job)

    def _apply(self, job: Job, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "plan":
            job.plan_info = {"total": ev["total"], "remaining": ev["remaining"]}
        elif kind == "start_tile":
            job.current_tile = (ev["z"], ev["x"], ev["y"])
        elif kind == "tile":
            job.published.add((ev["z"], ev["x"], ev["y"]))
            job.current_tile = None
        elif kind == "error":
            job.error = ev.get("message")

    @staticmethod
    def _log_tail(job: Job, n: int = 2000) -> str | None:
        try:
            data = job.log_file.read_bytes()[-n:]
            return data.decode("utf-8", "replace").strip() or None
        except OSError:
            return None

    def snapshot(self) -> dict:
        return {
            "running_kind": self.running_kind,
            "tile_delay": self.tile_delay,
            "jobs": {k: j.snapshot() for k, j in self.jobs.items()},
        }
