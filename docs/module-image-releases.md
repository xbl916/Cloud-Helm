# 模块镜像版本与分阶段发布

## 当前两种更新方式

Cloud Helm 保留原有的单容器“更换 Tag”操作。它会先拉取镜像并校验 Compose，再停止旧容器、原位创建新容器；启动或 Docker 健康检查失败时自动恢复旧容器和 Compose。

混合云模块应使用“模块期望镜像”发布。期望镜像同时作用于：

- 模块下所有已关联的本地 `local` 服务实例；
- 对应 ACS Deployment 或 StatefulSet 的目标容器；
- 未来由弹性策略新创建的 ACS Pod；
- 设备白名单目录内对应 Compose service 的镜像 Tag。

Agent 只获得管理员明确挂载并通过 `CLOUDHELM_AGENT_COMPOSE_ROOTS` 声明的 Compose 目录写权限，不扫描整个宿主机。无法唯一定位 Compose 文件时，升级会在旧容器停止前失败。

## 发布顺序

```text
设置模块期望镜像并创建发布单
        │
        ▼
所有本地节点预拉取镜像并检查 Compose
        ▼
单本地实例：启动独立旧版本 ACS 保护实例并确认可接单
        │
        ▼
本地实例 1：网关摘除 → 排空 → 更新 Compose → 切换容器
        │ Docker 健康 + 业务重新上报 Ready/accepting
        ▼
本地实例 2：网关摘除 → 排空 → 更新 Compose → 切换容器
        │
       ...
        ▼
更新高负载 ACS 模板 → 排空升级保护实例 → 发布完成
```

GPU 资源有限时，单个本地实例允许先停止旧容器再原位启动新容器。同一模块无论分布在多少节点，始终只存在一个未完成的本地更新任务。

进入下一实例前必须满足：

1. 实例已经从统一网关路由摘除；
2. 上报包含 `accepting=false`、`active_businesses=0`；
3. Agent 返回镜像和 Compose 更新任务成功；
4. 新 Docker 容器处于 `running`，且 Docker Health 不是 `unhealthy`；
5. 容器镜像与模块期望镜像一致；
6. 对应业务实例在本次替换完成后重新上报；
7. 上报包含 `healthy=true`、`ready=true`、`accepting=true`。

如果镜像启动、Docker 健康检查或任务执行失败，Agent 自动恢复该节点旧容器，平台停止整批发布。如果新进程已经启动但没有重新进行业务健康上报，平台也会停在当前实例，不会继续更新其他节点。发布超时默认一小时，可通过 `CLOUDHELM_IMAGE_RELEASE_TIMEOUT_SECONDS` 调整。

## ACS 工作负载要求

- Deployment 应使用 `RollingUpdate`；建议关键服务配置 `maxUnavailable: 0`、`maxSurge: 1`。
- StatefulSet 应使用可滚动更新策略。
- readinessProbe 必须在模型加载完成、依赖可用且能够处理业务后才成功。
- 多容器 Pod 必须在模块配置中填写“ACS 目标容器名”；单容器 Pod 可以留空。
- 新旧镜像必须属于同一仓库，只允许修改 Tag，避免误把模块切换到其他镜像来源。
- 不要让 GitOps、Helm、HPA/KEDA 或人工脚本同时覆盖同一字段；如使用 GitOps，应同步提交新镜像版本，否则外部控制器和 Cloud Helm 会持续互相纠正。

Cloud Helm 通过 Kubernetes 工作负载 PATCH 更新 Pod 模板镜像，并等待 `observedGeneration`、`updatedReplicas`、`readyReplicas`、`availableReplicas` 等状态确认滚动发布完成。本地容器在此后才开始更新。

## 管理接口

启动分阶段发布：

```http
POST /api/v1/scaling/services/{service_id}/release
Content-Type: application/json

{
  "target_image": "registry.example.com/ai/vlm:2.1.0"
}
```

回滚到平台记录的上一版本：

```http
POST /api/v1/scaling/services/{service_id}/release/rollback
```

查询至少三个历史回滚目标，或回滚到指定版本：

```http
GET /api/v1/scaling/services/{service_id}/versions
POST /api/v1/scaling/services/{service_id}/release/rollback/{version_id}
```

查询服务列表即可查看期望版本和进度：

```http
GET /api/v1/scaling/services
```

相关返回字段：

|字段|说明|
|---|---|
|`desired_image`|模块当前期望镜像，也是后续漂移纠正目标。|
|`previous_image`|兼容字段，记录最近一次发布前的版本。完整回滚目标从版本历史接口读取。|
|`release_status`|预检查、保护实例、排空、本地切换、ACS 模板更新和保护实例回收等当前阶段。|
|`release_error`|发布停止的原因。|
|`release_task_id`|当前唯一的本地更新任务。|
|`active_release_id`|当前独立发布单 ID，可用于查询逐实例步骤。|

所有发布和回滚操作要求全局管理员权限并写入审计日志。

## 推荐操作流程

1. 在镜像仓库完成新 Tag 推送和漏洞扫描。
2. 保持当前本地 GPU 容器运行，在测试环境验证新镜像能完整加载模型。
3. 确认所有本地容器已通过 `cloudhelm.service_id`、`cloudhelm.instance_id` 标签自动关联，并持续上报业务健康；旧接入方式也可继续显式上报 `container_id`。
4. 在管理台填写 ACS 目标容器名和新镜像，启动分阶段发布。
5. 观察 ACS Ready、本地实例逐个更新和业务指标；不要人工并行重启其他节点。
6. 发布完成后，把相同 Tag 同步到 Compose/Helm/GitOps 源文件，消除配置漂移。
