"""Built-in project: 益禾堂（茶饮）积分签到。

把独立脚本「益禾堂签到.py」移植到 app 的内置项目模型：用户选一个已绑定的微信账号，
后端用 codebridge 取益禾堂小程序的 wx.login code，登录换 qm-user-token；若账号未绑定
手机号，再取一次 getPhoneNumber code 绑定手机号并重新登录，随后用 token 换兑吧活动页登录
Cookie，最后执行每日签到。取码沿用账号绑定的地区代理，业务接口同样走该代理（异地防风控）。

任务逻辑（URL/headers/payload）忠实移植自原脚本；仅把「从网站 API 取账号/取码」换成内部
codebridge，并把日志收集到 result 里复用内置项目的结果文本框。签到 token 由服务端下发的一段
JS 生成，需本机可用 Node.js。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 小程序 / 活动配置 ====================
BASE = "https://86019-activity.dexfu.cn"
QMAI_BASE = "https://webapi.qmai.cn"
APPID = "wx4080846d0cec2fd5"
STORE_ID = "203009"
SIGN_ID = "334833691538956"
REDIRECT_URL = "https://86019.activity-12.m.duiba.com.cn/chw/visual-editor/skins?id=203576"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf254181c) XWEB/19977 miniProgram/wx4080846d0cec2fd5"
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _qmai_headers(token: str = "") -> dict:
    return {
        "User-Agent": UA,
        "Accept": "v=1.0",
        "Content-Type": "application/json",
        "xweb_xhr": "1",
        "Qm-From-Type": "catering",
        "Qm-From": "wechat",
        "qm-user-token": token,
        "Qm-User-Token": token,
        "store-id": STORE_ID,
        "scene": "1101",
        "accept-language": "zh-CN",
        "Referer": f"https://servicewechat.com/{APPID}/538/page-frame.html",
    }


def _read_set_cookie(headers) -> dict:
    raw_items = []
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        raw_items.extend(get_all("Set-Cookie") or get_all("set-cookie") or [])
    if not raw_items:
        value = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
        if value:
            raw_items.append(value)

    cookies = {}
    for item in raw_items:
        jar = SimpleCookie()
        try:
            jar.load(item)
        except Exception:
            continue
        for key, morsel in jar.items():
            cookies[key] = morsel.value
    return cookies


def _run_token_js(js_code: str) -> list[str]:
    """用 Node 执行服务端下发的 JS，取 window 上生成的候选 token。"""
    with tempfile.TemporaryDirectory() as td:
        js_path = Path(td) / "ctoken.js"
        runner = Path(td) / "runner.js"
        js_path.write_text(js_code, encoding="utf-8")
        runner.write_text(
            r"""
const fs = require('fs');
const vm = require('vm');
let code = fs.readFileSync(process.argv[2], 'utf8');
code = code.replace(/\b0([0-7]+)\b/g, '0o$1');
const assigned = [];
const targetWindow = {};
const windowProxy = new Proxy(targetWindow, {
  set(obj, prop, value) {
    assigned.push({ prop: String(prop), value });
    obj[prop] = value;
    return true;
  }
});
const sandbox = {
  window: windowProxy, document: {}, navigator: {}, console: { log(){}, warn(){}, error(){} },
  Math, Date, String, Array, Object, RegExp, Number, Boolean,
  parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent
};
vm.runInNewContext(code, sandbox, { timeout: 3000 });
const values = assigned
  .map(item => item.value)
  .filter(val => typeof val === 'string' && /^[a-z0-9]{5,12}$/i.test(val));
