#!/usr/bin/env python3
"""自测脚本（仅标准库）：覆盖分层调度与应急插队的全部关键约束。

  测试A 跨层出图与名额：观测“下一级先算完→持有→本层出齐才发布”，且在算+待出图合计≤2；
        最终 85/85，瓦片按 HTTP 可取、为合法 PNG，staging 无残留。
  测试B 本层失败 + 重试：fail_tile=2/0/0 时 z0/z1 照常出齐、z2 失败即停派；
        清空失败钩子重试只补缺片（已发布瓦片 mtime 不变）。
  测试C 预渲染延迟失败：fail_tile=3/0/0（z3 第一张，恰为 z2 收尾时的预渲染瓦片），
        z0-z2 完整出齐、z3 零发布，本层出齐后才报告下一级错误。
  测试D 应急插队：常规跑到一半受理应急→常规立即 interrupted、未发布瓦片全舍弃、
        应急全程仅一个渲染子进程；应急结束常规自动续做至 85/85，旧瓦片不重算。
  测试E 应急失败后续做：simulate_fail 应急发布 10 张后失败，常规仍续做至 85/85，
        应急已发布瓦片照常可取。

用法：先启动服务（DATA_DIR 与之一致），再运行
  BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 scripts/selftest.py
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
DATA = pathlib.Path(os.environ.get("DATA_DIR", "./data"))
TOTAL = 85
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def api(method: str, path: str) -> dict:
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def status() -> dict:
    return api("GET", "/api/status")


def job(s: dict, kind: str) -> dict:
    return s["jobs"][kind]


def wait_for(pred, timeout: float, desc: str, on_poll=None):
    t0 = time.time()
    s = None
    while time.time() - t0 < timeout:
        s = status()
        if on_poll:
            on_poll(s)
        if pred(s):
            return s
        time.sleep(0.05)
    raise AssertionError(f"超时未等到：{desc}（最后状态={s and s['jobs']}）")


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
    # 重置后 staging 必须为空
    stg = DATA / "tiles" / "regular" / ".staging"
    assert not list(stg.glob("*.png")), "重置后 staging 仍有残留"


def tile_file(kind: str, t: str) -> pathlib.Path:
    z, x, y = t.split("/")
    return DATA / "tiles" / kind / z / x / f"{y}.png"


def get_tile(kind: str, t: str) -> bytes:
    with urllib.request.urlopen(f"{BASE}/tiles/{kind}/{t}.png", timeout=10) as r:
        assert r.status == 200
        return r.read()


def published_tiles(kind: str) -> int:
    root = DATA / "tiles" / kind
    return len([p for p in root.rglob("*.png") if ".staging" not in str(p)])


def test_cross_layer_and_slots() -> None:
    print("== 测试A：跨层预渲染持有 + 两名额约束 ==")
    reset_all()
    log_file = DATA / "logs" / "regular.log"
    log_before = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
    api("POST", "/api/jobs/regular/start")
    max_used = 0

    def sample(s):
        nonlocal max_used
        j = job(s, "regular")
        max_used = max(max_used, len(j["inflight"]) + len(j["held"]))

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 60,
                 "常规完成", on_poll=sample)
    j = job(s, "regular")
    assert j["published"] == TOTAL, f"应出齐 85，实际 {j['published']}"
    print(f"  完成 {TOTAL}/{TOTAL}；在算+待出图名额峰值={max_used}（要求≤2）")
    assert max_used <= 2, "名额超发"

    # 持有/出齐发布在同一事件循环迭代内完成，HTTP 轮询难捕捉，改以调度日志为确定性证据
    log_after = log_file.read_text(encoding="utf-8")
    new_log = log_after[len(log_before):]
    held = [ln for ln in new_log.splitlines() if "完成并持有" in ln]
    released = [ln for ln in new_log.splitlines() if "出齐，发布持有的预渲染" in ln]
    assert len(held) == 3, f"应在 z0/z1/z2 三个层边界各持有一次，实际 {len(held)}"
    assert len(released) == 3, f"应有三次出齐后发布，实际 {len(released)}"

    def tile_of_tuple(line: str) -> str:
        # 形如 "...预渲染 (2, 0, 0) ..." -> "2/0/0"
        frag = line.split("预渲染 ")[1]
        nums = frag[frag.index("(") + 1:frag.index(")")].split(", ")
        return "/".join(nums)

    # 每次“持有等待”必须早于对应的“本层出齐发布”，且发布的瓦片就是持有的下一级第一张
    for h, r in zip(held, released):
        h_tile = tile_of_tuple(h)
        r_tile = tile_of_tuple(r)
        assert h_tile == r_tile, f"持有的瓦片 {h_tile} 与发布的 {r_tile} 不一致"
        assert new_log.index(h) < new_log.index(r), "持有必须早于出齐发布"
    print(f"  日志确认 3 次跨层持有→等待本层出齐→发布：{[tile_of_tuple(ln) for ln in held]}")

    # 每个缩放层瓦片数正确
    for z, n in ((0, 1), (1, 4), (2, 16), (3, 64)):
        cnt = len([t for t in j["tiles"] if t.startswith(f"{z}/")])
        assert cnt == n, f"z{z} 应有 {n} 张，实际 {cnt}"
    # 抽样 HTTP 可取且为合法 PNG
    for t in ("0/0/0", "2/3/1", "3/7/7"):
        assert get_tile("regular", t)[:8] == PNG_MAGIC, f"瓦片 {t} 不是合法 PNG"
    # staging 无残留
    assert not list((DATA / "tiles/regular/.staging").glob("*.png")), "完成后 staging 残留"
    print("  分层数量正确、抽样 PNG 合法可取、staging 无残留")
    print("  ✔ 测试A 通过")


def test_layer_fail_and_retry() -> None:
    print("== 测试B：本层失败停派 + 重试只补缺片 ==")
    reset_all()
    api("POST", "/api/jobs/regular/start?fail_tile=2/0/0")
    s = wait_for(lambda s: job(s, "regular")["status"] == "failed", 40, "常规失败")
    j = job(s, "regular")
    assert "2/0/0" in (j["error"] or ""), f"错误信息未指向失败瓦片：{j['error']}"
    assert j["published"] == 5, f"z0+z1 应出齐 5 张，实际 {j['published']}"
    assert "2/0/0" not in j["tiles"], "失败瓦片不应被发布"
    assert not [t for t in j["tiles"] if t.startswith("3/")], "z2 失败后不应再渲染 z3"
    z2 = next(L for L in j["layers"] if L["z"] == 2)
    assert z2["status"] == "failed"
    print(f"  z0/z1 出齐 {j['published']} 张后 z2 失败停派，错误={j['error']!r}")

    mtimes = {t: tile_file("regular", t).stat().st_mtime_ns for t in j["tiles"]}
    # 清空失败钩子重试：只补缺片
    api("POST", "/api/jobs/regular/start")
    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 60, "重试完成")
    j = job(s, "regular")
    assert j["published"] == TOTAL
    moved = [t for t, m in mtimes.items()
             if tile_file("regular", t).stat().st_mtime_ns != m]
    assert not moved, f"重试重算了已发布瓦片：{moved}"
    assert "2/0/0" in j["tiles"], "失败瓦片重试后应补齐"
    assert published_tiles("regular") == TOTAL
    print(f"  重试补齐至 {TOTAL}/{TOTAL}；{len(mtimes)} 张旧瓦片 mtime 未变（未重算）")
    print("  ✔ 测试B 通过")


def test_prerender_deferred_failure() -> None:
    print("== 测试C：预渲染先失败，本层出齐后才上报 ==")
    reset_all()
    log_file = DATA / "logs" / "regular.log"
    log_before = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
    api("POST", "/api/jobs/regular/start?fail_tile=3/0/0")
    s = wait_for(lambda s: job(s, "regular")["status"] == "failed", 40, "延迟失败上报")
    j = job(s, "regular")
    assert j["published"] == 21, f"z0-z2 应完整 21 张，实际 {j['published']}"
    assert not [t for t in j["tiles"] if t.startswith("3/")], "z3 不应有任何发布"
    assert "3/0/0" in (j["error"] or "")
    print(f"  z0/z1/z2 完整出齐 {j['published']} 张，z3 零发布；出齐后才报：{j['error']!r}")

    # 确定性时序（日志）：预渲染失败被“延迟”登记 → 本层 z2 出齐 → 才发生任务失败
    new_log = log_file.read_text(encoding="utf-8")[len(log_before):]
    idx_defer = new_log.find("预渲染 (3, 0, 0) 失败，错误延迟到本层 z2 出齐后上报")
    idx_finish_fail = new_log.find("常规任务失败")
    assert idx_defer != -1 and idx_finish_fail != -1, "缺少延迟失败相关日志"
    assert idx_defer < idx_finish_fail, "预渲染失败应早于任务失败上报"
    # 延迟登记与最终上报之间，本层 z2 仍在正常发布（至少一张 z2 瓦片发布夹在中间）
    middle = new_log[idx_defer:idx_finish_fail]
    assert "发布 2/" in middle, "延迟窗口内本层 z2 未照常收尾发布"
    print("  日志确认：z3 预渲染先失败被登记，z2 照常出齐后才上报任务失败")
    print("  ✔ 测试C 通过")


def test_emergency_preempt() -> None:
    print("== 测试D：应急插队 + 常规续做只算缺片 ==")
    reset_all()
    api("POST", "/api/jobs/regular/start")
    s = wait_for(lambda s: job(s, "regular")["published"] >= 15
                 and bool(job(s, "regular")["inflight"]), 30, "常规进入在渲状态")
    rg = job(s, "regular")
    pre_tiles = set(rg["tiles"])
    print(f"  常规已发布 {len(pre_tiles)} 张，在算 "
          f"{[x['tile'] for x in rg['inflight']]}，此刻受理应急…")

    t0 = time.time()
    api("POST", "/api/jobs/emergency/start")
    s = wait_for(lambda s: s["emergency_worker"] is True, 8, "应急接管")
    dt = time.time() - t0
    assert job(s, "regular")["status"] == "interrupted", "常规应被标记 interrupted"
    assert s["regular_workers"] == 0, "常规 worker 应已全部退出"
    print(f"  {dt:.2f}s 内应急接管，常规=interrupted，常规 worker 清零")

    # 受理后未发布 staging 必须被舍弃
    assert not list((DATA / "tiles/regular/.staging").glob("*.png")), "未发布瓦片未舍弃"

    peak, em_running_seen = 0, False

    def sample(s):
        nonlocal peak, em_running_seen
        if job(s, "emergency")["status"] == "running":
            em_running_seen = True
            peak = max(peak, (1 if s["emergency_worker"] else 0) + s["regular_workers"])

    s = wait_for(lambda s: job(s, "emergency")["status"] == "done", 60,
                 "应急完成", on_poll=sample)
    assert job(s, "emergency")["published"] == TOTAL
    assert em_running_seen and peak == 1, f"应急期间渲染进程峰值应为 1，实际 {peak}"
    print(f"  应急完成 {TOTAL}/{TOTAL}，running 期间渲染进程峰值=1（单进程）")

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 60, "常规续做完成")
    j = job(s, "regular")
    assert j["published"] == TOTAL
    assert pre_tiles.issubset(set(j["tiles"])), "应急前已发布瓦片丢失"
    print(f"  常规续做 {TOTAL}/{TOTAL}，应急前 {len(pre_tiles)} 张全部保留")
    print("  ✔ 测试D 通过")


def test_emergency_fail_resume() -> None:
    print("== 测试E：应急失败后常规照常续做 ==")
    reset_all()
    api("POST", "/api/jobs/regular/start")
    wait_for(lambda s: job(s, "regular")["published"] >= 8
             and bool(job(s, "regular")["inflight"]), 30, "常规进入在渲状态")
    api("POST", "/api/jobs/emergency/start?simulate_fail=true")
    s = wait_for(lambda s: job(s, "emergency")["status"] == "failed", 40, "应急失败")
    em = job(s, "emergency")
    assert em["published"] == 10, f"应急应发布 10 张后失败，实际 {em['published']}"
    print(f"  应急发布 {em['published']} 张后失败：{em['error']!r}")

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 60, "常规续做完成")
    assert job(s, "regular")["published"] == TOTAL
    assert get_tile("emergency", "0/0/0")[:8] == PNG_MAGIC, "应急已发布瓦片应仍可取"
    print(f"  常规续做 {TOTAL}/{TOTAL}；应急已发布瓦片仍为合法 PNG 可取")
    print("  ✔ 测试E 通过")


def main() -> int:
    tests = [
        test_cross_layer_and_slots,
        test_layer_fail_and_retry,
        test_prerender_deferred_failure,
        test_emergency_preempt,
        test_emergency_fail_resume,
    ]
    for t in tests:
        t()
    print("\n全部自测通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
