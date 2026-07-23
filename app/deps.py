"""FastAPI 鉴权依赖：用户会话 cookie / 管理员 / 授权码(外部脚本)。"""
from __future__ import annotations

from fastapi import HTTPException, Request

from . import auth, db


def get_user_id(request: Request) -> int | None:
    d = auth.verify_token(request.cookies.get(auth.USER_COOKIE, ""))
    if d and d.get("uid"):
        u = db.query_one("SELECT id FROM users WHERE id=? AND status='active'", (d["uid"],))
        if u:
            return u["id"]
    return None


def require_user(request: Request) -> int:
    uid = get_user_id(request)
    if not uid:
        raise HTTPException(401, "未登录或会话已过期")
    return uid


def is_admin(request: Request) -> bool:
    d = auth.verify_token(request.cookies.get(auth.ADMIN_COOKIE, ""))
    if d and d.get("admin"):
        return True
    # 只接受请求头传令牌：避免管理员令牌出现在 URL 中被访问日志/浏览器历史/Referer 泄漏。
    tok = request.headers.get("X-Admin-Token")
    return auth.check_admin_token(tok or "")


def require_admin(request: Request) -> bool:
    if not is_admin(request):
        raise HTTPException(401, "需要管理员登录")
    return True


def _license_key_from(request: Request) -> str:
    key = request.headers.get("X-License-Key") or request.query_params.get("licenseKey") or ""
    ah = request.headers.get("Authorization", "")
    if not key and ah.startswith("Bearer "):
        key = ah[7:].strip()
    return key


def resolve_actor(request: Request, body_auth: str = "") -> int:
    """返回操作者 user_id：优先用户会话 cookie，其次授权码(header/query/body)。"""
    uid = get_user_id(request)
    if uid:
        return uid
    key = _license_key_from(request) or body_auth
    if not key:
        raise HTTPException(401, "需要登录或提供授权码")
    lic = db.query_one("SELECT * FROM licenses WHERE license_key=? COLLATE NOCASE AND status='active'", (key,))
    if not lic:
        raise HTTPException(403, "授权码无效或已禁用")
    if lic["expires_at"] and lic["expires_at"] < db.now():
        raise HTTPException(403, "授权码已过期")
    return lic["user_id"]


def account_openids(user_id: int) -> set[str]:
    rows = db.query("SELECT openid FROM accounts WHERE user_id=?", (user_id,))
    return {r["openid"] for r in rows}
