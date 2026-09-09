# Cloud Helm 1.0.0 全新部署与旧版本升级

适用于全新安装，以及从 0.9.9、0.8.1 等旧版本升级。1.0.0 增加服务/节点权限修复、企微成员变更回调和管理员节点凭据恢复。升级管理平台不会自动升级受管业务容器。

准备好新版本、完成配置和备份之后再切换管理服务。不要提前停止业务容器或释放 GPU；不要执行 `docker compose down -v`、删除数据目录或重新初始化数据库。

## 一、获取发布文件

Linux 主机需已安装 Docker Engine、Docker Compose v2、curl 和用于校验的 sha256sum。Server 支持 amd64/arm64；Agent 还支持 arm/v7。管理中心需要正式域名、有效 HTTPS 和企业微信自建应用。NVIDIA 节点需已有驱动和 NVIDIA Container Toolkit。

```bash
curl -fLO https://github.com/xbl916/Cloud-Helm/releases/download/v1.0.0/cloudhelm-1.0.0.tar.gz
curl -fLO https://github.com/xbl916/Cloud-Helm/releases/download/v1.0.0/cloudhelm-1.0.0.tar.gz.sha256
sha256sum -c cloudhelm-1.0.0.tar.gz.sha256
tar -xzf cloudhelm-1.0.0.tar.gz
```

预期为 `cloudhelm-1.0.0.tar.gz: OK`。镜像由 Release 工作流构建完成后发布；如果下载暂时返回 404，应确认发布流程是否已完成，不要改用未知镜像。

## 二、全新安装

### 1. 配置管理中心

```bash
cd cloudhelm-1.0.0
cp .env.example .env
chmod 600 .env
install -d -m 0700 cloudhelm-data
openssl rand -hex 32
```

编辑 `.env`，替换全部必填示例值：

```dotenv
CLOUDHELM_ENVIRONMENT=production
CLOUDHELM_PUBLIC_BASE_URL=https://ops.company.com
CLOUDHELM_WECOM_CORP_ID=你的企业CorpID
CLOUDHELM_WECOM_AGENT_ID=你的自建应用AgentId
CLOUDHELM_WECOM_SECRET=你的自建应用Secret
CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID=初始管理员的准确UserId
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=上一步生成的随机值
CLOUDHELM_DATABASE_URL=sqlite:////data/cloudhelm.db
CLOUDHELM_BIND_ADDRESS=127.0.0.1
CLOUDHELM_PORT=8080
```

示例域名需替换为实际域名。不要覆盖配置文件中的其余默认项。Secret 仅写入服务器文件，不发给前端或 Agent。默认关闭自动伸缩；先完成 ACS 和业务排空接入，再按需启用。

```bash
docker compose config --quiet
docker compose pull server
docker compose up -d --no-build --wait server
docker compose ps
docker compose logs --tail=100 server
curl --fail http://127.0.0.1:8080/healthz
```

本项目管理服务的 Compose 名称是 `server`。首次启动会初始化数据库和初始管理员。健康接口应返回 `{"status":"ok"}`。

### 2. 配置 HTTPS 与企微登录

域名解析到管理中心；防火墙开放必要的 80/443，8080 保持回环绑定。使用 Caddy 时，安装 Caddy 后参考 `deploy/Caddyfile.example` 配置：

```caddyfile
ops.company.com {
    reverse_proxy 127.0.0.1:8080
}
```

将域名替换为真实值，用 `caddy validate --config /etc/caddy/Caddyfile` 验证后 reload。企业微信后台创建自建应用、配置可信域名并完成域名校验，应用主页设置为：

```text
https://ops.company.com/api/v1/auth/wecom/start
```

PC 扫码与 H5 使用同一回调地址 `/api/v1/auth/wecom/callback`。初始管理员的 UserId 必须是企微通讯录中的准确标识。首次登录后为其他成员创建平台账号并按资源授权；未绑定成员默认拒绝。

### 3. 安装节点 Agent

在每台被管理节点上解压同一发布包，执行：

```bash
cd cloudhelm-1.0.0/deploy
cp agent.env.example agent.env
chmod 600 agent.env
```

