"""Built-in project: 农夫山泉开盖赢奖 (bottle-cap draw).

This adapts the standalone 农夫山泉开盖赢奖 script to the app's built-in project
model: the user selects a bound WeChat account, the backend obtains fresh
wx.login codes through codebridge, then runs the activity getOpenId + drawPrize
main chain using the mini-program activity sub-package's AES/MD5 `param`
algorithm (copied byte-for-byte from the source script).

Notes preserved from source:
  - 默认 --skip-mall-login：仅走活动 getOpenId，跳过商城 thirdparty/login。
  - drawPrize/myPrize/authorizeStatus/getOpenId 使用活动子包 AES/MD5 param。
  - deviceToken 来自腾讯风控插件；缺省 ""，服务端风控严格时可能需要传入。
  - 每次 getOpenId 需要一枚全新的 wx.login code（一次性）。
  - 中奖记录 / 一元换购二维码：不落盘，只在结果与日志给出信息。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from typing import Any

try:
    import requests
except ImportError as _exc:  # pragma: no cover
    raise SystemExit("缺少 requests，请先执行：pip install requests") from _exc

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .logsink import emit as _emit


# ───────────────────────── 常量（逐字节照抄源脚本） ─────────────────────────
APPID = "wx3723729dc4ac916c"
APP_TYPE = 6
ACTIVITY_CODE = os.getenv("NFSQ_ACTIVITY_CODE", "S1")

API_MALL_BASE = "https://api-mall.yst.com.cn"
CRM_BASE = "https://crm-app.yst.com.cn/api-b2b-market-draw-front/consumer"
REFERER = f"https://servicewechat.com/{APPID}/168/page-frame.html"
DEFAULT_SIGN_KEY = "RaJ3EvQLCqhIE5In4CTIfA=="
DEFAULT_AES_KEY_B64 = "SJyooRdCFtqVz2aLUDNz9A=="
DEFAULT_LATITUDE = os.getenv("NFSQ_LATITUDE") or os.getenv("YST_LATITUDE") or "32.910069444444446"
DEFAULT_LONGITUDE = os.getenv("NFSQ_LONGITUDE") or os.getenv("YST_LONGITUDE") or "117.43306206597222"
KEY_INSERT_POSITIONS = (1, 3, 5)
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 15; Mi 10 Pro Build/AQ3A.250226.002; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/146.0.7680.178 Mobile Safari/537.36 XWEB/1460065 "
    "MMWEBSDK/20250604 MMWEBID/5366 MicroMessenger/8.0.75.2840(0x28004B35) "
    "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)

敏感键 = re.compile(
    r"(token|authorization|cookie|session|openid|unionid|phone|mobile|authcode|param|password|secret|code)",
    re.I,
)


# ───────────────────────── 通用工具 / 脱敏（照抄源脚本） ─────────────────────────
def 当前毫秒() -> int:
    return int(time.time() * 1000)


def 脱敏(value: Any, keep: int = 5) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return text[:keep] + "..." + text[-keep:]


def 脱敏手机号(text: Any) -> str:
    return re.sub(r"(1\d{2})\d{4}(\d{4})", r"\1****\2", str(text or ""))


def 脱敏对象(obj: Any) -> Any:
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            if 敏感键.search(str(key)):
                result[key] = 脱敏(value)
            else:
                result[key] = 脱敏对象(value)
        return result
    if isinstance(obj, list):
        return [脱敏对象(item) for item in obj]
    if isinstance(obj, str):
        return 脱敏手机号(obj)
    return obj


def 分割条目(raw: Any) -> list[str]:
    if not raw:
        return []
    text = str(raw).replace("\r", "\n")
    for sep in (",", "，", "&", ";", "；"):
        text = text.replace(sep, "\n")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


# ───────────────────────── 瓶盖码解析（照抄源脚本） ─────────────────────────
def 规范瓶盖码(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    match = re.search(r"NFSQ\.CC/([A-Z0-9]+)/([A-Z0-9]+)", text, re.I)
    if match:
        return f"{match.group(1).upper()}/{match.group(2).upper()}"
    match = re.search(r"\b(S\d+)/([A-Z0-9]{8,})\b", text, re.I)
    if match:
        return f"{match.group(1).upper()}/{match.group(2).upper()}"
    tail = text.rstrip("/").split("/")[-1].strip()
    if re.fullmatch(r"[A-Z0-9]{8,}", tail, re.I):
        return f"{ACTIVITY_CODE}/{tail.upper()}"
    return text


def 瓶盖码_url(code: str) -> str:
    if code.upper().startswith("HTTP"):
        return code
    return "HTTPS://NFSQ.CC/" + code.strip("/")


def 读取瓶盖码内联(inline_codes: str) -> list[str]:
    """仅从 params 内联文本读取瓶盖码（不读 .txt/HAR）。"""
    seen: set[str] = set()
    codes: list[str] = []
    for item in 分割条目(inline_codes):
        code = 规范瓶盖码(item)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def 经纬度可用(latitude: str, longitude: str) -> bool:
    try:
        lat = float(str(latitude).strip())
        lng = float(str(longitude).strip())
    except Exception:
        return False
    return -90 <= lat <= 90 and -180 <= lng <= 180


# ───────────────────────── 结果语义判定（照抄源脚本） ─────────────────────────
def 接口真值(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "是")
    return False


def 限制文本(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    data = obj.get("data")
    texts: list[str] = []
    if isinstance(data, dict):
        for key in ("notHitPrizeReasonMsg", "message", "msg", "rewardName", "rewardLevelName"):
            value = data.get(key)
            if value:
                texts.append(str(value))
    for key in ("message", "msg", "messageTitle"):
        value = obj.get(key)
        if value:
            texts.append(str(value))
    return " ".join(texts)


def 是否限制结果(obj: Any) -> bool:
    if isinstance(obj, dict):
        data = obj.get("data")
        if 接口真值(obj.get("dayCountLimitFlag")):
            return True
        if isinstance(data, dict) and 接口真值(data.get("dayCountLimitFlag")):
            return True
    text = 限制文本(obj)
    if not text:
        return False
    keywords = (
        "今日抽奖已达上限",
        "今日已达上限",
        "今日已上限",
        "每日限制",
        "次数已达上限",
        "次数已用完",
        "机会已用完",
        "没有机会",
        "不可再参加",
        "限制",
        "上限",
    )
    return any(keyword in text for keyword in keywords)


def 是否已扫过结果(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    data = obj.get("data")
    texts: list[str] = []
    if isinstance(data, dict):
        if data.get("codeUsed"):
            return True
        for key in ("notHitPrizeReasonMsg", "message", "msg", "rewardName", "rewardLevelName"):
            value = data.get(key)
            if value:
                texts.append(str(value))
    for key in ("message", "msg", "messageTitle"):
        value = obj.get(key)
        if value:
            texts.append(str(value))
    text = " ".join(texts)
    return any(keyword in text for keyword in ("该码已被扫过", "已被扫过", "已扫过", "已使用", "二维码已使用"))


def 是否未中奖结果(obj: Any) -> bool:
    if not isinstance(obj, dict) or 是否限制结果(obj):
        return False
    data = obj.get("data")
    texts: list[str] = []
    if isinstance(data, dict):
        if str(data.get("rewardType") or "") == "noSurprise":
            return True
        for key in ("notHitPrizeReasonMsg", "message", "msg", "rewardName", "rewardLevelName"):
            value = data.get(key)
            if value:
                texts.append(str(value))
    for key in ("message", "msg", "messageTitle"):
        value = obj.get(key)
        if value:
            texts.append(str(value))
    text = " ".join(texts)
    return any(keyword in text for keyword in ("未中奖", "谢谢惠顾", "好运下次", "下次再来", "下次就来"))


def 结果摘要(obj: Any) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    data = obj.get("data")
    msg = obj.get("message") or obj.get("msg") or ""
    if isinstance(data, dict):
        if 接口真值(data.get("dayCountLimitFlag")):
            reward_type = str(data.get("rewardType") or "")
            parts = ["已达今日扫码/抽奖上限"]
            if reward_type:
                parts.append(f"接口类型={reward_type}")
            if data.get("notHitPrizeReasonMsg"):
                parts.append(str(data.get("notHitPrizeReasonMsg")))
            return "；".join(parts)
        reward_type = str(data.get("rewardType") or "")
        if reward_type:
            reward_names = {
                "noSurprise": "未中奖",
                "redPacket": "红包",
                "bankPayment": "商家转账红包",
                "exchange": "兑换奖",
            }
            status_names = {
                0: "未兑换",
                1: "已兑换",
                2: "处理中",
                3: "已过期",
                4: "支付异常",
                5: "券冻结",
                6: "券过期",
            }
            title = reward_names.get(reward_type, reward_type)
            prize_name = data.get("rewardName") or data.get("rewardLevelName")
            parts = [f"{title}({reward_type})"]
            if prize_name:
                parts.append(str(prize_name))
            if data.get("rewardLevelName") and data.get("rewardLevelName") != prize_name:
                parts.append(str(data.get("rewardLevelName")))
            if data.get("winnerAmountYuanStr"):
                parts.append(str(data.get("winnerAmountYuanStr")))
            elif data.get("winnerAmount"):
                parts.append(str(data.get("winnerAmount")) + "元")
            if data.get("notHitPrizeReasonMsg"):
                parts.append(str(data.get("notHitPrizeReasonMsg")))
            if reward_type != "noSurprise":
                if data.get("exchangeStatusName"):
                    parts.append("状态=" + str(data.get("exchangeStatusName")))
                elif "exchangeStatus" in data and data.get("exchangeStatus") is not None:
                    parts.append("状态=" + status_names.get(data.get("exchangeStatus"), str(data.get("exchangeStatus"))))
            if data.get("expireTimeCn"):
                parts.append("有效期至=" + str(data.get("expireTimeCn")))
            if data.get("customerName"):
                parts.append("门店=" + str(data.get("customerName")))
            if data.get("codeUsed"):
                parts.append("该码已被扫过")
            return "；".join(parts)
        for key in ("rewardName", "notHitPrizeReasonMsg", "message", "rewardLevelName"):
            if data.get(key):
                return str(data.get(key))
        return json.dumps(脱敏对象(data), ensure_ascii=False)[:180]
    if isinstance(data, list):
        return f"{len(data)} 条记录"
    return msg or json.dumps(脱敏对象(obj), ensure_ascii=False)[:180]


def 奖品列表(records_obj: Any) -> list[dict[str, Any]]:
    data = records_obj.get("data") if isinstance(records_obj, dict) else records_obj
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def 查找奖品记录(records_obj: Any, bottle_code: str, fallback_latest: bool = False) -> dict[str, Any] | None:
    data = records_obj.get("data") if isinstance(records_obj, dict) else records_obj
    if not isinstance(data, list):
        return None
    target = 规范瓶盖码(bottle_code)
    for item in data:
        if not isinstance(item, dict):
            continue
        scan_code = str(item.get("scanCode") or "")
        if scan_code and 规范瓶盖码(scan_code) == target:
            return item
    if not fallback_latest:
        return None
    candidates = [item for item in data if isinstance(item, dict) and item.get("rewardTime")]
    if candidates:
        return sorted(candidates, key=lambda x: x.get("rewardTime") or 0, reverse=True)[0]
    return data[0] if data and isinstance(data[0], dict) else None


def 是否中奖记录(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    reward_type = str(item.get("rewardType") or "")
    return bool(reward_type and reward_type != "noSurprise")


def 是否红包记录(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    return str(item.get("rewardType") or "") in ("redPacket", "bankPayment")


def 是否一元换购(item: dict[str, Any] | None) -> bool:
    if not item or str(item.get("rewardType") or "") != "exchange":
        return False
    text = " ".join(
        str(item.get(key) or "")
        for key in ("rewardName", "rewardLevelName", "notHitPrizeReasonMsg", "message")
    )
    is_exchange_prize = any(keyword in text for keyword in ("壹元换购", "一元换购", "再来一瓶", "再来1瓶"))
    status_name = str(item.get("exchangeStatusName") or "")
    status_code = item.get("exchangeStatus")
    is_unredeemed = status_name == "未兑换" or status_code == 0 or status_code == "0"
    return is_exchange_prize and is_unredeemed


def 今日日期文本() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def 今日记录数(records_obj: Any) -> int:
    today = 今日日期文本()
    count = 0
    for item in 奖品列表(records_obj):
        reward_time = str(item.get("rewardTimeCn") or item.get("firstScanTimeCn") or "")
        if reward_time.startswith(today):
            count += 1
    return count


# ───────────────────────── param 加密算法（逐字节照抄源脚本） ─────────────────────────
def 清洗_key(value: str, positions: tuple[int, ...] = KEY_INSERT_POSITIONS) -> str:
    text = str(value or "")
    if not text or not positions:
        return text
    result: list[str] = []
    pos_idx = 0
    for idx, char in enumerate(text):
        if pos_idx < len(positions) and idx == pos_idx + positions[pos_idx]:
            pos_idx += 1
            continue
        result.append(char)
    return "".join(result)


def 排序过滤字段(data: dict[str, Any]) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for key, value in data.items():
        if value is None or value == "":
            continue
        filtered[str(key)] = str(value)
    return {key: filtered[key] for key in sorted(filtered)}


def md5_sign(data: dict[str, str], sign_key: str) -> str:
    sign_str = "&".join(f"{key}={value}" for key, value in data.items()) + "&key=" + sign_key
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()


def aes_ecb_encrypt_json(payload: dict[str, Any], aes_key_b64: str) -> str:
    try:
        key = base64.b64decode(aes_key_b64)
    except Exception as exc:
        raise RuntimeError("AES key 不是合法 Base64") from exc
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    cipher = AES.new(key, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(raw, AES.block_size))).decode("ascii")


def 生成活动_param(
    data: dict[str, Any],
    *,
    openid: str = "",
    sign_key: str = DEFAULT_SIGN_KEY,
    aes_key_b64: str = DEFAULT_AES_KEY_B64,
    time_millis: int | None = None,
) -> str:
    merged = {
        **data,
        "timeMillis": int(time_millis if time_millis is not None else 当前毫秒()),
        "openId": openid or data.get("openId") or "",
    }
    ordered = 排序过滤字段(merged)
    signed = {**ordered, "sign": md5_sign(ordered, sign_key)}
    return aes_ecb_encrypt_json(signed, aes_key_b64)


def 生成securityParam(
    data: dict[str, Any],
    *,
    openid: str,
    key_a: str,
    key_b: str,
    time_millis: int | None = None,
) -> str:
    sign_key = 清洗_key(key_a)
    aes_key = 清洗_key(key_b)
    if not sign_key or not aes_key:
        raise RuntimeError("缺少 keyA/keyB，无法生成 drawPrize 的 securityParam")
    return 生成活动_param(data, openid=openid, sign_key=sign_key, aes_key_b64=aes_key, time_millis=time_millis)


def 生成_param(action: str, context: dict[str, Any]) -> str:
    activity = context.get("activityCode") or ACTIVITY_CODE
    openid = str(context.get("openId") or "")
    timestamp = int(context.get("timestamp") or 当前毫秒())

    if action in ("myPrize", "authorizeStatus"):
        return 生成活动_param({"activityType": activity}, openid=openid, time_millis=timestamp)

    if action in ("getOpenId", "getOpenIdSimple"):
        data = {
            "activityType": activity,
            "code": context.get("wxCode") or "",
            "scanCode": context.get("scanUrl") or "",
        }
        return 生成活动_param(data, openid=openid, time_millis=timestamp)

    if action == "drawPrize":
        inner_data = {
            "scanCode": context.get("scanUrl") or "",
            "latitude": context.get("latitude") or "",
            "longitude": context.get("longitude") or "",
            "unionId": context.get("unionId") or "",
            "deviceToken": context.get("deviceToken") or "",
        }
        security_param = 生成securityParam(
            inner_data,
            openid=openid,
            key_a=str(context.get("keyA") or ""),
            key_b=str(context.get("keyB") or ""),
            time_millis=timestamp,
        )
        outer_data = {"activityType": activity, "openId": openid, "securityParam": security_param}
        return 生成活动_param(outer_data, openid=openid, time_millis=timestamp)

    raise RuntimeError(f"未知 param action：{action}")


# ───────────────────────── 同步 HTTP 客户端（照抄源脚本请求逻辑） ─────────────────────────
class NongfuRunner:
    def __init__(self, *, proxy: str, activity: str, latitude: str, longitude: str,
                 device_token: str, timeout: int = 20) -> None:
        self.activity = activity
        self.latitude = latitude
        self.longitude = longitude
        self.device_token = device_token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "content-type": "application/json",
                "charset": "utf-8",
                "Referer": REFERER,
                "User-Agent": USER_AGENT,
            }
        )
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.token = ""
        self.openid = ""
        self.unionid = ""
        self.key_a = ""
        self.key_b = ""
        self.当前活动瓶盖码 = ""

    def headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "charset": "utf-8",
            "Referer": REFERER,
            "User-Agent": USER_AGENT,
        }
        use_token = token if token is not None else self.token
        if use_token:
            headers["X-AUTH-TOKEN"] = use_token
        return headers

    def 请求(self, method: str, url: str, *, json_body: Any = None, params: dict[str, Any] | None = None) -> Any:
        resp = self.session.request(
            method,
            url,
            headers=self.headers(),
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        try:
            obj = resp.json()
        except Exception as exc:
            raise RuntimeError(f"接口返回非 JSON：HTTP {resp.status_code} {resp.text[:160]}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}：{脱敏对象(obj)}")
        return obj

    def 活动_getOpenId(self, wx_code: str, bottle_code: str = "", simple: bool = False) -> dict[str, Any]:
        action = "getOpenIdSimple" if simple else "getOpenId"
        context = self.param_context(action, bottle_code=bottle_code)
        context["wxCode"] = wx_code
        param = 生成_param(action, context)
        url = f"{CRM_BASE}/{'getOpenIdSimple' if simple else 'getOpenId'}"
        obj = self.请求("POST", url, json_body={"param": param})
        data = obj.get("data") if isinstance(obj, dict) else {}
        if not isinstance(data, dict):
            data = {}
        self.openid = str(data.get("openId") or data.get("openid") or self.openid or "")
        self.unionid = str(data.get("unionId") or data.get("unionid") or self.unionid or "")
        self.key_a = str(data.get("a") or data.get("keyA") or self.key_a or "")
        self.key_b = str(data.get("b") or data.get("keyB") or self.key_b or "")
        return data

    def 检查活动时间(self) -> Any:
        url = f"{CRM_BASE}/checkActivityTime/{self.activity}"
        return self.请求("GET", url)

    def 活动配置(self) -> Any:
        if not self.openid:
            return {"skipped": True, "reason": "缺少 openId"}
        url = f"{CRM_BASE}/config/{self.activity}"
        return self.请求("GET", url, params={"openId": self.openid})

    def 查询投票奖品(self) -> Any:
        if not self.openid:
            return {"skipped": True, "reason": "缺少 openId"}
        url = f"{CRM_BASE}/vote/myPrize"
        return self.请求("POST", url, json_body={"openId": self.openid})

    def 查询我的奖品(self) -> Any:
        context = self.param_context("myPrize")
        param = 生成_param("myPrize", context)
        url = f"{CRM_BASE}/myPrize"
        return self.请求("POST", url, json_body={"param": param})

    def 授权状态(self) -> Any:
        context = self.param_context("authorizeStatus")
        param = 生成_param("authorizeStatus", context)
        url = f"{CRM_BASE}/authorizeStatus"
        return self.请求("POST", url, json_body={"param": param})

    def 抽奖(self, bottle_code: str) -> Any:
        context = self.param_context("drawPrize", bottle_code=bottle_code)
        param = 生成_param("drawPrize", context)
        url = f"{CRM_BASE}/drawPrize"
        return self.请求("POST", url, json_body={"param": param})

    def param_context(self, action: str, bottle_code: str = "") -> dict[str, Any]:
        code_url = 瓶盖码_url(bottle_code) if bottle_code else ""
        return {
            "action": action,
            "appId": APPID,
            "appType": APP_TYPE,
            "activityCode": self.activity,
            "openId": self.openid,
            "unionId": self.unionid,
            "keyA": self.key_a,
            "keyB": self.key_b,
            "deviceToken": self.device_token,
            "sessionId": self.token,
            "bottleCode": bottle_code,
            "scanUrl": code_url,
            "timestamp": 当前毫秒(),
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


def _int_param(params: dict, key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_param(params: dict, key: str, default: float) -> float:
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


async def run_nongfu_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    """内置项目入口：农夫山泉开盖赢奖。用户选定绑定微信账号 → codebridge 取 wx.login code
    → 活动 getOpenId 拿身份/keyA/keyB → drawPrize 抽奖 → myPrize 奖品记录。"""
    from .codebridge import get_code_for_openid
    from . import db

    params = params or {}
    target_appid = params.get("appid") or appid or APPID

    codes = 读取瓶盖码内联(params.get("codes") or params.get("sn") or params.get("value") or "")
    if not codes:
        return {"ok": False, "stage": "params", "error": "请输入农夫山泉瓶盖码"}

    activity = str(params.get("activity") or params.get("activityCode") or ACTIVITY_CODE)
    latitude = str(params.get("latitude") or DEFAULT_LATITUDE)
    longitude = str(params.get("longitude") or DEFAULT_LONGITUDE)
    device_token = str(params.get("deviceToken") or "")
    timeout = _int_param(params, "timeout", 20)
    interval = _float_param(params, "interval", 0.8)
    daily_limit = max(_int_param(params, "dailyLimit", 5), 0)

    if not 经纬度可用(latitude, longitude):
        return {"ok": False, "stage": "params", "error": "经纬度无效，请传 latitude / longitude"}

    # 代理：优先本次表单填写的 proxyUrl；留空则回退该微信账号扫码时绑定的地区代理，
    # 让农夫山泉 yst 接口与账号同地区出网，避免异地 IP 触发风控。
    _acc = db.query_one("SELECT proxy_url FROM accounts WHERE openid=?", (openid,))
    proxy_url = (params.get("proxyUrl") or "").strip() or ((_acc["proxy_url"] or "") if _acc else "")

    # 先取一枚 code（校验账号可用），并作为活动身份种子 wx.login code。
    cr = await get_code_for_openid(openid, target_appid)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "取码失败"}

    async def _fresh_wx_code() -> str:
        r = await get_code_for_openid(openid, target_appid)
        if not r.get("success") or not r.get("code"):
            raise RuntimeError(r.get("error") or "取码失败")
        return str(r["code"])

    runner = NongfuRunner(
        proxy=proxy_url, activity=activity, latitude=latitude, longitude=longitude,
        device_token=device_token, timeout=timeout,
    )

    run_lines: list[str] = []

    def rlog(line: str) -> None:
        run_lines.append(line)
        _emit(line)

    async def 活动身份(bottle_code: str = "", *, force: bool = False, preset_code: str = "") -> dict[str, Any]:
        normalized = 规范瓶盖码(bottle_code) if bottle_code else ""
        if (not force and runner.当前活动瓶盖码 == normalized
                and runner.openid and runner.key_a and runner.key_b):
            return {}
        wx_code = preset_code or await _fresh_wx_code()
        data = await asyncio.to_thread(runner.活动_getOpenId, wx_code, bottle_code)
        runner.当前活动瓶盖码 = normalized
        return data

    try:
        seed_code = codes[0]
        rlog(f"[INFO] ▶ 农夫山泉开盖赢奖，账号：{脱敏(openid)}")
        rlog(f"[INFO] 瓶盖码 {len(codes)} 个；活动 {activity}")
        if not device_token:
            rlog("[WARNING] 未传 deviceToken（腾讯风控插件产物）；服务端风控严格时抽奖可能被拦，可在参数传入")

        # 活动身份（getOpenId）：拿 openId / unionId / keyA / keyB
        await 活动身份(seed_code, force=True, preset_code=str(cr["code"]))
        if not (runner.openid and runner.key_a and runner.key_b):
            raise RuntimeError("活动 getOpenId 未返回完整身份（openId/keyA/keyB），无法抽奖")
        rlog(f"[SUCCESS] 活动身份就绪：openId={脱敏(runner.openid)} keyA={'有' if runner.key_a else '无'} "
             f"keyB={'有' if runner.key_b else '无'}")

        activity_name = ""
        # 预检（仅日志展示，不影响主链路）
        try:
            rlog(f"[INFO] 活动时间：{结果摘要(runner.检查活动时间())}")
        except Exception as exc:
            rlog(f"[WARNING] 活动时间查询失败：{脱敏手机号(str(exc))}")
        try:
            cfg = await asyncio.to_thread(runner.活动配置)
            rlog(f"[INFO] 活动配置：{结果摘要(cfg)}")
            cfg_data = cfg.get("data") if isinstance(cfg, dict) else None
            if isinstance(cfg_data, dict):
                activity_name = str(cfg_data.get("activityName") or cfg_data.get("configName") or "")
        except Exception as exc:
            rlog(f"[WARNING] 活动配置查询失败：{脱敏手机号(str(exc))}")

        # myPrize 记录：估算今日新增额度 + 命中已有记录
        records_cache: Any = {}
        try:
            records_cache = await asyncio.to_thread(runner.查询我的奖品)
        except Exception as exc:
            rlog(f"[WARNING] 我的奖品预查询失败：{脱敏手机号(str(exc))}")
        today_count = 今日记录数(records_cache)
        remaining_draws = max(daily_limit - today_count, 0) if daily_limit else len(codes)
        rlog(f"[INFO] 今日 myPrize 记录 {today_count} 条；本次最多新增扫码 {remaining_draws} 个")

        results: list[dict[str, Any]] = []
        winners: list[dict[str, str]] = []
        new_draws = 0
        failed = 0

        for idx, code in enumerate(codes, 1):
            if daily_limit and new_draws >= remaining_draws:
                rlog("[INFO] 已达今日新增扫码额度，停止本次抽奖")
                break
            show = 脱敏(code, keep=4)
            item: dict[str, Any] = {"index": idx, "code": show, "win": False}
            try:
                existing = 查找奖品记录(records_cache, code)
                if existing:
                    summary = 结果摘要({"data": existing})
                    item["summary"] = summary
                    if 是否中奖记录(existing):
                        item["win"] = True
                        winners.append({"code": show, "summary": summary})
                        rlog(f"[SUCCESS] 码{idx} {show} 已有中奖记录：{summary}")
                        if 是否一元换购(existing):
                            rlog(f"[INFO] 码{idx} {show} 为一元换购/再来一瓶，二维码链接：{瓶盖码_url(code)}")
                        elif 是否红包记录(existing):
                            rlog(f"[INFO] 码{idx} {show} 为红包奖，请在微信侧领取")
                    else:
                        rlog(f"[INFO] 码{idx} {show} 已有记录：{summary}")
                    results.append(item)
                    continue

                # 需要新的活动身份（新码需新 wx.login code）
                await 活动身份(code)
                obj = await asyncio.to_thread(runner.抽奖, code)
                new_draws += 1

                detail = None
                data = obj.get("data") if isinstance(obj, dict) else {}
                reward_type = data.get("rewardType") if isinstance(data, dict) else ""
                if reward_type and reward_type != "noSurprise":
                    try:
                        records_cache = await asyncio.to_thread(runner.查询我的奖品)
                        detail = 查找奖品记录(records_cache, code, fallback_latest=True)
                    except Exception:
                        detail = None
                result_obj = {"data": detail} if detail else obj
                summary = 结果摘要(result_obj)
                item["summary"] = summary
                result_data = result_obj.get("data") if isinstance(result_obj, dict) else None
                if isinstance(result_data, dict) and 是否中奖记录(result_data):
                    item["win"] = True
                    winners.append({"code": show, "summary": summary})
                    rlog(f"[SUCCESS] 码{idx} {show} 抽奖：{summary}")
                    if 是否一元换购(result_data):
                        rlog(f"[INFO] 码{idx} {show} 为一元换购/再来一瓶，二维码链接：{瓶盖码_url(code)}")
                    elif 是否红包记录(result_data):
                        rlog(f"[INFO] 码{idx} {show} 为红包奖，请在微信侧领取")
                elif 是否已扫过结果(result_obj):
                    rlog(f"[WARNING] 码{idx} {show}：{summary or '该码已被扫过'}")
                else:
                    rlog(f"[INFO] 码{idx} {show} 抽奖：{summary or '未中奖'}")

                if 是否限制结果(result_obj):
                    item["limited"] = True
                    rlog("[WARNING] 检测到当前账号已达抽奖限制，停止本次运行")
                    results.append(item)
                    break

                results.append(item)
                if interval > 0 and idx < len(codes):
                    await asyncio.sleep(interval)
            except Exception as exc:
                failed += 1
                item["error"] = 脱敏手机号(str(exc))
                rlog(f"[ERROR] 码{idx} {show} 抽奖失败：{item['error']}")
                results.append(item)

        rlog(f"[INFO] 运行结束：处理 {len(results)} 个码，新增抽奖 {new_draws} 个，中奖 {len(winners)} 条，失败 {failed} 条")
        if winners:
            rlog("[SUCCESS] 中奖汇总：" + "；".join(f"{w['code']}={w['summary']}" for w in winners))
        else:
            rlog("[INFO] 本次没有匹配到中奖记录")

        display = {
            "account": 脱敏(openid),
            "activityName": activity_name or None,
            "activity": activity,
            "snCount": len(codes),
            "viaProxy": bool(proxy_url),
            "newDraws": new_draws,
            "failed": failed,
            "results": results,
            "winners": winners,
        }
        return {
            "ok": True,
            "stage": "nongfu",
            "account": 脱敏(openid),
            "viaProxy": bool(proxy_url),
            "snCount": len(codes),
            "nongfuResult": json.dumps(display, ensure_ascii=False, indent=2),
            "cookie": "\n".join(run_lines),
        }
    except Exception as exc:
        return {"ok": False, "stage": "nongfu", "error": str(exc)}
    finally:
        try:
            runner.session.close()
        except Exception:
            pass
