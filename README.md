# 灵渠（Lingqu）

架在 New API 和 9router 之间的流控代理：把每次请求的 token 按价格表折算成等价美元（eUSD），按"池"（一个订阅账户的一类上游）分别核算；超过每小时限额时，请求在灵渠里排队、按间隔匀速放出，而不是报错。设计见 [docs/DESIGN.md](docs/DESIGN.md)。

当前实现的是里程碑 M1 + M2（DESIGN §14）：

| 已实现 | 推后 |
|---|---|
| 按 key / 按模型名分池；`/v1/*` 透传，请求体一个字节不改，只换 key | 会话感知排队（M3） |
| SSE 逐块透传；三种协议的 usage 解析，Anthropic 在 `message_start` 就计入 | 动态小时预算、周期上限、上游额度采集（M4） |
| 价格从 New API / 9router / 静态配置同步，失败时沿用上次 | `downgrade` |
| `hourly_cap` + `fifo`（默认）/ `phase`（对照）；队首超时、`max_hold`、`max_queue`；并发、RPM；`shadow` | 控制台、9router 管理器、多实例（M5） |
| 账本、限速事件、重启恢复、热重载、管理员覆盖、webhook 告警 | |
| `/status`、`/stats`、`/events`、`/healthz`；状态端口 `/` 的只读状态页（池、计量、限速事件） | |
| 9router Dashboard 入口：关闭 / 临时 / 保持打开、opener token、开关页、审计 | |
| `flask check`、`flask replay`、`flask dashboard`、`flask pricing`、`flask ledger export`、`flask counters reset` | |

## 本地运行

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements/dev.txt
cp lingqu.example.yaml lingqu.yaml        # 按部署改，密钥用 ${ENV}
export FLASK_APP=lingqu.py LINGQU_CONFIG=$PWD/lingqu.yaml
.venv/bin/flask deploy                     # 建表 / 迁移
.venv/bin/flask check                      # 核对配置和整条链路
.venv/bin/gunicorn -c gunicorn.conf.py lingqu:application
```

- 一个 gunicorn worker（gthread，默认 64 线程）监听配置里的所有端口：代理（8090）、状态与管理（8091）、每个 Dashboard 入口端口。哪个端口进来的请求就只能访问对应的那部分。
- worker 数必须是 1：窗口、出队时刻、排队队列都在进程内存里。k8s 用 `replicas: 1` + `strategy: Recreate`，`terminationGracePeriodSeconds: 600`。
- `SIGTERM`：挂起中的请求立即返回 503，正在进行的流跑完（最多 600 秒），退出前写完账本。

## 测试

```bash
FLASK_APP=lingqu.py LINGQU_CONFIG=lingqu.example.yaml .venv/bin/flask test     # 或 python -m unittest discover tests
```

闸门用假时钟测；代理用一个本地假 9router 端到端测（透传、计量、错误分类、端口隔离、Dashboard 入口）。

## 回放

```bash
flask replay --source ledger --pool team-a/claude --from 2026-10-14 --to 2026-10-21 --policy policy.yaml
```

数据源可以是灵渠的账本、New API 的消费日志（`--source newapi`，需要 `NEWAPI_ACCESS_TOKEN`），或者一个 jsonl 文件（每行 `t, session, model, cost, cost_input, ttfb, dur, cache_read`）。`policy.yaml` 是一条策略，或者 `policies:` 列表：

```yaml
policies:
  - { name: "phase + 503 retry", release: phase, on_exceed: reject, visible: log }
  - { name: "fifo x1.5", release: fifo, scale: 1.5 }
```

## 依赖锁定

`requirements/*.in` 是直接依赖，`scripts/update_locks.sh` 用 pip-tools 生成 `*.txt`。镜像只装 `docker.txt`。
