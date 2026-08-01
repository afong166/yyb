# 应用宝取码后端 — 完整技术规格（供 Python/FastAPI 1:1 重写）

> 本文档覆盖 `src/main/` 下除 `src/main/pureproto/`（纯协议取码，已有 Python 版）以外的全部 `.js`，
> 以及 `scripts/headless/{bootstrap.mjs, electron-shim.mjs}`。目标：不看 Node 源码即可字节级复刻后端。
>
> 原实现：Express + node-sqlite3-wasm + @node-rs/bcrypt + helmet + cookie-parser + express-rate-limit + axios。
> 当前 Python 实现监听 `0.0.0.0`，默认端口 `18273`（`YYB_API_PORT` 覆盖）。

---

## 0. 全局约定

### 0.1 启动流程（bootstrap.mjs）
1. `logger.initLogger({ console:false })` — 仅写日志文件，不打控制台（warn/error 例外）。
2. `initDatabases(logFn)` — 初始化用户库+管理库，建表/迁移，播种管理员与内置项目，必要时迁移旧 JSON。失败 `process.exit(1)`。
3. `startYybApiServer({ log })` — 启动 Express。失败 `process.exit(1)`。
4. `startAutoRenew(logFn)` — 启动微信 token 自动续期/预检定时器。
5. 打印管理员令牌 + 令牌文件路径到控制台。
6. `setInterval(()=>{}, 1<<30)` 保活；`SIGINT` → `exit(0)`。

### 0.2 userData / 数据目录（electron-shim.mjs）
- `userData` 根 = `YYB_USERDATA` 环境变量，否则 = 项目根目录下的 `data/`（`scripts/headless` 往上两级 + `data`）。启动时 `mkdir -p`。
- 相关路径（`app.getPath(name)`）：`userData`、`logs = <userData>/logs`、`cache = <userData>/Cache` 等。
- Python 复刻：数据根目录用 `YYB_USERDATA` 或 `<project_root>/data`。

### 0.3 全局中间件顺序（yybApiServer.js，严格保持）
1. `app.disable('x-powered-by')`；`trust proxy` = `YYB_TRUST_PROXY`（数字，默认 false）。
2. **helmet 安全头 + CSP**（见 §12.3）。
3. **corsLockdown**（见 §3.6）。
4. **cookie-parser**（解析 Cookie 到 `req.cookies`）。
5. **accessLog**（见 §3.7，仅记录 `/api`、`/wx`）。
6. **tolerantJson**（宽松 JSON 解析，见下）。
7. **csrfGuard**，仅挂在 `/api`（见 §3.6）。
8. 路由（见 §1）。
9. 静态 SPA 托管（见 §12.4）。
10. 404 handler → `404 {success:false, error:"not found: <METHOD> <path>"}`。
11. 错误 handler → `500 {success:false, error:<err.message|"internal error">}`。

### 0.4 tolerantJson（请求体解析）
- `GET`/`HEAD`：`req.body = {}`，直接放行。
- 其它方法：读全部 body（上限 2MB，超限 destroy 连接、不回调 next → 请求悬挂）。
- 解析：`data ? JSON.parse(data) : {}`；**JSON 解析失败 → `req.body = {}`（不报错）**；流 error → `{}`。
- **重要**：无论 Content-Type，一律尝试当 JSON 解析。Python 端应模仿：无效/空 body 一律得到 `{}`，绝不 422。

### 0.5 统一响应约定
- 成功：`{ success: true, ... }`。
- 失败：`{ success: false, error: "<中文或英文错误信息>" }`，配合非 2xx 状态码。
- accessLog 依据 `body.success !== false` 判定成功。

### 0.6 端点总览（挂载前缀 → 路由）
| 前缀 | 模块 | 鉴权基线 |
|---|---|---|
| `/api/auth` | 用户认证 | 混合 |
| `/api/admin` | 管理后台 | 管理员会话 cookie |
| `/api/licenses` | 授权码传统管理 | 管理员令牌 header |
| `/api/login` | 扫码登录 | 用户会话或授权码 |
| `/api/accounts` | 微信账号管理 | 用户会话或授权码 |
| `/api/yyb` | 纯协议取码 / 云函数 / 手机号 / 公众号 OAuth 授权 | 用户会话或授权码 |
| `/api/projects` | 项目 | 用户会话 |
| `/api/panels` | 面板配置 | 用户会话 |
| `/api` | 信息页 | 公开 |
| `/wx` | wx_server 兼容 | 授权码（含 `auth` header） |

---

## 1. 鉴权机制汇总

系统有 **4 种独立鉴权**：

### 1.1 管理员令牌（Admin Token，机器令牌）
- 生成/持久化（`licenseManager.getAdminToken`）：
  1. `process.env.YYB_ADMIN_TOKEN` 优先。
  2. 否则读 `<userData>/yyb-admin-token.txt`（trim 非空）。
  3. 否则 `crypto.randomBytes(24).toString('hex')`（48 hex 字符），写入该文件并缓存。
- 提取顺序（`extractAdminToken`）：`X-Admin-Token` header > `Authorization: Bearer <token>` > query `adminToken`/`admin_token`。
- 校验（`verifyAdminToken`）：`crypto.timingSafeEqual`（长度不等直接 false）。
- 中间件 `requireAdminToken`：失败 `401 {success:false, error:"admin token required"}`。
- 用途：`/api/licenses/*`。**同时**作为首个管理员账号 `admin` 的初始密码。

### 1.2 管理员会话 cookie
- Cookie 名：`yyb_admin_sid`。值 = `crypto.randomBytes(32).toString('base64url')`。
- 存储：管理库 `admin_sessions` 表。TTL = **3 天**。
- Cookie 选项：`httpOnly:true, sameSite:'lax', secure:(req.secure||x-forwarded-proto==='https'), path:'/', maxAge:TTL(或 0 清除)`。
- 中间件 `requireAdmin`（middleware/adminSession.js）：
  - 无 cookie → `401 {success:false, error:"管理员未登录"}`。
  - session 不存在/过期 → `401 {success:false, error:"管理员会话已过期"}`（过期即删）。
  - admin 不存在或 `status!=='active'` → `403 {success:false, error:"管理员账号不可用"}`。
  - 成功：`req.admin = publicAdmin(row)`，`req.adminSessionId = sid`。
- 用途：`/api/admin/*`（登录/登出除外）。

### 1.3 用户会话 cookie
- Cookie 名：`yyb_sid`。值 = `crypto.randomBytes(32).toString('base64url')`。
- 存储：用户库 `user_sessions` 表。TTL = **7 天**。
- Cookie 选项同上（httpOnly/lax/secure/path='/'）。
- 中间件 `requireUser`（middleware/session.js）：
  - 解析 `yyb_sid` → `getUserSession` → `findUserById`，要求 `status==='active'`。
  - 失败 → `401 {success:false, error:"未登录"}`。
  - 成功：`req.user = publicUser(row)`（同时内部 `req.sessionId`、`req.userRow`）。
- 用途：`/api/auth/me`、`/api/projects/*`、`/api/panels/*`。

### 1.4 授权码 License（机器令牌）
- 提取顺序（`extractLicenseKey`）：`Authorization: Bearer <key>` > `X-License-Key` > query `licenseKey`/`license_key`。
- 兼容版 `extractCompatLicenseKey`（/wx）额外接受：`auth` header > query `auth`。
- 校验（`validateLicense`，SQLite）：
  - 无 key → `401 "license key required"`。
  - 找不到 → `401 "invalid license key"`。
  - `status!=='active'` → `403 "license disabled"`。
  - 过期（`expires_at && now>expires_at`）→ `403 "license expired"`。
  - 成功 → `req.lic = license 视图`（见 §4.1）。
- 中间件：
  - `requireLicense`（未使用于当前路由，行为同上）。
  - `requireCompatLicense`（/wx）。
  - `requireUserOrLicense`（混合，见下）。

### 1.5 混合鉴权 requireUserOrLicense（middleware/session.js）
优先级：**用户会话 cookie > 授权码 header**。
1. 若有有效 `yyb_sid` 会话（active 用户）：
   - 取该用户的 license（`getLicenseByUserId`）。
   - 无 license → `403 "尚未分配授权码，请联系管理员"`。
   - license `status!=='active'` → `403 "授权码已被禁用"`。
   - license 过期 → `403 "授权码已过期"`。
   - 成功：`req.user`、`req.lic`。
