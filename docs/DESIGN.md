# 灵渠（Lingqu）设计文档

> 版本：v0.3 草案 · 2026-10-20
> - v0.2：上游从仅 Codex 扩展为 Codex / Claude
> - v0.3：流控改为以 1 小时为控制周期（动态小时预算）；架构图改为 SVG
>
> 灵渠是秦代开凿的运河，用"陡门"分级蓄水、控制水流，让船只平稳翻越落差。本项目做的事情差不多：
> 架在 New API 和 9router 之间，按**折算后的消耗金额**给流量"分级蓄水"，避免 Codex / Claude 订阅额度被过快用完。

---

## 1. 背景与目标

### 1.1 现状

```
用户/客户端 ──► New API ──► 9router ──┬─► Codex  （ChatGPT 订阅，OAuth 登录）
               (多用户、令牌、计费)  (本地路由、OAuth、fallback) └─► Claude （Claude 订阅，OAuth 登录）
```

- **New API** 负责多用户、令牌、分组、倍率计费和渠道管理。它算的是"用户该扣多少额度"，**不知道上游订阅还剩多少**。
- **9router** 通过 OAuth 登录了 Codex 和 Claude，对外提供 OpenAI 兼容接口（默认 `:20128/v1/*`），有 combo/fallback 和用量统计，但**没有按金额或时间窗限速的能力**，管理主要靠 Dashboard，没有完整的管理 API。
- **两个订阅各有自己的滚动窗口限额，互相独立**：

| 上游 | 限额形态（公开信息，以实际为准） | 特点 |
|------|----------------------------------|------|
| Codex（ChatGPT Plus/Pro） | 短窗口（通常 5h）+ 周窗口 | 推理强度越高越费额度 |
| Claude（Pro/Max） | 5h 会话窗口 + 周窗口，部分套餐对 Opus 单独设周限额 | Opus 远比 Sonnet 费额度；Prompt 缓存对额度影响很大 |

用户一旦集中使用，短窗口很快打满，周额度也可能提前用完，严重时还会触发风控。

### 1.2 目标

| # | 目标 | 说明 |
|---|------|------|
| G1 | **按折算金额流控** | 把每次请求的 token 用量按价格表折算成"等价美元"，在多个时间窗内限额、匀速 |
| G2 | **匀速消耗、及时调整** | 以 **1 小时**为控制周期：把周期额度按权重拆成小时预算，每小时（以及上游余量变化时）重算，"一周额度不能三天用完" |
| G3 | **多上游独立核算** | Codex 和 Claude 分开计量、分开限额，一边用完不影响另一边 |
| G4 | **管理 9router** | 健康检查、连接/账号状态、模型同步、用量汇总、Dashboard 收口，必要时重启 |
| G5 | **对 New API 透明** | New API 只把灵渠当作 OpenAI / Claude 兼容渠道，不需要改 New API 代码 |
| G6 | **超限时行为友好** | 返回标准错误 + `Retry-After`，让 New API 能重试或切换渠道；也可以排队或降级模型 |

### 1.3 非目标

- 不重复造 New API 的用户体系和计费（灵渠不管"用户充了多少钱"）。
- 不替代 9router 的协议转换和 OAuth 登录（灵渠不直连 Codex / Claude）。
- 首版不做多节点集群（单实例 + 本地存储即可，留扩展口）。

---

## 2. 总体架构

![灵渠总体架构](images/architecture.svg)

灵渠分两层：

- **数据面**（每个请求都经过）：① 鉴权 → ② 识别协议和上游池 → ③ 预估成本 → ④ 闸门（1h 滚动预算 + 速率 + 并发）→ ⑤ 反向代理（SSE 透传）→ ⑥ 按实际 usage 结算。闸门不放行时返回 429、排队或降级。
- **控制面**（后台运行）：**小时预算计算器**根据周期剩余额度、上游真实使用率和小时权重，每到整点或有事件发生时重算各池的 1h 预算 `H(t)`，下发给闸门。账本、上游额度采集、9router 管理器、控制台为它提供数据。

### 2.1 上游池（Provider Pool）

这是 v0.2 新增的核心概念。每个订阅是一个**上游池**，流控规则默认挂在池上：

```
provider: codex   ← 模型匹配 gpt-*, *codex*, o*        ← 对应 9router 中的 Codex connection(s)
provider: claude  ← 模型匹配 claude-*, *opus*, *sonnet*, *haiku*  ← 对应 9router 中的 Claude connection(s)
```

- 模型到上游池的映射用"规则 + 显式表"两层：显式表优先（可从 9router 的模型/combo 同步），其次按通配符匹配，都不中就归到 `unknown` 池并告警。
- **跨池的 combo**（例如"先 Claude 再 Codex"）：预估时按 combo 的**第一个成员**预留；结算时按 9router 实际命中的模型记到对应池。实际模型从响应中的 `model` 字段获取。如果预留的池和实际结算的池不同，就在预留池释放、在实际池补扣。

### 2.2 部署拓扑

推荐 docker compose 放在同一个内网中，**9router 不对外暴露端口**，只有灵渠能访问它：

| 服务 | 端口 | 对外 | 备注 |
|------|------|------|------|
| new-api | 3000 | 是 | 面向最终用户 |
| lingqu | 8090（代理）/ 8091（控制台） | 控制台只对管理员开放 | New API 渠道的 Base URL 指向 `http://lingqu:8090` |
| 9router | 20128；Codex OAuth 回调 1455 | 否 | 开启 `REQUIRE_API_KEY=true`，密钥只给灵渠 |

