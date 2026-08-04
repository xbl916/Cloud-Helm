# Cloud Helm 0.5.1

## 无残留初始化容器

- Server 与 Agent Compose 删除一次性 `data-init` 服务，启动后不再留下 `Exited (0)` 容器。
- Server 主容器只在入口阶段使用最小文件权限能力修复 `/data`，随后切换到 UID/GID `10001` 并清空全部 Linux capabilities 后启动应用。
- Agent 主容器初始化自己的 root 状态目录后立即清空全部 Linux capabilities；Docker Socket 访问方式保持不变。
- 使用 `docker compose up -d --remove-orphans` 升级时会自动删除旧版遗留的 `data-init` 容器，不影响宿主机 `cloudhelm-data` 数据。

## 企业自用小程序

- 新增原生企业微信小程序前端，包含总览、节点、容器、NVIDIA GPU、日志、启停/重启、审计以及用户资源授权。
- 新增 `wx.qy.login()` 服务端登录接口；Server 使用关联小程序在当前企业下的独立 Secret 换取并校验 `CorpId` 与 `UserId`。
- 小程序使用短期随机 Bearer 会话，不在客户端保存企业 Secret、Agent 凭据、Docker 凭据或自包含权限令牌。
- 小程序与 H5 共用现有用户、容器范围、会话撤销和审计模型；未绑定、已停用或越权成员继续默认拒绝。

## NVIDIA Agent 修复

- GPU overlay 显式选择 `nvidia` OCI runtime，确保 NVIDIA Container Toolkit 注入与宿主机驱动匹配的 `/usr/bin/nvidia-smi` 和 NVML 库。
- 保留 `NVIDIA_VISIBLE_DEVICES=all` 和最小 `NVIDIA_DRIVER_CAPABILITIES=utility`，Agent 不需要完整 CUDA 工具链。
- GPU 运行方式支持 `linux/amd64` 与 `linux/arm64`；`linux/arm/v7` Agent 继续发布，但不承诺 NVIDIA GPU 监控。
- 增加容器内 `nvidia-smi -L` 和 Agent GPU 上报的部署验收说明。

`nvidia-smi` 属于宿主机驱动工具，不能在镜像中固定一个可能与宿主机驱动不兼容的版本。0.5.1 使用 NVIDIA 官方推荐的运行时注入方式；节点仍必须安装并通过 `nvidia-ctk runtime configure --runtime=docker` 正确配置 NVIDIA Container Toolkit。

## 管理员同仓库换 Tag

- H5 与企业自用小程序新增管理员镜像更新入口，填写新 Tag 后必须再次核对原镜像和目标镜像。
- Server 与 Agent 双重限制只能更换同一 registry/namespace/repository 的不同 Tag；普通运维角色、digest、同 Tag 和跨仓库替换都会被拒绝。
- Agent 先拉取镜像，再按原配置重建容器；保留端口、卷、网络、GPU、资源限制、重启策略及 Compose 标签，创建或启动失败时自动恢复旧容器。
- 替换成功后 Server 延续原容器记录并更新 Docker ID，避免资源授权因容器重建失效。
- 自动删除、静态 IP 和 Agent 自身容器不允许在线替换；Compose 文件仍需管理员在验证后手工同步新 Tag。

## H5 移动端自适应

- 针对 320–480px 手机视口重新约束总览、节点、GPU、容器详情、用户权限、审计、弹窗和底部操作区。
- 长镜像名、GPU 型号、时间和权限按钮可换行或收缩，页面根布局不再产生横向滚动。

## 部署和质量保障

- README 补充企业自用小程序注册、关联、Secret、合法域名、体验版和正式发布流程。
- README 补充容器化 Caddy 的共享网络方式，避免在 Caddy 容器内错误代理 `127.0.0.1:8080`。
- CI 新增小程序 JavaScript、JSON、WXML 和 WXSS 语法检查。
- 服务端认证、权限、GPU、Agent 任务及小程序会话测试保持全量通过。

## 发布产物

- `cloudhelm-0.5.1.tar.gz`：架构无关源码与小程序发布包。
- `cloudhelm-0.5.1.tar.gz.sha256`：源码包完整性校验。
- `ghcr.io/xbl916/cloud-helm-server:0.5.1`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:0.5.1`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`；GPU overlay 仅要求 amd64/arm64。

发布产物不包含 `.env`、企微 Secret、小程序 Secret、数据库、Agent 注册令牌或节点身份文件。
