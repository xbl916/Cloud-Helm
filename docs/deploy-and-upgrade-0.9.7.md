# Cloud Helm 0.9.7 全新部署与 0.8.1 升级

本文提供两条独立路径：全新部署 0.9.7，以及保留数据从 0.8.1 原地升级。不要把两套命令混用。

0.9.6 升级到 0.9.7 时不需要手工执行 SQL：保留原 `.env` 和数据目录，替换本手册中的 Compose 文件并拉取 0.9.7 Server/Agent 镜像即可。Server 启动时会自动执行 `0014_ai_service_alert_permissions`，为现有资源授权表增加 AI 服务范围；既有用户、容器授权和告警订阅不变。Agent 应逐节点更新，不要批量重建 AI 业务容器，也不要释放 GPU。

发布产物：

- `cloudhelm-0.9.7.tar.gz`
- `cloudhelm-0.9.7.tar.gz.sha256`
- `ghcr.io/xbl916/cloud-helm-server:0.9.7`
- `ghcr.io/xbl916/cloud-helm-agent:0.9.7`

## 一、全新部署 0.9.7

### 1. 前置条件

- 管理中心：64 位 Linux、Docker Engine、Docker Compose v2、2 核 CPU、2 GB 内存、10 GB 可用空间。
- 一个能够从企业微信访问的 HTTPS 域名；后端 `8080` 只绑定 `127.0.0.1`，不要直接暴露公网。
- 每台被管理节点可主动访问管理中心 `443`，并允许 Agent 只读写 Docker socket；节点不需要开放入站端口。
- NVIDIA 节点已安装驱动和 NVIDIA Container Toolkit，宿主机执行 `nvidia-smi` 正常。

### 2. 校验并解压

```bash
sha256sum -c cloudhelm-0.9.7.tar.gz.sha256
tar -xzf cloudhelm-0.9.7.tar.gz
cd cloudhelm-0.9.7
```

校验必须输出 `cloudhelm-0.9.7.tar.gz: OK`。失败时停止部署并重新获取发布包。

### 3. 配置 Server

```bash
cp .env.example .env
chmod 600 .env
install -d -m 0700 cloudhelm-data
openssl rand -hex 32
```

编辑 `.env`，至少替换以下内容：

```dotenv
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=独立的32字节以上随机令牌
CLOUDHELM_PUBLIC_BASE_URL=https://ops.company.com
CLOUDHELM_WECOM_CORP_ID=企业CorpID
CLOUDHELM_WECOM_AGENT_ID=企业微信应用AgentId
CLOUDHELM_WECOM_SECRET=企业微信应用Secret
CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID=首位管理员UserId
CLOUDHELM_DATABASE_URL=sqlite:////data/cloudhelm.db
CLOUDHELM_ENVIRONMENT=production
# 仅在接入统一网关时填写，不能与任何其他令牌复用。
# CLOUDHELM_GATEWAY_DISCOVERY_TOKEN=独立的32字节以上随机令牌
# CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS=10.10.0.0/16,10.20.0.0/16
```

首次部署保持以下开关关闭：

```dotenv
CLOUDHELM_AUTOSCALING_ENABLED=false
CLOUDHELM_ALERT_NOTIFICATIONS_ENABLED=false
```

ACS API、CA、Token、最小 RBAC 和业务排空尚未验证前，不要打开自动伸缩。ACS Secret 只以只读文件挂载到 Server，不能写入镜像、Git、数据库或前端配置。

### 4. 启动管理中心

```bash
docker compose config
docker compose pull server
docker compose up -d --no-build --remove-orphans server
docker compose ps
docker compose logs --tail=100 server
curl --fail http://127.0.0.1:8080/healthz
```

健康响应应为 `{"status":"ok"}`，`docker compose port server 8080` 应显示 `127.0.0.1` 绑定。再按主 README 配置 Caddy/Nginx 和企业微信可信域名，确认公网 `https://实际域名/healthz` 正常并完成首次管理员登录。

### 5. 逐台部署 Agent

在每台 Docker 节点解压同一发布包：

```bash
cd cloudhelm-0.9.7/deploy
cp agent.env.example agent.env
chmod 600 agent.env
install -d -m 0700 cloudhelm-data
```

至少填写：

```dotenv
CLOUDHELM_AGENT_SERVER_URL=https://ops.company.com
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=与Server一致的首次注册令牌
CLOUDHELM_AGENT_NAME=唯一且可识别的节点名
CLOUDHELM_AGENT_ENVIRONMENT=production
CLOUDHELM_AGENT_VERIFY_TLS=true
```

非 GPU 节点：

```bash
docker compose -f agent.compose.yml config
docker compose -f agent.compose.yml pull agent
docker compose -f agent.compose.yml up -d --no-build agent
docker compose -f agent.compose.yml logs --tail=100 agent
```