> OAuth 登录需要浏览器参与：Codex 回调到 `1455` 端口，Claude 的登录流程以 9router 实际实现为准。做法是通过灵渠控制台"打开 9router Dashboard"（由灵渠反代并鉴权），登录时临时用 SSH 隧道或在本机访问完成回调。

### 2.3 技术选型

| 维度 | 选择 | 理由 |
|------|------|------|
| 语言 | **Go 1.23+** | 与 New API 同栈，便于参考它的计费、流式解析和 Claude 渠道代码；单二进制部署 |
| HTTP | `net/http` + `httputil.ReverseProxy`（自定义流式 tee） | SSE 透传要可控，必须逐块 flush |
| 存储 | SQLite（默认）/ PostgreSQL（可选），GORM | 规则、价格表、请求账本 |
| 计数器 | 进程内滑动窗口（分桶环形数组）+ 定期落盘；多实例时换 Redis（Lua 脚本原子扣减） | 单机够用，接口抽象成 `CounterStore` |
| Tokenizer | Codex：`tiktoken-go`（`o200k_base`）；Claude：近似估算（见 §4.4） | 请求前预估输入 token |
| 前端 | Vite + React + shadcn/ui，`embed` 到二进制里 | 和 9router/New API 风格接近 |

---

## 3. 核心概念：折算金额（Equivalent Cost）

订阅制没有真实账单，但订阅额度本质上和"算力消耗"相关。灵渠用**各家 API 公开价格**把 token 折算成"等价美元（eUSD）"，作为统一度量。

### 3.1 折算公式

两家的 usage 字段不同，先统一成四类 token 再计价：

| 统一字段 | OpenAI / Codex | Anthropic / Claude |
|----------|----------------|--------------------|
| `input_uncached` | `prompt_tokens - cached_tokens`（responses：`input_tokens - cached_tokens`） | `input_tokens`（本身就不含缓存部分） |
| `input_cache_read` | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| `input_cache_write` | 0（OpenAI 自动缓存，写入不另计费） | `cache_creation_input_tokens`（细分 5m / 1h 时分别计价） |
| `output` | `completion_tokens`（含 reasoning） | `output_tokens`（含 thinking） |

```
cost = ( input_uncached    × P_in
       + input_cache_read  × P_cache_read
       + input_cache_write × P_cache_write     // Claude：5m ≈ 1.25×P_in，1h ≈ 2×P_in
       + output            × P_out
       ) / 1_000_000
       × model_multiplier                      // 模型系数：体现"这个模型更费订阅额度"
       × time_multiplier                       // 可选：时段系数
```

- 价格表 `model_prices` 可以在控制台编辑，内置一份两家公开价作为默认值，也可以一键从 New API 的模型倍率导入。
- **combo 和别名**：按 9router **实际命中的模型**计价（见 §2.1）。
- 未知模型：用所在池的 `default_price` 兜底，并在控制台告警。

### 3.2 两个池的 eUSD 不能直接比较

Codex 的 1 eUSD 和 Claude 的 1 eUSD 对各自订阅额度的消耗比例不同，所以**限额必须按池分别设定**，不要设一个跨池的总额度。跨池只做展示汇总（例如"今天一共折合 $12"）。

### 3.3 为什么不直接用 New API 的额度？

New API 的 quota 是"对用户收费"的口径，带分组倍率、充值折扣，和上游消耗不是一回事。灵渠的 eUSD 是"**对上游订阅消耗**"的口径，两者分开，互不干扰。

---

## 4. 流控模型（陡门）

### 4.1 作用域（谁被限）

请求必须**所有命中的规则都放行**才会被转发：

| 作用域 | 标识来源 | 典型用途 |
|--------|----------|----------|
| `provider` | 上游池（`codex` / `claude`） | **主要作用域**，对应一个订阅的总额度 |
| `upstream` | 9router 的 provider connection（具体账号） | 同一个池里有多个账号时分别限额 |
| `key` | 灵渠下发的 `lq-` 密钥（一个 New API 渠道对应一把） | 不同 New API 渠道/分组分配不同额度 |
| `model` | 请求的 model（归一化后） | 例如单独限制 Claude Opus（对应 Claude 的 Opus 周限额） |
| `user`（可选） | New API 透传的用户标识（见 §4.7） | 单用户防刷 |
| `global` | — | 仅用于并发/RPM 这类和金额无关的总量保护 |

作用域可以组合，例如 `provider=claude & key=lq-team-a`：A 组最多分到 Claude 池小时预算的 30%。

### 4.2 设计思路：以 1 小时为控制周期

上游的 5h / 7d 窗口太长，等发现超了已经来不及。灵渠把**周期总额度拆成"每小时预算"**，每个请求都用最近 1 小时的滚动消耗来判断，预算本身也随时重算：

- **反应快**：一阵猛用之后，最多 1 小时内就被约束住，不会把整个 5h 窗口吃光。
- **自动调节**：前面用少了，剩余额度会均摊到后面的小时，预算自动变多；前面用多了，后面自动变少。不需要人工改规则。
- **上游的 5h / 7d 只作为约束条件**，灵渠实际控制的是 1h。

