"""短效代理地区与管理员 51代理配置接口。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from .. import deps, shortproxy, svc
from ..util import client_ip, get_body

router = APIRouter()
admin_router = APIRouter()


def _require_any(request: Request) -> None:
    if deps.is_admin(request):
        return
    deps.resolve_actor(request)


@router.get("/capabilities")
async def capabilities(request: Request):
    _require_any(request)
    view = shortproxy.settings_view()
    return {"success": True, "shortProxyConfigured": view["configured"],
            "localRegionCode": view["localRegionCode"], "localRegionName": view["localRegionName"]}


@router.get("/regions")
async def list_regions(request: Request, parentCode: str = ""):
    _require_any(request)
    try:
        rows = await asyncio.to_thread(shortproxy.regions, parentCode)
    except Exception as exc:
        raise HTTPException(502, f"获取 51代理地区失败：{exc}")
    return {"success": True, "regions": rows}


@admin_router.get("/settings")
async def get_settings(request: Request):
    deps.require_admin(request)
    return {"success": True, **shortproxy.settings_view()}


@admin_router.put("/settings")
async def put_settings(request: Request):
    deps.require_admin(request)
    body = await get_body(request)
    try:
        if body.get("clear"):
            shortproxy.clear_api_url()
        elif (body.get("apiUrl") or "").strip():
            shortproxy.save_api_url(body["apiUrl"])
        else:
            raise ValueError("请填写 apiUrl；如需删除请传 clear=true")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    svc.audit("short-proxy-settings", detail="clear" if body.get("clear") else "update", ip=client_ip(request))
    return {"success": True, **shortproxy.settings_view()}

