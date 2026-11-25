"""FastAPI 入口：切片值班台。

接口：
  GET  /api/status                     两个任务的进度快照
  POST /api/jobs/{kind}/start          启动切片（kind=regular|emergency；emergency 支持 ?simulate_fail=true 自测失败）
  POST /api/jobs/{kind}/reset          清空已发布瓦片（任务空闲时才允许）
  GET  /tiles/{kind}/{z}/{x}/{y}.png   取已发布瓦片
  GET  /sources/{kind}.png             源图预览
  GET  /                               演示页面
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .scheduler import KINDS, Scheduler
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


@app.get("/api/status")
async def status():
    return sched.snapshot()


@app.post("/api/jobs/{kind}/start")
async def start(kind: str, simulate_fail: bool = False):
    _check(kind)
    job = sched.request_start(kind, simulate_fail=simulate_fail)
    return {"ok": True, "job": job.snapshot()}


@app.post("/api/jobs/{kind}/reset")
async def reset(kind: str):
    _check(kind)
    try:
        job = sched.reset(kind)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "job": job.snapshot()}


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


# 静态页面挂在最后，避免覆盖上面的 API 路由
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