```
          周期总额度 L（对齐上游 7d 窗口）
                 │  每到整点 / 有事件时重算
                 ▼
  ┌──────────────────────────────────────┐
  │ 小时预算 H(t) = 剩余额度 × 本小时权重占比 × 弹性 │ ──► 闸门：最近 1h 消耗 + 预估 ≤ H(t)
  │   再用 5h 剩余、上下限夹住                   │
  └──────────────────────────────────────┘
```

### 4.3 闸门

**① 小时预算（Hourly Budget）** —— 主闸门

每个上游池（也可以是 `model`、`key` 等作用域）各有一个预算，按下面的公式算出：

```
R      = L − used_cycle                       # 本周期剩余额度（eUSD）
W_rem  = Σ 从当前小时到周期结束的各小时权重        # 剩余权重
base   = R × w(当前小时) / W_rem                # 按权重均摊到本小时
H(t)   = clamp( base × (1 + flex),  H_min,  H_max )
H(t)   = min( H(t),  S_rem )                   # 不超过上游短窗口（5h）剩余，见下

允许请求 ⇔ used_1h + reserved + estimate ≤ H(t)
```

| 参数 | 含义 | 建议值 |
|------|------|--------|
| `L` | 周期总额度，周期对齐上游 7d 重置时间 | 按订阅档位设定，再用上游 used% 校准 |
| `w(h)` | 一周 168 个小时各自的权重 | 默认工作时间 1.0、夜间 0.2、周末 0.5；也可以按最近 4 周的实际使用曲线自动生成 |
| `flex` | 弹性系数，允许本小时比均摊值多用一些 | 0.5（即最多 1.5 倍） |
| `H_min` | 预算下限，保证再紧也有基本可用量 | 例如 0.2 eUSD |
| `H_max` | 预算上限，避免周期末剩余很多时一小时内猛冲触发风控 | 例如 3 eUSD |
| `S_rem` | 上游短窗口（5h）剩余额度：有上游 used% 时用真实值，否则用灵渠自己的 5h 滚动计数估算 | — |

**实现细节**：

- `used_1h` 用 1 小时滚动窗口计数，按 1 分钟一个桶，共 60 个桶。滚动窗口没有"整点清零"，所以整点前后不会出现突发。
- **重算时机**：每个整点固定重算一次，以下事件也会触发立即重算：
  - 上游额度响应头更新，且 used% 变化超过 1 个点；
  - 规则或权重被修改；
  - 管理员手动调整；
  - 上游周期重置。

  重算只是几个算术运算，开销可以忽略，可以做成每分钟都算，整点重算主要是为了保证可解释。
- 每次重算都写一条 `budget_snapshots`，控制台可以画出"预算曲线 vs 实际消耗曲线"。

**示例**：Claude 池周额度 80 eUSD，周三 14:00 时周期已用 30，剩余 R = 50；剩余权重和 W_rem = 62.5，当前小时权重 1.0：

```
base = 50 × 1.0 / 62.5 = 0.80
H    = 0.80 × 1.5 = 1.20 eUSD     → 14:00 这一刻起，最近 1 小时最多用 1.20 eUSD
```

如果上午用得少，R 更大，H 自动变大；如果 13 点猛用了一阵，used_1h 很快到顶，请求会被排队或降级，最多一小时后自然恢复。

**② 周期硬上限（Cycle Cap）** —— 兜底

`used_cycle + estimate ≤ L`。正常情况下小时预算会先生效，这条只在权重配置不合理或预算被手动放大时兜底。此外，对 Claude Opus 这类有单独周限额的模型，可以在 `model` 作用域上再挂一套"小时预算 + 周期上限"。

**③ 速率与并发（Rate & Concurrency）** —— 削峰

- 令牌桶：`rate: 0.5 eUSD/min, burst: 3 eUSD`，挡住短时间暴冲（例如 agent 循环）。
- 并发上限：每个池单独设，例如 Codex 4、Claude 3。并发太高容易触发风控。
- RPM 兜底：`rpm: 30`，主要防止大量小请求。

### 4.4 预扣与结算（两阶段）

流式请求在结束前不知道真实用量，所以照搬 New API 的"预扣费"思路：

```
1. 预估：est = 输入预估 + min(max_tokens, expected_out[model]) × P_out
         expected_out 默认取该模型近 24h 输出 token 的 P75（冷启动用配置值）
2. 预留：所有命中的窗口执行 reserve(est)，任何一个失败就整体回滚并按策略处理（§4.5）
3. 转发：OpenAI chat/completions 流式请求强制注入 stream_options.include_usage=true
         （Anthropic messages 和 OpenAI responses 的流里自带 usage，不用注入）
4. 结算：拿到 usage 后 commit(actual)，释放 (est - actual)；actual 可能大于 est，直接补扣
5. 异常：上游报错或客户端断开 → 有 usage 就按 usage 结算，没有就按"已收到的输出 + 输入"估算结算
```

**输入 token 预估**：

| 上游 | 方法 |
|------|------|
| Codex | `tiktoken-go`（`o200k_base`），比较准 |
| Claude | 分词器不公开。用 `o200k_base` 计数 × 校正系数（默认 1.15），系数按近期"预估值 vs 实际 usage"自动滚动修正。**缓存命中率**同样按近期该 key + 模型的历史比例估算，否则长上下文的 Claude Code 请求会被严重高估 |

