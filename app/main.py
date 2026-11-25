"""FastAPI 入口：切片值班台。

接口：
  GET  /api/status                     两个任务的进度快照（含在算/持有/分层状态）
  POST /api/jobs/{kind}/start          启动切片（kind=regular|emergency）
       - emergency 支持 ?simulate_fail=true：发布 10 张后模拟崩溃
       - regular  支持 ?fail_tile=Z/X/Y：指定瓦片渲染失败（演示分层失败/预渲染延迟上报）
  POST /api/jobs/{kind}/reset          清空已发布瓦片（任务空闲时才允许）
  GET  /tiles/{kind}/{z}/{x}/{y}.png   取已发布瓦片
  GET  /sources/{kind}.png             源图预览
  GET  /                               演示页面
"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .scheduler import KINDS, TOTAL, ZOOMS, Scheduler
from .sources import ensure_sources

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
TILE_DELAY = float(os.environ.get("TILE_DELAY", "0.15"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

sched = Scheduler(DATA_DIR, TILE_DELAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_sources(DATA_DIR)
    sched.start()
    yield
    await sched.shutdown()


app = FastAPI(title="应急切片插队服务", lifespan=lifespan)


def _check(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(404, "unknown job kind")


def _parse_fail_tile(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    m = re.fullmatch(r"(\d+)/(\d+)/(\d+)", raw.strip())
    if not m:
        raise HTTPException(400, "fail_tile 格式应为 Z/X/Y，如 2/0/0")
    z, x, y = (int(v) for v in m.groups())
    if z not in ZOOMS or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(400, f"fail_tile {raw} 超出瓦片范围")
    return z, x, y


@app.get("/api/status")
async def status():
    return sched.snapshot()


@app.post("/api/jobs/{kind}/start")
async def start(kind: str, simulate_fail: bool = False, fail_tile: str | None = None):
    _check(kind)
    tile = _parse_fail_tile(fail_tile)
    if tile is not None and kind != "regular":
        raise HTTPException(400, "fail_tile 仅用于常规底图的分层失败演示")
    if simulate_fail and kind != "emergency":
        raise HTTPException(400, "simulate_fail 仅用于应急任务")
    sched.request_start(
        kind, simulate_fail=simulate_fail,
        fail_tile=tile if kind == "regular" else None,
    )
    return {"ok": True, "job": sched.snapshot()["jobs"][kind]}


@app.post("/api/jobs/{kind}/reset")
async def reset(kind: str):
    _check(kind)
    try:
        sched.reset(kind)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "job": sched.snapshot()["jobs"][kind]}


@app.get("/tiles/{kind}/{z}/{x}/{y}.png")
def tile(kind: str, z: int, x: int, y: int):
    _check(kind)
    if z < 0 or z > 10 or x < 0 or y < 0 or x >= 2 ** z or y >= 2 ** z:
        raise HTTPException(404, "tile out of range")
    p = DATA_DIR / "tiles" / kind / str(z) / str(x) / f"{y}.png"
    if not p.is_file():
        raise HTTPException(404, "tile not published")
    return FileResponse(p, media_type="image/png")


@app.get("/sources/{kind}.png")
def source(kind: str):
    _check(kind)
    p = DATA_DIR / "sources" / f"{kind}.png"
    if not p.is_file():
        raise HTTPException(404, "source missing")
    return FileResponse(p, media_type="image/png")


@app.get("/api/meta")
async def meta():
    return {"zooms": ZOOMS, "total": TOTAL, "tile_delay": TILE_DELAY}


# 静态页面挂在最后，避免覆盖上面的 API 路由
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
