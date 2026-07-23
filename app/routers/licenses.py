"""/api/licenses/* — 授权码传统管理（管理员令牌，供外部工具/脚本）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import db, deps, models, svc
from ..util import get_body

router = APIRouter()


def _lic_by_key(key: str):
    return db.query_one("SELECT * FROM licenses WHERE license_key=? COLLATE NOCASE", (key,))


@router.get("")
async def list_licenses(request: Request):
    deps.require_admin(request)
    rows = db.query("SELECT * FROM licenses ORDER BY id DESC")
    return {"success": True, "licenses": [models.license_view(r) for r in rows]}


@router.post("")
async def create_license(request: Request):
    deps.require_admin(request)
    b = await get_body(request)
    uid = int(b.get("userId") or 0)
    if uid <= 0:
        raise HTTPException(400, "userId required")
    try:
        lic = svc.issue_license(uid, int(b.get("maxUsers") or 0), b.get("note") or "",
                                int(b.get("expiresAt") or 0), b.get("key"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, "license": models.license_view(lic)}


@router.get("/{key}")
async def get_license(request: Request, key: str):
    deps.require_admin(request)
    lic = _lic_by_key(key)
    if not lic:
        raise HTTPException(404, "license not found")
    return {"success": True, "license": models.license_view(lic)}


@router.patch("/{key}")
async def patch_license(request: Request, key: str):
    deps.require_admin(request)
    lic = _lic_by_key(key)
    if not lic:
        raise HTTPException(404, "license not found")
    b = await get_body(request)
    try:
        lic = svc.update_license(lic, max_users=b.get("maxUsers"), note=b.get("note"),
                                 status=b.get("status"), expires_at=b.get("expiresAt"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True, "license": models.license_view(lic)}


@router.post("/{key}/disable")
async def disable(request: Request, key: str):
    deps.require_admin(request)
    lic = _lic_by_key(key)
    if not lic:
        raise HTTPException(404, "license not found")
    lic = svc.update_license(lic, status="disabled")
    return {"success": True, "license": models.license_view(lic)}


@router.post("/{key}/enable")
async def enable(request: Request, key: str):
    deps.require_admin(request)
    lic = _lic_by_key(key)
    if not lic:
        raise HTTPException(404, "license not found")
    lic = svc.update_license(lic, status="active")
    return {"success": True, "license": models.license_view(lic)}


@router.delete("/{key}")
async def delete_license(request: Request, key: str):
    deps.require_admin(request)
    lic = _lic_by_key(key)
    if not lic:
        raise HTTPException(404, "license not found")
    view = models.license_view(lic)
    db.execute("DELETE FROM licenses WHERE id=?", (lic["id"],))
    return {"success": True, "license": view}


@router.post("/{key}/unbind")
async def unbind(request: Request, key: str):
    deps.require_admin(request)
    lic = _lic_by_key(key)
    if not lic:
        raise HTTPException(404, "license not found")
    b = await get_body(request)
    openids = b.get("openids") or ([b["openid"]] if b.get("openid") else [])
    for o in openids:
        db.execute("DELETE FROM accounts WHERE openid=? AND user_id=?", (o, lic["user_id"]))
    return {"success": True, "license": models.license_view(_lic_by_key(key))}


@router.post("/{key}/bind")
async def bind(request: Request, key: str):
    deps.require_admin(request)
    lic = _lic_by_key(key)
    if not lic:
        raise HTTPException(404, "license not found")
    if lic["status"] != "active":
        raise HTTPException(400, "license disabled")
    b = await get_body(request)
    openids = b.get("openids") or ([b["openid"]] if b.get("openid") else [])
    if not openids:
        raise HTTPException(400, "openids required")
    force = bool(b.get("force"))
    added, skipped, rejected = [], [], []
    for o in openids:
        cur = db.query_one("SELECT user_id FROM accounts WHERE openid=?", (o,))
        if cur and cur["user_id"] == lic["user_id"]:
            skipped.append({"openid": o, "reason": "already bound"})
            continue
        if cur and not force:
            skipped.append({"openid": o, "reason": f"bound to user #{cur['user_id']}"})
            continue
        if models.used_count(lic["user_id"]) >= lic["max_users"]:
            rejected.append(o)
            continue
        if cur:
            db.execute("UPDATE accounts SET user_id=? WHERE openid=?", (lic["user_id"], o))
        else:
            db.execute("INSERT INTO accounts(openid,user_id,logged_at,updated_at) VALUES(?,?,?,?)",
                       (o, lic["user_id"], db.now(), db.now()))
        added.append(o)
    return {"success": True, "license": models.license_view(_lic_by_key(key)),
            "added": added, "skipped": skipped, "rejectedByCap": rejected}
