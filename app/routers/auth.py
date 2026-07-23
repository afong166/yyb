"""/api/auth/* — 用户注册/登录/登出/我。会话用签名 cookie。"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, Response

from .. import auth, db, deps, models, ratelimit
from ..util import get_body, set_user_cookie, clear_user_cookie, client_ip

router = APIRouter()
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


def _has_license(uid: int) -> bool:
    return db.query_one("SELECT id FROM licenses WHERE user_id=?", (uid,)) is not None


@router.post("/register")
async def register(request: Request):
    b = await get_body(request)
    username = (b.get("username") or "").strip()
    password = b.get("password") or ""
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "用户名需为 3-32 位字母/数字/下划线/点/连字符")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if db.query_one("SELECT id FROM users WHERE username=? COLLATE NOCASE", (username,)):
        raise HTTPException(400, "用户名已存在")
    uid = db.execute(
        "INSERT INTO users(username,password_hash,status,role,created_at) VALUES(?,?,'pending','user',?)",
        (username, auth.hash_password(password), db.now()),
    )
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    return {"success": True, "user": models.public_user(u), "message": "注册成功，请等待管理员审核后登录"}


@router.post("/login")
async def login(request: Request, response: Response):
    b = await get_body(request)
    username = (b.get("username") or "").strip()
    password = b.get("password") or ""
    rl_key = f"user-login:{client_ip(request)}:{username.lower()}"
    wait = ratelimit.locked_seconds(rl_key)
    if wait:
        raise HTTPException(429, f"登录尝试过于频繁，请 {wait} 秒后再试")
    u = db.query_one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))
    if not u or not auth.verify_password(password, u["password_hash"]):
        ratelimit.record_fail(rl_key)
        raise HTTPException(401, "用户名或密码错误")
    ratelimit.clear(rl_key)
    if u["status"] == "pending":
        raise HTTPException(403, "账号待管理员审核")
    if u["status"] == "disabled":
        raise HTTPException(403, "账号已被禁用")
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), u["id"]))
    set_user_cookie(response, u["id"])
    u = db.query_one("SELECT * FROM users WHERE id=?", (u["id"],))
    return {"success": True, "user": models.public_user(u), "hasLicense": _has_license(u["id"])}


@router.post("/logout")
async def logout(response: Response):
    clear_user_cookie(response)
    return {"success": True}


@router.get("/me")
async def me(request: Request):
    uid = deps.require_user(request)
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    lic = db.query_one("SELECT * FROM licenses WHERE user_id=?", (uid,))
    return {"success": True, "user": models.public_user(u), "license": models.license_view(lic)}
