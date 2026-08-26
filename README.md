# music-organizer

`music-organizer` 是一个用 Python + Flask + SQLite 构建的音乐文件自动整理服务。它会扫描 qBittorrent 下载目录，把符合规则的音乐文件硬链接或复制到目标音乐库，同时保留源文件继续做种。

## 文件结构

```text
.
├── app.py
├── worker.py
├── review_worker.py
├── organizer.py
├── Dockerfile
├── docker-compose.yml
├── requirements.lock
├── requirements.txt
├── config/
├── secrets/
├── frontend-vue/        # 生产 Vue 3 前端源码
├── music_organizer/
└── templates
    ├── index.html
    └── history.html
```

首次部署前建议创建持久化目录，并把配置模板放入 `config`：

```bash
mkdir -p config data
cp config/config.example.yaml config/config.yaml
```

## 源码真相与发布边界

Git 的 `main` 分支及其 `origin/main` 是唯一源码真相。NAS 上
`/volume4/docker/music-organizer` 只是某个已提交版本的发布副本，不应直接修改后再反向覆盖仓库。
每个镜像都写入完整 Git 提交号，并通过 `/api/health` 返回
`source_revision`，便于把线上容器追溯到源码。

发布前要求工作树干净，并给 Web 和 Worker 使用同一个提交标签：

```bash
git diff --quiet
git diff --cached --quiet
export SOURCE_REVISION="$(git rev-parse HEAD)"
export APP_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose build web
docker compose up -d --no-deps --force-recreate web worker review-worker
```

`web`、`worker` 和 `review-worker` 通过同一个 Compose build anchor 生成同一
`music-organizer:${APP_IMAGE_TAG}` 镜像。因此可以定向构建其中任意一个服务，但更新共享 tag 后必须
同时重建三个容器，不能只重建被选中的服务；否则未重建的容器仍会持有旧 image ID。

## Docker Compose 部署

默认 `docker-compose.yml` 包含同镜像的三个角色，不会修改现有 qBittorrent：

- `web`：Flask/Gunicorn 控制面，只负责页面、API、配置和任务入队。
- `worker`：常规整理执行进程，负责调度、qB 轮询、文件操作和 FFmpeg。
- `review-worker`：音乐预审执行进程，负责 MusicBrainz 识别和用户确认后的 beets 入库。

三个角色通过同一个 SQLite 数据库共享持久任务、取消标志、调度表达式和下一次触发时间。
Web 或 Worker 重启不会丢失任务真相；进程内只保留退出信号，不保存任务状态。

```bash
docker compose build web
docker compose up -d --no-deps --force-recreate web worker review-worker
```

默认 `docker-compose.yml` 针对现有群晖环境。部署到普通 Linux、Windows Docker
Desktop 或其他 NAS 时，改用不含固定宿主 IP、群晖附加组和 host 网络的便携配置：

```bash
cp config/config.example.yaml config/config.yaml
mkdir -p data media secrets/magicpush
# 创建 secrets/auth_password 与 secrets/flask_secret_key 后再启动
docker compose -f compose.portable.yml config --quiet
docker compose -f compose.portable.yml up -d
```

通过 `.env` 设置 `MEDIA_ROOT`、`CONFIG_DIR`、`DATA_DIR`、`SECRETS_DIR`、
`APP_UID` 和 `APP_GID` 以适配宿主机。若 qBittorrent 在宿主机上，把配置中的地址设为
`http://host.docker.internal:端口`；媒体源和目标必须位于同一个 `MEDIA_ROOT` 挂载下，
否则 `hardlink` 模式应改为 `copy`。

使用 `dsm-compose-deploy` 脚本发布时，把三个共享镜像的角色组成同一个发布组；不论定向构建哪个角色，
脚本都会使用当前干净 Git 提交生成 `SOURCE_REVISION` 和 `APP_IMAGE_TAG`，随后重建、健康检查并核对
三个容器的 image ID 与 revision：

