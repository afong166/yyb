"""Built-in project: Mixue Bingcheng mini-program code login.

Flow:
  wx.login code -> /v1/app/code2Session -> /v1/app/regByUnionid -> accessToken
"""
from __future__ import annotations

import base64
import json
import os
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

MINI_APP_ID = os.environ.get("MXBC_MINI_APPID", "wx7696c66d2245d107")
APP_ID = os.environ.get("MXBC_APPID", "d82be6bbc1da11eb9dd000163e122ecb")
API_BASE = os.environ.get("MXBC_API_BASE", "https://mxsa.mxbc.net/api")
APP_VERSION = os.environ.get("MXBC_APP_VERSION", "2.8.28")
USER_AGENT = os.environ.get(
    "MXBC_USER_AGENT",
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36 "
    "MicroMessenger/7.0.4.501 NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF",
)
REFERER = os.environ.get(
    "MXBC_REFERER",
    "https://servicewechat.com/wx7696c66d2245d107/59/page-frame.html",
)

_PRIVATE_KEY = os.environ.get("MXBC_PRIVATE_KEY") or """-----BEGIN PRIVATE KEY-----
MIIEvwIBADANBgkqhkiG9w0BAQEFAASCBKkwggSlAgEAAoIBAQCtypUdHZJKlQ9L
L6lIJSphnhqjke7HclgWuWDRWvzov30du235cCm13mqJ3zziqLCwstdQkuXo9sOP
Ih94t6nzBHTuqYA1whrUnQrKfv9X4/h3QVkzwT+xWflE+KubJZoe+daLKkDeZjVW
nUku8ov0E5vwADACfntEhAwiSZUALX9UgNDTPbj5ESeII+VztZ/KOFsRHMTfDb1G
IR/dAc1mL5uYbh0h2Fa/fxRPgf7eJOeWGiygesl3CWj0Ue13qwX9PcG7klJXfToI
576MY+A7027a0aZ49QhKnysMGhTdtFCksYG0lwPz3bIR16NvlxNLKanc2h+ILTFQ
bMW/Y3DRAgMBAAECggEBAJGTfX6rE6zX2bzASsu9HhgxKN1VU6/L70/xrtEPp4SL
SpHKO9/S/Y1zpsigr86pQYBx/nxm4KFZewx9p+El7/06AX0djOD7HCB2/+AJq3iC
5NF4cvEwclrsJCqLJqxKPiSuYPGnzji9YvaPwArMb0Ff36KVdaHRMw58kfFys5Y2
HvDqh4x+sgMUS7kSEQT4YDzCDPlAoEFgF9rlXnh0UVS6pZtvq3cR7pR4A9hvDgX9
wU6zn1dGdy4MEXIpckuZkhwbqDLmfoHHeJc5RIjRP7WIRh2CodjetgPFE+SV7Sdj
ECmvYJbet4YLg+Qil0OKR9s9S1BbObgcbC9WxUcrTgECgYEA/Yj8BDfxcsPK5ebE
9N2teBFUJuDcHEuM1xp4/tFisoFH90JZJMkVbO19rddAMmdYLTGivWTyPVsM1+9s
tq/NwsFJWHRUiMK7dttGiXuZry+xvq/SAZoitgI8tXdDXMw7368vatr0g6m7ucBK
jZWxSHjK9/KVquVr7BoXFm+YxaECgYEAr3sgVNbr5ovx17YriTqe1FLTLMD5gPrz
ugJj7nypDYY59hLlkrA/TtWbfzE+vfrN3oRIz5OMi9iFk3KXFVJMjGg+M5eO9Y8m
14e791/q1jUuuUH4mc6HttNRNh7TdLg/OGKivE+56LEyFPir45zw/dqwQM3jiwIz
yPz/+bzmfTECgYATxrOhwJtc0FjrReznDMOTMgbWYYPJ0TrTLIVzmvGP6vWqG8rI
S8cYEA5VmQyw4c7G97AyBcW/c3K1BT/9oAj0wA7wj2JoqIfm5YPDBZkfSSEcNqqy
5Ur/13zUytC+VE/3SrrwItQf0QWLn6wxDxQdCw8J+CokgnDAoehbH6lTAQKBgQCE
67T/zpR9279i8CBmIDszBVHkcoALzQtU+H6NpWvATM4WsRWoWUx7AJ56Z+joqtPK
G1WztkYdn/L+TyxWADLvn/6Nwd2N79MyKyScKtGNVFeCCJCwoJp4R/UaE5uErBNn
OH+gOJvPwHj5HavGC5kYENC1Jb+YCiEDu3CB0S6d4QKBgQDGYGEFMZYWqO6+LrfQ
ZNDBLCI2G4+UFP+8ZEuBKy5NkDVqXQhHRbqr9S/OkFu+kEjHLuYSpQsclh6XSDks
5x/hQJNQszLPJoxvGECvz5TN2lJhuyCupS50aGKGqTxKYtiPHpWa8jZyjmanMKnE
dOGyw/X4SFyodv8AEloqd81yGg==
-----END PRIVATE KEY-----"""