预留有 TTL（默认 30 分钟），防止连接泄漏导致额度一直被占用。

### 4.5 超限策略

每条规则都可以配置 `on_exceed`：

| 策略 | 行为 | 适用 |
|------|------|------|
| `reject` | 返回 `429`，带 `Retry-After`（按 1h 窗口里最早的分钟桶何时过期、能腾出足够额度来算）；错误体格式跟随请求协议 | 默认。New API 收到 429 会按渠道重试或切到别的渠道 |
| `queue` | 在灵渠内排队，最多等 `max_wait`（例如 20s），期间释放出额度就放行 | 速率类闸门（令牌桶、并发） |
| `downgrade` | 改写 `model` 为配置的更便宜的模型，重新评估 | 小时预算超了但还想让用户能用 |
| `shadow` | 只记录不拦截 | 新规则上线前观察 |

降级只在同一个池、同一种协议内进行，例如：

- Codex：`gpt-5-codex` → `gpt-5-codex-mini`
- Claude：`claude-opus-*` → `claude-sonnet-*`

> **不做跨池降级**（比如 Claude 超限后改走 Codex）：两家协议不同，工具调用和 thinking 等语义无法无损转换，Claude Code 这类客户端会出错。需要跨池兜底的话，在 New API 层配置多个渠道更合适。

错误体按请求协议返回：

```jsonc
// OpenAI 协议（/v1/chat/completions、/v1/responses）
{ "error": { "message": "灵渠：Claude 池最近 1 小时已用 1.18/1.20 eUSD，预计 7 分钟后恢复",
             "type": "rate_limit_exceeded", "code": "lingqu_hourly_budget", "param": null } }

// Anthropic 协议（/v1/messages）
{ "type": "error",
  "error": { "type": "rate_limit_error",
             "message": "灵渠：Claude 池最近 1 小时已用 1.18/1.20 eUSD，预计 7 分钟后恢复" } }
```

> New API 收到 429 时的处理取决于它的重试配置。如果 New API 只配了灵渠这一个渠道，用户就会直接看到这条提示，这正是期望的效果。

### 4.6 自适应（读取上游真实余量）

两家后端都会在响应头里返回订阅额度的使用情况。**9router 是否透传这些头需要实测确认**。

| 上游 | 可能的响应头（需实测） |
|------|----------------------|
| Codex | `x-codex-primary-used-percent`、`x-codex-secondary-used-percent` 及对应的窗口长度和重置时间 |
| Claude | `anthropic-ratelimit-unified-*` 系列（5h / 7d 窗口的使用率、状态、重置时间） |

灵渠用适配器把它们统一成 `{window: short|weekly|weekly_opus, used_pct, reset_at}`，如果拿得到：

- 记录每个池/账号的真实使用率，在控制台展示"真实余量 vs 折算余量"。
- **直接驱动小时预算**：拿到周窗口 used% 时，剩余额度直接用上游真实值 `R = (1 − used_pct) × L`，不再依赖折算累计；短窗口 used% 用来算 `S_rem`。used% 每次变化都会触发重算（§4.3），这是 1h 预算能及时调整的主要数据来源。
- **校准**：用 `Δused_pct / Δ eUSD` 反推"1% 对应多少 eUSD"，修正周期总额度 `L` 和模型系数（可选，默认只提示不自动改）。
- **紧急刹车**：任何窗口 `used_pct ≥ 90%` 时，把该池的小时预算压到 `H_min`；`≥ 98%` 时直接 `reject`，或把该账号标记为不可用。
- **周期对齐**：小时预算的周期起点直接用上游 `reset_at`。

拿不到的话，退化方案是从 9router 的用量/配额数据中读取（§6），再不行就只用折算金额。

### 4.7 识别 New API 的用户（可选）

灵渠默认只能看到 New API 渠道级别的身份（`lq-` 密钥）。如果需要按用户限流，有以下办法：

1. 在 New API 渠道配置中通过**请求头覆盖**附加用户/令牌信息（需要确认所用 New API 版本是否支持变量替换）；
2. 读取请求体中的 `user`（OpenAI）或 `metadata.user_id`（Anthropic）字段，取决于客户端是否传；
3. 按 New API 的**分组**拆成多个渠道，每个渠道用不同的 `lq-` 密钥，变成 `key` 作用域限流（最稳妥，推荐首版用这种）。

---

## 5. 请求处理流水线

```
HTTP in
  │
  ├─ auth          校验 Authorization: Bearer lq-xxx 或 x-api-key: lq-xxx → 得到 Key
  ├─ parse         识别协议和端点：
  │                  openai.chat      /v1/chat/completions
  │                  openai.responses /v1/responses
  │                  anthropic        /v1/messages、/v1/messages/count_tokens
  │                解析 model、stream、max_tokens / max_output_tokens、输入内容
  ├─ resolve       model 归一化 → combo/别名 → 实际模型 → 上游池 → 价格
  ├─ estimate      按上游池选择 tokenizer → 预估 eUSD
  ├─ gate          收集命中规则 → 并发 → 令牌桶 → 1h 小时预算 H(t) → 周期上限（按代价从低到高检查）
  │                H(t) 由后台的小时预算计算器维护，请求路径上只读内存里的当前值
  │                └─ 失败：reject / queue / downgrade / shadow
  ├─ rewrite       替换为 9router 的 API Key；按需注入 include_usage；去掉 hop-by-hop 头
  ├─ proxy         转发到 9router，SSE 逐块 flush，同时 tee 给 usage 解析器
  ├─ settle        解析 usage 并统一成四类 token：
  │                  openai.chat      末尾 chunk 的 usage
  │                  openai.responses response.completed 事件里的 response.usage
  │                  anthropic        message_start.usage（输入、缓存）+ message_delta.usage（累计输出）
  │                → 确定实际模型和池 → commit 实际 eUSD → 释放预留
  │                → 读取上游额度响应头（§4.6）
  └─ ledger        异步写请求账本（批量写，不阻塞响应）
```