```text
--service review-worker \
--sync-service web \
--sync-service worker \
--sync-service review-worker
```

脚本遇到未提交文件、任一容器不健康、image ID 不一致或 revision 不是当前提交时会终止发布，
并保留部署前备份。`--service web` 或 `--service worker` 时使用同一组 `--sync-service` 参数即可。

构建上下文采用默认拒绝白名单：`.env*`、`config`、`data`、`secrets`、
备份和 Git 元数据不会上传给 Docker Builder。基础镜像使用 digest 固定，
`requirements.lock` 固定完整 Python 运行时依赖闭包。生产 Dockerfile 不再接受
`PIP_INDEX_URL` 构建参数，也不会把包源 URL 写入镜像环境。当前宿主构建器没有
BuildKit secret 能力，因此认证型私有 Python 源必须先升级构建器后再通过 secret
挂载实现，不能把带凭据的 URL 放进 `ARG`、`ENV` 或 Compose 文件。

如果基础镜像拉取也慢，可以把 `PYTHON_IMAGE` 换成你 Docker 环境里可用的国内镜像代理，例如：

```bash
docker compose build --build-arg PYTHON_IMAGE=<你的镜像代理>/library/python:3.11-slim-bookworm
```

之前使用 Alpine 时，`numba/llvmlite` 这类依赖容易因为没有 musl wheel 而进入源码编译，进而卡在 `numpy/cmake/LLVM` 构建链上。Debian slim 体积仍可控，同时更容易命中 manylinux 预编译 wheel，构建更稳。

默认安全边界如下：

- `web` 和 `review-worker` 使用 Compose bridge 网络；Web 仅发布到
  `127.0.0.1:15000`，供宿主机反向代理访问。
- `worker` 暂时保留 host 网络，因为现有 qBittorrent 地址指向 NAS 自身的
  `10.0.0.5:8082`；迁移为 Docker host-gateway 地址后可进一步移除。
- 三个角色均使用 `1026:100` 非 root 用户、丢弃全部 Linux capabilities，
  并启用 `no-new-privileges`。

首次从 root 容器升级前，需要让运行用户可读写持久化目录和 secret：

```bash
sudo chown -R 1026:100 config data secrets
sudo chmod 700 secrets/magicpush
sudo chmod 600 secrets/auth_password secrets/flask_secret_key
```

如 NAS 账户 UID/GID 不同，可在 `.env` 设置 `APP_UID` 和 `APP_GID` 后重建镜像。
Web 服务默认追加群晖 `administrators` 组 GID `101`，用于访问共享文件夹的
`#recycle` ACL；若 NAS 上该组的 GID 不同，可通过 `.env` 的
`DSM_RECYCLE_GID` 调整。Worker 服务不会获得这项附加权限。
若确实需要从局域网直接访问 15000 端口，应显式调整 Compose 的 Web 端口绑定；
默认配置优先避免绕过宿主机反向代理。

如果你想使用 bridge 网络，可把 `network_mode: host` 改为：

```yaml
ports:
  - "15000:15000"
```

无论使用 host 还是 bridge，都必须保证本容器与 qBittorrent 容器看到完全相同的文件系统路径。当前配置将整个 `/media` 挂载进容器：

```yaml
volumes:
  - ${MEDIA_ROOT:-./media}:/media
```

## 配置

配置模板位于 `config/config.example.yaml`，复制后编辑 `config/config.yaml`：

```yaml
paths_mapping:
  '/media/incoming/music': '/media/library/music'

mode: hardlink

keep_dir_struct: true
mkdir_if_single: true
```

`mode` 支持：

- `hardlink`：默认模式，不额外占用空间，源文件保留做种。
- `copy`：跨文件系统或硬链接失败时可改用复制。

排除规则支持目录 glob 和后缀过滤，默认会排除扫描图、ISO、MP3、日志和 qBittorrent 临时文件。

