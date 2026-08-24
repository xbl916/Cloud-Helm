# 本地常驻 + 阿里云 ACS 混合云弹性

Cloud Helm 把一个 AI 服务划分为两个实例池：

- `local`：本地常驻实例。它们上报业务数、容量和健康状态，但自动策略永远不会缩掉本地实例。
- `acs`：阿里云容器计算服务（ACS）中的突发实例。平时可以为 0；本地负载持续升高或本地实例故障时启动。

```text
本地 VLM/TTS/ASR ─┐
                  ├─ 每 15 秒上报当前业务数、健康度、是否接单
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

以默认配置为例：单实例上限 10、窗口 300 秒、扩容阈值 70%、缩容阈值 40%。

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

建议先关闭单条策略的“启用自动扩缩容”，确认本地和 ACS 实例均能稳定上报、实例池标记正确，再启用。
永久下线本地实例前，先在实例列表中点“忽略”；否则它停止上报后会被正确地当作本地故障并启动 ACS 兜底。

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
  "container_id": "可选的 Cloud Helm 容器 UUID",
  "metadata": {
    "model": "your-vlm-model",
    "region": "local-dc"
  }
}
```

字段说明：

|字段|含义|
|---|---|
|`instance_id`|实例稳定标识。ACS 中必须使用 Pod 名，通常直接读取 `HOSTNAME`，以便安全缩容时标记目标 Pod。|
|`pool`|本地实例传 `local`，ACS Pod 传 `acs`。|
|`active_businesses`|当前正在承载的业务数。按服务实际语义定义，可以是活跃会话、正在推理的请求、正在合成/识别的任务数，但同一服务必须保持口径一致。|
|`healthy`|模型、依赖和业务处理链路是否健康，不应只表示 HTTP 进程存活。|
|`ready`|模型是否已加载完成并具备处理能力。ACS 冷启动期间传 `false`。|
|`accepting`|是否还接受新业务。收到排空指令后先设为 `false`，并继续上报在途业务数。|
|`container_id`|仅供本地实例兼容旧配置。填管理台中的容器 UUID 可直接关联；配置下述 Docker 标签后可以省略。ACS 实例禁止填写。|

### 本地容器自动关联

推荐给每个本地 AI 容器配置以下 Docker/Compose 标签。`service_id` 就是业务上报 URL 中使用的服务 ID，`instance_id` 必须与应用上报值完全相同：

```yaml
services:
  vlm:
    labels:
      cloudhelm.service_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      cloudhelm.instance_id: "vlm-local-01"
```

Agent 心跳发现该容器后，会自动把它绑定为对应服务的 `local` 实例。应用上报可以不再传平台内部的 `container_id`，而且省略该字段不会清除已经建立的关联。为避免关联不确定，同一服务下的 `instance_id` 必须稳定且唯一；同一次 Agent 心跳中发现重复标签时不会自动绑定。

自动关联只处理由主机 Agent 实际发现的 Docker 容器。ACS Pod 没有主机 Agent，应使用 Pod 名作为 `instance_id` 并传 `pool=acs`；平台禁止 ACS 上报携带本地 `container_id`，也不会因为本地容器误用了相同实例标签而把 ACS 实例关联到主机。

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

服务必须把 HTTP `401` 视为令牌配置错误并告警，不要无限快速重试；网络故障使用指数退避。指标只包含数量和健康状态，不要上传请求内容、音频、图像或用户标识。

## 4. Python 接入示例

下面的类可嵌入现有 VLM/TTS/ASR 服务。`begin()` 和 `end()` 应包围真正占用服务容量的业务生命周期，而不是简单包围 HTTP 收包过程。

```python
import os
import socket
import threading
import time

import httpx


class CloudHelmReporter:
    def __init__(self):
        self.url = (
            os.environ["CLOUDHELM_URL"].rstrip("/")
            + "/api/v1/service-metrics/"
            + os.environ["CLOUDHELM_SERVICE_ID"]
            + "/report"
        )
        self.token = os.environ["CLOUDHELM_SERVICE_TOKEN"]
        self.instance_id = os.getenv("POD_NAME", socket.gethostname())
        self.pool = os.getenv("CLOUDHELM_INSTANCE_POOL", "local")
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
                    }
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

## 5. 上线顺序

1. 先部署支持上报和排空的新业务版本，但保持现有本地版本与 GPU 不变。
2. 确认新版本能完整加载模型、通过健康检查和真实业务验证。
3. 再滚动切换本地实例；不要先停止旧实例或释放 GPU。
4. 创建 ACS 突发工作负载并手动从 0 扩到 1，验证镜像、模型加载、网络和回源依赖后缩回 0。
5. 在 Cloud Helm 中创建策略，先仅监控至少一个完整窗口。
6. 分别演练高负载扩容、本地故障兜底和 ACS 排空缩容，最后启用自动扩缩容。

ACS 以 Kubernetes 为操作界面，Deployment/StatefulSet 的扩缩容通过标准 `scale` 子资源完成。阿里云也支持 HPA、KEDA、AHPA 等方案；本实现适合需要由现有 Cloud Helm 汇总本地与云上业务状态、并执行混合云故障切换的场景。

模块镜像升级与 Compose 版本漂移处理参见[模块镜像版本与分阶段发布](module-image-releases.md)。
