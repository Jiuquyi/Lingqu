# 计量、恢复与容量的可靠性

本文记录 0.9.0 对计量生命周期、账本、恢复和进程容量的约定，以及为下一步调参准备的 shadow 观察项。问题来源和分阶段计划见 [2026-10-22 评审](reviews/2026-10-22-analysis.md)，逐项处理结果见 [处理记录](reviews/2026-10-22-resolution.md)。升级前运行 `flask deploy`（迁移到 `0005`）。

## 计量生命周期

一个计量请求从拿到上游第一个字节到写出账本行，由 `Meter`（`app/proxy/views.py`）负责：

- 每一块先交给客户端，再解析副本。客户端在任意位置断开时，已经交出去的那一块在 `settle()` 里补解析，所以从上游读到的 usage 都会保留，客户端收到的字节不变。
- `settle()` 只执行一次：在响应体生成器的 `finally` 里，或者在生成器从未启动时，由响应的 close 执行。
- `direct_passthrough` 的响应会被 WSGI 服务器直接拿去迭代，`Response.call_on_close` 不会执行。所以 close 钩子挂在可迭代对象上（`ClosingIterator`）。旧代码在首块之前断开时，既不释放并发名额，也不关闭上游连接。
- 客户端中途断开的请求记为 `error_class = client_closed`：它保留在 `/stats` 的 `error_classes` 里，但不计入 `errors`，也不影响连续错误告警。
- 解析器异常不断流：该请求停止解析，行记为 `estimated`。

## 计量失败与补偿

`gate.charge` 失败（例如 Redis 不可用）时：

- 流照常转发，账本行照常写；`eusd` 是观察到的金额，`eusd_pending` 是还没进窗口的部分。
- 失败的那笔按原来的 key 放进补偿队列（上限 10000），由 gate 线程每 15 秒按顺序重试，遇到失败就停。
- 提交已经发出、但应答丢了的（`CommitUnconfirmed`），只在 gate 还记得这个 key 的 900 秒内重试；过期后记一条 `charge_lost` 事件并告警，因为这时已经分不清是不是重复。账本行里仍有这笔金额，重启恢复时会算进去。
- 新准入的失败策略不变：Redis 模式下 fail-closed，内存模式按 `fail_mode`。

`/status` 的 `metering`：`pending`、`pending_eusd`、`pending_oldest_sec`、`charges`（`failed` / `recovered` / `lost`），以及 `quality_1h`。`quality_1h` 按池和协议统计 usage 的来源（`upstream` / `estimated` / `none`）、各来源的金额，以及用默认价格计费的请求数和金额。

## 幂等

| 层 | 标识 | 去重方式 |
|---|---|---|
| 闸门计费 | `<request_id>:<seq>`，seq 1 是输入，2 是其余部分 | gate 记住 900 秒内已经计入的 key（Redis 模式写进共享状态）。重复的 key 不再计入，返回第一次的计费时间 |
| 账本行 | `ledger.request_id`（唯一，`0005` 之前的行为空） | `INSERT … ON CONFLICT DO NOTHING`（SQLite、PostgreSQL） |
| 计量事件 | `meter_events (request_id, seq)`（唯一） | 同上 |

所以，一次提交成功、应答却丢了的 flush 重试，不会把账本行或计量事件写两遍。计费时间在 counters 锁内读取：每 60 秒一次的计数快照，要么包含整笔，要么完全不包含。

## 恢复与 RPO

- `message_start` 算出的输入金额，在计入窗口的同时写一条 `meter_events`。
- 重启恢复的来源：计数快照，加快照之后的账本行（与之前相同），再加没有账本行的计量事件，也就是进程死掉时还在跑的请求。在 gunicorn 下实测：SIGKILL 整个进程组之后，那次请求的 $3.00005 输入重启后回到窗口，只计一次。
- 已结算请求的计量事件，2 小时后由每小时一次的清理删掉；孤儿事件保留，因为它们是那些请求唯一的记录。

**RPO**：数据库可写时，已观察到的 usage 在下一次 flush（≤ 1 秒）之后就能恢复。数据库不可写期间，积压在内存里（每个队列最多 10000 条）；这时进程崩溃，这部分就丢了。上游还没报告的消耗（主要是输出）无法恢复，要靠事后对账。

**readiness**：恢复失败时，`/healthz` 返回 503，`health.recovery.state = failed`，闸门拒绝计费请求（`lingqu is rebuilding its windows from the ledger`），gate 线程每 15 秒重试。内存模式在一个新的 Gate 上重建，成功后整体替换，所以重试不会把上一次的部分结果再加一遍。

**Redis**：共享状态里带一个 epoch。

