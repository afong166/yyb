"""/api/panels/* — 用户面板配置（青龙/呆呆）。密钥加密存、永不回显。"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from .. import auth, db, deps, models, panels_ext
from ..util import get_body, guard_public_url

router = APIRouter()
PANEL_TYPES = ("qinglong", "daidai")


def get_panel_config(uid: int, ptype: str) -> dict | None:
    p = db.query_one("SELECT * FROM panels WHERE user_id=? AND panel_type=?", (uid, ptype))
    if not p:
        return None
    return {"baseUrl": p["base_url"], "clientId": p["client_id"],
            "clientSecret": auth.secretbox_decrypt(p["client_secret_enc"])}


@router.get("")
async def list_panels(request: Request):
    uid = deps.require_user(request)
    rows = db.query("SELECT * FROM panels WHERE user_id=? ORDER BY panel_type", (uid,))
    return {"success": True, "panels": [models.panel_view(p) for p in rows]}


@router.get("/{ptype}")
async def get_panel(request: Request, ptype: str):
    uid = deps.require_user(request)
    p = db.query_one("SELECT * FROM panels WHERE user_id=? AND panel_type=?", (uid, ptype))
    return {"success": True, "panel": models.panel_view(p) if p else None}


@router.put("/{ptype}")
async def save_panel(request: Request, ptype: str):
    uid = deps.require_user(request)
    if ptype not in PANEL_TYPES:
        raise HTTPException(400, f"未知面板类型：{ptype}")
    b = await get_body(request)
    base_url = (b.get("baseUrl") or "").strip()
    if not re.match(r"^https?://", base_url):
        raise HTTPException(400, "面板地址需以 http(s):// 开头")
    guard_public_url(base_url)  # SSRF 防护：禁止指向内网/环回地址
    client_id = b.get("clientId") or ""
    secret = b.get("clientSecret") or ""
    existing = db.query_one("SELECT * FROM panels WHERE user_id=? AND panel_type=?", (uid, ptype))
    if secret:
        enc = auth.secretbox_encrypt(secret)
    else:
        enc = existing["client_secret_enc"] if existing else ""
    if existing:
        db.execute("UPDATE panels SET base_url=?,client_id=?,client_secret_enc=?,updated_at=? WHERE id=?",
                   (base_url, client_id, enc, db.now(), existing["id"]))
    else:
        db.execute("INSERT INTO panels(user_id,panel_type,base_url,client_id,client_secret_enc,created_at,updated_at)"
                   " VALUES(?,?,?,?,?,?,?)", (uid, ptype, base_url, client_id, enc, db.now(), db.now()))
    p = db.query_one("SELECT * FROM panels WHERE user_id=? AND panel_type=?", (uid, ptype))
    return {"success": True, "panel": models.panel_view(p)}


@router.post("/{ptype}/test")
async def test_panel(request: Request, ptype: str):
    uid = deps.require_user(request)
    if ptype not in PANEL_TYPES:
        raise HTTPException(400, f"未知面板类型：{ptype}")
    b = await get_body(request)
    existing = db.query_one("SELECT * FROM panels WHERE user_id=? AND panel_type=?", (uid, ptype))
    base_url = (b.get("baseUrl") or (existing["base_url"] if existing else "")).strip()
    client_id = b.get("clientId") if b.get("clientId") is not None else (existing["client_id"] if existing else "")
    secret = b.get("clientSecret") or (auth.secretbox_decrypt(existing["client_secret_enc"]) if existing else "")
    if not base_url or (not secret and not client_id):
        return {"success": True, "ok": False, "error": "请先填写面板地址与密钥"}
    guard_public_url(base_url)  # SSRF 防护：禁止指向内网/环回地址
    res = await panels_ext.test_connection(ptype, {"baseUrl": base_url, "clientId": client_id, "clientSecret": secret})
    if existing:
        db.execute("UPDATE panels SET last_test_at=?, last_test_ok=? WHERE id=?",
                   (db.now(), 1 if res.get("ok") else 0, existing["id"]))
    return {"success": True, **res}


@router.delete("/{ptype}")
async def delete_panel(request: Request, ptype: str):
    uid = deps.require_user(request)
    db.execute("DELETE FROM panels WHERE user_id=? AND panel_type=?", (uid, ptype))
    return {"success": True}
