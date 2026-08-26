# Docker Compose 部署指南

本指南适用于安装了 Docker Engine 和 Docker Compose v2 的 Linux、NAS，以及 Docker Desktop。
发布镜像当前为 `linux/amd64`。

## 1. 准备部署目录

```bash
git clone https://github.com/softfuttery/music-organizer.git
cd music-organizer
cp .env.example .env
cp config/config.example.yaml config/config.yaml
mkdir -p data media secrets/magicpush
```

也可以不克隆完整仓库，只下载以下文件并保持目录结构：

- `docker-compose.yml`
- `.env.example`
- `config/config.example.yaml`

## 2. 配置宿主机路径

编辑 `.env`：

```dotenv
MEDIA_ROOT=./media
CONFIG_DIR=./config
DATA_DIR=./data
SECRETS_DIR=./secrets
```

`MEDIA_ROOT` 可以替换为宿主机上的绝对媒体目录。无论宿主机路径是什么，容器内都统一映射为
`/media`，因此 `config/config.yaml` 中的输入和输出路径必须以 `/media` 开头。

硬链接要求输入和输出位于同一个文件系统、同一个挂载中。如果不能满足，请设置：

```yaml
mode: copy
```

Linux 主机还应让容器用户能够读写部署目录。默认容器用户是 `1026:100`，可在 `.env` 中
用 `APP_UID` 和 `APP_GID` 改成宿主机用户的 UID/GID。

```bash
sudo chown -R "${APP_UID:-1026}:${APP_GID:-100}" config data secrets media
chmod 700 secrets
chmod 700 secrets/magicpush
```

Docker Desktop 通常不需要手动修改 bind mount 的 UID/GID。

## 3. 创建登录和会话秘密

使用镜像内置的密码维护工具生成 scrypt 哈希。命令会交互式要求输入两次密码：

```bash
docker run --rm -it \
  --user 0:0 \
  -v "$PWD/secrets:/secrets" \
  softfuttery/music-organizer:1.8.0 \
  python -m music_organizer.auth --set /secrets/auth_password
```

生成 Flask 会话签名密钥：

```bash
docker run --rm softfuttery/music-organizer:1.8.0 \
  python -c "import secrets; print(secrets.token_hex(32))" \
  > secrets/flask_secret_key
chmod 600 secrets/auth_password secrets/flask_secret_key
```

不要把明文密码、API Key 或令牌写进 `.env`、Compose 文件或 Git。

## 4. 编辑应用配置

编辑 `config/config.yaml`，至少确认媒体映射：

```yaml
paths_mapping:
  /media/incoming/music: /media/library/music

mode: hardlink
```

如 qBittorrent 运行在宿主机，可使用：

```yaml
qbittorrent:
  enabled: true
  base_url: http://host.docker.internal:8082
  username: your-user
  password_file: /app/data/secrets/qbittorrent_password
```

`docker-compose.yml` 已在 Linux 上把 `host.docker.internal` 映射到宿主网关。qBittorrent
与 Music Organizer 必须看到一致的媒体路径；建议将它们的下载目录都映射到 `/media` 下。
密码可在 Web 配置页保存到 `data/secrets/`，不要写入 YAML。

## 5. 启动与验证

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

查看日志：

```bash
docker compose logs -f --tail=100 web worker review-worker
```

本机访问：

```text
http://127.0.0.1:15000
```

默认管理员用户名是 `admin`，可通过 `.env` 中的 `AUTH_USERNAME` 修改。

## 6. 局域网或公网访问

局域网直接访问时，在 `.env` 设置：

```dotenv
BIND_ADDRESS=0.0.0.0
```

然后重新创建 Web 容器：

```bash
docker compose up -d --force-recreate web
```

公网部署建议保持 `BIND_ADDRESS=127.0.0.1`，由 Caddy、Nginx 或 Traefik 反向代理到
`127.0.0.1:15000`，并在 HTTPS 生效后设置：

```dotenv
SESSION_COOKIE_SECURE=true
```

## 7. 升级

版本标签升级更可控。修改 `.env` 中的 `MUSIC_ORGANIZER_IMAGE` 后执行：

```bash
docker compose pull
docker compose up -d
docker image prune
```

升级前备份：

```bash
tar -czf music-organizer-backup.tgz config data secrets
```

## 8. 停止与卸载

停止服务但保留数据：

```bash
docker compose down
```

Compose 使用 bind mount，`docker compose down` 不会删除 `config/`、`data/`、
`media/` 或 `secrets/`。如需彻底删除，请先备份，再由管理员手动删除这些目录。

## 9. 常见问题

### 页面无法打开

```bash
docker compose ps
docker compose logs --tail=100 web
```

检查 `BIND_ADDRESS`、`PORT` 和主机防火墙。

### 容器提示 Permission denied

确认 `APP_UID`、`APP_GID` 与部署目录权限一致，并重新执行 `chown`。

### qBittorrent 无法连接

确认地址不是容器内的 `127.0.0.1`，优先使用 `host.docker.internal` 或同一 Compose
网络中的服务名，并确认两边媒体路径一致。

### 硬链接失败

源目录和目标目录必须位于同一宿主文件系统，并通过同一个 `MEDIA_ROOT` 挂载。否则使用
`mode: copy`。
