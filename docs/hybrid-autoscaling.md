# 本地常驻 + 阿里云 ACS 混合云弹性

Cloud Helm 把一个 AI 服务划分为两个实例池：

- `local`：本地常驻实例。它们上报业务数、容量和健康状态，但自动策略永远不会缩掉本地实例。
- `acs`：阿里云容器计算服务（ACS）中的突发实例。平时可以为 0；本地负载持续升高或本地实例故障时启动。

```text
本地 VLM/TTS/ASR ─┐
                  ├─ 每 15 秒上报业务数、健康度、是否接单、可达地址
ACS 突发 Pod ─────┘                    │
                                      ▼
                              Cloud Helm Server
                       5 分钟窗口 + 阈值 + 冷却状态机
                                      │
                        Kubernetes scale 子资源 API
                                      ▼
                              阿里云 ACS Deployment
```

## 扩缩容规则

以默认配置为例：新实例默认上限 10、窗口 300 秒、扩容阈值 70%、缩容阈值 40%。

同一服务中的实例可以有不同上限。例如 A10 实例配置为 8，L40S 实例配置为 24；未单独配置的实例继承服务默认值。平台以当前所有健康且接单实例的生效上限之和作为容量分母，不会假定同一服务的显卡性能相同。管理员可在“我的 → AI 服务与弹性 → 对应服务 → 实例容量”中修改覆盖值，清空覆盖值即可恢复继承。

1. Server 每 15 秒对所有有效上报做一次聚合采样。
2. 至少积累窗口的 80% 后才允许按负载伸缩，防止一次尖峰触发扩容。
3. 过去 5 分钟平均业务数 / 同期健康且接单实例容量达到 70% 时，增加 ACS 副本。
4. 任一本地受监控实例明确报告异常、停止接单、关联 Docker 容器异常，或超过上报失效时间后，跳过 5 分钟等待，立即把 ACS 拉到“故障兜底副本数”或缺失本地容量所需副本数中的较大值。
5. 本地全部恢复且平均利用率低于 40% 时，只选择 ACS 实例缩容。平台先在上报响应中返回 `desired_state=drain`；实例停止接新业务并在业务数归零后报告 `accepting=false`，平台才降低 ACS 工作负载副本数。
6. 缩容前平台把已排空 Pod 的 `controller.kubernetes.io/pod-deletion-cost` 设为 `-1000`，让 Kubernetes 优先选择它。该机制是尽力而为，因此应用仍必须配置优雅终止、足够的 `terminationGracePeriodSeconds` 和 `preStop`。

不要同时让 HPA、KEDA 或其他控制器修改同一工作负载的副本数，否则它们会互相覆盖。

## 1. 配置 Server 到 ACS

在 `.env` 中配置：

```dotenv
CLOUDHELM_AUTOSCALING_ENABLED=true
CLOUDHELM_AUTOSCALING_INTERVAL_SECONDS=15
CLOUDHELM_AUTOSCALING_HISTORY_RETENTION_HOURS=24
CLOUDHELM_ACS_KUBERNETES_API_URL=https://your-api-server:6443
CLOUDHELM_ACS_KUBERNETES_BEARER_TOKEN_FILE=/run/secrets/acs-token
CLOUDHELM_ACS_KUBERNETES_CA_FILE=/run/secrets/acs-ca.crt
CLOUDHELM_ACS_KUBERNETES_TIMEOUT_SECONDS=10
```

把 Token 和 CA 文件以只读方式挂载到 Server 容器。不要把 kubeconfig、Token 或 CA 内容保存到策略表或前端。Server 只支持 HTTPS，不提供跳过 TLS 校验的开关。

为专用 ServiceAccount 授予最小权限；把命名空间和资源名替换为实际值：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: cloudhelm-autoscaler
  namespace: inference
rules:
  - apiGroups: ["apps"]
    resources: ["deployments/scale", "statefulsets/scale"]
    resourceNames: ["vlm-burst", "tts-burst", "asr-burst"]
    verbs: ["get", "patch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets"]
    resourceNames: ["vlm-burst", "tts-burst", "asr-burst"]
    verbs: ["get", "patch"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "patch"]