关键实现点：

- **流式解析不能阻塞透传**：tee 出来的数据交给 goroutine 按 SSE 事件解析，主链路只负责写回和 flush。
- **Anthropic 的 `count_tokens`**：直接透传，不计费、不计入流控（只受 RPM 限制）。
- **超时**：首字节超时 60s，空闲超时 120s（extended thinking / 高推理强度时思考时间可能比较长，需要可配置）。
- **`/v1/models`**：转发 9router 的结果后，按 Key 的模型白名单过滤，便于 New API 一键拉取模型列表。
- **其他端点**（embeddings、audio）：默认透传，按配置的固定单价或 0 计费。

---

## 6. 9router 管理器

9router 的管理主要靠 Dashboard（Next.js），只有零散的内部 API。灵渠采用**分层适配**：能用 API 就用 API，不行就只读数据库，最后是容器控制，并且都封装在 `ninerouter.Adapter` 接口后面，以便适配 9router 的版本升级。

```go
type Adapter interface {
    Health(ctx) (HealthStatus, error)          // GET /v1/models 探活 + 延迟
    ListModels(ctx) ([]Model, error)           // 含 combo
    ListConnections(ctx) ([]Connection, error) // 账号：provider(codex/claude/...)、状态、token 过期时间
    ListCombos(ctx) ([]Combo, error)
    UsageSince(ctx, t time.Time) ([]UsageRow, error)
    Restart(ctx) error                         // 通过 Docker API
}
```

### 6.1 能力清单

| 功能 | 实现方式 | 优先级 |
|------|----------|--------|
| 健康检查 | 定时 `GET /v1/models`（带 key），并对每个池各发一次极小的探测请求（可选，默认关闭以免消耗额度）；连续失败就告警 | P0 |
| 模型/combo 同步 | `/v1/models` 已经包含 combo；combo 的成员模型从 Dashboard 内部 API 或 SQLite 读取，并自动生成"模型 → 上游池"映射 | P0 |
| 连接状态 | Dashboard 内部 API（用 `INITIAL_PASSWORD` 登录拿 JWT cookie）或只读打开 `~/.9router/db/data.sqlite`；按 Codex / Claude 分组展示 | P1 |
| 用量对账 | 读 9router usage 表，和灵渠账本按小时、按池对账，偏差 > 10% 时告警（可能有人绕过灵渠直连） | P1 |
| Dashboard 收口 | 灵渠控制台 `/ninerouter/*` 反代到 9router Dashboard，复用灵渠管理员登录，9router 本身不暴露 | P1 |
| API Key 管理 | 灵渠持有 9router key，支持轮换（在 9router 生成新 key → 灵渠热更新 → 删除旧 key） | P2 |
| 数据备份 | 定时复制 `data.sqlite`（使用 SQLite backup API，避免读到写了一半的数据）到灵渠备份目录 | P2 |
| 重启/升级 | 挂载 docker.sock（可选，默认关闭），一键重启/拉新镜像 | P2 |
| 同步到 New API | 调 New API 管理 API（Access Token）更新灵渠渠道的模型列表（Codex 模型同步到 OpenAI 渠道，Claude 模型同步到 Claude 渠道） | P2 |

> **只读 SQLite** 需要把 9router 的 `DATA_DIR` 以只读方式挂载给灵渠，并以只读模式打开。表结构随 9router 版本变化，适配器按版本号做映射，识别失败就降级为只做 API 探活。

### 6.2 OAuth 账号异常提醒

Codex 和 Claude 的 OAuth token 都由 9router 自动刷新。灵渠只做监控：如果某个账号状态异常或刷新失败（连接状态里能看出来，或者上游连续返回 401/403），就在控制台标红并发送通知（Webhook/企业微信/Telegram，可配置），同时可以把该账号所在的池临时置为不可用，避免请求一直失败。

---

## 7. 数据模型

