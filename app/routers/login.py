"""/api/login/* — 微信扫码添加账号。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from .. import db, deps, models, shortproxy, yyblogin
from ..logs import actor_name, event
from ..util import client_ip, get_body

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
    # 无授权码(max_users=0)一律视为 0 额度 → 拒绝；与 licenses.bind 的 `>= max_users` 语义一致，
    # 修复此前 `max_users and ...` 在无授权码时短路导致的「无限加号」配额绕过。
    if used >= max_users:
        raise HTTPException(403, f"授权数量已用尽 ({used}/{max_users})，请联系管理员扩容"
                            if max_users else "尚未分配授权码，请联系管理员开通后再添加账号")
    b = await get_body(request)
    try:
        proxy_meta = await asyncio.to_thread(
            shortproxy.resolve_params, uid, "", b, "direct",
            shortproxy.TIME_CODE_QR_LOGIN,
        )
    except (ValueError, RuntimeError, HTTPException) as e:
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        raise HTTPException(400, detail)
    proxy_url = proxy_meta["proxyUrl"]
    login_source = int(b.get("loginSource") or b.get("type") or 1)  # 1=应用宝, 2=手游助手
    # 拉二维码是同步走代理的：代理不可用时把真实原因回给前端，别让它被统一 500「internal error」吞掉。
    try:
        meta = await asyncio.to_thread(yyblogin.start_login, uid, proxy_url, login_source, proxy_meta)
    except HTTPException:
        raise
    except Exception as e:
        hint = "（代理不可用？短效代理可重试自动换 IP；长效代理请检查连通性）" if proxy_url else ""
        raise HTTPException(400, f"发起扫码失败：{type(e).__name__}: {e}{hint}")
    event("startQr", "发起扫码登录", project=actor_name(uid), 来源=client_ip(request),
          session=meta.get("sessionId"), 代理模式=proxy_meta.get("proxyMode"),
          地区=proxy_meta.get("proxyRegionName") or "")
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
    uid = deps.resolve_actor(request)  # 必须登录/授权码；否则任何人空 body 即可清空全体扫码会话
    b = await get_body(request)
    sid = b.get("sessionId") or None
    yyblogin.stop_login(sid, uid)      # 只能停自己名下的会话
    event("stopQr", "停止扫码登录", project=actor_name(uid), 来源=client_ip(request), session=sid or "all")
    return {"success": True}
