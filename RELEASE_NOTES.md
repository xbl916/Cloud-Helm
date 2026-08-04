# Cloud Helm 0.4.2

## 自动初始化宿主机数据目录

- Server 和 Agent 默认使用发布目录下的 `./cloudhelm-data:/data` bind mount。
- 新增一次性 `data-init` 服务，在主服务启动前自动修复数据目录属主和 `0700` 权限。
- 初始化容器不读取业务 Secret、不连接网络、不挂载 Docker Socket，并仅获得目录初始化所需的最小 capabilities。
- Server 继续以固定的非 root 用户 `10001:10001` 运行，并保持只读根文件系统和 `cap_drop: ALL`。
- 不再要求部署人员查询镜像 UID 或手动执行 `chown`，也不需要使用不安全的 `chmod 777`。

## 容器运行默认值

- Server、Agent 和 PostgreSQL 默认使用 `Asia/Shanghai` 时区。
- 所有 Compose 服务使用 `json-file` 日志轮转，单文件上限 `50m`、最多 3 个文件，即每个容器约 `150m`。
- PostgreSQL 数据改为宿主机目录 `deploy/cloudhelm-postgres-data`。
- Git 和源码发布包明确排除 Server 数据、PostgreSQL 数据及 Agent 节点凭据。

## 配置与发布可靠性

- README 补齐 Server、Agent 和 PostgreSQL 的全部部署变量、默认值、取值范围与生产建议。
- 新增版本一致性检查，防止项目版本、应用版本、页面版本和 Compose 镜像标签不一致。
- 新增 CI：在全新的空数据目录中构建并启动 Server，验证自动初始化、SQLite 写入和健康检查。
- 推送 `vMAJOR.MINOR.PATCH` 标签后继续自动创建 GitHub Release，并构建多架构 Server 与 Agent 镜像。

## 发布产物

- `cloudhelm-0.4.2.tar.gz`：架构无关源码发布包。
- `cloudhelm-0.4.2.tar.gz.sha256`：源码包完整性校验。
- `ghcr.io/xbl916/cloud-helm-server:0.4.2`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:0.4.2`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`。

发布产物不包含 `.env`、数据库、企微 Secret、Agent 注册令牌或节点身份文件。本版本仍按全新安装设计，不包含旧数据库升级逻辑。
