# Cloud Helm 0.4.1

## 多架构容器发布

- 推送 `vMAJOR.MINOR.PATCH` 标签后自动创建 GitHub Release。
- 自动构建并发布 Server 与 Agent 两个 GHCR 镜像。
- Server 镜像支持 `linux/amd64` 和 `linux/arm64`。
- Agent 镜像支持 `linux/amd64`、`linux/arm64` 和 `linux/arm/v7`。
- 发布版本号、Git 标签和 `latest` 三组镜像标签。
- 镜像包含 SBOM、构建来源证明和 OCI 源码/版本/提交标签。
- GitHub Actions 第三方依赖固定到完整提交 SHA，并使用短期 `GITHUB_TOKEN` 发布。

## 多架构兼容

- Python 基础镜像固定为支持目标架构的 `3.12-slim-bookworm`。
- Agent 增加 ARMv7 构建与 manifest 验证。
- Agent 镜像不再安装中心端 Web 和数据库依赖，降低 ARMv7 构建复杂度与镜像体积。
- Server 使用纯 Python Uvicorn，并通过系统 `libpq` 和纯 Python Psycopg 保留 PostgreSQL 支持。
- Docker 构建上下文采用文件白名单，不会把 `.env`、数据库、Git 历史或构建产物发送给构建器。

## NVIDIA GPU 监控

- 节点展示型号、UUID、驱动/CUDA、利用率、显存、温度和功耗。
- 识别 Docker 容器 GPU 分配，并在容器详情展示分配卡的整卡指标。
- 项目/容器级用户只能看到其可见容器明确分配到的 GPU。
- GPU 节点使用 `agent.gpu.compose.yml` overlay，普通节点不受影响。

## 发布产物

- `cloudhelm-0.4.1.tar.gz`：架构无关源码发布包。
- `cloudhelm-0.4.1.tar.gz.sha256`：源码包完整性校验。
- `ghcr.io/xbl916/cloud-helm-server:0.4.1`：多架构 Server 镜像。
- `ghcr.io/xbl916/cloud-helm-agent:0.4.1`：多架构 Agent 镜像。

发布产物不包含 `.env`、数据库、企微 Secret、Agent 注册令牌或节点身份文件。本版本仍按全新安装设计，不包含旧数据库升级逻辑。
