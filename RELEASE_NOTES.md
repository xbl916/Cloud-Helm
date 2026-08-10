# Cloud Helm 0.7.1

## 按资源配置企微告警接收人

- 告警接收人不再由固定的 `CLOUDHELM_ALERT_WECOM_USERIDS` 列表控制，直接复用云舵用户已绑定的企微 `UserId`。
- 全资源账号可在 H5“用户 → 权限与告警”统一订阅全部可见资源；自定义范围账号可按环境、节点、Compose 项目或容器独立订阅。
- 项目/容器订阅只接收对应容器告警，环境/节点订阅同时覆盖范围内的节点和容器告警；多条规则命中同一成员时自动去重。
- 发送前重新校验账号启用状态、当前资源权限和订阅状态。取消授权、取消订阅或停用账号会立即阻止后续通知。
- 告警规则的“通知”开关和 `CLOUDHELM_ALERT_NOTIFICATIONS_ENABLED` 仍作为规则级、全局级安全开关，三层条件必须同时满足。

## 权限管理与安全

- 资源授权和告警订阅在同一界面保存，预览会显示订阅变更数量，保存前二次确认并写入审计日志。
- 保留权限版本乐观锁，多个管理员同时修改同一成员时拒绝静默覆盖。
- 全局管理员可以配置自己和其他管理员是否接收告警；资源管理员只能调整自身管理范围内其他普通成员的授权与订阅。
- 管理员降级为普通角色时自动切换到受限资源模式并关闭全局告警订阅，避免遗留全资源能力。

## 数据库与升级

- 新增 `0006_alert_subscriptions` 幂等迁移：`users.alert_notifications` 和 `access_rules.alert_notify` 默认均为关闭，SQLite 与 PostgreSQL 均支持。
- SQLite 升级前创建 `cloudhelm.db.pre-0.7.1.bak` 一致性备份；既有用户、企微身份、授权、Agent 凭据、指标和审计记录保持不变。
- 从 0.7.0 升级只需更新 Server 镜像；旧 `.env` 中的 `CLOUDHELM_ALERT_WECOM_USERIDS` 会被忽略并可删除。升级后需在界面显式开启所需订阅。
- Agent 无功能改动，0.6.4/0.7.0 Agent 与 0.7.1 Server 兼容；发布同版本 Agent 镜像仅用于版本统一。

## 发布产物

- `main` 的 CI 成功后自动创建缺失的 `v0.7.1` 标签，再触发多架构 Release 流水线；已存在标签不会被覆盖。
- `cloudhelm-0.7.1.tar.gz` 与 `.sha256`：架构无关源码、部署文件、小程序代码及校验文件。
- `ghcr.io/xbl916/cloud-helm-server:0.7.1`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:0.7.1`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`；GPU overlay 仅要求 amd64/arm64。

发布产物不包含 `.env`、企微 Secret、小程序 Secret、数据库、自动备份、Agent 注册令牌或节点身份文件。