- 实例见过 epoch 之后又发现状态没了，说明 Redis 丢了状态（比如重启时没有持久化）。这时该实例拒绝准入，不会把预算当成新周期从零开始。
- 由一个实例拿到 `:restore` 锁，从数据库重建（`rebuilt`，并发 `shared_state_lost` 告警），其他实例等它重建完。集群第一次启动也同样从数据库建（`built`）。
- epoch 不能只放在 Redis 里：Redis 丢数据时它会和状态一起丢。

## 账本积压

- 每个条目都带入队时间。`lag` 是最老的未写条目的年龄，包括正在写的那一批，写失败回填后也不会被重置。实测：数据库连续 180 秒不可写，`lag` 是 180 秒，`/healthz` 在 120 秒后转为不健康。
- `/healthz` 的 `ledger`：`lag_sec`、`queued`（按队列）、`dropped`（按队列）、`dropped_total`、`last_success`、`failures`（连续失败次数）、`failing_sec`、`last_error`。
- 行、事件、观测和计量事件四个队列用同一个上限、同一套丢弃计数；写失败回填时也按这个上限裁剪。

## 进程容量

| 配置 | 默认 | 含义 |
|---|---|---|
| `server.max_active` | 0，即 worker 线程数 − 8 | 进程里同时存在的代理请求数，包括挂起中的 |
| `server.max_body_in_flight_mb` | 256 | 同时留在内存里的请求体总量。httpx 会一直持有请求体，直到响应结束 |

超过任一项的请求，在读请求体之前就得到 503（`lingqu: busy, …`），不会再去上游。被拒绝的次数在 `/healthz` 和 `/status` 的 `capacity.rejected_1h` 里；每分钟最多写一条 `capacity_reject` 事件，并告警一次。

gunicorn 启动时会检查 `max_active ≤ 线程数 − 2`，保证状态端口总有线程可用。实测 8 个线程、`max_active: 4`：4 条长流占满之后，第 5 个请求 1 毫秒内拿到 503，同时 `/healthz` 1 毫秒内返回 200。单个请求体的上限仍由 `LINGQU_MAX_BODY_MB` 控制（默认 128）。

## Shadow 观察项

下面两项都只记录、不执行，用来决定下一步调参。

**预算预留**：在 `hourly_cap` / `hourly_budget` 上加 `reserve: shadow`（`reserve_quantile` 默认 0.75）。

- 金额闸门只看到已经到达的计费。从请求放行到 `message_start` 之间，它的输入费用是看不见的，同时放行的几个请求可能一起越过限额。
- 预留按同一个池、同一种缓存状态（warm / cold / none）最近 200 个请求的输入费用分位数，估计这个请求的输入，持有到 `message_start` 结算为止（最长 10 分钟）。样本不足 10 个时退回整个池的样本；仍然不足就不估计。
- `/status` 的 `pools[].reservation` 给出：`reserved`、`in_flight`、`would_hold_1h`（"已观察 + 预留"会挡住、实际却放行了的请求数），以及 `error_1h`（`bias` 为估计减实际的平均值，`mae`，`p90_abs`，`under_share` 为低估的比例）。
- `reserve: enforce` 目前直接拒绝加载：估计误差和缓存重建的代价验证之前，不让它影响放行。

**并发上限并排对比**：同一个池可以配多条 `max` 不同的 shadow `concurrency` 规则。`/status` 的 `concurrency.would_hold_by_rule_1h` 按规则分别计数，例如 `{"concurrency<=2": 14, "concurrency<=3": 3}`。

## 第四阶段（Redis 拆分、集中采集、ASGI）的触发条件

当前部署是单副本 + SQLite，这三项都以出现容量需求为前提，现在不做。触发它们的信号已经能看到：

| 信号 | 位置 | 说明 |
|---|---|---|
| `capacity.rejected_1h.active` 持续非零，或 `peak_active_1h` 接近 `max_active` | `/healthz` | 线程成了瓶颈：先确认是长流或排队太多，再考虑 ASGI 外壳 |
| `coordination.lock.hold_ms.p99` > 20 ms，或 `state_bytes` > 1 MB | `/healthz`（Redis 模式） | 共享状态需要拆成热状态和长历史两部分 |
| 多实例时额度或管理器请求翻倍 | 9router 日志 | 需要采集者租约 |

Redis 模式下，`/healthz` 不再走读写事务：只做 ping、确认状态存在、比较独立的 `:signature` 键。配置指纹每份配置只计算一次。

Redis 的集成测试需要 `redis-server` 在 `PATH` 里；本地验证用的是从源码编译的 7.2.5，测试全部执行通过。
