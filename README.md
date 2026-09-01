# Music Organizer

Music Organizer 是一个基于 Flask、SQLite 和 Vue 3 的音乐整理服务。它可以：

- 按规则从下载目录整理音乐文件到媒体库。
- 使用硬链接保留做种文件，或改用复制模式。
- 管理 CUE 分轨、歌词、MusicBrainz 识别和人工预审。
- 通过 Web 界面管理配置、任务与整理历史。

[最新源码版本 v1.10.0](https://github.com/softfuttery/music-organizer/releases/tag/v1.10.0) · [更新日志](CHANGELOG.md) · [部署指南](DEPLOYMENT.md)

v1.10.0 新增按来源目录独立配置的预审工作流、大批次分页和浏览器兼容的 ALAC
试听。当前 Docker Hub 预构建镜像仍为 `1.8.1`；GitHub v1.10.0 Release 提供的是
已验证的源码快照，不应把镜像标签直接改为尚未发布的 `1.10.0`。

## 使用 Docker 部署

仓库中的 [docker-compose.yml](docker-compose.yml) 默认使用公开镜像：

```text
softfuttery/music-organizer:1.8.1
```

最简启动流程：

```bash
git clone https://github.com/softfuttery/music-organizer.git
cd music-organizer
cp .env.example .env
cp config/config.example.yaml config/config.yaml
mkdir -p data media secrets/magicpush
```

创建管理员密码哈希和 Flask 会话密钥后启动：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

完整的目录权限、秘密文件、媒体路径、qBittorrent、反向代理、升级和故障排查步骤见
[部署指南](DEPLOYMENT.md)。

默认只监听 `127.0.0.1:15000`。通过反向代理对外提供 HTTPS 最安全；如需直接从局域网访问，
可在 `.env` 中设置 `BIND_ADDRESS=0.0.0.0`。

## 服务组成

- `web`：Web 页面、API、配置和任务控制。
- `worker`：计划任务、qBittorrent 轮询、文件整理和 FFmpeg。
- `review-worker`：MusicBrainz 识别、人工确认和 beets 入库。

三个服务共享 `config/`、`data/` 和同一个媒体目录。SQLite 数据库、日志和运行期秘密不会写入镜像。

## 配置

主配置模板位于 [config/config.example.yaml](config/config.example.yaml)。容器内统一使用
`/media` 作为媒体根目录，例如：

```yaml
paths_mapping:
  /media/incoming/music: /media/library/music

mode: hardlink
```

如果源目录与目标目录不在同一文件系统，请把 `mode` 改为 `copy`。

## 镜像与源码

- Docker Hub: https://hub.docker.com/r/softfuttery/music-organizer
- GitHub: https://github.com/softfuttery/music-organizer
- 当前稳定源码标签：`v1.10.0`
- 当前预构建镜像标签：`1.8.1`
- 当前预构建镜像平台：`linux/amd64`

## 安全提示

- 不要提交 `.env`、`config/config.yaml`、`data/` 或 `secrets/` 中的真实内容。
- 管理员密码必须保存为 Werkzeug scrypt 哈希，不要保存明文密码。
- 公网部署应使用 HTTPS，并设置 `SESSION_COOKIE_SECURE=true`。
- 定期备份 `config/`、`data/` 和 `secrets/`。
