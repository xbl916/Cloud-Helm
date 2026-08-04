# 云舵 Cloud Helm

云舵是面向企业微信 H5 的多节点 Docker 运维控制台。管理中心集中处理企微身份、资源权限和审计；每台 Docker 主机运行主动出站连接的 Agent，因此节点不需要公网 IP，也不需要暴露 Docker API。

当前版本为 `0.3.0`，按全新部署设计，不包含旧版账号密码登录或旧数据库升级逻辑。

快速导航：

- [安全模型](#安全模型)
- [详细部署手册](#详细部署手册)
- [人员与容器权限](#人员与容器权限)
- [实际使用中的安全注意事项](#实际使用中的安全注意事项)
- [PostgreSQL 部署](#postgresql-部署)
- [备份](#备份)

## 安全模型

```text
企业微信成员
  │ HTTPS + OAuth（确认人员身份）
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
- 浏览器仅保存 `HttpOnly + Secure + SameSite` 随机会话 Cookie，不使用 `localStorage` JWT。
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
sha256sum -c cloudhelm-0.3.0.tar.gz.sha256
tar -xzf cloudhelm-0.3.0.tar.gz
cd cloudhelm-0.3.0
```

预期输出包含：

```text
cloudhelm-0.3.0.tar.gz: OK
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
openssl rand -hex 32
```

把随机结果写入 `CLOUDHELM_AGENT_ENROLLMENT_TOKEN`，再编辑 `.env`：

```dotenv
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=替换为openssl生成的随机值
CLOUDHELM_PUBLIC_BASE_URL=https://ops.company.com
CLOUDHELM_WECOM_CORP_ID=wwxxxxxxxxxxxxxxxx
CLOUDHELM_WECOM_AGENT_ID=1000002
CLOUDHELM_WECOM_SECRET=应用的Secret
CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID=首位管理员的企微UserId
CLOUDHELM_BOOTSTRAP_ADMIN_DISPLAY_NAME=系统管理员
CLOUDHELM_DATABASE_URL=sqlite:////data/cloudhelm.db
CLOUDHELM_ENVIRONMENT=production
CLOUDHELM_SESSION_MINUTES=60
CLOUDHELM_MAX_SESSIONS_PER_USER=5
CLOUDHELM_OAUTH_STATE_SECONDS=300
CLOUDHELM_NODE_OFFLINE_SECONDS=60
CLOUDHELM_TRUST_PROXY_HEADERS=true
CLOUDHELM_BIND_ADDRESS=127.0.0.1
CLOUDHELM_PORT=8080
```

变量说明：

|变量|说明|生产建议|
|---|---|---|
|`CLOUDHELM_AGENT_ENROLLMENT_TOKEN`|Agent 首次注册的共享令牌|至少 32 字节随机值|
|`CLOUDHELM_PUBLIC_BASE_URL`|用户实际访问的公网根地址|只包含 `https://域名`，不要带路径和结尾 `/`|
|`CLOUDHELM_WECOM_CORP_ID`|企业 ID|从企微管理后台复制|
|`CLOUDHELM_WECOM_AGENT_ID`|自建应用 AgentId|从应用详情复制|
|`CLOUDHELM_WECOM_SECRET`|自建应用 Secret|仅服务器保存|
|`CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID`|首次创建的管理员身份|填写准确企微 `UserId`|
|`CLOUDHELM_SESSION_MINUTES`|网页会话绝对有效期|保持 60 分钟或更短|
|`CLOUDHELM_MAX_SESSIONS_PER_USER`|单用户最大并发会话|默认 5|
|`CLOUDHELM_DATABASE_URL`|服务端数据库|单机初始部署保持 SQLite 默认值|
|`CLOUDHELM_TRUST_PROXY_HEADERS`|读取代理转发的真实 IP|仅在后端只允许可信代理访问时设为 `true`|
|`CLOUDHELM_BIND_ADDRESS`|宿主机监听地址|保持 `127.0.0.1`|
|`CLOUDHELM_PORT`|宿主机后端端口|默认 8080，不对公网开放|

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

### 7. 构建并启动管理中心

先检查 Compose 展开结果，再构建镜像：

```bash
docker compose config
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 server
```

首次构建会下载 Python 基础镜像和依赖，需要几分钟。正常情况下 `docker compose ps` 中 Server 最终应显示为 `healthy`。

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

SQLite 数据保存在 Docker volume `cloudhelm-data`，执行普通 `docker compose down` 不会删除数据。不要执行 `docker compose down -v`，否则会删除数据库 volume。

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
cd cloudhelm-0.3.0/deploy
cp agent.env.example agent.env
chmod 600 agent.env
```

编辑 `agent.env`：

```dotenv
CLOUDHELM_AGENT_SERVER_URL=https://ops.company.com
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=与管理中心一致的首次注册令牌
CLOUDHELM_AGENT_NAME=production-node-01
CLOUDHELM_AGENT_ENVIRONMENT=production
CLOUDHELM_AGENT_VERIFY_TLS=true
CLOUDHELM_AGENT_REPORT_SECONDS=15
CLOUDHELM_AGENT_POLL_SECONDS=3
```

变量说明：

|变量|说明|
|---|---|
|`CLOUDHELM_AGENT_SERVER_URL`|管理中心根地址，不带 `/api` 和结尾 `/`|
|`CLOUDHELM_AGENT_ENROLLMENT_TOKEN`|仅首次注册使用|
|`CLOUDHELM_AGENT_NAME`|节点显示名称，每台机器应清晰且唯一|
|`CLOUDHELM_AGENT_ENVIRONMENT`|环境名称，例如 `production`、`testing`|
|`CLOUDHELM_AGENT_VERIFY_TLS`|生产环境必须为 `true`|
|`CLOUDHELM_AGENT_REPORT_SECONDS`|容器状态上报间隔|
|`CLOUDHELM_AGENT_POLL_SECONDS`|任务轮询间隔|

先确认节点能访问管理中心，再启动：

```bash
curl --fail https://ops.company.com/healthz
docker compose -f agent.compose.yml config
docker compose -f agent.compose.yml build --pull
docker compose -f agent.compose.yml up -d
docker compose -f agent.compose.yml ps
docker compose -f agent.compose.yml logs --tail=100 agent
```

首次成功日志应包含节点已注册以及容器数量已上报。回到云舵页面，节点应在一个上报周期内显示为在线。

注册成功后，独立节点凭据保存在 `cloudhelm-agent-state` volume。删除 `agent.env` 中的 `CLOUDHELM_AGENT_ENROLLMENT_TOKEN` 行，然后重新创建容器：

```bash
docker compose -f agent.compose.yml up -d --force-recreate
docker compose -f agent.compose.yml logs --tail=50 agent
```

不要删除 `cloudhelm-agent-state` volume；否则节点会丢失独立身份并请求重新注册。其余节点重复相同步骤，但应使用不同的 `CLOUDHELM_AGENT_NAME` 和正确的环境名称。

### 10. 部署验收清单

- `https://实际域名/healthz` 返回 `200`；
- 浏览器显示的证书域名正确且证书链有效；
- 管理中心 `8080`、数据库 `5432` 和 Docker API 没有公网开放；
- 未登录访问 `/api/v1/nodes` 返回 `401`；
- 未绑定企微成员不能进入；
- 新建普通用户在未授权时看不到任何节点；
- 只读用户不能执行容器启停；
- Agent 页面状态正常，日志中没有持续认证或 TLS 错误；
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

排查命令：

```bash
docker compose ps
docker compose logs --tail=200 server
sudo journalctl -u caddy -n 200 --no-pager
docker compose -f deploy/agent.compose.yml logs --tail=200 agent
```

不要把包含企微 Secret、完整 Cookie、Agent Token 或 OAuth `code` 的日志直接发送给他人。

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
- 不把 `.env`、Agent 状态 volume 或数据库加入 Git、镜像和发布包；
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
POSTGRES_PASSWORD=替换为随机值
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

PostgreSQL 只加入内部 Docker 网络，不发布宿主机 `5432`。`deploy/postgres.env` 已加入 `.gitignore`，仍需保持 `600` 权限，并和根目录 `.env` 一起纳入服务器 Secret 管理和加密备份。

查看日志：

```bash
docker compose --env-file deploy/postgres.env \
  -f deploy/postgres.compose.yml logs --tail=100 server database
```

不要同时运行默认 `docker-compose.yml` 和 `deploy/postgres.compose.yml`，两者使用相同项目名和服务名，应二选一。

## 备份

备份包含企微用户映射、权限、审计和会话摘要，必须按敏感数据保护。备份文件应加密、限制访问并复制到另一台受控存储设备。

SQLite 默认数据位于 `cloudhelm-data` volume。为保证文件一致性，先短暂停止 Server，再备份 volume：

```bash
docker compose stop server
docker run --rm -v cloudhelm_cloudhelm-data:/data -v "$PWD":/backup alpine \
  tar -czf /backup/cloudhelm-data-backup.tar.gz -C /data .
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

至少每月在隔离环境做一次恢复演练。不要等到故障发生时才第一次验证恢复流程，也不要在未验证备份前删除 Docker volume。

## 开发、验证和发布

```bash
uv sync --extra test --extra agent --extra postgres
uv run ruff check .
uv run pytest
bash scripts/package-release.sh 0.3.0
```

发布包不包含 `.env`、数据库、Agent 状态或任何部署密钥；目标设备会为自身 CPU 架构构建 Server 与 Agent 镜像。