```sql
-- 灵渠下发给 New API 渠道的密钥
keys(id, name, key_hash, key_prefix, group, allowed_providers JSON, model_allowlist JSON,
     enabled, created_at)

-- 上游池
providers(id, name,                         -- codex | claude
          protocol,                         -- openai | anthropic
          model_patterns JSON, default_price JSON, enabled)

-- 9router 实例 / connection
upstreams(id, provider_id, name, base_url, api_key_enc, connection_ref, adapter_version, enabled)

-- 模型 → 池 显式映射（同步 + 手动）
model_routes(model, provider_id, source, updated_at)

-- 价格表（eUSD / 1M tokens）
model_prices(model, provider_id, p_input, p_cache_read, p_cache_write_5m, p_cache_write_1h,
             p_output, multiplier, source, updated_at)

-- combo 映射缓存
combos(name, members JSON, synced_at)

-- 流控规则
rules(id, name, scope JSON,                       -- {provider, upstream, key, model, user}
      kind,                                       -- hourly_budget | cycle_cap | rate | concurrency | rpm
      cycle_limit_eusd, cycle_period, anchor,     -- hourly_budget / cycle_cap：周期总额度 L
      weight_profile_id, flex, h_min, h_max,      -- hourly_budget
      share,                                      -- 按 key 分配时占池预算的比例（可选）
      rate_per_min, burst,                        -- rate
      max_concurrency, rpm,
      on_exceed, max_wait_sec, downgrade_to,
      priority, enabled, mode)                    -- mode: enforce | shadow

-- 小时权重表（一周 168 个小时）
weight_profiles(id, name, weights JSON,           -- [168]float
                source)                           -- manual | learned（按最近 4 周的使用曲线生成）

-- 小时预算快照（每次重算写一条，用于画预算曲线和排查）
budget_snapshots(rule_id, scope_key, ts, trigger,  -- hourly | quota_header | rule_change | manual | cycle_reset
                 remaining_r, weight_now, weight_rem, s_rem, h_value)

-- 请求账本（按月分表或定期归档）
ledger(id, ts, key_id, provider_id, upstream_id, user_tag, protocol, endpoint,
       model_req, model_actual, stream, status,
       input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens,
       usage_source,                              -- upstream | estimated
       est_eusd, actual_eusd, latency_ms, ttfb_ms, gate_result, error_code)

-- 上游真实额度快照（来自响应头或 9router）
upstream_quota(upstream_id, ts, window,           -- short | weekly | weekly_opus
               used_pct, reset_at)

-- 计数器快照（进程重启后恢复滑动窗口）
counter_snapshots(rule_id, scope_key, buckets BLOB, saved_at)
```

重启恢复：启动时先加载 `counter_snapshots`，再用 `ledger` 中快照时间之后的记录重放，保证窗口计数不丢。

---

## 8. 接口

### 8.1 代理（:8090，给 New API 用）

| 方法 | 路径 | 协议 | 说明 |
|------|------|------|------|
| POST | `/v1/chat/completions` | OpenAI | 流控 + 透传（Codex，也可以是 9router 转换后的 Claude） |
| POST | `/v1/responses` | OpenAI | 流控 + 透传（Codex 原生协议，需确认 9router 版本支持） |
| POST | `/v1/messages` | Anthropic | 流控 + 透传（Claude 原生协议，需确认 9router 版本支持） |
| POST | `/v1/messages/count_tokens` | Anthropic | 透传，不计费 |
| GET | `/v1/models` | OpenAI | 透传 + 白名单过滤 |
| * | `/v1/*` | — | 其他端点按配置透传 |

> 如果 9router 只提供 OpenAI 格式，Claude 也能通过 `/v1/chat/completions` 访问（由 9router 转换协议），灵渠同样按模型名把它归到 Claude 池，计价不受影响。但 Claude Code 等客户端需要 Anthropic 原生协议，所以优先确认 9router 是否支持 `/v1/messages`。

响应头额外返回（方便排查和客户端自适应）：

```
X-Lingqu-Provider: claude
X-Lingqu-Cost: 0.0213
X-Lingqu-Budget-1h: 1.20          # 当前小时预算 H(t)
X-Lingqu-Remaining-1h: 0.37       # H(t) − 最近 1 小时已用
X-Lingqu-Remaining-Cycle: 27.80   # 本周期剩余
X-Lingqu-Pace: ahead|on_track|behind
```

### 8.2 管理 API（:8091，`/api/admin/*`，管理员 JWT）

- `GET /dashboard/overview` — 按池展示当前小时预算与已用、周期剩余、上游真实余量、今日成本曲线
- `GET /budgets/{rule}/history` — 小时预算曲线（来自 `budget_snapshots`）
- `POST /budgets/{rule}/recalc` — 手动触发重算；`POST /budgets/{rule}/override` — 临时覆盖 H（带过期时间）
- `CRUD /weight-profiles`、`POST /weight-profiles/learn` — 从历史账本生成小时权重
- `CRUD /keys`、`/rules`、`/prices`、`/providers`、`/upstreams`、`/model-routes`
- `POST /rules/{id}/simulate` — 用最近 7 天的账本回放，看新规则会拦截多少请求（规则上线前必看）
- `GET /ledger?from&to&key&provider&model` — 明细查询与导出 CSV
- `GET /ninerouter/status`、`POST /ninerouter/sync`、`POST /ninerouter/restart`
- `POST /counters/{rule}/reset` — 手动清零（例如上游提前重置了）

### 8.3 控制台页面

1. **总览**：Codex / Claude 两个池并排；每个池一根"1h 水柱"（最近 1 小时已用 / H(t)）+ 周期进度条；近 24h 的"小时预算曲线 vs 实际消耗"叠加图；上游真实余量；拦截次数。
2. **规则**：可视化编辑、168 小时权重热力图编辑、shadow 模式开关、回放模拟。
3. **密钥**：给 New API 渠道发 key，设置可用的池，查看每个 key 的消耗。
4. **模型与价格**：价格表、模型 → 池映射、导入 New API 倍率。
5. **9router**：健康、账号状态（按 Codex / Claude 分组）、模型/combo、对账、内嵌 Dashboard。
6. **账本**：请求明细。

