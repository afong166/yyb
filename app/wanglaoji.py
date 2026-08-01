"""内置项目：王老吉「开盖扫码 赢5元礼金」。

把独立脚本 `王老吉开盖赢奖.py` 适配到 app 的内置项目模型：用户选定已绑定的微信账号，
后端经 codebridge 取 wx.login code，再走王老吉主链路：

  1. wechatec.brand.wljhealth.com/userInfoMini/userMemberLogin 换会员 token。
     未注册时用 codebridge 手机号授权 code 调 registerOrLogin 自动注册。
  2. wechatec.brand.wljhealth.com/openapi/getToken 换活动 sign。
  3. s3.lsa0.cn thirdLogin 换活动 Bearer Token。
  4. scanMaskCode -> maskCodeLottery 抽奖。
  5. 中奖红包默认 receiveReward -> claimReward。

同步 requests 逻辑放 asyncio.to_thread；瓶盖码只来自 params，不读 .txt/HAR，不落盘。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import requests

from .logsink import emit as _emit


APPID = "wxd25dc8ba975776e3"

MINI_BASE = "https://wechatec.brand.wljhealth.com"
PROMOTION_BASE = "https://s3.lsa0.cn"
CAMPAIGN_CODE = "jps_wlj_cny_2026"
DEFAULT_LATITUDE = "26.423996194795723"
DEFAULT_LONGITUDE = "112.86963245123752"
DEFAULT_DEVICE_TOKEN = ""
DEFAULT_POSSESSOR = "50c7d6a87202429a9871bf61ec85ad99"
DEFAULT_MINI_SIGN = "7FF7F6345AE61341358E27C609FF40D6"
DEFAULT_REGISTER_SOURCE = "2026pet"
DEFAULT_REGISTER_NICKNAME = "微信用户"
DEFAULT_REGISTER_HEADIMG = (
    "https://wljhealth-oss.oss-cn-shenzhen.aliyuncs.com/mini_img/v2023/userHead.png"
)
DEFAULT_TAG_SIGN = "BBA0F514C69A12717C58F862B00D1605"

MINI_REFERER = f"https://servicewechat.com/{APPID}/431/page-frame.html"
H5_PATH = "/h5OssSrc/2766232/zip/c1d1e811d82543b8ad883c1ee0642618/index.html"
USER_AGENT_MINI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b35) NetType/4G Language/zh_CN"
)
USER_AGENT_H5 = USER_AGENT_MINI + f" miniProgram/{APPID}"

敏感键 = re.compile(
    r"(token|authorization|cookie|session|openid|unionid|phone|mobile|code|encrypted|cipher|"
    r"password|secret|qrcode|headimg|avatar|nickname|memberid|consumerid|userid|usercode|"
    r"serialcode|sign|id)",
    re.I,
)


class 未注册账号异常(RuntimeError):
    def __init__(self, message: str, response: Any | None = None) -> None:
        super().__init__(message)
        self.response = response


# ───────────────────────── 脱敏 / 工具助手（照抄源脚本） ─────────────────────────

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


def 是否网络超时(exc: Exception) -> bool:
    text = str(exc).lower()
    timeout_types = (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    )
    return isinstance(exc, timeout_types) or "read timed out" in text or "timed out" in text


def 分割条目(raw: Any) -> list[str]:
    if not raw:
        return []
    text = str(raw).replace("\r", "\n")
    for sep in (",", "，", "&", ";", "；"):
        text = text.replace(sep, "\n")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def 规范瓶盖码(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    match = re.search(r"s3\.lsa0\.cn/N/00F2/([A-Z0-9]+)", text, re.I)
    if match:
        return f"http://s3.lsa0.cn/N/00F2/{match.group(1).upper()}"
    match = re.search(r"\b(200F[A-Z0-9]{8,})\b", text, re.I)
    if match:
        return f"http://s3.lsa0.cn/N/00F2/{match.group(1).upper()}"
    return text


def 编码瓶盖码(code: str) -> str:
    return quote(规范瓶盖码(code), safe="")


def 展示瓶盖码(value: str) -> str:
    tail = str(value or "").rstrip("/").split("/")[-1]
    return 脱敏(tail, keep=4)


def 读取瓶盖码(inline_codes: str = "") -> list[str]:
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


def 响应是否成功(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("success") is True:
        return True
    return str(obj.get("code")) in ("0", "200") and obj.get("success") is not False


def 响应消息(obj: Any) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    data = obj.get("data")
    texts: list[str] = []
    for source in (data, obj):
        if isinstance(source, dict):
            for key in ("bizMessage", "message", "msg", "errorMsg", "statusTxt", "rewardName"):
                value = source.get(key)
                if value:
                    texts.append(str(value))
    for key in ("message", "msg", "details", "code", "errorCode"):
        value = obj.get(key)
        if value:
            texts.append(str(value))
    return "；".join(dict.fromkeys(texts))


def 限制文本(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    data = obj.get("data")
    texts: list[str] = []
    for source in (data, obj):
        if isinstance(source, dict):
            for key in ("bizMessage", "message", "msg", "errorMsg", "rewardName"):
                value = source.get(key)
                if value:
                    texts.append(str(value))
    return " ".join(texts)


def 是否限制结果(obj: Any) -> bool:
    text = 限制文本(obj)
    if not text:
        return False
    keywords = (
        "上限", "次数已达", "次数已用完", "今日已达", "每日限制",
        "机会已用完", "没有机会", "频繁", "风险", "限制",
    )
    return any(keyword in text for keyword in keywords)


def 是否已扫过或消耗(obj: Any) -> bool:
    text = 限制文本(obj)
    return any(keyword in text for keyword in ("已扫码", "已扫过", "已被扫", "已参与", "重复", "已使用", "不能重复"))


def 抽奖奖品列表(obj: Any) -> list[dict[str, Any]]:
    data = obj.get("data") if isinstance(obj, dict) else None
    rewards = data.get("luckyRewardList") if isinstance(data, dict) else []
    return [item for item in rewards if isinstance(item, dict)] if isinstance(rewards, list) else []


def 结果摘要(obj: Any) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    data = obj.get("data")
    if isinstance(data, dict):
        rewards = data.get("luckyRewardList")
        if isinstance(rewards, list) and rewards:
            names = []
            for item in rewards:
                if isinstance(item, dict):
                    value = str(item.get("rewardUnitValue") or "")
                    unit = str(item.get("rewardUnit") or "")
                    label = str(item.get("rewardName") or item.get("name") or item.get("rewardTypeTxt") or "奖品")
                    if value and unit and value not in label:
                        label = f"{label}({value}{unit})"
                    names.append(label)
            return "中奖：" + "、".join(names)
        parts = []
        for key in ("bizMessage", "campaignName", "activityType", "productName"):
            if data.get(key):
                parts.append(str(data.get(key)))
        if data.get("items") and isinstance(data.get("items"), list):
            parts.append(f"{len(data.get('items') or [])} 条奖品记录")
        if parts:
            return "；".join(parts)
    return 响应消息(obj) or json.dumps(脱敏对象(obj), ensure_ascii=False)[:180]


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


# ───────────────────────── 王老吉主链路 Runner（同步 requests，源脚本逐字节照抄） ─────────

class 王老吉Runner:
    def __init__(self, cfg: SimpleNamespace, log) -> None:
        self.args = cfg
        self._log = log
        self.session = requests.Session()
        self.mini_token = cfg.mini_token or ""
        self.promotion_token = cfg.token or ""
        self.activity_sign = cfg.activity_sign or ""
        self.user_code = cfg.user_code or ""
        self.possessor = cfg.possessor or DEFAULT_POSSESSOR
        self.proxies = {"http": cfg.proxy, "https": cfg.proxy} if cfg.proxy else None

    def 写入会员身份(self, content: dict[str, Any]) -> None:
        self.mini_token = str(content.get("token") or "")
        summary = content.get("user_summary") if isinstance(content.get("user_summary"), dict) else {}
        self.user_code = str(summary.get("userCode") or summary.get("user_id") or self.user_code)
        self.possessor = str(summary.get("possessor") or self.possessor)

    def mini_headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT_MINI,
            "Referer": MINI_REFERER,
        }
        auth = self.mini_token if token is None else token
        if auth:
            headers["Authorization"] = auth
        return headers

    def h5_referer(self, sign: str = "") -> str:
        sign = sign or self.activity_sign
        return f"{PROMOTION_BASE}{H5_PATH}?v={当前毫秒()}&sign={quote(sign, safe='')}&timestamp={当前毫秒()}"

    def promotion_headers(self, token: str | None = None, sign: str = "") -> dict[str, str]:
        bearer = self.promotion_token if token is None else token
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": PROMOTION_BASE,
            "Referer": self.h5_referer(sign=sign),
            "User-Agent": USER_AGENT_H5,
            "Authorization": f"Bearer {bearer}" if bearer else "",
        }

    def request_json(
        self,
        method: str,
        url: str,
        *,
        data: Any = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        allow_business_fail: bool = False,
    ) -> dict[str, Any]:
        resp = self.session.request(
            method,
            url,
            data=data,
            json=json_body,
            headers=headers,
            timeout=self.args.timeout,
            proxies=self.proxies,
        )
        text = resp.text
        try:
            obj = resp.json()
        except Exception as exc:
            raise RuntimeError(f"HTTP {resp.status_code} 返回非 JSON：{text[:180]}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}：{响应消息(obj) or text[:160]}")
        if not allow_business_fail and not 响应是否成功(obj):
            raise RuntimeError(响应消息(obj) or json.dumps(脱敏对象(obj), ensure_ascii=False)[:240])
        return obj

    def 会员登录(self, wx_code: str) -> dict[str, Any]:
        if self.mini_token and self.user_code:
            return {"success": True, "message": "已使用手动 mini token", "content": {"token": self.mini_token}}
        body = {
            "code": wx_code,
            "possessor": "",
            "userCode": "",
            "mallCode": APPID,
            "saleChannel": "mall",
            "sign": self.args.mini_sign,
        }
        obj = self.request_json(
            "POST",
            f"{MINI_BASE}/userInfoMini/userMemberLogin",
            data=body,
            headers=self.mini_headers(token=""),
        )
        content = obj.get("content") if isinstance(obj, dict) else {}
        if isinstance(content, str) and "注册" in content:
            raise 未注册账号异常(content, obj)
        if not isinstance(content, dict) or not content.get("token"):
            raise RuntimeError(f"会员登录未返回 token：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]}")
        self.写入会员身份(content)
        return obj

    def 注册登录(self, login_code: str, phone_code: str) -> dict[str, Any]:
        if not login_code:
            raise RuntimeError("注册登录缺少 wx.login code")
        if not phone_code:
            raise RuntimeError("注册登录缺少手机号授权 code")
        user_info = {
            "country": "中国",
            "province": "",
            "city": "",
            "headimgurl": self.args.register_headimg,
            "nickname": self.args.register_nickname,
            "sex": 0,
            "mallName": "定制商城",
            "openAppid": APPID,
            "loginCode": login_code,
            "phoneCode": phone_code,
            "source": self.args.register_source,
        }
        body = {
            "userInfo": json.dumps(user_info, ensure_ascii=False, separators=(",", ":")),
            "possessor": "",
            "userCode": "",
            "mallCode": APPID,
            "saleChannel": "mall",
            "sign": self.args.mini_sign,
        }
        obj = self.request_json(
            "POST",
            f"{MINI_BASE}/userInfoMini/registerOrLogin",
            data=body,
            headers=self.mini_headers(token=""),
        )
        content = obj.get("content") if isinstance(obj, dict) else {}
        if not isinstance(content, dict) or not content.get("token"):
            raise RuntimeError(f"注册登录未返回 token：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]}")
        self.写入会员身份(content)
        return obj

    def 设置用户标签(self) -> dict[str, Any] | None:
        if not self.args.auto_set_tag or not self.user_code:
            return None
        body = {
            "name": self.args.register_source,
            "type": "admin_add",
            "possessor": self.possessor,
            "userCode": self.user_code,
            "mallCode": APPID,
            "saleChannel": "mall",
            "sign": self.args.tag_sign,
        }
        return self.request_json(
            "POST",
            f"{MINI_BASE}/userInfoMini/setUserTag",
            data=body,
            headers=self.mini_headers(),
            allow_business_fail=True,
        )

    def 生成活动_sign(self, mask_code: str) -> str:
        if self.args.activity_sign:
            self.activity_sign = self.args.activity_sign
            return self.activity_sign
        if not self.user_code:
            raise RuntimeError("缺少 userCode，无法调用 openapi/getToken")
        body = {
            "serialCode": 编码瓶盖码(mask_code),
            "latitude": str(self.args.latitude),
            "longitude": str(self.args.longitude),
            "possessor": self.possessor,
            "userCode": self.user_code,
            "mallCode": APPID,
            "saleChannel": "mall",
        }
        obj = self.request_json(
            "POST",
            f"{MINI_BASE}/openapi/getToken",
            data=body,
            headers=self.mini_headers(),
        )
        sign = str(obj.get("content") or "")
        if not sign:
            raise RuntimeError(f"openapi/getToken 未返回 sign：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]}")
        self.activity_sign = sign
        return sign

    def 活动登录(self, sign: str) -> dict[str, Any]:
        if self.promotion_token and self.args.token:
            return {"success": True, "message": "已使用手动 promotion token", "data": {"token": self.promotion_token}}
        obj = self.request_json(
            "POST",
            f"{PROMOTION_BASE}/openapi/promotion/consumer/auth/thirdLogin",
            json_body={"appid": APPID, "sign": sign},
            headers=self.promotion_headers(token="", sign=sign),
        )
        data = obj.get("data") if isinstance(obj, dict) else {}
        if not isinstance(data, dict) or not data.get("token"):
            raise RuntimeError(f"thirdLogin 未返回 token：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]}")
        self.promotion_token = str(data.get("token") or "")
        return obj

    def 准备活动身份(self, mask_code: str) -> None:
        sign = self.生成活动_sign(mask_code)
        self.活动登录(sign)

    def 活动请求(self, path: str, body: Any, allow_business_fail: bool = False) -> dict[str, Any]:
        return self.request_json(
            "POST",
            f"{PROMOTION_BASE}/openapi/promotion/{path.lstrip('/')}",
            json_body=body,
            headers=self.promotion_headers(),
            allow_business_fail=allow_business_fail,
        )

    def 扫码(self, mask_code: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "maskCode": 编码瓶盖码(mask_code),
            "lng": str(self.args.longitude),
            "lat": str(self.args.latitude),
            "lastScanResult": False,
        }
        if self.args.device_token:
            body["deviceToken"] = self.args.device_token
        return self.活动请求("campaignExecute/scanMaskCode", body, allow_business_fail=True)

    def 抽奖(self, mask_code: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "maskCode": 编码瓶盖码(mask_code),
            "lng": str(self.args.longitude),
            "lat": str(self.args.latitude),
        }
        if self.args.device_token:
            body["deviceToken"] = self.args.device_token
        return self.活动请求("campaignExecute/maskCodeLottery", body, allow_business_fail=True)

    def 领取奖品(self, user_reward_id: str) -> dict[str, Any]:
        return self.活动请求("consumer/receiveReward", {"userRewardId": user_reward_id}, allow_business_fail=True)

    def 提交奖品(self, user_reward_id: str) -> dict[str, Any]:
        return self.活动请求("consumer/claimReward", {"id": user_reward_id}, allow_business_fail=True)

    # ───── 线程内一次性完成：登录 + 逐码扫码/抽奖 + 统一领取，边跑边打 [LEVEL] 日志 ─────

    def 处理一个码(self, index: int, total: int, mask_code: str, do_lottery: bool) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        head = f"码{index}/{total} {展示瓶盖码(mask_code)}"
        item: dict[str, Any] = {"code": 展示瓶盖码(mask_code)}
        winners: list[dict[str, Any]] = []
        limited = False
        try:
            self.准备活动身份(mask_code)
            scan = self.扫码(mask_code)
            item["scan"] = 结果摘要(scan)
            if 响应是否成功(scan):
                self._log(f"[SUCCESS] {head} 扫码：{item['scan']}")
            else:
                self._log(f"[WARNING] {head} 扫码：{item['scan'] or '扫码未通过'}")
            if 是否限制结果(scan):
                item["limited"] = True
                self._log(f"[WARNING] {head}：检测到当日扫码已达上限，停止该账号")
                return item, winners, True

            if do_lottery:
                lottery = self.抽奖(mask_code)
                item["lottery"] = 结果摘要(lottery)
                rewards = 抽奖奖品列表(lottery)
                if rewards:
                    prizes: list[str] = []
                    for reward in rewards:
                        reward_name = str(reward.get("rewardName") or reward.get("name") or "王老吉奖品")
                        user_reward_id = str(reward.get("userRewardId") or "")
                        prizes.append(reward_name)
                        if user_reward_id:
                            winners.append({"reward_name": reward_name, "user_reward_id": user_reward_id})
                            self._log(f"[SUCCESS] {head} 中奖：{reward_name}（userRewardId={脱敏(user_reward_id)}）")
                        else:
                            self._log(f"[WARNING] {head} 中奖：{reward_name}，但未返回 userRewardId，无法领取")
                    item["prizes"] = prizes
                elif 响应是否成功(lottery):
                    self._log(f"[INFO] {head} 抽奖：{item['lottery'] or '未中奖'}")
                else:
                    self._log(f"[INFO] {head} 抽奖：{item['lottery'] or '未中奖'}")
                if 是否限制结果(lottery):
                    limited = True
                    self._log(f"[WARNING] {head}：抽奖返回限制结果，停止该账号")
        except Exception as exc:
            item["error"] = 脱敏手机号(str(exc))
            self._log(f"[ERROR] {head}：执行失败 {item['error']}")
            if 是否网络超时(exc):
                limited = True
        return item, winners, limited

    def 统一领取(self, winners: list[dict[str, Any]], do_receive: bool, do_claim: bool) -> None:
        if not winners:
            return
        if not do_receive:
            self._log("[INFO] 已关闭自动领取，中奖红包只记录不提交")
            return
        self._log(f"[INFO] 统一领取中奖红包，共 {len(winners)} 个")
        for winner in winners:
            reward_name = str(winner.get("reward_name") or "王老吉奖品")
            user_reward_id = str(winner.get("user_reward_id") or "")
            try:
                receive = self.领取奖品(user_reward_id)
                self._log(f"[SUCCESS] 领取 {reward_name}：{结果摘要(receive)}")
            except Exception as exc:
                self._log(f"[ERROR] 领取 {reward_name} 失败：{脱敏手机号(str(exc))}")
                continue
            if do_claim:
                try:
                    claim = self.提交奖品(user_reward_id)
                    self._log(f"[SUCCESS] 提交 {reward_name}：{结果摘要(claim)}")
                except Exception as exc:
                    self._log(f"[ERROR] 提交 {reward_name} 失败：{脱敏手机号(str(exc))}")


def _build_cfg(params: dict, proxy_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        mini_token=str(params.get("miniToken") or ""),
        user_code=str(params.get("userCode") or ""),
        possessor=str(params.get("possessor") or DEFAULT_POSSESSOR),
        mini_sign=str(params.get("miniSign") or DEFAULT_MINI_SIGN),
        tag_sign=str(params.get("tagSign") or DEFAULT_TAG_SIGN),
        token=str(params.get("token") or ""),
        activity_sign=str(params.get("activitySign") or ""),
        device_token=str(params.get("deviceToken") or DEFAULT_DEVICE_TOKEN),
        campaign_code=str(params.get("campaignCode") or CAMPAIGN_CODE),
        latitude=str(params.get("latitude") or DEFAULT_LATITUDE),
        longitude=str(params.get("longitude") or DEFAULT_LONGITUDE),
        register_source=str(params.get("registerSource") or DEFAULT_REGISTER_SOURCE),
        register_nickname=str(params.get("registerNickname") or DEFAULT_REGISTER_NICKNAME),
        register_headimg=str(params.get("registerHeadimg") or DEFAULT_REGISTER_HEADIMG),
        auto_set_tag=_bool_param(params, "autoSetTag", True),
        timeout=int(_float_param(params, "timeout", 45.0)) or 45,
        proxy=proxy_url,
    )


async def run_wanglaoji_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from .codebridge import get_code_for_openid, get_phone_for_openid
    from . import db

    params = params or {}
    target_appid = params.get("appid") or appid or APPID
    codes = 读取瓶盖码(params.get("codes") or params.get("sn") or params.get("value") or "")
    if not codes:
        return {"ok": False, "stage": "params", "error": "请输入王老吉瓶盖码链接"}

    auto_register = _bool_param(params, "autoRegister", True)
    do_lottery = _bool_param(params, "lottery", True)
    auto_claim = _bool_param(params, "autoClaim", True)
    auto_receive = _bool_param(params, "autoReceive", auto_claim)
    interval = _float_param(params, "interval", 5.0)

    # 代理：优先本次表单填写的 proxyUrl；留空则回退到该微信账号扫码时绑定的地区代理，
    # 让王老吉接口与账号同地区出网，避免异地 IP 触发风控。
    _acc = db.query_one("SELECT proxy_url FROM accounts WHERE openid=?", (openid,))
    from .shortproxy import project_proxy
    proxy_url = project_proxy(params, openid)

    # 实时日志：边跑边推（前端可实时看到扫码/抽奖/领取进度）
    lines: list[str] = []

    def log(line: str) -> None:
        lines.append(line)
        _emit(line)

    cr = await get_code_for_openid(openid, target_appid or APPID)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "failed to get 王老吉 code"}

    cfg = _build_cfg(params, proxy_url)
    runner = 王老吉Runner(cfg, log)
    try:
        log(f"[INFO] ▶ 账号：{脱敏(openid, keep=4)}｜瓶盖码 {len(codes)} 个｜代理 {'是' if proxy_url else '否'}")
        # 1) 会员登录（未注册则自动注册）
        try:
            await asyncio.to_thread(runner.会员登录, cr["code"])
            log("[SUCCESS] 会员登录成功")
        except 未注册账号异常:
            log("[WARNING] 会员未注册，尝试自动注册登录…")
            if not auto_register:
                return {"ok": False, "stage": "wanglaoji", "error": "账号未注册且已关闭自动注册"}
            register_cr = await get_code_for_openid(openid, target_appid or APPID)
            if not register_cr.get("success") or not register_cr.get("code"):
                return {"ok": False, "stage": "get-code", "error": register_cr.get("error") or "注册取 wx.login code 失败"}
            pr = await get_phone_for_openid(openid, target_appid or APPID)
            phone_code = pr.get("code") or ""
            if not pr.get("success") or not phone_code:
                return {"ok": False, "stage": "get-phone", "error": pr.get("error") or "注册取手机号授权 code 失败"}
            await asyncio.to_thread(runner.注册登录, register_cr["code"], str(phone_code))
            log("[SUCCESS] 自动注册登录成功")
            try:
                tag_obj = await asyncio.to_thread(runner.设置用户标签)
                if tag_obj is not None:
                    log(f"[INFO] 设置用户标签：{结果摘要(tag_obj)}")
            except Exception as exc:
                log(f"[WARNING] 设置用户标签失败：{脱敏手机号(str(exc))}")

        # 2) 逐个瓶盖码扫码 / 抽奖
        results: list[dict[str, Any]] = []
        winners: list[dict[str, Any]] = []
        total = len(codes)
        for index, mask_code in enumerate(codes, 1):
            if index > 1 and interval > 0:
                await asyncio.sleep(interval)
            item, item_winners, limited = await asyncio.to_thread(
                runner.处理一个码, index, total, mask_code, do_lottery
            )
            results.append(item)
            winners.extend(item_winners)
            if limited:
                break

        # 3) 统一领取中奖红包
        if winners:
            await asyncio.to_thread(runner.统一领取, winners, auto_receive, auto_claim)
        else:
            log("[INFO] 本次没有匹配到中奖红包")

        log("[INFO] 运行结束")

        display = {
            "account": 脱敏(runner.user_code or openid),
            "campaignCode": cfg.campaign_code,
            "snCount": total,
            "viaProxy": bool(proxy_url),
            "winnerCount": len(winners),
            "results": results,
        }
        return {
            "ok": True,
            "stage": "wanglaoji",
            "account": 脱敏(runner.user_code or openid),
            "viaProxy": bool(proxy_url),
            "snCount": total,
            "wanglaojiResult": json.dumps(display, ensure_ascii=False, indent=2),
            "cookie": "\n".join(lines),
        }
    except Exception as exc:
        return {"ok": False, "stage": "wanglaoji", "error": 脱敏手机号(str(exc))}
    finally:
        try:
            runner.session.close()
        except Exception:
            pass