```

如果 ACS API 端点只在 VPC 内可达，需要先打通本地管理中心到该 VPC 的专线、VPN 或其他私网链路。Token 文件应由独立的密钥轮换流程更新。

## 2. 在管理台创建服务

管理员进入“我的 → 本地基座与 ACS 弹性”，添加服务并配置：

- 默认单实例业务上限；已上报实例可以单独覆盖；
- ACS Namespace、Deployment/StatefulSet 和工作负载名；
- ACS 最小/最大副本数，混合云场景通常把最小副本设为 `0`；
- 故障兜底副本数；
- 扩容/缩容阈值、统计窗口和冷却时间。

创建后会显示一次业务上报令牌。令牌只保存在业务服务的 Secret 中；Server 仅保存 SHA-256 摘要。遗失时在管理台轮换，旧令牌立即失效。

创建结果和服务卡片中的 `service_id` 是 Cloud Helm 为这项逻辑服务生成的 UUID。它不是容器 ID、Agent 节点 ID 或模型名称。本地与 ACS 使用同一个业务镜像时，两边使用相同的 `service_id` 和该服务的 `report_token`；`report_token` 只允许写入这一项服务的业务状态，与 Agent 注册令牌、每节点 Agent 令牌、网关发现令牌都不同。

建议先关闭单条策略的“启用自动扩缩容”，确认本地和 ACS 实例均能稳定上报、实例池标记正确，再启用。
永久下线本地实例前，先在实例列表中点“忽略”；否则它停止上报后会被正确地当作本地故障并启动 ACS 兜底。

实例容量页允许删除未关联本地 Docker 容器的错误或失效实例。删除前页面会再次确认，并提示仍在上报的应用会重新创建同一实例；仍关联本地容器的实例必须先修正或移除 `cloudhelm.service_id`、`cloudhelm.instance_id` 标签，平台不会直接删除，以免误移除正常本地容量。删除操作会写入审计日志，镜像分阶段发布期间禁止删除实例。

节点详情的“最近 24 小时”区域同时显示业务负载曲线。Server 按节点历史采样周期汇总该节点已关联实例的当前业务数、健康接单容量和实例数；默认每 5 分钟采样，保留时间和总行数继续由 `CLOUDHELM_METRICS_HISTORY_*` 配置控制。

## 3. 业务上报接口

```http
POST /api/v1/service-metrics/{service_id}/report
X-CloudHelm-Service-Token: {report_token}
Content-Type: application/json

{
  "instance_id": "vlm-local-01",
  "pool": "local",
  "active_businesses": 7,
  "healthy": true,
  "ready": true,
  "accepting": true,
  "endpoint_scheme": "http",
  "endpoint_port": 9000,
  "metadata": {
    "model": "your-vlm-model",
    "region": "local-dc"
  }
}
```

字段说明：

|字段|含义|
|---|---|
|`instance_id`|由部署方注入的实例稳定标识，不需要向 Cloud Helm 查询。本地为每个容器设置固定且唯一的名称；ACS 使用 Pod 名，通常直接读取 `HOSTNAME`，以便安全缩容时标记目标 Pod。|
|`pool`|本地实例传 `local`，ACS Pod 传 `acs`。|
|`active_businesses`|当前正在承载的业务数。按服务实际语义定义，可以是活跃会话、正在推理的请求、正在合成/识别的任务数，但同一服务必须保持口径一致。|
|`healthy`|模型、依赖和业务处理链路是否健康，不应只表示 HTTP 进程存活。|
|`ready`|模型是否已加载完成并具备处理能力。ACS 冷启动期间传 `false`。|
|`accepting`|是否还接受新业务。收到排空指令后先设为 `false`，并继续上报在途业务数。|
|`endpoint_scheme` / `endpoint_port`|要发布给统一网关的协议和服务端口，必须成对出现在每次心跳中。支持 `http`、`https`、`grpc`、`grpcs`。|
|`endpoint_host`|ACS 必填，通过 Downward API 注入 Pod IP。本地可省略：关联容器后优先使用管理平台为所属节点手工配置的“网关访问 IP”；未配置节点 IP时才使用应用上报的 IP。|
|`container_id`|仅供本地实例兼容旧配置。填管理台中的容器 UUID 可直接关联；配置下述 Docker 标签后可以省略。ACS 实例禁止填写。|

### 本地容器自动关联

推荐给每个本地 AI 容器配置以下 Docker/Compose 标签。`service_id` 就是业务上报 URL 中使用的服务 ID，`instance_id` 必须与应用上报值完全相同：

```yaml
services:
  vlm:
    environment:
      CLOUDHELM_URL: "https://ops.company.com"
      CLOUDHELM_SERVICE_ID: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      CLOUDHELM_INSTANCE_ID: "vlm-local-01"
      CLOUDHELM_INSTANCE_POOL: "local"
      CLOUDHELM_ENDPOINT_SCHEME: "http"
      CLOUDHELM_ENDPOINT_PORT: "9000"
      CLOUDHELM_SERVICE_TOKEN_FILE: "/run/secrets/cloudhelm_service_token"
    secrets:
      - cloudhelm_vlm_report_token
    labels:
      cloudhelm.service_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      cloudhelm.instance_id: "vlm-local-01"

