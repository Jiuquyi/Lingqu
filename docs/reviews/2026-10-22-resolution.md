# 2026-10-22 评审：处理记录

对应 [评审](2026-10-22-analysis.md) 第九节的分阶段计划。代码基于 `7b7606e`，改动后版本为 0.9.0（迁移 `0005`）。行为约定见 [RELIABILITY.md](../RELIABILITY.md)。

验证：`python -m unittest discover -s tests`，193 项全部通过，0 项跳过。其中 Redis 集成测试用本地编译的 redis-server 7.2.5 执行，原来跳过的 11 项也在内；新增 32 项。另外在 gunicorn 下跑了一次真实进程测试：8 线程，真实 socket，SIGKILL 整个进程组。

## 第一批：三个已复现缺陷

| 问题 | 处理 | 验收 |
|---|---|---|
| 首块交给客户端后断开，输入和缓存漏计 | 引入 `Meter`：先交给客户端，再在 `settle()` 里解析；交出的块在断开时补解析；`settle()` 只执行一次 | `test_hanging_up_at_any_chunk_keeps_what_was_read`：在每个 chunk 位置断开，客户端字节不变，已观察的 usage 保留在账本行和窗口里。gunicorn 实测：`message_start` 后断开，行内 600 万缓存读取、$3.00005 输入，`client_closed` |
| 同上，评审时没有发现的部分 | `direct_passthrough` 下 `call_on_close` 从不执行。首块之前断开时，旧代码不调用 `gate.finish`，并发名额永久泄漏，上游连接也不关。close 钩子改挂在可迭代对象上 | `test_body_never_started_still_writes_its_row` |
| 计量后端失败导致断流、丢账 | 计费失败不再抛出：账本行照写，`eusd_pending` 记录没进窗口的部分，按原 key 补偿；`counted` 在提交成功后才赋值；结算出错也保证写行、释放名额 | `test_gate_down_keeps_the_stream_and_the_row`、`test_unconfirmed_commit_is_not_counted_twice`、`test_parser_failure_does_not_cut_the_stream` |
| 数据库持续不可写，积压年龄被反复重置 | 每个条目带入队时间；`lag` 包含正在写的那一批；新增 `last_success`、`failures`、`failing_sec`、`last_error`、按队列的 `dropped`；四个队列同一上限、同一计数 | `test_backlog_age_survives_failed_writes`：60 秒时 lag = 60 且仍健康，180 秒时 lag = 180 且不健康，恢复后归零 |

## 第二批：幂等、恢复、容量、配置

| 项 | 处理 | 验收 |
|---|---|---|
| 幂等计量 | 计费 key `request_id:seq`，gate 记住 900 秒（Redis 写进共享状态）；`ledger.request_id`、`meter_events(request_id, seq)` 唯一，insert-or-ignore；计费时间在 counters 锁内读取 | 同一批重复 flush 只存一份；Redis 上提交已生效、应答丢失后，由另一个实例重试，只计一次 |
| 崩溃窗口与 RPO | 输入金额写 `meter_events`；恢复时补上没有账本行的事件；RPO 写进 RELIABILITY.md | gunicorn 下 SIGKILL：孤儿事件 1 条，重启后窗口从 $15.0627 变为 $18.0628（+$3.00005，只计一次） |
| 恢复状态 | 恢复失败时不 ready、计费请求拒绝、每 15 秒重试；内存模式在新 Gate 上重建，避免重复叠加 | `ReadinessTest` |
| Redis 丢状态 | 共享状态带 epoch；见过 epoch 的实例发现状态缺失就拒绝准入；单个实例拿锁从数据库重建（`built` / `rebuilt`） | `SharedRecoveryTest`（真实 Redis） |
| 全局容量保护 | `server.max_active`（默认线程数 − 8）、`max_body_in_flight_mb`（默认 256），在读请求体之前判断；gunicorn 启动检查留够状态端口的线程 | gunicorn 实测：第 5 个请求 1 毫秒内 503，`/healthz` 1 毫秒内 200 |
| 基础指标 | `/healthz`：`ledger`、`capacity`、`recovery`；`/status`：`metering`（pending、补偿计数、按池和协议的 usage 来源与默认价格占比） | — |
| 配置生成链（MachGate） | `machgate.py`：新配置先交给运行中的灵渠校验，需要重启才能生效的改动直接拒绝；旧配置存到 `lingqu-config-previous`；reload 被拒时把旧 Secret 放回；新增 `lingqu-rollback`（固定，cron 不覆盖）和 `lingqu-unpin`；`check` 会报告固定状态 | 对生产 pod 只读校验：现有配置通过，坏配置、改端口都被拒绝；读写路径用桩测试。新键都不设时，渲染结果与原版逐字节一致；cron 入口照常运行，`config unchanged` |

## 第三阶段：观察一至两周（需要先上线新镜像）

| 项 | 实现 | 接入 MachGate | 还差 |
|---|---|---|---|
| 额度采集 | M4 已有 | `[lingqu] quota = true`：用 `ROUTER_*_PASSWORD` 生成 `upstreams[].quota` | 上线后看 `/status` 里额度的新鲜度 |
| 并发 shadow | 同一个池可以并排配多档；`would_hold_by_rule_1h` 分档计数 | `[lingqu] shadow_concurrency = [2, 3]` | 观察 |
| 预算预留 shadow | `reserve: shadow`：按池和缓存状态的分位数估计输入，`message_start` 时结算，记录误差和"会挡住"的次数 | `[lingqu] reserve = "shadow"` | 观察；`reserve: enforce` 目前拒绝加载 |

这三项在 `groups.toml` 里都没有打开，等新镜像上线后再开（见 MachGate `docs/lingqu-migration.md` 第 9 节）。观察期的结论（等待、缓存成本和额度消耗是否一起改善）需要真实流量，本次没有得出。

## 第四阶段：有明确容量需求后

现在不做 Redis 拆分、集中采集和 ASGI，评审本身也建议按证据推进。只做了前置条件：

- 让触发信号可见：`capacity`，以及 Redis 模式下锁等待和持有时间的 p50 / p95 / p99、状态字节数。
- Redis 的健康检查不再走读写事务，配置指纹缓存起来。
- 11 项 Redis 集成测试可以在本地真正执行。

触发阈值见 RELIABILITY.md 末节。

## 评审中本次未处理的部分

| 评审章节 | 内容 | 原因 |
|---|---|---|
| 一 | 按自然人限流需要 New API 传可信身份 | 要改 New API 侧，不在这个仓 |
| 四 | 估算 usage 的质量分层（有效输出估计、输入置信度）、显式的不可计量策略 | 先看上线后 `quality_1h` 的 estimated / none 占比再决定 |
| 四 | 生成配置带价格同步时间 | 时间戳会让每次 cron 都改配置、触发 reload，`check` 的逐字节比对也会失败；需要另外设计心跳 |
| 五 | SSE 单事件大小和解压后体积的上限 | 现有上限是 SSE 缓冲 8 MiB、非流响应 64 MiB，解压暂不设上限；列入后续 |
| 七 | 会话时钟区分待确认与已确认、回放支持真实到达轨迹 | 第四阶段（按证据推进） |
| 八 | stats 聚合表、Manager 锁外采集、增量对账 | 数据量还小；`/stats` 30 天查询的耗时上线后再看 |
