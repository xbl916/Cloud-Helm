# Cloud Helm 0.9.1

- 本地 AI 容器可通过 `cloudhelm.service_id`、`cloudhelm.instance_id` 标签自动关联 Agent 容器状态与业务上报；ACS 实例明确禁止关联本地主机容器。

## 模块镜像与严格串行发布

- 管理台可为每个混合云模块配置统一期望镜像，同时更新 ACS 工作负载和全部本地实例。
- ACS 新版本全部 Ready 后，本地 GPU 容器才开始更新；本地允许停止旧容器后原位启动新容器。
- 同一模块严格一次只更新一个本地容器。Docker 启动、健康检查、镜像校验和业务实例重新 Ready 全部通过后才更新下一个。
- 任一节点失败或整批超时都会停止后续节点，未下发任务保持原版本；支持按平台记录的上一版本发起串行回滚。
- Compose YAML 不由 Agent 改写；平台将期望镜像作为运行态权威值，Compose 再次创建旧 Tag 时自动逐个纠正，同时建议在代码仓库同步更新 Compose。
- 新增 `0009_module_image_releases` 迁移和 Kubernetes 工作负载镜像 PATCH 权限要求。

## 本地常驻 + 阿里云 ACS 混合云弹性

- VLM、TTS、ASR、LLM 等业务服务可使用独立令牌，按实例直接上报当前业务数、健康度、就绪状态和接单状态。
- 管理台新增默认及实例级业务上限、5 分钟统计窗口、扩缩阈值、冷却时间、ACS 副本边界和本地故障兜底副本配置。
- 本地实例只参与容量与故障判断，自动策略永远不会缩容本地常驻服务；ACS 突发池可保持 0 副本。
- 本地实例异常或停止上报时无需等待负载窗口，立即补充 ACS 兜底容量。
- ACS 缩容采用排空握手：先要求目标 Pod 停止接单，等待在途业务归零，再标记 Pod deletion cost 并修改 Kubernetes `scale` 子资源。
- 使用 Kubernetes JSON Patch 的前置副本数检查，避免与人工修改发生静默覆盖；同一工作负载不得再由 HPA/KEDA 等控制器管理副本数。

## 数据与安全

- 新增 `0008_hybrid_cloud_autoscaling` 和 `0009_module_image_releases` 迁移；SQLite 升级前创建 `cloudhelm.db.pre-0.9.1.bak` 一致性备份。
- 业务上报令牌只显示一次，Server 只保存 SHA-256 摘要；可在管理台轮换。
- ACS API 地址和凭据只从 Server 环境或只读 Secret 文件读取，不进入数据库和前端；TLS 校验不能关闭。
- 自动扩缩容全局开关默认关闭，升级不会自动创建或改变任何 ACS 资源。

## 兼容性

- 业务负载上报仍兼容旧 Agent；使用标签自动关联或分阶段镜像发布时必须升级到 0.9.1 Agent。
- SQLite 与 PostgreSQL 均支持新表；既有用户、权限、节点、容器、指标、告警和审计数据原样保留。
- `cloudhelm-0.9.1.tar.gz` 与 `.sha256`：架构无关源码和部署文件。
- `ghcr.io/xbl916/cloud-helm-server:0.9.1`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:0.9.1`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`。

发布产物不包含 `.env`、ACS Token/CA、企微 Secret、数据库、自动备份、Agent 注册令牌或节点身份文件。

全新部署、0.8.1 数据备份、Server 原地迁移、Agent 逐节点升级和回退命令见 `docs/deploy-and-upgrade-0.9.1.md`。
