# Cloud Helm 1.0.1 PostgreSQL 正式部署与阿里云弹性验收操作手册

本文依据仓库当前 `1.0.1` 的配置、Compose、数据库模型和清理逻辑编写，适用于**首次安装、空数据库**。主方案为阿里云 ECS + Docker PostgreSQL 17 + Cloud Helm Server，业务节点各安装一个 Agent。本次正式部署还包括从零准备 ACS 弹性环境、真实 GPU 业务验收及故障恢复。

本文中的 `ops.company.com`、Bucket、地域、IP、账号和路径示例均需按实际环境替换。容量数字是规划估算，不是本项目的压测结果或节点承载承诺。外部配置参考官方资料，核对日期为 2026-10-16。

**已有业务节点不停止业务容器、不释放 GPU。先准备配置和镜像，再启动管理组件；业务版本切换另按发布流程执行。** 已有 SQLite 数据需要迁移时，不要直接换数据库地址；另见 [版本部署与升级手册](deploy-and-upgrade-1.0.1.md)。

## 目录

1. [架构与阿里云资源](#1-架构与阿里云资源)
2. [节点规模、保留时间与数据库容量](#2-节点规模保留时间与数据库容量)
3. [全新部署 PostgreSQL 与 Server](#3-全新部署-postgresql-与-server)
4. [域名、HTTPS 与企业微信](#4-域名https-与企业微信)
5. [安装 Agent 与节点配置](#5-安装-agent-与节点配置)
6. [阿里云 ACS 从零部署与真实弹性验收](#6-阿里云-acs-从零部署与真实弹性验收)
7. [数据库巡检与参数变更](#7-数据库巡检与参数变更)
8. [数据备份与 OSS 配置](#8-数据备份与-oss-配置)
9. [恢复演练与故障恢复](#9-恢复演练与故障恢复)
10. [验收与常见问题](#10-验收与常见问题)

## 1. 架构与阿里云资源

```text
企微 H5 / PC / 可选小程序 ── HTTPS 443 ── ECS 上的 Caddy
                                               │ 127.0.0.1:8080
业务节点 Agent ─────── 出站 HTTPS 443 ─────── Cloud Helm Server
                                               │ Docker backend 网络
                                               └── PostgreSQL 17:5432
                                                    │ 持久化云盘
ECS 备份任务 ── HTTPS ── 私有 OSS Bucket               └── 数据 + WAL

可选：Server ── 私网 HTTPS ── ACS Kubernetes API
      本地/ACS 业务实例 ── HTTPS 上报 ── Server
      业务网关 ── 可达私网 ── 本地业务端口 / ACS Pod IP
```

### 1.1 购买与准备清单

|资源|配置与用途|
|---|---|
|ECS|64 位 Linux；本文以 Ubuntu 24.04 LTS 为例，管理中心不需要 GPU。小规模试运行可从 2 vCPU / 4 GiB 起步；50–100 节点规划从 4 vCPU / 8 GiB 起步，最终以真实心跳、轮询和查询压测为准。|
|系统盘|建议至少 40 GiB，存放系统和 Docker 镜像；监控 `/var/lib/docker`，镜像空间不计入下文数据库估算。|
|数据盘|独立 ESSD 云盘，按第 2 节预留。通过文件系统 UUID 持久挂载后再部署；数据库不放容器可写层或对象存储挂载目录。|
|VPC / vSwitch|ECS、可选 ACS、可选 RDS 尽量同地域、私网互通；本地网段和云上网段避免重叠。|
|EIP / 公网出口|域名指向稳定入口；Server 需访问企微 API、镜像仓库等。记录企微 API 请求的实际出口 IP，NAT 场景可能不是入口 EIP。|
|域名 / DNS|如 `ops.company.com`，A 记录指向 EIP；仅在完整支持 IPv6 时配置 AAAA。|
|OSS|私有备份 Bucket，同地域便于上传；重要系统再增加异地或独立账号副本。配置见第 8 节。|
|云监控|监控 ECS CPU/内存、云盘容量与 I/O、备份失败、证书到期；另需主机或数据库采集任务监测 PostgreSQL。|

以上规格是起步方案，不能由节点数单独确定：每节点容器数、轮询频率和用户操作量都会影响负载。300 节点以上先做压测，再决定分离数据库或使用托管数据库。当前 Server 会在进程内启动告警和伸缩后台循环，本文只运行 **1 个 Server 进程/副本**；不要直接增加 Uvicorn workers 或复制多个 Server 来实现高可用。

中国内地 ECS 对外提供网站服务前，按实际主体和接入方式完成备案；要求以 [阿里云 ICP 备案说明](https://www.alibabacloud.com/help/zh/icp-filing/basic-icp-service/product-overview/icp-filing-requirements-for-a-regular-website)为准。

### 1.2 安全组、主机防火墙和路由

|方向/目标|端口|来源或目的地|
|---|---|---|
|ECS 入站 SSH|TCP 22|仅运维固定 IP 或堡垒机；保留可用管理通道。|
|ECS 入站 HTTPS|TCP 443|用户、Agent、业务上报和企微回调所需来源；公网服务按接入需求开放。|
|ECS 入站 HTTP|TCP 80|采用 Caddy HTTP 证书验证及跳转时开放；其他证书方案按验证方式配置。|
|Server 端口|TCP 8080|只绑定宿主机 `127.0.0.1`，不创建公网入站规则。|
|PostgreSQL|TCP 5432|主方案仅 Docker 内网，不映射宿主机端口，不创建公网规则。|
|Docker API|TCP 2375/2376|无需开放；Agent 使用本地 Unix socket。|
|ECS/节点出站|HTTPS 443、DNS、时间同步|实际企微 API、镜像仓库、OSS、证书颁发服务及配置的 DNS/NTP。|
|可选 ACS API|实际端点端口，常见 6443|仅 Server 所在私网或指定出口，按 kubeconfig 中地址配置。|

安全组与主机防火墙需要同时检查；不要保留默认 SSH 全网开放规则。Agent 主动连接 Server，节点无需公网入站端口。规则语义见 [阿里云安全组规则](https://www.alibabacloud.com/help/zh/ecs/user-guide/security-group-rules)。

控制台操作：进入“ECS → 实例 → 管理主机 → 安全组”，打开实际绑定安全组的“访问规则 → 入方向 → 手动添加”。分别按上表添加 22、80、443，填写准确来源 CIDR；不要用新增 8080/5432 规则排查故障。启用了严格出站规则时，在“出方向”按目标网络依赖补充规则。修改后保留当前 SSH 连接，另开终端确认仍可登录。主机用 `ss -lntp` 查看监听，使用 `ufw status` 或现有防火墙工具核对规则；不要清空已有规则集。

### 1.3 执行位置和配置值来源

先准备三个终端，避免把管理中心命令执行到业务主机：

|终端|登录位置|用途|
|---|---|---|
|M 管理主机|管理中心 ECS，使用 root 或相应 sudo 权限|Server、PostgreSQL、Caddy、备份、ACS 凭据挂载|
|K 集群运维机|受保护的 Linux 运维机，可与 M 为同一台|保存管理员 kubeconfig，创建 ACS 资源|
|A 节点主机|每台受管 Docker/GPU 业务主机|仅安装/配置对应节点的 Agent|

文中 `nano 文件路径` 表示打开文件编辑，保存为 `Ctrl+O`、回车，退出为 `Ctrl+X`。`REPLACE_ME`、`实际…`、中文占位值必须替换；每节写明命令执行位置及通过条件。先完成第 1、2 节的记录和规划，再按第 3–6 节部署，最后完成第 8、9 节的备份恢复与第 10 节验收。

|配置值|在哪里获取|写入哪里|
|---|---|---|
|管理域名|自有域名 DNS 管理，例如 `ops.company.com`|`.env`、Caddyfile、Agent 地址、业务配置|
|ECS EIP/出口 IP|ECS/EIP 控制台；经 NAT 出站时核对 SNAT EIP|DNS A 记录、企微可信 IP、ACS API 访问控制|
|CorpID|企微管理后台“我的企业 → 企业信息”|`.env` 的 `CLOUDHELM_WECOM_CORP_ID`|
|AgentId / 应用 Secret|企微“应用管理 → 自建应用 → 云舵”详情；查看 Secret 按后台身份验证获取|`.env` 的 `CLOUDHELM_WECOM_AGENT_ID`、`CLOUDHELM_WECOM_SECRET`|
|管理员 UserId|企微“通讯录 → 对应成员 → 账号/UserId”，以后台字段为准|`.env` 的 `CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID`|
|Agent 注册令牌|M 终端执行 `openssl rand -hex 32`|Server `.env` 与节点 `agent.env`，两侧相同|
|数据库密码|M 终端另外执行 `openssl rand -hex 32`|`deploy/postgres.env`，独立于注册令牌|
|节点名称|自己命名，如 `prod-gpu-001`，每台唯一|各节点 `agent.env`|
|ACS 地域、集群、VPC、交换机、安全组|阿里云 ACS 集群详情、VPC 和安全组页面|部署记录、Deployment 网络注解|
|ACS API / CA|K 终端下载对应集群 kubeconfig，再按第 6.3 节提取|`/etc/cloudhelm/acs/api-url`、`ca.crt`|
|ACS Bearer Token|K 终端为专用 ServiceAccount 签发，第 6.4 节|`/etc/cloudhelm/acs/token`，不是阿里云 AccessKey|
|GPU 型号与 CPU/内存组合|ACS 所选地域的 GPU 规格列表与资源配置页面|Deployment 的 GPU 标签及资源 requests/limits|
|ACR Registry、镜像及 Tag|ACR“镜像仓库 → 仓库详情 → 拉取指令”，区分公网/VPC 地址|Deployment `image`|
|Registry 用户名/密码|ACR“访问凭证”，取 Registry 登录凭据，非阿里云控制台密码|Kubernetes `imagePullSecret`|
|业务 service_id / report_token|云舵管理页面创建逻辑 AI 服务后显示|业务环境中的服务 ID；令牌写业务 Secret 文件|
|网关发现令牌|M 终端再独立生成随机值|Server `.env` 与测试客户端/实际网关凭据文件|
|发现允许 CIDR|VPC/vSwitch 和本地业务网络实际网段；须覆盖实际 Pod IP|`.env` 的 `CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS`|
|模型目录、业务端口、探针路径|业务镜像负责人提供；通过镜像说明和实际请求验证|Deployment 参数、探针、模型挂载|
|NAS 挂载域名|NAS“文件系统 → 挂载点 → 挂载地址”|可选 NAS PV 的 `server`|
|备份 Bucket、地域、Endpoint|OSS Bucket 概览和访问端口页面|第 8 节 ossutil 配置与备份脚本|

**五种凭据不可混用：**Agent 注册令牌用于加入节点；每节点独立凭据位于 `agent-state.json`；业务 `report_token` 仅用于对应服务上报；网关发现令牌只读获取路由；ACS Token 用于 Kubernetes API。数据库密码和企微 Secret 也不发送给 Agent 或浏览器。

### 1.4 文件清单与最终目录

```text
M 管理主机
/srv/cloudhelm/cloudhelm-1.0.1/
  .env                               Server 设置及企微凭据
  deploy/postgres.env                PostgreSQL 初始密码
  deploy/postgres.compose.yml        仓库原有 PostgreSQL 主部署文件
  deploy/acs.override.yml            第 6 节新建的只读凭据挂载
  deploy/cloudhelm-postgres-data/    PostgreSQL 数据及 WAL
  cloudhelm-data/                    Server 数据目录
/etc/caddy/Caddyfile                 HTTPS 反向代理
/etc/cloudhelm/acs/{api-url,ca.crt,token}  ACS API 连接材料
/usr/local/sbin/ch                   第 3.4 节创建的统一 Compose 命令
/usr/local/sbin/cloudhelm-backup     第 8 节备份脚本
/var/backups/cloudhelm/              本地数据库备份

K 集群运维机
/root/cloudhelm-acs/
  admin.kubeconfig                   管理员下载的 kubeconfig
  rbac.yaml                         最小权限 SA/Role/RoleBinding
  cloudhelm-sa.kubeconfig            验证专用 SA 的客户端配置
  token, ca.crt, api-url             签发/提取的连接材料
  deployment.yaml                   ACS 业务工作负载
  nas.yaml                          可选模型 PV/PVC
  load-generator.py                 仅用于验收的本地负载上报器
  check-routes.py                    服务发现检查脚本
  test.env                           测试平台地址和服务 UUID，不含令牌
  test-control.json                 测试负载与健康状态
  report-token                      该测试服务的上报令牌
  gateway-token                     发现接口测试凭据

A 每台业务节点
/opt/cloudhelm/cloudhelm-1.0.1/deploy/
  agent.env                         该节点名称、平台地址与注册令牌
  agent.compose.yml                 基础 Agent 配置
  agent.gpu.compose.yml             GPU 节点追加配置
  cloudhelm-data/agent-state.json    该节点独立身份，不复制给其他节点
```

## 2. 节点规模、保留时间与数据库容量

### 2.1 当前版本实际配置

**没有 `NODE_COUNT` 或 `MAX_NODES` 之类的节点数环境变量。** 每台 Docker 主机运行一个 Agent，成功注册后成为一个节点；节点数是部署和容量规划输入。ACS Pod 属于业务实例，不会自动变成 Agent 节点。

|变量|配置位置|默认值|当前允许范围/含义|
|---|---|---|---|
|`CLOUDHELM_AGENT_MAX_CONTAINERS`|各节点 `agent.env`|500|1–2000，每节点最多上报容器数，不是节点上限；应覆盖实际容器数。|
|`CLOUDHELM_AGENT_REPORT_SECONDS`|各节点 `agent.env`|15|5–300 秒，心跳/实时指标上报周期。|
|`CLOUDHELM_AGENT_POLL_SECONDS`|各节点 `agent.env`|3|1–60 秒，任务轮询周期。|
|`CLOUDHELM_NODE_OFFLINE_SECONDS`|Server `.env`|60|15–3600 秒；应给心跳抖动留余量。|
|`CLOUDHELM_METRICS_HISTORY_ENABLED`|Server `.env`|true|是否记录节点/容器历史；关闭不会主动清空旧数据。|
|`CLOUDHELM_METRICS_HISTORY_INTERVAL_SECONDS`|Server `.env`|300|60–3600 秒，历史采样最小间隔。|
|`CLOUDHELM_METRICS_HISTORY_RETENTION_HOURS`|Server `.env`|168|1–8760 小时，即默认 7 天、最长 365 天。|
|`CLOUDHELM_METRICS_HISTORY_MAX_ROWS`|Server `.env`|200000|非负整数，无配置最大值；`0` 关闭行数限制，正整数为全平台历史行数上限，由后台清理。|
|`CLOUDHELM_ALERT_EVENT_RETENTION_HOURS`|Server `.env`|2160|24–17520 小时，默认 90 天。|
|`CLOUDHELM_ALERT_EVENT_MAX_ROWS`|Server `.env`|10000|100–1000000，告警事件全局行数上限。|
|`CLOUDHELM_AUTOSCALING_INTERVAL_SECONDS`|Server `.env`|15|5–300 秒，业务聚合/伸缩评估周期。|
|`CLOUDHELM_AUTOSCALING_HISTORY_RETENTION_HOURS`|Server `.env`|24|1–8760 小时，业务聚合历史的保留时间。|

源码依据：[Server 配置](../cloudhelm/config.py)、[Agent 配置](../cloudhelm_agent/config.py)、[历史写入及清理](../cloudhelm/metrics.py)、[数据库模型](../cloudhelm/models.py)。

每次符合历史采样间隔的心跳，写入 **1 条节点记录 + 本次容器清单中每个容器 1 条记录**。GPU 当前状态存在节点数据中，并非每张卡单独增加一条 `metric_samples`；表中也没有完整的逐卡 GPU 历史字段。容器数量包括实际被 Agent 上报的基础设施容器，不仅是 AI 业务容器。

时间保留始终生效；`MAX_ROWS` 为正整数时还会按行数保留最新数据，`0` 则只按时间清理。独立后台任务在启动后及每 5 分钟清理，即使全部节点离线或关闭采样也会运行。每批最多 1000 行、单独提交，每轮最多 20 批，积压时暂停 1 秒后继续；边界允许在清理前短暂超出。采样是随心跳执行的最小间隔，实际周期可能因请求耗时、心跳不整除或掉线而更长；下面按理想稳定周期计算并留余量。

前端通常展示最近 24 小时。历史 API 的 `hours` 最大 8760，但 `limit` 默认 1000、最大 2500，返回所选窗口内最新的若干点，无完整翻页或自动降采样功能。5 分钟采样下，30 天有 8640 个点，无法一次取全；长期报表需另行实现数据库只读导出/分析流程。参见 [历史 API](../cloudhelm/routers/api.py)。

### 2.2 计算公式

```text
N = 在线节点数
C = 平均每节点上报容器数（规划用峰值更稳妥）
I = 历史采样间隔，秒
D = 保留天数
T = N × (1 + C)；异构节点用 T = Σ(1 + Ci)
每天新增行数 = T × 86400 / I
目标历史行数 R = T × 86400 / I × D
建议 MAX_ROWS = 0（只按时间保留），或 MAX_ROWS ≥ ceil(R × 1.2)
行数约束下可保留天数 ≈ MAX_ROWS × I / (T × 86400)
```

例：100 个节点、每节点 10 个容器、5 分钟采样、30 天，需要 9,504,000 行，建议设 12,000,000 行。若保持默认 200,000 行，只能存约 **0.63 天（15.2 小时）**，即使保留小时数填写 720 也无效。

### 2.3 不同规模的配置组合

下表假设每条节点/容器历史连同现有索引占 **0.5–1 KiB**，用于首次预算；这是对模型的估计，必须上线后按第 7 节实测修正。GiB 按 1024³ 字节计算。数据盘起步值预留了数据库余量，但**不含多份本地备份与业务模型文件**。

|节点 N|容器/节点 C|间隔 I|保留 D|目标行数 R|建议 MAX_ROWS|历史表+索引估算|数据盘起步参考|
|---:|---:|---:|---:|---:|---:|---:|---:|
|10|5|300 秒|7 天|120,960|200,000|0.06–0.12 GiB|30 GiB|
|50|10|300 秒|30 天|4,752,000|6,000,000|2.27–4.53 GiB|50 GiB|
|100|10|300 秒|30 天|9,504,000|12,000,000|4.53–9.06 GiB|80 GiB|
|100|10|900 秒|90 天|9,504,000|12,000,000|4.53–9.06 GiB|80 GiB|
|300|20|1800 秒|30 天|9,072,000|11,000,000|4.33–8.65 GiB|100 GiB|
|1000|20|3600 秒|30 天|15,120,000|19,000,000|7.21–14.42 GiB|150 GiB，须先压测|

例如 300 节点、每节点 20 容器、5 分钟采样、30 天会产生 54,432,000 行；可以设 `MAX_ROWS=0`，或配置超过该数量并留余量的行数上限。取消配置上限不等于吞吐量已经验证；需要实测磁盘、写入、历史查询与备份性能，也可降低采样频率。

节点规模还影响控制面请求量：默认 15 秒心跳、3 秒轮询时，100 节点约有 `100/15 + 100/3 = 40` 次请求/秒，1000 节点约 400 次/秒，尚未计入用户操作、业务上报及每个心跳的容器数据量。历史清理已移出心跳路径，按时间索引和主键分批删除，不再使用大 OFFSET。`MAX_ROWS=0` 时不统计全表；启用行数保护时每轮在过期清理完成后统计一次。PostgreSQL 连接级 advisory lock 保证多 worker 清理互斥。大表性能仍需实测；这些表格仅证明容量配置可表达，不证明服务吞吐足够。

### 2.4 数据库总量与其他数据

```text
B_metric = R × 实测每条历史记录字节数（含索引）
R_business ≈ 业务服务数 S × 86400 / 评估周期 × 业务历史天数
数据库存活数据 ≈ B_metric + 业务聚合历史 + 节点/容器现状 + 账号权限 + 任务/审计/事件
磁盘预算 ≈ 存活数据 × 2～3 + WAL 峰值 + 恢复临时空间 + 本地备份空间
```

2～3 倍为维护与更新膨胀的规划余量，不是 PostgreSQL 固定开销；WAL 应以实际更新量监测，`pg_database_size` 不包含集群 `pg_wal`。即使每 5 分钟才写一次历史，15 秒心跳仍更新现状并产生 WAL，不能只用历史插入行数推算 WAL。

业务聚合记录是**按逻辑服务**采样，不是每个 Pod 一行。10 项服务、15 秒评估、保留 1 天约 57,600 行；首次可另按 0.3–0.8 KiB/行估算约 17–45 MiB，再实测。全局自动伸缩关闭时，服务评估与业务历史仍会运行，不应把这部分预算视为零。告警事件按触发/恢复写入，另受告警保留时间与行数控制。

任务结果单条默认允许 256 KiB，任务、审计、伸缩事件和发布记录等没有统一的可配置 TTL。要单独观察这些表的增长并制定归档规则，不能以“历史只保留 30 天”推断整个数据库永不增长。当前企微事件去重记录另有 30 天清理逻辑。容器日志正文、模型权重、业务数据库和音视频不属于监控历史容量预算。

## 3. 全新部署 PostgreSQL 与 Server

### 3.1 主机基础环境

**执行位置：新建的管理中心 ECS。** 以下系统安装命令按 Ubuntu 24.04 LTS、root 账号编写。先通过 SSH 登录，执行 `sudo -i`，后续保持此终端。已有业务节点只执行第 5 节，不在其上重装 Docker 或 GPU 驱动。

```bash
cat /etc/os-release
uname -m
apt-get update
apt-get install -y ca-certificates curl gnupg openssl tar python3 util-linux cron jq unzip nano
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker cron
timedatectl set-timezone Asia/Shanghai
timedatectl set-ntp true
docker version
docker compose version
docker compose up --help
```

确认 Compose 有 `--wait` 和多次 `--env-file` 支持。仓库配置见 [Docker 官方 Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)。如新主机预装了其他 Docker 包，先核对来源和冲突再按官方说明处理；有运行中业务的主机不执行卸载命令。

安装宿主机 Caddy：

```bash
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  > /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
chmod o+r /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy
caddy version
```

以上仅执行一次；已安装 Caddy 时先检查现有配置。来源：[Caddy 安装说明](https://caddyserver.com/docs/install)。

**数据盘准备：**在 ECS 控制台“云盘”中创建并挂载数据盘，按磁盘 ID、容量和 `lsblk` 的序列号核对实际设备。已经有文件系统的盘跳过格式化，直接查询 UUID 并挂载。

只有确认是**本次新建、空白、未分区、未挂载的独立数据盘**时，才执行以下格式化步骤。它会写入文件系统并破坏该设备原有数据，不能选择系统盘或已有业务盘。命令不预设 `/dev/vdb`，必须填写核实后的设备路径：

```bash
lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS,SERIAL
read -rp '输入本次新建空白数据盘的完整设备路径：' CLOUDHELM_DATA_DISK
lsblk -f "$CLOUDHELM_DATA_DISK"
wipefs --no-act "$CLOUDHELM_DATA_DISK"
```

确认只有一个空白设备、没有分区/文件系统签名/挂载点，并再次对照控制台磁盘 ID 后执行。使用整盘 ext4 文件系统，不再另外创建分区：

```bash
read -rp "格式化将清除设备内容；确认时输入完整设备路径 ${CLOUDHELM_DATA_DISK}：" CLOUDHELM_DISK_CONFIRM
if [[ -b "$CLOUDHELM_DATA_DISK" && "$CLOUDHELM_DISK_CONFIRM" == "$CLOUDHELM_DATA_DISK" ]]; then
  mkfs.ext4 -L cloudhelm-data "$CLOUDHELM_DATA_DISK"
else
  echo '设备无效或确认不匹配，未执行格式化'
fi
```

完成后按以下方式持久挂载：

```bash
lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS,SERIAL
blkid
install -d -m 0750 /srv/cloudhelm
nano /etc/fstab
```

在 `/etc/fstab` 添加一行，`实际UUID` 和 `ext4` 分别替换为上一步查到的 UUID 与实际文件系统类型：

```fstab
UUID=实际UUID /srv/cloudhelm ext4 defaults 0 2
```

不添加 `nofail`，使挂载失败可以被发现。继续执行：

```bash
mount /srv/cloudhelm
findmnt --target /srv/cloudhelm
df -h /srv/cloudhelm
```

确认显示预期数据盘及容量，不是系统根分区。若计划使用系统盘部署，必须明确记录该选择和剩余容量，跳过独立盘挂载步骤；其故障和容量与系统共享。

独立盘方案下，将 Docker 服务设置为依赖该挂载点，防止开机时误用空目录：

```bash
install -d /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/cloudhelm-data.conf <<'EOF'
[Unit]
RequiresMountsFor=/srv/cloudhelm
EOF
systemctl daemon-reload
```

这些设置在后续 Docker 启动时生效。本次无需为设置挂载依赖重启已有业务主机上的 Docker。

### 3.2 获取当前发布包

**执行位置：M。** 以下复制模板和初始化命令仅在本次全新、空部署目录执行一次；不要用模板覆盖已经投入使用的 `.env` 或密码文件。

```bash
cd /srv/cloudhelm
curl -fLO https://github.com/xbl916/Cloud-Helm/releases/download/v1.0.1/cloudhelm-1.0.1.tar.gz
curl -fLO https://github.com/xbl916/Cloud-Helm/releases/download/v1.0.1/cloudhelm-1.0.1.tar.gz.sha256
sha256sum -c cloudhelm-1.0.1.tar.gz.sha256
tar -xzf cloudhelm-1.0.1.tar.gz
cd /srv/cloudhelm/cloudhelm-1.0.1
umask 077
cp .env.example .env
cp deploy/postgres.env.example deploy/postgres.env
chmod 600 .env deploy/postgres.env
install -d -m 0700 cloudhelm-data deploy/cloudhelm-postgres-data
openssl rand -hex 32
openssl rand -hex 32
```

两次输出分别用于独立的 Agent 注册令牌和数据库密码。发布资源如返回 404，先确认该版本发布流程已完成；也可使用可信的当前版本源码并执行下文 `build server`，不替换为未知来源镜像。国内访问 GHCR/Docker Hub 困难时，可在可信环境预拉取并转存自己的阿里云 ACR，记录原始摘要并改 Compose 镜像地址。

### 3.3 配置数据库密码和环境变量

执行 `nano /srv/cloudhelm/cloudhelm-1.0.1/deploy/postgres.env`，编辑数据库文件：

```dotenv
TZ=Asia/Shanghai
POSTGRES_PASSWORD=填写第二次生成的64位十六进制随机值
```

本项目 Compose 会把密码拼入数据库 URL；采用 `openssl rand -hex 32` 可避免 `@`、`:`、`/`、`$` 等字符涉及 URL 编码或 Compose 插值。不要原样使用中文占位值。

执行 `nano /srv/cloudhelm/cloudhelm-1.0.1/.env`，替换所有必填示例项，并按下面的 **100 节点、每节点 10 容器、保留 30 天** 示例设置：

```dotenv
TZ=Asia/Shanghai
CLOUDHELM_DISPLAY_TIMEZONE=Asia/Shanghai
CLOUDHELM_ENVIRONMENT=production
CLOUDHELM_PUBLIC_BASE_URL=https://ops.company.com
CLOUDHELM_WECOM_CORP_ID=填写企业CorpID
CLOUDHELM_WECOM_AGENT_ID=填写自建应用AgentId
CLOUDHELM_WECOM_SECRET=填写自建应用Secret
CLOUDHELM_BOOTSTRAP_ADMIN_WECOM_USERID=填写首位管理员准确UserId
CLOUDHELM_BOOTSTRAP_ADMIN_DISPLAY_NAME=系统管理员
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=填写第一次生成的64位十六进制随机值
CLOUDHELM_BIND_ADDRESS=127.0.0.1
CLOUDHELM_PORT=8080
CLOUDHELM_TRUST_PROXY_HEADERS=true
CLOUDHELM_NODE_OFFLINE_SECONDS=60
CLOUDHELM_METRICS_HISTORY_ENABLED=true
CLOUDHELM_METRICS_HISTORY_INTERVAL_SECONDS=300
CLOUDHELM_METRICS_HISTORY_RETENTION_HOURS=720
CLOUDHELM_METRICS_HISTORY_MAX_ROWS=12000000
CLOUDHELM_ALERTS_ENABLED=true
CLOUDHELM_ALERT_EVENT_RETENTION_HOURS=2160
CLOUDHELM_ALERT_EVENT_MAX_ROWS=10000
CLOUDHELM_ALERT_NOTIFICATIONS_ENABLED=false
CLOUDHELM_AUTOSCALING_ENABLED=false
CLOUDHELM_AUTOSCALING_INTERVAL_SECONDS=15
CLOUDHELM_AUTOSCALING_HISTORY_RETENTION_HOURS=24
```

**删除 `.env` 中示例的 `CLOUDHELM_DATABASE_URL=sqlite:...` 行。** PostgreSQL 专用 Compose 的 `server.environment` 已设置 `postgresql+psycopg://cloudhelm:${POSTGRES_PASSWORD}@database:5432/cloudhelm`，其优先级高于 `.env`。本流程始终使用 PostgreSQL 专用 Compose，不运行根目录的默认 `docker compose up`。

Compose 服务的 `env_file: ../.env` 用于注入 Server 环境；命令行 `--env-file` 用于 Compose 自身的 `${...}` 替换，两者用途不同。下文同时指定两个 `--env-file`，使 `.env` 中的自定义绑定端口也参与插值；后一个 `deploy/postgres.env` 提供数据库密码。不要在 shell 里导出旧的同名变量覆盖文件。

开始启动前，核对 `.env` 的域名、企微值、管理员 UserId 和注册令牌均为真实值，数据库密码文件已替换，SQLite 行已删除。文件中每个变量只保留一条；以后修改配置应编辑现有项，不用 `echo` 反复追加。`CLOUDHELM_TRUST_PROXY_HEADERS=true` 依赖这里的回环绑定与可信 Caddy 转发，不能同时把 8080 暴露给任意来源。

### 3.4 统一 Compose 命令与启动

**执行位置：M 管理主机。** 创建统一命令，避免后续误用默认 SQLite Compose；ACS 挂载文件存在时自动追加。此命令固定本次部署目录，以后升级时同步维护该路径。

```bash
cat > /usr/local/sbin/ch <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /srv/cloudhelm/cloudhelm-1.0.1
args=(docker compose --env-file .env --env-file deploy/postgres.env
      -f deploy/postgres.compose.yml)
if [[ -f deploy/acs.override.yml ]]; then
  args+=(-f deploy/acs.override.yml)
fi
exec "${args[@]}" "$@"
EOF
chmod 0750 /usr/local/sbin/ch
ch config --quiet
ch pull
ch up -d --no-build --wait
ch ps
ch logs --tail=100 server database
curl --fail http://127.0.0.1:8080/healthz
```

如明确采用源码构建，在启动前用 `ch pull database` 和 `ch build server` 替代 `ch pull`。不要并行启动根目录的 SQLite Compose：两份 Compose 默认项目名都是 `cloudhelm`。

数据库数据实际位于 **`/srv/cloudhelm/cloudhelm-1.0.1/deploy/cloudhelm-postgres-data`**；Server `/data` 位于根目录 `cloudhelm-data`。数据库路径相对 `deploy/postgres.compose.yml`，不是当前终端目录。为以后升级保留这个路径，不能在新版本空目录随意启动同项目。

当前镜像为 `postgres:17-alpine`，数据卷挂到 `/var/lib/postgresql/data`。正式投产记录拉取后的摘要，可据此固定镜像；小版本升级需先备份验证，不能把 tag 直接改成其他 PostgreSQL 大版本。模板没有设置 PostgreSQL 参数调优；先使用默认配置、开启默认 autovacuum，再按巡检数据调整，不把 `max_connections` 当节点数使用。模板初始化的 `cloudhelm` 用户具有数据库超级用户权限；因此尤其要保持数据库内网隔离，后续需要最小权限时再分离管理账号与应用迁移账号并验证迁移权限。

`POSTGRES_*` 只在空数据目录首次初始化时创建账号/密码；修改 `postgres.env` 不会自动修改既有数据库用户密码。需要轮换时，交互执行 `ch exec database psql -U cloudhelm -d cloudhelm`，在 psql 中使用 `\password cloudhelm`，然后同步文件并只重建 Server；安排短暂管理面维护窗口。

### 3.5 验证确实使用 PostgreSQL

```bash
ch exec -T server python - <<'PY'
from sqlalchemy import text
from cloudhelm.db import engine
assert engine.dialect.name == 'postgresql', engine.dialect.name
with engine.connect() as conn:
    print(conn.execute(text('SELECT current_database(), version()')).one())
    print(conn.execute(text('SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1')).scalar_one())
PY
```

应看到数据库 `cloudhelm`、PostgreSQL 17 和 `0016_wecom_event_receipts`。当前 `/healthz` 只返回进程存活状态，不执行数据库查询；不能单独用它证明数据库正常。

## 4. 域名、HTTPS 与企业微信

1. 在阿里云 DNS 配置 A 记录，确认解析到 ECS EIP；检查安全组及 80/443 可达。
2. 按第 3.1 节安装宿主机 Caddy，在 M 执行 `nano /etc/caddy/Caddyfile`，保存下例，域名替换为实际值。
3. 企微后台创建自建应用，设置可见范围、可信域名并完成校验，记录 CorpID、AgentId、Secret 和首位管理员 UserId。

```caddyfile
ops.company.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080
}
```

```bash
getent hosts ops.company.com
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
curl --fail https://ops.company.com/healthz
```

Caddy 必须能写自己的证书目录并访问证书服务，具体行为见 [Caddy 配置入门](https://caddyserver.com/docs/quick-starts/caddyfile)。若企业要求使用阿里云签发证书，配置实际 `tls 证书路径 私钥路径`，给 Caddy 服务用户只读权限，并纳入到期提醒和续签流程。

企微配置对应关系：

|项目|值|
|---|---|
|应用主页|`https://ops.company.com/api/v1/auth/wecom/start`|
|H5 / PC 扫码回调|`https://ops.company.com/api/v1/auth/wecom/callback`|
|应用可信域名|`ops.company.com`，与 `.env` 的域名一致|
|企业可信 IP（后台要求时）|Server 请求企微 API 的实际出口 IP|
|首位管理员|企微通讯录中的准确 UserId，不是昵称或手机号|

若域名校验要求提供 `WW_verify_*.txt`，在 M 执行 `install -d -m 0755 /var/www/cloudhelm-verify`，将**企微后台下载的真实文件**上传到该目录，对该文件执行 `chmod 0644`。在上述站点中将单一 `reverse_proxy` 改为下列路由，再 validate/reload；用 `curl --fail https://实际域名/WW_verify_实际文件名.txt` 核对正文与后台文件一致：

```caddyfile
handle /WW_verify_*.txt {
    root * /var/www/cloudhelm-verify
    file_server
}
handle {
    reverse_proxy 127.0.0.1:8080
}
```

`.env` 或可信域名调整后，在 M 执行 `ch up -d --no-build --no-deps --wait server`。HTTPS 验证成功后测试企微内 H5 登录和 PC 扫码；首次登录后创建其他平台账号并分配资源权限。可选小程序配置 `CLOUDHELM_WECOM_MINIPROGRAM_SECRET`，按 [README 小程序说明](../README.md#企业自用小程序)设置合法域名，不把 Secret 写入前端。

可选企微通讯录变更回调：在企微应用的接收消息/通讯录变更相关配置中启用所需成员事件，接收地址填写 `https://ops.company.com/api/v1/auth/wecom/events`。在后台生成并保存 Token、43 字符 EncodingAESKey，把同一对值分别填入 `.env` 的 `CLOUDHELM_WECOM_CALLBACK_TOKEN`、`CLOUDHELM_WECOM_CALLBACK_AES_KEY`。两项同时配置后，在 M 执行 `ch config --quiet` 与 `ch up -d --no-build --no-deps --wait server`，再回企微后台提交 URL 验证。用专用测试成员验证事件送达、平台账号停用及会话失效。具体支持事件见 [安全接入说明](security-hardening.md)；只开通登录不会自动启用成员同步。

## 5. 安装 Agent 与节点配置

**执行位置：A，每台已有 Docker 的业务主机。** 无需安装 PostgreSQL。不得为了安装 Agent 重装或重启现有业务主机的 Docker/GPU 驱动。以下初始化只在新的 Agent 目录执行，已有 `agent.env` 和状态文件时先备份并按升级流程合并。

```bash
sudo -i
docker version
docker compose version
install -d -m 0750 /opt/cloudhelm
cd /opt/cloudhelm
curl -fLO https://github.com/xbl916/Cloud-Helm/releases/download/v1.0.1/cloudhelm-1.0.1.tar.gz
curl -fLO https://github.com/xbl916/Cloud-Helm/releases/download/v1.0.1/cloudhelm-1.0.1.tar.gz.sha256
sha256sum -c cloudhelm-1.0.1.tar.gz.sha256
tar -xzf cloudhelm-1.0.1.tar.gz
cd /opt/cloudhelm/cloudhelm-1.0.1/deploy
cp agent.env.example agent.env
chmod 600 agent.env
```

执行 `nano /opt/cloudhelm/cloudhelm-1.0.1/deploy/agent.env`，修改以下项。注册令牌从 M 的 `.env` 安全取得，名称逐台修改：

```dotenv
CLOUDHELM_AGENT_SERVER_URL=https://ops.company.com
CLOUDHELM_AGENT_ENROLLMENT_TOKEN=与Server一致的注册令牌
CLOUDHELM_AGENT_NAME=prod-gpu-001
CLOUDHELM_AGENT_ENVIRONMENT=production
CLOUDHELM_AGENT_VERIFY_TLS=true
CLOUDHELM_AGENT_REPORT_SECONDS=15
CLOUDHELM_AGENT_POLL_SECONDS=3
CLOUDHELM_AGENT_MAX_CONTAINERS=500
CLOUDHELM_AGENT_DISK_QUERY_SECONDS=300
CLOUDHELM_AGENT_STATE_FILE=/data/agent-state.json
```

每台节点使用唯一易识别名称，其他主机采集路径保留示例默认值。普通节点：

```bash
docker compose -f agent.compose.yml config --quiet
docker compose -f agent.compose.yml pull agent
docker compose -f agent.compose.yml up -d --no-build agent
docker compose -f agent.compose.yml logs --tail=100 agent
```

已有 NVIDIA 驱动与 Container Toolkit 的 GPU 节点改用以下命令。`docker info` 的 runtime 中应有 `nvidia`；没有时先交由节点负责人补齐 Toolkit 并安排变更，不在运行中的 GPU 主机直接重启 Docker：

```bash
nvidia-smi
docker info --format '{{json .Runtimes}}'
docker compose -f agent.compose.yml -f agent.gpu.compose.yml config --quiet
docker compose -f agent.compose.yml -f agent.gpu.compose.yml pull agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d --no-build agent
```

Agent 挂载 Docker socket，等同拥有很高的主机控制能力，只部署可信镜像。GPU overlay 用于读取 GPU 状态，不会给数据库或 Server 分配 GPU。确认平台节点在线、容器数量和 GPU 状态正确后，备份该节点的 `deploy/cloudhelm-data/agent-state.json`；其中包含独立节点凭据，禁止复制给其他节点。注册完成后可从 `agent.env` 移除共享注册令牌，只重建 Agent 并保留状态目录。

节点首次注册和配置完成后，在 A 创建该节点的受保护备份，含独立凭据：

```bash
cd /opt/cloudhelm/cloudhelm-1.0.1/deploy
umask 077
install -d -m 0700 /var/backups/cloudhelm-agent
tar -czf "/var/backups/cloudhelm-agent/agent-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" \
  agent.env agent.compose.yml agent.gpu.compose.yml cloudhelm-data/agent-state.json
```

将其复制到组织受保护的异机备份位置，按主机身份分类。不要从另一节点恢复这份状态文件。

节点扩容是重复安装 Agent，并同步重算全局历史行数上限。调整 `MAX_CONTAINERS` 不会增加节点数，也不应靠降低该值截断容器清单来节省容量。

## 6. 阿里云 ACS 从零部署与真实弹性验收

管理中心基础验收后，先完成第 8、9 节的首次备份和独立恢复，记录结果，再进行弹性验收。本节执行顺序为：创建集群 → 运维机连接 → 最小权限与凭据 → Server 接入 → 创建逻辑服务 → 部署业务 → 手工验证 → 自动扩缩容 → 真实业务验收。测试使用独立 Namespace `inference-test`、Deployment `vlm-burst-test`，副本上限为 1。不修改生产 GPU 容器。

操作导航：[创建集群](#61-创建-acs-和网络) → [kubectl](#62-安装-kubectl-并连接集群) → [API 与 CA](#63-提取-api-地址和-ca) → [权限与 Token](#64-创建最小权限与签发-token) → [挂载连接材料](#65-将-acs-连接材料挂载到-server) → [逻辑服务](#66-创建逻辑服务并取得业务凭据) → [业务协议](#67-准备实际业务镜像与排空逻辑) → [ACR](#68-准备-acr-拉取凭据) → [NAS](#69-可选-nas-模型存储) → [Deployment](#610-创建-gpu-deployment) → [手工验收](#611-手工验证真实-pod) → [测试负载](#612-准备路由检查与可调测试负载) → [扩容](#613-开启测试服务自动扩容) → [缩容](#614-验证低负载排空缩容) → [故障兜底](#615-验证本地故障兜底和恢复) → [真实业务](#616-从控制链路测试进入真实业务验收)。

### 6.1 创建 ACS 和网络

**执行位置：阿里云控制台，由有初始化权限的运维账号操作。** 控制台入口名称可能调整，可通过顶部搜索进入对应产品。

1. 打开“容器计算服务 ACS”，按页面要求开通服务与服务关联角色。首次验证先使用按量 GPU，不把容量预留作为必要前提。
2. 在“VPC → 专有网络”记录现有 VPC ID 和网段；没有时新建，与本地网络避免重叠。示例 `10.20.0.0/16` 仅为示例。
3. 在“交换机”创建或选择目标 GPU 可用区中的 vSwitch，例如网段 `10.20.10.0/24`；记录交换机 ID。GPU 的型号、资源组合及库存从 ACS 当前地域资源规格页面取得。
4. 在 ACS“集群 → 创建集群”选择地域、VPC、vSwitch、安全组；Service CIDR 选择与 VPC、本地及其他集群不重叠的网段，如 `172.21.0.1/20`。
5. 启用 CoreDNS，确认 NAS 方案所需 CSI 组件可用；日志与监控按实际需要开启。记录集群 ID、名称和 Kubernetes 版本。
6. 管理主机在同 VPC 或有 VPN/专线时，使用私网 API。只有确实无法访问私网时才配置 API 公网入口，并将访问控制限制到管理主机和运维机实际出口 IP。
7. ACS Pod 需要访问公网域名或公网镜像时，在同 VPC 创建/选择公网 NAT 网关、绑定 EIP，**为 Pod 使用的每个 vSwitch 配置 SNAT**；仅创建 EIP/NAT 不够。
8. 配置 Pod 安全组：出站允许 Cloud Helm `443`、DNS 以及镜像/模型依赖；入站仅允许测试客户端/业务网关网段访问业务端口，不直接公开 Pod。

VPC、vSwitch、Service CIDR 及 API 网络要求见 [ACS 集群创建说明](https://help.aliyun.com/zh/cs/user-guide/create-an-acs-cluster)。GPU 选择见 [ACS GPU 概述](https://help.aliyun.com/en/cs/user-guide/gpu/)，资源形态见 [ACS Pod 规格](https://help.aliyun.com/en/cs/user-guide/acs-pod-instance-overview)。

三条链路分别验证：M 到 API、ACS Pod 到 Server、测试客户端/实际网关到 Pod IP。`kubectl port-forward` 可以临时验证应用，但不能证明生产网关私网链路可达。

### 6.2 安装 kubectl 并连接集群

**执行位置：K 集群运维机。** 以下为 Linux amd64/arm64；K 可与 M 为同一台。已有兼容 kubectl 时跳过安装。

在 ACS 集群详情“连接信息”查看 Kubernetes 版本。从 [kubectl Linux 安装说明](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)选择对应版本，客户端与 API Server 主版本相同，次版本差不超过 1。示例先填写实际版本后下载：

```bash
sudo -i
install -d -m 0700 /root/cloudhelm-acs
cd /root/cloudhelm-acs
read -rp '输入匹配集群的 kubectl 完整版本，例如 v1.34.1：' KUBECTL_VERSION
case "$(uname -m)" in
  x86_64) KUBECTL_ARCH=amd64 ;;
  aarch64) KUBECTL_ARCH=arm64 ;;
  *) echo '需按官方文档选择平台'; exit 1 ;;
esac
curl -fLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl"
curl -fLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl.sha256"
python3 - <<'PY'
import hashlib
from pathlib import Path
assert hashlib.sha256(Path('kubectl').read_bytes()).hexdigest() == Path('kubectl.sha256').read_text().strip()
print('kubectl checksum OK')
PY
install -m 0755 kubectl /usr/local/bin/kubectl
```

在 ACS 集群详情“连接信息/凭证”下载有初始化权限的 kubeconfig，将其安全放到 K 的 `/root/cloudhelm-acs/admin.kubeconfig`。根据当前网络选择私网或公网端点；使用临时管理员凭据时留意有效期。

```bash
cd /root/cloudhelm-acs
chmod 600 admin.kubeconfig
export KUBECONFIG=/root/cloudhelm-acs/admin.kubeconfig
kubectl config current-context
kubectl version
kubectl cluster-info
kubectl get namespaces
kubectl create namespace inference-test --dry-run=client -o yaml | kubectl apply -f -
```

**通过条件：**连接的集群名正确，能读取 Namespace 并创建 `inference-test`。新终端重新执行 `export KUBECONFIG=...`。管理员 kubeconfig 只留在 K，不挂载到 Server。

### 6.3 提取 API 地址和 CA

**执行位置：K，当前目录 `/root/cloudhelm-acs`。** 使用当前集群凭据提取，不手抄相似集群的地址。导出的临时 JSON 含管理员凭据，处理后立即删除：

```bash
umask 077
kubectl config view --minify --raw --flatten -o json > cluster-private.json
python3 - <<'PY'
import base64, json
from pathlib import Path
try:
    data = json.loads(Path('cluster-private.json').read_text())
    cluster = data['clusters'][0]['cluster']
    assert cluster['server'].startswith('https://')
    assert not cluster.get('insecure-skip-tls-verify'), '需要正确的 CA，禁止跳过 TLS'
    Path('api-url').write_text(cluster['server'].rstrip('/') + '\n')
    Path('ca.crt').write_bytes(base64.b64decode(cluster['certificate-authority-data']))
    print('API:', cluster['server'])
finally:
    Path('cluster-private.json').unlink(missing_ok=True)
PY
openssl x509 -in ca.crt -noout -subject -dates
```

CA 来源是 kubeconfig 的 `certificate-authority-data`，不是从不可信网络下载一张证书后直接信任。如果集群使用系统信任链而 kubeconfig 不含该字段，按集群连接说明取得对应 CA 文件后放到 `ca.crt`，不要使用 `-k` 绕过校验。

### 6.4 创建最小权限与签发 Token

**执行位置：K。** 新建 `/root/cloudhelm-acs/rbac.yaml`：

```bash
nano /root/cloudhelm-acs/rbac.yaml
```

内容如下，固定只操作本次测试 Deployment：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cloudhelm-autoscaler
  namespace: inference-test
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: cloudhelm-autoscaler
  namespace: inference-test
rules:
  - apiGroups: ["apps"]
    resources: ["deployments/scale", "deployments"]
    resourceNames: ["vlm-burst-test"]
    verbs: ["get", "patch"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: cloudhelm-autoscaler
  namespace: inference-test
subjects:
  - kind: ServiceAccount
    name: cloudhelm-autoscaler
    namespace: inference-test
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: cloudhelm-autoscaler
```

```bash
kubectl apply -f rbac.yaml
umask 077
kubectl -n inference-test create token cloudhelm-autoscaler --duration=24h > token.new
test -s token.new
mv token.new token
python3 - <<'PY'
import base64, json
from datetime import datetime, timezone
from pathlib import Path
payload = Path('token').read_text().strip().split('.')[1]
data = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
print('Token 实际到期时间:', datetime.fromtimestamp(data['exp'], timezone.utc).isoformat())
PY
```

默认使用短期 Token，实际期限由 API Server 决定，不一定等于请求的 24 小时。到期前在 K 重复签发命令，再按第 6.5 节重新部署；24–48 小时观察期间也要续签。生产若使用短期 Token，必须把签发、部署、到期检查纳入现有定时密钥轮换系统。TokenRequest 用法见 [Kubernetes ServiceAccount 文档](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/)。

**无自动续签系统时的明确替代方案：**使用同样最小权限的 ServiceAccount Token Secret，代价是凭据长期有效、必须安排定期轮换。需要该方案时新建 `token-secret.yaml`，不要同时假定短期 Token 仍是当前凭据：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudhelm-autoscaler-token-v1
  namespace: inference-test
  annotations:
    kubernetes.io/service-account.name: cloudhelm-autoscaler
type: kubernetes.io/service-account-token
```

```bash
kubectl apply -f token-secret.yaml
# 等待控制器填充；若暂时为空，稍后重新读取，不使用空文件覆盖 Server。
umask 077
kubectl -n inference-test get secret cloudhelm-autoscaler-token-v1 \
  -o jsonpath='{.data.token}' | base64 -d > token.new
test -s token.new && mv token.new token
```

长期 Secret Token 的创建和风险见 [Kubernetes ServiceAccount 管理](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)。轮换时以新名称 `...-v2` 创建另一份 Secret，部署并验证新 Token 后，再删除旧 Secret；记录负责人和下次轮换日期。

为验证 SA 本身的权限，新建使用 `tokenFile` 的独立 kubeconfig；避免沿用管理员客户端证书影响测试：

```bash
python3 - <<'PY'
import base64, json
from pathlib import Path
root = Path('/root/cloudhelm-acs')
data = {
    'apiVersion': 'v1', 'kind': 'Config',
    'clusters': [{'name': 'acs', 'cluster': {
        'server': (root/'api-url').read_text().strip(),
        'certificate-authority-data': base64.b64encode((root/'ca.crt').read_bytes()).decode()}}],
    'users': [{'name': 'cloudhelm', 'user': {'tokenFile': str(root/'token')}}],
    'contexts': [{'name': 'cloudhelm', 'context': {
        'cluster': 'acs', 'user': 'cloudhelm', 'namespace': 'inference-test'}}],
    'current-context': 'cloudhelm',
}
(root/'cloudhelm-sa.kubeconfig').write_text(json.dumps(data))
(root/'cloudhelm-sa.kubeconfig').chmod(0o600)
PY
kubectl --kubeconfig=cloudhelm-sa.kubeconfig auth can-i get deployments/vlm-burst-test --subresource=scale
kubectl --kubeconfig=cloudhelm-sa.kubeconfig auth can-i patch deployments/vlm-burst-test --subresource=scale
kubectl --kubeconfig=cloudhelm-sa.kubeconfig auth can-i patch pods
kubectl --kubeconfig=cloudhelm-sa.kubeconfig auth can-i delete deployments/vlm-burst-test
```

前三项应为 `yes`，最后一项应为 `no`。本角色仅验收伸缩，不允许单实例镜像发布创建/回收保护 Deployment；发布功能需另按 [混合云权限说明](hybrid-autoscaling.md#1-配置-server-到-acs)扩展，并在专用 Namespace 验证。

### 6.5 将 ACS 连接材料挂载到 Server

**执行位置：M。** 准备接收目录：

```bash
install -d -m 0700 /root/cloudhelm-acs-import
```

K 和 M 为不同主机时，在 K 执行以下命令，地址换成实际管理主机、SSH 账号按现场策略设置：

```bash
scp -p token ca.crt api-url root@实际管理主机地址:/root/cloudhelm-acs-import/
```

若 K=M，在 M 执行 `cp /root/cloudhelm-acs/{token,ca.crt,api-url} /root/cloudhelm-acs-import/` 即可。

在 M 安装文件；Server 以 `10001:10001` 运行，需要组只读权限：

```bash
install -d -o root -g 10001 -m 0750 /etc/cloudhelm/acs
install -o root -g 10001 -m 0640 /root/cloudhelm-acs-import/token /etc/cloudhelm/acs/token.new
test -s /etc/cloudhelm/acs/token.new
mv /etc/cloudhelm/acs/token.new /etc/cloudhelm/acs/token
install -o root -g 10001 -m 0640 /root/cloudhelm-acs-import/ca.crt /etc/cloudhelm/acs/ca.crt
install -o root -g 10001 -m 0640 /root/cloudhelm-acs-import/api-url /etc/cloudhelm/acs/api-url
cd /srv/cloudhelm/cloudhelm-1.0.1
cat > deploy/acs.override.yml <<'EOF'
services:
  server:
    volumes:
      - /etc/cloudhelm/acs:/run/secrets/acs:ro
EOF
nano .env
```

在 `.env` 中增加或修改以下项，每项只保留一条。API 地址从 `/etc/cloudhelm/acs/api-url` 获取；CIDR 换成实际网关可达的本地业务网段和 Pod vSwitch 网段：

```dotenv
CLOUDHELM_ACS_KUBERNETES_API_URL=https://实际ACS端点:6443
CLOUDHELM_ACS_KUBERNETES_BEARER_TOKEN_FILE=/run/secrets/acs/token
CLOUDHELM_ACS_KUBERNETES_CA_FILE=/run/secrets/acs/ca.crt
CLOUDHELM_ACS_KUBERNETES_TIMEOUT_SECONDS=10
CLOUDHELM_AUTOSCALING_ENABLED=false
CLOUDHELM_GATEWAY_DISCOVERY_TOKEN=独立运行openssl-rand-hex-32生成的值
CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS=10.10.0.0/16,10.20.10.0/24
```

上面的 `openssl-rand-hex-32` 是来源说明，实际生成命令为 `openssl rand -hex 32`。推荐将发现令牌直接保存在受保护文件中，再将文件内容填入 `.env`：

```bash
umask 077
openssl rand -hex 32 > /root/cloudhelm-gateway-token
nano .env
ch config --quiet
ch up -d --no-build --no-deps --wait server
```

执行容器内的核心 Pod API 请求，预期 HTTP 200：

```bash
ch exec -T server python - <<'PY'
import ssl
from pathlib import Path
import httpx
from cloudhelm.config import get_settings
s = get_settings()
ctx = ssl.create_default_context(cafile=str(s.acs_kubernetes_ca_file))
with httpx.Client(verify=ctx, timeout=10) as client:
    r = client.get(s.acs_kubernetes_api_url.rstrip('/') + '/api/v1/namespaces/inference-test/pods',
                   headers={'Authorization': 'Bearer ' + Path(s.acs_kubernetes_bearer_token_file).read_text().strip()})
    r.raise_for_status()
    print('ACS core API OK:', r.status_code, 'pods:', len(r.json()['items']))
PY
```

此步骤验证 Server 容器的真实网络、CA、Token 与 RBAC。`401` 查 Token，`403` 查 RoleBinding，TLS 错误查 CA 与端点，超时查 VPC/访问控制。不要通过关闭证书校验解决。

后续轮换只需按上述流程原子替换 `/etc/cloudhelm/acs/token`。挂载整个目录而非单一文件，使新文件能被容器读取。当前客户端按评估周期重新创建；验证恢复后再撤销旧凭据。

### 6.6 创建逻辑服务并取得业务凭据

**执行位置：浏览器，用云舵全局管理员登录。** 打开“我的 → AI 服务与弹性/本地基座与 ACS 弹性 → 添加服务”，填写以下测试值：

|字段|首次测试值|来源/说明|
|---|---|---|
|服务名|`ACS真实弹性验收`|自行命名，不与生产混用|
|业务类型|`vlm` 或实际类型|按镜像用途|
|默认单实例容量|10|仅为控制链路验收，正式值需业务实测|
|自动扩缩容|关闭|先手工验证 Pod|
|最小/最大 ACS 副本|0 / 1|控制本次资源规模|
|故障兜底副本|1|验证本地测试实例异常|
|扩容/缩容阈值|70 / 40|百分比|
|统计窗口|60 秒|测试值，正式恢复 300 秒或经验证的值|
|冷却时间|30 秒|测试值，正式按实际负载与启动耗时调整|
|上报失效时间|60 秒|测试上报周期为 5 秒|
|Namespace|`inference-test`|与 RBAC、Deployment 完全一致|
|工作负载类型/名称|`Deployment` / `vlm-burst-test`|下文创建的资源名|
|ACS 目标容器名|`vlm`|必须等于 Deployment 容器名|

保存后记录页面返回的 **service_id 和一次性 report_token**。service_id 是 UUID，不是容器 ID、节点 ID、应用名称或企微 AgentId；令牌丢失时通过页面轮换。

在 K 创建只保存业务上报令牌的文件：

```bash
cd /root/cloudhelm-acs
umask 077
nano report-token
chmod 600 report-token
kubectl -n inference-test create secret generic cloudhelm-report-token \
  --from-file=token=./report-token --dry-run=client -o yaml | kubectl apply -f -
```

文件内容只填本服务的 report_token。`report-token` 暂留给后续独立负载上报器，测试结束后按密钥管理策略处理。

### 6.7 准备实际业务镜像与排空逻辑

**执行位置：业务开发/镜像构建环境。** 在 ACS 部署前确认镜像具备下述能力；仅注入 `CLOUDHELM_*` 环境变量不会让任意 AI 镜像自动实现上报。

业务程序读取这些值：

|变量|作用|
|---|---|
|`CLOUDHELM_URL`|平台 HTTPS 根地址|
|`CLOUDHELM_SERVICE_ID`|第 6.6 节创建的 service_id|
|`CLOUDHELM_SERVICE_TOKEN_FILE`|业务上报令牌文件路径|
|`CLOUDHELM_INSTANCE_POOL`|本地为 `local`、常规 ACS 为 `acs`|
|`CLOUDHELM_INSTANCE_ID`|本地固定唯一名称；ACS 为 Pod 名|
|`CLOUDHELM_ENDPOINT_HOST`|ACS 为 Pod IP；本地用实际可达地址或节点配置|
|`CLOUDHELM_ENDPOINT_SCHEME` / `CLOUDHELM_ENDPOINT_PORT`|业务实际协议和端口|

每 5–15 秒调用下列接口；在响应中读取 `report_after_seconds`，失败时有界退避并保留错误日志，不能仅发送请求而忽略响应：

```http
POST https://平台域名/api/v1/service-metrics/服务UUID/report
X-CloudHelm-Service-Token: 从令牌文件读取并strip后的内容
Content-Type: application/json
```

请求示例，各值由应用运行态生成：

```json
{
  "instance_id": "vlm-burst-test-实际Pod名",
  "pool": "acs",
  "active_businesses": 0,
  "healthy": true,
  "ready": true,
  "accepting": true,
  "endpoint_scheme": "http",
  "endpoint_host": "10.20.10.35",
  "endpoint_port": 9000,
  "metadata": {"model": "实际不可变模型版本"}
}
```

加载模型时必须上报 `ready=false`、`accepting=false`；健康但未加载完成不应发布路由。ACS 请求不能携带 `container_id`。当前真实响应包含：

```json
{
  "accepted": true,
  "desired_state": "serve",
  "endpoint_registered": true,
  "report_after_seconds": 15
}
```

按当前 1.0.1 状态执行：

|响应 desired_state|业务处理|
|---|---|
|`serve`|完成就绪检查后接单，持续上报|
|`draining`|立即停止新业务，上报 `accepting=false`；保留在途任务与上报循环，直到业务数归零|
|`maintenance`|保持停止接单和健康上报；不要自行重新接单，不要直接退出|
|`restoring`|检查模型和依赖，恢复后上报 healthy/ready/accepting 均为 true|

当前代码在收到 `accepting=false` 且 `active_businesses=0` 的上报后，可能立即将 `draining` 转为 `maintenance`；因此客户端必须处理这两种状态。兼容旧客户端时可以额外识别 `drain`，不能只识别它。

`healthy` 表示进程和处理能力健康，排空并不等于 unhealthy；缩容候选仍需健康上报。readiness 可以在排空时失败来阻止新请求，liveness 应保持成功。SIGTERM 也要停止新请求、等待在途完成并继续上报，终止宽限期覆盖最长任务。

**通过条件：**业务负责人提供镜像、启动参数、模型目录、实际端口、模型就绪探针路径、进程存活路径、真实业务请求示例。下节 `/ready`、`/health`、9000 都是必须与业务实现核对的示例。

### 6.8 准备 ACR 拉取凭据

**执行位置：ACR 控制台及 K。** 在 ACR 仓库详情复制完整镜像拉取地址与明确 Tag；从“访问凭证”取得可拉取此仓库的 Registry 用户名和密码。公网和 VPC Registry 主机名必须与镜像地址完全一致；跨账号 VPC 访问需先确认网络与实例访问控制，必要时同步到 ACS 所属账号仓库。

在 K 生成临时 Docker 配置并创建 Secret，密码通过交互输入，不放在命令行参数中：

```bash
cd /root/cloudhelm-acs
umask 077
python3 - <<'PY'
import base64, getpass, json
from pathlib import Path
registry = input('Registry 主机名，不含https和路径: ').strip()
username = input('Registry 用户名: ').strip()
password = getpass.getpass('Registry 密码: ')
assert registry and username and password
Path('registry-auth.json').write_text(json.dumps({'auths': {registry: {
    'auth': base64.b64encode((username + ':' + password).encode()).decode()}}}))
PY
kubectl -n inference-test create secret generic acr-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=registry-auth.json --dry-run=client -o yaml | kubectl apply -f -
rm registry-auth.json
```

同账号已配置免密拉取时，可不创建该 Secret，同时删除下文 `imagePullSecrets` 字段。Secret 仅解决镜像认证，不会解决网络不可达。

### 6.9 可选 NAS 模型存储

模型已完整打入镜像时跳过本节。需要共享模型时，在 NAS 控制台创建同地域文件系统与 ACS VPC 内挂载点，权限组只允许上传机及所需 Pod 网段，记录挂载域名。先在同 VPC 的模型上传机挂载并上传；下列目录和 NFSv3 示例适用于通用型 NAS，其他类型按挂载指令调整。

**执行位置：同 VPC 模型上传 ECS，不在生产 GPU 主机改已有挂载。**

```bash
sudo apt-get install -y nfs-common rsync
sudo mkdir -p /mnt/cloudhelm-model-nas
sudo mount -t nfs -o vers=3,nolock,tcp,noresvport \
  实际NAS挂载域名:/ /mnt/cloudhelm-model-nas
sudo mkdir -p /mnt/cloudhelm-model-nas/models/vlm/model-v1
sudo rsync -a --info=progress2 /实际本地模型目录/ \
  /mnt/cloudhelm-model-nas/models/vlm/model-v1/
sudo du -sh /mnt/cloudhelm-model-nas/models/vlm/model-v1
```

核对模型自带的校验清单，确认权重、Tokenizer、配置文件齐全；用业务镜像的实际 UID 验证只读权限，不通过放开整个 NAS 写权限解决。

在 K 新建 `/root/cloudhelm-acs/nas.yaml`，替换挂载域名、子目录和声明容量：

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: cloudhelm-test-model
spec:
  capacity:
    storage: 220Gi
  accessModes: [ReadOnlyMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: nasplugin.csi.alibabacloud.com
    volumeHandle: cloudhelm-test-model
    volumeAttributes:
      server: "REPLACE_ME_NAS_MOUNT_HOST"
      path: "/models/vlm/model-v1"
  mountOptions:
    - nolock,tcp,noresvport
    - vers=3
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cloudhelm-test-model
  namespace: inference-test
spec:
  accessModes: [ReadOnlyMany]
  storageClassName: ""
  volumeName: cloudhelm-test-model
  resources:
    requests:
      storage: 220Gi
```

```bash
kubectl apply --dry-run=server -f nas.yaml
kubectl apply -f nas.yaml
kubectl -n inference-test get pvc cloudhelm-test-model
kubectl get pv cloudhelm-test-model
```

应为 `Bound`。220Gi 仅是示例声明，不会限制实际 NAS 使用或代表预付容量；目录实际数据与 NAS 规格决定容量。模板见 [ACS NAS 静态卷说明](https://help.aliyun.com/zh/cs/user-guide/mount-statically-provisioned-nas-volumes)。

### 6.10 创建 GPU Deployment

**执行位置：K。** 新建 `/root/cloudhelm-acs/deployment.yaml`：

```bash
nano /root/cloudhelm-acs/deployment.yaml
```

完整模板如下。保持 `replicas: 0`，先替换所有 `REPLACE_ME`；GPU 型号、CPU/内存组合按选定规格填写。示例单卡、8 vCPU、32Gi 内存必须核对该型号实际支持的组合。

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vlm-burst-test
  namespace: inference-test
spec:
  replicas: 0
  selector:
    matchLabels:
      app: vlm-burst-test
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    metadata:
      labels:
        app: vlm-burst-test
        alibabacloud.com/compute-class: "gpu"
        alibabacloud.com/gpu-model-series: "REPLACE_ME_GPU_MODEL"
      annotations:
        network.alibabacloud.com/vswitch-ids: "REPLACE_ME_VSWITCH_ID"
        network.alibabacloud.com/security-group-ids: "REPLACE_ME_SECURITY_GROUP_ID"
    spec:
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 300
      imagePullSecrets:
        - name: acr-pull
      containers:
        - name: vlm
          image: REPLACE_ME_REGISTRY/REPLACE_ME_NAMESPACE/REPLACE_ME_IMAGE:REPLACE_ME_TAG
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 9000
          env:
            - name: CLOUDHELM_URL
              value: "https://REPLACE_ME_PLATFORM_DOMAIN"
            - name: CLOUDHELM_SERVICE_ID
              value: "REPLACE_ME_SERVICE_UUID"
            - name: CLOUDHELM_INSTANCE_POOL
              value: "acs"
            - name: CLOUDHELM_INSTANCE_ID
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: CLOUDHELM_ENDPOINT_HOST
              valueFrom:
                fieldRef:
                  fieldPath: status.podIP
            - name: CLOUDHELM_ENDPOINT_SCHEME
              value: "http"
            - name: CLOUDHELM_ENDPOINT_PORT
              value: "9000"
            - name: CLOUDHELM_SERVICE_TOKEN_FILE
              value: "/run/secrets/cloudhelm/token"
          volumeMounts:
            - name: report-token
              mountPath: /run/secrets/cloudhelm
              readOnly: true
          resources:
            requests:
              cpu: "8"
              memory: "32Gi"
              nvidia.com/gpu: "1"
            limits:
              cpu: "8"
              memory: "32Gi"
              nvidia.com/gpu: "1"
          startupProbe:
            httpGet:
              path: /ready
              port: http
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 180
          readinessProbe:
            httpGet:
              path: /ready
              port: http
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 15
            timeoutSeconds: 3
            failureThreshold: 4
      volumes:
        - name: report-token
          secret:
            secretName: cloudhelm-report-token
            defaultMode: 0444
```

Secret 仅挂给此业务容器；0444 使镜像内普通 UID 可读，不依赖 root 用户。整个目录挂载支持 Secret 更新，业务客户端仍需按其读取方式验证轮换生效。实际镜像如需要启动参数、模型路径或缓存目录，使用业务方提供的 `command`/`args`/环境变量补充，不能凭空假定 vLLM、ASR 和 TTS 的参数一致。

NAS 方案在业务容器的 `volumeMounts` 中追加以下两行对应项，并在 Pod `volumes` 中追加 PVC 项；它们分别位于现有列表中，不能替换令牌挂载：

```yaml
# spec.template.spec.containers 中 vlm 的 volumeMounts 列表追加
- name: model
  mountPath: /models
  readOnly: true
```

```yaml
# spec.template.spec.volumes 列表追加
- name: model
  persistentVolumeClaim:
    claimName: cloudhelm-test-model
```

应用通过 `/models` 读取第 6.9 节指定的模型版本目录，缓存/临时文件放独立可写路径。启动探针示例最多允许约 30 分钟加载，按模型实测调整；排空时 liveness 继续成功。当前最大副本 1 控制的是常规弹性副本，滚动发布的 `maxSurge: 1` 仍可能临时多申请一份 GPU，发布验收需另行规划配额。

```bash
# 应无输出，发现占位值则先编辑。
rg -n 'REPLACE_ME' deployment.yaml
kubectl apply --dry-run=server -f deployment.yaml
kubectl apply -f deployment.yaml
kubectl -n inference-test get deployment vlm-burst-test
```

没有安装 `rg` 时使用 `grep -n REPLACE_ME deployment.yaml`。预期初始副本为 0。从本步骤开始，正常伸缩不重复 apply 含 `replicas: 0` 的文件，否则会覆盖平台当前副本数。

### 6.11 手工验证真实 Pod

保持 Server 全局和测试服务自动伸缩均关闭，在 K 手工扩到 1：

```bash
kubectl -n inference-test scale deployment vlm-burst-test --replicas=1
kubectl -n inference-test get pods -l app=vlm-burst-test -o wide
kubectl -n inference-test get events --sort-by=.metadata.creationTimestamp
kubectl -n inference-test logs deployment/vlm-burst-test -c vlm --tail=200
```

另开 K 终端观察，重新设置 `KUBECONFIG` 后执行：

```bash
kubectl -n inference-test rollout status deployment/vlm-burst-test --timeout=30m
kubectl -n inference-test get pods -l app=vlm-burst-test -o wide
```

确认 GPU 调度、镜像拉取、模型挂载、模型加载、探针和业务上报分别正常。长时间等待时另开终端读日志和 events，不把 `rollout` 超时直接解释成平台错误。

在具有 Pod 私网路由的测试机（如同 VPC ECS）执行实际业务请求。先在 K 取得地址：

```bash
ACS_POD=$(kubectl -n inference-test get pods -l app=vlm-burst-test -o jsonpath='{.items[0].metadata.name}')
ACS_POD_IP=$(kubectl -n inference-test get pod "$ACS_POD" -o jsonpath='{.status.podIP}')
printf 'Pod=%s IP=%s\n' "$ACS_POD" "$ACS_POD_IP"
```

将实际 IP 填入测试机上的请求：

```bash
curl --fail --max-time 10 http://实际PodIP:9000/ready
# request.json 的结构和接口路径由业务 API 文档提供，不能对所有模型通用。
nano request.json
curl --fail --max-time 300 \
  -H 'Content-Type: application/json' \
  --data-binary @request.json http://实际PodIP:9000/实际业务接口路径
```

如业务需要认证，使用该业务的独立测试凭据。`/ready` 成功只证明探针，必须额外验证有效的模型请求及响应。没有 Pod 私网路由时可临时 `kubectl port-forward deployment/vlm-burst-test 19000:9000 -n inference-test` 验证应用，再单独打通实际业务网络。

确认该测试 Pod 没有在途业务后，手工缩回 0：

```bash
kubectl -n inference-test scale deployment vlm-burst-test --replicas=0
```

手工 scale 不经过 Cloud Helm 排空，只允许在无业务的测试实例上执行。

### 6.12 准备路由检查与可调测试负载

**执行位置：K。** 将 M 的 `/root/cloudhelm-gateway-token` 安全复制到 K 的 `/root/cloudhelm-acs/gateway-token`，模式 0600，内容应与 Server `.env` 一致。配置测试参数：

```bash
cd /root/cloudhelm-acs
umask 077
cat > test.env <<'EOF'
CLOUDHELM_URL=https://实际平台域名
SERVICE_ID=第6.6节生成的真实服务UUID
EOF
chmod 600 test.env gateway-token
nano test.env
```

在 K 执行 `nano /root/cloudhelm-acs/load-generator.py`，粘贴以下代码。它只为独立测试服务模拟一个本地业务实例，**不产生真实推理请求、不调用 Docker、不占用本地 GPU**；真实请求验收仍执行第 6.11、6.16 节。

```python
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parent
url = os.environ['CLOUDHELM_URL'].rstrip('/')
service_id = os.environ['SERVICE_ID']
assert url.startswith('https://')
while True:
    try:
        control = json.loads((root / 'test-control.json').read_text())
        body = {
            'instance_id': 'cloudhelm-acceptance-local',
            'pool': 'local',
            'active_businesses': int(control['active_businesses']),
            'healthy': bool(control['healthy']),
            'ready': bool(control['healthy']),
            'accepting': bool(control['healthy']),
            'metadata': {'purpose': 'dedicated-acceptance-only'},
        }
        request = urllib.request.Request(
            f'{url}/api/v1/service-metrics/{service_id}/report',
            data=json.dumps(body).encode(),
            headers={
                'Content-Type': 'application/json',
                'X-CloudHelm-Service-Token': (root/'report-token').read_text().strip(),
            }, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        print(datetime.now(timezone.utc).isoformat(), body['active_businesses'],
              body['healthy'], result.get('desired_state'), flush=True)
    except urllib.error.HTTPError as exc:
        print('Report HTTP error:', exc.code, exc.read(1000).decode(errors='replace'), flush=True)
    except Exception as exc:
        print(type(exc).__name__, str(exc), flush=True)
    time.sleep(5)
```

初始低负载，启动持续上报；此终端保持运行，其他命令在新 K 终端执行：

```bash
printf '%s\n' '{"active_businesses":0,"healthy":true}' > test-control.json
set -a
. ./test.env
set +a
python3 -u load-generator.py
```

模拟实例不携带地址，不应出现在发现路由中。不要在测试过程中随意停止此进程：超过失效时间会触发本地故障兜底，干扰低负载缩容用例。

另开 K 终端，执行 `nano /root/cloudhelm-acs/check-routes.py`，粘贴以下代码：

```python
import json, os, urllib.request
from pathlib import Path
root = Path(__file__).resolve().parent
request = urllib.request.Request(
    os.environ['CLOUDHELM_URL'].rstrip('/') + '/api/v1/service-discovery/routes',
    headers={'X-CloudHelm-Gateway-Token': (root/'gateway-token').read_text().strip()})
with urllib.request.urlopen(request, timeout=10) as response:
    data = json.load(response)
    print('ETag:', response.headers.get('ETag'))
service = next(item for item in data['services'] if item['service_id'] == os.environ['SERVICE_ID'])
print(json.dumps(service, ensure_ascii=False, indent=2))
```

```bash
cd /root/cloudhelm-acs
set -a
. ./test.env
set +a
python3 check-routes.py
```

初始 `endpoints=[]`。如 503，查 Server 发现令牌是否配置并重建生效；401 查发现令牌；找不到服务查 UUID。

### 6.13 开启测试服务自动扩容

**执行位置：M。** 修改 `.env` 的 `CLOUDHELM_AUTOSCALING_ENABLED=true`，只改已有行，不追加重复项：

```bash
cd /srv/cloudhelm/cloudhelm-1.0.1
nano .env
ch config --quiet
ch up -d --no-build --no-deps --wait server
ch logs --tail=100 server
```

浏览器只开启 `ACS真实弹性验收` 的自动扩缩容，其他服务保持关闭。K 的持续上报器运行至少一个完整窗口后，应保持 0 副本。接着在另一 K 终端提高负载，使用原子替换避免上报器读到半份文件：

```bash
cd /root/cloudhelm-acs
printf '%s\n' '{"active_businesses":8,"healthy":true}' > test-control.json.new
mv test-control.json.new test-control.json
export KUBECONFIG=/root/cloudhelm-acs/admin.kubeconfig
kubectl -n inference-test get pods -l app=vlm-burst-test -w
```

模拟容量 10、业务数 8，即 80%，超过 70% 阈值。平台至少需要覆盖统计窗口的 80%，再叠加下一次评估、冷却和模型冷启动时间；不承诺 60 秒内模型就绪。

在其他 K 终端查看副本和错误：

```bash
kubectl -n inference-test get deployment vlm-burst-test
kubectl -n inference-test get events --sort-by=.metadata.creationTimestamp
kubectl -n inference-test logs deployment/vlm-burst-test -c vlm --tail=200
python3 check-routes.py
```

**通过条件：**出现 `load_scale_up` 事件，Deployment 从 0 到 1；真实模型就绪后 ACS 实例健康上报，发现接口中的 `instance_id=Pod名`、`host=Pod IP`、端口与协议正确。模拟本地实例不在路由里。再从可达测试机发一个真实业务请求，确认扩出的实例可用。

### 6.14 验证低负载排空缩容

**执行位置：K。** 将模拟负载改为 0，但保持上报器持续运行：

```bash
cd /root/cloudhelm-acs
printf '%s\n' '{"active_businesses":0,"healthy":true}' > test-control.json.new
mv test-control.json.new test-control.json
```

同时观察平台事件、业务应用日志、`python3 check-routes.py` 和 `kubectl get pods -w`。预期顺序为：

```text
窗口利用率低于缩容阈值且冷却结束
→ 选择 ACS 实例，期望状态变为 draining
→ 发现结果排除该实例，业务端收到响应后停止接新请求
→ 业务继续上报 accepting=false 与真实在途数量
→ active_businesses=0 后进入 maintenance
→ 平台 PATCH Pod 删除优先级并降低 Deployment 副本
→ Pod 退出，Deployment 回到 0
```

需要分别看到 `load_drain_request` 和成功的 `load_scale_down`。路由应在 Pod 删除前移除。客户端只判断 `drain`、排空时退出上报、或错误上报 `healthy=false` 都可能阻塞正常缩容。

带在途任务验收时，先在测试 Pod 上发起一个真实长任务，然后降低模拟负载并停止其他新请求，记录排空指令、路由移除、任务完成和 Pod 退出时间。任务不得在完成前被终止；单个现有连接是否保留还取决于实际网关和应用行为，必须使用真实协议验证。

### 6.15 验证本地故障兜底和恢复

**执行位置：K，只改变模拟实例。** 在测试服务回到 0 后执行：

```bash
printf '%s\n' '{"active_businesses":0,"healthy":false}' > test-control.json.new
mv test-control.json.new test-control.json
```

应出现故障兜底事件，Deployment 回到 1，不需要等待负载窗口完整；但资源调度和模型启动仍需要时间。随后恢复模拟实例：

```bash
printf '%s\n' '{"active_businesses":0,"healthy":true}' > test-control.json.new
mv test-control.json.new test-control.json
```

等窗口和冷却满足，观察正常排空并缩回 0。另可在测试服务上暂时中断模拟上报，验证超时失联，恢复后重新核对实例状态。不得停止实际生产 GPU 容器做这个用例。

### 6.16 从控制链路测试进入真实业务验收

第 6.13 节的模拟业务数只验证策略；正式验收还需要完成以下步骤：

1. 浏览器先关闭测试服务自动伸缩，确认测试 Pod 无业务并回到 0，再停止模拟上报器。
2. 在实例列表删除 `cloudhelm-acceptance-local`；不要把停止上报的模拟实例留作受监控本地实例，否则下一次启用时会触发故障兜底。
3. 为一套**独立实际业务测试实例**接入同样上报协议。本地 `instance_id` 固定唯一，`pool=local`。使用第 6.6 节对应服务凭据；需要容器关联时按第 6.17 节处理。
4. 根据真实单实例承载能力设置容量；异构 GPU 分别设置实例容量覆盖值，不能继续把示例 10 当实测容量。
5. 打通实际网关到本地和 ACS 的网络，核对实例上报的 IP、协议、端口。网关按服务发现快照对账并处理地址过期；无网关时只能验收直接访问能力，不能认定流量自动接管成功。
6. 开启测试服务弹性，用业务自己的压测工具按真实协议增加请求。负载来源、请求样本、并发、持续时间和成功判定由该业务确定；平台没有通用于 VLM/ASR/TTS 的请求生成命令。
7. 记录本地饱和、平台触发、ACS 可接单、真实请求成功的时间，以及成功率、响应耗时、在途任务是否中断。
8. 降载并验证真实长任务排空与缩容；再验证副本上限、故障兜底和恢复。

如果没有可用的独立本地 GPU 测试资源，先完成云上实际模型与模拟本地上报测试，保留真实本地负载与网关接管验收项；不借用正在服务的 GPU 做破坏性验证。

### 6.17 本地实际业务容器的接入

**执行位置：A 业务节点，由业务发布负责人操作。** 在对应业务 Compose 中增加以下配置。环境变量必须由业务程序读取；标签由 Agent 读取，两侧实例 ID 保持相同：

```yaml
services:
  vlm:
    environment:
      CLOUDHELM_URL: "https://实际平台域名"
      CLOUDHELM_SERVICE_ID: "实际服务UUID"
      CLOUDHELM_INSTANCE_ID: "vlm-local-001"
      CLOUDHELM_INSTANCE_POOL: "local"
      CLOUDHELM_ENDPOINT_SCHEME: "http"
      CLOUDHELM_ENDPOINT_PORT: "9000"
      CLOUDHELM_SERVICE_TOKEN_FILE: "/run/secrets/cloudhelm_service_token"
    labels:
      cloudhelm.service_id: "实际服务UUID"
      cloudhelm.instance_id: "vlm-local-001"
    secrets:
      - cloudhelm_service_token
secrets:
  cloudhelm_service_token:
    file: ./secrets/report-token
```

创建业务 Secret 文件，使用镜像实际 UID/GID设置可读权限。先用镜像说明或不占 GPU 的元数据检查确定 UID；镜像声明为空时还需确认入口脚本会否切换用户：

```bash
docker image inspect 实际业务镜像:实际Tag --format '{{json .Config.User}}'
install -d -m 0700 secrets
nano secrets/report-token
chmod 600 secrets/report-token
docker compose config --quiet
```

非 root 业务需要实际用户可读的所有者/组设置，并验证容器能读取。业务挂载、启动参数、GPU 资源保持原配置；标签生效通常需重建业务容器，将其安排在正常版本切换，先准备镜像与测试再排空，不能为了接入平台提前释放 GPU。

Agent 发现标签后自动建立容器绑定，业务端不需要传 `container_id`。平台节点详情配置“网关访问 IP”为网关实际可达的宿主机 IP，并保证它在发现允许 CIDR 内。节点端口映射、实际监听地址和应用上报端口也要一致。

### 6.18 异常验证与试运行收尾

在独立测试服务中逐项记录，不在生产服务上注入故障：

|用例|操作位置与方式|预期结果|
|---|---|---|
|权限拒绝|K 使用无授权测试身份请求同一资源|返回 403，实际服务凭据不被替换|
|Token 过期|独立测试平台使用短期 Token 等待到期，再部署新 Token|能发现认证失败，更新后重新评估|
|错误镜像/模型|独立 Deployment 使用不存在的测试 Tag 或缺失模型目录|Pending/启动错误可定位，未就绪实例不发布路由|
|最大副本|持续高负载，最大副本保持 1|不无限创建 Pod；容量不足时按规则告警|
|Server 重启|M 在没有活动发布时仅重建 Server|节点/业务重新连接，检查后台评估是否持续推进|
|数据库中断|只在独立演练部署短暂停库再恢复|记录请求失败及后台循环恢复情况；发现不恢复则列为上线缺陷|
|带任务缩容|测试服务真实长请求运行时降载|先停止新业务，任务结束后再退出 Pod|

不要在负载扩缩容期间手工反复 `kubectl scale` 或 apply 固定副本值，不让 HPA/KEDA/GitOps 同时修改该工作负载副本。

完成测试后先关闭测试服务自动伸缩，确认无在途业务，再停止模拟上报并删除模拟实例。正式试运行把窗口、冷却、失效时间和容量改为实测参数；保留较小副本上限，连续观察 24–48 小时，短期 Token 在到期前续签。

每轮记录：配置版本、镜像摘要、负载条件、扩缩容事件、Pod 时间线、模型就绪时间、实际请求结果与失败日志。只有记录齐全并通过真实链路，才启用下一项实际服务。

## 7. 数据库巡检与参数变更

### 7.1 容量、保留范围和膨胀观测

在管理主机安装好 `ch` 后执行：

```bash
ch exec -T database psql -U cloudhelm -d cloudhelm -v ON_ERROR_STOP=1 <<'SQL'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT relname,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_with_indexes,
       n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC;
SELECT target_type, count(*) AS rows, min(sampled_at), max(sampled_at)
FROM metric_samples GROUP BY target_type;
SELECT pg_total_relation_size('metric_samples') / NULLIF(count(*), 0)
       AS measured_bytes_per_row_including_indexes
FROM metric_samples;
SHOW autovacuum;
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
SQL
df -h /srv/cloudhelm
sudo du -sh /srv/cloudhelm/cloudhelm-1.0.1/deploy/cloudhelm-postgres-data/pg_wal
```

`count(*)` 和全表统计在大表上较重，安排低峰；`n_live_tup`/`n_dead_tup` 是估计值。运行一周并经历清理后再估算实际单行占用；用目标行数乘这个值修正磁盘预算。表与索引大小函数见 [PostgreSQL 管理函数](https://www.postgresql.org/docs/17/functions-admin.html)。

检查磁盘使用率（例如 70% 预警、85% 严重）、增长速率、可用天数、WAL 增长、连接数、死元组、备份年龄和持续失败日志。节点掉线、监控数据陈旧和数据库查询失败应分开告警。

删除旧记录后 PostgreSQL 通常将空间留作内部复用，云盘占用不会立即下降。保持 autovacuum；不要把 `VACUUM FULL` 作为每日任务，它会重写表、占额外空间并持有强锁。维护机制见 [PostgreSQL 日常清理](https://www.postgresql.org/docs/17/routine-vacuuming.html)。

### 7.2 修改历史配置

编辑 `.env` 的 `INTERVAL_SECONDS`、`RETENTION_HOURS` 和 `MAX_ROWS` 后：

```bash
ch config --quiet
ch up -d --no-build --no-deps --wait server
ch logs --tail=100 server
```

`restart server` 不会重新加载 Compose 环境文件。增大保留期只影响后续保留，不会补回已删历史；减小保留期或非零行数上限会在后台清理时分批删除旧记录，先做备份。半年时间保留可设 `RETENTION_HOURS=4320`、`MAX_ROWS=0`。只有 Server 短暂切换，数据库和业务容器保持运行。

## 8. 数据备份与 OSS 配置

### 8.1 备份策略

|对象|建议频率与保留|说明|
|---|---|---|
|PostgreSQL 逻辑备份|每天一次；本地 7 天、OSS 30 天|采用 `pg_dump -Fc`；每次必须检查退出状态和文件完整性。|
|长期归档|按需每月一份，单独前缀保留 12 个月|单独安排复制任务/调度，不包含在下方日备份脚本中。|
|配置/密钥|每次改动后，随数据库备份同步留存|`.env`、`postgres.env`、Compose、自定义挂载、ACS 凭据及轮换方式、Caddy 配置。敏感备份加密存储、限制解密权限。|
|Agent 状态|首次注册后、凭据/配置变动后|每节点独立保存 `agent.env`、Compose/GPU overlay、`agent-state.json`；不混用节点文件。|
|恢复演练|至少每月一次及重大升级前|还原到独立空数据库，核对行数、权限和历史时间范围；记录耗时。|

每日备份且持续成功时，RPO（最多可丢失数据时间）约 24 小时；RTO（恢复服务耗时）需用真实备份恢复测定。单次 `pg_dump` 提供一致性快照，可在业务继续写入时备份；它只包含一个数据库，不包含全局角色。本文采用模板账号重新初始化目标数据库、用 `--no-owner --no-acl` 恢复；自定义角色/授权应另存受保护的 `pg_dumpall --globals-only`。参考 [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)。

不要直接打包正在写入的 PostgreSQL 数据目录作为唯一备份。阿里云云盘快照可补充主机恢复，但普通文件复制/未协调的快照不等同于已验证的数据库备份。需要分钟级恢复点时，另行配置基础备份与连续 WAL 归档，或采用满足要求的托管 PostgreSQL 备份服务；本 Compose **没有自动配置 PITR**。机制见 [PostgreSQL 连续归档与时间点恢复](https://www.postgresql.org/docs/17/continuous-archiving.html)。

### 8.2 OSS 与 RAM

**执行位置：阿里云控制台。**

1. OSS“Bucket 列表 → 创建 Bucket”：选择实际地域，设为私有、阻止公共访问，启用默认 SSE-OSS 加密；记录 Bucket 名和“概览 → 访问端口”中的地域与 Endpoint。同地域 M 可用内网 Endpoint。
2. RAM“身份管理 → 角色 → 创建角色”：可信实体选择阿里云服务 ECS，角色名例如 `CloudHelmBackupRole`。
3. RAM“权限策略 → 创建自定义策略 → 脚本编辑”，创建下面的上传策略，把 `YOUR-BACKUP-BUCKET` 替换为实际名称，然后授权给该角色。
4. ECS“实例 → 管理主机 → 实例设置 → 授予/收回 RAM 角色”，绑定这个角色。步骤见 [ECS 实例 RAM 角色](https://help.aliyun.com/zh/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance)。

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:PutObject", "oss:ListParts", "oss:AbortMultipartUpload"],
      "Resource": ["acs:oss:*:*:YOUR-BACKUP-BUCKET/cloudhelm/daily/*"]
    }
  ]
}
```

上述策略允许日备份上传和分片管理，无读取和对象删除权。大文件上传权限见 [OSS 上传命令](https://help.aliyun.com/zh/oss/developer-reference/cp-upload-file)。恢复操作另由具有目标前缀 `oss:GetObject` 权限的恢复身份执行；若需列目录，再单独授予 Bucket 列举权限。SSE-KMS 方案还需对应 KMS 权限，本示例使用 SSE-OSS。

**执行位置：M，root 账号。** 安装固定版本 ossutil，以下使用官方当前列出的 2.4.0；Linux amd64/arm64 分别校验对应 SHA-256。以后升级需从官方页面重新核对版本和校验值。

```bash
install -d -m 0700 /root/cloudhelm-oss-install
cd /root/cloudhelm-oss-install
case "$(uname -m)" in
  x86_64)
    OSS_ARCH=amd64
    OSS_SHA=85edf66b2fb7238f5c7e25cab820cf29312319fe4935b7c86a6b8485eb434f3c
    ;;
  aarch64)
    OSS_ARCH=arm64
    OSS_SHA=7767240e9667d1f26fbe4e04f90b796b4356889e77d0076cc8a543c8ce081987
    ;;
  *) echo '请按官方文档选择平台'; exit 1 ;;
esac
curl -fLO "https://gosspublic.alicdn.com/ossutil/v2/2.4.0/ossutil-2.4.0-linux-${OSS_ARCH}.zip"
printf '%s  %s\n' "$OSS_SHA" "ossutil-2.4.0-linux-${OSS_ARCH}.zip" | sha256sum -c -
unzip "ossutil-2.4.0-linux-${OSS_ARCH}.zip"
install -m 0755 "ossutil-2.4.0-linux-${OSS_ARCH}/ossutil" /usr/local/bin/ossutil
ossutil version
nano /root/.ossutilconfig
```

在 `/root/.ossutilconfig` 写入下例；地域和 Endpoint 从 Bucket 控制台获取，角色名与绑定角色相同：

```ini
[default]
mode=Ali-EcsRamRole
ecsRoleName=CloudHelmBackupRole
region=cn-beijing
endpoint=https://oss-cn-beijing-internal.aliyuncs.com
```

```bash
chmod 600 /root/.ossutilconfig
umask 077
printf 'Cloud Helm backup upload check\n' > /root/cloudhelm-oss-upload-check.txt
ossutil cp /root/cloudhelm-oss-upload-check.txt \
  oss://YOUR-BACKUP-BUCKET/cloudhelm/daily/initial-upload-check.txt
```

在 OSS 控制台核对文件存在。上传角色没有列举权限时，`ossutil ls` 失败不能当作上传失败。`Ali-EcsRamRole` 使用 IMDSv2 自动获取临时凭据；不需要填写 AccessKey。安装、校验和凭据格式见 [ossutil 官方文档](https://help.aliyun.com/en/oss/developer-reference/ossutil-overview/)。

在 Bucket“生命周期”中创建前缀 `cloudhelm/daily/` 的 30 天过期规则；分片任务另外设置过期清理。若保留月备份，为 `cloudhelm/monthly/` 建独立规则，授权策略和月备份任务也需对应增加，日备份脚本不会自动产生月备份。开启版本控制时同时配置非当前版本和删除标记处理，机制见 [OSS 生命周期](https://help.aliyun.com/zh/oss/user-guide/configuration-elements)。

备份恢复身份与日常上传身份分开管理。重要数据按要求增加异地或独立账号副本，定期验证下载与恢复。配置/密钥备份上传前使用组织的加密工具加密，解密密钥独立保存。

### 8.3 可直接落地的每日备份脚本

**执行位置：M。** 执行 `nano /usr/local/sbin/cloudhelm-backup` 新建文件，复制下面完整脚本并替换 Bucket；确认 `docker` 和 `ossutil` 所在目录在 `PATH` 中。本脚本仅备份数据库，配置与密钥归档另按上表执行。

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

install -d -m 0700 /var/backups/cloudhelm
exec 9>/var/backups/cloudhelm/.backup.lock
flock -n 9 || exit 1

cd /srv/cloudhelm/cloudhelm-1.0.1
compose=(docker compose --env-file .env --env-file deploy/postgres.env
         -f deploy/postgres.compose.yml)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_root=/var/backups/cloudhelm
work=$(mktemp -d "$backup_root/.partial-XXXXXX")
trap 'rm -rf -- "$work"' EXIT

"${compose[@]}" exec -T database pg_dump -U cloudhelm -d cloudhelm -Fc \
  > "$work/cloudhelm.dump"
test -s "$work/cloudhelm.dump"
"${compose[@]}" exec -T database pg_restore --list \
  < "$work/cloudhelm.dump" > /dev/null
(cd "$work" && sha256sum cloudhelm.dump > SHA256SUMS)

final="$backup_root/$stamp"
mv -- "$work" "$final"
# 先上传数据，再上传校验清单；清单存在才视为远端备份批次完成。
ossutil cp "$final/cloudhelm.dump" "oss://YOUR-BACKUP-BUCKET/cloudhelm/daily/$stamp/cloudhelm.dump"
ossutil cp "$final/SHA256SUMS" "oss://YOUR-BACKUP-BUCKET/cloudhelm/daily/$stamp/SHA256SUMS"
# 仅在本次本地备份和远端上传都成功后清理本脚本产生的过期批次。
find "$backup_root" -mindepth 1 -maxdepth 1 -type d \
  -name '20??????T??????Z' -mmin +10080 -exec rm -rf -- {} +
printf 'Cloud Helm backup uploaded: %s\n' "$stamp"
```

设置权限、先手动执行并核对 OSS 后再启用计划任务：

```bash
sudo chmod 700 /usr/local/sbin/cloudhelm-backup
sudo /usr/local/sbin/cloudhelm-backup
sudo install -m 0600 /dev/null /var/log/cloudhelm-backup.log
sudo crontab -e
```

在 root 的 crontab 中添加（时间按宿主机时区）：

```cron
15 2 * * * /usr/local/sbin/cloudhelm-backup >> /var/log/cloudhelm-backup.log 2>&1
```

为日志配置轮转，为任务非零退出及最近成功备份超过 26 小时配置外部监控。脚本本身不会发送告警。`pg_restore --list` 仅验证归档目录可读，不证明所有数据都能恢复，完整恢复演练仍然必需。数据大时注意备份读 I/O 与磁盘空间。

备份目录默认位于 `/var/backups`，若系统盘不足应独立挂载备份盘或调整路径。若一次 dump 实测为 B GiB，7 天本地保留加正在生成的一份至少准备约 `8 × B`，再留余量；OSS 日备份约 `30 × B`，另加版本、月备份和异地复制。不能假定 dump 总是按固定比例压缩。

### 8.4 配置与密钥备份

**执行位置：M。** 数据库 dump 不包含 `.env`、ACS 凭据、代理配置和部署路径。每次配置变更后手动执行以下流程；GPG 会交互要求加密口令，口令按组织密码库独立保管，不写入脚本或备份目录。

```bash
umask 077
install -d -m 0700 /var/backups/cloudhelm-config
config_stamp=$(date -u +%Y%m%dT%H%M%SZ)
config_tar="/var/backups/cloudhelm-config/config-${config_stamp}.tar.gz"
items=(srv/cloudhelm/cloudhelm-1.0.1/.env
       srv/cloudhelm/cloudhelm-1.0.1/deploy/postgres.env
       srv/cloudhelm/cloudhelm-1.0.1/deploy/postgres.compose.yml
       etc/caddy/Caddyfile
       usr/local/sbin/ch
       usr/local/sbin/cloudhelm-backup)
for optional in etc/cloudhelm/acs root/.ossutilconfig \
  srv/cloudhelm/cloudhelm-1.0.1/deploy/acs.override.yml \
  etc/systemd/system/docker.service.d/cloudhelm-data.conf; do
  [[ ! -e "/$optional" ]] || items+=("$optional")
done
tar -C / -czf "$config_tar" "${items[@]}"
if gpg --symmetric --cipher-algo AES256 --output "${config_tar}.gpg" "$config_tar"; then
  rm -- "$config_tar"
else
  echo '加密失败，保留本地受保护归档并处理错误'
  exit 1
fi
ossutil cp "${config_tar}.gpg" \
  "oss://YOUR-BACKUP-BUCKET/cloudhelm/daily/config-${config_stamp}.tar.gz.gpg"
```

文件系统权限由归档保存；应用 Secret、私有证书、业务 Compose、NAS 模型清单和额外挂载若在其他路径，加入自己的配置备份流程。此命令没有打包运行中的 PGDATA；数据仍使用第 8.3 节的逻辑备份。

恢复配置时先解密到受保护的临时目录核对，不直接覆盖运行目录：

```bash
install -d -m 0700 /var/backups/cloudhelm-config-review
gpg --output /var/backups/cloudhelm-config-review/config.tar.gz \
  --decrypt /实际下载的/config-实际时间戳.tar.gz.gpg
tar -tzf /var/backups/cloudhelm-config-review/config.tar.gz
```

按归档清单在新管理主机逐项恢复，检查路径、UID/GID 和权限，先恢复数据库后再启动 Server。短期 ACS Token 应重新签发，不能依赖归档中已过期的凭据。

## 9. 恢复演练与故障恢复

### 9.1 独立数据库恢复演练（不切换生产 Server）

先在 OSS 控制台找到同一批次的时间戳目录。日常上传角色没有下载权限：在恢复运维机使用有目标前缀 `oss:GetObject` 权限的恢复身份，按第 8.2 节配置其 ossutil；使用公网 Endpoint 时必须换成公网地址。下载 `cloudhelm.dump` 和 `SHA256SUMS` 到受保护目录，再安全复制到 M 的相同路径；若 M 已按制度临时取得恢复身份，可直接在 M 执行以下命令：

```bash
install -d -m 0700 /var/backups/cloudhelm-restore
cd /var/backups/cloudhelm-restore
ossutil cp oss://YOUR-BACKUP-BUCKET/cloudhelm/daily/实际时间戳/cloudhelm.dump ./cloudhelm.dump
ossutil cp oss://YOUR-BACKUP-BUCKET/cloudhelm/daily/实际时间戳/SHA256SUMS ./SHA256SUMS
sha256sum -c SHA256SUMS
```

以下演练在现有数据库实例中创建**唯一命名的独立数据库**，需要额外磁盘/I/O；大库应在独立 ECS PostgreSQL 17 上执行同样流程。演练数据库保留供核验，不自动删除。

```bash
cd /srv/cloudhelm/cloudhelm-1.0.1
# 确保已按第 3.4 节安装 ch。
restore_db="cloudhelm_restore_$(date -u +%Y%m%d%H%M%S)"
ch exec -T database createdb -U cloudhelm -T template0 "$restore_db"
ch exec -T database pg_restore -U cloudhelm -d "$restore_db" \
  --no-owner --no-acl --exit-on-error \
  < /var/backups/cloudhelm-restore/cloudhelm.dump
ch exec -T database psql -U cloudhelm -d "$restore_db" -v ON_ERROR_STOP=1 <<'SQL'
ANALYZE;
SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;
SELECT count(*) AS nodes FROM nodes;
SELECT count(*) AS users FROM users;
SELECT count(*), min(sampled_at), max(sampled_at) FROM metric_samples;
SQL
printf 'Restored database for verification: %s\n' "$restore_db"
```

核对结果与该备份时间点的记录一致，进一步抽查权限、告警、任务和服务绑定。恢复失败的数据库不要接入 Server；保留日志后使用另一个全新库重试。恢复参数含义见 [PostgreSQL pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)。

若需要启动演练 Server 验证页面，使用独立项目名、端口和受控测试域名，**阻断它到生产 Agent/ACS/业务端点的访问**，关闭通知与自动伸缩，不向生产 Agent 发布这个地址。只关闭自动伸缩不保证恢复库中的发布/任务状态不会影响测试，完整隔离更可靠。

### 9.2 正式恢复切换

1. **先准备目标数据库、已验证的备份、同版本 Server 镜像、配置与独立恢复结果，再安排管理面切换。** 不停止业务容器或释放 GPU。
2. 明确恢复到哪个时间点、备份之后的数据损失和正在进行的任务/发布。停止运维写操作，暂停网关/业务依赖变更；备份仍可读取的当前数据库。灾难期间若曾更新过 Agent 凭据，旧数据库可能不再匹配。
3. 在维护窗口仅执行 `ch stop server`，保留旧数据库和目录。不执行 `down -v`、删除数据目录或用恢复命令覆盖旧库。
4. 目标可以是第 9.1 节已验证的恢复库。将 `deploy/postgres.compose.yml` 中 Server 数据库 URL 的末尾库名 `cloudhelm` 改为实际 `cloudhelm_restore_...`；主机、账号和密码保持与目标相符。**此后备份脚本中 `pg_dump -d` 的库名也必须同步修改。**
5. 使用 `ch config --quiet`、`ch up -d --no-build --no-deps --wait server` 启动唯一 Server。核对当前数据库、迁移版本、登录、权限、Agent 重连、历史以及未完成任务/发布，避免重复执行旧任务。
6. 开放管理入口前核对业务实例实时上报和凭据有效性。如节点认证失败，用对应节点状态备份或 [管理员节点凭据恢复流程](security-hardening.md)处理，不随意清空状态重新注册。
7. 保存恢复用时、恢复点、丢失范围和验收结论，立即执行一次新备份。保留旧库直至回退窗口结束，再独立评估清理。

整机丢失时，在新 ECS 上恢复原 `.env`、Compose 和所需密钥，先只启动空 PostgreSQL、创建目标库并恢复，最后启动 Server、切换 DNS/入口。恢复原 Agent 独立状态即可重新连接，无需重建业务容器。ACS 短期 Token 应重新签发，不能假定备份中的 Token 仍有效。

## 10. 验收与常见问题

### 上线验收

- [ ] PostgreSQL 和 Server 健康，数据库实际为 PostgreSQL 17，迁移到 `0016_wecom_event_receipts`。
- [ ] 5432、8080、Docker API 未公网开放；正式 HTTPS、企微 H5/PC 登录成功，非授权成员不能访问资源。
- [ ] 节点数与实际部署一致，容器/GPU 清单正确，首次采样后历史出现，等待至少两个采样周期验证曲线。
- [ ] `RETENTION_HOURS` 和 `MAX_ROWS` 按节点及容器峰值配套计算；理解 2500 点查询上限。
- [ ] 数据盘开机挂载可靠；记录实际镜像摘要、部署路径、配置和数据库版本。
- [ ] 每日备份上传 OSS 成功，SHA-256 校验及独立恢复演练成功，失败/超时有外部告警。
- [ ] 可选企微事件、ACS/网关功能分别完成验收后再启用；现有业务 GPU 全程保留。
- [ ] ACS 真实镜像完成 GPU 调度、模型加载及有效请求，模拟测试与真实业务结果分开记录。
- [ ] 自动扩容、路由加入、带在途任务排空、路由先摘除、缩容及故障兜底均有时间线。
- [ ] Token 有效期与轮换安排明确，24–48 小时试运行期间未因凭据过期中断。

|现象|排查方向|
|---|---|
|设置保留 30 天，只有不足一天|检查全局 `MAX_ROWS` 和实际上报容器数，使用第 2 节公式。|
|数据库有长期数据，页面看不到|页面 24 小时窗口和 API 2500 点上限；保留与展示范围是两回事。|
|改 `.env` 数据库 URL 不生效|专用 Compose 的 `environment` 覆盖 `.env`；检查实际 Compose 与容器方言。|
|改数据库密码后 Server 连接失败|已有 PGDATA 不会重新初始化密码，必须同步数据库角色密码和 Compose 文件。|
|数据库突然为空|是否在新目录启动相对卷、数据盘未挂载、用了默认 SQLite Compose。立即核对路径并保护原数据。|
|健康接口正常但后台报错|`/healthz` 不探测数据库；执行 SQL 连通性检查并读 Server 日志。|
|磁盘删除历史后不下降|PostgreSQL 内部复用空间，检查 autovacuum、死元组、WAL 和其他持续增长的表。|
|ACS 401/403 或 TLS 错误|Token 过期、SA/RBAC 范围不对、CA 不符或 Server 10001 用户无法读取挂载文件。|
|节点重复注册 409|先核对原节点状态文件；凭据恢复用管理员流程，不复制其他节点的状态。|
|备份手动成功、cron 失败|检查 root 的 PATH、ossutil 凭据/配置、Docker 权限、目录、磁盘和日志。|
