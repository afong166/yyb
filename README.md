# 应用宝纯协议 Docker 版

这是一个 FastAPI + SQLite 服务，包含已构建的用户端/管理端前端。镜像同时包含
Python 3.11 与 Node.js；Node.js 用于饿了么参数和益禾堂签名计算。

> 本项目会保存微信及第三方平台登录凭据，并可能执行签到、抽奖、领取等奖励操作。
> 只应处理你有权使用的账号。公开仓库前，请确认你有权再分发仓库中的源码、前端产物
> 及 `eleme_assets` 第三方资源，并自行补充合适的开源许可证。

## 从 GHCR 运行

```bash
docker compose pull
docker compose up -d
docker compose logs -f yyb
```

默认访问地址为 `http://服务器IP:18273/`，管理后台为 `/admin`。首次启动生成的
管理员令牌可在 `data/yyb-admin-token.txt` 中查看。

宿主机 `./data` 保存 SQLite 数据库、密钥、日志、二维码和设备 ID。升级或重建容器
不会删除这些数据。目录内含敏感凭据，不要提交或公开。

宿主机当前仓库会直接挂载到容器 `/app`，因此 Python、前端静态文件或 Node.js 资源
都可以直接在本地修改。普通源码修改后只需要：

```bash
docker compose restart yyb
```

不需要重新构建镜像。只有修改 `requirements.txt`、Dockerfile 或系统依赖时，才需要
重新构建并发布镜像。

Compose 使用 Docker 自带的 `bridge` 网络，不会自动创建 `yyb_api_default` 项目网络。

## GitHub Actions 发布

工作流位于 `.github/workflows/docker-publish.yml`，行为如下：

- 推送到默认分支：发布 `ghcr.io/zhuanke8/yyb:latest`、分支标签和 SHA 标签。
- 推送 `v1.2.3` 标签：额外发布 `1.2.3` 和 `1.2`。
- Pull Request：只验证构建，不推送镜像。
- 同时构建 `linux/amd64` 和 `linux/arm64`，附带 SBOM 与 provenance。

仓库首次发布后，在 GitHub 的 **Packages → Package settings → Change visibility**
中把包改成 **Public**，全网用户即可匿名拉取。GitHub 默认的 `GITHUB_TOKEN` 已由工作流
的 `packages: write` 权限用于推送，无需另建 PAT。

## HTTPS 与反向代理

直接使用 HTTP 时，`compose.yaml` 中保持：

```yaml
YYB_COOKIE_SECURE: "0"
YYB_TRUST_PROXY: "0"
```

放到可信 Nginx/Caddy HTTPS 反代后，在 `compose.yaml` 中改为：

```yaml
YYB_COOKIE_SECURE: "1"
YYB_TRUST_PROXY: "1"
YYB_CLIENT_IP_HEADER: "X-Real-IP"
```

并确保反代覆盖而不是透传客户端提交的 `X-Real-IP`。不要将数据目录、管理员令牌或
18273 后端端口直接暴露给不可信网络。

## 备份

单实例运行即可，不要配置多个 Uvicorn worker，否则定时任务可能重复执行。备份时建议
先停止容器，再整体备份 `data/`：

```bash
docker compose stop yyb
tar -czf yyb-data-backup.tgz data
docker compose start yyb
```
