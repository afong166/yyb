"""Built-in project: Maidong bottle-cap scan/lottery.

This adapts the standalone Maidong script to the app's built-in project model:
the user selects a bound WeChat account, the backend obtains a fresh wx.login
code through codebridge, then performs Maidong login and optional scan/lottery.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .logsink import emit as _emit
from .turing_device_token import DEFAULT_PROFILE, generate_device_token

APP_ID = os.environ.get("MAIDONG_APPID", "wxef2336428c3873d2")
B2B_BASE = os.environ.get("MAIDONG_B2B_BASE", "https://b2b.danonewaters.com.cn")
ACTIVITY_CODE = os.environ.get("MAIDONG_ACTIVITY_CODE", "UTC202603311335149717")
PACKAGE_VERSION = os.environ.get("MAIDONG_PACKAGE_VERSION", "55")
TLS_VERIFY = os.environ.get("MAIDONG_TLS_VERIFY", "1").strip().lower() not in ("0", "false", "no", "off")
DEFAULT_LATITUDE = os.environ.get("MAIDONG_LATITUDE", "26.4247216796875")
DEFAULT_LONGITUDE = os.environ.get("MAIDONG_LONGITUDE", "112.84228569878472")
DEFAULT_DELAY_BETWEEN_SCANS = float(os.environ.get("MAIDONG_DELAY_BETWEEN_SCANS", "90"))
SIGN_SECRET = "secretKeyFor1664Xcx"
TURING_STATE_DIR = Path(
    os.environ.get(
        "MAIDONG_TURING_STATE_DIR",
        str(Path(__file__).resolve().parents[1] / "data" / "turing"),
    )
)

REFERER = os.environ.get(
    "MAIDONG_REFERER",
    f"https://servicewechat.com/wxef2336428c3873d2/{PACKAGE_VERSION}/page-frame.html",
)
UA = os.environ.get(
    "MAIDONG_USER_AGENT",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.73(0x18004939) NetType/WIFI Language/zh_CN "
    "miniProgram/wxef2336428c3873d2",
)


def _mask(value: Any, left: int = 5, right: int = 5) -> str:
    text = "" if value is None else str(value)
    if len(text) <= left + right:
        return "*" * len(text)
    return f"{text[:left]}***{text[-right:]}"


def _split_sns(raw: Any) -> list[str]:
    text = str(raw or "").replace(",", "\n").replace("，", "\n").replace("&", "\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _sn_tail(sn: str) -> str:
    value = str(sn or "").rstrip("/").rsplit("/", 1)[-1]
    return f"***{value[-4:]}" if len(value) >= 4 else "***"


def _redact_values(value: Any, sensitive_values: tuple[Any, ...]) -> Any:
    """递归清理上游响应中的账号凭证、wx code、deviceToken 和完整瓶盖码。"""
    if isinstance(value, dict):
        return {key: _redact_values(item, sensitive_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_values(item, sensitive_values) for item in value]
    if not isinstance(value, str):
        return value

    text = value
    for sensitive in sensitive_values:
        secret = str(sensitive or "")
        if len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return text


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    body = data.get("data") if isinstance(data, dict) else None
    if isinstance(body, dict):
        keys = (
            "message", "errorType", "errorMessage", "activityName",
            "remainLotteryTimes", "isWin", "prizeName", "amount",
            "state", "flowId",
        )
        return {key: body.get(key) for key in keys if key in body}
    return {
        "code": data.get("code") if isinstance(data, dict) else None,
        "message": data.get("message") if isinstance(data, dict) else str(data)[:120],
        "data": body,
    }


def _prizes(records: list[dict[str, Any]]) -> list[str]:
    out = []
    for item in records or []:
        name = item.get("prizePoolItemName") or item.get("prizeName") or item.get("name")
        if name:
            out.append(str(name))
    return out


class MaidongClient:
    def __init__(self, proxy_url: str = "") -> None:
        self.base_url = B2B_BASE.rstrip("/")
        kw = {"proxy": proxy_url} if proxy_url else {}
        self.client = httpx.AsyncClient(timeout=30.0, verify=TLS_VERIFY, **kw)
        self.proxy_url = proxy_url
        self.token = ""
        self.openid = ""
        self.device_token = ""
        self.user: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    def base_headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "referer": REFERER,
            "user-agent": UA,
            "brandcode": "[object Undefined]",
        }

    def auth_headers(self) -> dict[str, str]:
        headers = self.base_headers()
        timestamp = str(int(time.time() * 1000))
        headers.update({
            "authorization": f"Bearer {self.token}",
            "openid": self.openid,
            "timeStamp": timestamp,
            "sign": hashlib.sha512(f"{timestamp}{SIGN_SECRET}".encode()).hexdigest(),
        })
        if self.device_token:
            headers["deviceToken"] = self.device_token
        return headers

    async def init_device_token(self, *, plugin_login_code: str = "", force_refresh: bool = False) -> dict[str, Any]:
        """登录拿到脉动 openId 后，通过 Turing 纯协议生成真实 deviceToken。"""
        if not self.openid:
            raise RuntimeError("Maidong openId is empty before Turing initialization")

        profile = json.loads(json.dumps(DEFAULT_PROFILE))
        profile["plugin_login_code"] = plugin_login_code
        state_name = hashlib.sha256(self.openid.encode()).hexdigest() + ".json"
        result = await asyncio.to_thread(
            generate_device_token,
            self.openid,
            state_path=TURING_STATE_DIR / state_name,
            profile=profile,
            force_refresh=force_refresh,
            proxy_url=self.proxy_url,
        )
        token = str(result.get("deviceToken") or "")
        if result.get("ret") != 0 or not token.startswith("v3:"):
            raise RuntimeError(f"Turing deviceToken invalid: source={result.get('source')} ret={result.get('ret')}")
        self.device_token = token
        return {
            "source": result.get("source"),
            "ret": result.get("ret"),
            "length": len(token),
        }

    async def request_json(self, method: str, path: str, *, params=None, json_body=None, auth: bool = True) -> dict:
        url = self.base_url + path
        if method.upper() == "GET" and params:
            url += "?" + urlencode(params)
        resp = await self.client.request(
            method,
            url,
            headers=self.auth_headers() if auth else self.base_headers(),
            json=json_body,
        )
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"{method} {path} returned non-JSON: HTTP {resp.status_code}") from exc
        if resp.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else ""
            message = data.get("message") if isinstance(data, dict) else ""
            request_secrets: list[Any] = [self.token, self.openid, self.device_token]
            for values in (params, json_body):
                if isinstance(values, dict):
                    request_secrets.extend(
                        values.get(key) for key in ("code", "sn", "scanUrl", "mobile", "openId")
                    )
            message = _redact_values(str(message or ""), tuple(request_secrets))[:300]
            raise RuntimeError(f"{method} {path} HTTP {resp.status_code}: code={code} message={message}")
        return data

    async def login(self, login_code: str, sn: str) -> dict:
        data = await self.request_json(
            "POST",
            "/api/utc-mini-program/loginApi/login",
            json_body={"code": login_code, "sn": sn},
            auth=False,
        )
        if str(data.get("code")) != "200" or not data.get("data"):
            message = _redact_values(str(data.get("message") or ""), (login_code, sn))[:300]
            raise RuntimeError(f"Maidong login failed: code={data.get('code')} message={message or '-'}")
        self.token = str(data["data"])
        extend = data.get("extend") or {}
        self.openid = str(extend.get("openId") or "")
        self.user = extend.get("user") or {}
        return data

    async def activity_info(self, activity_code: str) -> dict:
        return await self.request_json("GET", f"/api/utc-mini-program/utcProgramScan/getActivityInfo/{activity_code}")

    async def query_times(self, activity_code: str) -> dict:
        return await self.request_json(
            "GET", "/api/utc-mini-program/utcScanV2/queryActivityTimes",
            params={"activityCode": activity_code},
        )

    async def prize_records(self, activity_code: str) -> dict:
        return await self.request_json(
            "GET", "/api/utc-mini-program/activity/userPagePrizes",
            params={"activityId": activity_code, "pageNum": 0, "pageSize": 10},
        )

    async def get_cell_phone(self, phone_code: str) -> str:
        data = await self.request_json(
            "GET", "/api/utc-mini-program/loginApi/getCellPhone",
            params={"code": phone_code},
        )
        mobile = data.get("data")
        if not mobile:
            raise RuntimeError(f"Maidong getCellPhone failed: {data.get('message') or data}")
        return str(mobile)

    async def update_privacy(self, sn: str) -> dict:
        return await self.request_json("POST", "/api/utc-mini-program/loginApi/updateUserInfo", json_body={
            "openId": self.openid, "type": 1, "showPrivacy": 1, "sn": sn,
        })

    async def update_mobile(self, mobile: str, sn: str) -> dict:
        return await self.request_json("POST", "/api/utc-mini-program/loginApi/updateUserInfo", json_body={
            "mobile": mobile, "type": 2, "openId": self.openid, "sn": sn,
        })

    async def update_location(self, latitude: str, longitude: str, sn: str) -> dict:
        return await self.request_json("POST", "/api/utc-mini-program/loginApi/updateUserInfo", json_body={
            "openId": self.openid, "type": 3, "latitude": latitude, "longitude": longitude, "sn": sn,
        })

    async def scan_log(self, sn: str) -> dict:
        return await self.request_json("POST", "/api/utc-mini-program/loginApi/userScanLog", json_body={
            "openId": self.openid, "userId": self.user.get("id"), "scanUrl": sn,
        })

    async def pre_validator(self, sn: str) -> dict:
        return await self.request_json("POST", "/api/utc-mini-program/utcProgramScan/utcPreValidator", json_body={
            "openId": self.openid, "sn": sn,
        })

    async def scan_code(self, sn: str, activity_code: str, latitude: str, longitude: str) -> dict:
        return await self.request_json("POST", "/api/utc-mini-program/utcScanV2/scanCode", json_body={
            "sn": sn, "latitude": latitude, "longitude": longitude, "activityCode": activity_code,
        })

    async def lottery(self, activity_code: str) -> dict:
        return await self.request_json("POST", "/api/utc-mini-program/utcScanV2/lottery", json_body={
            "activityCode": activity_code,
        })


def _bool_param(params: dict, key: str, default: bool) -> bool:
    if key not in params:
        return default
    value = params.get(key)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _float_param(params: dict, key: str, default: float) -> float:
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


async def _prepare_user(client: MaidongClient, openid: str, appid: str, sn: str,
                        latitude: str, longitude: str) -> dict:
    from .codebridge import get_phone_for_openid

    await client.update_privacy(sn)
    phone_result = await get_phone_for_openid(openid, appid)
    if not phone_result.get("success"):
        raise RuntimeError(phone_result.get("error") or "failed to get phone auth")
    phone_code = phone_result.get("code") or ""
    mobile = phone_result.get("mobile") or ""
    if phone_code:
        mobile = await client.get_cell_phone(str(phone_code))
    if not mobile:
        raise RuntimeError(phone_result.get("error") or "failed to get phone/mobile")
    await client.update_mobile(mobile, sn)
    await client.update_location(latitude, longitude, sn)
    return {"prepareUser": "ok", "mobile": _mask(mobile, 3, 4)}


def _needs_phone_auth(item: dict[str, Any]) -> bool:
    scan = item.get("scan") or {}
    message = str(scan.get("message") or scan.get("errorMessage") or scan.get("data") or "")
    code = str(scan.get("code") or "")
    # 注意运算符优先级：需要 (含关键词) and (命中错误码)，否则 `A or B and C` 会解析成 `A or (B and C)`，
    # 导致任何含「授权手机号」的消息无视 code 直接触发补授权。
    return ("授权手机号" in message or "手机号" in message) and code in ("50000", "500", "401")


async def _run_one_sn(client: MaidongClient, sn: str, activity_code: str,
                      latitude: str, longitude: str, do_lottery: bool) -> dict:
    item: dict[str, Any] = {"snTail": _sn_tail(sn)}
    try:
        item["timesBefore"] = (await client.query_times(activity_code)).get("data")
    except Exception as exc:
        item["timesBeforeError"] = str(exc)

    for key, func in (
        ("scanLog", client.scan_log),
        ("preValidator", client.pre_validator),
    ):
        try:
            item[key] = _summary(await func(sn))
        except Exception as exc:
            item[f"{key}Error"] = str(exc)

    scan = await client.scan_code(sn, activity_code, latitude, longitude)
    item["scan"] = _summary(scan)

    after_scan = (await client.query_times(activity_code)).get("data")
    item["timesAfterScan"] = after_scan
    remain = 0
    if isinstance(after_scan, dict):
        remain = int(after_scan.get("remainTimes") or 0)
    if do_lottery and remain > 0:
        item["lottery"] = _summary(await client.lottery(activity_code))
        item["timesAfterLottery"] = (await client.query_times(activity_code)).get("data")
    elif do_lottery:
        item["lottery"] = {"skipped": "remainTimes<=0"}

    prizes = (await client.prize_records(activity_code)).get("data") or {}
    item["prizeTotal"] = prizes.get("total", 0)
    item["prizes"] = _prizes(prizes.get("records") or [])
    sn_value = str(sn or "").rstrip("/").rsplit("/", 1)[-1]
    return _redact_values(item, (sn, sn_value))


def _item_lines(item: dict[str, Any]) -> list[str]:
    """单个瓶盖码的扫码 / 抽奖 / 中奖 结果 → [LEVEL] 日志行。"""
    idx = item.get("snIndex")
    head = f"码{idx} {item.get('snTail', '')}".strip() if idx else (item.get("snTail") or "")
    out: list[str] = []
    scan = item.get("scan") or {}
    if item.get("scanError"):
        out.append(f"[ERROR] {head}：扫码异常 {item['scanError']}")
    else:
        smsg = scan.get("message") or scan.get("errorMessage") or scan.get("errorType") or ""
        if scan.get("errorMessage") or scan.get("errorType"):
            out.append(f"[WARNING] {head}：{smsg or '扫码未通过'}")
        else:
            out.append(f"[SUCCESS] {head}：{smsg or '扫码完成'}")
    lot = item.get("lottery")
    if isinstance(lot, dict):
        if lot.get("skipped"):
            out.append(f"[INFO] {head}：无剩余次数，跳过抽奖")
        else:
            lmsg = lot.get("prizeName") or lot.get("message") or ("中奖" if lot.get("isWin") else "未中奖")
            lvl = "SUCCESS" if (lot.get("isWin") or lot.get("prizeName")) else "INFO"
            out.append(f"[{lvl}] {head} 抽奖：{lmsg}")
    if item.get("prizes"):
        out.append(f"[INFO] {head} 已中奖品（{item.get('prizeTotal', 0)}）：{'、'.join(item['prizes'])}")
    return out


async def run_maidong_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from .codebridge import get_code_for_openid
    from . import db

    params = params or {}
    target_appid = params.get("appid") or appid or APP_ID
    sns = _split_sns(params.get("sn") or params.get("scanUrl") or params.get("value") or params.get("defaultSn"))
    if not sns:
        return {"ok": False, "stage": "params", "error": "please input Maidong bottle-cap URL/SN"}

    activity_code = params.get("activityCode") or ACTIVITY_CODE
    latitude = str(params.get("latitude") or DEFAULT_LATITUDE)
    longitude = str(params.get("longitude") or DEFAULT_LONGITUDE)
    do_scan = _bool_param(params, "scan", True)
    do_lottery = _bool_param(params, "lottery", True)
    prepare_user = _bool_param(params, "prepareUser", False)
    auto_prepare_user = _bool_param(params, "autoPrepareUser", True)
    delay_between_scans = _float_param(params, "delayBetweenScans", DEFAULT_DELAY_BETWEEN_SCANS)
    # 代理：优先本次表单填写的 proxyUrl；留空则回退到该微信账号扫码时绑定的地区代理，
    # 让脉动 danone B2B 接口与账号同地区出网，避免异地 IP 触发风控。
    _acc = db.query_one("SELECT proxy_url FROM accounts WHERE openid=?", (openid,))
    from .shortproxy import project_proxy
    proxy_url = project_proxy(params, openid)

    cr = await get_code_for_openid(openid, target_appid)
    if not cr.get("success") or not cr.get("code"):
        error = _redact_values(str(cr.get("error") or "failed to get Maidong code"), (openid,))
        return {"ok": False, "stage": "get-code", "error": error}

    client = MaidongClient(proxy_url=proxy_url)
    try:
        login = await client.login(cr["code"], sns[0])
        turing = await client.init_device_token(
            plugin_login_code=str(params.get("pluginLoginCode") or os.environ.get("MAIDONG_PLUGIN_LOGIN_CODE", "")),
            force_refresh=_bool_param(params, "refreshDeviceToken", False),
        )
        user = (login.get("extend") or {}).get("user") or {}
        result: dict[str, Any] = {
            "ok": True,
            "stage": "maidong",
            "snCount": len(sns),
            "delayBetweenScans": delay_between_scans if len(sns) > 1 else 0,
            "viaProxy": bool(proxy_url),
            "deviceTokenSource": turing["source"],
            "deviceTokenLength": turing["length"],
            "account": _mask(user.get("id") or client.openid),
            "openid": _mask(client.openid),
            "userId": _mask(user.get("id")),
            "mobileBound": bool(user.get("mobile")),
            "results": [],
        }
        phone_prepared = False
        if prepare_user:
            result.update(await _prepare_user(client, openid, target_appid, sns[0], latitude, longitude))
            phone_prepared = True

        try:
            activity = await client.activity_info(activity_code)
            if isinstance(activity.get("data"), dict):
                result["activityName"] = activity["data"].get("configName") or activity["data"].get("topicName")
        except Exception as exc:
            result["activityError"] = str(exc)

        # 实时日志：边跑边推（前端可实时看到扫码进度与防风控等待）
        run_lines: list[str] = []

        def rlog(line: str) -> None:
            run_lines.append(line)
            _emit(line)

        rlog(f"[INFO] ▶ 账号：{result.get('account') or '-'}")
        rlog(
            f"[SUCCESS] Turing deviceToken 已生成"
            f"（来源 {result['deviceTokenSource']}，长度 {result['deviceTokenLength']}）"
        )
        if result.get("activityName"):
            rlog(f"[INFO] 活动：{result['activityName']}")
        if result.get("prepareUser") == "ok":
            rlog(f"[SUCCESS] 已完成隐私 / 手机号 / 定位授权（{result.get('mobile', '')}）")

        if do_scan:
            for index, sn in enumerate(sns, 1):
                if index > 1 and delay_between_scans > 0:
                    rlog(f"[INFO] 防风控间隔，等待 {delay_between_scans:.0f} 秒…")
                    await asyncio.sleep(delay_between_scans)
                rlog(f"[INFO] 开始扫码 码{index} {_sn_tail(sn)}")
                item = await _run_one_sn(client, sn, activity_code, latitude, longitude, do_lottery)
                item["snIndex"] = index
                if index > 1 and delay_between_scans > 0:
                    item["delayBeforeScan"] = delay_between_scans
                if _needs_phone_auth(item) and auto_prepare_user:
                    item["phoneAuthRetry"] = "triggered"
                    rlog(f"[WARNING] 码{index} 需授权手机号，正在补授权后重试…")
                    try:
                        if not phone_prepared:
                            result.update(await _prepare_user(client, openid, target_appid, sn, latitude, longitude))
                            phone_prepared = True
                        item = await _run_one_sn(client, sn, activity_code, latitude, longitude, do_lottery)
                        item["retriedAfterPhoneAuth"] = True
                    except Exception as exc:
                        item["phoneAuthError"] = _redact_values(
                            str(exc),
                            (openid, client.openid, client.token, client.device_token, sn),
                        )
                for ln in _item_lines(item):
                    rlog(ln)
                result["results"].append(item)
        else:
            result["times"] = (await client.query_times(activity_code)).get("data")
            prizes = (await client.prize_records(activity_code)).get("data") or {}
            result["prizeTotal"] = prizes.get("total", 0)
            result["prizes"] = _prizes(prizes.get("records") or [])
            if result.get("times") is not None:
                rlog(f"[INFO] 剩余次数：{result['times']}")
            if result.get("prizes"):
                rlog(f"[INFO] 已中奖品（{result.get('prizeTotal', 0)}）：{'、'.join(result['prizes'])}")

        rlog("[INFO] 运行结束")
        display = {
            "account": result.get("account"),
            "activityName": result.get("activityName"),
            "snCount": result.get("snCount"),
            "viaProxy": result.get("viaProxy"),
            "results": result.get("results"),
        }
        result["maidongResult"] = json.dumps(display, ensure_ascii=False, indent=2)
        # 复用内置项目结果文本框：统一成 [LEVEL] 日志行（与其他项目一致）
        result["cookie"] = "\n".join(run_lines)
        return _redact_values(
            result,
            (openid, cr.get("code"), client.openid, client.token, client.device_token, *sns),
        )
    except Exception as exc:
        sensitive_values = (
            openid,
            proxy_url,
            cr.get("code"),
            client.openid,
            client.token,
            client.device_token,
            *sns,
        )
        return {
            "ok": False,
            "stage": "maidong",
            "error": _redact_values(str(exc), sensitive_values),
        }
    finally:
        await client.aclose()