音乐预审的 MusicBrainz 查询可以单独使用带认证的 HTTP/HTTPS 代理：

```yaml
review:
  auto_discover: true
  discovery_interval_seconds: 15
  discovery_stable_seconds: 60
  proxy_url: 'http://10.0.0.2:7890'
  proxy_username: ''
  proxy_password_file: '/app/data/secrets/review_proxy_password'
  recycle_directory: '/volume2/影视/#recycle/music-organizer'
```

账号密码可以在 Web 配置页填写。密码框不会回显，保存时留空会保留已有密码；
密码会写入 `/app/data/secrets/` 下权限为 `0600` 的独立文件，不再写入 YAML。
旧版 YAML 中的 qBittorrent 密码、API Key 和代理密码会在服务启动时迁移到
对应的 `password_file`、`api_key_file` 或 `proxy_password_file`。
如果局域网 DNS 返回 Docker 无法路由的 Fake-IP，Compose 还支持通过 `.env` 中的
`MUSICBRAINZ_IP` 覆盖 MusicBrainz 地址映射。镜像构建阶段同样会用
`NPM_REGISTRY_IP`、`PYPI_ORG_IP` 和 `PYTHONHOSTED_IP` 为官方 npm/PyPI 域名提供
真实地址；CDN 地址变化时可在 `.env` 中更新对应值，无需切换第三方包源。

启用 `auto_discover` 后，Review Worker 会检查每个预审 Inbox 的直属专辑目录。
目录中的音频清单连续 `discovery_stable_seconds` 秒不变后，才会自动创建识别批次，
避免在文件仍同步时提前识别。已归档且内容未变化的目录不会重复排队；新增或修改
音频后会生成新的预审任务，但仍然需要人工确认才会真正入库。

## CUE 切分

服务会默认启用 CUE 切分。扫描到 `.cue` 文件时，会读取 CUE 中的 `FILE`、`TRACK`、`TITLE`、`PERFORMER`、`REM COMPOSER`、`ISRC` 和 `INDEX` 信息，用容器内置的 FFmpeg 将整轨音频切成多轨 FLAC。

默认输出到整理后的专辑目录：

```text
01 - Track Title.flac
02 - Track Title.flac
```

如果目标轨道文件已经存在且非空，会直接跳过，不会重复切分。即使 `.cue` 文件以前已经整理过，后续扫描也会继续检查是否缺少多轨文件，方便给旧专辑补切。

相关配置：

```yaml
cue_split:
  enabled: true
  output_subdir: ''
  filename_template: '{track:02d} - {title}'
  skip_existing: true
  split_multifile_cues: false
  ffmpeg_path: ffmpeg
  flac_compression_level: 6
```

`output_subdir` 留空表示输出到专辑目录；如果填 `tracks`，则输出到专辑目录下的 `tracks` 文件夹。
`split_multifile_cues` 默认关闭：当 CUE 里每首歌各自引用一个 `FILE` 时，会视为已经分轨并跳过切分，避免误报失败。
`flac_compression_level` 默认 6：FFmpeg 会显式使用 `-compression_level 6 -c:a flac`，与 foobar2000 Plus 的 `FLAC (Level6)` 预设对齐。FLAC 压缩等级只影响压缩速度和文件大小，不影响无损音质。

## qBittorrent 主动联动

服务可以主动登录 qBittorrent Web API，定时检查已完成的音乐种子。发现新的已完成种子后，才会触发一次整理；种子 hash 会记录到 SQLite，避免同一个完成任务反复触发空跑。

```yaml
qbittorrent:
  enabled: true
  base_url: http://127.0.0.1:8082
  username: admin
  password: ""
  timeout: 10
  min_completion_age_seconds: 60
  scan_mode: torrent_paths
  poll_mode: sync
  category: ""
  tag: ""
  retry_max_attempts: 5
  retry_base_seconds: 60
  retry_max_seconds: 3600
```

