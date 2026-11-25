# 切片值班台（常规底图 / 应急航片 · 应急插队）

FastAPI + Pillow 的切片演示服务：内置常规底图、应急航片各一张（2048×2048，启动时自动生成），
值班员可分别启动切片、查看进度、打开已发布的 PNG 瓦片；应急任务受理后立即抢占常规渲染。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements.txt
DATA_DIR=./data TILE_DELAY=0.15 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

环境变量：`DATA_DIR`（源图/瓦片/日志目录，默认容器内 `/data`）、`TILE_DELAY`（每张瓦片模拟渲染耗时秒数，默认 0.15，调大可放慢演示节奏）。

## 页面演示（渲染到一半插队）

1. 点「启动切片」→ 常规底图进度条开始推进，瓦片逐张点亮；
2. 渲染到一半时点「启动应急切片（插队）」→ 常规立即变为「被抢占·待续算」，应急开始渲染；
3. 应急完成后常规自动续做（本轮计划数 = 剩余未发布张数，已发布的不重算）；
4. 勾选「模拟渲染失败」再启动应急，可演示应急失败后常规照常续做；
5. 点击任意已点亮瓦片可在新标签页打开对应 PNG；「重置」清空该任务已发布瓦片（任务空闲时才允许）。

## 实现内容

- **任务模型**（`app/scheduler.py`）：常规 / 应急两个任务，各 85 张瓦片（z0–z3，256px）。
  状态机：`idle → queued → running → done / failed / interrupted`；`interrupted` 的常规任务在应急结束后被调度器自动续做。
- **单渲染子进程**：调度主循环串行执行 `_run()`，同一时刻最多一个 `worker.py` 子进程；
  应急受理时若常规在跑，先中止旧进程并 `await proc.wait()`，确认退出后才启动应急进程。
- **抢占协议**：父进程向子进程 stdin 写 `ABORT` 指令（worker 用 `select` 监听 stdin，
  渲染睡眠可被立即唤醒，收到指令或 EOF 即以约定退出码 42 退出），同时补发 SIGTERM 双保险。
  不依赖信号即可工作（某些沙箱环境禁止 kill），正常 Linux / Docker 中两条通道都有效。
- **舍弃与重算**：瓦片先写 `.tmp` 再 `os.replace` 原子发布；被中止时当前瓦片尚未发布，
  残存 `.tmp` 由下次启动的 worker 清理，该瓦片在续做时重算。worker 跳过已存在的 PNG，
  已发布瓦片不重算；父进程每轮结束按磁盘重新对齐已发布集合。
- **应急失败钩子**：`POST /api/jobs/emergency/start?simulate_fail=true`，worker 发布 10 张后以退出码 3 模拟崩溃，用于自测与演示。
- **API**：
  - `GET /api/status` 两任务进度快照（状态、百分比、在渲瓦片、本轮计划、已发布清单）；
  - `POST /api/jobs/{kind}/start`（kind=regular|emergency，幂等；emergency 支持 `simulate_fail`）；
  - `POST /api/jobs/{kind}/reset`（任务进行中返回 409）；
  - `GET /tiles/{kind}/{z}/{x}/{y}.png`（未发布 404）；
  - `GET /sources/{kind}.png` 源图预览；`GET /` 演示页面。
- **源图**（`app/sources.py`）：启动时用 Pillow 生成两张 2048×2048 区分度高的测试图（渐变 + 网格 + 坐标标注），已存在则不覆盖。

## 实测结果

环境：Python 3.11 / FastAPI 0.165 / Pillow 13，`TILE_DELAY=0.1`。自测脚本 `scripts/selftest.py`（仅标准库）：

```
== 测试A：应急插队 ==
  常规已发布 8 张，在渲 2/0/3，此刻受理应急…
  0.11s 内切换到应急，常规状态=interrupted
  应急完成 85/85，期间渲染进程始终为 emergency
  常规续做完成 85/85；8 张已发布瓦片 mtime 未变（未重算）；在渲瓦片 2/0/3 已重算；抽样瓦片均可取
  ✔ 测试A 通过
== 测试B：应急失败后续做 ==
  应急发布 10 张后失败（error='simulated render failure after 10 tiles'），等待常规续做…
  常规续做完成 85/85；应急失败留下的已发布瓦片仍可取
  ✔ 测试B 通过
```

另核验：

- 渲染期间 `ps` 实测 worker 进程数恒为 1；抢占后唯一进程即应急 worker（常规进程已退出）；
- `GET /`、`/app.js`、`/style.css`、`/sources/{kind}.png` 均 200；
- 已发布瓦片 200 且为合法 PNG，未发布瓦片 404；
- 复跑：`BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 scripts/selftest.py`。

## 未完成项 / 说明

- 本交付环境无 Docker 守护进程，`docker compose up --build` 未在本机实际构建执行；
  Dockerfile / compose 为标准写法，应用本体已在容器外以相同命令（uvicorn 单进程）实测通过。
- 调度状态在内存中，必须单 uvicorn worker 运行（Dockerfile 已固定单进程）；服务重启后已发布瓦片保留，
  但未完成任务需重新启动（不自动续跑）。
- 渲染耗时用 `TILE_DELAY` 模拟，未接真实切图负载；瓦片方案为单源图 z0–z3 固定金字塔，未做 XYZ 坐标系对齐与多比例尺重投影。
- 未做鉴权与并发操作限流（重复启动幂等，重置仅做空态校验）。