secrets:
  cloudhelm_vlm_report_token:
    file: ./secrets/vlm-report-token
```

`CLOUDHELM_SERVICE_ID` 与标签 `cloudhelm.service_id` 使用服务卡片显示的同一个 UUID；`CLOUDHELM_INSTANCE_ID` 与标签 `cloudhelm.instance_id` 使用同一个自定义实例名。Agent 心跳发现该容器后，会自动把它绑定为对应服务的 `local` 实例。应用不需要获取或上报平台内部的 `container_id`。为避免关联不确定，同一服务下的 `instance_id` 必须稳定且唯一；同一次 Agent 心跳中发现重复标签时不会自动绑定。

`secrets/vlm-report-token` 位于宿主机，内容仍是明文；Compose Secret 的作用是只把它挂载给获准服务，而不是加密文件。不要改用 `.env` 注入令牌：环境变量更容易出现在 `docker inspect`、进程环境、诊断输出和日志中。生产环境应把宿主机目录纳入加密磁盘、Vault/SOPS 等 Secret 管理，并禁止提交 Git。

先确认 AI 镜像的运行 UID/GID，再创建令牌文件。下面假设部署账号为 `deploy`，容器输出 `uid=10001 gid=10001`：

```bash
docker compose run --rm --entrypoint id vlm
install -d -m 0700 secrets
install -m 0600 /dev/null secrets/vlm-report-token
editor secrets/vlm-report-token
# 容器以 root 运行时保持 0600；非 root 时让部署账号仍为所有者，
# 并把文件组设为容器的数字 GID，再开放只读组权限。
sudo chown deploy:10001 secrets/vlm-report-token
chmod 0640 secrets/vlm-report-token
docker compose run --rm --entrypoint sh vlm \
  -c 'test -r /run/secrets/cloudhelm_service_token && echo readable'