主动检查只处理路径落在 `paths_mapping` 源目录下的种子。`min_completion_age_seconds` 用于等待刚完成的任务完全落盘，默认 60 秒。
`scan_mode` 默认 `torrent_paths`，只扫描 qBittorrent 返回的完成种子路径；如果想保持旧行为，可改成 `full`，发现新完成种子后扫描整个源目录。

整理失败的种子不会在每次轮询时立即重复扫描。服务会从
`retry_base_seconds` 开始指数退避，最长等待 `retry_max_seconds`；连续失败达到
`retry_max_attempts` 后状态变为 `needs_attention` 并暂停自动重试。处理完冲突后可调用
`POST /api/qb/retry/<torrent_hash>` 清除失败次数并立即提交一次 qB 检查。

## Web 面板和 API

- `GET /`：仪表盘，显示路径、统计、上次/下次运行时间、手动触发按钮、qb 主动联动状态、最近 10 条记录。
- `GET /history`：历史记录，支持分页和搜索。
- `GET /config`：配置页，可编辑路径映射、转移方式、音乐预审与入库、定时扫描、qBittorrent 主动联动和 MagicPush 整理通知。
- `POST /api/trigger`：向 SQLite 持久队列提交一次后台整理任务。
- `POST /api/qb/trigger`：提交一次 qBittorrent 检查任务。
- `POST /api/qb/retry/<torrent_hash>`：重试一个已进入 `needs_attention` 的种子。
- `POST /api/stop`：请求取消排队任务或停止 Worker 正在执行的任务。
- `GET /api/job`：返回手动整理任务状态。
- `GET /api/stats`：返回统计 JSON。
- `GET /api/logs`：返回最近日志。
- `GET /api/csrf`：返回当前会话的 CSRF token。
- `GET /api/session`：返回当前站内登录状态。
- `POST /api/login`：使用用户名和密码建立 Session。
- `POST /api/logout`：清除当前 Session。
- `POST /api/notifications/magicpush/test`：使用已保存的 secret 发送测试通知。
- `GET /api/health`：检查 Web、SQLite 和 Worker 心跳。

所有修改请求（包括登录和退出）都要求 CSRF token。生产部署使用站内登录页和
Session Cookie，不再发送 HTTP Basic Auth challenge。浏览器可识别登录页上的标准
`username` / `current-password` 字段并保存、自动填充账号密码。

```bash
mkdir -p secrets
umask 077
openssl rand -hex 32 > secrets/flask_secret_key
docker-compose build web
sudo docker run --rm -it \
  -v "$PWD/secrets:/run/secrets" \
  --entrypoint python "music-organizer:${APP_IMAGE_TAG:-dev}" \
  -m music_organizer.auth --set /run/secrets/auth_password
```

账号设置位置（部署目录默认为 `/volume4/docker/music-organizer`）：

- 用户名：编辑 `.env` 中的 `AUTH_USERNAME`。
- 登录密码：由上面的交互命令设置；`secrets/auth_password` 只保存不可逆的
  Werkzeug `scrypt` 哈希，不保存明文，也不使用可直接还原的 Base64。
- Session 签名密钥：`secrets/flask_secret_key`，一般不需要人工修改。
- MagicPush token：在配置页录入，保存到 `secrets/magicpush/token`；页面只显示
  “已保存”，留空提交会保留原值。

修改密码时重复执行 `python -m music_organizer.auth --set` 命令。修改后保持
secret 文件权限为 `600`，并重新创建 web 容器：

```bash
sudo chown -R ${APP_UID:-1026}:${APP_GID:-100} config data secrets
sudo chmod 600 .env secrets/auth_password secrets/flask_secret_key
sudo docker compose up -d --force-recreate web
```

密码、真实配置和运行数据不要写入仓库。缺失或空 secret 会令健康检查失败，
而不是静默关闭认证。

## 数据和日志

SQLite 数据库和日志持久化在：

```text
./data/organizer.sqlite3
./data/organizer.log
```

