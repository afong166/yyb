"""通用工具：宽松 JSON body、客户端 IP、会话 cookie 读写、SSRF 目标校验。"""
from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response

from . import auth
from .config import CLIENT_IP_HEADER, COOKIE_SECURE, TRUST_PROXY


async def get_body(request: Request) -> dict:
    """宽松解析：无效/空 body 一律得到 {}，绝不 422（对齐 Node tolerantJson）。"""
    try:
        raw = await request.body()
        if not raw:
            return {}
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def client_ip(request: Request) -> str:
    # 仅在显式信任反代/CDN 时才采信转发头，否则用直连对端 IP，避免日志/审计 IP 被伪造。
    if TRUST_PROXY:
        # 1) 优先用 CDN 注入的单值真实 IP 头（如 EdgeOne 的 EO-Client-IP）——由边缘覆盖写入，不可伪造。
        if CLIENT_IP_HEADER:
            v = (request.headers.get(CLIENT_IP_HEADER) or "").split(",")[0].strip()
            if v:
                return v
        # 2) 兜底 X-Forwarded-For：取最后一跳（可信代理追加的那个），而非可被客户端伪造的首值。
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


# ---------------- SSRF 防护 ----------------
def _ip_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def guard_public_url(url: str, allow_schemes: tuple[str, ...] = ("http", "https")) -> None:
    """拒绝目标解析到内网/环回/保留地址的请求（SSRF 防护）。

    注意：这是解析期校验，无法完全消除 DNS 重绑定（TOCTOU）；对可信用户的自托管
    场景足够，若需强隔离应在出站层固定已解析 IP。
    """
    try:
        p = urlparse(url)
    except Exception:
        raise HTTPException(400, "非法地址")
    if p.scheme.lower() not in allow_schemes:
        raise HTTPException(400, f"不支持的协议：{p.scheme or '（空）'}")
    host = p.hostname
    if not host:
        raise HTTPException(400, "非法地址：缺少主机名")
    try:
        infos = socket.getaddrinfo(host, p.port, proto=socket.IPPROTO_TCP)
    except Exception:
        raise HTTPException(400, "无法解析目标主机名")
    for info in infos:
        if _ip_blocked(info[4][0]):
            raise HTTPException(403, "拒绝访问内网/环回/保留地址（SSRF 防护）")


def set_user_cookie(resp: Response, user_id: int) -> None:
    resp.set_cookie(auth.USER_COOKIE, auth.sign_token({"uid": user_id}),
                    max_age=auth.SESSION_TTL, httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")


def clear_user_cookie(resp: Response) -> None:
    resp.delete_cookie(auth.USER_COOKIE, path="/")


def set_admin_cookie(resp: Response) -> None:
    resp.set_cookie(auth.ADMIN_COOKIE, auth.sign_token({"admin": True}, ttl=3 * 24 * 3600),
                    max_age=3 * 24 * 3600, httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")


def clear_admin_cookie(resp: Response) -> None:
    resp.delete_cookie(auth.ADMIN_COOKIE, path="/")