```

宿主机 `0600 deploy:deploy` 不等于容器内普通用户一定可读，必须以上述实际读取测试为准；启用 rootless Docker、user namespace 或 SELinux 时还要按对应 UID 映射和标签配置。令牌轮换后重新创建容器，使新的 Secret 文件生效。

管理员在“节点详情 → 配置网关访问 IP”中填写统一网关访问这台服务器上业务端口所用的实际 IPv4/IPv6。保存后，该节点所有已关联本地实例的发现地址都会使用这个 IP，各实例仍使用自己上报的端口。地址必须属于 `CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS`；Agent 心跳和容器重启不会覆盖手工配置。

自动关联只处理由主机 Agent 实际发现的 Docker 容器。ACS Pod 没有主机 Agent，应使用 Pod 名作为 `instance_id` 并传 `pool=acs`；平台禁止 ACS 上报携带本地 `container_id`，也不会因为本地容器误用了相同实例标签而把 ACS 实例关联到主机。

同一镜像部署到 ACS 时只需改环境注入，不需要修改程序：

```yaml
env:
  - name: CLOUDHELM_URL
    value: "https://ops.company.com"
  - name: CLOUDHELM_SERVICE_ID
    value: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
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
```

本地与 ACS 再分别把相同服务的令牌挂载到 `CLOUDHELM_SERVICE_TOKEN_FILE` 指定路径；Kubernetes Deployment 还需配置对应 Secret volume 和只读 volumeMount。不要把令牌直接写进 Compose、Deployment YAML、环境变量或镜像。

成功响应：

```json
{
  "accepted": true,
  "service_id": "...",
  "instance_id": "vlm-local-01",
  "container_id": "自动关联后的 Cloud Helm 容器 UUID，未关联时为 null",
  "container_linked": true,
  "desired_state": "serve",
  "report_after_seconds": 15
}
```

`desired_state` 有两个值：

- `serve`：正常接单；
- `drain`：停止接收新业务，等待已有业务完成。不要退出进程，继续每 15 秒上报；只有 `accepting=false` 且 `active_businesses=0` 后平台才会缩 ACS 副本。

服务必须把 HTTP `401` 视为令牌配置错误并告警，不要无限快速重试；网络故障使用指数退避。指标只包含数量、健康状态和路由地址，不要上传请求内容、音频、图像或用户标识。

## 4. 统一网关服务发现

为网关生成一个独立只读令牌并写入 Server 的 `.env`：

```dotenv
CLOUDHELM_GATEWAY_DISCOVERY_TOKEN=使用openssl-rand-hex-32生成的独立随机值
CLOUDHELM_GATEWAY_DISCOVERY_ALLOWED_CIDRS=10.10.0.0/16,10.20.0.0/16
```

允许网段只填写统一网关能够访问的本地服务地址段和 ACS Pod 地址段。实例上报地址必须是其中的 IPv4/IPv6，Cloud Helm 不接受域名或网段外地址，避免业务上报令牌泄露后被用于污染路由。该令牌不得与 Agent 注册令牌或任一服务的业务上报令牌复用。统一网关每 2 秒获取一次权威路由快照：

```http
GET /api/v1/service-discovery/routes
X-CloudHelm-Gateway-Token: {gateway_discovery_token}
If-None-Match: "上一次响应的ETag"
```

有变化时返回 `200`：

```json
{
  "generation": "...",
  "generated_at": "2026-08-24T10:00:00+00:00",
  "authoritative": true,
  "poll_after_seconds": 2,
  "services": [
    {
      "service_id": "...",
      "service_name": "VLM",
      "service_type": "vlm",
      "endpoints": [
        {
          "instance_id": "vlm-acs-6fd8c",
          "pool": "acs",
          "scheme": "http",
          "host": "10.20.0.15",
          "port": 9000,
          "url": "http://10.20.0.15:9000",
          "expires_at": "2026-08-24T10:01:00+00:00"
        }
      ]
    }
  ]
}
```

内容未变化时返回 `304`，网关继续使用当前快照。`services` 是全量权威集合：网关必须整体对账，不能只追加地址；某服务的 `endpoints=[]` 表示该服务当前没有可接单实例，应停止路由并返回明确的服务不可用响应。网关配置应使用不可变的 `service_id` 关联自身的业务路由，`service_name` 只用于显示，不要用名称或 `service_type` 猜测路由关系。

网关同步的核心逻辑应是：携带上次 `ETag` 拉取；收到 `200` 时先校验完整响应，再原子替换整套路由；收到 `304` 时只续用未过期地址；认证失败立即告警且不要清空仍在有效期内的快照；Cloud Helm 不可达时保留旧快照，但逐个按 `expires_at` 淘汰。不要把一次网络超时误当成“所有服务都已删除”。

Cloud Helm 只发布同时满足以下条件的实例：上报未超过该服务的失效时间、受监控、`healthy=true`、`ready=true`、`accepting=true`、`desired_state=serve`，且关联的本地 Docker 容器仍在运行。ACS 新 Pod 完成模型加载并首次健康上报后才加入；开始排空时会先从快照移除，再等待在途业务归零后缩容；实例异常上报会在下一次 2 秒轮询时移除，进程或节点突然失联则最迟在 `report_stale_seconds` 后移除。

网关还应独立遵守每个地址的 `expires_at`：如果暂时无法访问 Cloud Helm，不得无限期保留旧路由。默认失效时间为 60 秒；要求更快故障摘除时可在服务策略中调低到 15–30 秒，并让实例保持约 5–10 秒上报一次。过短会增加瞬时网络抖动导致误摘除的概率。

ACS Pod 推荐通过 Downward API 注入 `status.podIP` 作为 `endpoint_host`。本地容器应上报统一网关能到达的宿主机、macvlan 或容器网络 IP；该地址是否可达取决于实际专线/VPN/VPC 和 Docker 网络设计，Cloud Helm 不会替网关做 NAT。网关仍应启用主动/被动健康检查和短连接失败熔断：真实连接已经失败时直接临时摘除，不必等待下一次控制面快照。

## 5. Python 接入示例

下面的类可嵌入现有 VLM/TTS/ASR 服务。`begin()` 和 `end()` 应包围真正占用服务容量的业务生命周期，而不是简单包围 HTTP 收包过程。

```python
import os
import socket
import threading
import time
from pathlib import Path