`organized_files.source_path` 有唯一索引语义，已整理过的源文件会自动跳过，避免重复整理。

## 注意事项

- 所有路径必须是绝对路径。
- 硬链接要求源路径和目标路径在同一文件系统上。
- qBittorrent 和本服务必须挂载相同的 `/media` 路径，否则硬链接可能失败或路径不可见。
- 如果硬链接报 `Invalid cross-device link`，请确认挂载路径是否一致，或临时把 `mode` 改为 `copy`。


## 音乐预审与 beets 入库

常规整理流程不再自动调用 `beet import`。需要入库的专辑必须在“音乐预审”页选择
Inbox 目录、完成 MusicBrainz 识别并确认具体发行版本和曲目对应，随后才会进入
SQLite 持久预审队列，由 `review-worker` 执行 beets 入库。

在曲目映射区点击“试听与歌词”可打开歌词抽屉。抽屉使用“试听 / 歌词 / 处理”三段式：
浏览器原生播放器提供播放/暂停和音量控制；歌词段负责搜索候选及保存前编辑；试听段按进度
预览同步 LRC；处理段保存采用歌词、纯音乐或暂不处理的最终决定。手动滚动试听歌词会暂停
自动跟随，拖动滚动条或使用键盘翻页也不会被播放进度抢回；点击某行可跳转播放，增强 LRC
的逐字标记会按普通行歌词显示。歌词编辑器可把内容转换为标准 LRC、压缩连续空白行、使用
OpenCC 转为简体，并按毫秒整体调整行时间与逐字时间；未保存内容在关闭或刷新前会提示。

歌词决定与预审任务一起保存在 SQLite。用户确认入库后，`review-worker` 会在 beets
完成目标路径落盘后写入内置歌词标签并立即读回校验：MP3 等 ID3 文件使用 `USLT`，
M4A/MP4 使用 `©lyr`，FLAC/Ogg/Opus 等使用 `LYRICS`。只有校验成功后才会执行源目录
清理，因此建议歌词入库场景使用 `import_mode: copy`；Navidrome 无需嵌入到本项目，
重新扫描目标音乐库后即可直接读取音频文件中的歌词标签。

目标目录、Library DB、生成的 beets Config、导入方式、写标签和路径模板都统一放在
`review` 配置中。默认 `import_mode: hardlink`，不额外复制音频；跨文件系统时可改为
`copy`。默认 `write_tags: false`，因为 hardlink 场景下写标签也会改变 Inbox 或做种
文件对应的同一 inode。单次 beets 入库默认最多运行 3600 秒，可通过
`review.import_timeout_seconds` 或容器环境变量 `REVIEW_IMPORT_TIMEOUT_SECONDS` 调整；
超时或服务退出时会终止整个 beets 子进程组，任务保留在持久队列中供恢复。

可通过 `move_extra_files` 和 `extra_file_patterns` 将 `*.jpg`、`*.png` 等附加文件按
原相对路径复制到目标专辑目录（匹配不区分大小写），复制成功后删除源文件。启用
`cleanup_source_after_import` 后，还会删除已经确认入库的源音频入口，并自底向上删除
空目录；包含未匹配文件的目录会保留并在归档结果中显示警告。

路径模板使用 Picard 预设 3：
`$album_dir/%if{$albumartist,$album/}$disc_prefix$track_prefix$picard_multiartist_prefix$title`，
对应专辑艺术家、专辑、多碟编号、两位音轨号、多人专辑曲目艺术家和标题。

## 目标音乐库管理与规则入库