2. 否则用授权码（`extractLicenseKey` + `validateLicense`）：成功 `req.lic`。
3. 都失败 → `<v.code||401> {success:false, error:"需要登录或有效授权码"}`。

> `clientIp(req)` = `x-forwarded-for` 第一段 trim > `req.socket.remoteAddress` > `''`，全局复用。

---

## 2. 密码 / 密钥 / 加密

### 2.1 密码哈希（auth/password.js）
- 算法：**bcrypt**（`@node-rs/bcrypt`），`ROUNDS = 12`。
- `hashPassword(plain)` = `bcrypt.hashSync(String(plain), 12)`。
- `verifyPassword(plain, hash)` = `bcrypt.verifySync`（hash 空或异常 → false）。
- `randomPassword(bytes=24)` = `crypto.randomBytes(24).toString('base64url')`（旧数据迁移占位）。
- Python：用 `bcrypt` 库，`gensalt(rounds=12)`。哈希需与 bcrypt 兼容（`$2b$` 系列）。

### 2.2 敏感字段静态加密（crypto/secretBox.js）— 用于面板密钥落库
- 算法：**AES-256-GCM**，12 字节 IV，16 字节 tag。
- 密钥来源（`loadKey`）：
  1. `YYB_SECRET_KEY`：长度 64 → hex 解码；否则 base64 解码；若得 32 字节直接用；否则 `sha256(env值)` 派生 32 字节。
  2. 否则读 `<userData>/yyb-secret.key`（base64，需 32 字节）。
  3. 否则 `randomBytes(32)`，base64 写入该文件（mode 0600）。
- `encryptSecret(plain)`：空值/undefined/null/'' → `''`；否则返回 `"v1:" + base64(iv(12) || tag(16) || ciphertext)`。
- `decryptSecret(enc)`：去掉 `v1:` 前缀，base64 解码，切分 `iv=[0:12] tag=[12:28] ct=[28:]`，AES-256-GCM 解密；失败/空 → `''`。
- Python：`cryptography` 的 `AESGCM`；输出格式必须完全一致（`v1:` 前缀 + iv||tag||ct 顺序）。

### 2.3 scriptCrypto.js
- 仅 `readProtectedFile(relativePath)`：从 `src/main/wmpf`（dev）或 `__dirname/wmpf`（prod）读文件返回 utf8。**当前路由无引用**，可忽略（WMPF 目录已随纯协议移除）。

---

## 3. 中间件详解

### 3.1 accessLog（middleware/accessLog.js）
- 只对 path 以 `/api` 或 `/wx` 开头的请求生效，其它直接 next。
- 包装 `res.json` 捕获 `body.success`/`body.error`。
- `res.on('finish')`：计算 `ok = status∈[200,400) && payloadSuccess!==false`。
  - 成功的 GET → 静默不记录。
  - 其它 → `logger.api('response'|'response-fail', {reqId,method,path,status,ms,ip,error?}, "<M> <path> → <s> (<ms>ms)", level)`；level：`>=500 error`、`>=400 warn`、`ok success`、否则 `info`。
- Python 复刻：纯日志行为，不影响响应。可简化实现，但保留"成功 GET 不刷屏"。

### 3.2 rateLimit（middleware/rateLimit.js，express-rate-limit）
- key = `x-forwarded-for` 第一段 > `remoteAddress` > `'unknown'`。
- `loginLimiter`：窗口 15 分钟、上限 **20** 次/IP。
- `registerLimiter`：窗口 60 分钟、上限 **10** 次/IP。
- 超限响应：`429`（express-rate-limit 默认）body `{success:false, error:"操作过于频繁，请稍后再试"}`，含标准 `RateLimit-*` 头。
- 挂载点：`/api/auth/register`(register)、`/api/auth/login`(login)、`/api/admin/login`(login)。

### 3.3–3.5 auth / session / adminSession 中间件 — 见 §1。

### 3.6 security.js（corsLockdown + csrfGuard）
**corsLockdown**：
- 白名单 = `YYB_CORS_ORIGINS`（逗号分隔）。默认空 = **不发任何 CORS 头（同源）**。
- 若请求 `Origin` 在白名单：设 `Access-Control-Allow-Origin: <origin>`、`Vary: Origin`、`Access-Control-Allow-Credentials: true`。
- `OPTIONS` 请求：设 `Access-Control-Allow-Methods: GET, POST, PATCH, PUT, DELETE, OPTIONS`、`Access-Control-Allow-Headers: Content-Type, Authorization, X-License-Key, X-Admin-Token, X-Requested-With, auth`，返回 `204`。

**csrfGuard**（仅 `/api`）：
- 仅对 `POST/PUT/PATCH/DELETE` 生效。
- 若请求**带会话 cookie**（`yyb_sid` 或 `yyb_admin_sid`）且 header `X-Requested-With !== 'XMLHttpRequest'` → `403 {success:false, error:"CSRF 校验失败：缺少 X-Requested-With"}`。
- 无 cookie（机器令牌/授权码调用）→ 豁免放行。
- **前端影响**：所有带 cookie 的变更请求必须带 `X-Requested-With: XMLHttpRequest`。

### 3.7 helmet 安全头（yybApiServer.securityHeaders）
CSP directives：`default-src 'self'`；`script-src 'self'`；`style-src 'self' 'unsafe-inline'`；`img-src 'self' data: https:`；`font-src 'self' data:`；`connect-src 'self'`；`object-src 'none'`；`frame-ancestors 'self'`。
`crossOriginEmbedderPolicy:false`；`crossOriginResourcePolicy: {policy:'cross-origin'}`。其余 helmet 默认头（X-Content-Type-Options、X-Frame-Options 等）。

---

## 4. 数据库

引擎：node-sqlite3-wasm（同步 API）。两个独立库文件（无跨库外键）。
- 用户库：`<userData>/user/app.db`。
- 管理库：`<userData>/admin/app.db`。
- 打开 pragma：`foreign_keys=ON`、`journal_mode=TRUNCATE`、`synchronous=NORMAL`、`busy_timeout=5000`。
- 迁移：`schema_version(version INTEGER, applied_at INTEGER)` 表记录已应用版本。当前均为 version 1。
- 密钥文件：`<userData>/yyb-secret.key`（面板密钥 AES 密钥）。

### 4.1 用户库表

**users**
```sql
CREATE TABLE users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',   -- pending | active | disabled
  role          TEXT NOT NULL DEFAULT 'user',
  note          TEXT DEFAULT '',
  created_at    INTEGER NOT NULL,   -- Date.now() 毫秒
  approved_at   INTEGER DEFAULT 0,
  approved_by   INTEGER DEFAULT 0,  -- admin id
  last_login_at INTEGER DEFAULT 0
);
CREATE INDEX idx_users_status ON users(status);
```

**user_sessions**
```sql
CREATE TABLE user_sessions (
  id         TEXT PRIMARY KEY,   -- cookie 值 base64url(32B)
  user_id    INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,   -- created + 7天
  ip         TEXT DEFAULT '',
  user_agent TEXT DEFAULT '',    -- 截断 300 字符
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_user_sessions_user ON user_sessions(user_id);
CREATE INDEX idx_user_sessions_exp ON user_sessions(expires_at);
```

**licenses**（每用户至多一条，`license_key` 兼作机器令牌）
```sql
CREATE TABLE licenses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  license_key TEXT NOT NULL UNIQUE COLLATE NOCASE,
  max_users   INTEGER NOT NULL DEFAULT 1,   -- 可绑定微信账号数
  status      TEXT NOT NULL DEFAULT 'active', -- active | disabled
  note        TEXT DEFAULT '',
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER DEFAULT 0,   -- 0=永不过期
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_licenses_user ON licenses(user_id);
```

