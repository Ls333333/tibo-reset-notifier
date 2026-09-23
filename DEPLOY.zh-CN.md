# Tibo 重置通知：GitHub Actions 部署

本目录基于 [Aadisharma1/tibo-watcher](https://github.com/Aadisharma1/tibo-watcher)，保留原 MIT 许可。

部署后由 GitHub 云端定时运行，本机可以关机；不需要 X 登录、X Cookies 或 Cloudflare 账号。

## 当前行为

- 默认只监控 Tibo，工作流每 10 分钟检查一次；GitHub 调度可能延迟。
- 公告输入改为 [AIHOT 公共 API](https://aihot.news/api/v1/codex-resets)。2026-09-22 实测原 Dayclaw 接口最新数据停在 9 月 6 日，因此本版本不默认使用它。
- 按原帖 ID 去重，收录预告、确认和重置卡相关发文。AIHOT 可能只提供原文摘录和中文整理，通知有原帖链接。
- 上游最近核验时间超过 6 小时会让工作流报错；仅有 HTTP 200 不能算监控健康。
- 第一次正常运行静默建立基线，之后仅推新帖。`prime` 只重建已知帖子基线，保留待补发消息，不发送。
- 发送失败保存在队列，下次继续尝试；已成功的渠道不再重复发送。每轮最多处理 5 条，剩余条目保留。
- 网络确认丢失或云任务被强制终止仍可能造成重复，无法保证严格恰好一次送达。
- 不调用大模型，不消耗 Codex 推理额度。公告不代表个人账号已到账。

## GitHub 仓库

建议仓库名 `tibo-reset-notifier`，默认分支沿用 `master`。

公开仓库使用标准 GitHub 托管 runner 免费；私有仓库消耗账户的 Actions 免费分钟，超额可能计费或停跑。每 10 分钟约有 4320 次/月，应根据实际计费和运行时长决定是否改为每 30 分钟。仓库可见性由使用者确认后再创建。

来源：[GitHub Actions 计费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。

不要上传 `.env`、`.workbuddy-dispatch/`、本地授权配置、私有日志。推送代码时仅选择已审查的源码、测试、工作流和文档。工作流使用新的 `runtime_state.json`，不沿用原作者的基线。

## 通知配置

在仓库 Settings → Secrets and variables → Actions → New repository secret 设置。不要把令牌或授权码粘贴在聊天、代码或 Issue 中。

### 个人微信（优先）

1. 在 [PushPlus 官网](https://www.pushplus.plus/)按官方指引登录并绑定「pushplus 推送加」微信服务号。
2. 新建 Secret：`PUSHPLUS_TOKEN`，值填自己的推送令牌。
3. 本工作流已设 `PUSHPLUS_CHANNEL=wechat`，消息推到个人微信服务号；不需要企业微信账号。

PushPlus 接口接受消息不等于手机已收到；上线需要确认手机实际到达。免费服务号通知可能需要点开查看正文。通道限额和服务策略以 [PushPlus 官方文档](https://www.pushplus.plus/doc/guide/api.html)为准。

### 邮件（可单独使用或同时启用）

设置 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`MAIL_TO`；`MAIL_FROM`可选，默认与发件账号相同。465 使用 TLS，其他端口使用 STARTTLS。`SMTP_PASSWORD`填邮件服务提供的 SMTP 授权码/应用密码，具体按邮箱官方说明设置。

如果需要发给多个邮箱，当前设计以同一 SMTP 请求发送；部分收件人被拒绝会保留整个邮件渠道重试，可能让已成功收件人重复收到。个人使用建议仅填一个收件邮箱。

## 上线验收

1. Actions → Reliability tests 必须通过。
2. Actions → Tibo reset notifications → Run workflow，mode 选 `test`，确认微信或邮箱实际收到测试消息。
3. mode 选 `prime`，建立基线，不群发历史公告。
4. mode 选 `check`，确认绿色通过且没有旧消息重复发送。
5. 在 Actions repository variables 新建 `MONITOR_ENABLED=true`，启用定时监控；未设置时只允许手动运行，避免凭据还没配好就开始定时检查。
6. 等一次真实 schedule 运行并查看日志，再确认监控已启用。暂停时将 `MONITOR_ENABLED` 改为 `false`。

调试：`python -m unittest discover -s tests -v`；`python main.py --dry-run --verbose`仅拉取分析，不发消息、不保存状态。

工作流只请求本仓库 contents:write，用于保存去重和待补发状态。状态仅包含公开帖子内容、时间和渠道名，不保存令牌或邮箱地址。公共仓库状态和运行日志也公开；不要在帖子或测试夹具里加入私密内容。

## 可靠性的实际限制

第三方采集源可能延迟或遗漏，GitHub 调度也没有即时触发保证，不能承诺每条 Tibo 发文百分百送达。来源故障会使工作流失败，但该轮仍会尝试历史待补发消息。应启用 GitHub 自带的 Actions 失败邮件通知，以免源失效长期未察觉。