登录后可从侧边栏打开独立的“音乐库”页面（`/library`），直接管理
`review.directory` 指向的目标音乐库。这里不会再次复制音频文件，支持按目录或关键字查询、
试听、编辑常用标签，以及把同步歌词保存到音频内置标签或同名 `.lrc` 文件。
列表按实际文件夹和完整路径倒序分页并可展开到单曲；文件夹内所有曲目都已写入内嵌歌词时，
折叠标题会显示“内嵌完成”。文件夹和单曲都能独立删除。删除采用目标目录内的
`.music-organizer-trash` 隐藏回收站，可在页面中恢复，文件夹删除会连同封面与歌词等附属
文件整体移动。编辑抽屉使用“试听 / 歌词 / 标签”三段切换；内嵌歌词与同名 `.lrc` 分开保留，
保存后会回读校验并在抽屉内显示结果。歌词搜索默认使用标签内容自动查询，也可直接修改标题
和艺术家后手动重搜；候选按网易云音乐、QQ 音乐、酷狗音乐的优先级排列。同步播放只滚动歌词
框，不会把正在查看的标签或编辑区强制拉回歌词位置。歌词段同样提供标准格式、空白压缩、简体转换和时间偏移处理；播放器音量
和静音状态保存在当前浏览器，关闭侧栏后再次试听仍会恢复。
接口会拒绝根目录删除、目录穿越、符号链接和非音频文件。

当 MusicBrainz 没有对应发行版或曲目时，音乐预审页可选择“按文件名规则入库”。页面会
从专辑目录和文件名推断艺术家、专辑、曲名、碟号与轨号，确认前仍可逐项修改并试听、
匹配歌词。该分支不伪造 MusicBrainz ID，固定落到：

```text
/volume2/影视/整理/音乐-test/未分类/<专辑艺术家>/<音频标签专辑>/<原文件名>
```

源目录中的中间子目录不会带入目标库；所选音频和扫描到的附属文件都直接落在该专辑目录中。

具体根目录仍以 `review.directory` 配置为准。规则入库会将扫描到的封面、CUE、LOG、
TIFF 等非音频附属文件按原相对路径移入同一目标专辑目录，不受全局
`move_extra_files` 开关限制。规则入库服从 `review.import_mode` 的
copy、hardlink 或 move 设置，但会强制写入用户确认的标签；相同任务重试会复用 recovery
token，避免重复创建数据库记录或目标文件。

## Vue 3 前端

`frontend-vue/` 是生产 Vue 3 + Vite 前端源码。Docker 使用固定 Node 基础镜像和
`package-lock.json` 在独立构建阶段执行 `npm ci` / `npm run build`，最终 Python
运行镜像只包含静态构建产物，不包含 Node.js。

前端使用统一的中文系统字体、字号与按钮触控规范；手机窄屏采用底部主导航，并将
预审、音乐库和最近记录调整为单列交互。生产构建同时提供 Web App Manifest 和
Service Worker，可从支持的移动浏览器“添加到主屏幕”。Service Worker 不缓存 API
响应，避免离线时展示过期的任务或媒体库状态。

生产环境直接打开 `http://<NAS-IP>:15000`。本地开发时：

```powershell
cd frontend-vue
npm ci
npm run dev
```

开发服务器默认位于 `http://127.0.0.1:5173`，并代理后端
`http://127.0.0.1:15000`。生产主页由 Flask 同源提供 Vue 构建产物，因此 Session
Cookie、CSRF 和 API 不需要跨域配置。历史和配置页暂时保留现有服务端页面入口。

## 本地开发与 CI

项目运行时固定 Python 3.11；前端生产构建和 CI 使用 Node.js 22，本地支持 Node.js 22–24。
安装完整开发依赖后可运行统一检查：

```powershell
python -m pip install -r requirements-dev.txt
cd frontend-vue
npm ci
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/check.ps1
```

检查内容包括 Ruff、完整 pytest、Docker Compose 配置、ESLint、Vue 生产构建和依赖审计。
Gitea Actions 工作流位于 `.gitea/workflows/ci.yml`，每次 push 和 pull request 会分别执行
后端、前端与 Compose 检查。

前端后台刷新采用自适应轮询：任务执行或识别期间每 3 秒刷新，空闲时每 15 秒刷新；标签页
不可见时暂停，且同一页面不会发起重叠的刷新请求。