**wechat_accounts**（归属镜像 + 展示字段；敏感 token 存 JSON 文件，见 §8）
```sql
CREATE TABLE wechat_accounts (
  openid       TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL,
  nickname     TEXT DEFAULT '',
  unionid      TEXT DEFAULT '',
  head_img_url TEXT DEFAULT '',
  logged_at    INTEGER DEFAULT 0,
  expire_at    INTEGER DEFAULT 0,
  status       TEXT DEFAULT 'active',
  status_error TEXT DEFAULT '',
  is_current   INTEGER DEFAULT 0,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_wechat_user ON wechat_accounts(user_id);
```
> `boundOpenids` 概念 = 某用户在 wechat_accounts 中的所有 openid（派生，不单独存字段）。

**panel_configs**（密钥密文）
```sql
CREATE TABLE panel_configs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id           INTEGER NOT NULL,
  panel_type        TEXT NOT NULL,          -- qinglong | daidai
  base_url          TEXT NOT NULL,
  client_id         TEXT DEFAULT '',        -- 明文（青龙 client_id / 呆呆 app_key）
  client_secret_enc TEXT DEFAULT '',        -- AES-256-GCM 密文（"v1:..."）
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  last_test_at      INTEGER DEFAULT 0,
  last_test_ok      INTEGER DEFAULT 0,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE(user_id, panel_type)
);
CREATE INDEX idx_panel_user ON panel_configs(user_id);
```

**call_records**（无外键，删用户时手动清）
```sql
CREATE TABLE call_records (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  openid     TEXT DEFAULT '',
  project_id INTEGER DEFAULT 0,
  appid      TEXT DEFAULT '',
  action     TEXT DEFAULT '',   -- get-code | get-codes | jd-code-login | run-project | submit-panel ...
  result     TEXT DEFAULT '',   -- ok | fail
  error      TEXT DEFAULT '',   -- 截断 500
  instance   TEXT DEFAULT '',
  ms         INTEGER DEFAULT 0,
  ip         TEXT DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_call_user_time ON call_records(user_id, created_at);
CREATE INDEX idx_call_project ON call_records(project_id);
```

### 4.2 管理库表

**admins**
```sql
CREATE TABLE admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    INTEGER NOT NULL,
  last_login_at INTEGER DEFAULT 0
);
```

**admin_sessions**
```sql
CREATE TABLE admin_sessions (
  id         TEXT PRIMARY KEY,   -- cookie base64url(32B)
  admin_id   INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,   -- created + 3天
  ip         TEXT DEFAULT '',
  user_agent TEXT DEFAULT '',
  FOREIGN KEY(admin_id) REFERENCES admins(id) ON DELETE CASCADE
);
CREATE INDEX idx_admin_sessions_exp ON admin_sessions(expires_at);
```

**projects**
```sql
CREATE TABLE projects (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  summary     TEXT DEFAULT '',
  intro       TEXT DEFAULT '',    -- markdown
  tutorial    TEXT DEFAULT '',    -- markdown
  icon        TEXT DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'off',  -- on | off
  panel_type  TEXT DEFAULT '',    -- qinglong | daidai | ''
  run_config  TEXT DEFAULT '',    -- JSON 字符串
  sort_order  INTEGER DEFAULT 0,
  created_by  INTEGER DEFAULT 0,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX idx_projects_status ON projects(status);
```

**admin_audit_log**
```sql
CREATE TABLE admin_audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id    INTEGER DEFAULT 0,
  action      TEXT NOT NULL,
  target_type TEXT DEFAULT '',
  target_id   TEXT DEFAULT '',
  detail      TEXT DEFAULT '',   -- 截断 500
  ip          TEXT DEFAULT '',
  created_at  INTEGER NOT NULL
);
CREATE INDEX idx_audit_time ON admin_audit_log(created_at);
```

### 4.3 初始化播种（db/index.js）
- `ensureSeedAdmin`：管理库无 admin 时，插入 `username='admin'`，`password_hash = bcrypt(管理员令牌)`，`status='active'`。**初始密码 = 管理员令牌**。
- `seedBuiltinProjects`：见 §9（京东 Code 登录内置项目）。
- `migrateFromJsonIfNeeded`：仅当用户库 users 表为空 且存在旧 `yyb-licenses.json`。见 §4.4。

### 4.4 旧 JSON 迁移（db/migrateFromJson.js）
仅当 users 为空且 `<userData>/yyb-licenses.json` 有 `licenses[]`：
- 每个旧 license → 合成一个 `active` 用户：`username = 'legacy-' + key.replace(/[^A-Za-z0-9-]/g,'').slice(0,40)`，随机密码 hash，note='由旧授权码迁移生成'，`created_at=lic.createdAt||now`，`approved_at=now`。
- 建 license 记录：`license_key=key.toUpperCase()`、`max_users=lic.maxUsers||1`、`status=(lic.status==='disabled'?'disabled':'active')`、`note`(≤200)、`expires_at`。
- 每个 `boundOpenids[]` → `INSERT OR IGNORE wechat_accounts`，展示字段从 `yyb-accounts.json` 补齐。
- 不删旧 JSON。Python 复刻建议实现同等幂等迁移（可选）。

---

## 5. `/api/auth/*` — 用户认证

### POST /api/auth/register
- 鉴权：公开（registerLimiter）。
- Body：`{ username: string, password: string }`。
- 校验（registerUser）：username 匹配 `^[a-zA-Z0-9_.-]{3,32}$`；password 长度 ≥6；username 不重复（NOCASE）。
- 副作用：插入 users（`status='pending'`, `role='user'`），日志。
- 成功 `200`：
```json
{ "success": true, "user": { "id":1, "username":"foo", "status":"pending", "role":"user", "note":"", "createdAt":1700000000000, "approvedAt":0, "lastLoginAt":0 }, "message": "注册成功，请等待管理员审核后登录" }
```
- 失败 `400`：`{ "success": false, "error": "<用户名需为 3-32 位...|密码至少 6 位|用户名已存在|注册失败>" }`。

### POST /api/auth/login
- 鉴权：公开（loginLimiter）。
- Body：`{ username, password }`。
- 逻辑（authenticate）：用户名或密码错 → `401 "用户名或密码错误"`；`status==='pending'` → `403 "账号待管理员审核"`；`status==='disabled'` → `403 "账号已被禁用"`；成功更新 `last_login_at`。
- 成功：创建 user_session，`Set-Cookie: yyb_sid=...`。响应：
```json
{ "success": true, "user": {publicUser}, "hasLicense": true }
```
  `hasLicense` = 该用户是否有 license。
- 失败：`<code> {success:false, error}`。

### POST /api/auth/logout
- 公开。若有 `yyb_sid` → 删除该 session；清 cookie。响应 `{ "success": true }`。

### GET /api/auth/me
- 鉴权：requireUser。
- 响应：
```json
{ "success": true, "user": {publicUser}, "license": { "key":"XXXX-...", "maxUsers":3, "status":"active", "expiresAt":0, "usedCount":1, "remaining":2 } }
```
  无 license 时 `"license": null`。

> **publicUser 对象**：`{ id, username, status, role, note, createdAt, approvedAt, lastLoginAt }`。

---

## 6. `/api/admin/*` — 管理后台

### POST /api/admin/login
- 公开（loginLimiter）。Body `{username, password}`。
- authenticateAdmin：错 → `401 "用户名或密码错误"`；`status!=='active'` → `403 "管理员账号已禁用"`；成功更新 last_login。
- 副作用：创建 admin_session，`Set-Cookie: yyb_admin_sid`，audit `admin-login`。
- 成功：`{ "success": true, "admin": {publicAdmin} }`。
- **publicAdmin**：`{ id, username, status, createdAt, lastLoginAt }`。

### POST /api/admin/logout
- 公开。删 admin_session + 清 cookie。`{ "success": true }`。

> 以下全部需 `requireAdmin`（yyb_admin_sid）。变更请求还需 `X-Requested-With: XMLHttpRequest`（csrfGuard）。

### GET /api/admin/me → `{ success:true, admin:{publicAdmin} }`

### POST /api/admin/change-password
- Body `{password}`。setAdminPassword：`password` 长度 <8 → `400 "管理员密码至少 8 位"`。
- audit `admin-change-password`。成功 `{success:true}`。