import httpx


class CloudHelmReporter:
    def __init__(self):
        self.url = (
            os.environ["CLOUDHELM_URL"].rstrip("/")
            + "/api/v1/service-metrics/"
            + os.environ["CLOUDHELM_SERVICE_ID"]
            + "/report"
        )
        self.token = Path(os.environ["CLOUDHELM_SERVICE_TOKEN_FILE"]).read_text().strip()
        self.instance_id = os.getenv(
            "CLOUDHELM_INSTANCE_ID", os.getenv("POD_NAME", socket.gethostname())
        )
        self.pool = os.getenv("CLOUDHELM_INSTANCE_POOL", "local")
        self.endpoint_scheme = os.getenv("CLOUDHELM_ENDPOINT_SCHEME", "http")
        self.endpoint_host = os.getenv("CLOUDHELM_ENDPOINT_HOST")
        self.endpoint_port = int(os.environ["CLOUDHELM_ENDPOINT_PORT"])
        self.active = 0
        self.accepting = True
        self.lock = threading.Lock()

    def begin(self):
        with self.lock:
            if not self.accepting:
                raise RuntimeError("instance is draining")
            self.active += 1

    def end(self):
        with self.lock:
            self.active = max(0, self.active - 1)

    def run(self):
        with httpx.Client(timeout=5) as client:
            while True:
                with self.lock:
                    payload = {
                        "instance_id": self.instance_id,
                        "pool": self.pool,
                        "active_businesses": self.active,
                        "healthy": True,
                        "ready": True,
                        "accepting": self.accepting,
                        "endpoint_scheme": self.endpoint_scheme,
                        "endpoint_port": self.endpoint_port,
                    }
                if self.endpoint_host:
                    payload["endpoint_host"] = self.endpoint_host
                response = client.post(
                    self.url,
                    headers={"X-CloudHelm-Service-Token": self.token},
                    json=payload,
                )
                response.raise_for_status()
                desired = response.json()["desired_state"]
                with self.lock:
                    self.accepting = desired == "serve"
                time.sleep(15)
```

生产实现还应让业务入口的 readiness 在排空时变为失败，使负载均衡停止转发新请求；健康探针仍保持成功，确保 Pod 有时间完成在途业务。

## 6. 上线顺序

1. 先部署支持上报和排空的新业务版本，但保持现有本地版本与 GPU 不变。
2. 确认新版本能完整加载模型、通过健康检查和真实业务验证。
3. 再滚动切换本地实例；不要先停止旧实例或释放 GPU。
4. 创建 ACS 突发工作负载并手动从 0 扩到 1，验证镜像、模型加载、网络和回源依赖后缩回 0。
5. 配置独立网关发现令牌，验证新增健康实例被加入、异常或排空实例被移除，并验证 Cloud Helm 暂时不可达时网关会按 `expires_at` 淘汰旧地址。
6. 在 Cloud Helm 中创建策略，先仅监控至少一个完整窗口。
7. 分别演练高负载扩容、本地故障兜底和 ACS 排空缩容，最后启用自动扩缩容。

ACS 以 Kubernetes 为操作界面，Deployment/StatefulSet 的扩缩容通过标准 `scale` 子资源完成。阿里云也支持 HPA、KEDA、AHPA 等方案；本实现适合需要由现有 Cloud Helm 汇总本地与云上业务状态、并执行混合云故障切换的场景。

模块镜像升级与 Compose 版本漂移处理参见[模块镜像版本与分阶段发布](module-image-releases.md)。
