# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm

ARG VERSION=dev
ARG VCS_REF=unknown
ARG SOURCE_URL=https://github.com/zhuanke8/yyb

LABEL org.opencontainers.image.title="YYB pure protocol service" \
      org.opencontainers.image.description="FastAPI-based YYB protocol service" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="${SOURCE_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    YYB_USERDATA=/data \
    YYB_HOST=0.0.0.0 \
    YYB_API_PORT=18273 \
    YYB_PANEL_TLS_VERIFY=1 \
    YYB_TRUST_PROXY=0 \
    ELEME_DEVICE_FILE=/data/eleme-protocol-device.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app dist ./dist
COPY --chown=app:app eleme_assets ./eleme_assets
COPY --chown=app:app server.py ./server.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p /data \
    && chown app:app /data \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 18273

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "server.py"]
