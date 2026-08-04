# Cloud Helm 0.3.0

这是面向全新安装的企业微信强制认证版本。

## 安全能力

- 企业微信 `snsapi_base` OAuth，应用 `AgentId` 随授权请求提交。
- `UserId` 预绑定和默认拒绝，未授权企业成员无法进入。
- OAuth `state` 浏览器绑定、五分钟过期、一次性消费并限制发起频率。
- 256 位随机服务端会话，数据库只保存令牌摘要。
- `HttpOnly + Secure + SameSite` Cookie，不在 `localStorage` 保存认证凭据。
- 60 分钟默认绝对会话期限，每用户最多五个并发会话。
- 写请求强制同源 `Origin`、双提交 CSRF Token 和服务端会话校验。
- 管理员可停用成员或立即撤销其全部会话。
- 身份、角色和账号状态变化自动撤销旧会话。
- 普通用户默认无资源权限，资源授权继续由服务端逐接口校验。
- 生产环境强制 HTTPS 公网地址、可信 Host、HSTS、CSP、禁止页面嵌入。
- Server 默认只绑定宿主机 `127.0.0.1`，Uvicorn access log 默认关闭。
- PostgreSQL 使用内部 Docker 网络且不暴露数据库端口。

## 发布方式

`cloudhelm-0.3.0.tar.gz` 是架构无关源码发布包，在目标设备构建对应 CPU 架构的 Server 与 Agent 镜像。发布包不包含开发设备路径、`.env`、数据库、企微 Secret、Agent 注册令牌或节点身份文件。

本版本不包含旧账号密码数据库迁移逻辑；首次使用请直接按 README 的全新部署流程配置。
