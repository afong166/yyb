"""/api/yyb/* 取码 + /wx/code 兼容。"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db, deps
from ..codebridge import (
    get_code_for_openid, invoke_cloud_for_openid, get_phone_for_openid,
    get_userinfo_for_openid,
    cloud_call_function_for_openid, cloud_call_container_for_openid,
    oauth_authorize_for_openid, oauth_confirm_for_openid,
)
from ..logs import actor_name, event, mask_id
from ..util import get_body, client_ip


def _log(tag, msg, uid, appid, openid, result):
    event(tag, msg, project=actor_name(uid), 来源=result.get("_ip", ""), appid=appid,
          openid=mask_id(openid), 结果="成功" if result.get("success") else "失败",
          耗时ms=result.get("_ms", 0))

router = APIRouter()
wx_router = APIRouter()


def _mask_mobile(m: str) -> str:
    """手机号脱敏：153****0000。"""
    m = str(m or "")
    return f"{m[:3]}****{m[-4:]}" if len(m) >= 7 else m


def _outcome(action: str, result: dict) -> str:
    """从结果里取出本次调用产出的「凭据」，供管理端展示：
    get-code/get-codes → wx.login code；get-phone → 脱敏手机号；其余留空。"""
    if not result.get("success"):
        return ""
    if action in ("get-code", "get-codes"):
        return (result.get("code") or "")[:120]
    if action == "get-phone":
        return _mask_mobile(result.get("mobile") or "")
    if action == "oauth-authorize":
        return (result.get("redirect_url") or "")[:120]
    if action == "oauth-confirm":
        import re
        ru = result.get("redirect_url") or ""
        m = re.search(r"[?&]code=([^&]+)", ru)
        return (m.group(1) if m else ru)[:120]
    return ""


def _record(uid, openid, appid, action, result):
    db.execute(
        "INSERT INTO call_records(user_id,openid,appid,action,result,error,code,ms,ip,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uid, openid, appid, action, "ok" if result.get("success") else "fail",
         "" if result.get("success") else (result.get("error") or "")[:500],
         _outcome(action, result), result.get("_ms", 0), result.get("_ip", ""), db.now()),
    )


@router.post("/get-code")
async def get_code(request: Request):
    b = await get_body(request)
    uid = deps.resolve_actor(request, b.get("auth") or "")
    openid, appid = b.get("openid") or "", b.get("appid") or ""
    if not openid:
        raise HTTPException(400, "openid required")
    if not appid:
        raise HTTPException(400, "appid required")
    if openid not in deps.account_openids(uid):
        raise HTTPException(403, "openid not bound to this license")
    t0 = time.time()
    result = await get_code_for_openid(openid, appid)
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "get-code", result)
    _log("getCode", "获取Code", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


async def _guard(request: Request, b: dict):
    """公共校验：解析 actor + openid/appid + 绑定校验。返回 (uid, openid, appid)。"""
    uid = deps.resolve_actor(request, b.get("auth") or "")
    openid, appid = b.get("openid") or b.get("account") or "", b.get("appid") or b.get("appId") or ""
    if not openid:
        raise HTTPException(400, "openid required")
    if not appid:
        raise HTTPException(400, "appid required")
    if openid not in deps.account_openids(uid):
        raise HTTPException(403, "openid not bound to this license")
    return uid, openid, appid


@router.post("/invoke-cloud")
async def invoke_cloud(request: Request):
    b = await get_body(request)
    uid, openid, appid = await _guard(request, b)
    data_json = b.get("param2") or b.get("param1") or b.get("data") or ""
    t0 = time.time()
    result = await invoke_cloud_for_openid(openid, appid, data_json)
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "invoke-cloud", result)
    _log("invokeCloud", "调用云函数", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


@router.post("/get-phone")
async def get_phone(request: Request):
    b = await get_body(request)
    uid, openid, appid = await _guard(request, b)
    data_json = b.get("param2") or b.get("data") or ""
    t0 = time.time()
    result = await get_phone_for_openid(openid, appid, data_json)
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "get-phone", result)
    _log("getPhone", "获取手机号", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


@router.post("/get-userinfo")
async def get_userinfo(request: Request):
    """wx.getUserInfo：返回 rawData/signature/encryptedData/iv（走 webapi_getuserinfo）。"""
    b = await get_body(request)
    uid, openid, appid = await _guard(request, b)
    t0 = time.time()
    result = await get_userinfo_for_openid(openid, appid)
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "get-userinfo", result)
    _log("getUserInfo", "获取用户信息", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


@router.post("/cloud-call-function")
async def cloud_call_function(request: Request):
    """wx.cloud.callFunction：调用小程序云函数（qbase_commapi/tcbapi_slowcallfunction_v2）。"""
    b = await get_body(request)
    uid, openid, appid = await _guard(request, b)
    fn = b.get("functionName") or b.get("function_name") or ""
    if not fn:
        raise HTTPException(400, "functionName required")
    fdata = b.get("functionData")
    if fdata is None:
        fdata = b.get("function_data") or {}
    env = b.get("cloudEnv") or b.get("cloud_env") or ""
    t0 = time.time()
    result = await cloud_call_function_for_openid(openid, appid, fn, fdata, env)
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "cloud-call-function", result)
    _log("cloudCallFunction", "调用云函数", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


@router.post("/cloud-call-container")
async def cloud_call_container(request: Request):
    """wx.cloud.callContainer：调用小程序云托管容器（qbase_commapi/tcbapi_call_gateway）。"""
    b = await get_body(request)
    uid, openid, appid = await _guard(request, b)
    cloud_host = b.get("cloudHost") or b.get("cloud_host") or ""
    if not cloud_host:
        raise HTTPException(400, "cloudHost required")
    path = b.get("path") or ""
    if not path:
        raise HTTPException(400, "path required")
    method = b.get("method") or "GET"
    headers = b.get("headers") or {}
    data = b.get("data") or ""
    from .. import shortproxy
    try:
        proxy_params = await asyncio.to_thread(shortproxy.resolve_params, uid, openid, b, "account")
    except (ValueError, RuntimeError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(400, detail)
    proxy_url = proxy_params["proxyUrl"]
    direct = bool(b.get("direct") or b.get("directMode") or False)
    t0 = time.time()
    result = await cloud_call_container_for_openid(openid, appid, cloud_host, path, method, headers, data, proxy_url, direct)
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "cloud-call-container", result)
    _log("cloudCallContainer", "调用云托管", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


@router.post("/oauth-authorize")
async def oauth_authorize(request: Request):
    """公众号 OAuth2 授权（cmdid=1254）：提交授权 URL，返回 scope_list/redirect_url（含 code）。
    正确协议格式下，authorize 一步即可拿到 redirect_url(含code)，无需再调 confirm。"""
    b = await get_body(request)
    uid, openid, appid = await _guard(request, b)
    url = b.get("url") or b.get("oauth_url") or ""
    if not url:
        raise HTTPException(400, "url required")
    t0 = time.time()
    # scene 默认 4（网页授权）；调用方显式传值时尊重入参
    _scene = b.get("scene")
    scene = 4 if _scene in (None, "") else int(_scene)
    result = await oauth_authorize_for_openid(
        openid, appid, url,
        biz_username=b.get("biz_username") or "", scene=scene,
        referrer_url=b.get("referrer_url") or "", sub_scene=b.get("sub_scene"),
        auto_oauth=b.get("auto_oauth"))
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "oauth-authorize", result)
    _log("oauthAuthorize", "公众号OAuth授权", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


@router.post("/oauth-authorize-confirm")
async def oauth_authorize_confirm(request: Request):
    """公众号 OAuth2 确认授权（cmdid=1255）：返回 redirect_url（含网页授权 code）。
    注意：正确协议下 authorize 一步已含 code，此接口通常不需要调用。"""
    b = await get_body(request)
    uid, openid, appid = await _guard(request, b)
    oauth_url = b.get("oauth_url") or b.get("url") or ""
    if not oauth_url:
        raise HTTPException(400, "oauth_url required")
    t0 = time.time()
    result = await oauth_confirm_for_openid(
        openid, appid, oauth_url,
        opt=b.get("opt") or 0, avatar_id=b.get("avatar_id") or "",
        redirect_uri=b.get("redirect_uri") or "")
    result["_ms"] = int((time.time() - t0) * 1000)
    result["_ip"] = client_ip(request)
    _record(uid, openid, appid, "oauth-confirm", result)
    _log("oauthConfirm", "公众号OAuth确认授权", uid, appid, openid, result)
    body = {k: v for k, v in result.items() if not k.startswith("_")}
    return JSONResponse(status_code=200 if result.get("success") else 500, content=body)


@router.post("/get-codes")
async def get_codes(request: Request):
    b = await get_body(request)
    uid = deps.resolve_actor(request, b.get("auth") or "")
    accounts = b.get("accounts") or []
    appid = b.get("appid") or ""
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(400, "accounts array required")
    if not appid:
        raise HTTPException(400, "appid required")
    bound = deps.account_openids(uid)
    unbound = [o for o in accounts if o not in bound]
    if unbound:
        return JSONResponse(status_code=403, content={
            "success": False, "error": "some openids are not bound to this license", "unbound": unbound})

    ip = client_ip(request)
    t0 = time.time()

    async def one(openid):
        a0 = time.time()
        r = await get_code_for_openid(openid, appid)
        ms = int((time.time() - a0) * 1000)
        _record(uid, openid, appid, "get-codes",
                {"success": r.get("success"), "error": r.get("error"), "code": r.get("code"), "_ms": ms, "_ip": ip})
        return {"openid": openid, "success": bool(r.get("success")),
                "code": r.get("code") if r.get("success") else None,
                "error": None if r.get("success") else r.get("error"), "totalMs": ms}

    results = await asyncio.gather(*[one(o) for o in accounts])
    ok = sum(1 for r in results if r["success"])
    event("getCodes", "批量获取Code", project=actor_name(uid), 来源=ip, appid=appid,
          结果=f"{ok}/{len(results)}", 耗时ms=int((time.time() - t0) * 1000))
    return JSONResponse(status_code=200 if ok == len(results) else 207, content={
        "success": ok == len(results), "results": results,
        "totalMs": int((time.time() - t0) * 1000), "summary": f"{ok}/{len(results)} accounts succeeded"})


@wx_router.post("/code")
async def wx_code(request: Request):
    b = await get_body(request)
    body_auth = b.get("auth") or request.query_params.get("auth") or request.headers.get("auth") or ""
    uid = deps.resolve_actor(request, body_auth)
    openid = b.get("openid") or b.get("account") or b.get("wcsid") or ""
    appid = b.get("appid") or b.get("appId") or ""
    if not openid or not appid:
        return JSONResponse(status_code=400, content=_wx_shape({"success": False, "error": "openid/appid required"}))
    if openid not in deps.account_openids(uid):
        return JSONResponse(status_code=403, content=_wx_shape({"success": False, "error": "openid not bound to this license"}))
    result = await get_code_for_openid(openid, appid)
    event("wxCode", "取Code(wx兼容)", project=actor_name(uid), 来源=client_ip(request), appid=appid,
          openid=mask_id(openid), 结果="成功" if result.get("success") else "失败")
    return JSONResponse(status_code=200 if result.get("success") else 500, content=_wx_shape(result))


def _wx_shape(result: dict) -> dict:
    code = result.get("code") or ""
    if not result.get("success") or not code:
        err = result.get("error") or "wx.login code not found"
        return {"status": False, "success": False, "code": -1, "message": err, "msg": err, "data": result}
    return {"status": True, "success": True, "code": code,
            "data": {"code": code, "loginCode": code, "openid": result.get("openid"), "appid": result.get("appid")}}