### GET /api/admin/stats
```json
{ "success": true, "users": { "total":10, "pending":2, "active":7 }, "licenses": 5, "recentCalls": 123 }
```
- `recentCalls`：若存在任意调用记录，则取 `listAll({limit:500}).length`（0..500），否则 0。

### GET /api/admin/users?status=<pending|active|disabled>
- 响应 `{ success:true, users:[userSummary] }`，按 id 降序。
- **userSummary** = publicUser + `license: { key, maxUsers, status, usedCount } | null` + `wechatCount`（有 license 用 `license.usedCount`，否则该用户 wechat_accounts 计数）。

### GET /api/admin/users/:id
- 找不到 → `404 {success:false, error:"user not found"}`。
- 响应：
```json
{ "success": true, "user": {publicUser}, "license": {license视图|null}, "wechatAccounts": [<wechat_accounts行>], "callRecords": [<call_records行 最多100 id降序>], "callCount": 42 }
```

### 用户状态变更（三个端点，body 无）
- POST /api/admin/users/:id/approve → status='active'（首次置 active 会写 approved_at/approved_by），audit `user-approve`。
- POST /api/admin/users/:id/enable → active，audit `user-enable`。
- POST /api/admin/users/:id/disable → disabled，audit `user-disable`。
- 成功 `{success:true, user:{publicUser}}`；失败 `400 {success:false,error}`。

### DELETE /api/admin/users/:id
- 找不到 → `404`。
- 副作用：遍历该用户的 wechat_accounts，逐个 `removeYybAccount(openid)`（清 JSON 文件 token，best-effort）；`deleteUser`（先删 call_records，再删 users，级联删 sessions/licenses/wechat_accounts/panel_configs）。audit `user-delete`(detail=username)。
- 成功 `{success:true}`。

### POST /api/admin/users/:id/reset-password
- Body `{password}`（≥6，否则 400）。audit `user-reset-password`。成功 `{success:true}`。

### POST /api/admin/users/:id/license （发放或更新）
- Body `{ maxUsers?, note?, expiresAt? }`。
- 无 license → issueLicense（`maxUsers=body.maxUsers||1`），audit `license-issue`(detail=key)。
- 有 license → updateLicense，audit `license-update`(detail=key)。
- 成功 `{success:true, license:{license视图}}`；失败 `400`。

### POST /api/admin/users/:id/license/status
- Body `{status}`（'active' 或非 'active' 视为 'disabled'）。
- 无 license → `404 "该用户无授权码"`。audit `license-status`。
- 成功 `{success:true, license:{视图}}`。

### DELETE /api/admin/users/:id/license
- 无 license → `404 "该用户无授权码"`。audit `license-delete`。成功 `{success:true}`。

### GET /api/admin/licenses → `{ success:true, licenses:[license视图...] }`（id 降序）

### GET /api/admin/call-records?userId=&limit=&offset=
- `limit` 默认 200 上限 1000；`offset` 默认 0。userId>0 时按用户过滤，否则全量。
- `{ success:true, records:[call_records行...] }`（id 降序）。

### GET /api/admin/audit?limit=
- limit 默认 200 上限 1000。`{ success:true, audit:[admin_audit_log行...] }`（id 降序）。

### 项目管理
- GET /api/admin/projects → `{success:true, projects:[projectView(简) ...]}`（sort_order 降序, id 降序）。
- GET /api/admin/projects/:id → `{success:true, project:{projectView(全)}}`，找不到 404。
- POST /api/admin/projects → body=项目字段（见 §9.1），createProject，audit `project-create`(detail=name)，返回全视图。
- PUT /api/admin/projects/:id → updateProject（部分更新），audit `project-update`。
- POST /api/admin/projects/:id/shelf → body `{on:boolean}`，setShelf，audit `project-on`/`project-off`。
- DELETE /api/admin/projects/:id → removeProject，audit `project-delete`。

---

## 7. `/api/licenses/*` — 授权码传统管理（需管理员令牌）

全部经 `requireAdminToken`（§1.1）。注意：这是**基于 SQLite** 的 licenseService（不是旧 JSON）。

### GET /api/licenses → `{ success:true, licenses:[视图...] }`

### POST /api/licenses
- Body：`{ userId(必填), maxUsers(必填≥1), note?, expiresAt?, key? }`。
- issueLicense：`userId` 无效 → `"userId required"`；`maxUsers` 无效 → `"maxUsers must be a positive integer"`；user 不存在 → `"user not found"`；用户已有 license → `"user already has a license"`；key 冲突 → `"license key already exists"`（未传 key 时自动重生成≤5次）。
- key 生成规则：4 段×4 字符，字母表 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（去易混淆），`-` 连接（如 `AB2C-D3EF-...`）。传入 key 一律 `toUpperCase()`。
- 成功 `{success:true, license:{视图}}`；失败 `400`。

### GET /api/licenses/:key → 找不到 `404 "license not found"`，否则 `{success:true, license}`。

### PATCH /api/licenses/:key
- Body：`{ maxUsers?, note?, status?, expiresAt? }`。updateLicense。
- `maxUsers < 当前已绑定数` → 报错 `"maxUsers (N) less than current bound count (M)"`。`status` 只能 active/disabled。成功 `{success:true, license}`。

### POST /api/licenses/:key/disable → setLicenseStatus disabled。`{success:true, license}`。
### POST /api/licenses/:key/enable → active。
### DELETE /api/licenses/:key → 删除。找不到 `404`；成功 `{success:true, license:<删前视图>}`。

### POST /api/licenses/:key/unbind
- Body：`{ openid }` 或 `{ openids:[...] }`。从 wechat_accounts 解绑（删除该 user 下对应 openid 行）。`{success:true, license}`。

### POST /api/licenses/:key/bind
- Body：`{ openids:[...]|openid, force? }`。空 → `400 "openids required"`。
- license disabled/expired → 抛错 400。逐个绑定：
  - 已属该用户 → skipped(reason 'already bound')。
  - 属其它用户且 !force → skipped(`bound to user #N`)。
  - 超容量（count≥max_users）→ rejectedByCap。
  - 否则 upsert wechat_accounts(user_id=该用户)。force 时抢占其它用户。
- 成功：`{ success:true, license:{视图}, added:[...], skipped:[{openid,reason}], rejectedByCap:[...] }`。

---

## 8. `/api/login/*` — 扫码登录 OAuth（yybLogin.js，重点）

### 8.1 端点

**POST /api/login/start**（requireUserOrLicense）
- Body：`{ proxyUrl? }`（SOCKS5/HTTP 代理 URL，可空）。
- 容量检查：`canLoginUnder(license.key, null)`——已用数 ≥ maxUsers → `<code|403> {success:false, error:"license user count exceeded (N/M)"}`。
- 调用 `startYybLogin(onEvent, {proxyUrl})`，返回二维码元数据。会话态保存在**进程内 Map** `_loginSessions`（key=sessionId），登录事件异步更新。
- 成功 `200`：
```json
{ "success": true, "sessionId": "<16 hex>", "uuid": "<wx uuid>", "qrcodeDataUrl": "data:image/png;base64,...", "qrcodeUrl": "https://open.weixin.qq.com/connect/qrcode/<uuid>", "state": "<guid>" }
```

**POST /api/login/stop**（公开）
- Body：`{ sessionId? }`。传 → 停该会话并置 `status:'cancelled'`；不传 → 停全部并清空 `_loginSessions`。`{success:true}`。

**GET /api/login/status**（requireUserOrLicense）
- Query：`sessionId?`。
- 带 sessionId：合并 `getYybLoginStatus(sessionId)` 与 `_loginSessions.get(sessionId)`。若该会话 `licenseKey` 与当前 license 不符 → `403 "session does not belong to this license"`。响应 `{ success:true, ...loginStatus, ...state }`。
  - loginStatus（进程内轮询态）：`{ running:bool, uuid, status, sessionId }`（不存在时 `{running:false,uuid:null,status:null}`）。
  - state（_loginSessions 记录）：`{ running, status, error, uuid, qrcodeDataUrl, account?, licenseKey }`。
- 不带 sessionId：汇总 `{ success:true, running:bool, sessionCount:N, sessions:[{sessionId,uuid,status, ...state}] }`，只含属于本 license 的会话。

