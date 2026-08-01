"""响应视图整形：DB 行 → 前端期望的 camelCase JSON（字段名严格对齐 PORT_SPEC）。"""
from __future__ import annotations

import json

from . import db


def used_count(user_id: int) -> int:
    r = db.query_one("SELECT COUNT(*) c FROM accounts WHERE user_id=?", (user_id,))
    return r["c"] if r else 0


def public_user(u) -> dict:
    return {
        "id": u["id"], "username": u["username"], "status": u["status"],
        "role": u["role"], "note": u["note"] or "", "createdAt": u["created_at"],
        "approvedAt": u["approved_at"] or 0, "lastLoginAt": u["last_login_at"] or 0,
    }


def license_view(lic, uc: int | None = None) -> dict:
    if not lic:
        return None
    if uc is None:
        uc = used_count(lic["user_id"])
    return {
        "id": lic["id"], "userId": lic["user_id"], "key": lic["license_key"],
        "maxUsers": lic["max_users"], "status": lic["status"], "note": lic["note"] or "",
        "createdAt": lic["created_at"], "expiresAt": lic["expires_at"] or 0,
        "usedCount": uc, "remaining": max(0, lic["max_users"] - uc),
    }


def account_view(a) -> dict:
    from .shortproxy import account_mode, safe_proxy_label
    mode = account_mode(a)
    region_name = (a["proxy_region_name"] if "proxy_region_name" in a.keys() else "") or ""
    if mode == "short":
        proxy_label = f"短效代理 · {region_name}" if region_name else "短效代理"
    elif mode == "long":
        proxy_label = safe_proxy_label(a["proxy_url"] or "")
    else:
        proxy_label = "直连"
    return {
        "openid": a["openid"], "nickname": a["nickname"] or "", "unionid": a["unionid"] or "",
        "headImgUrl": a["head_img_url"] or "", "loggedAt": a["logged_at"] or 0,
        "expireAt": a["expire_at"] or 0, "hasSession": False,
        "isCurrent": bool(a["is_current"]), "status": a["status"] or "active",
        "statusError": a["status_error"] or None,
        "loginSource": (a["login_source"] if "login_source" in a.keys() else 1) or 1,
        "proxyMode": mode,
        "proxyRegionCode": (a["proxy_region_code"] if "proxy_region_code" in a.keys() else "") or "",
        "proxyRegionName": region_name,
        "proxyLabel": proxy_label,
    }


def _run_config(row) -> dict:
    try:
        return json.loads(row["run_config"] or "{}")
    except Exception:
        return {}


def project_view(p, full: bool = False) -> dict:
    rc = _run_config(p)
    v = {
        "id": p["id"], "name": p["name"], "summary": p["summary"] or "", "icon": p["icon"] or "",
        "status": p["status"], "panelType": p["panel_type"] or "",
        "submitPanels": rc.get("submitPanels", []), "builtin": rc.get("builtin", ""),
        "sortOrder": p["sort_order"] or 0, "updatedAt": p["updated_at"],
    }
    if full:
        v.update({"intro": p["intro"] or "", "tutorial": p["tutorial"] or "",
                  "runConfig": rc, "createdAt": p["created_at"]})
    return v


def panel_view(p) -> dict:
    return {
        "panelType": p["panel_type"], "baseUrl": p["base_url"] or "", "clientId": p["client_id"] or "",
        "hasSecret": bool(p["client_secret_enc"]), "lastTestAt": p["last_test_at"] or 0,
        "lastTestOk": bool(p["last_test_ok"]), "updatedAt": p["updated_at"] or 0,
    }


def admin_view() -> dict:
    lg = db.query_one("SELECT value FROM admin_config WHERE key='admin_last_login'")
    cr = db.query_one("SELECT value FROM admin_config WHERE key='admin_created_at'")
    return {
        "id": 1, "username": "admin", "status": "active",
        "createdAt": int(cr["value"]) if cr else 0,
        "lastLoginAt": int(lg["value"]) if lg else 0,
    }
