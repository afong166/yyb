"""扫码登录 OAuth + 微信账号存储/续期/资料。
QR 流程复用已验证的 pure_wxcode.oauth（fetch_qr → poll_for_code → oauth_exchange），
在后台线程里跑；账号(含 token)按 user_id 存进 DB accounts 表。"""
from __future__ import annotations

import base64
import re
import secrets
import threading
import time

import requests

from . import db
from .pure_wxcode import oauth as O, pcyyb

WX_APPID = "wxd44977328b36e647"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0 Safari/537.36"
)

_sessions: dict[str, dict] = {}
_slock = threading.Lock()


def _safe_yyb_error(msg: object) -> str:
    """应用宝错误 msg 偶尔会带 token；入库/返回前统一脱敏，避免前端和日志泄露敏感凭据。"""
    text = str(msg or "")
    text = re.sub(r"(?i)(access_token|refresh_token|token)\s*[:= ]\s*[^,\s]+", r"\1 [已隐藏]", text)
    return text[:300]


def _make_session(proxy_url: str = "") -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {"http": None, "https": None}
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


# ---------------- 乱码修复 ----------------
def _demojibake(s: str) -> str:
    """还原「UTF-8 被误按 latin-1/ISO-8859-1 解码」的乱码。
    正常中文（码点>255，无法 latin-1 编码）、纯 ASCII、无法还原者均原样返回 —— 安全且幂等。"""
    if not s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def repair_account_text() -> int:
    """一次性修复历史账号里被误解码的昵称/地区文本。返回实际修复的账号数（幂等，可重复调用）。"""
    rows = db.query("SELECT openid, nickname, country, province, city FROM accounts")
    fixed = 0
    for r in rows:
        new = {c: _demojibake(r[c] or "") for c in ("nickname", "country", "province", "city")}
        if any(new[c] != (r[c] or "") for c in new):
            db.execute("UPDATE accounts SET nickname=?, country=?, province=?, city=? WHERE openid=?",
                       (new["nickname"], new["country"], new["province"], new["city"], r["openid"]))
            fixed += 1
    return fixed


# ---------------- 微信资料 / 续期（sync，requests） ----------------
def fetch_userinfo(access_token: str, openid: str, proxy_url: str = "") -> dict:
    try:
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        r = requests.get(
            "https://api.weixin.qq.com/sns/userinfo",
            params={"access_token": access_token, "openid": openid, "lang": "zh_CN"},
            timeout=15, proxies=proxies,
        )
        r.encoding = "utf-8"  # 微信 userinfo 返回 UTF-8 但无 charset 头，requests 默认 ISO-8859-1 会致中文昵称乱码
        d = r.json()
        if d.get("errcode"):
            return {}
        return {
            "nickname": d.get("nickname", ""), "head_img_url": d.get("headimgurl", ""),
            "unionid": d.get("unionid", ""), "sex": d.get("sex", 0) or 0,
            "country": d.get("country", ""), "province": d.get("province", ""), "city": d.get("city", ""),
        }
    except Exception:
        return {}


def _upsert_account(user_id: int, fields: dict) -> None:
    openid = fields["openid"]
    cols = ["user_id", "accesstoken", "refreshtoken", "guid", "appid", "scope", "expires_in",
            "expire_at", "nickname", "head_img_url", "unionid", "sex", "country", "province",
            "city", "proxy_url", "status", "status_error", "logged_at", "updated_at"]
    exist = db.query_one("SELECT openid FROM accounts WHERE openid=?", (openid,))
    vals = {c: fields.get(c) for c in cols}
    vals["user_id"] = user_id
    vals["updated_at"] = db.now()
    if exist:
        sets = ", ".join(f"{c}=?" for c in cols if fields.get(c) is not None or c in ("user_id", "updated_at"))
        params = tuple(vals[c] for c in cols if fields.get(c) is not None or c in ("user_id", "updated_at"))
        db.execute(f"UPDATE accounts SET {sets} WHERE openid=?", params + (openid,))
    else:
        allcols = ["openid"] + cols
        db.execute(
            f"INSERT INTO accounts({','.join(allcols)}) VALUES ({','.join('?' * len(allcols))})",
            (openid,) + tuple(vals.get(c, "") if vals.get(c) is not None else "" for c in cols),
        )


