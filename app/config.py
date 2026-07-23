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
# 对接青龙/呆呆面板时是否校验 TLS 证书（默认开；仅自签环境可用 YYB_PANEL_TLS_VERIFY=0 关闭）。
PANEL_TLS_VERIFY = _flag("YYB_PANEL_TLS_VERIFY", "0")
# 是否信任反向代理/CDN 传来的真实 IP 头（默认不信任，直接用对端 IP，防伪造）。YYB_TRUST_PROXY=1
TRUST_PROXY = _flag("YYB_TRUST_PROXY", "1")
# 真实客户端 IP 头名（仅 TRUST_PROXY=1 时生效）。默认腾讯云 EdgeOne 的 EO-Client-IP；
# Cloudflare 设 CF-Connecting-IP；设为空则回退到 X-Forwarded-For 的最后一跳。
CLIENT_IP_HEADER = (os.environ.get("YYB_CLIENT_IP_HEADER", "EO-Client-IP") or "").strip()

# 取码相关（应用宝小程序 host appid，pure_wxcode 默认值一致）
DEFAULT_HOST_APPID = "wxd44977328b36e647"

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