_private_key_obj = None


def _private_key():
    global _private_key_obj
    if _private_key_obj is None:
        _private_key_obj = serialization.load_pem_private_key(
            _PRIVATE_KEY.encode("utf-8"), password=None
        )
    return _private_key_obj


def _sign(content: str) -> str:
    sig = _private_key().sign(
        content.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii").replace("/", "_").replace("+", "-")


def _js_value(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _signed_body(params: dict | None = None) -> dict:
    body = {**(params or {}), "t": int(time.time() * 1000), "appId": APP_ID}
    content = "&".join(
        "%s=%s" % (k, _js_value(body[k]))
        for k in sorted(body)
        if body[k] or body[k] == 0
    )
    return {**body, "sign": _sign(content)}


def _headers(access_token: str = "") -> dict:
    return {
        "Host": "mxsa.mxbc.net",
        "Connection": "keep-alive",
        "user-agent": USER_AGENT,
        "xweb_xhr": "1",
        "Access-Token": access_token,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": REFERER,
        "Accept-Language": "en-us,en",
        "Accept-Encoding": "gzip, deflate",
        "version": APP_VERSION,
    }


def _api_ok(data: dict) -> bool:
    return str((data or {}).get("code")) == "0"


def _mask_phone(phone: str = "") -> str:
    phone = str(phone or "")
    if len(phone) == 11 and phone.isdigit():
        return phone[:3] + "****" + phone[-4:]
    return phone


async def _post_json(cli: httpx.AsyncClient, path: str, body: dict, token: str = "") -> dict:
    r = await cli.post(f"{API_BASE}{path}", json=body, headers=_headers(token))
    try:
        data = r.json()
    except Exception:
        data = {"code": -1, "msg": (r.text or "")[:300]}
    data["_httpStatus"] = r.status_code
    return data


async def _get_user_info(cli: httpx.AsyncClient, access_token: str) -> dict:
    t = int(time.time() * 1000)
    sign = _sign(f"appId={APP_ID}&t={t}")
    r = await cli.get(
        f"{API_BASE}/v1/customer/info",
        params={"appId": APP_ID, "t": t, "sign": sign},
        headers=_headers(access_token),
    )
    try:
        data = r.json()
    except Exception:
        data = {"code": -1, "msg": (r.text or "")[:300]}
    data["_httpStatus"] = r.status_code
    return data


async def mxbc_code_login(code: str, proxy_url: str = "") -> dict:
    kw = {"proxy": proxy_url} if proxy_url else {}
    async with httpx.AsyncClient(timeout=25.0, verify=False, **kw) as cli:
        session = await _post_json(
            cli,
            "/v1/app/code2Session",
            _signed_body({"miniAppId": MINI_APP_ID, "code": code}),
        )
        if not _api_ok(session):
            return {
                "ok": False,
                "stage": "code2Session",
                "error": session.get("msg") or session.get("message") or "code2Session failed",
                "raw": session,
            }
        sdata = session.get("data") or {}
        open_id = sdata.get("openid") or ""
        union_id = sdata.get("unionid") or ""

        login = await _post_json(
            cli,
            "/v1/app/regByUnionid",
            _signed_body({
                "code": code,
                "openId": open_id,
                "unionid": union_id,
                "third": "wxmini",
                "miniAppId": MINI_APP_ID,
            }),
        )
        if not _api_ok(login):
            return {
                "ok": False,
                "stage": "regByUnionid",
                "error": login.get("msg") or login.get("message") or "MXBC login failed",
                "raw": login,
            }
        ldata = login.get("data") or {}
        token = ldata.get("accessToken") or ""
        if not token:
            return {"ok": False, "stage": "regByUnionid", "error": "no accessToken", "raw": login}

        info = await _get_user_info(cli, token)
        info_data = info.get("data") or {}
        info_ok = _api_ok(info)

    mobile = info_data.get("mobilePhone") or ldata.get("mobilePhone") or ""
    account = _mask_phone(mobile) or open_id
    return {
        "ok": True,
        "accessToken": token,
        "mxbcToken": token,
        "cookie": token,
        "account": account,
        "mobilePhone": mobile,
        "customerPoint": info_data.get("customerPoint", 0) if info_ok else 0,
        "openId": open_id,
        "unionId": union_id,
        "code": code,
    }


async def run_mxbc_code_login(user_id: int, openid: str, appid: str, proxy_url: str = "") -> dict:
    from .codebridge import get_code_for_openid

    cr = await get_code_for_openid(openid, appid or MINI_APP_ID)
    if not cr.get("success") or not cr.get("code"):
        return {
            "ok": False,
            "stage": "get-code",
            "error": cr.get("error") or "failed to get MXBC mini-program code",
        }
    login = await mxbc_code_login(cr["code"], proxy_url)
    if not login.get("ok"):
        return {
            "ok": False,
            "stage": login.get("stage") or "mxbc-login",
            "error": login.get("error") or "MXBC code login failed",
            "code": cr["code"],
        }
    return login
