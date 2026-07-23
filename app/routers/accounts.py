"""/api/accounts/* — 微信账号（限本账号/授权码名下）。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from .. import db, deps, models, yyblogin
from ..util import get_body

router = APIRouter()


def _max_users(uid: int) -> int:
    lic = db.query_one("SELECT max_users FROM licenses WHERE user_id=?", (uid,))
    return lic["max_users"] if lic else 0


@router.get("")
async def list_accounts(request: Request):
    uid = deps.resolve_actor(request)
    rows = db.query("SELECT * FROM accounts WHERE user_id=? ORDER BY logged_at DESC", (uid,))
    accounts = [models.account_view(a) for a in rows]
    err = sum(1 for a in rows if a["status"] == "error")
    cur = next((a["openid"] for a in rows if a["is_current"]), "")
    return {"success": True, "current": cur, "total": len(rows), "active": len(rows) - err,
            "error": err, "maxUsers": _max_users(uid), "accounts": accounts}


@router.post("/refresh")
async def refresh(request: Request):
    uid = deps.resolve_actor(request)
    b = await get_body(request)
    openid = b.get("openid") or ""
    if not openid:
        raise HTTPException(400, "openid required")
    a = db.query_one("SELECT * FROM accounts WHERE openid=? AND user_id=?", (openid, uid))
    if not a:
        raise HTTPException(403, "openid not bound to this license")
    r = await asyncio.to_thread(yyblogin.refresh_account, openid)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "续期失败")
    a = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
    return {"success": True, "account": {"openid": openid, "nickname": a["nickname"],
                                         "expireAt": a["expire_at"], "expiresIn": a["expires_in"]}}


async def _delete(request: Request, openid: str):
    uid = deps.resolve_actor(request)
    if not openid:
        raise HTTPException(400, "openid required")
    a = db.query_one("SELECT openid FROM accounts WHERE openid=? AND user_id=?", (openid, uid))
    if not a:
        raise HTTPException(403, "openid not bound to this license")
    db.execute("DELETE FROM accounts WHERE openid=?", (openid,))
    from .. import codebridge
    codebridge.invalidate(openid)
    return {"success": True, "openid": openid}


@router.post("/delete")
async def delete_post(request: Request):
    b = await get_body(request)
    return await _delete(request, b.get("openid") or "")


@router.delete("/{openid}")
async def delete_del(request: Request, openid: str):
    return await _delete(request, openid)
