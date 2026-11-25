#!/usr/bin/env python3
"""独立渲染子进程：只依赖标准库 + Pillow，全部配置经命令行参数传入。

协议（stdout 每行一个 JSON）：
  {"event":"plan","total":N,"remaining":M}
  {"event":"start_tile","z":z,"x":x,"y":y}
  {"event":"tile","z":z,"x":x,"y":y,"published":k}
  {"event":"error","message":"..."}
  {"event":"done","published":k}

发布语义：先写 .tmp 再 os.replace 原子改名，读者只会看到完整瓦片；
被中止时当前瓦片尚未发布（只剩 .tmp，下次启动时清理）。
已存在的 PNG 直接跳过 => 已发布瓦片不重算。

中止协议：父进程通过 stdin 写入任意字节（或关闭 stdin）即要求立即中止，
worker 以退出码 42 退出；渲染耗时用 select 模拟，可被 stdin 立即唤醒，
不依赖信号（某些沙箱环境禁止发信号）。父进程也可能直接 SIGTERM。
"""
import argparse
import json
import os
import select
import sys
from pathlib import Path

from PIL import Image

ABORT_RC = 42  # 被父进程中止时的约定退出码


def emit(**kw) -> None:
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def _abort_requested(timeout: float) -> bool:
    """等待 timeout 秒；期间 stdin 出现数据或 EOF（父进程要求中止/已退出）返回 True。"""
    r, _, _ = select.select([sys.stdin], [], [], max(timeout, 0))
    return bool(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tile", type=int, default=256)
    ap.add_argument("--zooms", default="0,1,2,3")
    ap.add_argument("--delay", type=float, default=0.0, help="每张瓦片的模拟渲染耗时(秒)")
    ap.add_argument("--fail-after", type=int, default=0, help="发布 N 张后模拟失败退出(自测用)")
    a = ap.parse_args()

    zooms = [int(z) for z in a.zooms.split(",") if z.strip()]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for tmp in out.rglob("*.tmp"):  # 清理上次被中止留下的半成品
        tmp.unlink(missing_ok=True)

    src = Image.open(a.src).convert("RGB")
    plan = [(z, x, y) for z in zooms for x in range(2 ** z) for y in range(2 ** z)]

    def final_of(t):
        z, x, y = t
        return out / str(z) / str(x) / f"{y}.png"

    remaining = [t for t in plan if not final_of(t).is_file()]
    emit(event="plan", total=len(plan), remaining=len(remaining))

    published = 0
    pyramids: dict[int, Image.Image] = {}
    for (z, x, y) in remaining:
        if _abort_requested(0):
            emit(event="aborted", published=published)
            return ABORT_RC
        emit(event="start_tile", z=z, x=x, y=y)
        if z not in pyramids:
            side = a.tile * (2 ** z)
            pyramids[z] = src if src.size == (side, side) else src.resize((side, side), Image.LANCZOS)
        img = pyramids[z].crop((x * a.tile, y * a.tile, (x + 1) * a.tile, (y + 1) * a.tile))
        if _abort_requested(a.delay):  # 模拟渲染耗时；此时被中止则该瓦片未发布
            emit(event="aborted", published=published)
            return ABORT_RC
        final = final_of((z, x, y))
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_suffix(".png.tmp")
        img.save(tmp, "PNG")
        os.replace(tmp, final)  # 原子发布
        published += 1
        emit(event="tile", z=z, x=x, y=y, published=published)
        if a.fail_after and published >= a.fail_after:
            emit(event="error", message=f"simulated render failure after {published} tiles")
            return 3
    emit(event="done", published=published)
    return 0


if __name__ == "__main__":
    sys.exit(main())