---

## 9. 配置示例

```yaml
# lingqu.yaml
server:
  proxy_addr: ":8090"
  admin_addr: ":8091"
  admin_password: "${LINGQU_ADMIN_PASSWORD}"

storage:
  driver: sqlite            # sqlite | postgres
  dsn: "data/lingqu.db"
  counter_store: memory     # memory | redis

ninerouter:
  base_url: http://9router:20128
  api_key: "${NINEROUTER_API_KEY}"
  dashboard_password: "${NINEROUTER_PASSWORD}"   # 可选，用于内部 API
  data_dir: /mnt/9router                          # 可选，只读挂载
  read_quota_headers: true

providers:
  - name: codex
    protocol: openai
    model_patterns: ["gpt-*", "*codex*", "o[0-9]*"]
    default_price: { input: 1.25, cache_read: 0.125, output: 10.0 }
  - name: claude
    protocol: anthropic
    model_patterns: ["claude-*", "*opus*", "*sonnet*", "*haiku*"]
    default_price: { input: 3.0, cache_read: 0.30, cache_write_5m: 3.75, cache_write_1h: 6.0, output: 15.0 }
    tokenizer_factor: 1.15

# 价格仅为示例，请以各家官方最新价格为准；完整价格表在控制台维护
pricing:
  gpt-5-codex:       { input: 1.25, cache_read: 0.125, output: 10.0 }
  gpt-5-codex-mini:  { input: 0.25, cache_read: 0.025, output: 2.0 }
  claude-sonnet-4-5: { input: 3.0,  cache_read: 0.30,  cache_write_5m: 3.75, cache_write_1h: 6.0,  output: 15.0 }
  claude-opus-4-5:   { input: 5.0,  cache_read: 0.50,  cache_write_5m: 6.25, cache_write_1h: 10.0, output: 25.0 }

budget:
  recalc: hourly              # 整点重算；另外由事件触发（上游 used% 变化 / 规则修改 / 周期重置）
  min_header_delta_pct: 1     # 上游 used% 变化超过 1 个点才触发重算

weight_profiles:
  workday:                    # 简写：按"工作日/周末 × 时段"展开成 168 小时
    weekday: { "09-12": 1.0, "13-19": 1.0, "19-24": 0.6, "00-09": 0.2, "12-13": 0.5 }
    weekend: { "10-22": 0.5, default: 0.2 }

rules:
  # ---------- Codex 池 ----------
  - name: Codex-小时预算
    scope: { provider: codex }
    kind: hourly_budget
    cycle_limit: 60.0         # 周期总额度 L（eUSD）
    cycle_period: 7d
    anchor: upstream_reset    # 跟随上游周重置；拿不到时退回 "mon 00:00"
    weight_profile: workday
    flex: 0.5
    h_min: 0.2
    h_max: 2.5
    on_exceed: downgrade
    downgrade_to: gpt-5-codex-mini

  - name: Codex-周期上限
    scope: { provider: codex }
    kind: cycle_cap
    cycle_limit: 60.0
    on_exceed: reject

  - name: Codex-并发
    scope: { provider: codex }
    kind: concurrency
    max_concurrency: 4
    on_exceed: queue
    max_wait: 30s

  # ---------- Claude 池 ----------
  - name: Claude-小时预算
    scope: { provider: claude }
    kind: hourly_budget
    cycle_limit: 80.0
    cycle_period: 7d
    anchor: upstream_reset
    weight_profile: workday
    flex: 0.5
    h_min: 0.3
    h_max: 3.0
    on_exceed: downgrade
    downgrade_to: claude-sonnet-4-5

  - name: Claude-Opus小时预算  # 对应 Claude 的 Opus 单独周限额
    scope: { provider: claude, model: "claude-opus-*" }
    kind: hourly_budget
    cycle_limit: 40.0
    cycle_period: 7d
    anchor: upstream_reset
    weight_profile: workday
    flex: 0.5
    h_min: 0.1
    h_max: 1.5
    on_exceed: downgrade
    downgrade_to: claude-sonnet-4-5

  - name: Claude-周期上限
    scope: { provider: claude }
    kind: cycle_cap
    cycle_limit: 80.0
    on_exceed: reject

  - name: Claude-并发
    scope: { provider: claude }
    kind: concurrency
    max_concurrency: 3
    on_exceed: queue
    max_wait: 30s

  # ---------- 通用削峰 ----------
  - name: 每Key削峰
    scope: { key: "*" }       # 对每把 key 分别生效
    kind: rate
    rate_per_min: 0.5
    burst: 3.0
    on_exceed: queue
    max_wait: 20s

notify:
  webhook: ""               # 额度 80%/95%、账号异常、对账偏差时推送
```

**New API 侧配置**（建两个渠道，指向同一个灵渠）：

| 渠道 | 类型 | Base URL | 密钥 | 模型 |
|------|------|----------|------|------|
| 灵渠-Codex | OpenAI | `http://lingqu:8090` | `lq-xxx` | 从灵渠拉取的 Codex 模型 |
| 灵渠-Claude | Anthropic Claude | `http://lingqu:8090` | `lq-yyy`（可以和上面共用一把） | 从灵渠拉取的 Claude 模型 |

这样客户端用 OpenAI 格式或 Anthropic 格式（如 Claude Code）都能经 New API 访问。

