# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e
ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
# Review pinned base images and Debian package versions at least every six months.
# Next review: 2027-01.

FROM ${NODE_IMAGE} AS frontend-build
WORKDIR /frontend
COPY frontend-vue/package.json frontend-vue/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend-vue/index.html frontend-vue/vite.config.js ./
COPY frontend-vue/public ./public
COPY frontend-vue/src ./src
RUN npm run build

FROM ${PYTHON_IMAGE}

ARG DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security
ARG APP_UID=1026
ARG APP_GID=100

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_PREFER_BINARY=1 \
    HOME=/app

WORKDIR /app

RUN set -eux; \
    sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates=20230311+deb12u1 \
        ffmpeg=7:5.1.9-0+deb12u1 \
        tzdata=2026b-0+deb12u1; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir \
        pip==26.1.2 setuptools==83.0.0 wheel==0.47.0 \
    && python -m pip install --no-cache-dir --prefer-binary --no-compile \
        --no-deps -r requirements.lock

RUN mkdir -p /app/config /app/data

COPY app.py organizer.py worker.py review_worker.py ./
COPY music_organizer ./music_organizer
COPY static ./static
COPY templates ./templates
COPY --from=frontend-build /frontend/dist ./frontend_dist
RUN chown -R ${APP_UID}:${APP_GID} /app

ARG SOURCE_REVISION=unknown
ARG SOURCE_URL=https://github.com/softfuttery/music-organizer
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
ENV SOURCE_REVISION=${SOURCE_REVISION}

EXPOSE 15000

USER ${APP_UID}:${APP_GID}

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '5000') + '/api/health?component=web', timeout=3)" || exit 1

CMD ["sh", "-c", "gunicorn -w ${WORKERS:-1} --worker-class gthread --threads ${GUNICORN_THREADS:-4} --timeout ${GUNICORN_TIMEOUT:-120} -b 0.0.0.0:${PORT:-5000} app:app"]
