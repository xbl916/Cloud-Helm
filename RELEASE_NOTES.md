# Cloud Helm 0.4.0

这是面向全新安装的企业微信强制认证与 NVIDIA GPU 监控版本。

## GPU 监控

- Agent 通过固定的 `nvidia-smi -q -x` 命令采集 NVIDIA GPU 快照。
- 展示型号、UUID、驱动/CUDA、GPU/显存利用率、显存容量、温度、风扇和功耗。
- 从 Docker `DeviceRequests` 或旧式 NVIDIA runtime 配置识别容器 GPU 分配。
- 容器详情显示其分配到的 GPU 当前指标。
- GPU 节点使用独立 `agent.gpu.compose.yml` overlay，普通节点部署方式不变。
- GPU 查询带超时、输出大小限制、XML 字段白名单，且不使用 shell。
- 项目/容器级用户只能看到其可见容器明确分配到的 GPU，不暴露同机其他 GPU 指标。

## 安全能力

- 企业微信 `snsapi_base` OAuth，应用 `AgentId` 随授权请求提交。
- `UserId` 预绑定和默认拒绝，未授权企业成员无法进入。
- OAuth `state` 浏览器绑定、五分钟过期、一次性消费并限制发起频率。
- 256 位随机服务端会话，数据库只保存令牌摘要。
- `HttpOnly + Secure + SameSite` Cookie，不在 `localStorage` 保存认证凭据。
- 写请求强制同源 `Origin`、双提交 CSRF Token 和服务端会话校验。
- 普通用户默认无资源权限，资源授权由服务端逐接口校验。
- Server 默认只绑定宿主机 `127.0.0.1`，PostgreSQL 不暴露数据库端口。

## 发布方式

`cloudhelm-0.4.0.tar.gz` 是架构无关源码发布包，在目标设备构建对应 CPU 架构的 Server 与 Agent 镜像。发布包不包含开发设备路径、`.env`、数据库、企微 Secret、Agent 注册令牌或节点身份文件。

本版本不包含旧账号密码数据库迁移逻辑；首次使用请直接按 README 的全新部署流程配置。
