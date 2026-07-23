"""内置项目：乐虎「开盖赢红包」扫码抽奖 + 现金红包自动提现。

把独立脚本 `乐虎开盖赢奖.py` 移植进 app 的内置项目模型：
用户选一个已绑定的微信账号，后端经 codebridge 取一枚 wx.login code，然后：
  1. hzhuihe.cn/cpp/offsetwxapplogin.json 换达利小程序 token。
  2. checkDaliQrCode -> queryPageUrl -> checkDaLiGroupFlag 识别活动。
  3. scanQrCodeV2/doActivityPreCheck -> scanQrCodeV2/doActivity 抽奖。
  4. 命中现金红包后 getWinGoodsAndCompleteInfo/getUserInfo/doTransfer 自动提现。

全部签名/加密（SIGN_KEY、X-Sign、X-RequestId、AES-ECB 等）逐字节照抄源脚本；
requests 同步链路统一放进 asyncio.to_thread。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .logsink import emit as _emit

# ───────────────── 源脚本常量（逐字节照抄，不得改写） ─────────────────
APPID = "wx1e7ba839c6bc0a27"

BASE_URL = "https://hzhuihe.cn"
CPP_BASE = BASE_URL + "/cpp"
SIGN_KEY = "MKnEu6zaS04N23XoMUL8GOwOKIQwXMvT"
XSIGN_CLIENT_ID = "dl89hUorI6Ky79SK"
CLIENT_TYPE = "wxapp_dl"
VERSION_NO = "20260602"
CLIENT_MAC = os.getenv("LEHU_CLIENT_MAC", "iPhone 14 Pro Max<iPhone15,3>;3.16.2;iOS 17.0;8.0.75")
DEFAULT_LATITUDE = os.getenv("LEHU_LATITUDE", "26.41995578342014")
DEFAULT_LONGITUDE = os.getenv("LEHU_LONGITUDE", "112.87409369574652")
DEFAULT_GROUP_ID = os.getenv("LEHU_GROUP_ID", "28757")

MINI_REFERER = f"https://servicewechat.com/{APPID}/27/page-frame.html"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b35) NetType/WIFI Language/zh_CN"
)

敏感键 = re.compile(
    r"(token|authorization|cookie|session|openid|unionid|phone|mobile|code|password|secret|"
    r"headimg|avatar|nickname|memberid|userid|wxchat|wxapp|voucher|qrcode|sign|idcard)",
    re.I,
)


# ───────────────── 工具函数（源脚本脱敏 / 签名助手照抄） ─────────────────
def 当前毫秒() -> str:
    return str(int(time.time() * 1000))


def md5_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def md5_lower(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


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
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if 敏感键.search(str(key)):
                result[key] = 脱敏(value) if not isinstance(value, (dict, list)) else "<已脱敏>"
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
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def 规范瓶盖码(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if re.match(r"^https?://", text, re.I):
        parsed = urlparse(text)
        tail = unquote(parsed.path.rstrip("/").split("/")[-1]).strip()
        return tail or text
    if "?" in text:
        text = text.split("?", 1)[0]
    return unquote(text.rstrip("/").split("/")[-1]).strip()


def 瓶盖码_full_url(qr_code: str) -> str:
    return f"http://v9.pw/d/{qr_code}"


def 展示瓶盖码(value: str) -> str:
    return 脱敏(规范瓶盖码(value), keep=4)


def 读取瓶盖码(inline_codes: str) -> list[str]:
    rows = 分割条目(inline_codes)
    seen: set[str] = set()
    codes: list[str] = []
    for item in rows:
        code = 规范瓶盖码(item)
        key = code.upper()
        if code and key not in seen:
            seen.add(key)
            codes.append(code)
    return codes


def 响应成功(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    error_code = obj.get("errorCode")
    if error_code not in (None, "") and str(error_code).upper() != "SUCCESS":
        return False
    if str(error_code or "").upper() == "SUCCESS":
        return True
    if obj.get("status") == 1 or str(obj.get("status")) == "1":
        return True
    if obj.get("success") is True:
        return True
    return False


def 响应消息(obj: Any) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    parts: list[str] = []
    for key in ("errorMsg", "msg", "message", "content", "errorCode"):
        value = obj.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("errorMsg", "msg", "message", "presentName", "brandName", "groupName"):
            value = data.get(key)
            if value not in (None, ""):
                parts.append(str(value))
    elif data not in (None, "") and not isinstance(data, (list, tuple)):
        parts.append(str(data))
    seen: list[str] = []
    for item in parts:
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return "；".join(seen) or json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]


def 结果摘要(obj: Any) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    data = obj.get("data")
    pieces: list[str] = []
    msg = 响应消息(obj)
    if msg:
        pieces.append(msg)
    if isinstance(data, dict):
        rewards = data.get("rewardVOList")
        if isinstance(rewards, list) and rewards:
            for reward in rewards:
                if isinstance(reward, dict):
                    name = reward.get("presentName") or reward.get("amount") or reward.get("voucherNo")
                    if name not in (None, ""):
                        pieces.append(f"奖品={name}")
        for key in ("pageUrl", "groupName", "brandName"):
            value = data.get(key)
            if value not in (None, ""):
                pieces.append(f"{key}={value}")
    return "；".join(str(item) for item in pieces if str(item))


def 是否限制结果(obj: Any) -> bool:
    if isinstance(obj, str):
        text = obj
    elif isinstance(obj, dict):
        parts: list[str] = []
        for key in ("errorMsg", "msg", "message", "content", "errorCode", "code"):
            value = obj.get(key)
            if value not in (None, ""):
                parts.append(str(value))
        data = obj.get("data")
        if isinstance(data, dict):
            for key in ("errorMsg", "msg", "message", "content", "toast", "tips", "errorCode", "code"):
                value = data.get(key)
                if value not in (None, ""):
                    parts.append(str(value))
        elif isinstance(data, str):
            parts.append(data)
        text = "；".join(parts)
    else:
        text = str(obj)
    keywords = (
        "当日扫码次数已达上限",
        "扫码次数已达上限",
        "次数已达上限",
        "明天再来",
        "今日已达上限",
        "当日上限",
        "SCAN_FREEZE_RISK",
    )
    return any(key in text for key in keywords)


def 是否已扫过或消耗(obj: Any) -> bool:
    text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    keywords = (
        "已扫过", "扫过", "重复", "不能重复", "已参与", "已领取", "已兑换", "已核销",
        "谢谢参与", "谢谢惠顾", "未中奖", "不中奖", "好运擦肩",
        "无活动配置信息", "活动已结束", "无效兑奖码",
    )
    return any(key in text for key in keywords)


def 是否签名失败(obj: Any) -> bool:
    text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    return "接口签名" in text or "签名为空" in text or '"1002008"' in text or "1002008" in text


class 临时接口异常(RuntimeError):
    pass


def 活动路由(page_url: str) -> tuple[str, str, bool]:
    page = str(page_url or "").lower()
    if page == "lehu25":
        return "scanGroup/dalilehu/index/index", "SSQC", False
    return "scanGroup/daliScan/index/index", "25DLKG", True


def 提取奖励列表(obj: Any) -> list[dict[str, Any]]:
    if not isinstance(obj, dict):
        return []
    data = obj.get("data")
    if not isinstance(data, dict):
        return []
    rewards = data.get("rewardVOList")
    if isinstance(rewards, list):
        result = []
        for item in rewards:
            if isinstance(item, dict):
                merged = dict(item)
                for key in ("groupId", "brandId", "activityId"):
                    if merged.get(key) in (None, "") and data.get(key) not in (None, ""):
                        merged[key] = data.get(key)
                result.append(merged)
        return result
    reward = data.get("scanRewardVO")
    return [reward] if isinstance(reward, dict) else []


def 是否现金红包(reward: dict[str, Any]) -> bool:
    try:
        amount = float(reward.get("amount") or 0)
    except Exception:
        amount = 0
    return str(reward.get("presentType")) == "2" and amount > 0


# ───────────────── 乐虎运行器（去掉 argparse，改吃显式配置） ─────────────────
class LehuRunner:
    def __init__(self, *, proxy: str = "", latitude: str = DEFAULT_LATITUDE,
                 longitude: str = DEFAULT_LONGITUDE, timeout: int = 25,
                 request_marker: str = "", no_request_id: bool = False) -> None:
        self.session = requests.Session()
        self.token = ""
        self.user_id = ""
        self.latitude = latitude
        self.longitude = longitude
        self.timeout = timeout
        self.request_marker = request_marker
        self.no_request_id = no_request_id
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    @property
    def locate(self) -> str:
        return f"{self.latitude},{self.longitude}"

    def headers(self, x_request_id: str = "", x_sign: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": MINI_REFERER,
        }
        if x_request_id:
            headers["X-RequestId"] = x_request_id
        if x_sign:
            headers["X-Sign"] = x_sign
        return headers

    def build_payload(self, data: Any, route: str, timestamp: str | None = None) -> dict[str, Any]:
        ts = timestamp or 当前毫秒()
        token = self.token or ""
        context = {
            "clientType": CLIENT_TYPE,
            "token": token,
            "clientMac": CLIENT_MAC,
            "versionNo": VERSION_NO,
            "timestamp": ts,
            "requestPath": route,
        }
        return {
            "data": data,
            "context": context,
            "sign": md5_upper(f"{token}{ts}key={SIGN_KEY}"),
        }

    def encode_body(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def xsign_value_text(self, value: Any) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    def xsign_kv_text(self, value: Any) -> str:
        if isinstance(value, dict):
            if not value:
                return "null={}"
            keys = sorted(value.keys(), key=lambda item: str(item).lower())
            return "&".join(f"{key}={self.xsign_value_text(value.get(key))}" for key in keys)
        if value in (None, ""):
            return "null=null"
        return f"null={self.xsign_value_text(value)}"

    def build_x_sign(self, payload: dict[str, Any]) -> str:
        context = payload.get("context") if isinstance(payload, dict) else {}
        context = context if isinstance(context, dict) else {}
        timestamp = str(context.get("timestamp") or "")
        if not timestamp:
            return ""
        context_text = self.xsign_kv_text(
            {
                "clientMac": context.get("clientMac") or "",
                "clientType": context.get("clientType") or CLIENT_TYPE,
                "requestPath": context.get("requestPath") or "",
                "timestamp": timestamp,
                "token": context.get("token") or "",
                "versionNo": context.get("versionNo") or VERSION_NO,
            }
        )
        plain = (
            f"clientId:{XSIGN_CLIENT_ID}|context:{context_text}|"
            f"data:{self.xsign_kv_text(payload.get('data'))}|timestamp:{timestamp}|sign"
        )
        key_source = md5_lower(md5_lower(timestamp))
        start = int(timestamp[-1]) if timestamp[-1:].isdigit() else 0
        if start + 16 > len(key_source):
            start = max(len(key_source) - 16, 0)
        key = key_source[start: start + 16].encode("utf-8")
        ciphertext = AES.new(key, AES.MODE_ECB).encrypt(pad(plain.encode("utf-8"), AES.block_size)).hex().upper()
        return md5_lower(ciphertext)

    def derive_request_id(self, request_seed: str) -> str:
        seed = str(request_seed or "")
        if not seed:
            return ""
        if self.request_marker:
            marker = self.request_marker
        elif self.token:
            marker = "[object Object]"
        else:
            marker = "undefined"
        short_seed = seed[:3] + seed[-3:] if len(seed) >= 6 else seed
        plaintext = f"{CLIENT_TYPE}{marker}{short_seed}"
        cipher = AES.new(seed.encode("utf-8"), AES.MODE_ECB)
        ciphertext = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size)).hex().upper()
        return hashlib.md5(ciphertext.encode("utf-8")).hexdigest()

    def get_request_id(self, route: str) -> str:
        if self.no_request_id:
            return ""
        payload = self.build_payload("", route)
        x_sign = self.build_x_sign(payload)
        resp = self.session.post(
            BASE_URL + "/wpp/commonrequest/getRequestId.json",
            data=self.encode_body(payload),
            headers=self.headers(x_sign=x_sign),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        try:
            obj = resp.json()
        except Exception as exc:
            raise RuntimeError(f"getRequestId 返回非 JSON：HTTP {resp.status_code} {resp.text[:160]}") from exc
        if not 响应成功(obj) or not obj.get("data"):
            raise RuntimeError(f"getRequestId 失败：{响应消息(obj)}")
        seed = obj.get("data") if isinstance(obj, dict) else ""
        return self.derive_request_id(str(seed or ""))

    def request_json(self, path: str, data: Any, route: str) -> dict[str, Any]:
        if path.startswith("http"):
            url = path
        elif path.startswith("/cpp/"):
            url = BASE_URL + path
        else:
            url = CPP_BASE + path
        payload = self.build_payload(data, route)
        x_request_id = self.get_request_id(route)
        x_sign = self.build_x_sign(payload)
        resp = self.session.post(
            url,
            data=self.encode_body(payload),
            headers=self.headers(x_request_id, x_sign),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        text = resp.text
        try:
            obj = resp.json()
        except Exception as exc:
            raise RuntimeError(f"HTTP {resp.status_code} 返回非 JSON：{text[:180]}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}：{响应消息(obj) or text[:160]}")
        if 是否签名失败(obj):
            raise 临时接口异常(f"{响应消息(obj)}；当前码未删除")
        return obj

    # ── 业务接口（路径 / route / payload 照抄源脚本） ──
    def 登录(self, wx_code: str) -> dict[str, Any]:
        obj = self.request_json("/offsetwxapplogin.json", {"code": wx_code}, "pages/index/index")
        if not 响应成功(obj):
            raise RuntimeError(响应消息(obj))
        data = obj.get("data") if isinstance(obj, dict) else {}
        if not isinstance(data, dict) or not data.get("token"):
            raise RuntimeError(f"会员登录未返回 token：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:260]}")
        self.token = str(data.get("token") or "")
        self.user_id = str(data.get("userId") or self.user_id or "")
        return obj

    def 检查达利码(self, qr_code: str) -> dict[str, Any]:
        return self.request_json("/dali/checkDaliQrCode.json", {"qrCode": qr_code}, "scanGroup/index/index")

    def 查询活动页(self, qr_code: str) -> dict[str, Any]:
        return self.request_json("/system/queryPageUrl.json", qr_code, "scanGroup/index/index")

    def 检查达利活动标记(self, page_url: str) -> dict[str, Any]:
        return self.request_json("/onethingoneyard/activity/checkDaLiGroupFlag.json",
                                 {"pageUrl": page_url}, "scanGroup/index/index")

    def 查询关闭信息(self, brand_id: Any, route: str) -> dict[str, Any]:
        return self.request_json("/dali/queryBizClosedownInfo.json", brand_id or "", route)

    def 活动预检(self, qr_code: str, biz_code: str, route: str) -> dict[str, Any]:
        return self.request_json(
            "/scanQrCodeV2/doActivityPreCheck.json",
            {
                "locate": self.locate,
                "qrCode": qr_code,
                "bizCode": biz_code,
                "qrCodeFullUrl": 瓶盖码_full_url(qr_code),
            },
            route,
        )

    def 抽奖(self, qr_code: str, biz_code: str, route: str) -> dict[str, Any]:
        return self.request_json(
            "/scanQrCodeV2/doActivity.json",
            {
                "locate": self.locate,
                "qrCode": qr_code,
                "bizCode": biz_code,
                "qrCodeFullUrl": 瓶盖码_full_url(qr_code),
            },
            route,
        )

    def 查询领取信息(self, group_id: Any, brand_id: Any, voucher_no: str, route: str) -> dict[str, Any]:
        return self.request_json(
            "/my/getWinGoodsAndCompleteInfo.json",
            {"groupId": group_id, "brandId": brand_id, "voucherNo": voucher_no},
            route,
        )

    def 查询用户信息(self, route: str) -> dict[str, Any]:
        return self.request_json("/my/getUserInfo.json", {}, route)

    def 提现(self, group_id: Any, brand_id: Any, voucher_no: str, route: str) -> dict[str, Any]:
        return self.request_json(
            "/my/doTransfer.json",
            {"groupId": group_id, "brandId": brand_id, "voucherNo": voucher_no},
            route,
        )


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


def _run_sync(wx_code: str, codes: list[str], *, proxy: str, latitude: str, longitude: str,
              group_id: str, auto_transfer: bool, interval: float, timeout: int,
              request_marker: str, no_request_id: bool) -> dict[str, Any]:
    """完整同步链路：登录 -> 逐码扫码抽奖 -> 现金红包提现。放进 asyncio.to_thread 执行。

    实时日志：每一步转成 `[LEVEL] 文本` 并 emit（contextvar 会随 to_thread 复制到本线程）。
    """
    lines: list[str] = []

    def log(line: str) -> None:
        lines.append(line)
        _emit(line)

    runner = LehuRunner(proxy=proxy, latitude=latitude, longitude=longitude,
                        timeout=timeout, request_marker=request_marker, no_request_id=no_request_id)

    login_obj = runner.登录(wx_code)
    log(f"[SUCCESS] 会员登录成功：{结果摘要(login_obj) or 'ok'}")

    results: list[dict[str, Any]] = []
    winners: list[dict[str, Any]] = []
    total = len(codes)

    for idx, code in enumerate(codes, 1):
        qr_code = 规范瓶盖码(code)
        head = f"码{idx}/{total} {展示瓶盖码(qr_code)}"
        log(f"[INFO] 开始处理 {head}")
        item: dict[str, Any] = {"index": idx, "qrCode": 展示瓶盖码(qr_code), "winners": []}
        try:
            check_obj = runner.检查达利码(qr_code)
            if 是否限制结果(check_obj):
                log(f"[WARNING] {head}：当日扫码已达上限，停止处理")
                item["state"] = "达上限"
                results.append(item)
                break

            page_obj = runner.查询活动页(qr_code)
            page_msg = 结果摘要(page_obj)
            log(f"[INFO] {head} 活动识别：{page_msg or '-'}")
            if 是否限制结果(page_obj):
                log(f"[WARNING] {head}：当日扫码已达上限，停止处理")
                item["state"] = "达上限"
                results.append(item)
                break

            page_data = page_obj.get("data") if isinstance(page_obj, dict) else {}
            page_data = page_data if isinstance(page_data, dict) else {}
            page_url = str(page_data.get("pageUrl") or "")
            brand_id = page_data.get("brandId") or 99
            grp_id = page_data.get("id") or page_data.get("groupId") or group_id
            route, biz_code, need_precheck = 活动路由(page_url)
            item["activity"] = page_data.get("groupName") or page_data.get("brandName") or page_url

            runner.检查达利活动标记(page_url)

            if need_precheck:
                pre_obj = runner.活动预检(qr_code, biz_code, route)
                if 是否限制结果(pre_obj):
                    log(f"[WARNING] {head}：当日扫码已达上限，停止处理")
                    item["state"] = "达上限"
                    results.append(item)
                    break

            close_obj = runner.查询关闭信息(brand_id, route)
            if isinstance(close_obj.get("data"), dict) and close_obj["data"].get("interceptFlag"):
                log(f"[WARNING] {head} 活动拦截：{结果摘要(close_obj) or '-'}，停止处理")
                item["state"] = "拦截"
                results.append(item)
                break

            lottery_obj = runner.抽奖(qr_code, biz_code, route)
            lottery_msg = 结果摘要(lottery_obj)
            if 是否限制结果(lottery_obj):
                log(f"[WARNING] {head} 抽奖：{lottery_msg or '当日上限'}，停止处理")
                item["state"] = "达上限"
                results.append(item)
                break

            rewards = 提取奖励列表(lottery_obj)
            cash_rewards = [r for r in rewards if 是否现金红包(r)]
            if cash_rewards:
                log(f"[SUCCESS] {head} 抽奖：{lottery_msg or '中奖'}")
            elif 是否已扫过或消耗(lottery_obj):
                log(f"[INFO] {head} 抽奖：{lottery_msg or '未中奖/已处理'}")
            else:
                log(f"[INFO] {head} 抽奖：{lottery_msg or '完成'}")

            for reward in cash_rewards:
                voucher_no = str(reward.get("voucherNo") or "")
                amount = reward.get("amount")
                win = {
                    "qrCode": 展示瓶盖码(qr_code),
                    "voucher": 脱敏(voucher_no, keep=4),
                    "amount": amount,
                    "presentType": reward.get("presentType"),
                    "couponType": reward.get("couponType"),
                    "groupId": reward.get("groupId") or grp_id,
                    "brandId": reward.get("brandId") or brand_id,
                    "transferStatus": "",
                    "message": "",
                }
                log(f"[SUCCESS] {head} 中奖现金红包：{amount} 元，券号 {脱敏(voucher_no, keep=4)}")
                if auto_transfer and voucher_no:
                    info_obj = runner.查询领取信息(win["groupId"], win["brandId"], voucher_no, route)
                    need_complete = isinstance(info_obj.get("data"), dict) and info_obj["data"].get("needComplete")
                    if need_complete:
                        win["transferStatus"] = "needComplete"
                        win["message"] = "需要补全领奖信息，未自动提现"
                        log(f"[WARNING] {head} 提现跳过：需补全领奖信息")
                    else:
                        runner.查询用户信息(route)
                        transfer_obj = runner.提现(win["groupId"], win["brandId"], voucher_no, route)
                        ok = 响应成功(transfer_obj)
                        win["transferStatus"] = "submitted" if ok else "failed"
                        win["message"] = 结果摘要(transfer_obj)
                        lvl = "SUCCESS" if ok else "WARNING"
                        log(f"[{lvl}] {head} 提现结果：{win['message'] or ('已提交' if ok else '失败')}")
                elif not auto_transfer:
                    win["transferStatus"] = "recorded"
                    log(f"[INFO] {head} 已记录中奖红包（未自动提现）")
                item["winners"].append(win)
                winners.append(win)

            item["state"] = "中奖" if cash_rewards else "已处理"
            results.append(item)
        except 临时接口异常 as exc:
            log(f"[ERROR] {head} 接口临时失败：{脱敏手机号(str(exc))}，停止处理")
            item["state"] = "接口异常"
            item["error"] = str(exc)
            results.append(item)
            break
        except Exception as exc:
            log(f"[ERROR] {head} 执行失败：{脱敏手机号(str(exc))}")
            item["state"] = "失败"
            item["error"] = str(exc)
            results.append(item)
        if idx < total and interval > 0:
            time.sleep(interval)

    log(f"[INFO] 运行结束：处理 {len(results)} 个码，命中现金红包 {len(winners)} 个")
    return {
        "userId": runner.user_id,
        "token": runner.token,
        "results": results,
        "winners": winners,
        "lines": lines,
    }


async def run_lehu_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from .codebridge import get_code_for_openid
    from . import db

    params = params or {}
    target_appid = params.get("appid") or appid or APPID

    codes = 读取瓶盖码(str(params.get("codes") or params.get("sn") or params.get("value") or ""))
    if not codes:
        return {"ok": False, "stage": "params", "error": "请输入乐虎瓶盖码链接"}

    latitude = str(params.get("latitude") or DEFAULT_LATITUDE)
    longitude = str(params.get("longitude") or DEFAULT_LONGITUDE)
    group_id = str(params.get("groupId") or DEFAULT_GROUP_ID)
    auto_transfer = _bool_param(params, "autoTransfer", True)
    interval = _float_param(params, "interval", 5.0)
    timeout = int(_float_param(params, "timeout", 25.0)) or 25
    request_marker = str(params.get("requestMarker") or "")
    no_request_id = _bool_param(params, "noRequestId", False)

    # 代理：优先本次表单填写的 proxyUrl；留空则回退到该微信账号扫码时绑定的地区代理，
    # 让 hzhuihe 达利接口与账号同地区出网，避免异地 IP 触发风控。
    _acc = db.query_one("SELECT proxy_url FROM accounts WHERE openid=?", (openid,))
    proxy_url = (params.get("proxyUrl") or "").strip() or ((_acc["proxy_url"] or "") if _acc else "")

    cr = await get_code_for_openid(openid, target_appid)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "取乐虎登录 code 失败"}

    try:
        out = await asyncio.to_thread(
            _run_sync,
            cr["code"], codes,
            proxy=proxy_url, latitude=latitude, longitude=longitude, group_id=group_id,
            auto_transfer=auto_transfer, interval=interval, timeout=timeout,
            request_marker=request_marker, no_request_id=no_request_id,
        )
    except Exception as exc:
        return {"ok": False, "stage": "lehu", "error": str(exc)}

    account = 脱敏(out.get("userId") or openid)
    display = {
        "account": account,
        "snCount": len(codes),
        "viaProxy": bool(proxy_url),
        "results": out.get("results"),
        "winners": out.get("winners"),
    }
    return {
        "ok": True,
        "stage": "lehu",
        "account": account,
        "viaProxy": bool(proxy_url),
        "snCount": len(codes),
        "lehuResult": json.dumps(display, ensure_ascii=False, indent=2),
        "cookie": "\n".join(out.get("lines") or []),
    }
