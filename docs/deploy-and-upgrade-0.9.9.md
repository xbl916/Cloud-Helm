# Cloud Helm 0.9.9 全新部署与 0.8.1 升级

0.9.9 增加安全的容器版本升级、Compose 文件同步、持久化版本历史、最近三个版本回滚、单实例 ACS 发布保障和手动维护状态。升级 Cloud Helm 平台本身不会自动升级受管业务容器，也不会停止业务容器或释放 GPU。

## 发布产物

- `cloudhelm-0.9.9.tar.gz` 与 `.sha256`
- `ghcr.io/xbl916/cloud-helm-server:0.9.9`
- `ghcr.io/xbl916/cloud-helm-agent:0.9.9`

先校验源码包：

```bash
sha256sum -c cloudhelm-0.9.9.tar.gz.sha256
tar -xzf cloudhelm-0.9.9.tar.gz
cd cloudhelm-0.9.9
```

## 升级前准备

1. 备份 `.env`、数据库和当前部署文件。SQLite 必须先通过 `PRAGMA integrity_check`；PostgreSQL 使用 `pg_dump` 创建逻辑备份。
2. 保留现有 Cloud Helm、Agent 和业务镜像，确认可以从本机镜像缓存或镜像仓库重新启动旧版本。
3. 在每台需要 Compose 同步的 Agent 主机上，把业务 Compose 目录以相同路径挂载给 Agent，并配置 `CLOUDHELM_AGENT_COMPOSE_ROOTS`。需要自动更新文件时必须可写。
4. 确认业务容器带有 Compose v2 的项目、工作目录和 service 标签，且同一容器只能定位到一个 Compose service。
5. 单本地实例服务使用发布保障时，ACS 身份需要目标命名空间中 Deployment 的 `get`、`create`、`delete` 权限。业务上报端必须原样上报平台注入的 `pool=acs_release`、`release_id` 和实例标识。
6. 网关必须按 Cloud Helm 发现结果停止向 `draining` 或 `maintenance` 实例分配新任务；业务服务必须持续上报 `accepting` 与 `active_businesses`。

## 推荐升级顺序

先拉取并验证全部 0.9.9 镜像，再开始切换。不要为了准备升级提前停止受管业务容器或释放 GPU。

逐节点升级 Agent，一次只处理一台：

```bash
docker pull ghcr.io/xbl916/cloud-helm-agent:0.9.9
cd /path/to/cloudhelm/deploy
docker compose -f agent.compose.yml -f agent.gpu.compose.yml config
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d
docker compose -f agent.compose.yml -f agent.gpu.compose.yml ps
```

非 GPU 节点去掉 `agent.gpu.compose.yml`。每台 Agent 重新上线，并确认容器清单和 GPU 数量正确后，再处理下一台。Agent 升级不会停止该节点的业务容器。

所有 Agent 验证完成后升级 Server：

```bash
docker pull ghcr.io/xbl916/cloud-helm-server:0.9.9
docker compose config
docker compose up -d cloudhelm-server
docker compose logs --tail=200 cloudhelm-server
```

Server 启动时会执行增量迁移，0.9.9 的最新迁移是 `0015_durable_image_releases`。SQLite 检测到待执行迁移时会先创建一致性备份；仍建议保留独立的离线备份。

## 验收

- 页面显示 `Cloud Helm 0.9.9`，所有 Agent 在线且版本为 0.9.9。
- 数据库迁移列表包含 `0015_durable_image_releases`。
- 在测试服务上验证手动进入维护：网关停止派单、在途任务归零、状态变为维护；退出维护后只有健康上报通过才恢复路由。
- 发起测试 Tag 发布，确认预检时旧容器仍在运行，Compose 文件产生备份且 Tag 被更新。
- 多本地实例严格一次升级一台；单本地实例先出现独立的 `release_guard_scale_up` 事件并确认旧版本 ACS 实例可服务。
- 发布完成后，常规高负载 ACS 工作负载才切换到新 Tag；版本页面能看到发布步骤和最近三个回滚目标。

## 回退

若 0.9.9 Server 无法正常启动，先保留日志，再把 Server 镜像恢复到升级前已经验证的版本并启动。`0015` 是增量表和可空字段迁移，不要删除数据目录或手工删除迁移记录。

Agent 需要回退时仍应逐节点操作：先拉取并验证旧 Agent 镜像，只切换当前 Agent，确认重新上线后再处理下一台。不要批量停止 Agent，不要停止受管业务容器，也不要释放 GPU。

业务镜像发布失败时，优先在管理台选择最近的成功版本回滚；平台会沿用相同的预检、排空、逐实例替换和健康确认流程，而不是直接批量重启容器。