**登录会话进程内状态机（_loginSessions 值）**：
`{ running:bool, status:('waiting'|'scanned'|'confirmed'|'success'|'expired'|'rejected'|'cancelled'|null), error:string|null, uuid, qrcodeDataUrl, licenseKey, account:{...}|null }`。

**成功事件 account 结构**（写入 state.account）：
`{ openid, nickname, unionid, headImgUrl, sex, country, province, city, loggedAt, expireAt }`。

### 8.2 OAuth 完整外部调用链

常量：
- `APPID = 'wxd44977328b36e647'`
- `SCOPE = 'snsapi_login,snsapi_runtime_pcsdk'`
- `REDIRECT_URI = 'https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX'`
- `USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0 CefView/1.0 (Windows; en-us) YYBAppClient/5.10.3340.4757 Safari/537.36'`
- `QRCONNECT_HREF_CSS` = 固定 `data:text/css;base64,...`（见源码，样式覆盖，逐字复制）。

**设备身份**（`<userData>/yyb-device.json`）：
`{ guid: randomBytes(16).hex(32字符), yybClientId: randomUUID() }`，不存在则生成并落盘。`state = guid`。

**步骤 1：拉 uuid** — `GET https://open.weixin.qq.com/connect/qrconnect`
Query（顺序）：`appid=<APPID>&fast_login=1&href=<QRCONNECT_HREF_CSS>&redirect_uri=<REDIRECT_URI>&response_type=code&scope=<SCOPE>&self_redirect=true&state=<guid>`
Header：`User-Agent:<UA>`, `Accept:*/*`, `Referer: https://pc.yyb.qq.com/`。
响应：HTML，正则 `connect\/qrcode\/([A-Za-z0-9_-]+)` 取 uuid。非 200 报错。

**步骤 2：二维码图片** — 本地生成，不请求。二维码内容 = `https://open.weixin.qq.com/connect/confirm?uuid=<uuid>`，用 `QRCode.toDataURL(url, {width:200, margin:1})` 生成 PNG dataURL。
（`qrcodeUrl` 字段 = `https://open.weixin.qq.com/connect/qrcode/<uuid>` 仅作展示）。

**步骤 3：长轮询扫码状态** — `GET https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid=<uuid>&last=<last>`
Header：`User-Agent`, `Accept:*/*`, `Referer: https://open.weixin.qq.com/`。timeout 40s。**始终直连（不走代理）**。
响应体 JS 文本，正则解析：`wx_errcode=(\d+)`、`wx_code='([^']*)'`。
- errcode `408`：等待扫码（status='waiting'）。
- `404`：已扫码待确认（status='scanned'）。
- `405` 或拿到 `wx_code`：用户确认（status='confirmed'，进入步骤 4）。
- `402`：二维码过期（status='expired'，结束）。
- `403`：用户拒绝（status='rejected'，结束）。
循环把上次 errcode 作为 `last` 传入。

**步骤 4：换应用宝账号** — `GET https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX&code=<wx_code>&state=<guid>`
Header：`User-Agent`, `Accept:*/*`, `Referer: https://open.weixin.qq.com/`, `Accept-Language: en-US,en;q=0.9`,
`Cookie:` 以 `; ` 连接：`yybClient-id=<yybClientId>`, `guid=<guid>`, `appid=<APPID>`, `login_from=platform_login`, `login_method=fast_login`, `channel=2100100013`, `wx_sdk_version=5.10.2700.327`, `androws_version=5.10.3340.4757`。
响应：**302 或 200**，从 `Set-Cookie` 解析（每条取 `k=v` 首段，去引号）。必须有 `accesstoken` 和 `openid`，否则报错。
账号对象字段（从 cookie）：`openid, accesstoken, refreshtoken(默认''), appid(默认APPID), logintype('WX'), scope, expiresIn=Number(expires_in)||0, expireAt=(expires_in? now+expires_in*1000 :0), nickname(decodeURIComponent), headImgUrl(head_img_url), guid, yybClientId, loggedAt=now`。

**步骤 5：补全 userinfo** — `GET https://api.weixin.qq.com/sns/userinfo?access_token=<at>&openid=<openid>&lang=zh_CN`
成功（无 errcode）补：`nickname, headImgUrl(headimgurl), unionid, sex, country, province, city`。

**代理策略**：`code` 兑换（步骤4）与 userinfo 走 `proxyUrl`；但 `account.proxyUrl` **不持久化**（短效代理用完即丢，后续 refresh/validate 走直连）。步骤 3 长轮询恒直连。

**代理实现（proxyUtil + httpRequest 分派）**：
- SOCKS（`^socks(4|5h?)?://`）→ SocksProxyAgent。
- HTTP/HTTPS 代理 → 优先 Electron `session.fetch`（headless 下 session 抛错）→ 降级 raw CONNECT（Proxy-Authorization Basic）→ 手动 TLS。
- Python 复刻：SOCKS 用 `python-socks`/`httpx[socks]`；HTTP 代理用标准 proxy。Electron 分支在 headless 不可用，等价于"带认证 HTTP 代理走 CONNECT 隧道"。

### 8.3 账号持久化（`<userData>/yyb-accounts.json`）
结构：`{ current: <openid>, accounts: [account...] }`。写时先写 `.bak` 再写正式文件；读损坏时回退 `.bak`。异步文件锁串行化写。
account 完整字段：`{ openid, accesstoken, refreshtoken, appid, logintype, scope, expiresIn, expireAt, nickname, headImgUrl, unionid, sex, country, province, city, guid, yybClientId, loggedAt, status('active'|'error'), statusError, proxyUrl? }`。
> 敏感 token 存此 JSON，SQLite `wechat_accounts` 只存归属+展示镜像。

### 8.4 Token 刷新 / 自动续期
**refreshYybAccount(openid)**：
- `GET https://api.weixin.qq.com/sns/oauth2/refresh_token?appid=<appid>&grant_type=refresh_token&refresh_token=<rt>`。
- errcode 40030 → `"refresh_token 已失效，需重新扫码登录"`；其它 errcode → `"refresh_token 续期失败 (errcode=N)"`。
- 成功更新 `accesstoken/refreshtoken/expiresIn/expireAt(now+expires_in*1000)/scope`，再拉 userinfo 补 nickname/headImgUrl/unionid，upsert。

**validateAccessToken**：`GET https://api.weixin.qq.com/sns/auth?access_token=&openid=` → `errcode===0` 为有效。

**startAutoRenew**（定时器）：
- 每 **3 分钟**：遍历账号（跳过 status='error'）；先 validate，失效则标记 error(statusError='access_token 已失效')；有效且 `expireAt-now < 10分钟` → refresh，失败标 error。
- 启动 5 秒后 + 每 **30 分钟**：`fetchLoginBufferByOpenid`（accountSwitcher.js）预检，失败标 error。

### 8.5 accountSwitcher.js — login_buffer 预检
- `POST https://yybadaccess.3g.qq.com/pc_yyb_auth/pcyyb_get_wx_login_buffer_auth`
- 常量：`CLIENT_ID='9ce1c7c4-0254-44a9-8064-70024b134644'`，`ACCESS_KEY='wgrdg373hy26ww2'`。
- Body（JSON）：`{ extInfo: { listS: { unionid:{value:[null]}, user_id:{value:[guid]}, access_token:{value:[accesstoken]} }, listI: { user_type:{value:[1]} } } }`。
- 签名：`Ual-Access-Signature = md5(bodyStr + timestamp + ACCESS_KEY + nonce)`；`timestamp=Date.now()`，`nonce=floor(10000*random())`，`businessid='pc_yyb_auth'`。
- Header：含 `Ual-Access-Businessid/Timestamp/Nonce/Signature`、`Cookie`（见源码 buildCookieString）、YYB UA、`sec-ch-ua*`。
- 成功：`json.code===0 && json.ext_info.list_s.login_buffer.value[0]`；`code===-101 && (invalid credential|invalid scope)` → `"access_token 已失效，需重新扫码登录"`。
- **仅用于预检/健康检查，取码本身由 pureproto 完成**（见 §11）。

---

## 9. `/api/accounts/*` — 微信账号管理（requireUserOrLicense）

