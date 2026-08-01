"""取码桥接：openid → wx.login code。用 DB 账号(guid+accesstoken) 驱动 Python 纯协议(aio)。

关键优化：**会话复用**（朋友思路：握手提前握、别每次重来）。按 openid 缓存登录会话，分三档取码：
  档1  复用整套 {PSK票据 ap, psk_secret, 登录凭据 cred} → 只跑 shortcloud 0-RTT（最快 ~0.6s）
  档2  票据失效但登录还在 → 复用 cred + 重握一次拿新 ap（跳过 login_buffer 1.8s，~0.75s）
  档3  登录也失效 → 完整重建（login_buffer → 握手 → cmd3453 登录），必要时先 refresh token
device_id 随机持久化（env > data/ilink-device-id > 随机）。
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time

import httpx

from . import db
from .config import DATA_DIR
from .pure_wxcode import aio
from .pure_wxcode import shortcloud as SC
from .pure_wxcode import cloud_auth as CA
from .pure_wxcode import ilink_mmtls_client as M
from .pure_wxcode import pure_login as PL
from .pure_wxcode import ilink_packet as P
from .yyblogin import refresh_account

# host_appid 默认回退（YYB）。SYZS(手游助手) 账号会从 login_buffer 解析出自己的 host_appid。
DEFAULT_HOST_APPID = "wxd44977328b36e647"

_STATE: dict = {}
_sess_cache: dict[str, dict] = {}   # openid -> {"ap","psk","cred","proxy","ts"}
_locks: dict[str, asyncio.Lock] = {}
# 对齐朋友 Go 版默认 30 分钟；旧默认 10 分钟太短，热会话更容易掉到重握/重建。
SESS_TTL = int(os.environ.get("YYB_SESSION_TTL", "1800"))  # 登录会话缓存软过期(秒)，可用环境变量覆盖
# 会话池监控计数：0-RTT 整套复用 / 复用 cred 重握 / 完整重建，供 admin 监控接口查看命中率
_stats = {"total": 0, "hit_0rtt": 0, "hit_relogin": 0, "rebuild": 0}


def session_pool_stats() -> dict:
    """MMTLS 会话池监控：缓存条目 / 存活 / 过期 + 0-RTT 复用 vs 重建命中计数。"""
    now = time.time()
    live = sum(1 for c in _sess_cache.values() if c.get("ap") and now < _session_deadline(c))
    stored = 0
    try:
        row = db.query_one("SELECT COUNT(*) AS c FROM yyb_sessions WHERE expires_at>?", (int(now),))
        stored = int(row["c"] if row else 0)
    except Exception:
        stored = 0
    total = max(_stats["total"], 1)
    reuse = _stats["hit_0rtt"] + _stats["hit_relogin"]
    return {
        "cached": len(_sess_cache),          # 缓存里的账号会话总数
        "stored": stored,                    # SQLite 持久化的可用会话，服务重启后可继续 0-RTT
        "live": live,                        # 仍在 TTL 内、可直接 0-RTT 的会话
        "expired": len(_sess_cache) - live,  # 已软过期、下次需重握/重建的会话
        "ttlSeconds": SESS_TTL,
        "totalRequests": _stats["total"],    # 累计取码/云函数/手机号请求次数
        "hit0rtt": _stats["hit_0rtt"],       # 档1：整套 PSK+cred 复用（最快 ~100-300ms）
        "hitRelogin": _stats["hit_relogin"], # 档2：复用 cred 仅重握一次
        "rebuild": _stats["rebuild"],        # 档3：完整重建登录会话（~2-5s）
        "reuseRate": round(reuse / total, 3),  # 复用命中率（越高越快）
    }


class LoginBufferError(RuntimeError):
    """login_buffer 拿不到 —— token/scope 级失效（GetLoginBuffer 40188 invalid scope、被抢新等）。
    刻意区别于握手/传输等瞬时错误：只有这类失败才代表账号真的不可用，才该把 DB 状态如实翻红。"""


def _mark_account_dead(openid: str, reason: str) -> None:
    """把账号置为异常并写明原因，让前端「在线/正常」如实翻红为「异常」，提示用户重扫。"""
    try:
        db.execute("UPDATE accounts SET status='error', status_error=? WHERE openid=?",
                   ((reason or "取码授权失效，请重新扫码")[:300], openid))
    except Exception:
        pass
    invalidate(openid)


def get_device_id() -> bytes:
    env = os.environ.get("YYB_DEVICE_ID")
    if env and len(env.strip()) == 64:
        return bytes.fromhex(env.strip())
    f = DATA_DIR / "ilink-device-id"
    try:
        h = f.read_text(encoding="utf-8").strip()
        if len(h) == 64:
            return bytes.fromhex(h)
    except Exception:
        pass
    d = secrets.token_bytes(32)
    try:
        f.write_text(d.hex(), encoding="utf-8")
    except Exception:
        pass
    return d


async def startup() -> None:
    _STATE["client"] = httpx.AsyncClient(verify=False, timeout=20.0)
    _STATE["device_id"] = get_device_id()
    try:
        _STATE["ip"] = await aio.async_resolve_ip()
    except Exception:
        _STATE["ip"] = ""


async def shutdown() -> None:
    c = _STATE.get("client")
    if c:
        await c.aclose()


def invalidate(openid: str) -> None:
    _sess_cache.pop(openid, None)
    try:
        db.execute("DELETE FROM yyb_sessions WHERE openid=?", (openid,))
    except Exception:
        pass


def _session_deadline(c: dict) -> float:
    return float(c.get("expires_at") or (float(c.get("ts", 0)) + SESS_TTL))


def _save_session(openid: str, proxy: str, ap, psk: bytes, cred,
                  host_appid: str = DEFAULT_HOST_APPID, ilink_appid: str = P.ILINK_APPID) -> int:
    """把可复用会话落库；服务重启后还能直接 0-RTT，速度对齐 Go 版 sessions 表。
    host_appid/ilink_appid 一并持久化，SYZS(手游助手) 会话重启后仍能带对 appid 取码。"""
    expires_at = int(time.time() + SESS_TTL)
    blob = {
        "ap": {
            "psk_type": ap.psk_type,
            "ticket_lifetime_hint": ap.ticket_lifetime_hint,
            "mac_value": ap.mac_value.hex(),
            "key_version": ap.key_version,
            "iv": ap.iv.hex(),
            "encrypted_ticket": ap.encrypted_ticket.hex(),
            "raw": (ap.raw or b"").hex(),
        },
        "psk": psk.hex(),
        "cred": {
            "keys": [k.hex() for k in cred.keys],
            "server_id": cred.server_id.hex(),
            "uin": int(cred.uin),
            "session_token": cred.session_token.hex(),
            "username": cred.username or "",
            "raw_plain": (cred.raw_plain or b"").hex(),
        },
        "host_appid": host_appid,
        "ilink_appid": ilink_appid,
    }
    try:
        db.execute(
            "INSERT INTO yyb_sessions(openid,proxy_url,blob,expires_at,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(openid,proxy_url) DO UPDATE SET "
            "blob=excluded.blob, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (openid, proxy, json.dumps(blob, ensure_ascii=False, separators=(",", ":")),
             expires_at, int(time.time())),
        )
    except Exception:
        pass
    return expires_at


def _load_session(openid: str, proxy: str) -> dict | None:
    """从 SQLite 取回热会话。落库内容只在本机复用，反序列化失败就删掉，避免反复拖慢。"""
    row = db.query_one(
        "SELECT blob, expires_at FROM yyb_sessions WHERE openid=? AND proxy_url=? AND expires_at>?",
        (openid, proxy, int(time.time())),
    )
    if not row:
        return None
    try:
        blob = json.loads(row["blob"] or "{}")
        apd = blob["ap"]
        cd = blob["cred"]
        ap = M.MmtlsPsk(
            int(apd["psk_type"]),
            int(apd["ticket_lifetime_hint"]),
            bytes.fromhex(apd["mac_value"]),
            int(apd["key_version"]),
            bytes.fromhex(apd["iv"]),
            bytes.fromhex(apd["encrypted_ticket"]),
            raw=bytes.fromhex(apd.get("raw") or ""),
        )
        cred = PL.SessionCredential(
            keys=[bytes.fromhex(k) for k in cd["keys"]],
            server_id=bytes.fromhex(cd["server_id"]),
            uin=int(cd["uin"]),
            session_token=bytes.fromhex(cd.get("session_token") or ""),
            username=cd.get("username") or "",
            raw_plain=bytes.fromhex(cd.get("raw_plain") or ""),
        )
        return {"ap": ap, "psk": bytes.fromhex(blob["psk"]), "cred": cred,
                "proxy": proxy, "ts": time.time(), "expires_at": int(row["expires_at"]),
                "host_appid": blob.get("host_appid", DEFAULT_HOST_APPID),
                "ilink_appid": blob.get("ilink_appid", P.ILINK_APPID)}
    except Exception:
        try:
            db.execute("DELETE FROM yyb_sessions WHERE openid=? AND proxy_url=?", (openid, proxy))
        except Exception:
            pass
        return None


def _lock(openid: str) -> asyncio.Lock:
    lk = _locks.get(openid)
    if lk is None:
        lk = _locks[openid] = asyncio.Lock()
    return lk


def parse_login_buffer_full(login_buffer_b64: str) -> dict:
    """解析 login_buffer → {auth_payload, ilink_appid, host_appid}。
    YYB / SYZS(手游助手) 的 login_buffer 同一 protobuf 格式：f1=auth_payload, f2=ilink_appid, f3=host_appid。
    YYB 若缺 f2/f3 会回退到默认（P.ILINK_APPID / DEFAULT_HOST_APPID），行为与旧版一致。"""
    import base64 as _b64
    raw = _b64.b64decode(login_buffer_b64, validate=True)
    f = CA.parse_pb(raw) if hasattr(CA, "parse_pb") else PL.parse_pb(raw)
    def _s(fid):
        v = f.get(fid)
        if not v:
            return ""
        return (v[-1] if isinstance(v, list) else v).decode("utf-8", "replace")
    auth = f.get(1)
    return {
        "auth_payload": (auth[-1] if isinstance(auth, list) else auth) if auth else b"",
        "ilink_appid": _s(2),
        "host_appid": _s(3),
    }


async def _login_session(a) -> object:
    """完整登录：login_buffer(一次性) → 握手 + cmd3453 → (ap, psk, cred, host_appid, ilink_appid)。
    动态从 login_buffer 解析 ilink_appid / host_appid，兼容 YYB 与 SYZS(手游助手)。"""
    # 取码链路直连：账号绑定的地区代理只用于「扫码登录」，取码/云函数/手机号重建登录一律不走代理。
    proxy = ""
    r = await aio.async_fetch_login_buffer(
        _STATE["client"],
        a["guid"],
        a["accesstoken"],
        unionid=a["openid"],
        user_id=a["openid"],
        user_type=0,
        refresh_token=a["refreshtoken"],
        proxy_url=proxy,
    )
    if r.get("code") != 0 or not r.get("login_buffer"):
        raise LoginBufferError(f"取 login_buffer 失败: {r.get('msg')}（token 过期/被抢新，2h 内重试或重扫）")
    parsed = parse_login_buffer_full(r["login_buffer"])
    ld = parsed["auth_payload"] or CA.login_data_from_buffer(r["login_buffer"])
    ilink_appid = parsed["ilink_appid"] or P.ILINK_APPID
    # host_appid 回退：buffer 解析值 → 账号 DB appid（SYZS=wxef99873dbfab493c）→ 默认 YYB
    try:
        acc_appid = a["appid"]
    except (KeyError, IndexError, TypeError):
        acc_appid = ""
    host_appid = parsed["host_appid"] or acc_appid or DEFAULT_HOST_APPID
    ap, psk, cred = await aio.async_prepare_session(
        _STATE["device_id"], ld, proxy_url=proxy, ilink_appid=ilink_appid)
    return ap, psk, cred, host_appid, ilink_appid


def _rv(b, p):
    o = s = 0
    while True:
        x = b[p]; p += 1; o |= (x & 0x7F) << s
        if x < 0x80:
            return o, p
        s += 7


def extract_json(b: bytes) -> str:
    """从响应 protobuf 里抽出业务 JSON 字符串（wx_phone / err_code 等），取最长的一段。"""
    best = ""
    def walk(x):
        nonlocal best
        p, n = 0, len(x)
        while p < n:
            try:
                tag, p = _rv(x, p)
            except Exception:
                return
            wt = tag & 7
            if wt == 2:
                try:
                    l, p = _rv(x, p)
                except Exception:
                    return
                v = x[p:p + l]; p += l
                s = None
                try:
                    s = v.decode("utf-8")
                except Exception:
                    pass
                if s and s.lstrip().startswith("{") and ("mobile" in s or "err" in s or "code" in s or "data" in s):
                    if len(s) > len(best):
                        best = s
                elif 2 < l < 8000 and (s is None or not s.isprintable()):
                    walk(v)
            elif wt == 0:
                _, p = _rv(x, p)
            elif wt == 5:
                p += 4
            elif wt == 1:
                p += 8
            else:
                return
    walk(b)
    return best


async def _send(ap, psk, cred, payload: bytes, proxy: str, ilink_appid: str = P.ILINK_APPID) -> bytes | None:
    """发一次 shortcloud 0-RTT，返回响应明文 payload（bytes）或 None（传输失败/link_err）。"""
    info = await aio.async_getcode_with_session(ap, psk, cred, payload,
                                                ip=_STATE.get("ip", ""), proxy_url=proxy,
                                                ilink_appid=ilink_appid)
    if not info or info.get("link_err") or not info.get("payload"):
        return None
    return info["payload"]


async def _sender_code(ap, psk, cred, appid, data, proxy,
                       host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    pt = await _send(ap, psk, cred, SC.build_getcode_payload(appid, host_appid=host_appid),
                     proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    code = aio.extract_code(pt)
    return {"code": code} if code else None


async def _sender_cloud(ap, psk, cred, appid, data, proxy,
                        host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    pt = await _send(ap, psk, cred, SC.build_operatewxdata_payload(appid, data or "{}", host_appid=host_appid),
                     proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    return {"respJson": extract_json(pt)}


async def _sender_phone(ap, psk, cred, appid, data, proxy,
                        host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    pt = await _send(ap, psk, cred, SC.build_getphone_payload(appid, data or "", host_appid=host_appid),
                     proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    js = extract_json(pt)
    out = {"respJson": js}
    try:
        d = json.loads(js) if js else {}
        wp = d.get("wx_phone") or {}
        if wp:
            out["mobile"] = wp.get("mobile", "")
            _m = str(out["mobile"] or "")
            out["masked_phone"] = f"{_m[:3]}****{_m[-4:]}" if len(_m) >= 7 else _m
            out["encryptedData"] = wp.get("encryptedData", "")
            out["iv"] = wp.get("iv", "")
            out["cloudId"] = wp.get("cloud_id", "")
            inner = wp.get("data")
            if isinstance(inner, str):
                try:
                    out["code"] = (json.loads(inner) or {}).get("code", "")
                except Exception:
                    pass
        out["customPhoneList"] = d.get("custom_phone_list") or []
    except Exception:
        pass
    return out


async def _sender_userinfo(ap, psk, cred, appid, data, proxy,
                           host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    # wx.getUserInfo：走 js-operatewxdata(cmdid=1133) 的 webapi_getuserinfo，与取手机号同一条已验证链路。
    # 内层 JSON 对齐 WMPF 抓包：{"api_name":"webapi_getuserinfo","with_credentials":true,"data":{}}。
    data_json = json.dumps(
        {"api_name": "webapi_getuserinfo", "with_credentials": True, "data": {}},
        separators=(",", ":"))
    pt = await _send(ap, psk, cred, SC.build_operatewxdata_payload(appid, data_json, host_appid=host_appid),
                     proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    js = extract_json(pt)
    out = {"respJson": js}
    try:
        d = json.loads(js) if js else {}
        # err_no=0 且带 encryptedData/iv 才算拿到用户信息授权数据；否则仅回 respJson 供排查（scope 未授权等）。
        if d.get("err_no", 0) == 0 and d.get("encryptedData") and d.get("iv"):
            out["rawData"] = d.get("data", "")
            out["signature"] = d.get("signature", "")
            out["encryptedData"] = d.get("encryptedData", "")
            out["iv"] = d.get("iv", "")
            out["cloudId"] = d.get("cloud_id", "")
        else:
            out["errNo"] = d.get("err_no")
            out["errMsg"] = d.get("err_msg") or d.get("errmsg", "")
    except Exception:
        pass
    return out


# ───────────────────────── 云函数 callFunction / 云托管 callContainer ─────────────────────────
# 走 js-operatewxdata(cmdid=1133) 的 qbase_commapi 通道（build_cloud_operatewxdata_payload）。
# JSON 结构 1:1 对齐朋友 WMPF 抓包 wxhook/server/protocol.py:do_cloud_call_function / do_cloud_call_container。
_CLOUD_SDK_VERSION = "wx-miniprogram-sdk/3.15.3 (1781252964000 platform/android})"
_CLOUD_CONTAINER_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 6 Build/TQ3A.230605.010; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.178 "
    "Mobile Safari/537.36 XWEB/1460205 MMWEBSDK/20260202 MMWEBID/3713 "
    "MicroMessenger/8.0.70.3060(0x28004652) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)


def _build_cloud_function_data_json(function_name: str, function_data, cloud_env: str = "") -> str:
    """wx.cloud.callFunction → qbase_commapi/tcbapi_slowcallfunction_v2 的 operatewxdata JSON。"""
    ts_ms = str(int(time.time() * 1000))
    call_id = f"{ts_ms}-{secrets.token_hex(6)}"
    cli_req_id = f"{ts_ms}_{secrets.token_hex(6)}"
    if isinstance(function_data, (dict, list)):
        fdata = json.dumps(function_data, separators=(",", ":"), ensure_ascii=False)
    else:
        fdata = str(function_data or "")
    qbase_req = json.dumps({
        "function_name": function_name,
        "data": fdata,
        "action": 1, "scene": 1, "call_id": call_id, "cloudid_list": [],
    }, separators=(",", ":"), ensure_ascii=False)
    payload = {
        "api_name": "qbase_commapi",
        "data": {
            "qbase_api_name": "tcbapi_slowcallfunction_v2",
            "qbase_req": qbase_req,
            "qbase_options": ({"env": cloud_env} if cloud_env else {}),
            "qbase_meta": {"session_id": ts_ms, "filter_user_info": False,
                           "sdk_version": _CLOUD_SDK_VERSION},
            "cli_req_id": cli_req_id,
        },
        "operate_directly": False, "env": 1,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _build_cloud_container_data_json(cloud_host: str, path: str, method: str,
                                     headers_extra: dict, data, target_appid: str) -> str:
    """wx.cloud.callContainer → qbase_commapi/tcbapi_call_gateway 的 operatewxdata JSON。"""
    ts_ms = str(int(time.time() * 1000))
    cli_req_id = f"{ts_ms}_{secrets.token_hex(6)}"
    call_id = f"v2-{ts_ms}-{secrets.token_hex(4)}"

    req_headers = [{"k": "content-type", "v": "application/json"}]
    provided = set()
    for k, v in (headers_extra or {}).items():
        req_headers.append({"k": k, "v": str(v)})
        provided.add(str(k).lower())
    # WMPF 同款业务头：InvokeType=inside 让网关把 REROUTE 走【内部网络路由】到目标站，
    # 以内网/可信 IP 到达，绕过目标站公网 WAF（这正是"用 WMPF 不被拦"的根因）。deviceToken 占位对齐。
    if "invoketype" not in provided:
        req_headers.append({"k": "InvokeType", "v": "inside"})
    if "devicetoken" not in provided:
        req_headers.append({"k": "deviceToken", "v": ""})
    req_headers.extend([
        {"k": "X-WX-HTTP-MODE", "v": "REROUTE"},
        {"k": "User-Agent", "v": _CLOUD_CONTAINER_UA},
        {"k": "referer", "v": f"https://servicewechat.com/{target_appid}/0/page-frame.html"},
        {"k": "X-WX-HTTP-HOST", "v": cloud_host},
        {"k": "X-WX-HTTP-PATH", "v": path},
    ])
    qbase_req_obj = {
        "method": (method or "GET").lower(), "headers": req_headers,
        "data_type": 0, "action": 1, "retryType": 0, "call_id": call_id, "user_timeout": 60000,
    }
    data_str = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if data_str:
        qbase_req_obj["data"] = data_str
    qbase_req = json.dumps(qbase_req_obj, separators=(",", ":"), ensure_ascii=False)
    # 对齐东鹏机场.py(WMPF 生产验证)的直连形状：operate_directly=True、无 env、qbase_options.rand、
    # session_id 略早于当前时刻。operate_directly=False+env=1 会走云环境作用域路由（公网 egress，易被目标 WAF 拦）。
    rand_nonce = f"0.{secrets.randbelow(10 ** 16):016d}"
    payload = {
        "api_name": "qbase_commapi",
        "data": {
            "qbase_api_name": "tcbapi_call_gateway",
            "qbase_req": qbase_req,
            "qbase_options": {"rand": rand_nonce},
            "qbase_meta": {"session_id": str(int(ts_ms) - (300 + secrets.randbelow(600))),
                           "sdk_version": _CLOUD_SDK_VERSION},
            "cli_req_id": cli_req_id,
        },
        "operate_directly": True,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _parse_cloud_resp(pt: bytes) -> dict:
    """云调用响应解析：抽出 JSON 字符串，并尽量解析成对象；始终附 respJson 供排查。"""
    js = extract_json(pt)
    out = {"respJson": js}
    try:
        if js:
            out["data"] = json.loads(js)
    except Exception:
        pass
    return out


async def _sender_cloud_function(ap, psk, cred, appid, data, proxy,
                                 host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    d = data or {}
    data_json = _build_cloud_function_data_json(
        d.get("function_name", ""), d.get("function_data", {}), d.get("cloud_env", ""))
    pt = await _send(ap, psk, cred, SC.build_cloud_operatewxdata_payload(appid, data_json, host_appid=host_appid),
                     proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    return _parse_cloud_resp(pt)


# ── 云托管 WAF 兜底 ──
# 网关 REROUTE 到业务域名时，出口 IP 常被腾讯云 WAF 403 拦截（东鹏机场.py 同样现象）。
# 兜底：命中 WAF → 用直连（优先走账号/指定代理）直接打目标 URL，绕过云网关那段被拦的链路。
_WAF_MARKERS = ("WAF拦截页面", "腾讯云WAF", "您的请求已中断", "waf-attack-feedback", "block-pages/403")


def _looks_like_waf(text) -> bool:
    t = str(text or "")
    return any(m in t for m in _WAF_MARKERS)


def _container_layer2(out: dict):
    """从云托管响应里取 layer2（含 http_code / 上游 body / headers）。
    out["data"] = {"data": "<layer2 json 串>", "err_no": 0} → 再解一层。"""
    try:
        d = out.get("data")
        if isinstance(d, dict):
            inner = d.get("data")
            if isinstance(inner, str):
                return json.loads(inner)
            if isinstance(inner, dict):
                return inner
    except Exception:
        pass
    return None


def _resolve_container_url(cloud_host: str, path: str) -> str:
    """REROUTE 模式 path 本就是完整 URL；相对路径则拼到 cloud_host 上。"""
    p = str(path or "")
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if p and not p.startswith("/"):
        p = "/" + p
    return f"https://{cloud_host}{p}"


async def _direct_http_request(url: str, method: str, headers_extra: dict, data,
                               proxy_url: str, appid: str) -> dict:
    """WAF 兜底直连：以微信小程序容器 UA + 业务头直接请求目标 URL（可走代理）。"""
    hdrs = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://servicewechat.com/{appid}/0/page-frame.html",
        "User-Agent": _CLOUD_CONTAINER_UA,
        "X-Requested-With": "com.tencent.mm",
        "deviceToken": "",
    }
    for k, v in (headers_extra or {}).items():
        hdrs[k] = str(v)
    method_l = (method or "GET").lower()
    kw = {"proxy": proxy_url} if proxy_url else {}
    try:
        async with httpx.AsyncClient(timeout=25.0, verify=False, trust_env=False,
                                     follow_redirects=True, **kw) as cli:
            if method_l == "post":
                body = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
                resp = await cli.post(url, headers=hdrs, content=(body or None))
            else:
                resp = await cli.get(url, headers=hdrs)
    except Exception as e:
        return {"ok": False, "error": f"direct request failed: {e}", "viaProxy": bool(proxy_url)}
    text = resp.text
    result = {"ok": True, "statusCode": resp.status_code, "viaProxy": bool(proxy_url)}
    if resp.status_code in (403, 412) or _looks_like_waf(text):
        result["ok"] = False
        result["wafBlockedDirect"] = True
        result["bodyText"] = text[:500]
        return result
    try:
        result["data"] = json.loads(text)
    except Exception:
        result["bodyText"] = text[:2000]
    return result


async def _sender_cloud_container(ap, psk, cred, appid, data, proxy,
                                  host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    d = data or {}
    data_json = _build_cloud_container_data_json(
        d.get("cloud_host", ""), d.get("path", ""), d.get("method", "GET"),
        d.get("headers") or {}, d.get("data", ""), appid)
    pt = await _send(ap, psk, cred, SC.build_cloud_operatewxdata_payload(appid, data_json, host_appid=host_appid),
                     proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    out = _parse_cloud_resp(pt)

    # WAF 兜底：网关 REROUTE 命中目标站 WAF → 直连重试
    layer2 = _container_layer2(out)
    if layer2 is not None and _looks_like_waf(layer2.get("data")):
        out["wafBlocked"] = True
        proxy_url = d.get("proxy_url", "") or ""
        url = _resolve_container_url(d.get("cloud_host", ""), d.get("path", ""))
        direct = await _direct_http_request(
            url, d.get("method", "GET"), d.get("headers") or {}, d.get("data", ""),
            proxy_url, appid)
        out["wafFallback"] = direct
        out["viaProxy"] = bool(proxy_url)
        if direct.get("ok"):
            # 直连成功拿到业务响应 → 提升为主结果（respJson 仍保留原网关响应供排查）
            if "data" in direct:
                out["data"] = direct["data"]
            elif "bodyText" in direct:
                out["bodyText"] = direct["bodyText"]
    return out


def _fail(err, openid, appid):
    return {"success": False, "error": err, "openid": openid, "appid": appid}


async def _run_with_session(openid: str, appid: str, data, sender) -> dict:
    """通用三档会话执行：sender(ap,psk,cred,appid,data,proxy) 返回业务结果 dict 或 None（失败重试下一档）。"""
    if not openid:
        return _fail("openid required", openid, appid)
    if not appid:
        return _fail("appid required", openid, appid)
    _stats["total"] += 1
    a = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
    if not a:
        return _fail("account not found (未扫码或已删除)", openid, appid)
    # 取码链路直连：账号绑定的地区代理只用于「扫码登录」，取 Code / 云函数 / 手机号 / OAuth 一律直连出网，
    # 不再复用账号代理（账号 proxy_url 仍由扫码登录写入，供自动续期与运行项目的业务接口使用）。
    proxy = ""
    was_error = (a["status"] == "error")   # 记住入口状态：这次若取码成功，好把已恢复/误标的账号自愈回「正常」

    def _ok(res, kind="hit_0rtt"):
        _stats[kind] = _stats.get(kind, 0) + 1
        if was_error:   # 之前被标异常，本次却取码成功 → 账号已恢复，清掉异常状态
            try:
                db.execute("UPDATE accounts SET status='active', status_error='' WHERE openid=?", (openid,))
            except Exception:
                pass
        return {"success": True, "openid": openid, "appid": appid, **res}

    def _fresh_cache():
        now = time.time()
        c = _sess_cache.get(openid)
        if c and c.get("proxy") == proxy and c.get("ap") and now < _session_deadline(c):
            return c
        c = _load_session(openid, proxy)
        if c:
            _sess_cache[openid] = c
            return c
        return None

    # 档1（无锁快速路径）：复用整套会话
    c = _fresh_cache()
    if c and c.get("ap"):
        _ha = c.get("host_appid", DEFAULT_HOST_APPID); _ia = c.get("ilink_appid", P.ILINK_APPID)
        try:
            res = await sender(c["ap"], c["psk"], c["cred"], appid, data, proxy, _ha, _ia)
            if res:
                return _ok(res)
        except Exception:
            pass
        c["ap"] = c["psk"] = None

    async with _lock(openid):
        c = _fresh_cache()
        if c and c.get("ap"):
            _ha = c.get("host_appid", DEFAULT_HOST_APPID); _ia = c.get("ilink_appid", P.ILINK_APPID)
            try:
                res = await sender(c["ap"], c["psk"], c["cred"], appid, data, proxy, _ha, _ia)
                if res:
                    return _ok(res)
            except Exception:
                pass

        # 档2：复用登录凭据，只重握一次拿新票据
        if c and c.get("cred"):
            _ha = c.get("host_appid", DEFAULT_HOST_APPID); _ia = c.get("ilink_appid", P.ILINK_APPID)
            try:
                ap, psk = await aio.async_fresh_psk(proxy_url=proxy)
                res = await sender(ap, psk, c["cred"], appid, data, proxy, _ha, _ia)
                if res:
                    expires_at = _save_session(openid, proxy, ap, psk, c["cred"], _ha, _ia)
                    _sess_cache[openid] = {"ap": ap, "psk": psk, "cred": c["cred"],
                                           "proxy": proxy, "ts": time.time(), "expires_at": expires_at,
                                           "host_appid": _ha, "ilink_appid": _ia}
                    return _ok(res, "hit_relogin")
            except Exception:
                pass
            invalidate(openid)

        # 档3：完整重建（login_buffer 失效则先 refresh 再重试一次）
        try:
            ap, psk, cred, host_appid, ilink_appid = await _login_session(a)
        except LoginBufferError as e1:
            # login_buffer 拿不到 = token/scope 级失效 → 先 refresh 换新 access_token 再重试一次
            try:
                await asyncio.to_thread(refresh_account, openid)
                a = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
                ap, psk, cred, host_appid, ilink_appid = await _login_session(a)
            except LoginBufferError as e2:
                # 刷新后仍拿不到 buffer → 应用宝运行态 scope 已死、refresh 救不回 → 如实翻红，提示重扫
                _mark_account_dead(openid, str(e2) or str(e1))
                return _fail(str(e2) or str(e1), openid, appid)
            except Exception as e2:
                # 刷新成功但握手/传输失败 → 瞬时问题，不动 status
                return _fail(str(e2) or str(e1), openid, appid)
        except Exception as e1:
            # login_buffer 之外的失败（握手/传输）→ 瞬时问题，token 本身没问题，不 refresh、不动 status
            return _fail(str(e1), openid, appid)
        expires_at = _save_session(openid, proxy, ap, psk, cred, host_appid, ilink_appid)
        _sess_cache[openid] = {"ap": ap, "psk": psk, "cred": cred, "proxy": proxy,
                               "ts": time.time(), "expires_at": expires_at,
                               "host_appid": host_appid, "ilink_appid": ilink_appid}
        try:
            res = await sender(ap, psk, cred, appid, data, proxy, host_appid, ilink_appid)
            if res:
                return _ok(res, "rebuild")
            return _fail("shortcloud 无有效响应", openid, appid)
        except Exception as e:
            invalidate(openid)
            return _fail(str(e), openid, appid)


async def get_code_for_openid(openid: str, appid: str) -> dict:
    return await _run_with_session(openid, appid, None, _sender_code)


async def invoke_cloud_for_openid(openid: str, appid: str, data_json: str = "") -> dict:
    return await _run_with_session(openid, appid, data_json, _sender_cloud)


async def get_phone_for_openid(openid: str, appid: str, data_json: str = "") -> dict:
    return await _run_with_session(openid, appid, data_json, _sender_phone)


async def get_userinfo_for_openid(openid: str, appid: str) -> dict:
    return await _run_with_session(openid, appid, None, _sender_userinfo)


async def cloud_call_function_for_openid(openid: str, appid: str, function_name: str,
                                         function_data=None, cloud_env: str = "") -> dict:
    """wx.cloud.callFunction：调用小程序云函数（qbase_commapi/tcbapi_slowcallfunction_v2）。"""
    return await _run_with_session(
        openid, appid,
        {"function_name": function_name, "function_data": function_data or {}, "cloud_env": cloud_env},
        _sender_cloud_function)


async def cloud_call_container_for_openid(openid: str, appid: str, cloud_host: str, path: str,
                                          method: str = "GET", headers: dict | None = None,
                                          data="", proxy_url: str = "", direct: bool = False) -> dict:
    """wx.cloud.callContainer：调用小程序云托管容器（qbase_commapi/tcbapi_call_gateway）。
    - 默认：走腾讯网关，命中目标站 WAF 时自动直连兜底。
    - direct=True：强制直连模式——完全跳过腾讯网关，直接 HTTP 打目标 URL（走代理），
      绕开被 WAF 拦的网关 egress。此模式不需要 ilink 会话（path 里已含 code 等参数）。
    proxy_url 优先本次入参，留空回退账号扫码绑定的地区代理。"""
    if not proxy_url:
        try:
            _acc = db.query_one("SELECT proxy_url FROM accounts WHERE openid=?", (openid,))
            proxy_url = ((_acc["proxy_url"] or "") if _acc else "")
        except Exception:
            proxy_url = ""

    if direct:
        url = _resolve_container_url(cloud_host, path)
        d = await _direct_http_request(url, method, headers or {}, data, proxy_url, appid)
        out = {"success": bool(d.get("ok")), "openid": openid, "appid": appid,
               "direct": True, "viaProxy": bool(proxy_url), "statusCode": d.get("statusCode")}
        if "data" in d:
            out["data"] = d["data"]
        if "bodyText" in d:
            out["bodyText"] = d["bodyText"]
        if d.get("wafBlockedDirect"):
            out["wafBlockedDirect"] = True
        if d.get("error"):
            out["error"] = d["error"]
        return out

    return await _run_with_session(
        openid, appid,
        {"cloud_host": cloud_host, "path": path, "method": method,
         "headers": headers or {}, "data": data, "proxy_url": proxy_url},
        _sender_cloud_container)


# ───────────────── OAuth2 公众号授权（cmdid=1254/1255，复用三档会话缓存） ─────────────────

async def _sender_oauth_authorize(ap, psk, cred, appid, data, proxy,
                                  host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    d = data or {}
    payload = SC.build_oauth_authorize_payload(
        appid, d.get("url", ""),
        biz_username=d.get("biz_username", ""), scene=d.get("scene", 0),
        referrer_url=d.get("referrer_url", ""), sub_scene=d.get("sub_scene"),
        auto_oauth=d.get("auto_oauth"), host_appid=host_appid)
    pt = await _send(ap, psk, cred, payload, proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    r = aio.parse_oauth_response(pt)
    r.pop("payload", None)   # bytes，不参与 JSON 序列化
    return r


async def _sender_oauth_confirm(ap, psk, cred, appid, data, proxy,
                                host_appid=DEFAULT_HOST_APPID, ilink_appid=P.ILINK_APPID):
    d = data or {}
    payload = SC.build_oauth_authorize_confirm_payload(
        appid, d.get("oauth_url", ""),
        opt=d.get("opt", 0), avatar_id=d.get("avatar_id", ""),
        redirect_uri=d.get("redirect_uri", ""), host_appid=host_appid)
    pt = await _send(ap, psk, cred, payload, proxy, ilink_appid=ilink_appid)
    if not pt:
        return None
    r = aio.parse_oauth_response(pt)
    r.pop("payload", None)
    return r


async def oauth_authorize_for_openid(openid: str, appid: str, url: str, *,
                                     biz_username: str = "", scene: int = 0,
                                     referrer_url: str = "", sub_scene=None,
                                     auto_oauth=None) -> dict:
    """公众号 OAuth2 授权：提交授权 URL → 返回 scope_list/avatar_list/redirect_url。"""
    data = {"url": url, "biz_username": biz_username, "scene": scene,
            "referrer_url": referrer_url, "sub_scene": sub_scene, "auto_oauth": auto_oauth}
    return await _run_with_session(openid, appid, data, _sender_oauth_authorize)


async def oauth_confirm_for_openid(openid: str, appid: str, oauth_url: str, *,
                                   opt: int = 0, avatar_id: str = "",
                                   redirect_uri: str = "") -> dict:
    """公众号 OAuth2 确认授权：返回 redirect_url（其中可能含网页授权 code）。"""
    data = {"oauth_url": oauth_url, "opt": opt, "avatar_id": avatar_id, "redirect_uri": redirect_uri}
    return await _run_with_session(openid, appid, data, _sender_oauth_confirm)
