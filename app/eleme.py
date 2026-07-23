"""内置项目：饿了么 / 淘宝闪购 小程序 Code 登录换 Cookie。

流程：code(饿了么小程序 wx.login) → https://ipassport.ele.me/mini_program/login.do
      → 提取 session → 组装账号 Cookie（cookie2/sid/SID/userId/openId/unionId/sgcookie/munb/UTUSER/st）。

算法参数（阿里 fireye/AWSC 加固：bx-ua / mini-janus / bx-umidtoken）无法纯 Python 还原，
由 eleme_assets/ 下的 Node 桥（awsc_bridge.cjs + mor.v.js + fireyejs.min.js）生成，
本模块通过子进程一次性取回后，其余请求全部在 Python 侧完成（协议主体纯 Python）。

前置：服务器需可执行 `node`（可用 ELEME_NODE 环境变量指定路径）；缺 Node 时该项目会给出明确提示。
appId 固定为饿了么小程序 wxece3a9a4c82f58c9，可用 ELEME_APPID 覆盖。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

import httpx

from .config import ROOT

APP_ID = os.environ.get("ELEME_APPID", "wxece3a9a4c82f58c9")
APP_VERSION = os.environ.get("ELEME_APP_VERSION", "12.6.3")
LOGIN_ENDPOINT = os.environ.get("ELEME_LOGIN_ENDPOINT", "https://ipassport.ele.me/mini_program/login.do")

ELEME_ASSETS = os.path.join(str(ROOT), "eleme_assets")
AWSC_BRIDGE = os.path.join(ELEME_ASSETS, "awsc_bridge.cjs")

DEFAULT_UA = os.environ.get(
    "ELEME_USER_AGENT",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "Mobile/15E148 MicroMessenger/8.0.58 miniProgram/%s" % APP_ID,
)
DEFAULT_REFERER = os.environ.get(
    "ELEME_REFERER", "https://servicewechat.com/%s/831/page-frame.html" % APP_ID
)

_RISK_HINTS = ("风控", "风险", "验证", "拦截", "频繁", "异常", "核身", "risk")


# ---------------------------------------------------------------------------
# JS 对齐工具（与独立版 eleme_login.py 一致）
# ---------------------------------------------------------------------------
def _js_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _js_str(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return ""
    return str(value)


_URI_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")
_FORM_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789*-._")


def _pct(s, safe_set, space_char) -> str:
    out = []
    for ch in str(s):
        if ch == " ":
            out.append(space_char)
        elif ch in safe_set:
            out.append(ch)
        else:
            for b in ch.encode("utf-8"):
                out.append("%%%02X" % b)
    return "".join(out)


def _enc_uri_component(s) -> str:
    return _pct(s, _URI_SAFE, "%20")


def _form_encode(pairs) -> str:
    items = list(pairs.items()) if isinstance(pairs, dict) else list(pairs)
    return "&".join("%s=%s" % (_pct(k, _FORM_SAFE, "+"), _pct(v, _FORM_SAFE, "+")) for k, v in items)


# 账号 Cookie 只保留这些字段（输出键名, session 取值候选键），顺序即输出顺序
ACCOUNT_COOKIE_FIELDS = [
    ("cookie2", ("cookie2", "sid")),
    ("sid", ("sid",)),
    ("SID", ("SID",)),
    ("userId", ("user_id", "userId", "USERID")),
    ("openId", ("open_id", "openId")),
    ("unionId", ("union_id", "unionId")),
    ("sgcookie", ("sgcookie",)),
    ("munb", ("munb",)),
    ("UTUSER", ("UTUSER",)),
    ("st", ("st",)),
]


def build_account_cookie(session: dict) -> str:
    session = session or {}
    parts = []
    for out_key, src_keys in ACCOUNT_COOKIE_FIELDS:
        value = None
        for sk in src_keys:
            v = session.get(sk)
            if v is not None and _js_str(v) != "":
                value = v
                break
        if value is None:
            continue
        if out_key == "SID":
            parts.append("%s=%s" % (out_key, _js_str(value)))  # SID 不编码
        else:
            parts.append("%s=%s" % (out_key, _enc_uri_component(_js_str(value))))
    return "; ".join(parts)


def _session_to_x_smallstc(session: dict):
    copy = dict(session or {})
    copy.pop("username", None)
    return _js_json(copy) if copy else ""


def _node_exe() -> str:
    return os.environ.get("ELEME_NODE") or shutil.which("node") or shutil.which("node.exe") or ""


# ---------------------------------------------------------------------------
# 算法参数：异步调用 eleme_assets/awsc_bridge.cjs（Node）
# ---------------------------------------------------------------------------
async def build_awsc_params(url: str, session: dict | None = None, umid_token: str = "") -> dict:
    node = _node_exe()
    if not node:
        return {"ok": False, "error": "未检测到 Node 运行时（生成 bx-ua/mini-janus 需要 node），"
                                       "请安装 Node.js 或用 ELEME_NODE 指定路径"}
    if not os.path.isfile(AWSC_BRIDGE):
        return {"ok": False, "error": "缺少 eleme_assets/awsc_bridge.cjs 等算法资源"}

    args = {"url": url, "session": session or {}, "umidToken": umid_token or "",
            "debug": False, "noNetwork": False}
    args_fd, args_path = tempfile.mkstemp(suffix="_awsc_args.json")
    out_fd, out_path = tempfile.mkstemp(suffix="_awsc_out.json")
    os.close(args_fd)
    os.close(out_fd)
    try:
        with open(args_path, "w", encoding="utf-8") as f:
            f.write(_js_json(args))
        proc = await asyncio.create_subprocess_exec(
            node, AWSC_BRIDGE, args_path, out_path,
            cwd=ELEME_ASSETS,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": "算法参数生成超时（Node 桥 90s 未返回）"}
        with open(out_path, "r", encoding="utf-8") as f:
            result = json.load(f)
        if not isinstance(result, dict):
            return {"ok": False, "error": "算法参数结果格式异常"}
        return result
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "算法参数生成失败：%s" % e}
    finally:
        for p in (args_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# code -> login.do -> session
# ---------------------------------------------------------------------------
async def havana_code_login(code: str, proxy_url: str = "") -> dict:
    awsc = await build_awsc_params(LOGIN_ENDPOINT, session={})
    awsc_ok = bool(awsc.get("ok"))
    params = awsc.get("params") or {}
    auto_umid = params.get("bxUmidToken") or ""

    authorization_code = _js_json({"authorizationCode": code})
    request_data = {
        "type": "weixin_mini_program",
        "appId": APP_ID,
        "appName": "eleme",
        "appEntrance": "weixin",
        "lang": "zh_CN",
        "isMobile": True,
        "returnUrl": "",
        "needPassWebViewCookie": False,
        "authorizationCode": authorization_code,
    }
    if auto_umid:
        request_data["umidToken"] = auto_umid
    body = _form_encode([(k, _js_str(v)) for k, v in request_data.items()])

    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": DEFAULT_UA,
        "Referer": DEFAULT_REFERER,
        "x-tap": "wx",
    }
    for k, v in (awsc.get("headers") or {}).items():
        if v not in (None, ""):
            headers[k] = str(v)

    kw = {"proxy": proxy_url} if proxy_url else {}
    async with httpx.AsyncClient(timeout=20.0, verify=False, **kw) as cli:
        r = await cli.post(LOGIN_ENDPOINT, content=body.encode("utf-8"), headers=headers)

    try:
        j = r.json()
    except Exception:
        j = None
    content_data = {}
    if isinstance(j, dict) and isinstance(j.get("content"), dict) \
            and isinstance(j["content"].get("data"), dict):
        content_data = j["content"]["data"]
    session = dict(content_data)
    if session.get("sid") and not session.get("cookie2"):
        session["cookie2"] = session["sid"]

    cookie = build_account_cookie(session)
    ok = 200 <= r.status_code < 300 and bool(session.get("cookie2") or session.get("sid") or session.get("st"))

    err = ""
    if not ok:
        if not j:
            err = "接口未返回 JSON（HTTP %s）" % r.status_code
        elif j.get("hasError"):
            c = j.get("content") or {}
            err = "接口错误：%s" % (c.get("errorMsg") or c.get("msg") or j.get("msg") or "未知错误")
        elif content_data.get("titleMsg"):
            err = "业务错误：%s" % content_data["titleMsg"]
        elif any(content_data.get(k) for k in ("redirect", "iframeRedirect", "redirectUrl", "iframeRedirectUrl")):
            err = "返回跳转/核身/绑定流程，未直接下发 cookie（可能触发风控或需绑定）"
        else:
            err = "登录未成功：响应无 sid/st/cookie2（HTTP %s）" % r.status_code
        if not awsc_ok:
            err += "；且算法参数(bx-ua)未生成：%s" % (awsc.get("error") or "未知")

    return {
        "ok": ok,
        "status": r.status_code,
        "error": err,
        "cookie": cookie,
        "session": session,
        "username": session.get("username") or "",
        "userId": session.get("user_id") or session.get("userId") or session.get("USERID") or "",
        "xSmallstc": _session_to_x_smallstc(session),
        "awscOk": awsc_ok,
    }


async def run_eleme_code_login(user_id: int, openid: str, appid: str, proxy_url: str = "") -> dict:
    """内置项目入口：与 jd.run_jd_code_login 对齐的返回结构。"""
    from .codebridge import get_code_for_openid

    cr = await get_code_for_openid(openid, appid or APP_ID)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code",
                "error": cr.get("error") or "获取饿了么小程序 code 失败"}

    login = await havana_code_login(cr["code"], proxy_url)
    if not login["ok"]:
        err = login.get("error") or "饿了么 code 登录失败"
        if any(h in err for h in _RISK_HINTS):
            err = "疑似触发风控或需绑定：%s" % err
        return {"ok": False, "stage": "eleme-login", "error": err, "code": cr["code"]}

    return {
        "ok": True,
        "elemeCookie": login["cookie"],
        "cookie": login["cookie"],
        "account": login.get("username") or login.get("userId") or "",
        "username": login.get("username") or "",
        "userId": login.get("userId") or "",
        "code": cr["code"],
    }