---

## 10. 代码结构

```
lingqu/
├── cmd/lingqu/main.go
├── internal/
│   ├── config/            # 配置加载、热更新
│   ├── proxy/             # 反向代理、SSE tee、请求改写
│   ├── protocol/          # 协议适配：请求解析、usage 提取、错误体生成
│   │   ├── openai_chat.go
│   │   ├── openai_responses.go
│   │   └── anthropic_messages.go
│   ├── provider/          # 上游池、模型 → 池路由、combo 解析
│   ├── pricing/           # 价格表、统一 usage → eUSD
│   ├── tokenizer/         # 输入预估（tiktoken + Claude 校正系数）
│   ├── gate/              # 流控核心
│   │   ├── window.go      #   1h 分桶滑动窗口（60 × 1min）
│   │   ├── budget.go      #   小时预算计算器：公式、权重表、重算触发
│   │   ├── bucket.go      #   令牌桶
│   │   ├── concurrency.go #   并发信号量（可排队）
│   │   ├── reservation.go #   预留/结算/跨池转移/TTL
│   │   └── store/         #   memory / redis
│   ├── quota/             # 上游额度响应头解析（codex / anthropic）与校准
│   ├── ninerouter/        # 9router 适配器（api / sqlite / docker）
│   ├── newapi/            # New API 管理 API 客户端（模型同步、倍率导入）
│   ├── ledger/            # 账本批量写入、对账、归档
│   ├── notify/
│   ├── admin/             # 管理 API
│   └── store/             # GORM 模型与迁移
├── web/                   # 控制台前端（构建后 embed）
├── deploy/docker-compose.yml
└── docs/
```

---

## 11. 里程碑

| 阶段 | 内容 | 验收标准 |
|------|------|----------|
| **M1 能用**（约 1 周） | 透传 chat/completions、responses、messages，SSE 透传，三种协议的 usage 解析，上游池路由，eUSD 计算，账本 | New API → 灵渠 → 9router 在 Codex 和 Claude 上都跑通流式和非流式（含 Claude Code），账本里的 token 数与 9router 一致 |
| **M2 能控** | 1h 滚动窗口 + 小时预算计算器（整点 + 事件重算）+ 周期上限 + 令牌桶 + 并发，预扣/结算（含跨池转移），按协议返回 429 + Retry-After，YAML 规则 | 压测下 1h 计数准确（误差 < 1%），重启不丢计数，两个池互不影响；用历史账本回放时，预算曲线能让周期额度在期末前不被用完 |
| **M3 能管** | 控制台（总览/规则/密钥/模型与价格/账本），9router 健康检查与模型同步 | 可在页面完成全部配置 |
| **M4 更聪明** | 读取两家额度响应头并直接驱动小时预算、按历史学习小时权重、降级/排队、Claude tokenizer 系数与缓存命中率自动校正、规则回放、对账、通知 | 上游 used% 与折算值的偏差可视化；Claude 预估误差 < 15% |
| **M5 可选** | Redis 多实例、多 9router 实例、docker 控制、用户级限流 | — |

---

## 12. 风险与待确认事项

| 项 | 说明 | 应对 |
|----|------|------|
| 9router 是否支持 `/v1/responses` 和 `/v1/messages` | 影响 Codex / Claude 原生客户端（Codex CLI、Claude Code）能否经 New API 使用 | M1 先实测；不支持就只走 chat/completions，由 9router 转换协议 |
| 9router 是否透传两家的额度响应头 | 影响 §4.6 自适应 | M1 抓包确认；不透传就退回只读 9router 的配额数据，或只用折算金额 |
| 9router 内部 API / 表结构不稳定 | 管理功能可能因升级失效 | 适配器按版本隔离，失败时降级为只探活；固定 9router 镜像版本 |
| eUSD 与订阅真实额度不成比例 | 两家订阅额度的计算方式都不公开 | 用上游 used% 校准；限额先保守设置，用 shadow 模式观察一周再收紧 |
| Claude 输入预估偏差 | 分词器不公开，缓存命中率波动大 | 校正系数和缓存命中率按历史自动修正；结算以实际 usage 为准，预估只影响预留 |
| 流式结束时缺少 usage | 部分路径可能不回 usage | 灵渠对输出计数兜底，并在账本里标记 `usage_source=estimated` |
| 跨池 combo 导致预留不准 | 预留在 A 池，实际走了 B 池 | 结算时做跨池转移；在控制台提示尽量不要配置跨池 combo |
| 绕过灵渠直连 9router | 账本不完整 | 9router 只在内网暴露 + `REQUIRE_API_KEY`；对账告警 |
| 服务条款 | 把个人 ChatGPT / Claude 订阅通过 API 共享给多人使用，可能违反 OpenAI 和 Anthropic 的使用条款并导致账号被封 | 请自行评估；灵渠的匀速和并发控制可以降低异常流量特征，但不能消除这个风险 |

---

## 13. 一句话总结

**New API 管"谁能用、该收多少钱"，9router 管"怎么连上 Codex / Claude"，灵渠管"每个订阅能撑多久"**：把 token 按各家价格折算成等价金额，Codex 和 Claude 分池核算，把周期额度按权重拆成每小时预算，用 1h 滚动窗口及时约束、随用量和上游余量随时重算，用令牌桶和并发削峰，并把 9router 收进统一的管理控制台。
