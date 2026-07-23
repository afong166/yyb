"""内置项目：京东 Code 登录换 Cookie。code(京东小程序 wx.login) → silentauthlogin → pt_key/pt_pin。

参数来源（京东快递小程序 wx73247c7819d61796 解包，plugin wxefe655223916819e）：
  · sign = md5(appid + wxappid + client_ver + ts + cmd + sub_cmd + gsalt)，标准 MD5，ts 每次取当前秒。
  · eid_token（finger_tk）：京东风控 SDK 设备指纹，缓存在小程序 storage，离线算不出，过期需重抓（不参与 sign）。
  · X-WECHAT-HOSTSIGN：微信运行时在 wx.request 注入，小程序 JS 不生成；此接口校验较松，可用一次抓到的值。
设备/会话级参数会过期，失效后用 JD_* 环境变量覆盖即可，无需改代码。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

import httpx

# —— 设备/会话级默认值（抓包，会过期；用 JD_* 环境变量覆盖刷新）——
DEFAULTS = {
    "appid": "599",
    "wxappid": "wx73247c7819d61796",
    "client_ver": "2.0.2",
    "gsalt": "sb2cwlYyaCSN1KUv5RHG3tmqxfEb8NKN",
    "cmd": "52",
    "sub_cmd": "1",
    "return_url": "/pages/login/web-view/web-view",
    "eid_token": ("jdd01w4AS6CD7CIG2JXB54AQC4LIR2Q4FVGKFUEXPCCJXKWDK5PSSQBQL6AAZWY5ZWC7WUB"
                  "UMFGMKGKQG5EOSJBPAZCTFDEPQUM56YZYXXMDSXXEIBZ7CIICDGXISQRWFXSKN4VO"),
    "hostsign_noncestr": "4311023ad04719e963d52022c370a83e",
    "hostsign_timestamp": "1781869158",
    "hostsign_signature": "d16be9475d2349e7694aeb38299e5c9e8e87ba63",
    "ua": ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_5 like Mac OS X) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.74(0x18004a30) NetType/WIFI Language/zh_CN"),
    "referer": "https://servicewechat.com/wx73247c7819d61796/870/page-frame.html",
}

_RISK_HINTS = ("风控", "风险", "验证", "拦截", "频繁", "异常", "risk")


def _env(key: str, fallback: str) -> str:
    v = os.environ.get(key)
    return v.strip() if v and v.strip() else fallback


def _cfg() -> dict:
    ts = str(int(time.time()))
    c = {
        "appid": _env("JD_APPID", DEFAULTS["appid"]),
        "wxappid": _env("JD_WXAPPID", DEFAULTS["wxappid"]),
        "client_ver": _env("JD_CLIENT_VER", DEFAULTS["client_ver"]),
        "gsalt": _env("JD_GSALT", DEFAULTS["gsalt"]),
        "cmd": _env("JD_CMD", DEFAULTS["cmd"]),
        "sub_cmd": _env("JD_SUB_CMD", DEFAULTS["sub_cmd"]),
        "ts": ts,
        "eid_token": _env("JD_EID_TOKEN", DEFAULTS["eid_token"]),
        "return_url": _env("JD_RETURN_URL", DEFAULTS["return_url"]),
        "hostsign_noncestr": _env("JD_HOSTSIGN_NONCESTR", DEFAULTS["hostsign_noncestr"]) or secrets.token_hex(16),
        "hostsign_timestamp": _env("JD_HOSTSIGN_TIMESTAMP", DEFAULTS["hostsign_timestamp"]) or str(int(time.time())),
        "hostsign_signature": _env("JD_HOSTSIGN_SIGNATURE", DEFAULTS["hostsign_signature"]),
        "ua": _env("JD_USER_AGENT", DEFAULTS["ua"]),
        "referer": _env("JD_REFERER", DEFAULTS["referer"]),
    }
    raw = c["appid"] + c["wxappid"] + c["client_ver"] + ts + c["cmd"] + c["sub_cmd"] + c["gsalt"]
    c["sign"] = hashlib.md5(raw.encode()).hexdigest()
    return c


def _pick_payload(j):
    """京东多层响应里挑出带 pt_key/pt_pin/cookie 的那层。"""
    if not isinstance(j, dict):
        return {}
    for c in (j, j.get("data"), j.get("result"), j.get("ret"), j.get("payload")):
        if isinstance(c, dict) and (c.get("pt_key") or c.get("pt_pin") or c.get("cookie") or c.get("jdCookie")):
            return c
    return j


def _ck(text: str, name: str) -> str:
    import re
    m = re.search(rf"{name}=([^;]+)", text or "")
    return m.group(1) if m else ""


async def silent_auth_login(code: str, proxy_url: str = "") -> dict:
    c = _cfg()
    if not c["eid_token"] or not c["sign"] or not c["hostsign_signature"]:
        return {"ok": False, "status": 0, "errCode": -1,
                "errMsg": "京东设备参数未配置（eid_token / sign / hostsign），请用 JD_* 环境变量配置后重试",
                "jdCookie": "", "ptKey": "", "ptPin": "", "raw": {}}
    form = {
        "code": code, "eid_token": c["eid_token"], "goToLogin": "true",
        "returnurl": c["return_url"], "wxappid": c["wxappid"], "appid": c["appid"],
        "client_ver": c["client_ver"], "ts": c["ts"], "sign": c["sign"],
    }
    hostsign = json.dumps({"noncestr": c["hostsign_noncestr"],
                           "timestamp": int(c["hostsign_timestamp"]),
                           "signature": c["hostsign_signature"]}, separators=(",", ":"))
    headers = {
        "X-WECHAT-HOSTSIGN": hostsign, "User-Agent": c["ua"], "Referer": c["referer"],
        "Content-Type": "application/x-www-form-urlencoded",
        "cookie": "guid=; pt_pin=; pt_key=; pt_token=",
    }
    kw = {"proxy": proxy_url} if proxy_url else {}
    async with httpx.AsyncClient(timeout=20.0, verify=False, **kw) as cli:
        r = await cli.post("https://wxapplogin.m.jd.com/cgi-bin/jxpp/silentauthlogin",
                           data=form, headers=headers)
    try:
        j = r.json()
    except Exception:
        j = None
    payload = _pick_payload(j) if j else {}
    # 京东用 snake_case：err_code / err_msg（老端点偶尔 errCode）
    ec = payload.get("err_code", payload.get("errCode"))
    if ec is None and isinstance(j, dict):
        ec = j.get("err_code", j.get("errCode"))
    try:
        errcode = int(ec)
    except (TypeError, ValueError):
        errcode = -1
    errmsg = str(payload.get("err_msg") or payload.get("errMsg")
                 or (j.get("err_msg") if isinstance(j, dict) else "") or "")
    # 提取 pt_key/pt_pin：响应体字段 > cookie 文本 > Set-Cookie
    ctext = payload.get("cookie") or payload.get("jdCookie") or ""
    pt_key = payload.get("pt_key") or _ck(ctext, "pt_key") or (r.cookies.get("pt_key") or "")
    pt_pin = payload.get("pt_pin") or _ck(ctext, "pt_pin") or (r.cookies.get("pt_pin") or "")
    jd_cookie = f"pt_key={pt_key};pt_pin={pt_pin};" if pt_key and pt_pin else ""
    ok = r.status_code < 400 and errcode == 0 and bool(jd_cookie)
    if not errmsg and not ok:
        snippet = (r.text or "")[:200]
        errmsg = f"HTTP {r.status_code} err_code={errcode} {snippet}".strip()
    return {"ok": ok, "status": r.status_code, "errCode": errcode, "errMsg": errmsg,
            "jdCookie": jd_cookie, "ptKey": pt_key, "ptPin": pt_pin, "raw": j}


async def run_jd_code_login(user_id: int, openid: str, appid: str, proxy_url: str = "") -> dict:
    from .codebridge import get_code_for_openid
    cr = await get_code_for_openid(openid, appid or DEFAULTS["wxappid"])
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "获取京东小程序 code 失败"}
    sa = await silent_auth_login(cr["code"], proxy_url)
    if not sa["ok"]:
        err = sa.get("errMsg") or "京东静默授权失败"
        if sa.get("errCode") == 35:
            err = "该微信号尚未绑定京东账号，请先在京东 App 绑定"
        elif any(h in err for h in _RISK_HINTS):
            err = f"疑似触发风控：{err}"
        return {"ok": False, "stage": "jd-auth", "error": err,
                "errCode": sa.get("errCode"), "code": cr["code"]}
    return {"ok": True, "jdCookie": sa["jdCookie"], "ptPin": sa["ptPin"], "ptKey": sa["ptKey"], "code": cr["code"]}
