# Vue 3 前端

这个目录是生产主页的 Vue 3 + Vite 源码。Docker 多阶段构建会把
`dist/` 复制进 Python 运行镜像，由 Flask 在同一域名下提供。

## 本地运行

```powershell
npm ci
npm run dev
```

Vite 默认监听 `http://127.0.0.1:5173`，并把 `/api` 代理到
`http://127.0.0.1:15000`。如需连接其他后端：

```powershell
$env:VITE_BACKEND_URL='http://127.0.0.1:15000'
npm run dev
```

前端覆盖 Session 登录/退出、统计、健康状态、持久任务、CSRF、开始、qB 检查
和停止接口。登录表单使用标准浏览器自动填充字段；用户名来自部署目录
`.env` 的 `AUTH_USERNAME`，密码来自 `secrets/auth_password`。