结果只含**绑定到本 license 的账号**（`boundSet = new Set(lic.boundOpenids)`）。

### GET /api/accounts
- 读 `yyb-accounts.json`，过滤 `boundSet.has(openid)`。
- 响应：
```json
{
  "success": true,
  "current": "<openid|''>",     // 仅当 store.current 在 boundSet 内
  "total": 3, "active": 2, "error": 1,
  "maxUsers": 5,
  "accounts": [
    { "openid":"", "nickname":"", "unionid":"", "headImgUrl":"", "loggedAt":0, "expireAt":0,
      "hasSession": false, "isCurrent": false, "status":"active", "statusError": null }
  ]
}
```
  - `hasSession` = `!!(a.protocolSession && a.protocolSession.encryptKey)`。
  - `active = total - errorCount`（error = status==='error'）。

### POST /api/accounts/refresh
- Body `{openid}`。缺 → `400 "openid required"`；未绑定 → `403 "openid not bound to this license"`。
- 调 refreshYybAccount。成功：
```json
{ "success": true, "account": { "openid":"", "nickname":"", "expireAt":0, "expiresIn":0 } }
```

### POST /api/accounts/delete  与  DELETE /api/accounts/:openid（同一处理）
- Body/param 提供 openid。缺 → 400；未绑定 → 403。
- 副作用：`removeYybAccount`（清 JSON token）失败 → `500 {success:false,error}`；再 `unbindOpenid` + `removeOpenid`（清 SQLite，best-effort）。
- 成功：`{ "success": true, "openid": "<openid>" }`。

---

## 10. `/api/yyb/*` — 纯协议取码 / 云函数 / 手机号 / 公众号 OAuth 授权（code.py，requireUserOrLicense）

### POST /api/yyb/get-code
- Body：`{ openid, appid }`。缺 openid/appid → `400`。未绑定 → `<code|403>`。
- 调 `getCodeForOpenid(openid, appid)`（pureproto/account.js，见 §11）。
- 记录 call_records(action='get-code')。
- 响应（成功 `200`，失败 `500`）：`{ ...result, openid, appid }`，其中 result = `{ success, code?, error? }`。
  - 成功例：`{ "success":true, "code":"<32位wx.login code>", "openid":"", "appid":"" }`。
  - 失败例：`{ "success":false, "error":"...", "openid":"", "appid":"" }`。

### POST /api/yyb/get-codes（批量并发）
- Body：`{ accounts:[openid,...], appid }`。空数组 → `400 "accounts array required"`；缺 appid → `400`。
- 未全部绑定 → `<code|403> { success:false, error:"some openids are not bound to this license", unbound:[...] }`。
- `Promise.all` 并发取码。逐个记 call_records(action='get-codes')。
- 响应（全成功 `200`，部分成功 `207`）：
```json
{ "success": true, "results": [ { "openid":"", "success":true, "code":"", "error":null, "totalMs":1200 } ], "totalMs":1500, "summary":"2/2 accounts succeeded" }
```

### POST /api/yyb/invoke-cloud
- Body：`{ openid, appid, param2|param1|data }`。`param2` 为云函数 JSON，如 `{"api_name":"webapi_getshareconf","data":{...}}`。缺 openid/appid → `400`；未绑定 → `403`。
- 调 `invoke_cloud_for_openid(openid, appid, data_json)`（operateWXData，cmdid=1133）。记 call_records(action='invoke-cloud')。
- 响应（成功 `200`/失败 `500`）：`{ success, respJson?, error?, openid, appid }`。`respJson` 为云函数返回的业务 JSON 字符串。

### POST /api/yyb/get-phone
- Body：`{ openid, appid, param2|data? }`。缺 openid/appid → `400`；未绑定 → `403`。
- 调 `get_phone_for_openid(openid, appid, data_json)`（getallphone，cmdid=2536）。记 call_records(action='get-phone')。
- 响应：`{ success, respJson, mobile?, encryptedData?, iv?, cloudId?, code?, customPhoneList?, openid, appid }`。call_records 的 `code` 列存脱敏手机号。

### POST /api/yyb/oauth-authorize ★本次新增（公众号 OAuth2 授权）
- Body：`{ openid, appid, url, biz_username?, scene?, referrer_url?, sub_scene?, auto_oauth? }`。
  - `url`：OAuth2 授权 URL，例 `https://open.weixin.qq.com/connect/oauth2/authorize?appid=<公众号appid>&redirect_uri=<回调>&response_type=code&scope=snsapi_userinfo&state=<state>#wechat_redirect`。
  - 缺 openid/appid → `400`；缺 url → `400 "url required"`；未绑定 → `403`。
- 走 ilink mmtls 通道 `/ilink/ilinkapp/mm/bizoauth/oauth_authorize`（cmdid=4313），复用 codebridge 三档会话缓存（与取码同一登录会话，支持账号级 SOCKS5 代理）。记 call_records(action='oauth-authorize'，`code` 列存 redirect_url 摘要)。
- 响应（传输成功 `200`/传输失败 `500`）：
```json
{ "success": true, "openid": "", "appid": "",
  "ok": true, "ret": 0, "errmsg": null,
  "redirect_url": "",
  "is_recent_has_auth": 0, "is_slient_auth": 0,
  "scope_list": [ { "scope":"snsapi_userinfo", "desc":"", "auth_state":0, "ext_desc":"", "auth_sub_desc":"" } ],
  "avatar_list": [ { "id":"", "nickname":"", "avatarurl":"" } ] }
```
  - `success` 只表示 shortcloud 传输成功；业务成败看 `ret`：`0`=成功，`-1`=通用错误，`10010`=scope 为空，`10011`=redirect_uri 为空，`-12001`=invalid scope。

### POST /api/yyb/oauth-authorize-confirm ★本次新增（公众号 OAuth2 确认授权）
- Body：`{ openid, appid, oauth_url, opt?, avatar_id?, redirect_uri? }`。
  - `oauth_url`：oauth-authorize 返回的 URL 或原始授权 URL；`opt`：0=确认授权。
  - 缺 openid/appid → `400`；缺 oauth_url → `400 "oauth_url required"`；未绑定 → `403`。
- 走 `/ilink/ilinkapp/mm/bizoauth/oauth_authorize_confirm`（cmdid=4313）。记 call_records(action='oauth-confirm')，其 `code` 列自动从 `redirect_url` 抽取 `code=` 参数。
- 响应：
```json
{ "success": true, "openid": "", "appid": "",
  "ok": true, "ret": 0, "errmsg": null,
  "redirect_url": "https://<回调>?code=<网页授权code>&state=<state>",
  "scope_list": [], "avatar_list": [] }
```
  - `redirect_url` 中的 `code` 即公众号网页授权 code，可用于换取 access_token。

### wx_server 兼容：POST /wx/code（wxCompat.js，requireCompatLicense）
- Body：`{ openid|account|wcsid, appid|appId }`（三选一取 openid）。
- `getCodeForLicense`：缺 openid/appid → `400`；未绑定 → `<code|403>`；取码。
- 响应经 `toWxServerCodeResponse` 整形（**形状必须字节兼容旧 wx_server**）：
  - 成功：
```json
{ "status": true, "success": true, "code": "<code>", "data": { "code":"<code>", "loginCode":"<code>", "openid":"", "appid":"" } }
```
  - 失败：
```json
{ "status": false, "success": false, "code": -1, "message": "<error>", "msg": "<error>", "data": { <原始 result> } }
```
  - HTTP 状态码 = getCodeForLicense 返回的 status（200/400/403/500）。

---

## 11. pureproto 调用接口（跳过内部，仅说明契约）

`routes/code.js` 与 `services/codeService.js` 通过 `pureproto/account.js` 的 `getCodeForOpenid(openid, appid)` 取码：

```
getCodeForOpenid(openid, appid) -> Promise<{ success:boolean, code?:string, error?:string, openid, appid }>
```
- 内部：`getYybAccount(openid)`（读 yyb-accounts.json）→ 若无 → `{success:false, error:'account not found (未扫码或已删除)'}`。
- 调 `pureproto/index.js` 的 `fetchCode(account, appid)`，`account = { guid, accesstoken, proxyUrl }`。
- 失败时自动 `refreshYybAccount(openid)` 后重试一次。
- `fetchCode` 返回 **32 位 wx.login code 字符串**（内部走 MMTLS 握手 + cmd 登录 + shortcloud 0-RTT，你已有 Python 版）。