编辑 `agent.env`：

```dotenv
CLOUDHELM_AGENT_SERVER_URL=https://ops.company.com
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=与管理中心相同的注册令牌
CLOUDHELM_AGENT_NAME=唯一且易识别的节点名称
CLOUDHELM_AGENT_ENVIRONMENT=production
CLOUDHELM_AGENT_VERIFY_TLS=true
```

普通节点：

```bash
docker compose -f agent.compose.yml config --quiet
docker compose -f agent.compose.yml pull agent
docker compose -f agent.compose.yml up -d --no-build agent
docker compose -f agent.compose.yml logs --tail=100 agent
```

NVIDIA 节点改用下列命令，保留原有 GPU overlay 及 Toolkit 设置：

```bash
docker compose -f agent.compose.yml -f agent.gpu.compose.yml config --quiet
docker compose -f agent.compose.yml -f agent.gpu.compose.yml pull agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d --no-build agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml logs --tail=100 agent
```

确认平台中节点在线、容器及 GPU 清单正确后，备份 `deploy/cloudhelm-data/agent-state.json`。首次注册成功后可从 `agent.env` 删除共享注册令牌，并沿用上面的 `up` 命令只重建 Agent；保留独立状态文件，受管业务容器不受影响。

### 4. 可选：PostgreSQL

需要 PostgreSQL 时，应在首次启动 Server 前选择该方式；不要先初始化 SQLite 后直接改数据库地址并以为空间中的账号和节点会自动迁移。

```bash
cp deploy/postgres.env.example deploy/postgres.env
chmod 600 deploy/postgres.env
```

编辑 `POSTGRES_PASSWORD`，然后在项目根目录执行：

```bash
docker compose --env-file deploy/postgres.env -f deploy/postgres.compose.yml config --quiet
docker compose --env-file deploy/postgres.env -f deploy/postgres.compose.yml pull
docker compose --env-file deploy/postgres.env -f deploy/postgres.compose.yml up -d --no-build --wait
```

### 5. 启用企微成员自动撤权

新增配置为 `CLOUDHELM_WECOM_CALLBACK_TOKEN` 和 `CLOUDHELM_WECOM_CALLBACK_AES_KEY`，接收地址为 `/api/v1/auth/wecom/events`。两项须与企微后台保持一致并同时设置；AES key 使用后台生成的 43 字符 EncodingAESKey。

按照 [安全接入说明](security-hardening.md) 完成通讯录事件配置、URL 验证及测试成员删除/重建验证。仅配置企微登录或仅更新代码，不会自动启用这项同步。回调未接入或中断时，离职流程仍需在平台中停用账号。

## 三、从旧版本升级

### 1. 保留原目录，准备镜像和备份

先在独立目录解压新版包。后续命令在**原部署目录**执行，沿用原来的 Compose 项目名、`.env`、数据库目录、节点状态文件与自定义挂载。不要直接进入新版空目录启动，否则相对路径可能指向新数据库。

确认没有正在进行的业务镜像发布，核对各服务实例的容器和 Kubernetes 绑定。记录旧镜像版本并保留镜像；将当前配置备份到受保护目录：

```bash
cd /你的原部署目录
backup_dir="$(mktemp -d "$PWD/upgrade-1.0.0-backup-XXXXXX")"
chmod 700 "$backup_dir"
cp -p .env docker-compose.yml "$backup_dir/"
docker pull ghcr.io/xbl916/cloud-helm-server:1.0.0
docker pull ghcr.io/xbl916/cloud-helm-agent:1.0.0
```

SQLite 默认路径可在 Server 运行时使用 SQLite backup API 创建一致性备份，而不是直接复制正在写入的数据库。使用其他数据库位置时调整以下路径：

```bash
docker compose exec -T server python - <<'PY'
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

source = sqlite3.connect('/data/cloudhelm.db')
assert source.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
path = Path('/data') / ('pre-1.0.0-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.db')
with sqlite3.connect(path) as target:
    source.backup(target)
    assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
source.close()
path.chmod(0o600)
print(path)
PY
```

把生成的备份另存到受保护的备份位置。PostgreSQL 则使用现有配置备份，替代上述 SQLite 命令：

