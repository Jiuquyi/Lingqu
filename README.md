# 灵渠（Lingqu）

架在 New API 和 9router 之间的流控代理：把每次请求的 token 按价格表折算成等价美元（eUSD），按"池"（一个订阅账户的一类上游）分别核算；超过每小时限额时，请求在灵渠里排队、按间隔匀速放出，而不是报错。设计见 [docs/DESIGN.md](docs/DESIGN.md)。

当前实现覆盖里程碑 M1 ～ M5（DESIGN §14）。M5 的配置、升级和多实例边界见 [docs/M5.md](docs/M5.md)：

| 已实现 | 推后 |
|---|---|
| 按 key / 按模型名分池；`/v1/*` 透传，请求体一个字节不改，只换 key | |
| SSE 逐块透传；三种协议的 usage 解析，Anthropic 在 `message_start` 就计入 | `downgrade` |
| 价格从 New API / 9router / 静态配置同步，失败时沿用上次 |  |
| `hourly_cap` + `fifo`（默认）/ `phase`（对照）；队首超时、`max_hold`、`max_queue`；并发、RPM；`shadow` | |
| M3：会话感知的 `fifo`（`session_aware: true`，热请求赶在缓存过期前放行，冷请求排后面；默认关闭，按池回放后打开，DESIGN §4.5.1）；并发上限先 shadow、空出的名额按等待先后分配 | |
| M4：9router 额度采集、响应头补充；`hourly_budget`、168 小时权重、`cycle_cap`；90% 最低预算 / 98% 拒绝；预算与额度快照、长周期恢复（[配置及边界](docs/M4.md)） | |
| 账本（含每个请求的 `cache_state`）、限速事件、重启恢复（窗口和会话时钟）、热重载、管理员覆盖、webhook 告警 | |
| 错误分类（含流里的错误、连不上 9router）；`account_auth` / `router_unreachable` 连续出现时告警，恢复时再推一条 | |
| `/status`、`/stats`、`/events`、`/healthz`；状态端口 `/` 的只读状态页（池、计量、限速事件） | |
| M5：Jinja2 + 本地 Bootstrap 控制台、9router 库存/健康/额度同步与对账、模型优先级/歧义/回退、Redis 全局准入、可信用户级限流 | |
| 9router Dashboard 入口：关闭 / 临时 / 保持打开、opener token、开关页、审计 | |
| `flask check`、`flask replay`（含会话感知、并发上限、冷 / 热判断核对）、`flask dashboard`、`flask pricing`、`flask ledger export`、`flask counters reset`；`scripts/replay_synth.py` 合成负载 | |

## 本地运行

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements/dev.txt
cp lingqu.example.yaml lingqu.yaml        # 按部署改，密钥用 ${ENV}
export FLASK_APP=lingqu.py LINGQU_CONFIG=$PWD/lingqu.yaml
.venv/bin/flask deploy                     # 建表 / 迁移
.venv/bin/flask check                      # 核对配置和整条链路
.venv/bin/gunicorn -c gunicorn.conf.py lingqu:application
```

- 默认一个 gunicorn worker（gthread，64 线程）监听配置里的所有端口：代理（8090）、状态与管理（8091）、每个 Dashboard 入口端口。哪个端口进来的请求就只能访问对应的那部分。
- 未配置 Redis 时，worker 和副本数必须是 1，k8s 用 `replicas: 1` + `strategy: Recreate`。启用 Redis 后可用 `LINGQU_WORKERS` 或多个副本，需共享 PostgreSQL、同一份配置与 `SECRET_KEY`；[compose.redis.yaml](compose.redis.yaml) 提供双副本部署模板。退出宽限时间均为 600 秒。
- `SIGTERM`：挂起中的请求立即返回 503，正在进行的流跑完（最多 600 秒），退出前写完账本。

控制台：状态端口 `/console`，用管理员 token 登录。共享模型 key 未命中默认拒绝；旧配置如需继续仅计量 unknown 池，必须显式设置 `unmatched: unknown`。升级先运行 `flask deploy`，迁移到 `0004`。

## 测试

```bash
FLASK_APP=lingqu.py LINGQU_CONFIG=lingqu.example.yaml .venv/bin/flask test     # 或 python -m unittest discover tests
```

闸门用假时钟测；代理用一个本地假 9router 端到端测（透传、计量、错误分类、端口隔离、Dashboard 入口）。

## 回放

```bash
flask replay --source ledger --pool team-a/claude --from 2026-10-14 --to 2026-10-21 --policy policy.yaml
```

数据源可以是灵渠的账本、New API 的消费日志（`--source newapi`，需要 `NEWAPI_ACCESS_TOKEN`），或者一个 jsonl 文件（`--source jsonl --jsonl <文件>`，每行 `t, session, model, cost, cost_input, ttfb, dur, cache_read, cache_write`；可以不给 `--from` / `--to`）。`policy.yaml` 是一条策略，或者 `policies:` 列表：

```yaml
policies:
  - { name: "phase + 503 retry", release: phase, on_exceed: reject, visible: log }
  - { name: "fifo x1.5", release: fifo, scale: 1.5 }
  - { name: "sessions", release: fifo, session_aware: true }
  - { name: "fifo + concurrency 3", release: fifo, concurrency: 3 }
```

### 打开会话感知之前

```bash
# 用池自己的账本回放 M3 的对比集；先看"冷 / 热判断"那几行，再看各策略的峰值和等待
.venv/bin/flask replay --source ledger --pool team-a/claude --from 2026-10-14 --to 2026-10-21 \
    --policy docs/replay/m3.yaml
# 没有账本时，看看人更多时会怎样（合成负载，5 个随机种子取平均）
.venv/bin/python scripts/replay_synth.py --users 4 --limit 30 --seeds 5 --policy docs/replay/m3.yaml
```

回放里会话感知的 60 分钟峰值或热请求等待明显好于 `fifo` 时，再在该池的 `hourly_cap` 上加 `session_aware: true`，`POST /admin/reload` 生效。

## 依赖锁定

`requirements/*.in` 是直接依赖，`scripts/update_locks.sh` 用 pip-tools 生成 `*.txt`。镜像只装 `docker.txt`。
