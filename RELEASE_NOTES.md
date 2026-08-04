# Cloud Helm 0.5.0

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

`nvidia-smi` 属于宿主机驱动工具，不能在镜像中固定一个可能与宿主机驱动不兼容的版本。0.5.0 使用 NVIDIA 官方推荐的运行时注入方式；节点仍必须安装并通过 `nvidia-ctk runtime configure --runtime=docker` 正确配置 NVIDIA Container Toolkit。

## 部署和质量保障

- README 补充企业自用小程序注册、关联、Secret、合法域名、体验版和正式发布流程。
- README 补充容器化 Caddy 的共享网络方式，避免在 Caddy 容器内错误代理 `127.0.0.1:8080`。
- CI 新增小程序 JavaScript、JSON、WXML 和 WXSS 语法检查。
- 服务端认证、权限、GPU、Agent 任务及小程序会话测试保持全量通过。

## 发布产物

- `cloudhelm-0.5.0.tar.gz`：架构无关源码与小程序发布包。
- `cloudhelm-0.5.0.tar.gz.sha256`：源码包完整性校验。
- `ghcr.io/xbl916/cloud-helm-server:0.5.0`：`linux/amd64`、`linux/arm64`。
- `ghcr.io/xbl916/cloud-helm-agent:0.5.0`：`linux/amd64`、`linux/arm64`、`linux/arm/v7`；GPU overlay 仅要求 amd64/arm64。

发布产物不包含 `.env`、企微 Secret、小程序 Secret、数据库、Agent 注册令牌或节点身份文件。