```bash
cp -p deploy/postgres.env deploy/postgres.compose.yml "$backup_dir/"
docker compose --env-file deploy/postgres.env -f deploy/postgres.compose.yml \
    exec -T database pg_dump -U cloudhelm -d cloudhelm -Fc > "$backup_dir/cloudhelm.dump"
```

### 2. 合并配置并切换 Server

参考新版 `docker-compose.yml` 合并配置，将 `server.image` 改为 `ghcr.io/xbl916/cloud-helm-server:1.0.0`。保留原数据目录、端口、自定义 Secret/CA 挂载及代理配置；不要用 `.env.example` 覆盖现有 `.env`。

```bash
docker compose config --quiet
docker compose up -d --no-build --no-deps --wait server
docker compose logs --tail=100 server
curl --fail http://127.0.0.1:8080/healthz
```

PostgreSQL 部署修改 `deploy/postgres.compose.yml` 中 Server 镜像，使用原 Compose 参数，仅切换 Server，不重启数据库：

```bash
docker compose --env-file deploy/postgres.env -f deploy/postgres.compose.yml config --quiet
docker compose --env-file deploy/postgres.env -f deploy/postgres.compose.yml \
    up -d --no-build --no-deps --wait server
```

启动会增量迁移至 `0016_wecom_event_receipts`，保留原数据；SQLite 有待执行迁移时沿用自动备份。新增迁移仅增加事件摘要表和版本记录。管理接口切换期间可能短暂不可用，Agent 会自动重试，业务容器继续运行。

### 3. 逐节点更新 Agent

在各节点先保存 `agent.env`、Agent Compose、GPU overlay 和 `cloudhelm-data/agent-state.json`，然后合并新版 Agent Compose，将镜像改为 `ghcr.io/xbl916/cloud-helm-agent:1.0.0`。保留自定义业务 Compose 路径挂载与 `CLOUDHELM_AGENT_COMPOSE_ROOTS`。

在原 Agent 部署目录执行：

```bash
docker compose -f agent.compose.yml -f agent.gpu.compose.yml pull agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml config --quiet
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d --no-build --no-deps agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml logs --tail=100 agent
```

非 GPU 节点去掉第二个 `-f` 参数。每台上线并验证后再处理下一台，不重新注册。凭据丢失时通过管理员恢复接口处理，见 [安全接入说明](security-hardening.md)。

### 4. 兼容性与验收

- 页面和 Agent 版本显示 1.0.0，原账号、权限、节点、容器及历史数据仍存在。
- 业务上报仍正常；已有绑定可以省略 `container_id` 或上报同一 ID。首次依赖该字段建立关联的旧客户端会收到 403，须改为 Agent 标签绑定。
- 新增 Docker 标签如需重建业务容器，应在正常业务版本切换中处理，不要为了这次平台升级提前停止 GPU 业务。
- 普通操作员仍可调整策略，但不能更换命名空间、工作负载或目标容器。
- 重复注册已有节点返回 409，原凭据仍有效。
- 企微成员回调使用测试成员验收；没有完成后台接入时，不宣称已实现自动撤权。
- 从 0.8.1 等较早版本升级时，同时参考 [模块发布说明](module-image-releases.md) 补齐 Compose 路径白名单、ACS RBAC 和业务排空协议；先保持自动伸缩关闭，验证完毕再开启。

## 四、回退

保留失败日志。在原 Compose 中将 Server 镜像改回升级前记录的版本，确认旧镜像已就绪后，只执行针对 `server` 的 `up -d --no-build --no-deps --wait`。不要删除数据库或迁移记录。

`0016` 是增量表迁移；0.9.9 可以忽略新增表，但回退会失去本次新增权限保护和企微回调处理。从更早版本跨多个迁移升级后，不应直接假定数据库可向下兼容；应先用备份副本验证旧版本，必要时仅停止管理服务后恢复对应数据库备份。恢复备份会丢失备份之后的管理数据，应明确取舍。

Agent 回退也只切换 Agent 镜像并保留状态文件，每次处理一台。全程不对受管业务容器执行 stop/down，不释放 GPU。
