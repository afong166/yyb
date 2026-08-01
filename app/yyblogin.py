"""扫码登录 OAuth + 微信账号存储/续期/资料。
QR 流程复用已验证的 pure_wxcode.oauth（fetch_qr → poll_for_code → oauth_exchange），
在后台线程里跑；账号(含 token)按 user_id 存进 DB accounts 表。"""
from __future__ import annotations

import base64
import re
import secrets
import threading

import requests

from . import db, shortproxy
from .logs import event
from .pure_wxcode import oauth as O, pcyyb
from .util import normalize_proxy_url

WX_APPID = "wxd44977328b36e647"
SYZS_APPID = O.SYZS_WX_QRCONNECT_APPID  # 手游助手专用 appid (wxef99873dbfab493c)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0 Safari/537.36"
)

_sessions: dict[str, dict] = {}
_slock = threading.Lock()

# 51 转发池禁止 lp.open.weixin.qq.com，但微信官方 long 端点接受同一二维码 UUID。
# 短效代理固定使用 long 端点；实际连接失败才换 IP，不做 DNS/协议/域名回退。
SHORT_PROXY_LONGPOLL_HOST = "https://long.open.weixin.qq.com"
# 创建二维码、轮询、OAuth 交换共用同一预算：整个登录流程最多尝试 10 个代理。
MAX_QR_PROXY_ATTEMPTS = 10
_QR_NETWORK_ERROR_HINTS = (
    "socks", "proxy", "connection", "connect", "ruleset", "0x02",
    "timeout", "timed out", "ssl", "eof", "name resolution", "network",
    "代理", "连接", "超时", "网络",
)


def _safe_yyb_error(msg: object) -> str:
    """应用宝错误 msg 偶尔会带 token；入库/返回前统一脱敏，避免前端和日志泄露敏感凭据。"""
    text = str(msg or "")
    text = re.sub(r"(?i)(access_token|refresh_token|token)\s*[:= ]\s*[^,\s]+", r"\1 [已隐藏]", text)
    return text[:300]


def _make_session(proxy_url: str = "") -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    proxy_url = normalize_proxy_url(proxy_url)  # socks5 → socks5h：远端 DNS，修复微信握手 SSLEOFError
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
        proxy_url = normalize_proxy_url(proxy_url)
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
    fields = dict(fields)
    openid = fields["openid"]
    cols = ["user_id", "accesstoken", "refreshtoken", "guid", "appid", "scope", "expires_in",
            "expire_at", "nickname", "head_img_url", "unionid", "sex", "country", "province",
            "city", "proxy_url", "status", "status_error", "logged_at", "updated_at",
            "login_source", "proxy_mode", "proxy_region_code", "proxy_region_name"]
    exist = db.query_one("SELECT openid, proxy_mode FROM accounts WHERE openid=?", (openid,))
    effective_proxy_mode = fields.get("proxy_mode") or (exist["proxy_mode"] if exist else "direct")
    if effective_proxy_mode == "short":
        # 短效 SOCKS 地址回收后会变，只保存模式和地区，不保存本次提取的地址。
        fields["proxy_url"] = ""
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


