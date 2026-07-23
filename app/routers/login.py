"""/api/login/* — 微信扫码添加账号。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from .. import db, deps, models, yyblogin
from ..logs import actor_name, event
from ..util import client_ip, get_body, guard_public_url

_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")

router = APIRouter()

# checkQr 是前端高频轮询接口：只在扫码状态变化时打一行，重复轮询不刷屏。
_last_qr_status: dict[str, str] = {}


def _log_qr_change(uid: int, ip: str, sid: str, status: str) -> None:
    key = f"{sid}"
    if _last_qr_status.get(key) == status:
        return
    _last_qr_status[key] = status
    event("checkQr", "扫码状态变化", project=actor_name(uid), 来源=ip, session=sid, 状态=status)
    # 终态后清理，避免字典无限增长
    if status in ("success", "cancelled", "expired", "rejected", "error", "无会话"):
        _last_qr_status.pop(key, None)


@router.post("/start")
async def start(request: Request):
    uid = deps.resolve_actor(request)
    lic = db.query_one("SELECT max_users FROM licenses WHERE user_id=?", (uid,))
    max_users = lic["max_users"] if lic else 0
    used = models.used_count(uid)
    if max_users and used >= max_users:
        raise HTTPException(403, f"授权数量已用尽 ({used}/{max_users})，请联系管理员扩容")
    b = await get_body(request)
    proxy_url = (b.get("proxyUrl") or "").strip()
    if proxy_url:
        guard_public_url(proxy_url, allow_schemes=_PROXY_SCHEMES)  # 防止把服务端当探针连内网
    # 拉二维码是同步走代理的：代理不可用时把真实原因回给前端，别让它被统一 500「internal error」吞掉。
    try:
        meta = await asyncio.to_thread(yyblogin.start_login, uid, proxy_url)
    except HTTPException:
        raise
    except Exception as e:
        hint = "（代理不可用？确认服务器能出网访问该 SOCKS5；IPv6 代理要求服务器具备 IPv6 出口）" if proxy_url else ""
        raise HTTPException(400, f"发起扫码失败：{type(e).__name__}: {e}{hint}")
    event("startQr", "发起扫码登录", project=actor_name(uid), 来源=client_ip(request),
          session=meta.get("sessionId"))
    return {"success": True, **meta}


@router.get("/status")
async def status(request: Request):
    uid = deps.resolve_actor(request)
    ip = client_ip(request)
    sid = request.query_params.get("sessionId")
    if sid:
        s = yyblogin.get_status(sid)
        if not s:
            _log_qr_change(uid, ip, sid, "无会话")
            return {"success": True, "running": False, "status": None, "uuid": None}
        if s["user_id"] != uid:
            raise HTTPException(403, "session does not belong to this user")
        _log_qr_change(uid, ip, sid, s["status"])
        out = {"success": True, "running": s["running"], "status": s["status"],
               "error": s["error"], "uuid": s["uuid"], "qrcodeDataUrl": s["qrcodeDataUrl"]}
        if s["account"]:
            out["account"] = s["account"]
        return out
    sessions = yyblogin.list_sessions(uid)
    return {"success": True, "running": any(x["running"] for x in sessions),
            "sessionCount": len(sessions), "sessions": sessions}


@router.post("/stop")
async def stop(request: Request):
    b = await get_body(request)
    sid = b.get("sessionId") or None
    yyblogin.stop_login(sid)
    event("stopQr", "停止扫码登录", 来源=client_ip(request), session=sid or "all")
    return {"success": True}
