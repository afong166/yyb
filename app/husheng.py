"""Built-in project: 沪上阿姨（小满活动）每日签到。

把独立脚本「沪上阿姨签到.py」移植到 app 的内置项目模型：用户选一个已绑定的微信账号，
后端用 codebridge 取沪上阿姨小程序的 wx.login code，登录 qmai 拿会员 id；若账号未绑定
手机号 / 未注册会员，用 getPhoneNumber code 绑定手机号并重新登录；随后打开小满授权页并用
授权回跳刷新活动 li，动态生成 tokenSign / xmSign，最后查询并执行每日签到。取码与业务接口
统一走账号绑定的地区代理（异地防风控）。

任务逻辑（URL / headers / payload / 签名 / base64 常量）忠实移植自原脚本；仅把「从本地
YYB code 服务取账号/取码」换成内部 codebridge，并把日志收集到 result 里复用内置项目的结果
文本框。签名为纯 Python（md5），无需 Node.js。
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import hashlib
import json
import re
import secrets
import time
import urllib.parse
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 小程序 / 活动 / 签名配置（照抄原脚本，不得改写） ====================
TOKEN_SECRET_B64 = "SjdoOCZeQmdzNSNibio3aG4lIT1raDMwOCpidjIhc14="
SIGN_SECRET_B64 = "dWgzJEhnJl5ISzg3NiVnYnhWRzdmJCVwPTBNfj5zMXg="
TOKEN_SECRET = base64.b64decode(TOKEN_SECRET_B64).decode("utf-8")
SIGN_SECRET = base64.b64decode(SIGN_SECRET_B64).decode("utf-8")

QMAI_APP_ID = "wxd92a2d29f8022f40"
QMAI_STORE_ID = "201424"
SIGN_PRIZE_FUNCTION_ID = 100540000
DEFAULT_OA_OPENID = "null"
DEFAULT_CHANNEL_CODE = "scrm_uubct8r8ote4anz"
DEFAULT_FLOW_SCENE = 1179
ACTIVITY_ENTRY_URL = (
    "https://p7955695914055-hsay.bx-index.meta-huanxuan.com"
    "/xm/activity/place/7955695914055/54-95107-06erzhqq6e/v1-hdzyhsay"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b37) NetType/WIFI Language/zh_CN "
    "miniProgram/wxd92a2d29f8022f40"
)

NANOID_ALPHABET = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"


class NotMemberError(RuntimeError):
    pass


def phone_mask(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(1\d{2})\d{4}(\d{4})", text)
    if match:
        return f"{match.group(1)}****{match.group(2)}"
    match = re.search(r"(1\d{2})\*{3,4}(\d{4})", text)
    if match:
        return f"{match.group(1)}****{match.group(2)}"
    return ""


class PhoneAlreadyBoundError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.masked_phone = phone_mask(message)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def now_ms() -> str:
    return str(int(time.time() * 1000))


def random_nonce(size: int = 32) -> str:
    return "".join(secrets.choice(NANOID_ALPHABET) for _ in range(size))


def mask_text(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}***{value[-keep:]}"


def format_mobile(value: Any) -> str:
    return mask_text(str(value or "")) or "未绑定"


def js_stringify_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def calc_xm_sign(params: dict[str, Any], *, drop_null: bool = False) -> str:
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if drop_null and (value is None or value == "null"):
            continue
        parts.append(js_stringify_value(value))
    return md5_hex("".join(parts) + SIGN_SECRET)


def append_query(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    clean = {key: str(value) for key, value in params.items() if value is not None}
    query = urllib.parse.urlencode(clean)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{query}"


def today_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


# ==================== 传输层（改为 requests，支持代理；签名/URL/payload 不变） ====================
def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    proxies: dict | None = None,
    timeout: int = 20,
) -> Any:
    final_url = append_query(url, params)
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    r = requests.request(
        method.upper(), final_url, headers=req_headers, data=data,
        proxies=proxies, timeout=timeout, verify=False,
    )
    text = r.text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"响应不是 JSON：HTTP {r.status_code} {text[:300]}") from exc


def _request_final_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    proxies: dict | None = None,
    timeout: int = 20,
) -> str:
    r = requests.get(
        url, headers=headers or {}, proxies=proxies, timeout=timeout,
        verify=False, allow_redirects=True, stream=True,
    )
    final = r.url
    try:
        r.close()
    except Exception:
        pass
    return final


def qmai_headers(user_agent: str, token: str = "") -> dict[str, str]:
    return {
        "Qm-From": "wechat",
        "Qm-From-Type": "catering",
        "Qm-User-Token": token,
        "store-id": QMAI_STORE_ID,
        "User-Agent": user_agent,
        "Referer": f"https://servicewechat.com/{QMAI_APP_ID}/459/page-frame.html",
        "Content-Type": "application/json",
    }


class HsayClient:
    """活动页签到客户端：动态生成 xmToken / xmSign，忠实移植自原脚本。"""

    def __init__(self, activity_url: str, user_agent: str,
                 proxies: dict | None = None, timeout: int = 20) -> None:
        self.activity_url = activity_url
        parsed = urllib.parse.urlparse(activity_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("activity_url 不是完整 URL")
        qs = urllib.parse.parse_qs(parsed.query)
        self.li = qs.get("li", [""])[0]
        if not self.li:
            raise ValueError("activity_url 缺少 li 参数")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.proxies = proxies
        self.timeout = timeout

    def _headers(
        self,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        need_token: bool = False,
        function_id: int = 0,
    ) -> dict[str, str]:
        nonce = random_nonce()
        timestamp = now_ms()
        sign_params: dict[str, Any] = {}
        sign_params.update(params or {})
        sign_params.update(body or {})
        sign_params["nonceStr"] = nonce
        sign_params["xmTimestamp"] = timestamp
        if method.upper() == "GET":
            sign_params.pop("functionId", None)

        header_function_id: Any = function_id
        if header_function_id == 0:
            source = body or params or {}
            header_function_id = source.get("functionId", 0)

        headers = {
            "User-Agent": self.user_agent,
            "Referer": self.activity_url,
            "functionId": str(header_function_id),
            "nonceStr": nonce,
            "xmTimestamp": timestamp,
        }

        if need_token:
            token = self.get_user_token(parent_nonce=nonce, parent_timestamp=timestamp)
            headers["xmToken"] = token
            sign_params["xmToken"] = token

        headers["xmSign"] = calc_xm_sign(sign_params)
        if method.upper() == "POST":
            headers["Origin"] = self.origin
            headers["Content-Type"] = "application/json"
        return headers

    def api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        need_token: bool = False,
    ) -> Any:
        headers = self._headers(method, params=params, body=body, need_token=need_token)
        data = _request_json(
            method,
            self.origin + path,
            headers=headers,
            params=params,
            body=body,
            proxies=self.proxies,
            timeout=self.timeout,
        )
        code = str(data.get("code", "")) if isinstance(data, dict) else ""
        if code and code != "0":
            desc = data.get("desc") or data.get("message") or data
            raise RuntimeError(f"接口返回失败 code={code}：{desc}")
        return data.get("data") if isinstance(data, dict) and "data" in data else data

    def get_user_token(self, *, parent_nonce: str, parent_timestamp: str) -> str:
        token_sign = md5_hex(self.li + parent_nonce + parent_timestamp + TOKEN_SECRET)
        data = self.api(
            "GET",
            "/xm/token/getUserToken",
            params={
                "timestamp": parent_timestamp,
                "nonceStr": parent_nonce,
                "tokenSign": token_sign,
            },
        )
        if not isinstance(data, str) or "@" not in data:
            raise RuntimeError(f"获取 xmToken 失败：{data}")
        return data

    def get_sign_config(self, date_text: str) -> dict[str, Any]:
        data = self.api("GET", "/sign/getConfig", params={"curDate": date_text})
        if not isinstance(data, dict):
            raise RuntimeError(f"签到配置异常：{data}")
        return data

    def get_prize_list(self, function_id: int = SIGN_PRIZE_FUNCTION_ID) -> dict[str, Any]:
        data = self.api(
            "GET",
            "/activity/function/getPrizeList",
            params={"functionId": function_id, "filterThank": 1},
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"奖品列表异常：{data}")
        return data

    def sign(self, date_text: str) -> dict[str, Any]:
        data = self.api("POST", "/sign/action", body={"patchDate": date_text}, need_token=True)
        if not isinstance(data, dict):
            raise RuntimeError(f"签到响应异常：{data}")
        return data

    def prepare_activity(self) -> None:
        """按前端初始化顺序刷新活动态，否则未签账号提交会提示页面变化。"""
        steps: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = [
            ("GET", "/activity/function/getThemeConfig", None, None),
            ("GET", "/activity/getGlobalConfig", {"key": "xmLogoSwitch"}, None),
            ("POST", "/behavior/log", None, {"behaviorId": 1011000}),
            ("POST", "/activity/function/task/handleTask", None, None),
            ("GET", "/activity/function/getConfig", None, None),
            ("GET", "/message/notice", None, None),
            ("GET", "/activity/function/getUserAccount", None, None),
            ("GET", "/activity/getThresholdConfig", None, None),
        ]
        for method, path, params, body in steps:
            self.api(method, path, params=params, body=body)


def pick_prize(sign_result: dict[str, Any]) -> str:
    action = sign_result.get("actionResult") or {}
    pop = action.get("actionPop") or {}
    title = pop.get("title") or ""
    sub_title = pop.get("subTitle") or ""
    if title or sub_title:
        return f"{title} {sub_title}".strip()
    award = action.get("actionAward") or {}
    rewards = award.get("rewardList") or []
    if rewards:
        first = rewards[0]
        if isinstance(first, dict):
            return str(first.get("prizeName") or first.get("name") or first)
    return "无奖品信息"


def find_today_mc_conf(config: dict[str, Any], date_text: str) -> dict[str, Any]:
    date_info_list = config.get("dateInfoList") or []
    if not isinstance(date_info_list, list):
        return {}

    fallback: dict[str, Any] = {}
    for item in date_info_list:
        if not isinstance(item, dict):
            continue
        mc_conf = item.get("mcConf")
        if not isinstance(mc_conf, dict):
            continue
        if str(item.get("curDate") or "") == date_text:
            return mc_conf
        start_date = str(mc_conf.get("startDate") or "")
        end_date = str(mc_conf.get("endDate") or "")
        if start_date and end_date and start_date <= date_text <= end_date:
            fallback = mc_conf
    return fallback


def extract_prize_titles(
    prize_data: dict[str, Any],
    *,
    prize_scene_type: Any = None,
    limit: int = 6,
) -> list[str]:
    configs = prize_data.get("functionPrizeConfig") or []
    if not isinstance(configs, list):
        return []

    titles: list[str] = []
    seen: set[str] = set()
    expected_scene = str(prize_scene_type) if prize_scene_type is not None else ""
    for config in configs:
        if not isinstance(config, dict):
            continue
        prize_list = config.get("prizeList") or []
        if not isinstance(prize_list, list):
            continue
        for prize in prize_list:
            if not isinstance(prize, dict):
                continue
            if expected_scene and str(prize.get("prizeSceneType") or "") != expected_scene:
                continue
            title = str(prize.get("prizeTitle") or prize.get("prizeName") or prize.get("name") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            titles.append(title)
            if len(titles) >= limit:
                return titles
    return titles


def format_today_sign_detail(config: dict[str, Any], prize_data: dict[str, Any], date_text: str) -> str:
    mc_conf = find_today_mc_conf(config, date_text)
    title = str(mc_conf.get("title") or "").strip()
    prize_titles = extract_prize_titles(prize_data, prize_scene_type=mc_conf.get("prizeSceneType"))

    if title and prize_titles:
        return f"今日签到信息：{title}；可见奖励池：{'、'.join(prize_titles)}"
    if title:
        return f"今日签到信息：{title}"
    if prize_titles:
        return f"今日签到信息：可见奖励池：{'、'.join(prize_titles)}"
    return ""


class HushengRunner:
    """单账号运行器：持有本次运行的代理 / UA / 日志；任务方法忠实移植自原脚本。"""

    def __init__(self, proxies: dict | None = None, user_agent: str = DEFAULT_USER_AGENT,
                 timeout: int = 20, channel_code: str = DEFAULT_CHANNEL_CODE,
                 flow_scene: int = DEFAULT_FLOW_SCENE, oa_openid: str = DEFAULT_OA_OPENID) -> None:
        self.proxies = proxies
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.channel_code = channel_code
        self.flow_scene = flow_scene
        self.oa_openid = oa_openid or DEFAULT_OA_OPENID
        self.lines: list[str] = []

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{level}] {message}"
        self.lines.append(line)
        from .logsink import emit
        emit(line)

    # ---------------- qmai 登录 / 手机号授权 ----------------
    def _qmai_request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = _request_json(
            method,
            "https://webapi.qmai.cn" + path,
            headers=qmai_headers(self.user_agent, token),
            params=params,
            body=body,
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"qmai 响应异常：{payload}")
        if str(payload.get("code")) != "0":
            if str(payload.get("code")) == "10012":
                raise PhoneAlreadyBoundError(str(payload.get("message") or "手机号已经被绑定"))
            raise RuntimeError(f"qmai 接口失败 code={payload.get('code')}：{payload.get('message') or payload}")
        return payload

    def qmai_mini_login(self, wx_code: str) -> dict[str, Any]:
        payload = self._qmai_request(
            "POST",
            "/web/account-center/oauth/mini-app-login",
            body={"code": wx_code, "eVersion": "1.0", "appid": QMAI_APP_ID},
        )
        data = payload.get("data") or {}
        user = data.get("user") or {}
        if not user.get("id"):
            raise RuntimeError(f"qmai 登录响应缺少会员 id：{payload}")
        return data

    def qmai_exit_login(self, token: str) -> None:
        try:
            self._qmai_request(
                "POST",
                "/web/account-center/oauth/exitLogin",
                token=token,
                body={"appid": QMAI_APP_ID},
            )
        except RuntimeError as exc:
            if "20000" in str(exc) or "用户不存在" in str(exc):
                return
            raise

    def qmai_bind_mobile(self, phone_code: str, token: str) -> dict[str, Any]:
        payload = self._qmai_request(
            "POST",
            "/web/account-center/oauth/bind-mobile",
            token=token,
            body={
                "code": phone_code,
                "reg_activity_source": 0,
                "is_update_mobile": 0,
                "channel_code": self.channel_code,
                "flowScene": self.flow_scene,
                "eVersion": "1.0",
                "appid": QMAI_APP_ID,
            },
        )
        data = payload.get("data") or {}
        login_token = data.get("loginToken") or {}
        user = login_token.get("user") or {}
        if not user.get("id"):
            raise RuntimeError(f"绑定手机号后缺少会员 id：{payload}")
        return login_token

    # ---------------- 活动入口 / 授权回跳刷新 li ----------------
    def get_activity_auth_page(self, customer_id: str) -> str:
        entry_url = append_query(ACTIVITY_ENTRY_URL, {"platform": 3, "openId": customer_id})
        final_url = _request_final_url(
            entry_url, headers={"User-Agent": self.user_agent},
            proxies=self.proxies, timeout=self.timeout,
        )
        if "not_member" in final_url:
            raise NotMemberError("该微信号不是沪上阿姨会员或未绑定手机号")
        if "xm_open_auth2.html" not in final_url:
            raise RuntimeError(f"活动入口未返回授权页：{final_url}")
        return final_url

    def refresh_activity_url_by_auth_redirect(self, auth_page_url: str) -> str:
        parsed = urllib.parse.urlparse(auth_page_url)
        qs = urllib.parse.parse_qs(parsed.query)
        redirect_values = qs.get("redirect")
        if not redirect_values:
            raise RuntimeError("授权页中没有 redirect 参数")
        redirect_url = urllib.parse.unquote(redirect_values[-1])
        redirect_url = (
            f"{redirect_url}&oaOpenId={urllib.parse.quote(str(self.oa_openid or 'null'))}"
            "&nickName=null&avatar=null"
        )
        final_url = _request_final_url(
            redirect_url,
            headers={"User-Agent": self.user_agent, "Referer": "https://yl-auth.meta-xuantan.com/"},
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if "saas_marketing_signin.html" not in final_url or "li=" not in final_url:
            raise RuntimeError(f"未拿到活动页 li，最终 URL：{final_url}")
        return final_url

    # ---------------- 签到主流程 ----------------
    def run_sign_flow(self, activity_url: str, *, date_text: str,
                      dry_run: bool, force: bool) -> dict[str, Any]:
        client = HsayClient(activity_url, user_agent=self.user_agent,
                            proxies=self.proxies, timeout=self.timeout)
        summary: dict[str, Any] = {"date": date_text, "li": mask_text(client.li)}

        config = client.get_sign_config(date_text)
        status = int(config.get("curSignStatus") or 0)
        signed_days = config.get("signedDays")
        summary["signedDays"] = signed_days
        self.log(f"{date_text} 当前签到状态：{'已签到' if status == 1 else '未签到'}，本月已签 {signed_days} 天")

        if dry_run or (status == 1 and not force):
            try:
                detail = format_today_sign_detail(config, client.get_prize_list(), date_text)
                if detail:
                    self.log(detail)
                    summary["detail"] = detail
            except Exception as exc:
                self.log(f"今日签到信息获取失败：{exc}", "WARNING")

        if dry_run:
            self.log("dry-run：只查询状态，未提交签到。")
            summary["result"] = "dry-run"
            return summary

        if status == 1 and not force:
            self.log(f"今日已签到，本月已签 {signed_days} 天，跳过提交。", "SUCCESS")
            summary["result"] = "已签到"
            return summary

        self.log("执行活动初始化，刷新签到前置状态")
        client.prepare_activity()
        result = client.sign(date_text)
        prize = pick_prize(result)
        self.log(f"签到成功：{prize}", "SUCCESS")
        summary["prize"] = prize

        latest = client.get_sign_config(date_text)
        latest_status = int(latest.get("curSignStatus") or 0)
        summary["signedDays"] = latest.get("signedDays")
        self.log(f"复查签到状态：{'已签到' if latest_status == 1 else '未签到'}")
        summary["result"] = "签到成功" if latest_status == 1 else "已提交但复查未签到"
        return summary


async def run_husheng_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from .codebridge import get_code_for_openid, get_phone_for_openid
    from . import db

    params = params or {}
    app_id = appid or QMAI_APP_ID
    user_agent = (params.get("userAgent") or "").strip() or DEFAULT_USER_AGENT
    date_text = str(params.get("date") or "").strip() or today_text()
    dry_run = str(params.get("dryRun") or "").strip().lower() in ("1", "true", "yes", "on")
    force = str(params.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
    channel_code = (params.get("channelCode") or "").strip() or DEFAULT_CHANNEL_CODE
    try:
        flow_scene = int(params.get("flowScene") or DEFAULT_FLOW_SCENE)
    except (TypeError, ValueError):
        flow_scene = DEFAULT_FLOW_SCENE
    oa_openid = (params.get("oaOpenId") or "").strip() or DEFAULT_OA_OPENID
    allow_bind = not dry_run

    # 代理：优先本次表单填的 proxyUrl；留空回退到该微信账号绑定的地区代理，业务接口与取码同地区出网防风控。
    _acc = db.query_one("SELECT proxy_url, nickname FROM accounts WHERE openid=?", (openid,))
    from .shortproxy import project_proxy
    proxy_url = project_proxy(params, openid)
    nickname = (_acc["nickname"] if _acc and _acc["nickname"] else "")
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    runner = HushengRunner(
        proxies, user_agent=user_agent, timeout=int(params.get("timeout") or 20),
        channel_code=channel_code, flow_scene=flow_scene, oa_openid=oa_openid,
    )

    # 1. 取 wx.login code
    cr = await get_code_for_openid(openid, app_id)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "取沪上阿姨小程序 code 失败"}

    account_label = nickname or "-"
    try:
        # 2. qmai 登录拿会员 id
        login_data = await asyncio.to_thread(runner.qmai_mini_login, cr["code"])
        user = login_data.get("user") or {}
        customer_id = str(user.get("id") or "")
        mobile = str(user.get("mobile") or "")
        account_label = nickname or mask_text(customer_id)
        runner.log(f"{account_label}：qmai 登录成功，会员 id={mask_text(customer_id)} 手机={format_mobile(mobile)}")

        # 3. 未绑定手机号 / 未注册 → 退出重登拿新 code，仍无手机号则用 getPhoneNumber code 绑定
        if not mobile and allow_bind:
            runner.log(f"{account_label}：当前会员未绑定手机号，尝试授权绑定", "WARNING")
            token = str(login_data.get("token") or "")
            if token:
                await asyncio.to_thread(runner.qmai_exit_login, token)
            cr2 = await get_code_for_openid(openid, app_id)
            if cr2.get("success") and cr2.get("code"):
                login_data = await asyncio.to_thread(runner.qmai_mini_login, cr2["code"])
                user = login_data.get("user") or {}
                customer_id = str(user.get("id") or "")
                mobile = str(user.get("mobile") or "")

            if not mobile:
                token = str(login_data.get("token") or "")
                pr = await get_phone_for_openid(openid, app_id)
                phone_code = pr.get("code") if pr.get("success") else ""
                if phone_code and token:
                    try:
                        bound_login = await asyncio.to_thread(runner.qmai_bind_mobile, str(phone_code), token)
                        login_data = bound_login
                        user = login_data.get("user") or {}
                        customer_id = str(user.get("id") or "")
                        mobile = str(user.get("mobile") or "")
                        runner.log(f"{account_label}：绑定成功，手机={format_mobile(mobile)}", "SUCCESS")
                    except PhoneAlreadyBoundError as exc:
                        runner.log(
                            f"{account_label}：手机号 {exc.masked_phone or '已绑定'} 已绑定在其它 qmai 会员，"
                            "改用当前会员继续尝试签到", "WARNING")
                else:
                    runner.log(f"{account_label}：未取得手机号授权 code，尝试直接以当前会员继续", "WARNING")

        if not customer_id:
            raise RuntimeError("qmai 登录后缺少会员 id，无法进入活动")
        account_label = nickname or mask_text(customer_id)

        # 4. 打开小满授权页并用回跳刷新活动 li
        auth_page_url = await asyncio.to_thread(runner.get_activity_auth_page, customer_id)
        runner.log(f"{account_label}：活动入口已返回授权页，使用授权回跳刷新活动 li")
        activity_url = await asyncio.to_thread(runner.refresh_activity_url_by_auth_redirect, auth_page_url)

        # 5. 查询并执行签到（同步 requests，丢线程里跑避免阻塞事件循环）
        sign_summary = await asyncio.to_thread(
            runner.run_sign_flow, activity_url, date_text=date_text, dry_run=dry_run, force=force,
        )
    except NotMemberError as exc:
        runner.log(f"{account_label}：{exc}（未注册会员/未绑定手机号）", "WARNING")
        return {"ok": False, "stage": "husheng", "error": str(exc)}
    except Exception as exc:
        runner.log(f"{account_label}：签到异常：{exc}", "ERROR")
        return {"ok": False, "stage": "husheng", "error": str(exc)}

    display: dict[str, Any] = {
        "account": account_label,
        "viaProxy": bool(proxy_url),
        "date": date_text,
        "sign": sign_summary,
    }
    return {
        "ok": True,
        "stage": "husheng",
        "account": account_label,
        "viaProxy": bool(proxy_url),
        "hushengResult": json.dumps(display, ensure_ascii=False, indent=2),
        # 复用内置项目的结果文本框
        "cookie": "\n".join(runner.lines) or "（无输出）",
    }
