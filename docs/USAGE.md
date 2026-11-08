# 内置用量分析

## 合并范围

`/usage/`、`/usage/index.html`、`/usage/api/stats`、`/usage/api/quotas` 和 `/usage/healthz` 已纳入 Lingqu。兼容 `/usage`，并跳转到 `/usage/`。页面复用公共模板、登录、导航、主题和本地静态资源，不再启动另一套 HTTP server。

代码由部署仓库 `c5a2543` 中的 `usage/server.py` 和 `index.html` 迁入，原统计计算与前端交互保留，并拆成：

- `app/usage/service.py`：每个 worker 独立的只读日志缓存与采集任务。
- `app/usage/aggregate.py`：时间序列、分组 / 用户 / 模型 / Key 明细、最近请求、画像、错误原因及额度校准展示。
- `app/status/usage.py`：受保护的页面和 JSON 路由。
- `app/status/templates/usage.html`、`app/static/usage.css`、`app/static/usage.js`：公共页面框架内的图表、筛选、表格和局部样式。

没有迁入原独立服务用于暂停 / 恢复 New API 渠道的写操作。新模块只向配置中的 New API 发 GET 请求，限流继续由现有 gate 执行。

## 启用

默认关闭，现有安装不增加采集任务。开启需配置管理员 token 和 New API 系统访问令牌：

```yaml
server:
  admin_token: "${LINGQU_ADMIN_TOKEN}"

usage:
  enabled: true
  newapi:
    base_url: http://new-api:3000
    token: "${NEWAPI_ACCESS_TOKEN}"
    user_id: "${NEWAPI_USER_ID}"
  scope: managed
  retention_days: 90
  max_records: 100000
  timezone: Asia/Shanghai
  title: 九衢驿 订阅渠道用量
  cost_label: 计费金额
  cost_hint: New API 售价，含分组倍率
```

令牌需具有读取 New API 渠道与日志的权限。使用系统访问令牌，不使用管理员密码反复登录，不产生额外 New API 登录会话。凭据经现有配置环境变量展开机制加载，不进入 HTML 或 JSON 页面配置。

`scope: managed` 与原九衢驿部署一致：只统计带 `other_info.machgate_router` 的渠道，渠道 `<group>` 对应 Claude、`<group>-gpt` 对应 Codex。`scope: groups` 则按 New API 的 group 字段统计。账号额度与限额卡片按池名 `<group>/<provider>` 关联，provider 类型使用池配置的 kind。

配置改变需重启 worker；热重载会明确拒绝使用旧采集器读取新凭据或混合不同统计范围。已有其他配置仍沿用原来的热重载逻辑。

启用后自动提供“用量分析”导航，去掉 `server.links` 中重复的本地 `/usage/` 链接。所有 usage 页面和接口始终要求控制台登录或管理员 Bearer token，包含本机访问；不受 `private_status: false` 放宽。未登录浏览器回到登录页并保留目标地址，API 未登录返回 401。

## 数据与运行边界

计费金额来自 New API consume 日志的 `quota / quota_per_unit`，包含售价和分组倍率。没有把这些金额替换为 Lingqu 账本中的 eUSD。输入 / 缓存读 / 缓存写保持原语义：Anthropic 的 prompt tokens 不包含缓存，OpenAI 的包含缓存。

- 账单日志每 20 秒检查一次，渠道信息每 5 分钟刷新；增量读取重叠 15 分钟，并按日志 ID 去重，补上长流结束后延迟写入的日志。水位仅在批次完整成功后提交，失败重试不会跳过尚未完成的历史区间；历史日志中的 `frt=-1` 缺省值保留原语义。
- 采集器由 Runtime 在 gunicorn worker 启动时管理。创建 Flask app、运行 CLI、渲染页面或请求 JSON 均不触发采集；退出标记在分页之间生效。
- 账号额度复用 QuotaCollector 的已采集窗口，名称复用其脱敏字段缓存及 Manager 库存；没有额外登录 9router 或轮询其额度。只展示已接入 Lingqu 的池 / 连接；没有配置 `upstreams[].quota` 时提示等待采集，不把缺失额度当成零。原服务特有的 email / plan 展示不额外抓取，账号名称优先使用现有快照。
- 限额和校准直接读取同一 Runtime 的 gate / quota 状态，不通过 HTTP 回调自身 `/status`。共享账号的同一个额度窗口只显示一次，采用较新的快照。
- `stats.throttles` 读取已写入账本的 `throttle_start` 事件，`throttle_source` 为 `lingqu_events`；不再用售价账单反推 eUSD 限流。`cost_source` 为 `newapi_billed`。
- 保留原来的时间筛选、Claude / Codex 筛选、分组筛选、指标切换、四种明细维度、列排序、最近 80 条请求和图表 tooltip。表格排序也支持 Enter / 空格键。
- 日志仅保存在每个 worker 的内存中。最多保留 90 天，成功与失败记录合计受 `max_records` 约束，超限移除最早记录并明确提示统计可能不完整。扩大保留上限前需相应调整内存预算；多 worker 各自读取并缓存，不进行写入协调。
- 同步失败保留之前的数据，并显示脱敏错误。`/usage/healthz` 在首次同步前或同步失败时返回 503，成功后返回 200；它也受管理员鉴权。主 `/healthz` 不以账单源是否可用为准，避免可选看板故障阻断代理流量。