def _refresh_syzs_account(a) -> dict:
    """手游助手账号续期：走微信官方 sns/oauth2/refresh_token（SYZS appid 不走应用宝 pcyyb 链路）。"""
    openid = a["openid"]
    refresh_appid = a["appid"] or SYZS_APPID
    try:
        proxy_budget = shortproxy.RenewalProxyBudget(openid)

        def _call(current_proxy):
            proxies = {"http": current_proxy, "https": current_proxy} if current_proxy else None
            response = requests.get(
                "https://api.weixin.qq.com/sns/oauth2/refresh_token",
                params={"appid": refresh_appid, "grant_type": "refresh_token",
                        "refresh_token": a["refreshtoken"]},
                timeout=15, proxies=proxies,
            )
            response.raise_for_status()
            return response
        r, px = proxy_budget.call(_call)
        d = r.json()
    except Exception as e:
        return {"ok": False, "error": f"续期请求失败：{e}"}
    ec = d.get("errcode")
    if ec == 40030:
        db.execute("UPDATE accounts SET status='error', status_error=? WHERE openid=?",
                   ("refresh_token 已失效，需重新扫码登录", openid))
        return {"ok": False, "error": "refresh_token 已失效，需重新扫码登录"}
    if ec:
        return {"ok": False, "error": f"手游助手续期失败 (errcode={ec})"}
    at = d.get("access_token", "")
    rt = d.get("refresh_token", a["refreshtoken"])
    try:
        ein = int(d.get("expires_in", 0) or 0)
    except (TypeError, ValueError):
        ein = 0
    if not at:
        return {"ok": False, "error": "手游助手续期响应缺少 access_token"}
    if ein <= 0:
        ein = 7200
    # 关键 scope = snsapi_login；仅当微信「明确回了 scope」且丢失时才判失效，避免误伤（不回 scope 不动）。
    ret_scope = (d.get("scope") or "").strip()
    old_scope = a["scope"] or ""
    new_scope = ret_scope or old_scope
    if ret_scope and "snsapi_login" in old_scope and "snsapi_login" not in ret_scope:
        db.execute("UPDATE accounts SET scope=?, status='error', status_error=? WHERE openid=?",
                   (new_scope, "手游助手登录授权（snsapi_login）已失效，请重新扫码登录", openid))
        return {"ok": False, "error": "手游助手登录授权已失效，需重新扫码登录"}
    info = fetch_userinfo(at, openid, px) if at else {}
    fields = {"openid": openid, "accesstoken": at, "refreshtoken": rt, "guid": a["guid"],
              "appid": a["appid"] or SYZS_APPID, "scope": new_scope, "expires_in": ein,
              "expire_at": (db.now() + ein * 1000) if ein else 0, "status": "active",
              "status_error": "", "login_source": 2}
    fields.update(info)
    _upsert_account(a["user_id"], fields)
    return {"ok": True, "expires_in": ein}


def refresh_account(openid: str) -> dict:
    """刷新运行态 token。应用宝(login_source=1)走 pcyyb 续期并校验 login_buffer；
    手游助手(login_source=2)走微信官方 sns 续期。"""
    a = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
    if not a:
        return {"ok": False, "error": "account not found"}
    if not a["refreshtoken"]:
        return {"ok": False, "error": "无 refresh_token，需重新扫码"}
    login_source = int((a["login_source"] if "login_source" in a.keys() else 1) or 1)
    if login_source == 2:
        return _refresh_syzs_account(a)
    try:
        # 手动按钮、定时自动续期和运行态触发的续期都走这里；两个 HTTP 阶段共享 5 次代理预算。
        proxy_budget = shortproxy.RenewalProxyBudget(openid)
        body = {
            "userInfo": {
                "openId": openid,
                "refreshToken": a["refreshtoken"],
                "accessToken": a["accesstoken"],
                "loginType": "WX",
            }
        }
        headers, body_str = pcyyb.ual_headers(body, "pc_yyb_auth", pcyyb.PC_YYB_AUTH_KEY)
        def _refresh_call(current_proxy):
            proxies = {"http": current_proxy, "https": current_proxy} if current_proxy else None
            response = requests.post(
                f"{pcyyb.HOST}/pc_yyb_auth/pcyyb_refresh_token_auth",
                data=body_str.encode("utf-8"), headers=headers, timeout=20,
                proxies=proxies, verify=False,
            )
            response.raise_for_status()
            return response
        r, px = proxy_budget.call(_refresh_call)
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
        def _buffer_call(current_proxy):
            proxies = {"http": current_proxy, "https": current_proxy} if current_proxy else None
            response = requests.post(
                f"{pcyyb.HOST}/pc_yyb_auth/pcyyb_get_wx_login_buffer_auth",
                data=lb_body_str.encode("utf-8"), headers=lb_headers, timeout=20,
                proxies=proxies, verify=False,
            )
            response.raise_for_status()
            return response
        lb_r, px = proxy_budget.call(_buffer_call)
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