> **Python 复刻**：`getCodeForOpenid` 用你现有的 Python 纯协议模块替换；输入 `(openid, appid)`，从 `yyb-accounts.json` 读 `{guid, accesstoken, proxyUrl}`，输出上述结构。刷新失败重试逻辑需保留。

---

## 12. 项目 / 面板 / 内置项目 / crypto / logger / 静态托管

### 12.1 `/api/projects/*`（requireUser）

**projectView**（简版，列表）：`{ id, name, summary, icon, status, panelType, submitPanels:[], builtin:'', sortOrder, updatedAt }`。
**projectView**（全版）：额外 `{ intro, tutorial, runConfig:{...}, createdAt }`。

- **GET /api/projects** → `{success:true, projects:[简版, status='on' 的]}`（sort_order 降序, id 降序）。
- **GET /api/projects/:id** → `getPublished`（仅 status='on'）→ 全版；未上架 `404 "项目不存在或未上架"`。
- **POST /api/projects/:id/run**：
  - 项目须已上架，否则 404。
  - 若 `runConfig.builtin` 命中 BUILTIN_RUNNERS（如 `'jd-code-login'`）：
    - Body `{ params:{ openid, proxyUrl? } }`。缺 openid → `400 "请选择要使用的微信账号"`。
    - 无 license → `403 "尚未分配授权码，请联系管理员"`。
    - 调 runner（见 §9 京东流程）；记 call_records(action=builtinKey)。
    - 失败 `400 {success:false, error, errCode?}`；成功 `{success:true, result:{ ok:true, jdCookie, ptPin, ptKey, code }}`。
  - 否则走面板：`panelType` 未配 → `400 "该项目未配置面板运行方式"`；未配置该面板 → `400 "请先在「面板设置」中配置并测试 <type> 面板"`；`runOnPanel(panelType, config, runConfig, params)`；记录；成功 `{success:true, result}`；异常 `502 {success:false,error}`。
- **POST /api/projects/:id/submit**（写面板环境变量）：
  - Body（或 `body.params`）：`{ envName, value, panels:[panelType...] }`。
  - envName 须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，否则 `400`；value 空 → `400 "缺少要提交的值"`。
  - 只保留 `p.runConfig.submitPanels` 允许且用户已配的面板；无有效目标 → `400 "未选择有效的提交面板"`。
  - 逐面板 `submitEnv(pt, config, {name:envName, value, remarks:'呆呆Code 自动提交'})`；记录 call_records(action='submit-panel')。
  - 响应：`{ success:true, results:[ {panel, ok, message?}|{panel, ok:false, error} ] }`。

### 12.2 `/api/panels/*`（requireUser）
面板类型：`PANEL_TYPES = ['qinglong','daidai']`。密钥永不回显。

**maskView**：`{ panelType, baseUrl, clientId, hasSecret:bool, lastTestAt, lastTestOk:bool, updatedAt }`。

- **GET /api/panels** → `{success:true, panels:[maskView...]}`（按 panel_type 排序）。
- **GET /api/panels/:type** → `{success:true, panel:maskView|null}`。
- **PUT /api/panels/:type** → Body `{ baseUrl, clientId, clientSecret }`。
  - saveConfig：baseUrl 须 `^https?://`，否则 `400 "面板地址需以 http(s):// 开头"`；type 非法 → `"未知面板类型：<t>"`。
  - clientSecret 为空且已存在 → 保留旧密钥；否则 `encryptSecret`。upsert。返回 `{success:true, panel:maskView}`。
- **POST /api/panels/:type/test** → Body `{ baseUrl?, clientId?, clientSecret? }`。
  - 若传了 baseUrl+（secret 或 clientId）用提交凭据（secret 空回退已存密钥），否则用已存配置。
  - 无有效配置 → `{success:true, ok:false, error:"请先填写面板地址与密钥"}`。
  - `testConnection`；markTest 更新 last_test。响应 `{ success:true, ok:bool, message?, note?, error? }`。
- **DELETE /api/panels/:type** → 删除。`{success:true}`。

### 12.2.1 青龙面板外部调用（panels/qinglong.js）
`base = baseUrl 去尾部斜杠`。token 缓存（key=base|clientId，提前 60s 失效）。
- **鉴权**：`GET {base}/open/auth/token?client_id=<>&client_secret=<>` → 期望 `{code:200, data:{token, expiration(秒)}}`。失败 `"青龙鉴权失败：<msg|HTTP N>"`。之后 `Authorization: Bearer <token>`。
- **testConnection**：`GET {base}/open/envs?searchValue=` → 期望 `code:200`，否则 `"青龙接口校验失败：..."`。返回 `{ok:true, message:"青龙连接成功"}`。
- **run 模式**：
  - `run_cron_by_id`：`PUT {base}/open/crons/run` body `[id]`。
  - `run_cron_by_name`：`GET {base}/open/crons?searchValue=<name>` 找匹配 → `PUT /open/crons/run` body `[cron.id]`。
  - `add_env`：转 upsertEnv。
- **upsertEnv**：`GET {base}/open/envs?searchValue=<name>` → 存在则 `PUT {base}/open/envs` body `{id,name,value,remarks}` 再 `PUT {base}/open/envs/enable` body `[id]`；不存在则 `POST {base}/open/envs` body `[{name,value,remarks}]`。期望 `code:200`。
- 所有请求 timeout 15000，`validateStatus:()=>true`。

### 12.2.2 呆呆面板外部调用（panels/daidai.js）
`config.clientId = App Key`，`config.clientSecret = App Secret`。
- **鉴权**：`POST {base}<token路径>` body `{ app_key, app_secret }`（JSON），token 路径依次尝试 `/api/v1/open-api/token`、`/api/open-api/token`、`/open-api/token`。深度查找响应中 `access_token`（+`expires_in`，默认 86400 秒）。全失败 → `"呆呆面板鉴权失败：<lastErr>（请确认面板地址、App Key / App Secret 正确）"`。之后 `Authorization: Bearer <token>`。
- **testConnection** → `{ok:!!token, message:"呆呆面板连接成功"}`。
- **run（run_task_by_id / run_cron_by_id）**：`PUT {base}/api/v1/tasks/{id}/run`（回退 `/api/tasks/{id}/run`），body `{}`。2xx → `{ok:true, message:"已触发任务 #id", detail}`，否则 `{ok:false, message:"触发任务失败：..."}`。
- **upsertEnv**：`GET {base}/api/v1/envs?search=<name>&keyword=<name>` → 深度找环境数组，存在（有 id）则 `PUT {base}/api/v1/envs/{id}` body `{name,value,remarks,enabled:true}`；否则 `POST {base}/api/v1/envs` body `{name,value,remarks}`。非 2xx 抛错（取 message/msg）。
- timeout 15000，validateStatus true。

### 12.3 内置项目：京东 Code 登录（projects/jdCodeLogin.js + jdSilentAuth.js）
**seedBuiltinProjects**（管理库）：若无 `run_config LIKE '%jd-code-login%'` 的项目则插入，`status='on'`，`sort_order=100`：
```json
runConfig = { "builtin":"jd-code-login", "appid":"wx73247c7819d61796", "submitPanels":["qinglong","daidai"], "envName":"JD_COOKIE" }
```
name="京东 Code 登录获取 Cookie"，summary/intro/tutorial 为固定 markdown（见源码），icon="🛒"。已存在则补齐 submitPanels/envName。

**runJdCodeLogin({license, openid, appid, proxyUrl})**：
1. `getCodeForLicense({license, openid, appid:appid||'wx73247c7819d61796'})` 取京东小程序 wx.login code。失败 → `{ok:false, stage:'get-code', error}`。
2. `runJdSilentAuthLogin({code, proxyUrl})` 换京东 Cookie。
3. 错误映射：errCode 35 → "该微信号尚未绑定京东账号..."；命中风控关键词 → "疑似触发风控：..."。
4. 成功 → `{ ok:true, jdCookie:"pt_key=..;pt_pin=..;", ptPin, ptKey, code }`。

