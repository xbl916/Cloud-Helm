# 云舵 Cloud Helm

云舵是面向企业微信 H5 和企业自用小程序的多节点 Docker 运维控制台。管理中心集中处理企微身份、资源权限和审计；每台 Docker 主机运行主动出站连接的 Agent，因此节点不需要公网 IP，也不需要暴露 Docker API。

当前版本为 `0.5.0`，按全新部署设计，不包含旧版账号密码登录或旧数据库升级逻辑。

快速导航：

- [安全模型](#安全模型)
- [详细部署手册](#详细部署手册)
- [NVIDIA GPU 监控](#nvidia-gpu-监控)
- [人员与容器权限](#人员与容器权限)
- [企业自用小程序](#企业自用小程序)
- [实际使用中的安全注意事项](#实际使用中的安全注意事项)
- [PostgreSQL 部署](#postgresql-部署)
- [备份](#备份)

## 安全模型

```text
企业微信成员
  │ H5 OAuth 或小程序 wx.qy.login（确认人员身份）
  ▼
Cloud Helm Server
  │ 服务端短期会话 + CSRF/Origin 校验
  │ 用户角色 + 资源范围（确认能看什么、能做什么）
  ▼
Agent 主动轮询 + 每节点独立令牌
  │ logs / start / stop / restart 白名单
  ▼
Docker Engine
```

- 云舵不提供公网密码登录，只接受企业微信 OAuth。
- 企微返回的 `UserId` 必须提前绑定；未绑定或停用成员默认拒绝。
- H5 浏览器仅保存 `HttpOnly + Secure + SameSite` 随机会话 Cookie，不使用 `localStorage` JWT。
- 小程序只保存短期随机 Bearer 会话；它不是自包含 JWT，服务端仅保存其 SHA-256 摘要并可立即撤销。
- OAuth `state` 绑定浏览器、五分钟过期且只能使用一次。
- 写操作必须同时通过同源 `Origin` 和 CSRF 校验。
- 普通用户创建后默认没有任何容器权限；管理员明确授权后才可访问。
- 每个 Agent 使用独立令牌，只接受日志、启动、停止和重启四类任务。
- 服务端不向 Agent 发送任意命令，不支持删除容器、拉取镜像或修改挂载。

企微授权地址和成员身份接口依据[企业微信官方网页授权文档](https://developer.work.weixin.qq.com/document/path/91022)和[获取访问用户身份文档](https://developer.work.weixin.qq.com/document/path/91023)实现。

## 详细部署手册

以下流程按全新 Linux 服务器编写。推荐拓扑是：Caddy 直接安装在管理中心宿主机，Cloud Helm Server 运行在 Docker 中并只绑定宿主机回环地址；每台被管理的 Docker 主机各运行一个 Agent。

### 1. 部署前检查

管理中心最低建议配置：

- 2 核 CPU、2 GB 内存、10 GB 可用磁盘；
- 64 位 Linux，支持 `amd64` 或 `arm64`；
- Docker Engine 和 Docker Compose v2；
- 可对外提供 HTTPS 的正式域名；
- 系统时间已启用 NTP 同步；
- 能主动访问 `open.weixin.qq.com:443` 和 `qyapi.weixin.qq.com:443`。

每台 Agent 节点需要：

- Linux Docker 主机；
- 能主动访问管理中心域名的 `443`；
- 能挂载本机 `/var/run/docker.sock`；
- 不需要任何公网入站端口。

需要 GPU 监控的 NVIDIA 节点还应安装 NVIDIA 驱动和 NVIDIA Container Toolkit，并确保宿主机运行 `nvidia-smi` 正常。Agent 只需要 Toolkit 提供的 `utility` 驱动能力，不需要在 Agent 镜像内安装完整 CUDA 工具链。

检查 Docker：

```bash
docker version
docker compose version
```

如果尚未安装，请按 [Docker Engine 官方安装文档](https://docs.docker.com/engine/install/)选择对应发行版安装，不要使用来源不明的镜像源或安装脚本。

端口规划：

|位置|方向|端口|用途|是否公网开放|
|---|---|---:|---|---|
|管理中心|入站|443|HTTPS 页面、OAuth 回调、Agent API|是|
|管理中心|入站|80|HTTPS 跳转和证书签发|可选|
|管理中心|入站|8080|Cloud Helm 容器后端|否，只绑定 `127.0.0.1`|
|管理中心|出站|443|访问企业微信 API|是|
|Agent 节点|出站|443|上报状态和轮询任务|是|
|Agent 节点|入站|任意|云舵 Agent 不需要入站|否|
|Docker 主机|入站|2375/2376|Docker Remote API|不要开放|

### 2. 校验并解压发布包

把 `.tar.gz` 和 `.sha256` 放在同一目录，先验证文件未损坏或被替换：

```bash
sha256sum -c cloudhelm-0.5.0.tar.gz.sha256
tar -xzf cloudhelm-0.5.0.tar.gz
cd cloudhelm-0.5.0
```

预期输出包含：

```text
cloudhelm-0.5.0.tar.gz: OK
```

如果校验失败，不要继续部署，应重新获取发布包。

### 3. 配置 DNS 和防火墙

为管理中心配置独立域名，例如 `ops.company.com`：

1. 添加指向管理中心公网 IP 的 `A` 记录；使用 IPv6 时同时检查 `AAAA` 记录是否真的可达。
2. 等待解析生效，并从外部网络确认结果。
3. 在云安全组和主机防火墙中只放行必要的 `80/443`。
4. 保留已有 SSH 管理规则，避免修改防火墙时把自己锁在服务器之外。

```bash
getent hosts ops.company.com
```

不要为 `8080`、PostgreSQL `5432` 或 Docker `2375/2376` 创建公网规则。

### 4. 创建企业微信自建应用

1. 在企业微信管理后台创建自建应用。
2. 将应用可见范围限制为实际需要使用云舵的人员或部门。
3. 记录企业 `CorpID`、应用 `AgentId` 和应用 `Secret`。
4. 按企业微信后台要求配置 `ops.company.com` 为应用可信域名，并完成域名归属校验。
5. 将应用主页设置为：

   ```text
   https://ops.company.com/api/v1/auth/wecom/start
   ```

6. 从企业微信通讯录确认首位管理员的 `UserId`。

`UserId` 不是姓名、手机号、邮箱或页面显示昵称，大小写也应保持一致。应用主页域名、可信域名和后面的 `CLOUDHELM_PUBLIC_BASE_URL` 必须完全一致。`Secret` 只能写入管理中心服务器，不得发送给浏览器、Agent，也不要粘贴到聊天或工单中。

企微授权流程依据[构造网页授权链接](https://developer.work.weixin.qq.com/document/path/91022)和[获取访问用户身份](https://developer.work.weixin.qq.com/document/path/91023)实现。

### 5. 配置管理中心环境变量

在发布包根目录执行：

```bash
cp .env.example .env
chmod 600 .env
install -d -m 0700 cloudhelm-data
openssl rand -hex 32
```

把随机结果写入 `CLOUDHELM_AGENT_ENROLLMENT_TOKEN`，再编辑 `.env`：

```dotenv
TZ=Asia/Shanghai
CLOUDHELM_APP_NAME="云舵 Cloud Helm"
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=替换为openssl生成的随机值
CLOUDHELM_PUBLIC_BASE_URL=https://ops.company.com
CLOUDHELM_WECOM_CORP_ID=wwxxxxxxxxxxxxxxxx
CLOUDHELM_WECOM_AGENT_ID=1000002
CLOUDHELM_WECOM_SECRET=应用的Secret
# CLOUDHELM_WECOM_MINIPROGRAM_SECRET=关联小程序在当前企业下的Secret
CLOUDHELM_WECOM_API_TIMEOUT_SECONDS=8
CLOUDHELM_WECOM_API_BASE=https://qyapi.weixin.qq.com
CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID=首位管理员的企微UserId
CLOUDHELM_BOOTSTRAP_ADMIN_DISPLAY_NAME=系统管理员
CLOUDHELM_DATABASE_URL=sqlite:////data/cloudhelm.db
CLOUDHELM_ENVIRONMENT=production
CLOUDHELM_SESSION_MINUTES=60
CLOUDHELM_MAX_SESSIONS_PER_USER=5
CLOUDHELM_OAUTH_STATE_SECONDS=300
CLOUDHELM_NODE_OFFLINE_SECONDS=60
CLOUDHELM_MAX_TASK_RESULT_BYTES=262144
CLOUDHELM_TRUST_PROXY_HEADERS=true
CLOUDHELM_BIND_ADDRESS=127.0.0.1
CLOUDHELM_PORT=8080
```

变量说明：

|变量|说明|生产建议|
|---|---|---|
|`TZ`|容器系统时区|默认 `Asia/Shanghai`|
|`CLOUDHELM_APP_NAME`|网页标题和 API 服务名称|默认 `云舵 Cloud Helm`，可按企业名称修改|
|`CLOUDHELM_AGENT_ENROLLMENT_TOKEN`|Agent 首次注册的共享令牌|至少 32 字节随机值|
|`CLOUDHELM_PUBLIC_BASE_URL`|用户实际访问的公网根地址|只包含 `https://域名`，不要带路径和结尾 `/`|
|`CLOUDHELM_WECOM_CORP_ID`|企业 ID|从企微管理后台复制|
|`CLOUDHELM_WECOM_AGENT_ID`|自建应用 AgentId|从应用详情复制|
|`CLOUDHELM_WECOM_SECRET`|自建应用 Secret|仅服务器保存|
|`CLOUDHELM_WECOM_MINIPROGRAM_SECRET`|关联小程序在当前企业下的 Secret|仅启用小程序时填写；只保存于 Server，不是微信小程序 AppSecret|
|`CLOUDHELM_WECOM_API_TIMEOUT_SECONDS`|调用企微 API 的单次超时秒数|默认 8，可设 2–30|
|`CLOUDHELM_WECOM_API_BASE`|企微 API 根地址|保持官方地址 `https://qyapi.weixin.qq.com`|
|`CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID`|首次创建的管理员身份|填写准确企微 `UserId`|
|`CLOUDHELM_BOOTSTRAP_ADMIN_DISPLAY_NAME`|首次创建的管理员显示名称|默认 `系统管理员`，最长 120 字符|
|`CLOUDHELM_DATABASE_URL`|服务端数据库连接地址|单机初始部署保持 `sqlite:////data/cloudhelm.db`|
|`CLOUDHELM_ENVIRONMENT`|运行环境；生产模式会强制 HTTPS 并关闭 API 文档|正式部署保持 `production`|
|`CLOUDHELM_SESSION_MINUTES`|网页会话绝对有效期|默认 60，可设 5–1440；生产建议不超过 60|
|`CLOUDHELM_MAX_SESSIONS_PER_USER`|单用户最大并发会话|默认 5，可设 1–20|
|`CLOUDHELM_OAUTH_STATE_SECONDS`|企微 OAuth 临时 state 的有效秒数|默认 300，可设 60–600|
|`CLOUDHELM_NODE_OFFLINE_SECONDS`|多久未收到 Agent 上报后判定节点离线|默认 60，可设 15–3600；应大于 Agent 上报间隔|
|`CLOUDHELM_MAX_TASK_RESULT_BYTES`|每次容器任务结果允许保存的最大字节数|默认 262144，可设 4096–2097152|
|`CLOUDHELM_TRUST_PROXY_HEADERS`|读取代理转发的真实 IP|仅在后端只允许可信代理访问时设为 `true`|
|`CLOUDHELM_BIND_ADDRESS`|宿主机监听地址|保持 `127.0.0.1`|
|`CLOUDHELM_PORT`|宿主机后端端口|默认 8080，不对公网开放|

`CLOUDHELM_BIND_ADDRESS` 和 `CLOUDHELM_PORT` 由 Docker Compose 用于端口映射，其余 `CLOUDHELM_*` 变量由 Server 读取。代码还支持内部变量 `CLOUDHELM_STATIC_DIR`，其默认值指向镜像内随包安装的静态资源目录，常规部署不要设置。

程序会拒绝带有 `REPLACE_ME` 的示例 Secret、生产环境 HTTP 地址以及示例域名，避免误带样例配置上线。

### 6. 安装并配置 Caddy

按照 [Caddy 官方安装说明](https://caddyserver.com/docs/install)将 Caddy 安装为宿主机 systemd 服务。然后复制示例配置：

```bash
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo editor /etc/caddy/Caddyfile
```

把文件中的 `ops.example.com` 替换为实际域名，保留：

```caddyfile
ops.company.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "no-referrer"
    }
}
```

验证并启动 Caddy：

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
```

在 Cloud Helm Server 尚未启动时，域名可能暂时返回 `502`，这是反向代理找不到后端的正常现象。如果使用 Nginx 或其他网关，也必须转发原始 `Host`、客户端 IP，并保证公网只能通过 HTTPS 进入。

如果 Caddy 自身也运行在容器中，不能使用 `127.0.0.1:8080`，因为该地址指向 Caddy 容器自己。创建只供反向代理使用的外部网络：

```bash
docker network create cloudhelm-proxy
```

通过 Compose override 将 Cloud Helm `server` 和 Caddy 服务都加入 `cloudhelm-proxy`，并给 Server 设置网络别名 `cloudhelm-server`；Caddy 应改为：

```caddyfile
reverse_proxy cloudhelm-server:8080
```

Server 的宿主机端口仍保持 `127.0.0.1:8080`，不要为了让 Caddy 容器连接而改成公网监听 `0.0.0.0:8080`。临时执行 `docker network connect` 只适合排障；将网络写入两个 Compose 文件，才能在容器重新创建后自动恢复。

### 7. 拉取并启动管理中心

发布标签会生成 `amd64`、`arm64` 两种架构的 Server 镜像。Docker 会自动拉取与当前设备匹配的镜像：

```bash
docker compose config
docker compose pull
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=100 server
```

如果需要审计或修改源码后本机构建，可以改为执行 `docker compose build --pull`，再去掉 `up` 的 `--no-build`。正常情况下 `docker compose ps` 中 Server 最终应显示为 `healthy`。

Compose 会先运行一次性 `data-init` 服务，自动把宿主机数据目录设置为 Server 所需的 `10001:10001` 和 `0700`，随后才启动 Server。执行 `docker compose ps -a data-init` 时看到其状态为 `Exited (0)` 是正常现象。该初始化服务不读取 `.env`、不连接网络、不挂载 Docker Socket；不要删除 `depends_on` 或改用 `chmod 777`。

分别检查本机后端和公网 HTTPS：

```bash
curl --fail http://127.0.0.1:8080/healthz
curl --fail https://ops.company.com/healthz
curl -I https://ops.company.com/
```

健康检查预期返回：

```json
{"status":"ok"}
```

确认后端没有监听所有网卡：

```bash
docker compose port server 8080
```

输出应以 `127.0.0.1:` 开头。从另一台机器访问 `http://管理中心IP:8080` 应失败。

SQLite 数据保存在发布目录的 `./cloudhelm-data/cloudhelm.db`。容器内路径仍为 `/data/cloudhelm.db`，目录由 `data-init` 自动交给容器用户 `10001:10001`。`docker compose down` 和 `docker compose down -v` 都不会删除这个宿主机目录；删除或覆盖 `cloudhelm-data` 才会丢失数据库。

所有 Compose 服务（Server、Agent 和 PostgreSQL）都使用 `json-file` 日志轮转：单文件最多 `50m`、保留 3 个文件，即每个容器最多约 `150m` Docker 日志。业务数据和数据库文件不计入该上限。

### 8. 完成首次企微登录

1. 从企业微信工作台打开云舵应用。
2. 浏览器会进入 `/api/v1/auth/wecom/start`，完成 OAuth 后回到云舵首页。
3. 系统使用 `.env` 中的管理员 `UserId` 完成首次识别。
4. 进入“我的 → 用户与权限”，添加第二位可信管理员，避免唯一管理员不可用时无法维护。
5. 为普通成员创建映射后，再配置其环境、节点、项目或容器范围。

可以从普通浏览器验证未认证 API 确实被拒绝：

```bash
curl -i https://ops.company.com/api/v1/nodes
```

预期状态为 `401 Unauthorized`。直接获得网址的人最多看到企微登录入口，无法读取节点和容器数据。未提前绑定的企微成员完成 OAuth 后也会收到拒绝提示。

### 9. 在 Docker 节点部署 Agent

不要把管理中心 `.env` 复制到节点。每台节点只需要发布包和自己的 `deploy/agent.env`。

在节点上校验并解压发布包，然后执行：

```bash
cd cloudhelm-0.5.0/deploy
cp agent.env.example agent.env
chmod 600 agent.env
install -d -m 0700 cloudhelm-data
```

编辑 `agent.env`：

```dotenv
TZ=Asia/Shanghai
CLOUDHELM_AGENT_SERVER_URL=https://ops.company.com
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=与管理中心一致的首次注册令牌
CLOUDHELM_AGENT_NAME=production-node-01
CLOUDHELM_AGENT_ENVIRONMENT=production
CLOUDHELM_AGENT_STATE_FILE=/data/agent-state.json
CLOUDHELM_AGENT_VERIFY_TLS=true
CLOUDHELM_AGENT_REPORT_SECONDS=15
CLOUDHELM_AGENT_POLL_SECONDS=3
CLOUDHELM_AGENT_REQUEST_TIMEOUT_SECONDS=20
CLOUDHELM_AGENT_MAX_CONTAINERS=500
CLOUDHELM_AGENT_GPU_MONITORING_ENABLED=true
CLOUDHELM_AGENT_NVIDIA_SMI_PATH=/usr/bin/nvidia-smi
CLOUDHELM_AGENT_GPU_QUERY_TIMEOUT_SECONDS=5
CLOUDHELM_AGENT_GPU_MAX_OUTPUT_BYTES=4194304
```

变量说明：

|变量|说明|默认值/生产建议|
|---|---|---|
|`TZ`|Agent 容器系统时区|默认 `Asia/Shanghai`|
|`CLOUDHELM_AGENT_SERVER_URL`|管理中心根地址|必填；使用 HTTPS，不带 `/api` 和结尾 `/`|
|`CLOUDHELM_AGENT_ENROLLMENT_TOKEN`|Agent 首次注册的共享令牌|首次启动必填；注册成功后从文件中删除|
|`CLOUDHELM_AGENT_NAME`|节点显示名称|默认宿主机名；每台机器应清晰且唯一|
|`CLOUDHELM_AGENT_ENVIRONMENT`|节点所属环境|代码默认 `default`；生产节点建议明确填写 `production`|
|`CLOUDHELM_AGENT_STATE_FILE`|节点独立凭据的容器内路径|默认 `/data/agent-state.json`，必须位于持久化挂载中|
|`CLOUDHELM_AGENT_VERIFY_TLS`|是否校验 Server 的 HTTPS 证书|默认 `true`，生产环境必须保持开启|
|`CLOUDHELM_AGENT_REPORT_SECONDS`|容器和 GPU 状态上报间隔|默认 15 秒，可设 5–300|
|`CLOUDHELM_AGENT_POLL_SECONDS`|任务轮询间隔|默认 3 秒，可设 1–60|
|`CLOUDHELM_AGENT_REQUEST_TIMEOUT_SECONDS`|访问 Server API 的单次超时|默认 20 秒，可设 3–120|
|`CLOUDHELM_AGENT_MAX_CONTAINERS`|单节点一次最多采集的容器数量|默认 500，可设 1–2000|
|`CLOUDHELM_AGENT_GPU_MONITORING_ENABLED`|是否探测 NVIDIA GPU|默认 `true`；非 GPU 节点可设为 `false`|
|`CLOUDHELM_AGENT_NVIDIA_SMI_PATH`|Agent 容器内 `nvidia-smi` 的固定路径|默认 `/usr/bin/nvidia-smi`|
|`CLOUDHELM_AGENT_GPU_QUERY_TIMEOUT_SECONDS`|单次 GPU 查询超时|默认 5 秒，可设 1–30|
|`CLOUDHELM_AGENT_GPU_MAX_OUTPUT_BYTES`|单次 `nvidia-smi` 输出读取上限|默认 4194304 字节（4 MiB），可设 65536–16777216|

先确认节点能访问管理中心，再启动：

```bash
curl --fail https://ops.company.com/healthz
docker compose -f agent.compose.yml config
docker compose -f agent.compose.yml pull
docker compose -f agent.compose.yml up -d --no-build
docker compose -f agent.compose.yml ps
docker compose -f agent.compose.yml logs --tail=100 agent
```

首次成功日志应包含节点已注册以及容器数量已上报。回到云舵页面，节点应在一个上报周期内显示为在线。

Agent 启动前也会运行一次性 `data-init`，自动将其状态目录设置为仅容器 root 可读写的 `0700`。执行 `docker compose -f agent.compose.yml ps -a data-init` 看到 `Exited (0)` 是正常现象；该初始化服务不连接网络，也不会挂载 Docker Socket。

#### NVIDIA GPU 节点启动方式

普通节点继续只使用 `agent.compose.yml`。GPU overlay 仅支持 `amd64`、`arm64`，不要求 `arm/v7` 支持 NVIDIA GPU。

先确保 NVIDIA Container Toolkit 已注册为 Docker runtime。仅安装工具包但没有执行 runtime 配置时，容器可能看得到设备声明，却没有注入 `nvidia-smi`：

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker info --format '{{json .Runtimes}}' | grep nvidia
docker run --rm --runtime=nvidia --gpus all ubuntu:24.04 nvidia-smi -L
```

最后一条命令必须成功列出 GPU，再启动 Agent。NVIDIA 节点必须把 GPU overlay 一起传给每条 Compose 命令：

```bash
docker compose -f agent.compose.yml -f agent.gpu.compose.yml config
docker compose -f agent.compose.yml -f agent.gpu.compose.yml pull
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d --no-build
docker compose -f agent.compose.yml -f agent.gpu.compose.yml exec agent test -x /usr/bin/nvidia-smi
docker compose -f agent.compose.yml -f agent.gpu.compose.yml exec agent nvidia-smi -L
docker compose -f agent.compose.yml -f agent.gpu.compose.yml logs --tail=100 agent
```

0.5.0 的 overlay 显式使用 `runtime: nvidia`，为 Agent 保留全部 NVIDIA GPU，并只启用 `NVIDIA_DRIVER_CAPABILITIES=utility`。NVIDIA runtime 会把与宿主机驱动版本匹配的 `/usr/bin/nvidia-smi` 和 NVML 库只读注入容器；镜像不会内置一个可能与宿主机驱动不兼容的固定版本。上述检查应能列出显卡，并在日志中看到 `Reported N NVIDIA GPUs`。若容器启动时报 `unknown or invalid runtime name: nvidia`，说明尚未执行 `nvidia-ctk runtime configure`；若 `config` 阶段报 GPU device reservation 错误，应检查 Docker Compose v2 和 Toolkit 安装。

注册成功后，NVIDIA 节点重新创建容器时也必须继续带两个 `-f` 参数：

```bash
docker compose -f agent.compose.yml -f agent.gpu.compose.yml \
  up -d --force-recreate
```

参考：[NVIDIA Container Toolkit 安装与架构](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/)、[NVIDIA 容器环境变量](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html)、[Docker Compose GPU 支持](https://docs.docker.com/compose/how-tos/gpu-support/)。

注册成功后，独立节点凭据保存在当前目录的 `./cloudhelm-data/agent-state.json`。删除 `agent.env` 中的 `CLOUDHELM_AGENT_ENROLLMENT_TOKEN` 行，然后重新创建容器：

```bash
docker compose -f agent.compose.yml up -d --force-recreate
docker compose -f agent.compose.yml logs --tail=50 agent
```

不要删除或复制到其他节点使用 `cloudhelm-data` 目录；否则节点会丢失独立身份或复用错误身份。其余节点重复相同步骤，但应使用不同的 `CLOUDHELM_AGENT_NAME` 和正确的环境名称。

### 10. 部署验收清单

- `https://实际域名/healthz` 返回 `200`；
- 浏览器显示的证书域名正确且证书链有效；
- 管理中心 `8080`、数据库 `5432` 和 Docker API 没有公网开放；
- 未登录访问 `/api/v1/nodes` 返回 `401`；
- 未绑定企微成员不能进入；
- 新建普通用户在未授权时看不到任何节点；
- 只读用户不能执行容器启停；
- Agent 页面状态正常，日志中没有持续认证或 TLS 错误；
- NVIDIA 节点的 Agent 容器内 `nvidia-smi` 正常，页面显示的型号、显存和数量与宿主机一致；
- 执行一次测试容器日志读取，并确认审计页面有记录；
- 已创建第二位管理员并完成首次数据库备份。

### 11. 常见部署故障

|现象|优先检查|
|---|---|
|Server 启动即退出|`.env` 是否仍有 `REPLACE_ME`、域名是否为 HTTPS、企微字段是否完整|
|Caddy 返回 502|Server 是否 `healthy`，`127.0.0.1:8080/healthz` 是否可访问|
|证书申请失败|DNS 是否指向本机，80/443 是否放行，AAAA 是否错误指向其他主机|
|企微提示重定向域名错误|可信域名、应用主页和 `CLOUDHELM_PUBLIC_BASE_URL` 是否完全一致|
|登录后提示未授权|填写的是不是准确 `UserId`，是否误用了姓名或手机号|
|回调后反复要求登录|是否全程 HTTPS，代理是否改写 Host，浏览器是否禁止 Cookie|
|Agent 注册返回 401|管理中心和 Agent 的注册令牌是否一致，是否多了空格|
|Agent 出现 TLS 错误|证书链和域名是否正确；不要通过关闭 TLS 校验绕过|
|节点注册但没有容器|Docker Socket 是否正确挂载，Agent 是否有读取 socket 的权限|
|节点持续离线|节点到管理中心 443 是否可达，系统时间是否准确，查看 Agent 日志|
|宿主机可运行 `nvidia-smi`，Agent 容器却没有该命令|是否同时使用 `agent.gpu.compose.yml`；执行 `nvidia-ctk runtime configure --runtime=docker` 后是否重启 Docker；`docker info` 是否列出 `nvidia` runtime|
|容器内可运行 `nvidia-smi`，页面却提示 GPU 不可用|`CLOUDHELM_AGENT_NVIDIA_SMI_PATH` 是否为 `/usr/bin/nvidia-smi`，查看 Agent 日志中的 XML 解析、超时或输出上限错误|
|页面有 GPU，但某容器显示未分配|Docker inspect 的 `HostConfig.DeviceRequests` 是否包含 NVIDIA GPU；旧式 runtime 则检查 `NVIDIA_VISIBLE_DEVICES`|

排查命令：

```bash
docker compose ps
docker compose logs --tail=200 server
sudo journalctl -u caddy -n 200 --no-pager
docker compose -f deploy/agent.compose.yml logs --tail=200 agent
```

不要把包含企微 Secret、完整 Cookie、Agent Token 或 OAuth `code` 的日志直接发送给他人。

## NVIDIA GPU 监控

GPU 节点每个状态上报周期执行一次固定命令 `/usr/bin/nvidia-smi -q -x`，不拼接用户输入，也不经过 shell；查询默认 5 秒超时并限制 XML 输出大小。Server 只保存经过字段白名单解析后的指标，不保存 `nvidia-smi` 原始输出。

当前页面可查看：

- 节点 GPU 数量、型号、UUID、驱动和 CUDA 版本；
- GPU/显存利用率、显存已用与总量、温度、风扇、实时功耗与功率上限；
- Docker 为每个容器配置的 GPU ID、数量请求或“全部 GPU”。

容器上的 GPU 信息来自 Docker 配置，表示“允许该容器访问哪些 GPU”。容器详情中的负载、显存、温度和功耗是被分配物理卡的整卡指标，不是该容器独占的利用率；若多容器共享同一张卡，指标包含这些容器及宿主机进程的合计活动。节点指标是上报时刻的快照，当前版本不保存历史曲线，也不采集进程级或逐容器 GPU 利用率。MIG 开启时，部分利用率字段可能由驱动返回 `N/A`，页面会显示 `—`；这是 NVIDIA 工具本身的数据限制。

主机级 GPU 指标可能反映同机其他工作负载的活动，因此权限做了单独隔离：管理员、全资源用户和具有环境/节点查看权限的人可以看到全部 GPU；只有项目或容器授权的人，只能看到其可见容器明确分配到的 GPU 及这些卡的当前指标，看不到同机其他 GPU。容器使用 `count:N` 但 Docker 未记录具体设备 ID 时，页面只能显示请求数量，不能把指标猜测性地归到某张卡。

字段语义参考 [NVIDIA `nvidia-smi` 官方文档](https://docs.nvidia.com/deploy/nvidia-smi/index.html)。

## 人员与容器权限

|角色|查看状态|查看日志|启停/重启|人员管理|
|---|---:|---:|---:|---:|
|管理员 `admin`|是|是|是|是|
|运维 `operator`|按授权|按授权|按授权|否|
|只读 `viewer`|按授权|按授权|否|否|

添加用户时填写其准确的企业微信 `UserId`。新建普通用户默认使用“自定义资源范围”且规则为空，即登录后看不到任何节点。

管理员点击“配置范围”，可以按环境、节点、Compose 项目或单个容器授予“仅查看”“查看 + 日志”或“查看 + 日志 + 运维”。服务端会对列表、容器详情、任务和审计接口重复执行授权校验，不能通过直接输入容器 ID 绕过。

管理员还可以：

- 点击“下线”立即撤销某人的全部云舵会话；
- 点击“停用”阻止后续访问并撤销现有会话；
- 通过 `PATCH /api/v1/users/{id}` 调整显示名、角色或企微 `UserId`，身份或角色变化会自动撤销该用户现有会话。

## 企业自用小程序

仓库的 `miniprogram/` 是原生企业微信小程序前端，面向单一企业内部使用，不需要注册企业微信第三方服务商。它复用现有 Server、Agent、用户表、容器权限和审计数据，包含总览、节点、NVIDIA GPU、容器详情、日志、启停/重启、审计以及管理员的用户授权页面。

登录流程为：

```text
企业微信小程序 wx.qy.login()
  │ 五分钟一次性 code
  ▼
POST /api/v1/auth/wecom-mini/login
  │ Server 使用当前企业的小程序 Secret 调用 jscode2session
  │ 校验返回 CorpId，并匹配已绑定 UserId
  ▼
短期随机 Bearer 会话 → 现有资源权限和审计 API
```

小程序不会获得企业 Secret、Agent 令牌、数据库凭据或 Docker Socket。返回给小程序的是随机、不透明、绝对过期的短期会话，不是携带权限信息的 JWT；Server 只保存令牌摘要，管理员执行“下线”或“停用”后会立即失效。企业微信的 `session_key` 也不会返回给小程序或保存到数据库。

### 1. 注册并关联小程序

1. 在微信公众平台注册组织主体的小程序，取得小程序 AppID。
2. 使用微信开发者工具导入仓库中的 `miniprogram/` 目录，将 `project.config.json` 的 `touristappid` 换成实际 AppID。
3. 在企业微信管理后台进入“应用管理 → 小程序 → 关联小程序”，按页面指引关联并设置成员可见范围。
4. 从关联后的小程序详情取得**当前企业对应的 Secret**。它与 H5 自建应用 Secret、微信小程序 AppSecret 是不同的凭据，以企业微信后台实际展示为准。
5. 小程序正式发布前，在微信小程序后台完成版本审核、备案和服务器域名配置。

企业微信支持关联微信小程序，并使用 `wx.qy.login()` 获取临时 code；Server 再通过企业微信 `jscode2session` 换取员工 `UserId`。参考[企业微信小程序开发前须知](https://developers.weixin.qq.com/miniprogram/dev/dev_wxwork/dev-doc/qywx-api.html)和[小程序登录接口](https://developers.weixin.qq.com/miniprogram/dev/dev_wxwork/dev-doc/qywx-api/login/wx.qy.login.html)。

### 2. 配置 Server

在管理中心 `.env` 增加：

```dotenv
CLOUDHELM_WECOM_MINIPROGRAM_SECRET=关联小程序在当前企业下的Secret
```

不要把该 Secret 写入 `miniprogram/config.js`、提交到 Git 或发送给 Agent。更新后只需重新创建 Server：

```bash
docker compose up -d --no-build --force-recreate server
docker compose logs --tail=100 server
```

未配置该变量时，H5 登录和 Agent 不受影响，小程序登录会明确返回 `503`。

### 3. 配置小程序 API 地址

编辑 `miniprogram/config.js`：

```javascript
module.exports = {
  baseUrl: "https://ops.company.com"
}
```

地址必须与 `CLOUDHELM_PUBLIC_BASE_URL` 一致，只包含 HTTPS 协议和域名，不要附加 `/api` 或结尾 `/`。该文件只含公开地址，不应放任何 Secret。

在微信小程序后台把 `https://ops.company.com` 配置为 `request` 合法域名。开发者工具可以临时关闭域名校验用于本地联调，但体验版和正式版必须使用有效 HTTPS 域名，不能依赖这个开发选项绕过平台检查。

### 4. 联调与发布

1. 在微信开发者工具中选择企业微信运行环境或企业模拟，确认 `wx.qy.login()` 能返回 code。
2. 先上传体验版，并把测试成员同时加入小程序体验范围、企业微信小程序可见范围和云舵“用户与权限”。
3. 验证未绑定成员被拒绝，普通成员只看见已授权容器，只读成员不能启停容器。
4. 验证日志、停止和重启操作均出现在审计页面。
5. 完成小程序隐私说明、备案和版本审核后，再扩大企业微信可见范围。

小程序与 H5 可以同时保留：移动端使用小程序，桌面端和应急入口继续使用 H5。两者共用会话数量上限；默认单用户最多 5 个有效会话、每个会话 60 分钟。

## 实际使用中的安全注意事项

### 1. 离职与账号异常不会自动实时推送

企微 OAuth 在登录时确认成员身份。人员离职、企微账号冻结或手机丢失后，已经签发的云舵会话最长仍可能存活到配置的会话期限。发现异常时应立即在云舵中“停用 + 下线”；建议保持默认 60 分钟，不要为了方便设置成全天或数天。

### 2. Docker Socket 是最高风险点

挂载 `/var/run/docker.sock` 等同于给 Agent 很高的宿主机权限。Docker 官方也提醒 Docker daemon 控制权可能影响整台主机。务必做到：

- 永远不要开放未加密的 Docker TCP `2375`；
- Agent 主机只运行可信镜像和本发布包代码；
- 不给 Agent 增加任意命令、创建特权容器或任意目录挂载能力；
- 保持 Linux、Docker Engine 和容器运行时安全更新；
- 生产与测试节点分开授权。

参考：[Docker Engine 安全说明](https://docs.docker.com/engine/security/)与[保护 Docker daemon socket](https://docs.docker.com/engine/security/protect-access/)。

### 3. 容器日志可能包含秘密

应用日志常包含令牌、连接串、手机号或业务数据。只给必要人员开放日志权限，并在业务容器侧避免打印密码、Cookie、Authorization Header 和完整请求体。云舵审计记录不保存日志正文，但浏览器中已经显示的内容仍可能被截图或复制。

### 4. 保护企微 Secret、Agent 令牌和数据库备份

- `.env` 权限保持 `600`，只允许部署账号读取；
- 不把 `.env`、`cloudhelm-data`、Agent 状态目录或数据库加入 Git、镜像和发布包；
- 备份应加密并限制下载权限；
- Secret 疑似泄露时，在企微后台重置应用 Secret 并重启服务；
- Agent 注册令牌泄露时立即轮换，核对节点列表中是否出现未知节点；
- 定期检查审计日志中的登录拒绝、用户授权和容器操作。

### 5. 反向代理与访问日志

保持 HTTPS、HSTS 和证书自动续期。云舵容器默认关闭 Uvicorn access log，避免 OAuth 一次性 `code` 和 `state` 出现在请求日志中。如果在 Caddy、Nginx、CDN 或负载均衡器启用访问日志，应对查询参数进行脱敏，并严格限制日志读取权限。

服务器时间必须通过 NTP 保持准确，否则 OAuth 状态、会话过期和审计时间会出现偏差。

### 6. 客户端仍然需要基本防护

企微认证解决“是谁”，但不会证明手机一定安全。管理员和运维人员应开启设备锁屏、企微账号保护和系统更新；手机丢失、Root/Jailbreak、恶意软件或共享已解锁设备仍可能导致当前用户权限被利用。当前版本未强制设备证书，也未限制只能从某一台物理设备访问。

### 7. 最小权限和操作复核

- 日常查看使用只读角色；
- 只有确实需要启停服务的人使用运维角色；
- 管理员账号数量保持少量；
- 生产容器停止和重启前确认影响窗口；
- 定期复核企微应用可见范围、云舵人员列表和容器授权；
- 至少每月验证一次备份可恢复，而不只是确认“备份文件存在”。

## PostgreSQL 部署

如果预计长期运行、多管理员频繁操作或希望使用现有数据库备份体系，可以在首次启动前选择 PostgreSQL。当前版本按全新安装设计，请在第一次启动前确定使用 SQLite 还是 PostgreSQL，不提供两者之间的在线迁移命令。

配置 PostgreSQL 密码：

```bash
cp deploy/postgres.env.example deploy/postgres.env
chmod 600 deploy/postgres.env
openssl rand -hex 32
```

把随机值写入：

```dotenv
TZ=Asia/Shanghai
POSTGRES_PASSWORD=替换为随机值
```

变量说明：

|变量|说明|生产建议|
|---|---|---|
|`TZ`|PostgreSQL 容器系统时区|默认 `Asia/Shanghai`|
|`POSTGRES_PASSWORD`|`cloudhelm` 数据库用户密码，同时用于 Server 数据库连接|使用 `openssl rand -hex 32` 生成，只保存在 `deploy/postgres.env`|

创建 PostgreSQL 的宿主机数据目录：

```bash
install -d -m 0700 deploy/cloudhelm-postgres-data
```

确认根目录 `.env` 中的企微和域名配置已经完成，然后启动：

```bash
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml config
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml build --pull
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml up -d
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml ps
```

PostgreSQL 只加入内部 Docker 网络，不发布宿主机 `5432`，数据保存在 `deploy/cloudhelm-postgres-data`。Server 的 `cloudhelm-data` 仍由一次性 `data-init` 自动初始化；PostgreSQL 官方入口负责初始化自己的数据库目录。`deploy/postgres.env` 已加入 `.gitignore`，仍需保持 `600` 权限，并和根目录 `.env` 一起纳入服务器 Secret 管理和加密备份。

查看日志：

```bash
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml logs --tail=100 server database
```

不要同时运行默认 `docker-compose.yml` 和 `deploy/postgres.compose.yml`，两者使用相同项目名和服务名，应二选一。

## 备份

备份包含企微用户映射、权限、审计和会话摘要，必须按敏感数据保护。备份文件应加密、限制访问并复制到另一台受控存储设备。

SQLite 默认数据位于发布目录的 `cloudhelm-data`。为保证文件一致性，先短暂停止 Server，再备份目录：

```bash
docker compose stop server
sudo tar -czf - -C cloudhelm-data . > cloudhelm-data-backup.tar.gz
chmod 600 cloudhelm-data-backup.tar.gz
docker compose start server
curl --fail https://ops.company.com/healthz
```

使用 PostgreSQL 时执行逻辑备份：

```bash
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml exec -T database \
  pg_dump -U cloudhelm -d cloudhelm --format=custom \
  > cloudhelm-postgres.dump
```

检查文件非空并限制权限：

```bash
test -s cloudhelm-postgres.dump
chmod 600 cloudhelm-postgres.dump
```

至少每月在隔离环境做一次恢复演练。不要等到故障发生时才第一次验证恢复流程，也不要在未验证备份前删除宿主机数据目录。

## 开发、验证和发布

```bash
uv sync --extra test --extra server --extra agent --extra postgres
uv run ruff check .
uv run pytest
bash scripts/package-release.sh 0.5.0
```

发布包不包含 `.env`、数据库、Agent 状态或任何部署密钥。

推送与项目版本一致的标签会自动创建 GitHub Release，并发布两个 OCI 多架构镜像：

```bash
git tag v0.5.0
git push origin v0.5.0
```

- `ghcr.io/xbl916/cloud-helm-server:0.5.0`
- `ghcr.io/xbl916/cloud-helm-agent:0.5.0`

Server 镜像包含 `linux/amd64`、`linux/arm64`；Agent 镜像包含 `linux/amd64`、`linux/arm64`、`linux/arm/v7`。两者都额外发布 `v0.5.0` 和 `latest` 标签、SBOM 与构建来源证明。GitHub Actions 使用 `packages: write` 的仓库临时令牌，不需要保存长期 GHCR 密钥；第三方 Actions 均固定到完整提交 SHA。自动发布方式参考 [GitHub 容器镜像发布文档](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)和 [Docker 多平台构建文档](https://docs.docker.com/build/ci/github-actions/multi-platform/)。
