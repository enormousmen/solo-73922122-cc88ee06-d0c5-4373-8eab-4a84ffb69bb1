#!/usr/bin/env python3
"""自测脚本（仅标准库）：

  测试A 应急插队：常规渲染到一半启动应急 -> 常规立即被中止、在渲瓦片被舍弃，
        应急算完后常规自动续做，已发布瓦片不重算（mtime 不变）、瓦片可取。
  测试B 应急失败后续做：应急模拟失败退出后，常规仍然续做至完成，
        应急已发布的部分瓦片照常可取。

用法：先启动服务（DATA_DIR 与之一致），再运行
  BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 scripts/selftest.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
DATA = Path(os.environ.get("DATA_DIR", "./data"))
TOTAL = 85  # z0..z3


def api(method: str, path: str) -> dict:
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def status() -> dict:
    return api("GET", "/api/status")


def job(s: dict, kind: str) -> dict:
    return s["jobs"][kind]


def wait_for(pred, timeout: float, desc: str, on_poll=None) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = status()
        if on_poll:
            on_poll(s)
        if pred(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"超时未等到：{desc}")


def reset_all() -> None:
    for k in ("regular", "emergency"):
        try:
            api("POST", f"/api/jobs/{k}/reset")
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
    wait_for(
        lambda s: all(j["status"] in ("idle", "done", "failed") for j in s["jobs"].values()),
        10, "重置后回到空闲",
    )


def tile_path(kind: str, t: str) -> Path:
    z, x, y = t.split("/")
    return DATA / "tiles" / kind / z / x / f"{y}.png"


def get_tile(kind: str, t: str) -> bytes:
    z, x, y = t.split("/")
    with urllib.request.urlopen(f"{BASE}/tiles/{kind}/{z}/{x}/{y}.png", timeout=10) as r:
        assert r.status == 200, f"{kind}/{t} HTTP {r.status}"
        return r.read()


def test_preempt() -> None:
    print("== 测试A：应急插队 ==")
    reset_all()
    api("POST", "/api/jobs/regular/start")
    s = wait_for(
        lambda s: job(s, "regular")["published"] >= 8 and job(s, "regular")["current_tile"],
        30, "常规进入在渲状态",
    )
    j = job(s, "regular")
    inflight = tuple(j["current_tile"])
    inflight_key = "/".join(map(str, inflight))
    pre = set(j["tiles"])
    assert inflight_key not in pre, "在渲瓦片不应已发布"
    mtimes = {t: tile_path("regular", t).stat().st_mtime_ns for t in pre}
    print(f"  常规已发布 {len(pre)} 张，在渲 {inflight_key}，此刻受理应急…")

    t0 = time.time()
    api("POST", "/api/jobs/emergency/start")
    s = wait_for(lambda s: s["running_kind"] == "emergency", 5, "应急接管渲染进程")
    dt = time.time() - t0
    assert job(s, "regular")["status"] == "interrupted", "常规应被标记为被抢占"
    print(f"  {dt:.2f}s 内切换到应急，常规状态=interrupted")

    seen = set()

    def sample(s):
        # 只统计应急任务本身存续期间的渲染进程归属
        if job(s, "emergency")["status"] in ("queued", "running"):
            seen.add(s["running_kind"])

    s = wait_for(lambda s: job(s, "emergency")["status"] == "done", 90, "应急完成",
                 on_poll=sample)
    assert job(s, "emergency")["published"] == TOTAL, "应急应完整出图"
    assert seen <= {"emergency"}, f"应急期间不应出现其他渲染进程：{seen}"
    print(f"  应急完成 {TOTAL}/{TOTAL}，期间渲染进程始终为 emergency")

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 90, "常规自动续做至完成")
    j = job(s, "regular")
    assert j["published"] == TOTAL, "常规应最终出齐 85 张"
    for t, m in mtimes.items():
        assert tile_path("regular", t).stat().st_mtime_ns == m, f"已发布瓦片被重算：{t}"
    assert inflight_key in set(j["tiles"]), "被舍弃的在渲瓦片应已重算发布"
    for t in ("0/0/0", "2/3/1", "3/7/7"):
        assert get_tile("regular", t)[:8] == b"\x89PNG\r\n\x1a\n", f"瓦片 {t} 不是合法 PNG"
    print(f"  常规续做完成 {TOTAL}/{TOTAL}；{len(mtimes)} 张已发布瓦片 mtime 未变（未重算）；"
          f"在渲瓦片 {inflight_key} 已重算；抽样瓦片均可取")
    print("  ✔ 测试A 通过")


def test_emergency_failure() -> None:
    print("== 测试B：应急失败后续做 ==")
    reset_all()
    api("POST", "/api/jobs/regular/start")
    wait_for(
        lambda s: job(s, "regular")["published"] >= 5 and job(s, "regular")["current_tile"],
        30, "常规进入在渲状态",
    )
    api("POST", "/api/jobs/emergency/start?simulate_fail=true")
    s = wait_for(lambda s: job(s, "emergency")["status"] == "failed", 60, "应急失败")
    j = job(s, "emergency")
    assert j["published"] == 10, f"应急应发布 10 张后失败，实际 {j['published']}"
    print(f"  应急发布 {j['published']} 张后失败（error={j['error']!r}），等待常规续做…")

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 90, "常规续做至完成")
    assert job(s, "regular")["published"] == TOTAL, "应急失败后常规应出齐 85 张"
    assert get_tile("emergency", "0/0/0")[:8] == b"\x89PNG\r\n\x1a\n", "应急已发布瓦片应仍可取"
    assert get_tile("regular", "3/0/0")[:8] == b"\x89PNG\r\n\x1a\n", "常规瓦片应可取"
    print("  常规续做完成 85/85；应急失败留下的已发布瓦片仍可取")
    print("  ✔ 测试B 通过")


def main() -> int:
    test_preempt()
    test_emergency_failure()
    print("\n全部自测通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
