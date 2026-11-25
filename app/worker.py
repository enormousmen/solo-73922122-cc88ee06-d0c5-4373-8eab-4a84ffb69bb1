#!/usr/bin/env python3
"""常驻渲染子进程（worker 池成员），只依赖标准库 + Pillow。

调度器按名额拉起常驻 worker：常规任务 2 个（合计两个名额）、应急任务 1 个。
每个 worker 同一时刻只渲染一张瓦片，任务通过 stdin 行协议下发，事件经 stdout 逐行上报。

stdin 协议（每行一个 JSON；单独一行 ``ABORT`` 或 EOF 表示立即中止，退出码 42）：
  {"cmd":"render","id":"<uuid>","z":2,"x":0,"y":0,
   "staging":"/data/tiles/regular/.staging/<id>.png"}
  {"cmd":"quit"}

stdout 事件（每行一个 JSON）：
  {"event":"ready"}
  {"event":"start","id":"...","z":2,"x":0,"y":0}
  {"event":"tile","id":"...","z":2,"x":0,"y":0,"staging":"..."}
  {"event":"error","id":"...","message":"..."}   # 随后进程以非 0 退出

发布语义：worker 只把成品写到调度器指定的 staging 路径，是否发布（os.replace 到最终
PNG）或作废（删除）完全由父进程调度器决定——这样“算完先持有、本层出齐再发布/失败作废”
才能在父进程一侧统一实现。

中止协议：渲染耗时用 select 监听 stdin 原始 fd 睡眠，ABORT/EOF 可立即唤醒并以退出码
42 退出，不依赖信号（某些沙箱禁止 kill）；父进程同时会补发 SIGTERM 双保险。
注意：管道下 TextIOWrapper 的行迭代是块缓冲的，单行命令会滞留在子进程缓冲区，
因此这里直接用 os.read(0) + select 自行按行解析。

失败钩子（自测/演示用，触发后进程崩溃，由调度器按失败策略处理）：
  --fail-tile Z/X/Y   渲染指定瓦片时不写文件、上报 error 后以退出码 3 退出
  --fail-after N      成功渲染 N 张后以退出码 3 退出（应急任务用）
"""
from __future__ import annotations

import argparse
import json
import os
import select
import sys
from collections import deque
from pathlib import Path

from PIL import Image

ABORT_RC = 42  # 被父进程中止时的约定退出码


def emit(**kw) -> None:
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


class StdinReader:
    """基于原始 fd 的行读取器，规避管道下 TextIOWrapper 块缓冲导致的命令滞留。"""

    def __init__(self, fd: int = 0):
        self.fd = fd
        self._buf = b""
        self._pending: deque[str] = deque()

    def _pump(self, timeout: float | None) -> bool:
        """读一次 fd；timeout=None 时阻塞。返回 False 表示超时无数据。"""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        chunk = os.read(self.fd, 65536)
        if not chunk:  # EOF：父进程已关闭 stdin
            raise EOFError
        self._buf += chunk
        return True

    def readline(self, timeout: float | None = None) -> str | None:
        """读取一行；超时返回 None；EOF 抛 EOFError。"""
        if self._pending:
            return self._pending.popleft()
        while b"\n" not in self._buf:
            if not self._pump(timeout):
                return None
            timeout = None  # 首次等待后，缓冲区可能仍不足一行，继续阻塞凑齐
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").strip()

    def abort_requested(self, timeout: float) -> bool:
        """渲染睡眠：timeout 内收到 ABORT/EOF 返回 True；其它行缓存待渲染后处理。"""
        try:
            while True:
                line = self.readline(timeout)
                if line is None:
                    return False
                timeout = 0
                if line == "ABORT":
                    return True
                if line:  # 渲染途中不应有派单；若有则缓存，不丢失
                    self._pending.append(line)
        except EOFError:
            return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True, help="瓦片输出根目录（staging 位于其 .staging 子目录）")
    ap.add_argument("--tile", type=int, default=256)
    ap.add_argument("--delay", type=float, default=0.0, help="每张瓦片的模拟渲染耗时(秒)")
    ap.add_argument("--fail-tile", default=None, help="如 2/0/0：渲染该瓦片时模拟崩溃")
    ap.add_argument("--fail-after", type=int, default=0, help="成功渲染 N 张后模拟崩溃")
    a = ap.parse_args()

    fail_tile = None
    if a.fail_tile:
        fz, fx, fy = (int(v) for v in a.fail_tile.split("/"))
        fail_tile = (fz, fx, fy)

    Path(a.out).mkdir(parents=True, exist_ok=True)  # 确保输出根目录存在
    src = Image.open(Path(a.src)).convert("RGB")
    pyramids: dict[int, Image.Image] = {}
    emitted = 0
    reader = StdinReader()
    emit(event="ready")

    while True:
        try:
            line = reader.readline(None)  # 阻塞等待派单
        except EOFError:
            return ABORT_RC
        if line is None:
            continue
        if line == "ABORT":
            return ABORT_RC
        try:
            job = json.loads(line)
        except ValueError:
            emit(event="error", id=None, message=f"bad command line: {line!r}")
            return 1
        if job.get("cmd") == "quit":
            return 0

        z, x, y = int(job["z"]), int(job["x"]), int(job["y"])
        jid = job.get("id")
        staging = Path(job["staging"])
        emit(event="start", id=jid, z=z, x=x, y=y)

        try:
            if fail_tile is not None and (z, x, y) == fail_tile:
                emit(event="error", id=jid,
                     message=f"simulated render failure at tile {z}/{x}/{y}")
                return 3
            if z not in pyramids:
                side = a.tile * (2 ** z)
                pyramids[z] = (
                    src if src.size == (side, side)
                    else src.resize((side, side), Image.LANCZOS)
                )
            # 模拟渲染耗时，期间可被 ABORT 立即唤醒
            if reader.abort_requested(a.delay):
                return ABORT_RC
            img = pyramids[z].crop(
                (x * a.tile, y * a.tile, (x + 1) * a.tile, (y + 1) * a.tile)
            )
            staging.parent.mkdir(parents=True, exist_ok=True)
            img.save(staging, "PNG")  # 仅写 staging；发布权在父进程
            emitted += 1
            emit(event="tile", id=jid, z=z, x=x, y=y, staging=str(staging))
            if a.fail_after and emitted >= a.fail_after:
                emit(event="error", id=jid,
                     message=f"simulated render failure after {emitted} tiles")
                return 3
        except Exception as e:  # 真实渲染异常：上报后崩溃，调度器按失败处理
            emit(event="error", id=jid, message=f"worker error: {e!r}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
