# 阿里云 ACS 动态扩缩容完整验证手册

当前 1.0.1 正式部署请使用 [PostgreSQL 与 ACS 完整操作手册](fresh-deploy-postgresql-aliyun-1.0.1.md#6-阿里云-acs-从零部署与真实弹性验收)。本文保留 0.9.7 验证记录；其中 `drain` 状态、默认 Compose 命令和绑定说明不应直接用于当前部署，当前协议使用 `draining` / `maintenance`，PostgreSQL 使用专用 Compose。

本文适用于 Cloud Helm `0.9.7`，目标是在统一业务网关尚未开发完成时，独立验证以下链路：

1. Cloud Helm 根据本地业务负载把阿里云 ACS 工作负载从 `0` 扩到 `1`；
2. ACS GPU Pod 完成模型加载后向 Cloud Helm 上报 Pod IP、协议和端口；
3. 使用 `curl` 模拟网关，确认服务发现接口发布完整、正确的 ACS 地址；
4. 业务负载下降后，Cloud Helm 先要求 ACS Pod 排空并从发现结果移除地址；
5. ACS Pod 报告停止接单且在途业务归零后，工作负载从 `1` 缩到 `0`。

本文使用一个独立测试服务和模拟负载，不停止现有本地 GPU 容器，也不影响生产服务。验证完成前，不要直接在生产服务上打开自动扩缩容。

## 1. 验证范围和暂不验证的内容

本次可以验证：

- Cloud Helm 到 ACS Kubernetes API 的认证、TLS 和 RBAC；
- Deployment/StatefulSet `scale` 子资源扩缩容；
- ACS GPU Pod 镜像拉取、模型加载、健康检查和业务指标上报；
- `pool=acs`、Pod 名、Pod IP、协议、端口是否正确；
- `GET /api/v1/service-discovery/routes` 的全量路由、ETag 和 `304`；
- 排空指令、路由摘除和缩容顺序；
- 扩缩容事件和持续容量超限告警。

由于真实网关尚未开发，本次不验证：

- 网关是否能实际把业务请求转发到 ACS Pod；
- 本地机房到 ACS Pod 网段的专线/VPN/CEN 路由；
- 网关连接失败熔断、重试和负载均衡算法。

即使发现接口返回的 Pod IP 正确，如果未来网关无法路由到该私网 IP，真实业务仍不能使用 ACS 实例。正式上线前必须另行验证“网关到 ACS Pod IP”的双向网络。

## 2. 验证架构

```text
模拟本地实例 ── 每 5 秒上报业务数 ──┐
                                     ▼
                              Cloud Helm Server
                           60 秒测试窗口 + 70/40 阈值
                                     │
                          Kubernetes scale API
                                     ▼
                    ACS Deployment：0 → 1 → 0
                                     │
                       ACS Pod 上报 Pod IP:9000
                                     ▼
curl 模拟网关 ───── GET /service-discovery/routes
```

模拟本地实例只提供负载和容量，不上报地址，所以不会进入路由结果。路由结果中只应出现真正完成上报的 ACS Pod。

## 3. 需要提前记录的参数

部署前填写以下记录表，后续所有命令保持一致：

|参数|示例|实际值|
|---|---|---|
|Cloud Helm 地址|`https://ops.company.com`||
|阿里云地域|`cn-beijing`||
|ACS 集群名|`cloudhelm-acs`||
|VPC CIDR|`10.20.0.0/16`||
|ACS vSwitch ID|`vsw-xxxxxxxx`||
|ACS vSwitch CIDR|`10.20.10.0/24`||
|ACS Pod 安全组|`sg-xxxxxxxx`||
|Kubernetes Namespace|`inference-test`||
|Deployment|`vlm-burst-test`||
|目标容器名|`vlm`||
|业务端口|`9000`||
|ACR 镜像|`registry-vpc.../vlm:test`||
|GPU 型号|`A10`（仅示例）||
|Cloud Helm `service_id`|创建测试服务后获得||
|业务 `report_token`|创建测试服务后获得||
|网关发现 Token|Server `.env` 中配置||
|ACS API 地址|从 kubeconfig 获取||
|NAS 挂载点|采用 NAS 时填写||

## 4. 模型文件是否需要 NAS

NAS 不是使用 ACS 的强制条件。应根据模型大小、冷启动时间和并发 Pod 数选择存储方案。

|方案|是否需要 NAS|优点|缺点|适用情况|
|---|---|---|---|---|
|模型打进容器镜像|否|配置最简单；镜像与模型版本绑定|镜像很大；每个新 Pod 都要拉取；模型更新要重做镜像|首次小规模验证、小模型|
|启动时从 OSS 下载到 `emptyDir`|否|模型版本管理灵活；OSS 成本通常较低|每个 Pod 冷启动都重新下载；临时盘随 Pod 删除|模型只读、可接受下载时间|
|NAS 通过 PVC 只读挂载|是|多个 Pod 共享同一模型；Pod 删除后模型保留；无需重复下载|增加 NAS 成本和挂载配置；性能要实测|大模型、频繁弹性、多副本，推荐|
|CPFS|否，但需另购 CPFS|高吞吐，适合更高性能 AI/HPC|配置和成本更高|NAS 性能不足的大规模场景|

ACS 默认临时存储只有有限容量，Pod 删除后其中内容也会消失。阿里云当前说明 GPU 型 ACS Pod 支持 NAS、CPFS 通用版和 OSS；具体能力参见[ACS 存储概述](https://help.aliyun.com/zh/cs/user-guide/storage-overview)。

本次建议：

- 当前项目模型较大：默认采用 NAS，通过 PVC 只读挂载到所有 ACS Pod；
- 模型已经完整包含在镜像中的临时小模型：可以不申请 NAS，只用于快速走通扩缩容；
- 正式生产频繁从 `0` 启动 GPU Pod：优先验证 NAS 只读共享，或者评估 OSS/NAS 加本地缓存方案。

伸缩阈值只能提前发起扩容，不能消除模型加载耗时。无论使用哪种存储，应用必须在模型完全可用前保持 `ready=false`，否则网关会过早收到一个实际不能处理业务的地址。

### 4.1 200 GiB 模型的 NAS 成本估算

本项目先按中国内地地域、通用型 NAS、标准存储、实际占用 `200 GiB`、持续保存一个月估算。阿里云 NAS 容量计费单位是 `GiB`；用户通常口头所说的“200G”如果实际是十进制 `200 GB`，换算后约为 `186.3 GiB`。为了留出配置、Tokenizer 和后续模型小版本空间，预算按 `200 GiB` 更稳妥。

阿里云官方当前计费示例使用以下参考单价：

|NAS 类型|参考单价|200 GiB 每小时|200 GiB 每天|200 GiB 每 30 天|
|---|---:|---:|---:|---:|
|通用容量型标准存储|`0.35 元/GiB/月`|约 `0.097 元`|约 `2.33 元`|约 `70 元`|
|通用高级型标准存储|`0.85 元/GiB/月`|约 `0.236 元`|约 `5.67 元`|约 `170 元`|
|通用性能型标准存储|`1.85 元/GiB/月`|约 `0.514 元`|约 `12.33 元`|约 `370 元`|

计算公式：

```text
容量型每月：200 GiB × 0.35 元/GiB/月 = 70 元/月
容量型每小时：200 × (0.35 ÷ 30 ÷ 24) ≈ 0.0972 元/小时
```

如果按模型增长预留 10%，实际保存 `220 GiB`：

```text
220 GiB × 0.35 元/GiB/月 ≈ 77 元/月
```

这里的 `70 元/月` 是适合首轮验证的“通用容量型 NAS 标准存储”容量费参考值，不是整个 ACS 方案总价。官方说明通用型 NAS 按每小时实际存储容量峰值结算，并给出了容量型 `0.35 元/GiB/月` 的换算示例；高级型和性能型参考价也以官方定价案例为准。价格可能按地域和活动调整，创建前必须在北京地域购买页或价格计算器再次确认，参见[通用型 NAS 计费](https://help.aliyun.com/zh/nas/product-overview/billing-of-general-purpose-nas-file-systems)。

以下费用不包含在上表中：

- ACS GPU Pod 的 GPU、CPU、内存和临时存储费用；
- API Server 私网 CLB 或公网 EIP；
- NAT 网关、SNAT 和公网流量；
- NAS 回收站、快照/备份、生命周期低频或归档存储费用；
- 低频存储的数据读写流量费用；
- ACR、OSS、SLS 和跨地域数据传输费用；
- 为上传模型临时创建的 ECS。

需要特别注意：ACS 副本缩到 `0` 后 GPU Pod 停止计费，但 NAS 中的 `200 GiB` 模型仍然存在，所以 NAS 仍约按 `70 元/月` 持续计费。通用型 NAS 创建空文件系统本身通常不因容量产生费用，写入模型后才按实际存储量计费；删除 Pod 或 PVC 不等于删除 NAS 中的数据。具体停费规则参见[NAS 计费常见问题](https://help.aliyun.com/zh/nas/product-overview/billing-faq)。

本项目建议先选择通用容量型并实测三个指标：

1. Pod 从创建到 NAS 挂载完成的时间；
2. 从 NAS 加载 200 GiB 模型到 `ready=true` 的总时间；
3. 同时启动两个 Pod 时的加载时间和吞吐变化。

如果容量型加载太慢，再切换高级型、性能型或 CPFS。不要仅为了缩短启动时间直接购买更贵规格，先用真实模型和目标并发做一次基准测试。模型应按不可变版本目录只读挂载，避免多个 Pod 并发修改同一份权重。

## 5. 阿里云侧准备

### 5.1 账号权限和费用保护

使用有权创建 ACS、VPC、vSwitch、安全组、CLB、EIP/NAT、ACR、RAM 服务角色以及可选 NAS 的阿里云账号完成初始化。控制台如果提示创建 ACS/CSI 服务角色，应先核对授权内容再创建。

建议先做以下成本保护：

- 测试 Deployment 的 `maxReplicas` 只设为 `1`；
- Cloud Helm 测试服务最大副本也只设为 `1`；
- 不运行 HPA、KEDA、AHPA 或其他副本控制器；
- 在阿里云费用中心设置预算和余额告警；
- 测试结束确认 Deployment 回到 `0`；
- NAS、NAT 网关、EIP、CLB 和容量预留即使没有 GPU Pod，也可能继续计费。

ACS 集群管理本身与 Pod、网络、存储等资源的计费边界以[阿里云 ACS 计费规则](https://help.aliyun.com/zh/cs/product-overview/product-billing-rules)为准。

#### 5.1.1 首次开通页面如何选择

首次进入 ACS 购买/开通页时可能看到三个产品入口：

|入口|本项目是否开通|作用|
|---|---|---|
|ACS 容器计算服务|是，第一步开通|创建和管理 ACS Kubernetes 集群，是基础服务|
|GPU 容器计算服务|不需要重复开通|开通 ACS 后可直接在集群中创建按量 GPU Pod；页面再次购买可能提示“已达到最大购买量”|
|GPU 容量预留服务|暂时不需要|提前锁定特定地域、可用区和 GPU 规格；即使 Pod 缩到 0，未使用的预留仍会产生预留费用|

按下面顺序操作：

1. 第一次保持“ACS 容器计算服务”选中；
2. 付费模式保持“按量付费”；
3. 确认服务关联角色显示“已创建”，阅读协议后单击“立即购买/开通”；
4. 开通完成后直接进入 ACS 控制台创建集群，不再重复购买“GPU 容器计算服务”；
5. 不选择“GPU 容量预留服务”，先用按量 GPU Pod 完成本手册验证；
6. 在 Deployment Pod 模板中使用 `alibabacloud.com/compute-class: "gpu"` 和实际 GPU 型号申请按量 GPU；
7. 只有测试确认按量库存经常导致 Pod `Pending`，且业务要求确定性扩容时，再单独评估容量预留。

开通 ACS 和实际创建 GPU Pod 是两件事。官方首次使用和 GPU 推理流程都只要求先开通并授权 ACS，然后在 ACS 集群中部署 GPU 工作负载；如果已经开通 ACS 后再次购买“GPU 容器计算服务”提示“已达到最大购买量”，不要继续重复购买。GPU Pod 按秒计费、按小时出账，费用从准备拉取镜像开始到 Pod 停止；测试 Deployment 保持 `replicas: 0` 时不会产生该 Deployment 的 GPU Pod 运行费。具体流程参见[首次使用 ACS](https://help.aliyun.com/zh/cs/getting-started/use-container-computing-for-the-first-time)，公式和卡型价格参见[ACS 计费说明](https://help.aliyun.com/zh/cs/product-overview/product-billing-rules)。

容量预留不是“开启 GPU 功能”的必要条件。按量 GPU Pod 可以直接申请资源；容量预留用于提高稀缺 GPU 的交付确定性，未使用部分也按预留价格计费，参见[GPU Pod 容量预留](https://help.aliyun.com/zh/cs/user-guide/capacity-reservation-egs)。

### 5.2 选择地域、GPU 和可用区

进入：

```text
阿里云控制台
→ 容器服务 Kubernetes 版 ACK
→ ACS
→ GPU 规格或创建集群的资源推荐
```

确认：

1. 目标 GPU 能装下模型并兼容镜像中的 CUDA；
2. 所选地域和可用区支持该 GPU；
3. 当前有按量库存，或者已经购买并关联容量预留；
4. ACR、NAS/OSS 与 ACS 尽量在同一地域。

GPU 型号和可用区库存会变化，不要直接照抄本文的 `A10`。以创建当天控制台和[ACS GPU 支持列表](https://help.aliyun.com/zh/cs/user-guide/gpu/)为准。

### 5.3 规划 VPC、vSwitch 和 Service CIDR

示例规划：

```text
本地机房：              10.10.0.0/16
阿里云 ACS VPC：        10.20.0.0/16
ACS vSwitch A：         10.20.10.0/24
ACS vSwitch B：         10.20.11.0/24
Kubernetes Service CIDR：172.21.0.1/20
```

要求：

- VPC、vSwitch、Service CIDR、本地机房和其他 Kubernetes 集群网段不能重叠；
- Service CIDR 创建后不能修改；
- vSwitch 要预留足够 IP，每个 ACS Pod 会占用私网地址；
- 生产建议选择两个目标 GPU 有库存的可用区；
- 本次单 Pod 验证可以先使用一个可用区。

详细约束参见[ACS 集群网络规划](https://help.aliyun.com/zh/cs/user-guide/acs-cluster-network-planning)。

### 5.4 选择本次测试的网络路径

#### 快速验证方案

网关尚未开发时，可以先采用：

```text
Cloud Helm → ACS API Server 公网 EIP:6443
ACS Pod → NAT 网关/SNAT → Cloud Helm 公网 HTTPS:443
测试人员 → Cloud Helm 服务发现接口
```

该方案不要求本地能够访问 ACS Pod IP，只验证控制面和发现数据。

安全要求：

- API Server 公网访问只允许 Cloud Helm 固定公网出口 IP；
- ACS Pod 只通过 NAT/SNAT主动访问 Cloud Helm；
- 不给业务 Pod 单独绑定公网 EIP；
- 不把业务端口 `9000` 暴露到公网。

#### 正式生产方案

正式网关投入前建议改为：

```text
本地机房 ←→ VPN/专线/CEN ←→ ACS VPC
Cloud Helm → ACS API 私网地址
ACS Pod → Cloud Helm 私网地址
统一网关 → ACS Pod 私网 IP:9000
```

### 5.5 创建 ACS 集群

进入：

```text
阿里云控制台
→ 容器服务 Kubernetes 版 ACK
→ 集群
→ 创建集群
→ ACS 集群
```

建议配置：

1. 填写集群名，例如 `cloudhelm-acs`；
2. 选择目标地域；
3. 选择已有 VPC；
4. 选择目标 GPU 有库存的可用区和 vSwitch；
5. 选择或自动创建专用安全组；
6. 填写不冲突的 Service CIDR；
7. 快速验证需要 ACS Pod 访问公网时，创建或选择 NAT 网关并配置 SNAT；
8. Cloud Helm 无法访问私网 API 时，临时开启 API Server 公网访问；
9. 创建后等待集群状态变为运行中。

ACS 默认通过私网 CLB 提供 API Server；开启公网时会通过 EIP 暴露 `6443`。具体字段参见[创建 ACS 集群](https://help.aliyun.com/zh/cs/user-guide/create-an-acs-cluster)和[控制 API Server 公网访问](https://help.aliyun.com/zh/cs/user-guide/control-the-public-network-access-capability-of-the-cluster-api/)。不要删除集群自动创建的 API Server CLB。

### 5.6 配置 ACS Pod 公网出口

如果 `CLOUDHELM_URL` 是公网域名，ACS Pod 必须能访问公网：

1. 进入专有网络 VPC 控制台；
2. 创建或选择与 ACS 相同 VPC 的公网 NAT 网关；
3. 给 NAT 网关绑定 EIP；
4. 创建 SNAT 条目；
5. SNAT 源网段选择 ACS Pod 使用的每个 vSwitch；
6. 确认 SNAT 条目状态为可用；
7. 安全组出方向允许访问 Cloud Helm 的 `TCP/443`、DNS 和镜像/模型依赖。

仅创建 NAT 网关和 EIP 不够，必须为 ACS vSwitch 创建 SNAT 条目，参见[ACS Pod 访问外部网络](https://help.aliyun.com/zh/cs/user-guide/accessing-the-external-network-in-the-pod)。

### 5.7 配置 Pod 安全组

本次只验证上报和发现数据时，至少允许：

|方向|来源/目标|协议端口|用途|
|---|---|---|---|
|出方向|Cloud Helm 公网或私网地址|TCP/443|业务负载上报|
|出方向|DNS|UDP/TCP 53|域名解析|
|出方向|ACR/OSS/NAS 等依赖|按实际服务|镜像和模型|
|入方向|测试网关网段|TCP/9000|本次可以暂不配置，正式联调时配置|

禁止直接把 `9000` 对公网开放。Deployment 可以通过以下 Annotation 固定 vSwitch 和安全组：

```yaml
spec:
  template:
    metadata:
      annotations:
        network.alibabacloud.com/vswitch-ids: "vsw-xxxxxxxx"
        network.alibabacloud.com/security-group-ids: "sg-xxxxxxxx"
```

指定的 vSwitch 必须已经属于该 ACS 集群。详细规则参见[为 Pod 指定 vSwitch 和安全组](https://help.aliyun.com/zh/cs/user-guide/specify-vswitch-and-securitygroups-for-the-pod)。

### 5.8 准备 ACR 镜像

进入：

```text
阿里云控制台
→ 容器镜像服务 ACR
→ 实例
→ 命名空间
→ 镜像仓库
```

ACR 与 ACS 可以属于不同阿里云账号，但要额外处理网络和认证。推荐顺序是：

1. **推荐：把镜像跨账号同步到 ACS 账号自己的 ACR。** 运行时不依赖跨账号凭据，ACS 继续使用同地域、同账号的 ACR 内网地址；适合生产。ACR 企业版支持跨账号手动或自动同步，具体限制和授权步骤参见[ACR 跨账号同步实例](https://help.aliyun.com/zh/acr/user-guide/synchronize-instances-across-accounts)。
2. **企业版高级方案：使用 ACR 免密组件和 RRSA/角色扮演。** ACR 账号创建只允许拉取目标仓库的 RAM 角色，ACS 账号的集群身份只能扮演该角色；避免长期 Registry 密码。是否可用取决于 ACS 集群版本、免密组件形态和 ACR 实例类型，应先在集群“组件管理”确认。跨账号免密机制参见[ACR 免密拉取镜像](https://help.aliyun.com/zh/acr/user-guide/use-secret-free-components-to-pull-images)。
3. **首次验证最简单：在测试 Namespace 创建 `imagePullSecret`。** ACR 所属账号创建一个专用 RAM 用户，只授予目标仓库的拉取权限并设置 Registry 登录凭据；ACS Deployment 使用这个 Secret。凭据需要定期轮换。

跨账号使用 ACR 的两项前提：

- **网络可达**：不同账号通常也是不同 VPC。若要使用 ACR VPC 内网域名，不仅要打通两个 VPC 和 DNS，还要确认该 ACR 实例允许 ACS 所在 VPC 访问；不能满足时，应同步到 ACS 账号的 ACR，或者通过 ACS NAT/SNAT 访问源 ACR 公网域名。
- **仓库域名完全一致**：`imagePullSecret` 中的 `--docker-server` 必须与 Deployment 镜像地址中的 Registry 主机名完全一致。内网域名和公网域名不能混用。

#### 跨账号 `imagePullSecret` 测试方法

在 ACR 所属账号中：

1. 创建只用于镜像拉取的专用 RAM 用户，不授予无关的控制台和云资源权限；
2. 只授予 `cr:GetAuthorizationToken`、`cr:ListInstanceEndpoint` 和 `cr:PullRepository` 等拉取所需权限；
3. 能按仓库范围配置自定义策略时，不要授予整个账号的 ACR 只读权限；
4. 在 ACR 访问凭证页面生成/设置 Registry 登录密码；
5. 记录控制台显示的完整 Registry 用户名、密码和仓库域名。

ACR 官方提供的 `AliyunContainerRegistryReadOnlyAccess` 可以拉取账号内全部仓库，但权限范围较大；生产更建议使用自定义最小策略，参见[ACR 仓库访问控制](https://help.aliyun.com/zh/acr/user-guide/repository-access-control)。

在连接 ACS 的安全运维机中创建 Secret，下面以公网或已打通的实际仓库域名为准：

```bash
export ACR_REGISTRY='REPLACE_ME_EXACT_ACR_REGISTRY_HOST'
export ACR_USERNAME='REPLACE_ME_ACR_REGISTRY_USERNAME'
read -rsp 'ACR registry password: ' ACR_PASSWORD; echo

kubectl -n inference-test create secret docker-registry acr-cross-account-pull \
  --docker-server="$ACR_REGISTRY" \
  --docker-username="$ACR_USERNAME" \
  --docker-password="$ACR_PASSWORD" \
  --dry-run=client -o yaml |
kubectl apply -f -

unset ACR_PASSWORD
```

Kubernetes Secret 只是 Base64 编码，不是加密保险箱。不要把生成结果、Registry 密码或 RAM AK/SK 写入 Git、Deployment YAML、`.env` 或聊天记录；限制测试 Namespace 的 Secret 读取权限，并定期轮换。

Deployment 中增加：

```yaml
spec:
  template:
    spec:
      imagePullSecrets:
        - name: acr-cross-account-pull
```

直接跨账号拉取只影响 Kubernetes 拉镜像，不改变 Cloud Helm 的扩缩容和镜像发布逻辑。同一服务后续更新 Tag 时，目标镜像仍必须属于同一个 Registry/命名空间/仓库，并保证新 Tag 已存在且跨账号凭据仍可拉取。

无论是否跨账号，镜像要求都是：

- ACR 与 ACS 建议在同一地域，避免跨地域拉取延迟和流量；
- 网络条件满足时优先使用控制台显示的 VPC 内网仓库地址；
- 使用明确 Tag，不使用 `latest`；
- 保存镜像 digest，防止同一 Tag 被覆盖；
- 镜像包含 Cloud Helm Reporter 和排空处理；
- 镜像启动后不能在模型加载完成前返回 Ready。

同账号 ACR 可以按 ACS 当前能力配置免密拉取；跨账号验证则按上述同步、RRSA 或 `imagePullSecret` 三种方式选择。参见[ACS 镜像说明](https://help.aliyun.com/zh/cs/user-guide/mirror/)。

## 6. 为大模型创建并挂载 NAS（本项目推荐必做）

当前项目按约 200 GiB 大模型设计，建议完成本节后再创建 ACS Deployment。只有使用已将模型完整打进镜像的临时测试镜像时，才跳到第 7 节。

### 6.1 创建 NAS

进入：

```text
阿里云控制台
→ 文件存储 NAS
→ 文件系统列表
→ 创建文件系统
```

建议：

1. 地域与 ACS 相同；
2. 协议选择 NFS，不使用 SMB；
3. 首次验证可从通用型开始，吞吐不足再评估极速型或 CPFS；
4. 加密要求在创建文件系统时确定；
5. 创建与 ACS 相同 VPC 的挂载点；
6. 挂载点状态必须为可用；
7. 记录挂载点地址，例如 `xxxx.cn-beijing.nas.aliyuncs.com`。

ACS 的 NAS 挂载只支持相同 VPC，当前使用 NFSv3；同一 VPC 内可以跨可用区。参见[ACS 挂载 NAS](https://help.aliyun.com/zh/nas/user-guide/acs-mount-file-system-of-alibaba-cloud-container-computing-service)。

### 6.2 配置 NAS 权限组

在 NAS 文件系统的挂载使用页面：

1. 创建专用权限组；
2. 只允许 ACS Pod vSwitch CIDR，例如 `10.20.10.0/24`、`10.20.11.0/24`；
3. 模型只读场景优先配置只读权限；
4. 不要直接允许 `0.0.0.0/0`；
5. 将权限组绑定到 NAS 挂载点。

NAS 权限组是挂载白名单。网段未加入权限组时，Pod 可能一直挂载失败，参见[NAS 权限组排查](https://help.aliyun.com/zh/nas/user-guide/other-issues)。

### 6.3 检查 CSI 组件和角色

进入 ACS 集群：

```text
组件管理
→ 存储
→ csi-provisioner
```

确认组件正常。如果控制台要求为 CSI 创建 `AliyunCCCSIPluginRole`，按提示完成授权。不要把该云账号权限授给 Cloud Helm；Cloud Helm 只需要后面定义的 Kubernetes Namespace RBAC。

### 6.4 把模型上传到 NAS

推荐使用同 VPC 的临时 ECS 挂载 NAS，然后把模型上传到例如：

```text
/models/vlm/model-version-202608/
```

上传后：

- 校验模型文件数量和 SHA-256；
- 记录不可变的模型版本目录；
- 不让多个 Pod同时修改模型目录；
- 测试完成后可释放临时上传 ECS，但不要删除 NAS 或挂载点。

### 6.5 创建静态 PV 和 PVC

把下列 NAS 地址和路径替换为实际值：

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: cloudhelm-vlm-model-nas-pv
spec:
  capacity:
    storage: 220Gi
  accessModes:
    - ReadOnlyMany
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  csi:
    driver: nasplugin.csi.alibabacloud.com
    volumeHandle: cloudhelm-vlm-model-nas-pv
    volumeAttributes:
      server: "REPLACE_ME_NAS_MOUNT_TARGET"
      path: "/models/vlm/model-version-202608"
  mountOptions:
    - nolock,tcp,noresvport
    - vers=3
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cloudhelm-vlm-model-nas-pvc
  namespace: inference-test
spec:
  accessModes:
    - ReadOnlyMany
  storageClassName: ""
  volumeName: cloudhelm-vlm-model-nas-pv
  resources:
    requests:
      storage: 220Gi
```

如果测试 Namespace 尚未创建，先创建；然后应用并检查：

```bash
kubectl get namespace inference-test >/dev/null 2>&1 || \
  kubectl create namespace inference-test
kubectl apply -f vlm-model-nas.yaml
kubectl get pv cloudhelm-vlm-model-nas-pv
kubectl -n inference-test get pvc cloudhelm-vlm-model-nas-pvc
```

PV 和 PVC 都应为 `Bound`。`220Gi` 是按 200 GiB 模型加 10% 余量填写的 Kubernetes 声明值，实际可用容量和计费仍由 NAS 中真正存储的数据及文件系统规格决定。

Deployment 中增加：

```yaml
containers:
  - name: vlm
    volumeMounts:
      - name: model
        mountPath: /models
        readOnly: true
volumes:
  - name: model
    persistentVolumeClaim:
      claimName: cloudhelm-vlm-model-nas-pvc
```

不要为了挂载 NAS 随意配置 `securityContext.fsGroup`，这可能显著增加挂载时间，部分配置还可能挂载失败。完整字段参见[ACS NAS 静态存储卷](https://help.aliyun.com/zh/cs/user-guide/mount-statically-provisioned-nas-volumes)。

## 7. 初始化 Kubernetes 资源

### 7.1 获取管理员 kubeconfig

在 ACS 集群详情中下载 kubeconfig，只放在安全的初始化运维机：

```bash
export KUBECONFIG=/secure/path/acs-admin-kubeconfig
kubectl cluster-info
kubectl get namespaces
```

管理员 kubeconfig 只用于创建 Namespace、RBAC、Secret、PV/PVC 和 Deployment。不要把完整管理员 kubeconfig 挂载给 Cloud Helm。参见[阿里云 kubeconfig 操作指南](https://help.aliyun.com/zh/cs/user-guide/kubeconfig-operation-guide)。

### 7.2 创建测试 Namespace

```bash
kubectl get namespace inference-test >/dev/null 2>&1 || \
  kubectl create namespace inference-test
```

独立 Namespace 可以限制 Cloud Helm 的 Pod `patch` 权限只影响测试资源。

### 7.3 创建 Cloud Helm 最小 RBAC

这里的 ServiceAccount 用于：

```text
Cloud Helm Server → ACS Kubernetes API
```

它不是 AI 容器上报业务指标使用的 Token。

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
    resources: ["deployments/scale"]
    resourceNames: ["vlm-burst-test"]
    verbs: ["get", "patch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
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

应用：

```bash
kubectl apply -f cloudhelm-acs-rbac.yaml
```

### 7.4 创建并导出 ACS API Token

当前 Cloud Helm 使用文件形式的 Bearer Token。为了首次验证，可以为最小权限 ServiceAccount 创建 Token Secret：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudhelm-autoscaler-token
  namespace: inference-test
  annotations:
    kubernetes.io/service-account.name: cloudhelm-autoscaler
type: kubernetes.io/service-account-token
```

```bash
kubectl apply -f cloudhelm-acs-token.yaml
umask 077

kubectl -n inference-test get secret cloudhelm-autoscaler-token \
  -o jsonpath='{.data.token}' | base64 -d > acs-token

kubectl -n inference-test get configmap kube-root-ca.crt \
  -o jsonpath='{.data.ca\.crt}' > acs-ca.crt

kubectl config view --minify --raw \
  -o jsonpath='{.clusters[0].cluster.server}'; echo

chmod 0600 acs-token
chmod 0644 acs-ca.crt
```

长期 Token 泄露后的风险高于短期 Token。生产应建立轮换流程；Cloud Helm 会按评估周期重新读取 Token 文件，因此可以通过同路径原子替换完成轮换。Kubernetes 官方建议优先使用短期 TokenRequest，参见[ServiceAccount 管理](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)。

### 7.5 验证 RBAC

```bash
export ACS_API='https://REPLACE_ME_ACS_API:6443'
export ACS_TOKEN="$(<acs-token)"

kubectl \
  --server="$ACS_API" \
  --certificate-authority=acs-ca.crt \
  --token="$ACS_TOKEN" \
  auth can-i get deployment/vlm-burst-test \
  --subresource=scale -n inference-test

kubectl \
  --server="$ACS_API" \
  --certificate-authority=acs-ca.crt \
  --token="$ACS_TOKEN" \
  auth can-i patch deployment/vlm-burst-test \
  --subresource=scale -n inference-test

kubectl \
  --server="$ACS_API" \
  --certificate-authority=acs-ca.crt \
  --token="$ACS_TOKEN" \
  auth can-i patch pod -n inference-test
```

前面三项应返回 `yes`。删除 Deployment 应返回 `no`：

```bash
kubectl \
  --server="$ACS_API" \
  --certificate-authority=acs-ca.crt \
  --token="$ACS_TOKEN" \
  auth can-i delete deployment -n inference-test
```

## 8. 配置 Cloud Helm Server

### 8.1 放置 ACS 凭据

在 Cloud Helm 发布目录中：

```bash
install -d -m 0700 secrets
install -m 0600 acs-token secrets/acs-token
install -m 0644 acs-ca.crt secrets/acs-ca.crt
docker compose run --rm --entrypoint id server
```

如果 Server 以 `uid=10001 gid=10001` 运行：

```bash
sudo chown root:10001 secrets/acs-token
sudo chmod 0640 secrets/acs-token
sudo chown root:10001 secrets/acs-ca.crt
sudo chmod 0644 secrets/acs-ca.crt
```

### 8.2 修改 Compose 挂载

在 `server.volumes` 下增加：

```yaml
services:
  server:
    volumes:
      - ./cloudhelm-data:/data
      - ./secrets/acs-token:/run/secrets/acs-token:ro
      - ./secrets/acs-ca.crt:/run/secrets/acs-ca.crt:ro
```

### 8.3 配置 `.env`

第一阶段保持全局自动化关闭：

```dotenv
CLOUDHELM_AUTOSCALING_ENABLED=false
CLOUDHELM_AUTOSCALING_INTERVAL_SECONDS=15
CLOUDHELM_AUTOSCALING_HISTORY_RETENTION_HOURS=24

CLOUDHELM_ACS_KUBERNETES_API_URL=https://REPLACE_ME_ACS_API:6443
CLOUDHELM_ACS_KUBERNETES_BEARER_TOKEN_FILE=/run/secrets/acs-token
CLOUDHELM_ACS_KUBERNETES_CA_FILE=/run/secrets/acs-ca.crt
CLOUDHELM_ACS_KUBERNETES_TIMEOUT_SECONDS=10

CLOUDHELM_GATEWAY_DISCOVERY_TOKEN=REPLACE_ME_INDEPENDENT_RANDOM_TOKEN
CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS=10.20.0.0/16
```

发现允许网段必须覆盖 ACS Pod 的实际 vSwitch CIDR。可以填写更精确的 `10.20.10.0/24,10.20.11.0/24`，不要为了省事直接允许所有地址。

重建并验证：

```bash
docker compose up -d --force-recreate server
docker compose exec server sh -c '
  test -r /run/secrets/acs-token &&
  test -r /run/secrets/acs-ca.crt &&
  echo readable
'
docker compose logs --tail=100 server
```

## 9. 在 Cloud Helm 创建独立测试服务

进入：

```text
我的
→ 本地基座与 ACS 弹性
→ 添加服务
```

使用以下测试值：

|配置|测试值|生产建议|
|---|---:|---:|
|服务名|`ACS动态扩缩容测试`|实际服务名|
|类型|VLM|实际类型|
|默认单实例容量|`10`|ACS 单 Pod 实测容量|
|自动扩缩容|关闭|全部验证后开启|
|最小副本|`0`|混合云一般为 `0`|
|最大副本|`1`|按预算和容量规划|
|扩容阈值|`70%`|通常 `70%`|
|缩容阈值|`40%`|通常 `40%`|
|统计窗口|`60秒`|通常 `300秒`|
|冷却时间|`30秒`|建议 `300～600秒`|
|上报失效时间|`30秒`|建议 `30～60秒`|
|单次扩容|`1`|根据冷启动和流量|
|故障兜底副本|`1`|按可用性要求|
|Namespace|`inference-test`|实际 Namespace|
|工作负载类型|Deployment|实际类型|
|工作负载名|`vlm-burst-test`|实际名称|
|ACS 目标容器名|`vlm`|实际容器名|

创建后立即保存：

```text
service_id
report_token
```

`report_token` 只显示一次。它只允许向该测试服务上报业务状态，与 Agent Token、ACS Kubernetes Token、网关发现 Token 均不同。

## 10. 创建业务上报 Secret 和 ACS Deployment

### 10.1 创建业务上报 Secret

```bash
umask 077
install -m 0600 /dev/null vlm-test-report-token
editor vlm-test-report-token

kubectl -n inference-test create secret generic cloudhelm-vlm-test-report-token \
  --from-file=token=./vlm-test-report-token

rm -f vlm-test-report-token
```

文件内容只放测试服务的 `report_token`，末尾换行可以存在，应用读取时应执行 `strip()`。

### 10.2 Deployment 模板

以下模板假设模型在镜像中。使用 NAS 时按第 6.5 节增加 PVC 挂载。替换镜像、GPU、vSwitch、安全组、Cloud Helm 地址和 `service_id`：

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
        alibabacloud.com/gpu-model-series: "A10"
      annotations:
        network.alibabacloud.com/vswitch-ids: "REPLACE_ME_VSWITCH_ID"
        network.alibabacloud.com/security-group-ids: "REPLACE_ME_SECURITY_GROUP_ID"
    spec:
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 300
      # 跨账号直接拉取时保留；镜像已同步到 ACS 同账号并免密拉取时删除。
      imagePullSecrets:
        - name: acr-cross-account-pull
      containers:
        - name: vlm
          image: REPLACE_ME_ACR_VPC_IMAGE_WITH_TAG
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 9000
          env:
            - name: CLOUDHELM_URL
              value: "https://REPLACE_ME_CLOUDHELM_HOST"
            - name: CLOUDHELM_SERVICE_ID
              value: "REPLACE_ME_SERVICE_ID"
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
              value: "/run/secrets/cloudhelm_service_token"
          volumeMounts:
            - name: cloudhelm-report-token
              mountPath: /run/secrets/cloudhelm_service_token
              subPath: token
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
          readinessProbe:
            httpGet:
              path: /ready
              port: 9000
            initialDelaySeconds: 10
            periodSeconds: 5
            failureThreshold: 120
          livenessProbe:
            httpGet:
              path: /health
              port: 9000
            initialDelaySeconds: 120
            periodSeconds: 15
            failureThreshold: 4
      volumes:
        - name: cloudhelm-report-token
          secret:
            secretName: cloudhelm-vlm-test-report-token
            defaultMode: 0400
```

注意：

- `A10` 只是示例，必须换成所选可用区当天支持的 GPU；
- ACS GPU 资源标签和 CPU/内存/GPU 的 requests/limits 应按实际规格填写；
- 不要把 ACS 系统标签放进 Deployment `selector.matchLabels`；
- readiness 必须表示模型和依赖已完整就绪，不只是 HTTP 进程启动；
- liveness 不应因为收到排空指令而失败；
- 应用仍需处理 SIGTERM，并设置足够的优雅终止时间；
- 多容器 Pod 必须保证 Cloud Helm 服务配置中的目标容器名为 `vlm`。

应用并确认初始为 0：

```bash
kubectl apply -f vlm-burst-test.yaml
kubectl -n inference-test get deployment vlm-burst-test
kubectl -n inference-test get deployment vlm-burst-test \
  -o jsonpath='{.spec.replicas}{"\n"}'
```

结果应为 `0`。

## 11. ACS 应用必须实现的上报和排空契约

ACS Pod 每约 5～15 秒调用：

```http
POST /api/v1/service-metrics/{service_id}/report
X-CloudHelm-Service-Token: {report_token}
Content-Type: application/json
```

请求示例：

```json
{
  "instance_id": "vlm-burst-test-7b8c9d-abcde",
  "pool": "acs",
  "active_businesses": 0,
  "healthy": true,
  "ready": true,
  "accepting": true,
  "endpoint_scheme": "http",
  "endpoint_host": "10.20.10.35",
  "endpoint_port": 9000,
  "metadata": {
    "model": "model-version-202608",
    "region": "cn-beijing"
  }
}
```

ACS 上报不能携带 `container_id`。响应中的：

```json
{
  "desired_state": "serve",
  "endpoint_registered": true,
  "report_after_seconds": 15
}
```

有两种状态：

- `serve`：正常接单；
- `drain`：立即停止接新业务，但进程继续运行和上报。

收到 `drain` 后应用必须：

1. 拒绝或转移新业务；
2. 下一次上报 `accepting=false`；
3. 继续完成已有任务；
4. 业务数归零后上报 `active_businesses=0`；
5. 不主动退出，等待 Kubernetes 缩容；
6. 排空期间 readiness 可以失败以阻止新流量，但 liveness 必须保持成功。

如果应用不处理 `drain`，Cloud Helm 会停留在等待排空状态，不会安全缩容。

## 12. 在启用自动化前手工验证一个 ACS Pod

此时全局和服务自动扩缩容都保持关闭。手工从 0 扩到 1：

```bash
kubectl -n inference-test scale deployment vlm-burst-test --replicas=1
kubectl -n inference-test rollout status deployment vlm-burst-test --timeout=30m
kubectl -n inference-test get pods -l app=vlm-burst-test -o wide
kubectl -n inference-test logs deployment/vlm-burst-test --tail=200
```

确认：

- GPU Pod 能调度，不是长时间 `Pending`；
- ACR 镜像拉取成功；
- NAS/PVC 使用时为 `Bound` 且挂载成功；
- 模型文件存在且可读；
- CUDA/GPU 初始化成功；
- 模型完成前 readiness 为失败；
- 模型完成后 readiness 通过；
- 应用成功调用 Cloud Helm 上报接口；
- 平台实例列表出现 `pool=acs`，没有绑定本地 Agent。

测试 Pod 无真实业务时缩回 0：

```bash
kubectl -n inference-test scale deployment vlm-burst-test --replicas=0
```

手工 `kubectl scale` 不经过 Cloud Helm 排空，只能在确认没有业务的测试阶段执行。

## 13. 用 curl 模拟网关检查初始路由

测试机安装 `curl` 和 `jq`，然后设置非敏感参数：

```bash
export CLOUDHELM_URL='https://REPLACE_ME_CLOUDHELM_HOST'
export SERVICE_ID='REPLACE_ME_SERVICE_ID'
read -rsp 'Gateway discovery token: ' GATEWAY_TOKEN; echo
export GATEWAY_TOKEN
```

调用发现接口：

```bash
curl -fsS \
  -H "X-CloudHelm-Gateway-Token: ${GATEWAY_TOKEN}" \
  "${CLOUDHELM_URL}/api/v1/service-discovery/routes" |
jq --arg sid "$SERVICE_ID" '
  .services[] | select(.service_id == $sid)
'
```

初始应为：

```json
{
  "service_id": "测试服务 UUID",
  "service_name": "ACS动态扩缩容测试",
  "service_type": "vlm",
  "endpoints": []
}
```

错误含义：

- `503`：Server 没配置网关发现 Token；
- `401`：请求 Token 与 Server 不一致；
- 找不到服务：`service_id` 错误或账号创建的是另一项服务。

## 14. 启动模拟本地负载

ACS 当前为 0，没有 Pod 能产生负载，因此使用一个持续上报但不发布地址的模拟本地实例。它不需要 Agent 和 Docker 容器。

```bash
read -rsp 'Service report token: ' REPORT_TOKEN; echo
export REPORT_TOKEN
export TEST_LOAD=8
```

持续上报：

```bash
while true; do
  date
  curl -fsS \
    -X POST \
    -H "X-CloudHelm-Service-Token: ${REPORT_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "{
      \"instance_id\": \"autoscaling-load-generator\",
      \"pool\": \"local\",
      \"active_businesses\": ${TEST_LOAD},
      \"healthy\": true,
      \"ready\": true,
      \"accepting\": true,
      \"metadata\": {
        \"purpose\": \"acs-autoscaling-test\"
      }
    }" \
    "${CLOUDHELM_URL}/api/v1/service-metrics/${SERVICE_ID}/report"
  echo
  sleep 5
done
```

这个实例继承默认容量 `10`，上报业务数 `8`，初始利用率为 `80%`，超过 `70%` 扩容阈值。它没有上报协议、IP 和端口，所以不会出现在网关发现接口。

保持该循环运行。停止超过 `report_stale_seconds` 后，平台会把它判断为本地故障并触发故障兜底，而不是本次要验证的负载扩容。

## 15. 启用自动扩容

### 15.1 打开全局开关

确认第 12 节全部通过后，把 `.env` 改为：

```dotenv
CLOUDHELM_AUTOSCALING_ENABLED=true
```

重建 Server：

```bash
docker compose up -d --force-recreate server
docker compose logs --tail=100 server
```

### 15.2 打开测试服务开关

在测试服务配置中开启“自动扩缩容”。不要打开任何生产服务。

### 15.3 观察扩容

测试窗口为 60 秒，但平台至少需要积累窗口的 80%，所以开始看到 `collecting` 属于正常。另开终端：

```bash
watch -n 2 '
kubectl -n inference-test get deployment vlm-burst-test
kubectl -n inference-test get pods -l app=vlm-burst-test -o wide
'
```

预期：

```text
0 副本
→ collecting
→ 平均利用率达到 70%
→ scaling_up
→ Deployment 1 副本
→ Pod Pending/ContainerCreating
→ 模型加载
→ Pod Ready
→ ACS 首次健康上报
```

核对副本：

```bash
kubectl -n inference-test get deployment vlm-burst-test \
  -o jsonpath='{.spec.replicas}{" / "}{.status.readyReplicas}{"\n"}'
```

最终应为：

```text
1 / 1
```

## 16. 验证 ACS IP 和端口发布正确

持续查看服务发现结果：

```bash
watch -n 2 "
curl -fsS \
  -H 'X-CloudHelm-Gateway-Token: ${GATEWAY_TOKEN}' \
  '${CLOUDHELM_URL}/api/v1/service-discovery/routes' |
jq --arg sid '${SERVICE_ID}' '
  .services[] | select(.service_id == \$sid)
'
"
```

ACS Pod Ready 并成功上报后应出现：

```json
{
  "service_id": "测试服务 UUID",
  "service_name": "ACS动态扩缩容测试",
  "service_type": "vlm",
  "endpoints": [
    {
      "instance_id": "vlm-burst-test-xxxxxxxxxx-xxxxx",
      "pool": "acs",
      "scheme": "http",
      "host": "10.20.10.35",
      "port": 9000,
      "url": "http://10.20.10.35:9000",
      "expires_at": "时间"
    }
  ]
}
```

从 Kubernetes 取实际值：

```bash
ACS_POD="$(kubectl -n inference-test get pod \
  -l app=vlm-burst-test \
  -o jsonpath='{.items[0].metadata.name}')"

ACS_POD_IP="$(kubectl -n inference-test get pod "$ACS_POD" \
  -o jsonpath='{.status.podIP}')"

printf 'Pod name: %s\nPod IP:   %s\n' "$ACS_POD" "$ACS_POD_IP"
```

逐项核对：

|发现字段|必须等于|
|---|---|
|`instance_id`|Kubernetes Pod 名|
|`pool`|`acs`|
|`host`|`status.podIP`|
|`port`|业务实际监听端口 `9000`|
|`scheme`|实际协议 `http/https/grpc/grpcs`|
|`url`|协议、Pod IP 和端口拼接结果|

模拟本地实例 `autoscaling-load-generator` 不应出现在 `endpoints` 中。

Pod 已 Ready 但地址没有出现时：

```bash
kubectl -n inference-test logs "$ACS_POD" --tail=300
```

检查：

- 上报 `401`：业务 `report_token` 错误；
- 上报 `422`：ACS Pod IP 不在 `CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS`；
- 无连接：Pod 到 Cloud Helm `443` 不通，检查 DNS、NAT/SNAT、安全组；
- `ready=false`：模型尚未加载；
- `accepting=false`：应用没有进入接单状态；
- `endpoint_registered=false`：协议、IP 或端口缺失。

## 17. 验证 ETag 和 304

```bash
curl -fsS -D /tmp/cloudhelm-route-headers \
  -o /tmp/cloudhelm-routes.json \
  -H "X-CloudHelm-Gateway-Token: ${GATEWAY_TOKEN}" \
  "${CLOUDHELM_URL}/api/v1/service-discovery/routes"

jq . /tmp/cloudhelm-routes.json

ETAG="$(
  awk 'BEGIN {IGNORECASE=1}
       /^etag:/ {
         sub(/\r$/, "", $2)
         print $2
       }' /tmp/cloudhelm-route-headers
)"

curl -i \
  -H "X-CloudHelm-Gateway-Token: ${GATEWAY_TOKEN}" \
  -H "If-None-Match: ${ETAG}" \
  "${CLOUDHELM_URL}/api/v1/service-discovery/routes"
```

路由未变化时应返回：

```text
HTTP/1.1 304 Not Modified
```

未来网关应每约 2 秒携带上一次 ETag 拉取；收到 `200` 时原子替换全量路由，收到 `304` 时继续使用未过期地址。

## 18. 把负载降为 0 并验证缩容

在模拟负载终端按 `Ctrl+C`，立即改为：

```bash
export TEST_LOAD=0
```

重新运行第 14 节相同循环。必须继续上报：

```json
{
  "active_businesses": 0,
  "healthy": true,
  "ready": true,
  "accepting": true
}
```

不要停止模拟实例，也不要把它改成 unhealthy，否则会进入本地故障兜底，ACS 不会按低负载缩回去。

继续同时观察：

```bash
watch -n 2 '
kubectl -n inference-test get deployment vlm-burst-test
kubectl -n inference-test get pods -l app=vlm-burst-test -o wide
'
```

和：

```bash
watch -n 1 "
curl -fsS \
  -H 'X-CloudHelm-Gateway-Token: ${GATEWAY_TOKEN}' \
  '${CLOUDHELM_URL}/api/v1/service-discovery/routes' |
jq --arg sid '${SERVICE_ID}' '
  .services[] | select(.service_id == \$sid) | .endpoints
'
"
```

正确顺序：

```text
平均利用率降至 40% 以下
→ Cloud Helm 选择一个 ACS Pod
→ 上报响应 desired_state=drain
→ 该 Pod 停止接单
→ 发现接口立即移除该 Pod 地址
→ Pod 继续处理在途业务
→ accepting=false 且 active_businesses=0
→ Cloud Helm 标记 Pod 删除优先级
→ Deployment 1 → 0
→ Pod 终止
```

最重要的验收点是：路由地址先移除，Pod 后删除。

如果停留在 `draining` 或 `scale_down_waiting_for_reports`：

- 检查应用是否读取上报响应中的 `desired_state`；
- 检查收到 `drain` 后是否仍错误报告 `accepting=true`；
- 检查 `active_businesses` 是否始终大于 0；
- 检查应用是否在排空过程中直接退出，导致最后一次归零上报未发送；
- 检查 Cloud Helm 是否有 Pod `patch` 和 Deployment `scale` 权限。

## 19. 可选：验证本地故障立即兜底

负载扩缩容验证完成并回到 0 后，可以在同一测试服务验证故障兜底。仍使用模拟实例，把上报临时改为：

```json
{
  "instance_id": "autoscaling-load-generator",
  "pool": "local",
  "active_businesses": 0,
  "healthy": false,
  "ready": false,
  "accepting": false
}
```

本地实例明确异常不等待完整统计窗口，平台应把 ACS 扩到故障兜底副本数。恢复时重新持续上报：

```json
{
  "healthy": true,
  "ready": true,
  "accepting": true,
  "active_businesses": 0
}
```

恢复完整窗口并满足冷却条件后，ACS 应再次排空缩到 0。不要通过停止生产 GPU 容器验证故障兜底。

## 20. 完整验收表

|编号|检查项|预期结果|实际结果|
|---|---|---|---|
|1|ACS API TLS|CA 校验通过||
|2|ACS API Token|认证成功||
|3|RBAC scale|get/patch 为 yes||
|4|RBAC 删除 Deployment|为 no||
|5|Deployment 初始副本|0||
|6|模拟负载|8/10，约 80%||
|7|统计窗口|收集后触发扩容||
|8|Deployment 扩容|0 → 1||
|9|GPU 调度|Pod 成功运行||
|10|镜像拉取|成功||
|11|模型存储|镜像/OSS/NAS 可读||
|12|模型 Ready|加载完成前不 Ready||
|13|ACS 上报|`pool=acs`||
|14|实例 ID|等于 Pod 名||
|15|发现 host|等于 Pod IP||
|16|发现端口|等于业务端口||
|17|模拟实例路由|不出现在 endpoints||
|18|ETag|返回 ETag||
|19|无变化请求|返回 304||
|20|低负载|平均利用率低于 40%||
|21|排空指令|ACS 收到 drain||
|22|路由摘除|Pod 删除前 endpoints 已移除||
|23|业务归零|accepting=false、active=0||
|24|Deployment 缩容|1 → 0||
|25|最终发现结果|endpoints=[]||

## 21. 常见问题定位

|现象|原因和检查方向|
|---|---|
|Cloud Helm 显示 `provider_error`|ACS API 地址、CA、Token、EIP 白名单或私网链路|
|ACS API 返回 `401`|ServiceAccount Token 错误、已轮换或文件包含错误内容|
|ACS API 返回 `403`|Role/RoleBinding 缺少 scale 或 Pod patch|
|副本变为 1，但 Pod Pending|GPU 型号或可用区库存、资源规格、vSwitch IP、PVC|
|`ImagePullBackOff`|ACR 内网地址、免密配置、imagePullSecret|
|`FailedMount`|NAS VPC、挂载点、权限组、CSI、PV/PVC、NFSv3|
|Pod 无法访问 Cloud Helm|NAT/SNAT、DNS、安全组、NetworkPolicy、TLS|
|上报返回 `401`|用了 ACS Token 或 Agent Token，而不是服务 report_token|
|上报返回 `422`|Pod IP 不在发现允许 CIDR，或字段组合错误|
|Pod Ready 但 endpoints 为空|没有上报 endpoint、状态不是健康接单、上报已失效|
|持续扩容到最大值|负载口径错误、容量过小、ACS Pod 未 Ready/未上报|
|一直不缩容|本地模拟实例失效触发故障兜底，或 ACS 未执行 drain|
|路由未及时移除|ACS 仍报告接受业务，或调用的不是同一个 Cloud Helm 实例|
|缩容删除了错误 Pod|存在其他副本控制器或调度策略干预 Pod 删除优先级|

## 22. 测试结束和生产化

验证完成后：

1. 确认测试 Deployment 已回到 `0`；
2. 关闭测试服务自动扩缩容；
3. 停止模拟负载循环；
4. 在平台删除未关联容器的模拟实例；
5. 保留测试服务用于后续网关联调，或确认无用后删除；
6. 删除不再使用的 API Server 公网 EIP、NAT/EIP、NAS 等资源前，先确认没有其他业务依赖；
7. 把统计窗口从 `60` 恢复为生产建议的 `300` 秒；
8. 把冷却时间恢复为 `300～600` 秒；
9. 根据目标 GPU 实测结果设置 ACS 默认单实例容量；
10. 为不同本地显卡实例配置各自容量覆盖值；
11. 建立 ACS ServiceAccount Token 轮换和审计；
12. 网关开发完成后，再验证网关到 ACS Pod 私网 IP 的真实请求链路。

正式服务不要同时使用 Cloud Helm 和 HPA/KEDA/AHPA 控制同一个 Deployment 副本数。镜像升级时先准备并验证新版本，再执行分阶段切换，不要先停止本地旧版本或释放 GPU。

更完整的运行原理、接口和网关对账契约参见[本地常驻 + 阿里云 ACS 混合云弹性](hybrid-autoscaling.md)。
