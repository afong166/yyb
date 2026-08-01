"""应用宝微信扫码 OAuth 凭据获取/刷新/持久化。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, parse_qs, unquote

try:
    import requests
except ImportError:
    print("[fatal] 需要 requests: pip install requests", file=sys.stderr)
    raise

WX_QRCONNECT_APPID = "wxd44977328b36e647"
WX_QRCONNECT_SCOPE = "snsapi_login,snsapi_runtime_pcsdk"
WX_QRCONNECT_REDIRECT = "https://yybadaccess.3g.qq.com/pc_yyb/pcyyb_oauth?login_type=WX"
WX_QRCONNECT_URL = "https://open.weixin.qq.com/connect/qrconnect"
YYB_BASE = "https://yybadaccess.3g.qq.com"

# SYZS (手游助手) 专用配置 —— 独立的 WeChat appid 和 redirect_uri（逆向自 pc.syzs.qq.com 前端 JS）
#   th = {WX: "wxef99873dbfab493c", QC: "101549767"}
SYZS_WX_QRCONNECT_APPID = "wxef99873dbfab493c"
SYZS_QRCONNECT_REDIRECT = f"{YYB_BASE}/syzslogin/syzs_oauth?login_type=wx&domain=1&syzs_prefix="
SYZS_OAUTH_URL = f"{YYB_BASE}/v3/syzs_get_wx_login_buffer_auth"
SYZS_LOGIN_BUFFER_URL = f"{YYB_BASE}/syzslogin/syzs_login_buffer"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent
DEFAULT_EVIDENCE_DIR = _PROJECT_ROOT / "evidence"
DEFAULT_CREDENTIALS = _PROJECT_ROOT / "credentials.json"

@dataclass
class Credentials:
    openid: str = ""
    access_token: str = ""
    refresh_token: str = ""
    scope: str = ""
    login_type: str = "WX"
    expire_time: int = 0
    nick_name: str = ""
    guid: str = ""
    source: str = ""
    updated_at: int = 0

    def is_valid(self) -> bool:
        return bool(self.openid and self.access_token and self.refresh_token)

    def access_token_expired(self, skew: int = 120) -> bool:
        if self.expire_time <= 0:
            return False
        return time.time() > (self.expire_time - skew)

    def redacted(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("access_token", "refresh_token"):
            v = d.get(k) or ""
            d[k] = f"<len={len(v)} tail={v[-6:]}>" if v else ""
        return d

def _now() -> int:
    return int(time.time())

def _evidence_dir(path: Optional[str]) -> Path:
    p = Path(path) if path else DEFAULT_EVIDENCE_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p

def _dump(evidence_dir: Path, name: str, obj: Any) -> Path:
    fp = evidence_dir / name
    try:
        fp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except TypeError:
        fp.write_text(str(obj), encoding="utf-8")
    return fp

def _new_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    return s

def build_qrconnect_url(state: str, fast_login: bool = True,
                        redirect_uri: str = "",
                        appid: str = "") -> str:
    query = urlencode(
        {
            "appid": appid or WX_QRCONNECT_APPID,
            "redirect_uri": redirect_uri or WX_QRCONNECT_REDIRECT,
            "response_type": "code",
            "scope": WX_QRCONNECT_SCOPE,
            "fast_login": 1 if fast_login else 0,
            "state": state,
            "self_redirect": "true",
        }
    )
    return f"{WX_QRCONNECT_URL}?{query}#wechat_redirect"

_UUID_PATTERNS = [

    re.compile(r"<uuid>\s*<!\[CDATA\[([0-9A-Za-z_\-]+)\]\]>\s*</uuid>"),
    re.compile(r"<uuid>\s*([0-9A-Za-z_\-]+)\s*</uuid>"),
    re.compile(r"uuid\s*[:=]\s*['\"]([0-9A-Za-z_\-]+)['\"]"),
    re.compile(r"/connect/qrcode/([0-9A-Za-z_\-]+)"),
    re.compile(r"[?&]uuid=([0-9A-Za-z_\-]+)"),
]

def _xml_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""

def _parse_uuid(text: str) -> str:
    for pat in _UUID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return ""

def _parse_usenewdomain(text: str) -> bool:
    val = _xml_tag(text, "usenewdomain")
    if val:
        return val in ("1", "true", "True")

    m = re.search(r"usenewdomain\s*[:=]\s*['\"]?(\d)", text)
    return bool(m and m.group(1) == "1")

@dataclass
class QrSession:
    state: str
    page_url: str
    uuid: str
    qr_image: bytes
    qr_content_type: str
    longpoll_host: str
    session: requests.Session = field(repr=False, default=None)

def fetch_qr(session: Optional[requests.Session] = None, state: str = "",
             redirect_uri: str = "",
             appid: str = "") -> QrSession:
    """生成微信扫码 QR 会话。
    redirect_uri 为空时走 YYB（应用宝）回调；传 SYZS_QRCONNECT_REDIRECT 时走手游助手回调。
    appid 为空时用 YYB 的 appid；传 SYZS_WX_QRCONNECT_APPID 时用手游助手的 appid。"""
    s = session or _new_session()
    state = state or f"{int(time.time() * 1000)}{_now() % 10000:04d}"
    page_url = build_qrconnect_url(state, redirect_uri=redirect_uri, appid=appid)

    s.get(page_url, headers={"Referer": WX_QRCONNECT_URL}, timeout=20)

    base = page_url.split("#", 1)[0]
    xml_url = f"{base}&f=xml&_={int(time.time() * 1000)}"
    xr = s.get(
        xml_url,
        headers={"Accept": "application/xml,text/xml,*/*;q=0.1", "Referer": page_url},
        timeout=20,
    )
    xml_text = xr.content.decode("utf-8", "ignore")
    uuid = _parse_uuid(xml_text)
    if not uuid:
        raise RuntimeError(f"qr uuid is empty; xml head={xml_text[:200]!r}")
    use_new = _parse_usenewdomain(xml_text)

    qr_url = f"https://open.weixin.qq.com/connect/qrcode/{uuid}"
    qr = s.get(
        qr_url,
        headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8", "Referer": page_url},
        timeout=20,
    )
    longpoll_host = (
        "https://lp.open.weixin.qq.com" if use_new else "https://long.open.weixin.qq.com"
    )
    return QrSession(
        state=state,
        page_url=page_url,
        uuid=uuid,
        qr_image=qr.content,
        qr_content_type=qr.headers.get("Content-Type", ""),
        longpoll_host=longpoll_host,
        session=s,
    )

# ── SYZS (手游助手) OAuth 交换 & login_buffer ──
def syzs_oauth_exchange(wx_code: str,
                        state: str = "",
                        session: Optional[requests.Session] = None) -> dict[str, Any]:
    """用 wx_code 向 SYZS redirect_uri 端点交换 tokens/cookies（三重提取 cookies+Location+JSON）。"""
    s = session or _new_session()
    url = f"{SYZS_QRCONNECT_REDIRECT}&code={wx_code}&state={state}"
    resp = s.get(url, headers={"Referer": SYZS_QRCONNECT_REDIRECT}, allow_redirects=False, timeout=25)
    cookies = {c.name: c.value for c in s.cookies}
    location = resp.headers.get("Location", "")
    body_text = resp.content.decode("utf-8", "ignore")
    loc_sources: dict[str, str] = {}
    if location:
        pu = urlparse(location)
        for part in (pu.query, pu.fragment):
            for k, v in parse_qs(part).items():
                loc_sources[k] = unquote(v[0]) if v else ""
    json_body: dict = {}
    try:
        json_body = json.loads(body_text)
        if not isinstance(json_body, dict):
            json_body = {"_root": json_body}
    except Exception:
        json_body = {}
    flat_json = dict(json_body)
    for nest_key in ("data", "extInfo", "result", "userInfo"):
        nv = json_body.get(nest_key)
        if isinstance(nv, dict):
            flat_json.update(nv)
    tokens = _extract_tokens_from_sources({"location": loc_sources, "cookies": cookies, "json": flat_json})
    for canon, keys in _TOKEN_KEYS.items():
        val = tokens.get(canon, "")
        if val:
            for k in keys:
                if k not in cookies:
                    cookies[k] = val
                    break
    return {
        "ok": bool(cookies) and bool(tokens.get("openid")) and bool(tokens.get("access_token")),
        "cookies": cookies, "tokens": tokens,
        "http_status": resp.status_code, "location": location,
    }


def syzs_oauth_cookie(wx_code: str, session: Optional[requests.Session] = None) -> dict[str, Any]:
    """POST {"code":"<wx_code>"} 到 SYZS OAuth 端点获取 cookies（含 pc_yyb_auth 签名 cookie）。"""
    s = session or _new_session()
    resp = s.post(
        SYZS_OAUTH_URL, json={"code": wx_code},
        headers={"Content-Type": "application/json", "Referer": "https://pc.syzs.qq.com/"},
        timeout=25,
    )
    cookies = {c.name: c.value for c in s.cookies}
    return {"ok": bool(cookies), "cookies": cookies, "http_status": resp.status_code}


def _syzs_signed_headers(cookies: dict[str, str]) -> dict[str, str]:
    """SYZS 签名头：Nonce(32hex) + Timestamp + Sign=MD5(nonce+timestamp+cookies['pc_yyb_auth'])。"""
    import hashlib
    import secrets as _secrets
    nonce = _secrets.token_hex(16)
    timestamp = str(int(time.time()))
    auth_val = cookies.get("pc_yyb_auth", "")
    sign = hashlib.md5((nonce + timestamp + auth_val).encode("utf-8")).hexdigest()
    return {"X-SYZS-Nonce": nonce, "X-SYZS-Timestamp": timestamp, "X-SYZS-Sign": sign}


def syzs_get_login_buffer(wx_code: str, cookies: dict[str, str],
                          session: Optional[requests.Session] = None) -> dict[str, Any]:
    """用 wx_code + 签名头向 SYZS 端点获取 login_buffer（兼容扁平 key 与嵌套 ext_info 结构）。"""
    s = session or _new_session()
    headers = _syzs_signed_headers(cookies)
    headers["Content-Type"] = "application/json"
    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    resp = s.post(SYZS_LOGIN_BUFFER_URL, json={"code": wx_code}, headers=headers, timeout=25)
    data: dict = {}
    try:
        data = resp.json()
    except Exception:
        pass
    login_buffer = data.get("loginBuffer") or data.get("login_buffer") or ""
    openid = data.get("openid") or data.get("openId") or ""
    if not login_buffer:
        ext = data.get("ext_info") or data.get("extInfo") or {}
        list_s = ext.get("list_s") or ext.get("listS") or {}
        lb = list_s.get("login_buffer") or {}
        if isinstance(lb, dict):
            vals = lb.get("value") or []
            if vals:
                login_buffer = vals[0]
        oid = list_s.get("openid") or {}
        if isinstance(oid, dict):
            vals = oid.get("value") or []
            if vals:
                openid = vals[0]
    return {"ok": bool(login_buffer), "login_buffer": login_buffer,
            "openid": openid, "http_status": resp.status_code, "raw": data}


def _open_file(path: Path) -> bool:
    import os
    import platform
    import subprocess
    try:
        if os.name == "nt":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False

def render_qr_terminal(qr_image: bytes) -> bool:
    try:
        import io
        from PIL import Image
    except Exception:
        return False
    try:
        with Image.open(io.BytesIO(qr_image)) as im:
            im = im.convert("L")

            w, h = im.size
            side = min(w, h)

            target = 41
            small = im.resize((target, target), Image.NEAREST)
            px = small.load()
            lines = []
            for y in range(target):
                row = []
                for x in range(target):
                    row.append("  " if px[x, y] > 128 else "██")
                lines.append("".join(row))
        print("\n".join(lines))
        return True
    except Exception:
        return False

def _build_longpoll_url(host: str, uuid: str, last: Optional[str]) -> str:
    params = {"uuid": uuid, "_": str(int(time.time() * 1000))}
    if last:
        params["last"] = last
    return f"{host}/connect/l/qrconnect?{urlencode(params)}"

_ERRCODE_RE = re.compile(r"window\.wx_errcode\s*=\s*(\d+)")
_CODE_RE = re.compile(r"window\.wx_code\s*=\s*'([^']*)'")

_STATE_BY_ERRCODE = {
    408: "waiting",
    404: "scanned",
    405: "confirmed",
    403: "cancelled",
    402: "expired",
}

def poll_for_code(
    qr: QrSession,
    max_polls: int = 120,
    poll_timeout: int = 35,
    on_state: Optional[Any] = None,
) -> str:
    s = qr.session or _new_session()
    last: Optional[str] = None
    last_state = ""
    for idx in range(max_polls):
        url = _build_longpoll_url(qr.longpoll_host, qr.uuid, last)
        # 微信实测约 17 秒返回一次 408 waiting；35 秒仍无响应不是正常轮询，
        # 应把超时交给上层判定代理失效，而不是静默吞掉后继续占用二维码会话。
        r = s.get(url, headers={"Referer": qr.page_url}, timeout=poll_timeout)
        r.raise_for_status()
        body = r.content.decode("utf-8", "ignore")
        m_err = _ERRCODE_RE.search(body)
        m_code = _CODE_RE.search(body)
        if not m_err and not m_code:
            # 代理网关偶尔返回 200 错误页；若当成 errcode=-1 连跑 120 次，
            # 会在几秒内把有效二维码误报为过期。
            raise requests.ConnectionError("微信二维码轮询响应格式异常")
        errcode = int(m_err.group(1)) if m_err else -1
        wx_code = m_code.group(1) if m_code else ""

        state = _STATE_BY_ERRCODE.get(errcode, f"errcode_{errcode}")
        if state != last_state:
            last_state = state
            if on_state:
                on_state(state, errcode)
            else:
                print(f"[qr] {state} (errcode={errcode})")

        if wx_code:
            return wx_code
        if errcode in (402, 403):
            return ""
        last = str(errcode)

    return ""

_TOKEN_KEYS = {
    "openid": ["openid", "openId", "open_id"],
    "access_token": ["access_token", "accessToken", "accesstoken"],
    "refresh_token": ["refresh_token", "refreshToken", "refreshtoken"],
    "scope": ["scope"],
    "expires_in": ["expires_in", "expiresIn", "expire_in"],
    "nick_name": ["nickname", "nick_name", "nickName"],
}

def _pick(d: dict, keys: list[str]) -> str:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            v = d[k]
            return v[0] if isinstance(v, list) and v else (v if not isinstance(v, list) else "")
    return ""

def _extract_tokens_from_sources(sources: dict[str, dict]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for _src_name, d in sources.items():
        if not isinstance(d, dict):
            continue
        flat = {str(k).lower(): v for k, v in d.items()}
        for canon, keys in _TOKEN_KEYS.items():
            if merged.get(canon):
                continue
            val = _pick({**d, **flat}, keys + [k.lower() for k in keys])
            if val:
                merged[canon] = val
    return merged

def oauth_exchange(
    wx_code: str,
    state: str,
    session: Optional[requests.Session] = None,
    evidence_dir: Optional[Path] = None,
) -> dict[str, Any]:
    s = session or _new_session()

    url = f"{YYB_BASE}/pc_yyb/pcyyb_oauth?login_type=WX&code={wx_code}&state={state}"
    r = s.get(url, headers={"Referer": WX_QRCONNECT_REDIRECT}, allow_redirects=False, timeout=25)

    location = r.headers.get("Location", "")
    body_text = r.content.decode("utf-8", "ignore")
    cookies = {c.name: c.value for c in s.cookies}

    loc_sources: dict[str, str] = {}
    if location:
        pu = urlparse(location)
        for part in (pu.query, pu.fragment):
            for k, v in parse_qs(part).items():
                loc_sources[k] = unquote(v[0]) if v else ""

    json_body: dict = {}
    try:
        json_body = json.loads(body_text)
        if not isinstance(json_body, dict):
            json_body = {"_root": json_body}
    except Exception:
        json_body = {}

    flat_json = dict(json_body)
    for nest_key in ("data", "extInfo", "result", "userInfo"):
        nv = json_body.get(nest_key)
        if isinstance(nv, dict):
            flat_json.update(nv)

    tokens = _extract_tokens_from_sources(
        {"location": loc_sources, "cookies": cookies, "json": flat_json}
    )

    expires_in = 0
    try:
        expires_in = int(str(tokens.get("expires_in") or "0"))
    except ValueError:
        expires_in = 0

    result = {
        "ok": bool(tokens.get("openid") and tokens.get("access_token")),
        "http_status": r.status_code,
        "location": location,
        "cookie_names": list(cookies.keys()),
        "token_keys_found": sorted([k for k, v in tokens.items() if v]),
        "tokens": tokens,
        "expires_in": expires_in,
    }

    # 仅在显式传 evidence_dir（CLI/调试）时落原始响应；部署时不把 token 写盘
    if evidence_dir is not None:
        _dump(
            evidence_dir,
            f"oauth_exchange_raw_{state}.json",
            {
                "request_url": url,
                "http_status": r.status_code,
                "resp_headers": dict(r.headers),
                "location": location,
                "set_cookies": cookies,
                "body_head": body_text[:4000],
                "parsed_tokens": tokens,
            },
        )
    return result

def scan_login(
    max_polls: int = 120,
    evidence_dir: Optional[str] = None,
    save_qr_to: Optional[str] = None,
    open_image: bool = True,
    show_ascii: bool = False,
) -> Credentials:
    ev = _evidence_dir(evidence_dir)
    qr = fetch_qr()

    ext = "jpg" if "jpeg" in (qr.qr_content_type or "").lower() else "png"
    ev_path = ev / f"login_qr_{qr.state}.{ext}"
    ev_path.write_bytes(qr.qr_image)
    if save_qr_to:
        primary = Path(save_qr_to)
    else:
        primary = _PROJECT_ROOT / f"qr.{ext}"
    primary.write_bytes(qr.qr_image)

    print(f"\n[qr] ====== 请用【微信】扫码并确认登录（snsapi_login） ======")
    print(f"[qr] ✅ 二维码图片(扫这张): {primary}")
    print(f"[qr]    evidence 副本: {ev_path}")
    print(f"[qr]    uuid={qr.uuid}  size={len(qr.qr_image)}B  type={qr.qr_content_type}")
    if open_image and _open_file(primary):
        print("[qr] 已自动打开图片查看器，请扫弹出的二维码。\n")
    else:
        print(f"[qr] 请手动打开上面的图片文件用微信扫。\n")
    if show_ascii:
        render_qr_terminal(qr.qr_image)

    wx_code = poll_for_code(qr, max_polls=max_polls)
    if not wx_code:
        raise RuntimeError("未取得 wx_code（二维码过期/取消/超时）")
    print(f"[qr] 已确认，wx_code 长度={len(wx_code)}")

    ex = oauth_exchange(wx_code, qr.state, session=qr.session, evidence_dir=ev)
    if not ex.get("ok"):
        raise RuntimeError(
            f"oauth_exchange 未提取到 token：status={ex.get('http_status')} "
            f"keys={ex.get('token_keys_found')} location={ex.get('location')[:120]!r}；"
            f"原始响应已落盘 {ev}/oauth_exchange_raw_{qr.state}.json，据此修正提取逻辑"
        )

    t = ex["tokens"]
    cred = Credentials(
        openid=t.get("openid", ""),
        access_token=t.get("access_token", ""),
        refresh_token=t.get("refresh_token", ""),
        scope=(t.get("scope", "") or WX_QRCONNECT_SCOPE).strip().strip('"'),
        login_type="WX",
        expire_time=(_now() + ex["expires_in"]) if ex.get("expires_in") else 0,
        nick_name=t.get("nick_name", ""),
        guid=qr.state,
        source=f"qr_scan:{qr.state}",
        updated_at=_now(),
    )
    return cred

def refresh_credentials(cred: Credentials, evidence_dir: Optional[str] = None) -> Credentials:
    ev = _evidence_dir(evidence_dir)
    s = _new_session()
    url = f"{YYB_BASE}/pc_yyb/pcyyb_check_login"
    body = {
        "openid": cred.openid,
        "access_token": cred.access_token,
        "refresh_token": cred.refresh_token,
        "login_type": cred.login_type,
    }
    r = s.post(url, json=body, headers={"Referer": WX_QRCONNECT_REDIRECT}, timeout=25)
    text = r.content.decode("utf-8", "ignore")
    _dump(ev, f"refresh_raw_{_now()}.json", {"url": url, "status": r.status_code, "body_head": text[:4000]})
    try:
        data = json.loads(text)
    except Exception:
        data = {}
    flat = dict(data)
    for nest in ("data", "extInfo", "result"):
        if isinstance(data.get(nest), dict):
            flat.update(data[nest])
    new_at = _pick(flat, _TOKEN_KEYS["access_token"])
    new_rt = _pick(flat, _TOKEN_KEYS["refresh_token"])
    if new_at:
        cred.access_token = new_at
    if new_rt:
        cred.refresh_token = new_rt
    cred.updated_at = _now()
    cred.source = "refresh"
    return cred

def save_credentials(cred: Credentials, path: Optional[str] = None) -> Path:
    fp = Path(path) if path else DEFAULT_CREDENTIALS
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(asdict(cred), ensure_ascii=False, indent=2), encoding="utf-8")
    return fp

def load_credentials(path: Optional[str] = None) -> Optional[Credentials]:
    fp = Path(path) if path else DEFAULT_CREDENTIALS
    if not fp.exists():
        return None
    data = json.loads(fp.read_text(encoding="utf-8"))
    known = {f for f in Credentials.__dataclass_fields__}
    return Credentials(**{k: v for k, v in data.items() if k in known})

def ensure_credentials(path: Optional[str] = None, max_polls: int = 120) -> Credentials:
    cred = load_credentials(path)
    if cred and cred.is_valid():
        if cred.access_token_expired():
            print("[oauth] access_token 可能过期，尝试 refresh_token 续期…")
            try:
                cred = refresh_credentials(cred)
                if cred.is_valid():
                    save_credentials(cred, path)
                    return cred
            except Exception as exc:
                print(f"[oauth] 续期失败：{exc}；改走扫码")
        else:
            return cred
    print("[oauth] 无可用长效凭据，进入扫码登录…")
    cred = scan_login(max_polls=max_polls)
    save_credentials(cred, path)
    return cred

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="应用宝微信扫码 OAuth（蓝图 #1）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_qr = sub.add_parser("qr", help="扫码登录并保存 credentials.json")
    p_qr.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    p_qr.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR))
    p_qr.add_argument("--max-polls", type=int, default=120)
    p_qr.add_argument("--save-qr-to", default="", help="二维码图保存路径；默认项目根 qr.png")
    p_qr.add_argument("--no-open", dest="open_image", action="store_false", help="不自动打开图片查看器")
    p_qr.add_argument("--ascii", dest="show_ascii", action="store_true", help="额外在终端渲染块字符二维码")

    p_rf = sub.add_parser("refresh", help="用 refresh_token 续期")
    p_rf.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    p_rf.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR))

    p_show = sub.add_parser("show", help="查看当前 credentials（脱敏）")
    p_show.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))

    args = ap.parse_args(argv)

    if args.cmd == "qr":
        try:
            cred = scan_login(
                max_polls=args.max_polls,
                evidence_dir=args.evidence_dir,
                save_qr_to=args.save_qr_to or None,
                open_image=args.open_image,
                show_ascii=args.show_ascii,
            )
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            return 1
        fp = save_credentials(cred, args.credentials)
        print(json.dumps({"ok": True, "credentials": str(fp), "cred": cred.redacted()}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "refresh":
        cred = load_credentials(args.credentials)
        if not cred or not cred.is_valid():
            print(json.dumps({"ok": False, "error": "no valid credentials to refresh"}, ensure_ascii=False))
            return 1
        cred = refresh_credentials(cred, evidence_dir=args.evidence_dir)
        save_credentials(cred, args.credentials)
        print(json.dumps({"ok": cred.is_valid(), "cred": cred.redacted()}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "show":
        cred = load_credentials(args.credentials)
        if not cred:
            print(json.dumps({"ok": False, "error": "no credentials file"}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": cred.is_valid(), "cred": cred.redacted()}, ensure_ascii=False, indent=2))
        return 0

    return 2

if __name__ == "__main__":
    raise SystemExit(main())
