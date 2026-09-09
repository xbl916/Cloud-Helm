# Cloud Helm 1.0.0

本版重点修复服务与节点权限边界，并新增企业微信成员变更撤权能力。延续 Compose 镜像同步、逐实例排空发布、ACS 发布保障和版本回滚功能。

## 更新内容

- **容器绑定权限**：业务上报令牌不能建立或修改容器关联。`container_id` 只能确认已有绑定，正常业务数与健康状态上报不变。
- **Kubernetes 目标权限**：命名空间、工作负载名称/类型及目标容器名只允许平台管理员修改；服务操作员仍可调整授权服务的伸缩策略。
- **节点身份保护**：共享注册令牌不能覆盖已有节点身份。重复注册返回 409；原节点凭据继续有效。新增管理员节点凭据轮换/恢复接口，操作写入审计。
- **企微成员生命周期**：新增经过签名验证、AES 解密和企业校验的通讯录回调。删除、重建或明确禁用成员时停用旧平台账号、撤销会话；重复事件不会覆盖管理员后续恢复操作。
- **认证配置加固**：生产环境企微 API 地址强制 HTTPS，拒绝包含凭据、查询串和 fragment 的 API/扫码授权地址。
- **数据库**：新增 `0016_wecom_event_receipts` 增量迁移，持久化事件摘要用于去重。

## 从旧版本升级

1. 核对现有服务实例的容器绑定和 Kubernetes 目标；备份配置与数据库，保留 Agent 的 `agent-state.json`。
2. 先拉取 `1.0.0` 镜像，在原部署目录中合并新版 Compose 文件，保留自定义挂载、GPU overlay、项目名和数据路径。
3. 只切换 Server 服务，确认健康及数据库迁移成功；Agent 逐节点更新并验证上线。
4. 原有企微登录可继续使用。要启用成员自动撤权，必须额外配置企微通讯录回调并确认事件送达。

升级不会自动停止或重建受管业务容器；不要执行 `docker compose down -v`，也不要为准备升级提前释放 GPU。

**兼容变化：**首次依赖上报 `container_id` 建立绑定的客户端须迁移到 Agent 标签绑定；省略该字段或确认已有绑定仍可用。普通操作员不能修改 Kubernetes 目标。恢复旧节点身份须保留原状态文件或由管理员重置凭据。

完整步骤、SQLite 在线备份、PostgreSQL 分支、回退与验收见 [1.0.0 全新部署与旧版本升级](https://github.com/xbl916/Cloud-Helm/blob/v1.0.0/docs/deploy-and-upgrade-1.0.0.md)。

## 全新安装

1. 下载源码包和校验文件，执行 `sha256sum -c cloudhelm-1.0.0.tar.gz.sha256` 后解压。
2. 复制 `.env.example` 为 `.env`，设置正式 HTTPS 域名、企微 CorpID/AgentId/Secret、初始管理员 UserId 和随机 Agent 注册令牌。
3. 拉取 Server 镜像，使用 `docker compose up -d --no-build --wait server` 启动管理中心；配置 HTTPS 反向代理及企微可信域名。
4. 在各节点配置 `deploy/agent.env`，使用 Agent Compose 启动；NVIDIA 节点继续叠加 GPU 配置。
5. 验证登录、授权和节点上报；如需成员自动撤权，按 [安全接入说明](https://github.com/xbl916/Cloud-Helm/blob/v1.0.0/docs/security-hardening.md) 配置回调。

完整安装手册和安全接入说明同时作为本次 Release 附件提供。

## 验证与范围

- 108 项自动测试通过，包括越权回归、加密回调、凭据恢复及数据库升级；代码检查、前端语法和版本一致性检查通过。
- 企微真实事件送达必须在部署环境配置后验证；未配置或回调中断时不保证企微侧删除成员立即撤销平台访问。
- 版本发布不会替用户部署或重启现有管理平台、Agent 或业务容器。

## 发布产物

- `cloudhelm-1.0.0.tar.gz` 与 `.sha256`：源码和部署文件。
- `deploy-and-upgrade-1.0.0.md`：全新安装、升级、验收和回退说明。
- `security-hardening.md`：权限兼容、节点恢复和企微回调接入说明。
- `ghcr.io/xbl916/cloud-helm-server:1.0.0`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:1.0.0`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`。

发布包不包含实际 `.env`、企微 Secret、数据库或 Agent 状态文件。
