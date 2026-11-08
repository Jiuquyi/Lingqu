# 2026-11-08：总览按团队合并流量卡片

运行总览按团队名称和上游实例合并展示：`atlas/claude`、`atlas/gpt` 在同一张 `atlas` 卡片中，`blanc`、`cervino` 同样处理。每张卡片显示最近 60 分钟总费用及总排队、进行中请求，内部保留 Claude / GPT 的状态、额度、限额与错误明细。顶部计数改为三个流量组。

此变更只影响总览呈现。六个实际计量池、独立限流规则、`/status` JSON、统计表明细及事件记录保持原有粒度，账本无需迁移。

镜像：`local/lingqu:14`，配置 ID：

```
sha256:9db53f35566849430e6ef93827d138c5e515f977c3e409d3b94cb79184380a73
```

发布前逐文件核对线上应用与本地 HEAD 一致。镜像基于 `local/lingqu:13`，仅覆盖 `app/status/views.py`、`app/status/templates/overview.html`、`app/static/lingqu.css`。服务器 `k8s/jiuquyi/lingqu.yaml` 中的应用和初始化容器镜像声明同步为 14，其余部署配置保持原样。

验证：52 项现有代理、管理页面及 usage 回归通过，包含合并显示后仍维持独立池和独立临时限额的检查。桌面 / 手机 / 深色模式五组本地浏览器检查通过；以 Claude $25、GPT $5 的样例确认团队总费用显示 $30，Claude 限额条与 GPT 只计量状态各自保留，无页面横向溢出。

私有回退目录：`/home/ubuntu/backups/lingqu-grouped-overview-20261108-14`，保留发布前 Deployment 和镜像版本声明。`/home/ubuntu/lingqu-grouped-release-14.py rollback` 可回到版本 13，保留现有数据与渠道；执行前需确认没有后续发布需要保留。

线上真实 HTTPS 登录检查通过：1440px 桌面、390px 手机深色、320px 手机均为三张团队卡片及六个提供方分区，无整页横向溢出；原六个 `/status` 池及 Claude $100 / 小时、GPT 只计量规则保持一致。部署标准 `machgate/run.sh check` 返回 `ALL PASS`，定时器已恢复。