## 从当前独立 usage 服务迁移

2026-11-08 已在九衢驿上线，当前运行镜像为 `local/lingqu:13`；线上 Deployment、配置生成源和 usage 路由已更新，旧独立服务暂留作回退。详细记录见 [发布记录](releases/2026-11-08-ui-usage.md)。下列步骤保留供其他部署迁移时使用。

1. 在部署配置的**生成源**加入上面的 usage 配置。当前服务器的 `lingqu-config` 由 MachGate 从 `groups.toml` 生成；相应修改生成逻辑，避免下一次 apply / sync-models 把 usage 配置覆盖掉。不要只手工改生成结果。
2. 将 `NEWAPI_ACCESS_TOKEN`、`NEWAPI_USER_ID` 注入 Lingqu 容器。可从现有 `usage-env` Secret 中选择这两个键作为 `valueFrom.secretKeyRef`；该 Secret 名由 Kustomize 带 hash 生成，需在相同 Kustomization 中按逻辑名引用，可使用 [usage-env.patch.example.yaml](../deploy/usage-env.patch.example.yaml) 作为 strategic-merge patch。不要把整个旧服务环境照搬进容器，已有 9router 凭据沿用 Lingqu 配置。
3. 构建包含本次修改的新 Lingqu 镜像，并同时更新 Deployment 中主容器和 `chown-data` initContainer 的镜像标签。先通过现有 `/healthz` 验证主代理就绪，再从集群内部使用管理员鉴权确认 `/usage/`、stats、quotas 和 usage healthz；无需提前切换公开入口。
4. 将入口 `/usage` 从 `usage:8080` 改为 `lingqu:8091`，或先使用 [usage-ingress.example.yaml](../deploy/usage-ingress.example.yaml) 中优先级 60 的新路由覆盖原优先级 50 的路由。新 handler 自己检查登录，不需要原来的 forwardAuth 中间件。其余页面和 API 路由不动。
5. 确认登录回跳、实际统计范围、计费金额、额度、主题和移动端体验后，再移除旧 usage Deployment / Service、源码 ConfigMap、专属 NetworkPolicy 和 Kustomize 资源声明。若其他组件还引用旧 Secret / 登录中间件，保留相应资源；本例的 Lingqu 仍引用 `usage-env`，所以保留其 Secret 生成声明。新路由撤销即可回退旧页面；若回退镜像，还需同时回退新的 usage 配置。

现有已提交网络策略允许 Lingqu 访问同命名空间的 New API 和各 router，也允许 ingress namespace 访问 Lingqu 8091，无需为了该迁移开放额外端口。

## 本地验证

新增测试覆盖配置、直接访问鉴权、登录回跳、代理端口隔离、统计及缓存语义、日志去重、分页上限、时区、故障后保留数据、实例状态隔离、只读请求和运行生命周期。浏览器验证使用隔离测试配置和样例数据，不启动采集线程或连接生产 API。

本次验证结果：完整测试 219 项通过；新旧统计 45 种筛选组合匹配；桌面 / 手机五组尺寸与主题组合的登录回跳、筛选、图表和键盘排序检查通过。示例 YAML 解析及 JavaScript 语法检查通过。已连接线上账单源完成历史回填，并将今天、7 天和 90 天汇总与旧服务核对一致；公开 HTTPS 登录及桌面 / 手机验收已通过。
