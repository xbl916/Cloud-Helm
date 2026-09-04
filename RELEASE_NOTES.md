# Cloud Helm 0.9.9

## 安全的容器版本升级

- 更新服务镜像 Tag 时，Agent 会在白名单目录内定位对应的 Docker Compose service，备份并原子更新 `docker-compose.yml`，同时保留原有格式和注释。
- 升级前先拉取目标镜像并运行 Compose 配置校验；预检期间旧容器持续服务，不会提前停止容器或释放 GPU。
- 本地容器严格逐台排空、升级和业务健康确认。单本地实例场景会先启动旧版本 ACS 发布保障实例并确认可服务；多本地实例场景依靠其余本地实例持续承载。
- 全部本地实例升级成功后才更新常规高负载 ACS 工作负载使用的 Tag；发布保障弹性和高负载弹性分别记录为独立事件。
- 发布单、每个实例步骤和服务镜像历史持久化保存，管理台显示完整进度，并提供最近至少三个成功版本的指定回滚入口。
- 任一步骤失败会停止后续升级。Agent 在新容器替换失败时恢复 Compose 文件并尝试恢复旧容器。

## 手动维护

- 实例容量页面新增“进入维护”和“退出维护”操作。
- 进入维护先把实例切换为排空状态，网关不再分配新任务；待业务上报 `active_businesses=0` 后进入维护，便于管理员手工修改服务器上的 Compose 配置。
- 退出维护后需要实例重新上报健康、就绪且可接单，网关才会恢复路由。

## 数据、权限与兼容性

- 新增 `0015_durable_image_releases` 迁移，保存发布单、逐实例步骤、服务版本历史和当前活动发布。
- ACS 凭据仍只从 Server 环境变量或只读 Secret 文件读取；单实例发布保障要求 Kubernetes Deployment 的读取、创建和删除权限。
- Compose 文件只能位于 `CLOUDHELM_AGENT_COMPOSE_ROOTS` 显式允许的目录。没有唯一匹配 service、配置校验失败或目标镜像预拉取失败时，任务会在旧容器停止前失败。
- 自动伸缩全局开关仍默认关闭；升级 Cloud Helm 本身不会停止、重建或升级任何受管业务容器。

## 发布产物

- `cloudhelm-0.9.9.tar.gz` 与 `.sha256`：架构无关源码和部署文件。
- `ghcr.io/xbl916/cloud-helm-server:0.9.9`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:0.9.9`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`。

发布产物不包含 `.env`、ACS Token/CA、企微 Secret、数据库、自动备份、Agent 注册令牌或节点身份文件。

全新部署、从旧版本升级、迁移验证和回退步骤见 `docs/deploy-and-upgrade-0.9.9.md`。
