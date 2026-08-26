# Cloud Helm 0.9.8 全新部署与 0.8.1 升级

0.9.8 是 0.9.7 的兼容补丁版本，修正 Web 与企业微信小程序中容器日志及容器结束时间的时区显示。数据库仍保存 UTC，没有新增迁移，也不会停止或重建受管业务容器。

## 发布产物

- `cloudhelm-0.9.8.tar.gz` 与 `.sha256`
- `ghcr.io/xbl916/cloud-helm-server:0.9.8`
- `ghcr.io/xbl916/cloud-helm-agent:0.9.8`

## 全新部署

配置 `.env`、Server 和 Agent 的完整步骤沿用 [0.9.7 部署手册](deploy-and-upgrade-0.9.7.md)，将其中所有镜像与目录版本替换为 `0.9.8`。GPU 节点仍应同时使用 `deploy/agent.gpu.compose.yml`，不要停止或重建节点上的 AI 业务容器。

## 从 0.9.7 升级

先拉取并验证 0.9.8 镜像，再只切换 Server：

```bash
docker pull ghcr.io/xbl916/cloud-helm-server:0.9.8
docker compose up -d --no-deps server
docker compose ps
docker compose logs --tail=100 server
```

Server 健康后，浏览器重新载入页面即可使用设备本地时区查看日志。小程序需要发布包含 0.9.8 前端代码的新版本。

Agent 0.9.8 与 0.9.7 的日志采集协议兼容；如统一版本，必须逐节点更新 Agent，并在每个节点重新上线、容器与 GPU 数量确认正确后再处理下一台：

```bash
docker compose -f agent.compose.yml -f agent.gpu.compose.yml pull agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml up -d --no-deps agent
docker compose -f agent.compose.yml -f agent.gpu.compose.yml logs --tail=100 agent
```

非 GPU 节点删除命令中的 `-f agent.gpu.compose.yml`。Agent 更新不会停止受管业务容器，也不应释放 GPU。

## 从 0.8.1 升级

备份、SQLite/PostgreSQL 迁移、配置合并和逐节点升级流程沿用 [0.9.7 部署手册](deploy-and-upgrade-0.9.7.md)，但所有发布包、镜像和目录使用 `0.9.8`。首次升级会按既有流程执行 `0008` 至 `0014` 迁移；不要跳过升级前备份与完整性检查。

## 回退

若新版 Server 无法启动，先保留日志，再把 Server 镜像恢复到升级前已验证版本并启动。不要删除数据目录或恢复数据库；0.9.8 没有新增数据库迁移。Agent 需要回退时仍按节点逐台处理，不要批量停止 Agent 或业务容器。