def refresh_account(openid: str) -> dict:
    """用应用宝续期接口刷新运行态 token，并验证新 token 能重新拿到 login_buffer。"""
    a = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
    if not a:
        return {"ok": False, "error": "account not found"}
    if not a["refreshtoken"]:
        return {"ok": False, "error": "无 refresh_token，需重新扫码"}
    px = a["proxy_url"] or ""
    proxies = {"http": px, "https": px} if px else None

    try:
        body = {
            "userInfo": {
                "openId": openid,
                "refreshToken": a["refreshtoken"],
                "accessToken": a["accesstoken"],
                "loginType": "WX",
            }
        }
        headers, body_str = pcyyb.ual_headers(body, "pc_yyb_auth", pcyyb.PC_YYB_AUTH_KEY)
        r = requests.post(
            f"{pcyyb.HOST}/pc_yyb_auth/pcyyb_refresh_token_auth",
            data=body_str.encode("utf-8"),
            headers=headers,
            timeout=20,
            proxies=proxies,
            verify=False,
        )
        if r.status_code < 200 or r.status_code >= 300:
            return {"ok": False, "error": f"应用宝续期请求失败：HTTP {r.status_code}"}
        d = r.json()
    except Exception as e:
        return {"ok": False, "error": f"续期请求失败：{e}"}

    # 应用宝接口才会刷新可继续换 login_buffer 的运行态 token；微信官方 sns 刷新会导致网站显示续期成功但取码仍失败。
    code = d.get("code")
    if code not in (0, "0", None):
        safe_msg = _safe_yyb_error(d.get("msg"))
        db.execute("UPDATE accounts SET status='error', status_error=? WHERE openid=?",
                   (f"应用宝续期失败：code={code} msg={safe_msg}", openid))
        return {"ok": False, "error": f"应用宝续期失败 (code={code})"}

    info = {}
    for box in (d, d.get("data"), d.get("result")):
        if not isinstance(box, dict):
            continue
        for key in ("user_info", "userInfo"):
            if isinstance(box.get(key), dict):
                info.update(box[key])

    at = info.get("access_token") or info.get("accessToken") or info.get("accesstoken") or ""
    rt = info.get("refresh_token") or info.get("refreshToken") or info.get("refreshtoken") or a["refreshtoken"]
    try:
        ein = int(info.get("expires_in") or info.get("expiresIn") or 7200)
    except (TypeError, ValueError):
        ein = 7200
    if not at:
        return {"ok": False, "error": "应用宝续期响应缺少 access_token"}
    if ein <= 0:
        ein = 7200

    try:
        lb_body = {
            "extInfo": {
                "listS": {
                    "unionid": {"value": [openid]},
                    "user_id": {"value": [openid]},
                    "access_token": {"value": [at]},
                },
                "listI": {"user_type": {"value": [0]}},
            }
        }
        lb_headers, lb_body_str = pcyyb.ual_headers(lb_body, "pc_yyb_auth", pcyyb.PC_YYB_AUTH_KEY)
        lb_headers["Cookie"] = f"openid={openid}; accesstoken={at}; refreshtoken={rt}"
        lb_r = requests.post(
            f"{pcyyb.HOST}/pc_yyb_auth/pcyyb_get_wx_login_buffer_auth",
            data=lb_body_str.encode("utf-8"),
            headers=lb_headers,
            timeout=20,
            proxies=proxies,
            verify=False,
        )
        if lb_r.status_code < 200 or lb_r.status_code >= 300:
            return {"ok": False, "error": f"续期后校验 login_buffer 失败：HTTP {lb_r.status_code}"}
        lb_d = lb_r.json()
        value = (((lb_d.get("ext_info") or {}).get("list_s") or {}).get("login_buffer") or {}).get("value") or []
        login_buffer = value[0] if value else ""
        if lb_d.get("code") not in (0, "0", None) or not login_buffer:
            safe_msg = _safe_yyb_error(lb_d.get("msg"))
            db.execute("UPDATE accounts SET status='error', status_error=? WHERE openid=?",
                       (f"续期后仍无法获取 login_buffer：code={lb_d.get('code')} msg={safe_msg}", openid))
            return {"ok": False, "error": "续期后仍无法获取 login_buffer，需重新扫码登录"}
    except Exception as e:
        return {"ok": False, "error": f"续期后校验 login_buffer 失败：{e}"}

    info = fetch_userinfo(at, openid, px) if at else {}
    scope = (a["scope"] or O.WX_QRCONNECT_SCOPE).strip().strip('"')
    fields = {"openid": openid, "accesstoken": at, "refreshtoken": rt, "guid": a["guid"],
              "appid": a["appid"] or WX_APPID, "scope": scope, "expires_in": ein,
              "expire_at": (db.now() + ein * 1000) if ein else 0, "status": "active", "status_error": ""}
    fields.update(info)
    _upsert_account(a["user_id"], fields)
    return {"ok": True, "expires_in": ein}