const candidates = [];
for (const item of assigned) {
  if (/^[0-9a-f]{7}t$/i.test(item.prop) && typeof item.value === 'string') {
    candidates.push(item.value);
  }
}
if (!candidates.length && values.length >= 3) {
  candidates.push(values[2]);
}
process.stdout.write(JSON.stringify([...new Set(candidates)]));
""",
            encoding="utf-8",
        )
        try:
            out = subprocess.check_output(["node", str(runner), str(js_path)], text=True, timeout=8)
        except FileNotFoundError:
            raise RuntimeError("未找到 node，无法执行 getToken 返回的 JS。请在服务器安装 Node.js。")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"执行 token JS 失败：{e}")
    values = json.loads(out or "[]")
    if not values:
        raise RuntimeError("token JS 未生成候选 token")
    return values


class YihetangRunner:
    """单账号运行器：持有本次运行的代理与日志，任务方法忠实移植自原脚本。"""

    def __init__(self, proxies: dict | None = None, timeout: int = 15) -> None:
        self.proxies = proxies
        self.timeout = timeout
        self.lines: list[str] = []

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{level}] {message}"
        self.lines.append(line)
        from .logsink import emit
        emit(line)

    # ---------------- 登录 / 手机号授权（qmai） ----------------
    def login(self, wx_code: str):
        """wx.login code -> mini-app-login，换 qm-user-token。返回 (token, data)。"""
        url = f"{QMAI_BASE}/web/account-center/oauth/mini-app-login"
        body = {"code": wx_code, "eVersion": "1.0", "appid": APPID}
        r = requests.post(url, headers=_qmai_headers(""), json=body,
                          proxies=self.proxies, timeout=self.timeout, verify=False)
        try:
            obj = r.json()
        except Exception as exc:
            raise RuntimeError(f"益禾堂登录返回非 JSON：HTTP {r.status_code} {r.text[:120]}") from exc
        if not obj.get("status"):
            raise RuntimeError(obj.get("message") or obj.get("msg") or json.dumps(obj, ensure_ascii=False)[:160])
        token = ((obj.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError("益禾堂登录成功但未返回 qm-user-token")
        return token, obj.get("data") or {}

    def bind_mobile(self, qm_token: str, phone_code: str) -> None:
        url = f"{QMAI_BASE}/web/account-center/oauth/bind-mobile"
        body = {
            "code": phone_code,
            "reg_activity_source": 0,
            "is_update_mobile": 0,
            "channel_code": "",
            "flowScene": 1256,
            "eVersion": "1.0",
            "appid": APPID,
        }
        r = requests.post(url, headers=_qmai_headers(qm_token), json=body,
                          proxies=self.proxies, timeout=self.timeout, verify=False)
        try:
            obj = r.json()
        except Exception as exc:
            raise RuntimeError(f"bind-mobile 返回非 JSON：HTTP {r.status_code} {r.text[:120]}") from exc
        if not obj.get("status"):
            raise RuntimeError(obj.get("message") or obj.get("msg") or json.dumps(obj, ensure_ascii=False)[:160])

    def token_to_duiba_cookie(self, qm_token: str) -> str:
        """qm-user-token -> 兑吧活动页登录 Cookie。"""
        redirect_api = f"{QMAI_BASE}/web/catering/crm/member/redirect"
        body = {"redirectUrl": REDIRECT_URL, "appid": APPID}
        r = requests.post(redirect_api, headers=_qmai_headers(qm_token), json=body,
                          proxies=self.proxies, timeout=self.timeout, verify=False)
        try:
            obj = r.json()
        except Exception as exc:
            raise RuntimeError(f"member/redirect 返回非 JSON：HTTP {r.status_code} {r.text[:120]}") from exc
        if not obj.get("status") or not obj.get("data"):
            raise RuntimeError(obj.get("message") or obj.get("msg") or json.dumps(obj, ensure_ascii=False)[:160])

        activity_url = obj["data"]
        res = requests.get(activity_url, headers={"User-Agent": UA}, allow_redirects=False,
                           proxies=self.proxies, timeout=self.timeout, verify=False)

        cookies = {}
        cookies.update(_read_set_cookie(res.raw.headers))
        cookies.update(_read_set_cookie(res.headers))
        cookies.update(res.cookies.get_dict())

        wanted = ("wdata4", "w_ts", "_ac", "tokenId", "wdata3", "createdAtToday", "isNotLoginUser", "dcustom", "deap")
        parts = [f"{key}={cookies[key]}" for key in wanted if cookies.get(key)]
        if not parts:
            raise RuntimeError(f"兑吧活动页未返回登录 Cookie：HTTP {res.status_code}")
        return "; ".join(parts)

    # ---------------- 签到（兑吧活动页） ----------------
    def _sign_session(self, cookie: str, sign_id: str) -> requests.Session:
        s = requests.Session()
        if self.proxies:
            s.proxies.update(self.proxies)
        s.verify = False
        s.headers.update({
            "User-Agent": UA,
            "Cookie": cookie,
            "Origin": BASE,
            "Referer": f"{BASE}/sign/component/page?signOperatingId={sign_id}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })
        return s

    def _req_json(self, s: requests.Session, method: str, path: str, **kwargs):
        url = BASE + path
        r = s.request(method, url, timeout=self.timeout, **kwargs)
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"接口未返回 JSON：HTTP {r.status_code} {r.text[:120]}")

    def _query_credits(self, s):
        obj = self._req_json(s, "POST", "/ctool/getCredits", params={"_": _now_ms()}, data="")
        data = obj.get("data") or {}
        return data.get("credits") or data.get("consumerCredits")

    def _query_sign_status(self, s, sign_id):
        obj = self._req_json(
            s, "GET", "/sign/component/index",
            params={"signOperatingId": sign_id, "preview": "false", "_": _now_ms()},
        )
        data = obj.get("data") or {}
        return {
            "signed": bool(data.get("signResult")),
            "consecutive": data.get("consecutiveCount"),
            "total": data.get("totalCount"),
            "title": data.get("title"),
        }

    def _token_candidates(self, s):
        ts = _now_ms()
        obj = self._req_json(
            s, "POST", "/chw/ctoken/getToken",
            headers={**s.headers, "Accept": "*/*"},
            data={"timestamp": str(ts)},
        )
        js_code = obj.get("token") or ""
        if not js_code:
            raise RuntimeError("getToken 未返回 token JS")
        return _run_token_js(js_code)

    def _poll_result(self, s, order_num):
        last = {}
        for _ in range(8):
            obj = self._req_json(
                s, "GET", "/sign/component/signResult",
                params={"orderNum": order_num, "_": _now_ms()},
            )
            data = obj.get("data") or {}
            last = data
            if data.get("signResult") == 2:
                return data
            time.sleep(1)
        raise RuntimeError(f"签到结果轮询超时：{json.dumps(last, ensure_ascii=False)}")

    def do_sign(self, cookie: str, sign_id: str) -> str:
        s = self._sign_session(cookie, sign_id)
        before_score = self._query_credits(s)
        status = self._query_sign_status(s, sign_id)
        zero_score = str(before_score) in ("0", "0.0", "None", "")
        zero_count = status.get("consecutive") in (None, 0, "0") and status.get("total") in (None, 0, "0")
        if status["signed"] and not (zero_score and zero_count):
            return f"今日已签到；当前积分 {before_score}，连续 {status.get('consecutive')} 天。"

        candidates = self._token_candidates(s)
        last_err = ""
        for token in candidates:
            obj = self._req_json(
                s, "POST", "/sign/component/doSign",
                params={"_": _now_ms()},
                data={"signOperatingId": sign_id, "token": token},
            )
            if not obj.get("success"):
                last_err = obj.get("desc") or obj.get("code") or json.dumps(obj, ensure_ascii=False)[:120]
                if "非法token" in str(last_err):
                    continue
                if "请先登录" in str(last_err):
                    return ("签到失败：请先登录（Code 登录成功，但兑吧活动没有拿到有效登录态，"
                            "常见于账号未完成手机号/会员授权或该账号暂无积分会员信息）。")
                return f"签到失败：{last_err}"

            order_num = ((obj.get("data") or {}).get("orderNum") or "").strip()
            if not order_num:
                return "签到接口成功但未返回 orderNum。"
            result = self._poll_result(s, order_num)
            after_score = self._query_credits(s)
            credits = result.get("credits")
            return f"签到成功：本次 +{credits} 积分；当前积分 {after_score}。"

        return f"签到失败：所有 token 候选都未通过，最后错误：{last_err or '未知'}"


async def run_yihetang_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from .codebridge import get_code_for_openid, get_phone_for_openid
    from . import db

    params = params or {}
    app_id = appid or APPID
    sign_id = str(params.get("signId") or SIGN_ID)

    # 代理：优先本次表单填的 proxyUrl；留空则回退到该微信账号绑定的地区代理，业务接口与取码同地区出网防风控。
    _acc = db.query_one("SELECT proxy_url, nickname FROM accounts WHERE openid=?", (openid,))
    proxy_url = (params.get("proxyUrl") or "").strip() or ((_acc["proxy_url"] or "") if _acc else "")
    username = (_acc["nickname"] if _acc and _acc["nickname"] else openid)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    runner = YihetangRunner(proxies, timeout=int(params.get("timeout") or 15))

    # 1. 取 wx.login code
    cr = await get_code_for_openid(openid, app_id)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "取益禾堂小程序 code 失败"}

    try:
        # 2. 登录换 qm-user-token
        token, login_data = await asyncio.to_thread(runner.login, cr["code"])
        runner.log(f"{username}：登录成功，已取得 qm-user-token")

        # 3. 未绑定手机号则补授权后重新登录
        user = (login_data or {}).get("user") or {}
        if not user.get("mobile"):
            pr = await get_phone_for_openid(openid, app_id)
            phone_code = pr.get("code") if pr.get("success") else ""
            if phone_code:
                await asyncio.to_thread(runner.bind_mobile, token, phone_code)
                cr2 = await get_code_for_openid(openid, app_id)
                if cr2.get("success") and cr2.get("code"):
                    token, login_data = await asyncio.to_thread(runner.login, cr2["code"])
                    runner.log(f"{username}：已完成手机号授权并重新登录")
            else:
                runner.log(f"{username}：账号未绑定手机号且取手机号 code 失败，尝试直接签到", "WARNING")

        # 4. token 换兑吧活动页登录 Cookie
        cookie = await asyncio.to_thread(runner.token_to_duiba_cookie, token)

        # 5. 执行签到（同步 requests + Node，丢线程里跑避免阻塞事件循环）
        msg = await asyncio.to_thread(runner.do_sign, cookie, sign_id)
        runner.log(f"{username}：{msg}", "SUCCESS")
    except Exception as exc:
        runner.log(f"{username}：签到异常：{exc}", "ERROR")

    summary: dict[str, Any] = {
        "account": username,
        "viaProxy": bool(proxy_url),
        "log": runner.lines,
    }
    text = "\n".join(runner.lines) or "（无输出）"
    return {
        "ok": True,
        "stage": "yihetang",
        "account": username,
        "viaProxy": bool(proxy_url),
        "yihetangResult": json.dumps(summary, ensure_ascii=False, indent=2),
        # 复用内置项目的结果文本框
        "cookie": text,
    }