NVIDIA GPU 节点：

```bash
docker compose -f agent.compose.yml -f agent.gpu.compose.yml config
docker compose -f agent.compose.yml -f agent.gpu.compose.yml pull agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d --no-build agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml logs --tail=100 agent
```

确认管理台节点在线、Agent 版本为 0.9.7、容器和 GPU 数量正确后，再部署下一台。Agent 更新或启动不会停止节点上的 AI 业务容器。

### 6. 接入业务与 ACS

1. 在管理台创建 VLM/TTS/ASR/LLM 服务，复制 `service_id` 并保存只显示一次的 `report_token`。这是先创建逻辑服务、再由容器上报实例，不是预先手工创建每个容器。
2. 给本地 AI 容器注入管理平台地址、`service_id`、稳定且唯一的 `instance_id`，并把令牌以只读文件挂载；配置相同的 `cloudhelm.service_id`、`cloudhelm.instance_id` 标签供 Agent 自动关联。
3. 容器应用必须直接读取 `/run/secrets/cloudhelm_service_token`，令牌不要写入镜像、Compose YAML 或环境变量。该文件在宿主机仍是明文，应限制权限并纳入受控 Secret 管理；具体 UID/GID 检查和 Compose 示例见混合云文档。
4. ACS Pod 没有主机 Agent，与本地使用同一个业务镜像、`service_id` 和 `report_token`，但通过 Downward API 以 Pod 名上报 `pool=acs` 和 Pod IP，禁止携带本地 `container_id`。
5. 在每台本地节点的详情中配置统一网关实际访问该服务器所用的 IP；先验证业务上报、自动关联、网关路由、Ready、排空及 ACS 手工扩缩容。
6. 最后才启用全局 ACS 连接开关和单服务自动伸缩策略。

具体接口、RBAC 和上线检查见[混合云弹性文档](hybrid-autoscaling.md)，镜像升级顺序见[模块分阶段发布文档](module-image-releases.md)。

## 二、从 0.8.1 原地升级到 0.9.7

### 1. 升级边界

0.9.7 会新增以下数据库对象：

- `scaling_services`、`service_instances`、`business_metric_samples`、`scaling_events`；
- 模块期望镜像、ACS 目标容器和分阶段发布状态字段；
- PC 企业微信扫码登录的认证来源字段；
- 服务实例网关端点和节点手工网关访问 IP 字段；
- 节点历史采样的业务数、健康接单容量和实例数字段；
- AI 服务级只读、配置、发布和企微告警授权字段；
- `0008_hybrid_cloud_autoscaling` 至 `0014_ai_service_alert_permissions` 迁移记录。

迁移是增量式的，不重建已有表，不修改已有用户、企微身份、权限、节点凭据、容器、指标、告警和审计记录。普通微信登录未启用；PC 浏览器只允许企业微信扫码。全局 ACS 自动化默认关闭，因此仅升级不会创建、扩容、缩容或更新 ACS 工作负载。

### 2. 升级前检查与备份

先确认当前确实运行 0.8.1，并保存状态：

```bash
cd /实际的0.8.1部署目录
docker compose ps
docker compose images
docker compose logs --tail=100 server
```

SQLite 部署先短暂停止 Server，再备份；Agent 和 AI 业务容器保持运行：

```bash
docker compose stop server
sudo tar -czf ../cloudhelm-0.8.1-before-0.9.7.tar.gz -C cloudhelm-data .
chmod 600 ../cloudhelm-0.8.1-before-0.9.7.tar.gz
test -s ../cloudhelm-0.8.1-before-0.9.7.tar.gz
```

如果使用 PostgreSQL，先执行逻辑备份，再停止 Server：

```bash
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml exec -T database \
  pg_dump -U cloudhelm -d cloudhelm --format=custom \
  > ../cloudhelm-0.8.1-before-0.9.7.dump
test -s ../cloudhelm-0.8.1-before-0.9.7.dump
chmod 600 ../cloudhelm-0.8.1-before-0.9.7.dump
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml stop server
```

把备份复制到另一台受控设备并记录校验值。不要删除、清空或覆盖现有数据目录。

### 3. 准备 0.9.7 文件

在另一目录校验并解压 0.9.7 发布包：

```bash
sha256sum -c cloudhelm-0.9.7.tar.gz.sha256
tar -xzf cloudhelm-0.9.7.tar.gz
```

保留 0.8.1 部署目录中的 `.env`、`cloudhelm-data`、`deploy/agent.env`、`deploy/cloudhelm-data` 和 PostgreSQL 数据目录。不要用 `.env.example`、`agent.env.example` 或空目录覆盖它们。

把下面这些默认关闭的变量追加到现有 Server `.env`；不计划接入 ACS 时也可以暂时不添加，代码默认值相同：