# ---------------- 扫码登录会话 ----------------
_STATE_MAP = {"waiting": "waiting", "scanned": "scanned", "confirmed": "confirmed",
              "cancelled": "rejected", "expired": "expired"}


def start_login(user_id: int, proxy_url: str = "") -> dict:
    qr = O.fetch_qr(session=_make_session(proxy_url))
    sid = secrets.token_hex(8)
    ctype = qr.qr_content_type or "image/png"
    data_url = f"data:{ctype};base64," + base64.b64encode(qr.qr_image).decode("ascii")
    sess = {"running": True, "status": "waiting", "error": None, "uuid": qr.uuid,
            "qrcodeDataUrl": data_url, "account": None, "user_id": user_id,
            "cancel": threading.Event()}
    with _slock:
        _sessions[sid] = sess
    threading.Thread(target=_run_login, args=(sid, qr, proxy_url), daemon=True).start()
    return {"sessionId": sid, "uuid": qr.uuid, "qrcodeDataUrl": data_url,
            "qrcodeUrl": f"https://open.weixin.qq.com/connect/qrcode/{qr.uuid}", "state": qr.state}


def _run_login(sid: str, qr, proxy_url: str) -> None:
    sess = _sessions[sid]

    def on_state(state, errcode):
        if not sess["cancel"].is_set():
            sess["status"] = _STATE_MAP.get(state, sess["status"])

    try:
        wx_code = O.poll_for_code(qr, on_state=on_state)
        if sess["cancel"].is_set():
            sess.update(running=False, status="cancelled")
            return
        if not wx_code:
            st = sess["status"] if sess["status"] in ("expired", "rejected") else "expired"
            sess.update(running=False, status=st, error="二维码已过期或被取消")
            return
        ex = O.oauth_exchange(wx_code, qr.state, session=qr.session)
        if not ex.get("ok"):
            sess.update(running=False, status="error", error="换取 token 失败（应用宝授权）")
            return
        t = ex["tokens"]
        openid = t.get("openid", "")
        at = t.get("access_token", "")
        ein = ex.get("expires_in", 0) or 0
        info = fetch_userinfo(at, openid, proxy_url) if at else {}
        fields = {
            "openid": openid, "accesstoken": at, "refreshtoken": t.get("refresh_token", ""),
            "guid": qr.state, "appid": WX_APPID,
            "scope": (t.get("scope") or O.WX_QRCONNECT_SCOPE), "expires_in": ein,
            "expire_at": (db.now() + ein * 1000) if ein else 0,
            "nickname": t.get("nick_name", ""), "proxy_url": proxy_url,  # 持久化，异地账号取码复用
            "status": "active", "status_error": "", "logged_at": db.now(),
        }
        fields.update(info)
        _upsert_account(sess["user_id"], fields)
        a = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
        sess.update(running=False, status="success", account={
            "openid": openid, "nickname": a["nickname"], "unionid": a["unionid"],
            "headImgUrl": a["head_img_url"], "sex": a["sex"], "country": a["country"],
            "province": a["province"], "city": a["city"], "loggedAt": a["logged_at"],
            "expireAt": a["expire_at"],
        })
    except Exception as e:
        sess.update(running=False, status="error", error=str(e))


def get_status(sid: str) -> dict | None:
    s = _sessions.get(sid)
    if not s:
        return None
    return {"running": s["running"], "status": s["status"], "error": s["error"],
            "uuid": s["uuid"], "qrcodeDataUrl": s["qrcodeDataUrl"], "account": s["account"],
            "user_id": s["user_id"]}


def list_sessions(user_id: int) -> list[dict]:
    out = []
    for sid, s in _sessions.items():
        if s["user_id"] == user_id:
            out.append({"sessionId": sid, "uuid": s["uuid"], "status": s["status"],
                        "running": s["running"], "error": s["error"]})
    return out


def stop_login(sid: str | None) -> None:
    if sid:
        s = _sessions.get(sid)
        if s:
            s["cancel"].set()
            s.update(running=False, status="cancelled")
    else:
        with _slock:
            for s in _sessions.values():
                s["cancel"].set()
            _sessions.clear()


def auto_renew_tick() -> None:
    """定时：校验/续期账号 token（每几分钟调一次）。"""
    rows = db.query("SELECT openid, expire_at, status FROM accounts")
    now = db.now()
    for r in rows:
        if r["status"] == "error":
            continue
        if r["expire_at"] and (r["expire_at"] - now) < 10 * 60 * 1000:
            try:
                refresh_account(r["openid"])
            except Exception:
                pass
