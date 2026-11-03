# Cloud Helm 1.0.1

本版支持 PostgreSQL 长期历史保留，取消历史行数的 2000 万配置上限，并将历史清理移出心跳请求，按索引分批执行。

## 更新内容

- **历史容量**：`CLOUDHELM_METRICS_HISTORY_MAX_ROWS` 不再限制配置最大值；设为 `0` 可关闭行数限制，只按保留时间清理。原有默认 200000 行保持兼容。
- **后台清理**：启动后及每 5 分钟运行，按时间索引选择过期数据，每批最多 1000 行并单独提交，每轮最多 20 批；积压时暂停 1 秒后继续。不再在心跳路径执行大 `OFFSET` 查询或历史删除。
- **故障与并发**：清理失败记录日志并重试，已提交批次保持有效；PostgreSQL 使用连接级 advisory lock 防止多个 worker 重复清理。关闭采样或全部节点离线时仍会清理过期历史。
- **部署文档**：补充 PostgreSQL、阿里云 ECS/ACS、容量预算、OSS 备份、恢复演练和半年历史配置说明。

## 半年历史配置

在 Server `.env` 设置后，使用新版镜像重新创建 Server：

```dotenv
CLOUDHELM_METRICS_HISTORY_INTERVAL_SECONDS=300
CLOUDHELM_METRICS_HISTORY_RETENTION_HOURS=4320
CLOUDHELM_METRICS_HISTORY_MAX_ROWS=0
```

这些参数控制节点与容器趋势历史，告警和业务聚合历史需分别配置。后台清理允许时间或行数边界短暂超出；取消配置上限不代表已经验证千万级数据的吞吐能力，仍需按实际规模规划磁盘和压测。

## 升级与回退

从 1.0.0 升级没有新增数据库迁移，保留现有配置、数据与 Agent 状态文件。先准备镜像和备份，再只切换 Server；Agent 可逐节点更新。受管业务容器无需重启，不提前释放 GPU。

如需回退到 1.0.0，先将 `MAX_ROWS=0` 或超过 2000 万的配置改为旧版本可接受的范围；旧版会恢复按行数清理，可能缩短保留时长。提高保留期无法恢复已删除的数据。

完整步骤见 [1.0.1 全新部署与旧版本升级](https://github.com/xbl916/Cloud-Helm/blob/v1.0.1/docs/deploy-and-upgrade-1.0.1.md) 和 [PostgreSQL 与阿里云完整操作手册](https://github.com/xbl916/Cloud-Helm/blob/v1.0.1/docs/fresh-deploy-postgresql-aliyun-1.0.1.md)。

## 验证

- 全套测试及 PostgreSQL 14.24 实库验证：121 项通过，1 项 SQLite 不适用的 advisory lock 测试跳过。
- 覆盖超过 2000 万的配置、关闭行数限制、过期边界、主键空洞、分批提交、失败重试、后台调度和 PostgreSQL 清理互斥。
- 代码检查、前端语法、版本一致性与源码打包检查。

## 发布产物

- `cloudhelm-1.0.1.tar.gz` 与 `.sha256`：源码和部署文件。
- `deploy-and-upgrade-1.0.1.md`、`security-hardening.md`：部署升级与安全接入说明。
- `ghcr.io/xbl916/cloud-helm-server:1.0.1`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:1.0.1`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`。

镜像同时发布 `v1.0.1` 和 `latest` 标签。发布包不包含实际 Secret、数据库或 Agent 状态文件。
