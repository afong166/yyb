# 应用宝纯协议 1.1.8 · 51短效代理版

仓库镜像：`ghcr.io/zhuanke8/yyb:latest`。

## 51短效代理

管理员登录 `/admin`，进入「短效代理」，由前端提交 51代理 API提取页面生成的完整 URL。
API 保持页面生成的原始 `http://` 或 `https://`，并明文保存在 SQLite `admin_config` 中。
普通用户只能选择省市，看不到完整 API。

- 短效 SOCKS 地址不写入数据库；每次开始登录、续期或项目业务操作时都重新向 51代理提取。
- 二维码创建、扫码轮询和 OAuth 交换共享登录预算，整个登录流程最多使用 10 个代理。
- 手动续期、定时自动续期和运行态续期使用同一规则，整个续期流程最多使用 5 个代理。
- 呼和浩特（150100）始终使用服务器本机 IP，不请求 51代理。
- 扫码和 token 续期使用账号代理；获取 Code 保持上游 1.1.8 行为，使用本机直连。
- 项目、定时任务和云托管支持账号默认、直连、长效代理及 51短效地区。

## Docker Compose

```bash
docker compose up -d
docker compose logs -f yyb
```

Compose 固定使用 `ghcr.io/zhuanke8/yyb:latest`，直接运行镜像内由 GitHub 工作流打包的
后端、饿了么资源和前端，只把运行数据持久化到宿主机 `./data`。

默认地址为 `http://服务器IP:18273/`，管理后台为 `/admin`。

## GitHub Packages

推送到 `zhuanke8/yyb` 的 `main` 或 `master` 后，工作流会编译两套 Vue 前端，
并发布 amd64/arm64 镜像：

```text
ghcr.io/zhuanke8/yyb:latest
```

推送 `v1.1.8` 标签会额外发布 `1.1.8` 和 `1.1`。

## 注意

51 API 现在按需求明文存储，`data/app.db` 包含套餐凭据，不应公开、提交或分享。
抓包文件同样包含登录 Cookie、Token 和代理账号凭据，不要加入仓库。
