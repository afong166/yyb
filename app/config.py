"""全局配置：路径、密钥、常量。data/ 为运行时数据目录（DB、令牌、密钥、日志、二维码）。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 项目根（含 server.py）
DATA_DIR = Path(os.environ.get("YYB_USERDATA") or (ROOT / "data"))
DB_PATH = DATA_DIR / "app.db"
LOG_DIR = DATA_DIR / "logs"
QR_DIR = DATA_DIR / "qr"

# 前端构建产物（Python 直接托管）
DIST_USER = ROOT / "dist" / "web" / "user"
DIST_ADMIN = ROOT / "dist" / "web" / "admin"

API_PORT = int(os.environ.get("YYB_API_PORT", "18273"))
API_HOST = os.environ.get("YYB_HOST", "0.0.0.0")


def _flag(name: str, default: str = "") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


# 会话 Cookie 是否加 Secure（默认开，仅 HTTPS 下回传；线上已是 HTTPS）。本地 HTTP 调试需设 YYB_COOKIE_SECURE=0
COOKIE_SECURE = _flag("YYB_COOKIE_SECURE", "1")
# 对接青龙/呆呆面板时是否校验 TLS 证书（默认关：面板多为自托管，常用免费证书或反代域名的自签
# 证书，开启校验会直接连不上；需要严格校验的可设 YYB_PANEL_TLS_VERIFY=1 开启）。
PANEL_TLS_VERIFY = _flag("YYB_PANEL_TLS_VERIFY", "0")
# 面板地址是否允许指向内网/环回（127.0.0.1、局域网、docker 网络）。默认开：青龙/呆呆几乎都
# 自托管在私网，一律按 SSRF 拦截会导致面板无法保存/使用。多租户对外部署可设 YYB_PANEL_ALLOW_PRIVATE=0 收紧。
PANEL_ALLOW_PRIVATE = _flag("YYB_PANEL_ALLOW_PRIVATE", "1")
# 是否信任反向代理/CDN 传来的真实 IP 头（默认不信任，直接用对端 IP，防伪造 IP 绕过登录限流/伪造审计）。
# 仅在确实位于会覆盖 CLIENT_IP_HEADER 的可信 CDN（如 EdgeOne）后方，才设 YYB_TRUST_PROXY=1。
TRUST_PROXY = _flag("YYB_TRUST_PROXY", "0")
# 真实客户端 IP 头名（仅 TRUST_PROXY=1 时生效）。默认腾讯云 EdgeOne 的 EO-Client-IP；
# Cloudflare 设 CF-Connecting-IP；设为空则回退到 X-Forwarded-For 的最后一跳。
CLIENT_IP_HEADER = (os.environ.get("YYB_CLIENT_IP_HEADER", "EO-Client-IP") or "").strip()

# 取码相关（应用宝小程序 host appid，pure_wxcode 默认值一致）
DEFAULT_HOST_APPID = "wxd44977328b36e647"

# 调用记录 / 审计日志保留天数（超期自动清理，防单库无限膨胀）。设 0 关闭清理、永久保留。
try:
    RECORD_RETENTION_DAYS = int(os.environ.get("YYB_RECORD_RETENTION_DAYS", "90") or 0)
except ValueError:
    RECORD_RETENTION_DAYS = 90

for _d in (DATA_DIR, LOG_DIR, QR_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _load_or_create(path: Path, gen) -> str:
    if path.exists():
        v = path.read_text(encoding="utf-8").strip()
        if v:
            return v
    v = gen()
    path.write_text(v, encoding="utf-8")
    return v


# 服务端密钥：签名会话 cookie + secretBox 加密（面板密钥等）。持久化，重启不变。
SECRET_KEY = _load_or_create(DATA_DIR / "secret.key", lambda: secrets.token_hex(32))

# 管理员令牌：可用环境变量固定，否则随机生成并持久化到文件（首启动打印）。
ADMIN_TOKEN = os.environ.get("YYB_ADMIN_TOKEN") or _load_or_create(
    DATA_DIR / "yyb-admin-token.txt", lambda: secrets.token_hex(24)
)