def start_login(user_id: int, proxy_url: str = "", login_source: int = 1,
                proxy_meta: dict | None = None) -> dict:
    """发起扫码登录。login_source: 1=YYB(应用宝), 2=SYZS(手游助手)。"""
    proxy_meta = dict(proxy_meta or {})
    proxy_mode = proxy_meta.get("proxyMode") or ("long" if proxy_url else "direct")
    sid = secrets.token_hex(8)
    sess = {
        "running": True, "status": "waiting", "error": None, "account": None,
        "user_id": user_id, "cancel": threading.Event(), "created_at": db.now(),
        "login_source": login_source, "proxy_mode": proxy_mode,
        "proxy_region_code": proxy_meta.get("proxyRegionCode") or "",
        "proxy_region_name": proxy_meta.get("proxyRegionName") or "",
        "proxy_attempts": 1,
    }
    while True:
        try:
            if login_source == 2:
                qr = O.fetch_qr(session=_make_session(proxy_url),
                                redirect_uri=O.SYZS_QRCONNECT_REDIRECT,
                                appid=O.SYZS_WX_QRCONNECT_APPID)
            else:
                qr = O.fetch_qr(session=_make_session(proxy_url))
            break
        except Exception as error:
            if not _uses_51_short_proxy(sess):
                raise
            proxy_url = _rotate_short_login_proxy(
                sid, sess, None, error, "二维码创建"
            )
    if proxy_mode == "short":
        qr.longpoll_host = SHORT_PROXY_LONGPOLL_HOST
    ctype = qr.qr_content_type or "image/png"
    data_url = f"data:{ctype};base64," + base64.b64encode(qr.qr_image).decode("ascii")
    sess.update(uuid=qr.uuid, qrcodeDataUrl=data_url)
    with _slock:
        _sessions[sid] = sess
    threading.Thread(target=_run_login, args=(sid, sess, qr, proxy_url), daemon=True).start()
    return {"sessionId": sid, "uuid": qr.uuid, "qrcodeDataUrl": data_url,
            "qrcodeUrl": f"https://open.weixin.qq.com/connect/qrcode/{qr.uuid}",
            "state": qr.state, "loginSource": login_source}


def _is_qr_network_error(error: BaseException) -> bool:
    """仅把实际网络/代理错误视为可轮换；二维码拒绝、过期等协议状态不会进入这里。"""
    if isinstance(error, requests.RequestException):
        return True
    text = f"{type(error).__name__}: {error}".lower()
    return any(hint in text for hint in _QR_NETWORK_ERROR_HINTS)


def _uses_51_short_proxy(sess: dict) -> bool:
    return (sess.get("proxy_mode") == "short"
            and not shortproxy.is_local_region(sess.get("proxy_region_code") or "",
                                               sess.get("proxy_region_name") or ""))


def _rotate_short_login_proxy(sid: str, sess: dict, qr, error: object,
                              stage: str) -> str:
    """登录全流程共用 10 个代理的硬上限；新提取代理固定 3 分钟。"""
    attempts = int(sess.get("proxy_attempts") or 1)
    if attempts >= MAX_QR_PROXY_ATTEMPTS:
        raise RuntimeError(
            f"短效代理连续连接失败，登录全流程已尝试 {MAX_QR_PROXY_ATTEMPTS} 个代理"
        ) from None
    user_id = int(sess["user_id"])
    region_code = str(sess.get("proxy_region_code") or "")
    region_name = str(sess.get("proxy_region_name") or "")
    try:
        got = shortproxy.acquire(
            user_id, region_code, region_name,
            time_code=shortproxy.TIME_CODE_QR_LOGIN,
        )
    except Exception:
        raise RuntimeError("短效代理自动更换失败，请稍后重新获取二维码") from None
    proxy_url = got["proxyUrl"]
    if qr is not None:
        qr.session.proxies = ({"http": proxy_url, "https": proxy_url}
                              if proxy_url else {"http": None, "https": None})
    attempts += 1
    sess["proxy_attempts"] = attempts
    sess["proxy_rotations"] = attempts - 1
    event("qrProxy", f"{stage}连接失败，已自动更换短效代理",
          session=sid, user_id=user_id, 地区=region_name or region_code,
          尝试=f"{attempts}/{MAX_QR_PROXY_ATTEMPTS}", 错误类型=type(error).__name__)
    return proxy_url


def _poll_with_short_proxy_rotation(sid: str, sess: dict, qr, proxy_url: str,
                                    on_state) -> tuple[str, str]:
    """轮询同一个二维码；短效代理网络失败时提取新 IP 后继续。"""
    current_proxy = proxy_url
    while True:
        try:
            return O.poll_for_code(qr, on_state=on_state), current_proxy
        except Exception as error:
            if sess["cancel"].is_set():
                return "", current_proxy
            if not _uses_51_short_proxy(sess) or not _is_qr_network_error(error):
                raise
            current_proxy = _rotate_short_login_proxy(
                sid, sess, qr, error, "二维码轮询"
            )