```dotenv
CLOUDHELM_AUTOSCALING_ENABLED=false
CLOUDHELM_AUTOSCALING_INTERVAL_SECONDS=15
CLOUDHELM_AUTOSCALING_HISTORY_RETENTION_HOURS=24
CLOUDHELM_ACS_KUBERNETES_TIMEOUT_SECONDS=10
CLOUDHELM_IMAGE_RELEASE_TIMEOUT_SECONDS=3600
# 仅在接入统一网关时填写；二者必须同时配置。
# CLOUDHELM_GATEWAY_DISCOVERY_TOKEN=独立的32字节以上随机令牌
# CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS=10.10.0.0/16,10.20.0.0/16
```

### 4. 升级 Server

SQLite 部署：

```bash
cp /0.9.7解压目录/docker-compose.yml ./docker-compose.yml
docker compose config
docker compose pull server
docker compose up -d --no-build --remove-orphans server
docker compose logs --tail=150 server
curl --fail http://127.0.0.1:8080/healthz
curl --fail https://ops.company.com/healthz
```

PostgreSQL 部署改为复制 `deploy/postgres.compose.yml`，并继续使用原来的 `deploy/postgres.env`：

```bash
cp /0.9.7解压目录/deploy/postgres.compose.yml ./deploy/postgres.compose.yml
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml config
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml pull server
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml up -d --no-build --remove-orphans server database
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml logs --tail=150 server database
curl --fail https://ops.company.com/healthz
```

SQLite 首次启动会在迁移前自动创建 `cloudhelm-data/cloudhelm.db.pre-0.9.7.bak` 并执行完整性检查。确认管理台显示 0.9.7，企微 H5 与 PC 扫码登录、用户权限、节点列表、容器数量、历史指标、告警和审计均正常。此时自动伸缩仍应显示为关闭。

### 5. 逐节点升级 Agent

不要同时重建所有节点的 Agent。每次只升级一台，确认重新上线后再继续下一台。该操作只替换 Agent 容器，不会停止或重建 AI 业务容器，也不会释放 GPU。

在每个 0.8.1 Agent 部署目录中保留原有 `agent.env` 和 `cloudhelm-data`，复制新版 Compose：

```bash
cp /0.9.7解压目录/deploy/agent.compose.yml ./agent.compose.yml
cp /0.9.7解压目录/deploy/agent.gpu.compose.yml ./agent.gpu.compose.yml
```

GPU 节点：

```bash
docker compose -f agent.compose.yml -f agent.gpu.compose.yml config
docker compose -f agent.compose.yml -f agent.gpu.compose.yml pull agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d --no-build agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml logs --tail=100 agent
```

非 GPU 节点删除所有 `-f agent.gpu.compose.yml`。在管理台确认该节点重新在线、Agent 版本为 0.9.7、原节点 ID 未变化、容器/GPU 状态正确，再处理下一台。

升级平台和 Agent 时不要顺便批量重建 AI 容器。业务负载上报、自动关联标签和分阶段镜像发布应在平台升级验证完成后单独接入；同一服务的多个 GPU 容器仍按逐个验证的方式变更。

### 6. 升级验收

- Server 和全部 Agent 显示 0.9.7，所有既有节点仍使用原节点身份。
- 企微登录、角色、资源授权、告警订阅和审计记录正常。
- 既有容器和 GPU 数量与升级前一致，没有业务容器被重建。
- 全局 `CLOUDHELM_AUTOSCALING_ENABLED=false`，尚未误操作 ACS。
- 新建测试服务后，业务上报可用；本地标签只关联 `local`，ACS `pool=acs` 不出现主机容器关联。
- 网关使用独立只读令牌获取全量路由；节点手工网关 IP 生效，实例异常、排空或过期后会从路由中移除。
- 在非生产服务完成一次严格串行镜像发布演练后，再用于生产模块。

### 7. 回退

如果 0.9.7 Server 无法启动，先保存日志，再把 Compose 中 Server 镜像改回 `ghcr.io/xbl916/cloud-helm-server:0.8.1` 并重新启动。0.8.1 不会读取新增表；不要立即恢复数据库或删除新增对象。

如果必须精确恢复升级前数据状态：停止 Server，把当前数据目录移动到明确命名的故障保留目录，再从已验证的升级前备份恢复。PostgreSQL 使用现有 DBA 恢复流程恢复 `.dump`。这是破坏性恢复操作，必须先复核路径和备份可读性。

Agent 可在单个节点把镜像改回 `ghcr.io/xbl916/cloud-helm-agent:0.8.1` 后重新执行该节点的 `up -d agent`；保留 `agent.env` 和 `cloudhelm-data`，不需要重新注册，也不要操作业务容器或 GPU。
