# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS web-builder
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm config set registry https://registry.npmmirror.com \
    && npm ci --prefer-offline || npm ci
COPY web ./web
RUN npm run build:web

FROM python:3.11-slim-bookworm

ARG VERSION=dev
ARG VCS_REF=unknown
ARG SOURCE_URL=https://github.com/afong166/yyb

LABEL org.opencontainers.image.title="YYB 应用宝 ARMv7 多架构" \
      org.opencontainers.image.description="应用宝取码服务 · 支持 ARMv7/arm64/amd64 三架构" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="${SOURCE_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    YYB_USERDATA=/data \
    YYB_HOST=0.0.0.0 \
    YYB_API_PORT=18273 \
    YYB_TRUST_PROXY=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates gosu curl \
        libcurl4-openssl-dev libbrotli-dev libkrb5-dev \
        libuv1-dev libffi-dev libssl-dev \
        python3-dev build-essential \
        gcc g++ make \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app eleme_assets ./eleme_assets
COPY --chown=app:app server.py ./server.py
COPY --from=web-builder --chown=app:app /src/dist ./dist
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p /data \
    && chown app:app /data \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 18273

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18273/api', timeout=3).read()" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "server.py"]