**runJdSilentAuthLogin**（外部调用）：
- `POST https://wxapplogin.m.jd.com/cgi-bin/jxpp/silentauthlogin`（`application/x-www-form-urlencoded`）。
- Form：`code, eid_token, goToLogin='true', returnurl='/pages/login/web-view/web-view', wxappid='wx73247c7819d61796', appid='599', client_ver='2.0.2', ts=<秒>, sign`。
- `sign = md5(appid + wxappid + client_ver + ts + cmd(52) + sub_cmd(1) + gsalt)`，`gsalt='sb2cwlYyaCSN1KUv5RHG3tmqxfEb8NKN'`（标准 MD5，ts 用当前秒）。
- Header：`X-WECHAT-HOSTSIGN: {"noncestr","timestamp","signature"}`(JSON)、UA(iPhone MicroMessenger)、Referer、`cookie: guid=; pt_pin=; pt_key=; pt_token=`。
- 设备参数（可被环境变量覆盖）：`JD_APPID, JD_WXAPPID, JD_CLIENT_VER, JD_GSALT, JD_CMD, JD_SUB_CMD, JD_EID_TOKEN, JD_RETURN_URL, JD_HOSTSIGN_NONCESTR, JD_HOSTSIGN_TIMESTAMP, JD_HOSTSIGN_SIGNATURE, JD_USER_AGENT, JD_REFERER`（默认值见源码 DEFAULTS，含一组会过期的抓包值）。
- 从响应 JSON（多层 pickLoginPayload）+ Set-Cookie 提取 `pt_key`/`pt_pin`，拼 `pt_key=..;pt_pin=..;`。
- 成功判定：`status 2xx && errCode===0 && jdCookie 非空`。返回 `{ok, status, errCode, errMsg, jdCookie, ptKey, ptPin, raw}`。

### 12.4 静态前端托管（yybApiServer.js）
- **管理端 SPA**：挂 `/admin`（`dist/web/admin` 或 `YYB_ADMIN_DIST`/相对路径候选）。SPA history 回退：`/admin` 及子路径 GET 未命中静态文件 → 回 `admin/index.html`。
- **用户端 SPA**：`dist/web/user`（或 `YYB_USER_DIST`）挂根 `/`。history 回退：非 API 的 GET → 回 `user/index.html`。判定为 API 的前缀：`/api`、`/api/*`、`/wx`、`/wx/*`、`/admin`、`/admin/*`。
- 未构建时回退旧 `yyb-web-console`。
- 静态选项：`index.html` 设 `Cache-Control: no-cache`，其它 etag 缓存。
- Python(FastAPI) 复刻：`StaticFiles` + 显式 catch-all 路由回退 index.html（排除上述 API 前缀）。

### 12.5 logger.js（行为）
- 结构化事件写入 `<userData>/logs/yyb-<YYYY-MM-DD>.log`（按日期分卷，追加）。
- 行格式：`<本地时间 YYYY-MM-DD HH:mm:ss.SSS> [<LEVEL 7宽>] [<cat>/<action>] <msg> | <JSON fields>`。
- 内存 ring buffer 3000 条（供 UI 拉历史，当前 HTTP 路由未直接暴露）。
- 敏感字段脱敏（maskSensitive）：`authorization/cookie/accesstoken/refreshtoken/token/admintoken` → `***<后4位>`；`proxyurl` → 隐藏账密；`licensekey` → `…<后4位>`。
- Levels：`debug|info|success|warn|error`。分类：api/login/account/hook/cloud/wmpf/sandbox/license/system。
- Python 复刻：等价日志即可（按日文件 + 脱敏），不影响 HTTP 契约。

---

## 13. 环境变量清单

| 变量 | 用途 | 默认 |
|---|---|---|
| `YYB_API_PORT` | HTTP 端口 | 18273 |
| `YYB_USERDATA` | 数据根目录 | `<project>/data` |
| `YYB_ADMIN_TOKEN` | 管理员令牌 | 随机 24 字节 hex，落 `yyb-admin-token.txt` |
| `YYB_SECRET_KEY` | 面板密钥 AES 主密钥 | 随机落 `yyb-secret.key` |
| `YYB_CORS_ORIGINS` | CORS 白名单（逗号分隔） | 空（同源） |
| `YYB_TRUST_PROXY` | Express trust proxy | false |
| `YYB_USER_DIST`/`YYB_ADMIN_DIST`/`YYB_WEB_CONSOLE_DIR` | 前端目录覆盖 | 相对 dist |
| `YYB_DEVICE_ID` | pureproto 设备 id（hex64） | 随机落 `ilink-device-id` |
| `JD_*` | 京东静默授权设备参数 | 见 §12.3 |

---

## 14. 复刻注意事项（易错点）

1. **时间戳全部为 `Date.now()` 毫秒整数**，session/license/expiresAt 一致。
2. **cookie base64url**（无 `+/=`），会话 id 32 字节；secret box iv/tag 顺序为 `iv||tag||ct`，前缀 `v1:`。
3. **bcrypt rounds=12**，管理员初始密码 = 管理员令牌，须能被 bcrypt 校验。
4. **csrfGuard**：带 cookie 的变更请求必须 `X-Requested-With: XMLHttpRequest`；机器令牌（授权码/admin token）调用豁免。
5. **license_key、username NOCASE 唯一**（大小写不敏感），license_key 存储/比较统一 upper。
6. **/wx/code 响应形状**（status/success/code/message/msg/data 双字段）必须逐字段保留。
7. **get-codes 部分成功返回 207**；get-code 失败返回 500（但 body 仍带 openid/appid）。
8. **tolerantJson**：任何无法解析的 body → `{}`，绝不 400/422。
9. **授权码校验错误码**：401（缺失/无效）、403（禁用/过期/超容量/未绑定）。
10. **扫码长轮询恒直连**，只有 code 兑换与 userinfo 走代理，且代理不持久化。

---

## 15. 端点清单（57 个）

```
POST   /api/auth/register
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me
POST   /api/admin/login
POST   /api/admin/logout
GET    /api/admin/me
POST   /api/admin/change-password
GET    /api/admin/stats
GET    /api/admin/users
GET    /api/admin/users/:id
POST   /api/admin/users/:id/approve
POST   /api/admin/users/:id/enable
POST   /api/admin/users/:id/disable
DELETE /api/admin/users/:id
POST   /api/admin/users/:id/reset-password
POST   /api/admin/users/:id/license
POST   /api/admin/users/:id/license/status
DELETE /api/admin/users/:id/license
GET    /api/admin/licenses
GET    /api/admin/call-records
GET    /api/admin/audit
GET    /api/admin/projects
GET    /api/admin/projects/:id
POST   /api/admin/projects
PUT    /api/admin/projects/:id
POST   /api/admin/projects/:id/shelf
DELETE /api/admin/projects/:id
GET    /api/licenses
POST   /api/licenses
GET    /api/licenses/:key
PATCH  /api/licenses/:key
POST   /api/licenses/:key/disable
POST   /api/licenses/:key/enable
DELETE /api/licenses/:key
POST   /api/licenses/:key/unbind
POST   /api/licenses/:key/bind
POST   /api/login/start
POST   /api/login/stop
GET    /api/login/status
GET    /api/accounts
POST   /api/accounts/refresh
POST   /api/accounts/delete
DELETE /api/accounts/:openid
POST   /api/yyb/get-code
POST   /api/yyb/get-codes
POST   /api/yyb/invoke-cloud
POST   /api/yyb/get-phone
POST   /api/yyb/oauth-authorize
POST   /api/yyb/oauth-authorize-confirm
GET    /api/projects
GET    /api/projects/:id
POST   /api/projects/:id/run
POST   /api/projects/:id/submit
GET    /api/panels
GET    /api/panels/:type
PUT    /api/panels/:type
POST   /api/panels/:type/test
DELETE /api/panels/:type
GET    /api            (信息页 info.js)
POST   /wx/code
```
（另有静态 SPA 路由：`/admin/*`、`/*` history 回退。）
