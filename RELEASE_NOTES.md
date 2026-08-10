# Cloud Helm 0.7.0

## 监控告警

- 新增节点离线、节点内存/根磁盘/inode、容器内存、健康检查、OOM Kill 和反复重启等默认规则；管理员可调整阈值、连续次数、启用状态和通知状态。
- 规则支持全部资源、环境、节点和单容器范围；修改规则会清空旧活动状态，避免继承旧阈值的连续计数。
- 告警只在“触发”和“恢复”状态转换时各写一条事件，不按心跳重复写库。
- H5 审计页新增告警事件列表；事件按既有资源授权过滤，只有全局管理员或具有对应资源运维权限的运维人员可以确认，只读用户不可确认。
- 告警事件默认保留 90 天且最多 10000 行，每小时执行时间和行数双重清理；两个上限均可通过环境变量调整。

## 企业微信通知

- 可选使用现有企微自建应用向明确配置的一个或多个 `UserId` 发送触发和恢复消息，默认关闭。
- 通知失败会记录在事件中并显示于页面，不会中断 Agent 心跳或节点状态上报。
- 新增 `CLOUDHELM_ALERT_NOTIFICATIONS_ENABLED`、`CLOUDHELM_ALERT_WECOM_USERIDS` 及告警事件留存变量；现有 `.env` 无需修改即可安全升级。

## 数据库升级保护

- 新增 `alert_rules`、`alert_states`、`alert_events` 表和 `0005_alerting` 迁移记录，SQLite 与 PostgreSQL 均幂等执行。
- SQLite 在任何建表或迁移之前运行完整性检查，并创建 `cloudhelm.db.pre-0.7.0.bak` 一致性备份；升级后再次检查。
- 从 0.6.4 升级只需更新 Server 镜像；0.6.4 Agent 与 0.7.0 Server 兼容。同步更新 Agent 仅用于保持镜像版本一致。

## 发布产物

- `main` 的 CI 成功后自动创建缺失的 `v0.7.0` 标签，再触发多架构 Release 流水线；已存在标签不会被覆盖。
- `cloudhelm-0.7.0.tar.gz` 与 `.sha256`：架构无关源码、部署文件、小程序代码及校验文件。
- `ghcr.io/xbl916/cloud-helm-server:0.7.0`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:0.7.0`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`；GPU overlay 仅要求 amd64/arm64。

发布产物不包含 `.env`、企微 Secret、小程序 Secret、数据库、自动备份、Agent 注册令牌或节点身份文件。