def _oauth_exchange_with_short_proxy_rotation(sid: str, sess: dict, qr,
                                               wx_code: str,
                                               proxy_url: str) -> tuple[dict, str]:
    """扫码确认后的应用宝 OAuth；仅连接异常/网关错误时换代理重试。"""
    current_proxy = proxy_url
    while True:
        try:
            result = O.oauth_exchange(wx_code, qr.state, session=qr.session)
        except Exception as error:
            if sess["cancel"].is_set():
                return {}, current_proxy
            if not _uses_51_short_proxy(sess) or not _is_qr_network_error(error):
                raise
            current_proxy = _rotate_short_login_proxy(
                sid, sess, qr, error, "OAuth 交换"
            )
            continue
        try:
            status = int(result.get("http_status") or 0)
        except (TypeError, ValueError):
            status = 0
        if not result.get("ok") and (status == 407 or status >= 500):
            error = requests.exceptions.ProxyError(f"OAuth 网关 HTTP {status}")
            if not _uses_51_short_proxy(sess):
                return result, current_proxy
            current_proxy = _rotate_short_login_proxy(
                sid, sess, qr, error, "OAuth 交换"
            )
            continue
        return result, current_proxy


def _finish_syzs_login(sess: dict, qr, wx_code: str, proxy_url: str) -> None:
    """SYZS(手游助手) 登录完成：wx_code → login_buffer + openid → 落库。
    方式A：syzs_oauth_cookie → syzs_get_login_buffer（签名端点）；
    方式B(回退)：syzs_oauth_exchange → pcyyb.get_fresh_login_buffer。"""
    login_buffer = openid = at = rt = scope = ""
    ein = 7200
    try:
        oc = O.syzs_oauth_cookie(wx_code, session=qr.session)
        if oc.get("ok") and oc["cookies"].get("pc_yyb_auth"):
            lb_res = O.syzs_get_login_buffer(wx_code, oc["cookies"], session=qr.session)
            if lb_res.get("ok"):
                login_buffer = lb_res["login_buffer"]
                openid = lb_res.get("openid", "")
                at = oc["cookies"].get("accesstoken", "")
                rt = oc["cookies"].get("refreshtoken", "")
                scope = oc["cookies"].get("scope", "")
                ein = int(oc["cookies"].get("expires_in", 7200) or 7200)
    except Exception as e:
        print(f"[syzs] 方式A失败: {e}")
    if not login_buffer:
        try:
            ex = O.syzs_oauth_exchange(wx_code, qr.state, session=qr.session)
            if ex.get("ok"):
                cookies = ex["cookies"]
                openid = openid or cookies.get("openid", "")
                at = at or cookies.get("accesstoken", "")
                rt = rt or cookies.get("refreshtoken", "")
                scope = scope or cookies.get("scope", "")
                ein = int(cookies.get("expires_in", 7200) or 7200)
                if openid and at:
                    cred = O.Credentials(openid=openid, access_token=at, refresh_token=rt,
                                         scope=scope, login_type="WX", guid=qr.state)
                    res = pcyyb.get_fresh_login_buffer(cred, False)
                    if res.get("code") == 0 and res.get("login_buffer"):
                        login_buffer = res["login_buffer"]
        except Exception as e:
            print(f"[syzs] 方式B失败: {e}")
    if not login_buffer or not openid:
        sess.update(running=False, status="error",
                    error="手游助手登录失败：无法获取 login_buffer 或 openid")
        return
    if sess["cancel"].is_set():
        sess.update(running=False, status="cancelled")
        return
    info = fetch_userinfo(at, openid, proxy_url) if at else {}
    fields = {
        "openid": openid, "accesstoken": at, "refreshtoken": rt, "guid": qr.state,
        "appid": SYZS_APPID, "scope": scope or O.WX_QRCONNECT_SCOPE, "expires_in": ein,
        "expire_at": (db.now() + ein * 1000) if ein else 0, "nickname": "",
        "proxy_url": proxy_url if sess.get("proxy_mode") == "long" else "", "login_source": 2,
        "proxy_mode": sess.get("proxy_mode") or ("long" if proxy_url else "direct"),
        "proxy_region_code": sess.get("proxy_region_code") or "",
        "proxy_region_name": sess.get("proxy_region_name") or "",
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


def _run_login(sid: str, sess: dict, qr, proxy_url: str) -> None:
    # 直接持有会话对象引用（不再按 sid 回查 _sessions），避免会话被 stop/reap 弹出后 KeyError。

    def on_state(state, errcode):
        if not sess["cancel"].is_set():
            sess["status"] = _STATE_MAP.get(state, sess["status"])

    try:
        wx_code, proxy_url = _poll_with_short_proxy_rotation(
            sid, sess, qr, proxy_url, on_state
        )
        if sess["cancel"].is_set():
            sess.update(running=False, status="cancelled")
            return
        if not wx_code:
            st = sess["status"] if sess["status"] in ("expired", "rejected") else "expired"
            sess.update(running=False, status=st, error="二维码已过期或被取消")
            return
        if sess.get("login_source") == 2:
            _finish_syzs_login(sess, qr, wx_code, proxy_url)
            return
        ex, proxy_url = _oauth_exchange_with_short_proxy_rotation(
            sid, sess, qr, wx_code, proxy_url
        )
        if sess["cancel"].is_set():  # 用户在换取 token 期间取消：不落库，避免写入被取消会话的账号
            sess.update(running=False, status="cancelled")
            return
        if not ex.get("ok"):
            sess.update(running=False, status="error", error="换取 token 失败（应用宝授权）")
            return
        t = ex["tokens"]
        openid = t.get("openid", "")
        at = t.get("access_token", "")
        # 应用宝偶尔不返回 expires_in；缺省 7200s 与 refresh_account 一致，
        # 否则 expire_at=0 会被 auto_renew_tick 跳过 → 新号永不自动续期。
        ein = int(ex.get("expires_in") or 0) or 7200
        info = fetch_userinfo(at, openid, proxy_url) if at else {}
        fields = {
            "openid": openid, "accesstoken": at, "refreshtoken": t.get("refresh_token", ""),
            "guid": qr.state, "appid": WX_APPID,
            "scope": (t.get("scope") or O.WX_QRCONNECT_SCOPE), "expires_in": ein,
            "expire_at": (db.now() + ein * 1000) if ein else 0,
            "nickname": t.get("nick_name", ""),
            "proxy_url": proxy_url if sess.get("proxy_mode") == "long" else "",
            "login_source": 1,
            "proxy_mode": sess.get("proxy_mode") or ("long" if proxy_url else "direct"),
            "proxy_region_code": sess.get("proxy_region_code") or "",
            "proxy_region_name": sess.get("proxy_region_name") or "",
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
    with _slock:  # 加锁快照，避免与 stop/reap 的字典修改并发迭代抛 RuntimeError
        items = list(_sessions.items())
    for sid, s in items:
        if s["user_id"] == user_id:
            out.append({"sessionId": sid, "uuid": s["uuid"], "status": s["status"],
                        "running": s["running"], "error": s["error"]})
    return out


def stop_login(sid: str | None, user_id: int | None = None) -> None:
    """停止扫码会话；传 user_id 时严格限定归属，避免越权取消/清空他人会话。

    - sid 指定：仅当该会话属于 user_id（或 user_id 为 None 的内部调用）才取消；
    - sid 为空：仅停止并清理 user_id 名下的会话；user_id 为 None（内部）才全清。
    """
    with _slock:
        if sid:
            s = _sessions.get(sid)
            if s and (user_id is None or s.get("user_id") == user_id):
                s["cancel"].set()
                s.update(running=False, status="cancelled")
                _sessions.pop(sid, None)
            return
        for k in [k for k, s in _sessions.items() if user_id is None or s.get("user_id") == user_id]:
            _sessions[k]["cancel"].set()
            _sessions.pop(k, None)


def reap_sessions(ttl_ms: int = 10 * 60 * 1000) -> int:
    """回收已到终态且超过 ttl 的扫码会话，防止 _sessions 无限增长。返回清理条数。"""
    now = db.now()
    removed = 0
    with _slock:
        for k in [k for k, s in _sessions.items()
                  if not s.get("running") and (now - s.get("created_at", now)) > ttl_ms]:
            _sessions.pop(k, None)
            removed += 1
    return removed


def auto_renew_tick() -> None:
    """定时：校验/续期账号 token（每几分钟调一次），并顺带回收过期的扫码会话。"""
    reap_sessions()
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
