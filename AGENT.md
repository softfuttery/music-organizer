# AGENT.md

本文件适用于项目根目录及其所有子目录，供在本仓库中工作的编码代理参考。

## 项目概览

- 后端使用 Python 3.11、Flask 和 SQLite。
- 前端位于 `frontend-vue/`，使用 Vue 3、Vite 和 Node.js 22–24。
- `worker.py` 负责常规整理任务，`review_worker.py` 负责音乐预审识别与确认后的入库任务。
- `music_organizer/` 存放核心业务、数据库仓储、路由和媒体处理逻辑。
- `tests/` 存放后端测试；前端测试位于 `frontend-vue/tests/*.test.js`。

## 本地环境

使用项目虚拟环境，不要把依赖安装到全局 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
cd frontend-vue
npm ci
```

生产依赖以 `requirements.lock` 和 `frontend-vue/package-lock.json` 为准。修改依赖时必须同步更新对应锁文件，并检查依赖审计结果。

## 常用验证命令

提交改动前优先运行统一检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1
```

按改动范围快速验证：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest -q

cd frontend-vue
npm run lint
npm test
npm run build
npm audit --audit-level=high
```

新增或修改行为时必须补充相应测试；修复缺陷时应加入能够复现问题的回归测试。

## 实现约定

- 保持 Flask 路由轻量，把可复用的业务和持久化逻辑放入 `music_organizer/`。
- 数据库结构调整必须兼容已有 SQLite 数据库，并在初始化流程中提供迁移。
- 前端网络请求统一封装在 `frontend-vue/src/api.js`，写操作必须沿用现有 CSRF 机制。
- 不要绕过预审任务状态机；识别、确认、入库和归档状态必须保持数据库与界面一致。
- 所有删除、移动和清理操作都必须校验解析后的路径位于配置允许的根目录内。
- 禁止删除配置根目录、音乐库根目录或预审根目录；拒绝目录穿越和符号链接逃逸。
- 永久删除必须有清晰的用户确认；可恢复操作应优先使用项目现有回收区或隔离区机制。
- 保持中文界面文案清楚、一致，错误信息应说明用户可以采取的下一步操作。

## 配置与敏感数据

- 不要提交 `config.yaml`、`.env*`、数据库、日志、令牌、密码或 `secrets/` 中的真实内容。
- 测试必须使用临时目录、临时数据库和模拟凭据，不得访问真实音乐库或下载目录。
- 不要在日志、异常、测试快照或前端响应中回显密码、API Key、Cookie 或代理凭据。

## 改动范围

- 保留用户已有且与当前任务无关的修改。
- 避免无关重构、批量格式化和生成文件变更。
- `frontend-vue/dist/`、`frontend-vue/node_modules/`、本地虚拟环境及运行数据不应提交。
- 改动完成后检查 `git diff --check` 和 `git status --short`，确认只包含预期文件。
