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
from .yyblogin import refresh_account

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


def _save_session(openid: str, proxy: str, ap, psk: bytes, cred) -> int:
    """把可复用会话落库；服务重启后还能直接 0-RTT，速度对齐 Go 版 sessions 表。"""
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
                "proxy": proxy, "ts": time.time(), "expires_at": int(row["expires_at"])}
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


async def _login_session(a) -> object:
    """完整登录：login_buffer(一次性) → 握手 + cmd3453 → (ap, psk, cred)。返回 (ap, psk, cred)。"""
    proxy = a["proxy_url"] or ""
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
    ld = CA.login_data_from_buffer(r["login_buffer"])
    return await aio.async_prepare_session(_STATE["device_id"], ld, proxy_url=proxy)


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


async def _send(ap, psk, cred, payload: bytes, proxy: str) -> bytes | None:
    """发一次 shortcloud 0-RTT，返回响应明文 payload（bytes）或 None（传输失败/link_err）。"""
    info = await aio.async_getcode_with_session(ap, psk, cred, payload,
                                                ip=_STATE.get("ip", ""), proxy_url=proxy)
    if not info or info.get("link_err") or not info.get("payload"):
        return None
    return info["payload"]


async def _sender_code(ap, psk, cred, appid, data, proxy):
    pt = await _send(ap, psk, cred, SC.build_getcode_payload(appid), proxy)
    if not pt:
        return None
    code = aio.extract_code(pt)
    return {"code": code} if code else None


async def _sender_cloud(ap, psk, cred, appid, data, proxy):
    pt = await _send(ap, psk, cred, SC.build_operatewxdata_payload(appid, data or "{}"), proxy)
    if not pt:
        return None
    return {"respJson": extract_json(pt)}


async def _sender_phone(ap, psk, cred, appid, data, proxy):
    pt = await _send(ap, psk, cred, SC.build_getphone_payload(appid, data or ""), proxy)
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
    proxy = a["proxy_url"] or ""
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
        try:
            res = await sender(c["ap"], c["psk"], c["cred"], appid, data, proxy)
            if res:
                return _ok(res)
        except Exception:
            pass
        c["ap"] = c["psk"] = None

    async with _lock(openid):
        c = _fresh_cache()
        if c and c.get("ap"):
            try:
                res = await sender(c["ap"], c["psk"], c["cred"], appid, data, proxy)
                if res:
                    return _ok(res)
            except Exception:
                pass

        # 档2：复用登录凭据，只重握一次拿新票据
        if c and c.get("cred"):
            try:
                ap, psk = await aio.async_fresh_psk(proxy_url=proxy)
                res = await sender(ap, psk, c["cred"], appid, data, proxy)
                if res:
                    expires_at = _save_session(openid, proxy, ap, psk, c["cred"])
                    _sess_cache[openid] = {"ap": ap, "psk": psk, "cred": c["cred"],
                                           "proxy": proxy, "ts": time.time(), "expires_at": expires_at}
                    return _ok(res, "hit_relogin")
            except Exception:
                pass
            invalidate(openid)

        # 档3：完整重建（login_buffer 失效则先 refresh 再重试一次）
        try:
            ap, psk, cred = await _login_session(a)
        except LoginBufferError as e1:
            # login_buffer 拿不到 = token/scope 级失效 → 先 refresh 换新 access_token 再重试一次
            try:
                await asyncio.to_thread(refresh_account, openid)
                a = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
                ap, psk, cred = await _login_session(a)
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
        expires_at = _save_session(openid, proxy, ap, psk, cred)
        _sess_cache[openid] = {"ap": ap, "psk": psk, "cred": cred, "proxy": proxy,
                               "ts": time.time(), "expires_at": expires_at}
        try:
            res = await sender(ap, psk, cred, appid, data, proxy)
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


# ───────────────── OAuth2 公众号授权（cmdid=4313，复用三档会话缓存） ─────────────────

async def _sender_oauth_authorize(ap, psk, cred, appid, data, proxy):
    d = data or {}
    payload = SC.build_oauth_authorize_payload(
        appid, d.get("url", ""),
        biz_username=d.get("biz_username", ""), scene=d.get("scene", 0),
        referrer_url=d.get("referrer_url", ""), sub_scene=d.get("sub_scene"),
        auto_oauth=d.get("auto_oauth"))
    pt = await _send(ap, psk, cred, payload, proxy)
    if not pt:
        return None
    r = aio.parse_oauth_response(pt)
    r.pop("payload", None)   # bytes，不参与 JSON 序列化
    return r


async def _sender_oauth_confirm(ap, psk, cred, appid, data, proxy):
    d = data or {}
    payload = SC.build_oauth_authorize_confirm_payload(
        appid, d.get("oauth_url", ""),
        opt=d.get("opt", 0), avatar_id=d.get("avatar_id", ""),
        redirect_uri=d.get("redirect_uri", ""))
    pt = await _send(ap, psk, cred, payload, proxy)
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
