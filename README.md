# 灵渠（Lingqu）

架在 New API 和 9router 之间的流控代理：把每次请求的 token 按价格表折算成等价美元（eUSD），按"池"（一个订阅账户的一类上游）分别核算；超过每小时限额时，请求在灵渠里排队、按间隔匀速放出，而不是报错。设计见 [docs/DESIGN.md](docs/DESIGN.md)。

当前实现覆盖里程碑 M1 ～ M5（DESIGN §14）。M5 的配置、升级和多实例边界见 [docs/M5.md](docs/M5.md)。计量生命周期、幂等、恢复与 RPO、进程容量和 shadow 观察项见 [docs/RELIABILITY.md](docs/RELIABILITY.md)（0.9.0，迁移 `0005`）：

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
| 0.9.0：客户端任意位置断开都保留已读到的 usage；计费失败照写账本并按原 key 补偿；账本行和计量事件幂等写入；运行中请求的输入可在崩溃后恢复；恢复未完成即不 ready；Redis 丢状态时拒绝准入并由单个实例重建；进程级并发 / 请求体上限；预算预留和多档并发的 shadow 对比 | `reserve: enforce` |
| 0.9.1：多个池共用一个订阅账户时用 `account` 标出，额度校准按账户合并各池费用、按整个周期累计并保留最近 48 个周期；同一周期的重置时间允许 2 分钟抖动（[M4](docs/M4.md)） | |
| 0.9.2：额度校准不再被旧快照切碎（周期内只认不低于最高值的读数，跳过已过期或没有重置时间的读数），每个周期记下同账户其他窗口的涨幅（`peers`），可按比例在窗口间换算（[M4](docs/M4.md)） | |
| 0.9.3：`server.private_status` 让状态页只对管理员和本机开放，`server.dashboards_url` 指定关闭页上的开关页地址，`server.dashboard_gate_host` 指定开关页上入口链接的主机；状态端口可以对外开放来承载 Dashboard 开关页；`flask check` 不再探测没有池使用的 upstream（只开 Dashboard 入口、还在接入账号的实例）；`/console/auth-check` 让反向代理把控制台登录借给同域名下的其他页面，登录页支持 `next`；`server.links` 给页面加导航栏，未登录打开总览会跳到登录页（[M5](docs/M5.md)） | |