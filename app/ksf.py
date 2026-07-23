"""内置项目：康师傅开盖赢奖（扫码 + 抽奖 + 中奖核销二维码）。

把独立脚本「康师傅开盖赢奖.py」移植到 app 的内置项目模型：用户选一个已绑定的微信账号，
后端用 codebridge 取 wx.login code（原脚本走外部应用宝 Code 服务 http://192.168.50.1:8000，
这里换成内部 codebridge.get_code_for_openid）。业务主链路忠实照搬原脚本：
  1. nclub.ksf.cn 会员登录，拿康师傅会员 Token（无 token 时自动补授权资料并用新 code 重登）。
  2. club.ksf.cn/api/third/ac/ciphertext 生成 encryptedData。
  3. 3.ksf.cn thirdLogin 换活动 Bearer Token。
  4. scanMaskCode -> maskCodeLottery 抽奖。
  5. 中奖后 receiveReward + getReportQrcode 拿核销二维码（不落盘，作为字段/日志给出）。

所有 URL/headers/payload/加密(encryptedData)/常量逐字节照抄原脚本；curl_cffi 缺失时优雅回退
到 requests（原脚本 curl_requests 可能为 None，这里降级为普通 TLS 而非报错）。
原脚本是同步 requests/curl_cffi；这里保留其同步结构放到线程里跑（asyncio.to_thread），
通过 asyncio.run_coroutine_threadsafe 把异步 codebridge 取码桥回主事件循环。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from .logsink import emit as _emit

try:
    import requests as plain_requests
except ImportError:  # pragma: no cover - requests 是本项目必备依赖
    plain_requests = None

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None


APPID = "wx54f3e6a00f7973a7"

NCLUB_BASE = "https://nclub.ksf.cn/pro/whale-member/api"
CLUB_BASE = "https://club.ksf.cn/api"
PROMOTION_BASE = "https://3.ksf.cn"
CAMPAIGN_CODE = os.getenv("KSF_CAMPAIGN_CODE", "jps_qg_ksflc")
DEFAULT_LATITUDE = os.getenv("KSF_LATITUDE", "32.910069444444446")
DEFAULT_LONGITUDE = os.getenv("KSF_LONGITUDE", "117.43306206597222")
DEFAULT_DEVICE_TOKEN = os.getenv("KSF_DEVICE_TOKEN", "")
DEFAULT_IMPERSONATE = os.getenv("KSF_IMPERSONATE", "safari184")

MINI_REFERER = f"https://servicewechat.com/{APPID}/848/page-frame.html"
USER_AGENT_MINI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b35) NetType/WIFI Language/zh_CN"
)
USER_AGENT_H5 = USER_AGENT_MINI + f" miniProgram/{APPID}"

敏感键 = re.compile(
    r"(token|authorization|cookie|session|openid|unionid|phone|mobile|code|encrypted|cipher|"
    r"password|secret|qrcode|headimg|nickname|memberid|consumerid|userid)",
    re.I,
)


# ───────────────────────── 通用工具（照搬源脚本） ─────────────────────────

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
    if text.upper().startswith(("HTTP://", "HTTPS://")):
        return text
    tail = text.rstrip("/").split("/")[-1]
    return f"HTTPS://3.KSF.CN/X/L/{tail}"


def 展示瓶盖码(value: str) -> str:
    tail = str(value or "").rstrip("/").split("/")[-1]
    return 脱敏(tail, keep=4)


def 读取瓶盖码(inline_codes: str = "") -> list[str]:
    seen: set[str] = set()
    codes: list[str] = []
    for item in 分割条目(inline_codes):
        code = 规范瓶盖码(item)
        key = code.upper()
        if code and key not in seen:
            seen.add(key)
            codes.append(code)
    return codes


# ───────────────────────── 响应解析（照搬源脚本） ─────────────────────────

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
    for source in (data, obj):
        if isinstance(source, dict):
            for key in ("bizMessage", "message", "msg", "errorMsg"):
                value = source.get(key)
                if value:
                    return str(value)
    return str(obj.get("message") or obj.get("msg") or obj.get("code") or "")


def 抽奖奖品列表(obj: Any) -> list[dict[str, Any]]:
    data = obj.get("data") if isinstance(obj, dict) else None
    rewards = data.get("luckyRewardList") if isinstance(data, dict) else []
    return [item for item in rewards if isinstance(item, dict)] if isinstance(rewards, list) else []


def 是否已扫过或消耗(obj: Any) -> bool:
    text = 响应消息(obj)
    return any(key in text for key in ("已扫码", "已参与", "重复", "已使用", "已兑换", "已领取"))


def 扫码是否已给抽奖结果(obj: Any) -> bool:
    text = 结果摘要(obj)
    return any(key in text for key in ("谢谢惠顾", "已中奖", "不能重复抽奖", "已扫过", "已参与", "重复抽奖"))


def 是否限制结果(obj: Any) -> bool:
    text = 响应消息(obj)
    return any(key in text for key in ("上限", "太频繁", "风险", "机会不足", "没有抽奖机会", "限制"))


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
                    names.append(str(item.get("rewardName") or item.get("name") or item.get("rewardTypeTxt") or "奖品"))
            return "中奖：" + "、".join(names)
        parts = []
        for key in ("bizMessage", "campaignName", "activityType", "productName"):
            value = data.get(key)
            if value:
                parts.append(str(value))
        if parts:
            return "；".join(parts)
    return 响应消息(obj) or json.dumps(脱敏对象(obj), ensure_ascii=False)[:180]


def 奖品名称(item: dict[str, Any]) -> str:
    return str(
        item.get("rewardName")
        or item.get("name")
        or item.get("rewardTypeTxt")
        or item.get("productName")
        or item.get("bizMessage")
        or "奖品"
    )


# ───────────────────────── 核心运行器（照搬源脚本 康师傅Runner） ─────────────────────────

class KsfRunner:
    """单账号运行器：持有本次运行的（可能带浏览器 TLS 指纹的）Session、代理、日志与配置。

    业务方法逐字节照搬源脚本 `康师傅Runner`；args 用 SimpleNamespace 承接原来的命令行参数，
    仅把外部应用宝 Code 服务替换为内部 codebridge。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, openid: str, appid: str,
                 initial_code: str, proxy_url: str = "", params: dict | None = None) -> None:
        self.loop = loop
        self.openid_ref = openid          # codebridge 用的微信 openid（取码用）
        self.appid = appid or APPID
        self.initial_code = initial_code
        self.params = params or {}
        self.lines: list[str] = []

        p = self.params
        self.args = SimpleNamespace(
            latitude=str(p.get("latitude") or DEFAULT_LATITUDE),
            longitude=str(p.get("longitude") or DEFAULT_LONGITUDE),
            device_token=(p.get("deviceToken") if p.get("deviceToken") is not None else DEFAULT_DEVICE_TOKEN) or "",
            campaign_code=str(p.get("campaignCode") or CAMPAIGN_CODE),
            impersonate=str(p.get("impersonate") or DEFAULT_IMPERSONATE),
            timeout=int(p.get("timeout") or 20),
            member_token=str(p.get("memberToken") or os.getenv("KSF_MEMBER_TOKEN", "")),
            token=str(p.get("token") or os.getenv("KSF_TOKEN", "")),
            encrypted_data=str(p.get("encryptedData") or os.getenv("KSF_ENCRYPTED_DATA", "")),
            dry_run=False,
        )
        try:
            self.interval = max(float(p.get("interval", 5.0)), 0.0)
        except (TypeError, ValueError):
            self.interval = 5.0
        self.double_scan = _bool_param(p, "doubleScan", False)
        self.try_active = _bool_param(p, "tryActive", True)
        self.auto_member_auth = _bool_param(p, "autoMemberAuth", True)

        # curl_cffi 缺失时优雅回退到 requests（源脚本会直接报错；此处降级为普通 TLS）
        if curl_requests is not None:
            self.session = curl_requests.Session(impersonate=self.args.impersonate)
            self.tls_ok = True
        elif plain_requests is not None:
            self.session = plain_requests.Session()
            self.tls_ok = False
        else:  # pragma: no cover - 两者都缺才会到这
            raise RuntimeError("缺少 requests / curl_cffi，无法发起康师傅业务请求")

        self.member_token = self.args.member_token or ""
        self.promotion_token = self.args.token or ""
        self.openid = ""
        self.unionid = ""
        self.last_encrypted_data = ""
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    # ---------------- 日志 ----------------
    def log(self, line: str) -> None:
        t = str(line or "").strip()
        if not t:
            return
        if not re.match(r"^\[[A-Z]+\]", t):
            t = f"[INFO] {t}"
        self.lines.append(t)
        _emit(t)

    # ---------------- codebridge 取码桥（异步 -> 主事件循环） ----------------
    def _await(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def _fresh_wx_code(self) -> str:
        from . import codebridge

        r = self._await(codebridge.get_code_for_openid(self.openid_ref, self.appid))
        if r.get("success") and r.get("code"):
            return str(r["code"])
        raise RuntimeError(f"取 wx.login code 失败：{r.get('error') or json.dumps(r, ensure_ascii=False)[:200]}")

    # ---------------- headers ----------------
    def nclub_headers(self, token: str | None = None) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT_MINI,
            "Referer": MINI_REFERER,
            "Token": token if token is not None else self.member_token,
        }

    def h5_referer(self, mask_code: str = "", encrypted_data: str = "") -> str:
        timestamp = 当前毫秒()
        if encrypted_data:
            return (
                "https://3.ksf.cn/h5OssSrc/1/zip/f7b2901d155e4f94b482ae49f7528807/index.html"
                f"?userInfo={quote(encrypted_data, safe='')}&timestamp={timestamp}"
            )
        if mask_code:
            return (
                "https://3.ksf.cn/h5OssSrc/1/zip/e29946ca7fa241688a6905bcec537e6f/index.html"
                f"?actid=18&maskcode={quote(mask_code, safe=':/')}&timestamp={timestamp}"
            )
        return "https://3.ksf.cn/h5OssSrc/1/zip/e29946ca7fa241688a6905bcec537e6f/index.html"

    def promotion_headers(self, token: str | None = None, mask_code: str = "", encrypted_data: str = "") -> dict[str, str]:
        bearer = token if token is not None else self.promotion_token
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": PROMOTION_BASE,
            "Referer": self.h5_referer(mask_code=mask_code, encrypted_data=encrypted_data),
            "User-Agent": USER_AGENT_H5,
            "Authorization": f"Bearer {bearer}" if bearer else "",
        }

    # ---------------- 请求封装 ----------------
    def request_json(
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        allow_business_fail: bool = False,
    ) -> dict[str, Any]:
        resp = self.session.request(
            method,
            url,
            json=body,
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

    # ---------------- 会员登录链路 ----------------
    def 会员登录(self, wx_code: str) -> dict[str, Any]:
        if self.member_token and self.args.member_token:
            return {"code": 0, "msg": "已使用手动 member token", "data": {"token": self.member_token}}
        body = {
            "code": wx_code,
            "inviterId": "",
            "inviterType": 1,
            "inviterMatchUserId": "",
            "spUrl": None,
        }
        obj = self.request_json(
            "POST",
            f"{NCLUB_BASE}/login/login",
            body=body,
            headers=self.nclub_headers(token=""),
        )
        data = obj.get("data") if isinstance(obj, dict) else {}
        if not isinstance(data, dict):
            raise RuntimeError(f"会员登录返回异常：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]}")
        if not data.get("token"):
            return obj
        self.member_token = str(data.get("token") or "")
        member = data.get("member") if isinstance(data.get("member"), dict) else {}
        self.openid = str(member.get("openid") or self.openid)
        self.unionid = str(member.get("unionid") or self.unionid)
        return obj

    def 会员补授权资料(self, member: dict[str, Any]) -> dict[str, Any]:
        member_id = str(member.get("id") or member.get("memberId") or "")
        if not member_id:
            raise RuntimeError("会员登录返回缺少 member.id，无法补授权资料")
        nickname = str(member.get("nickname") or "").strip()
        if not nickname or re.fullmatch(r"user[_-]?\d*", nickname, flags=re.I):
            nickname = f"用户{member_id[-8:]}"
        body = {
            "headimg": str(member.get("headimg") or "https://cluboss.ksf.cn/images/2022newui/November/publicUserBg.png"),
            "memberId": member_id,
            "nickname": nickname,
            "channelCode": None,
        }
        return self.request_json(
            "PUT",
            f"{NCLUB_BASE}/login/memberAuthSetInfo",
            body=body,
            headers=self.nclub_headers(token=""),
        )

    # ---------------- 活动登录链路 ----------------
    def 生成_encrypted_data(self, mask_code: str) -> str:
        if self.args.encrypted_data:
            self.last_encrypted_data = self.args.encrypted_data
            return self.last_encrypted_data
        body = {
            "code": mask_code,
            "actId": "",
            "userRewardId": "",
            "actTemplateId": "",
            "lat": float(self.args.latitude),
            "lng": float(self.args.longitude),
        }
        obj = self.request_json(
            "POST",
            f"{CLUB_BASE}/third/ac/ciphertext",
            body=body,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT_MINI,
                "Referer": MINI_REFERER,
                "Token": self.member_token,
            },
        )
        encrypted_data = str(obj.get("data") or "")
        if not encrypted_data:
            raise RuntimeError("ciphertext 未返回 encryptedData")
        self.last_encrypted_data = encrypted_data
        return encrypted_data

    def 活动登录(self, encrypted_data: str, mask_code: str = "") -> dict[str, Any]:
        if self.promotion_token and self.args.token:
            return {"success": True, "message": "已使用手动 promotion token", "data": {"token": self.promotion_token}}
        obj = self.request_json(
            "POST",
            f"{PROMOTION_BASE}/openapi/promotion/consumer/auth/thirdLogin",
            body={"encryptedData": encrypted_data},
            headers=self.promotion_headers(token="", mask_code=mask_code, encrypted_data=encrypted_data),
        )
        data = obj.get("data") if isinstance(obj, dict) else {}
        if not isinstance(data, dict) or not data.get("token"):
            raise RuntimeError(f"thirdLogin 未返回 token：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]}")
        self.promotion_token = str(data.get("token") or "")
        self.openid = str(data.get("openId") or self.openid)
        self.unionid = str(data.get("unionId") or self.unionid)
        return obj

    def 活动请求(self, path: str, body: Any, mask_code: str = "", allow_business_fail: bool = False) -> dict[str, Any]:
        return self.request_json(
            "POST",
            f"{PROMOTION_BASE}/openapi/promotion/{path.lstrip('/')}",
            body=body,
            headers=self.promotion_headers(mask_code=mask_code),
            allow_business_fail=allow_business_fail,
        )

    def 扫码(self, mask_code: str, campaign_code: Any = "", last_scan_result: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "maskCode": mask_code,
            "mobilePhone": True,
            "lat": float(self.args.latitude),
            "lng": float(self.args.longitude),
            "campaignCode": campaign_code,
            "lastScanResult": last_scan_result,
        }
        if self.args.device_token and not last_scan_result:
            body["deviceToken"] = self.args.device_token
        return self.活动请求("campaignExecute/scanMaskCode", body, mask_code=mask_code, allow_business_fail=True)

    def 抽奖(self, mask_code: str) -> dict[str, Any]:
        body = {
            "maskCode": mask_code,
            "lat": float(self.args.latitude),
            "lng": float(self.args.longitude),
        }
        return self.活动请求("campaignExecute/maskCodeLottery", body, mask_code=mask_code, allow_business_fail=True)

    def 领取奖品(self, user_reward_id: str) -> dict[str, Any]:
        return self.活动请求("consumer/receiveReward", {"userRewardId": user_reward_id}, allow_business_fail=True)

    def 生成核销二维码(self, card_ids: list[str]) -> dict[str, Any]:
        body = {
            "lat": float(self.args.latitude),
            "lng": float(self.args.longitude),
            "cardIds": card_ids,
        }
        return self.活动请求("verification/getReportQrcode", body, allow_business_fail=True)

    def 激励金探测(self) -> dict[str, Any]:
        return self.活动请求("campaignExecute/tryActiveIncentive", {}, allow_business_fail=True)

    # ---------------- 单账号编排 ----------------
    def 准备活动身份(self, mask_code: str) -> dict[str, Any]:
        encrypted_data = self.生成_encrypted_data(mask_code)
        return self.活动登录(encrypted_data, mask_code=mask_code)

    def 登录(self) -> None:
        """会员登录：无 token 时自动补授权资料并用新 wx.login code 重登（照搬源脚本 执行账号 前半段）。"""
        wx_code = ""
        if not self.args.member_token:
            wx_code = self.initial_code
        login_obj = self.会员登录(wx_code)
        login_data = login_obj.get("data") if isinstance(login_obj, dict) else {}
        if not self.args.member_token and isinstance(login_data, dict) and not login_data.get("token"):
            member = login_data.get("member") if isinstance(login_data.get("member"), dict) else {}
            if member and self.auto_member_auth:
                self.log("[WARNING] 会员登录未返回 token：检测到需补授权资料，正在自动处理…")
                self.会员补授权资料(member)
                wx_code = self._fresh_wx_code()
                self.log("[INFO] 已补授权资料，重新获取 wx.login code 后重登")
                login_obj = self.会员登录(wx_code)
        if not self.args.member_token and not self.member_token:
            raise RuntimeError(f"会员登录未返回 token：{json.dumps(脱敏对象(login_obj), ensure_ascii=False)[:240]}")
        self.log("[SUCCESS] 康师傅会员登录成功")

    def 处理一个码(self, idx: int, total: int, mask_code: str) -> dict[str, Any] | None:
        """返回中奖记录（用于后续统一核销）或 None。异常已在内部转 [ERROR] 行。"""
        self.log(f"[INFO] [{idx}/{total}] 康师傅码 {展示瓶盖码(mask_code)}")
        try:
            if not self.args.token:
                self.准备活动身份(mask_code)
            first_scan = self.扫码(mask_code, campaign_code="", last_scan_result=False)
            self.log(f"[INFO] 码{idx} 扫码：{结果摘要(first_scan)}")
            if 是否限制结果(first_scan):
                self.log(f"[WARNING] 码{idx} 检测到当日扫码已达上限/受限，停止处理后续码")
                return {"__stop__": True}

            scan_final = 扫码是否已给抽奖结果(first_scan)
            if self.double_scan and not scan_final:
                self.扫码(mask_code, campaign_code=None, last_scan_result=True)
            if self.try_active and not scan_final:
                self.激励金探测()

            if scan_final:
                lottery_obj = first_scan
            else:
                lottery_obj = self.抽奖(mask_code)
                self.log(f"[INFO] 码{idx} 抽奖：{结果摘要(lottery_obj)}")

            rewards = 抽奖奖品列表(lottery_obj)
            if rewards:
                names = "、".join(奖品名称(item) for item in rewards)
                self.log(f"[SUCCESS] 码{idx} 中奖：{names}")
                for item in rewards:
                    urid = str(item.get("userRewardId") or "")
                    if not urid:
                        self.log(f"[WARNING] 码{idx} 中奖 {奖品名称(item)} 但未返回 userRewardId，无法核销")
                        continue
                    return {
                        "mask_code": mask_code,
                        "reward_name": 奖品名称(item),
                        "user_reward_id": urid,
                        "promotion_token": self.promotion_token,
                    }
            else:
                self.log(f"[INFO] 码{idx} 未中奖：{结果摘要(lottery_obj)}")
                if 是否限制结果(lottery_obj):
                    self.log(f"[WARNING] 码{idx} 抽奖受限，停止处理后续码")
                    return {"__stop__": True}
            return None
        except Exception as exc:
            self.log(f"[ERROR] 码{idx} 执行失败：{脱敏手机号(str(exc))}")
            return None

    def 核销中奖(self, winner: dict[str, Any]) -> dict[str, Any]:
        """receiveReward + getReportQrcode，返回可展示的核销信息（不落盘，二维码作为字段给出）。"""
        reward_name = str(winner.get("reward_name") or "康师傅奖品")
        urid = str(winner.get("user_reward_id") or "")
        info: dict[str, Any] = {"奖品": reward_name, "瓶盖码": 展示瓶盖码(winner.get("mask_code"))}
        try:
            recv = self.领取奖品(urid)
            info["领取结果"] = 结果摘要(recv)
        except Exception as exc:
            info["领取结果"] = f"领取异常：{脱敏手机号(str(exc))}"
        try:
            qr = self.生成核销二维码([urid])
            data = qr.get("data") if isinstance(qr, dict) else None
            if isinstance(data, dict):
                qr_text = str(data.get("qrcode") or "")
                qr_url = str(data.get("qrcodeUrl") or "")
                deadline = str(data.get("deadline") or "")
                if deadline:
                    info["核销截止"] = deadline
                if qr_url:
                    info["核销二维码URL"] = qr_url
                if qr_text.startswith("data:image/"):
                    # 不落盘：直接把 data URI 作为字段给出，前端可 <img> 渲染
                    info["核销二维码图片"] = qr_text
                    info["核销二维码格式"] = "base64 data-uri（未落盘）"
                elif qr_text:
                    info["核销二维码"] = qr_text
                if not (qr_url or qr_text):
                    info["核销二维码"] = f"接口未返回二维码：{结果摘要(qr)}"
            else:
                info["核销二维码"] = f"接口未返回 data：{结果摘要(qr)}"
        except Exception as exc:
            info["核销二维码"] = f"核销异常：{脱敏手机号(str(exc))}"
        # 日志脱敏：不把长 base64 打进日志，仅提示可用信息
        log_bits = [reward_name]
        if info.get("核销二维码URL"):
            log_bits.append(f"二维码URL={info['核销二维码URL']}")
        elif info.get("核销二维码图片"):
            log_bits.append("二维码=base64 data-uri（见结果字段，未落盘）")
        if info.get("核销截止"):
            log_bits.append(f"截止={info['核销截止']}")
        self.log("[SUCCESS] 核销二维码已生成：" + "，".join(log_bits))
        return info

    def run(self, codes: list[str]) -> dict[str, Any]:
        self.登录()
        winners: list[dict[str, Any]] = []
        total = len(codes)
        stop = False
        for idx, mask_code in enumerate(codes, 1):
            rec = self.处理一个码(idx, total, mask_code)
            if isinstance(rec, dict) and rec.get("__stop__"):
                stop = True
                break
            if rec:
                winners.append(rec)
            if idx < total and self.interval > 0:
                time.sleep(self.interval)

        winner_display: list[dict[str, Any]] = []
        if winners:
            self.log(f"[INFO] 共 {len(winners)} 个中奖，开始统一核销…")
            for winner in winners:
                winner_display.append(self.核销中奖(winner))
        else:
            self.log("[INFO] 本次没有中奖记录")

        self.log(
            f"[INFO] 运行结束：处理 {min(len(codes), (idx if codes else 0))} 个码"
            f"{'（中途受限提前停止）' if stop else ''}，中奖 {len(winners)} 个"
        )
        return {"winners": winner_display, "winnerCount": len(winners), "stopped": stop}


# ───────────────────────── 参数与入口 ─────────────────────────

def _bool_param(params: dict, key: str, default: bool) -> bool:
    if key not in params:
        return default
    value = params.get(key)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


async def run_ksf_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from . import db

    params = params or {}
    target_appid = params.get("appid") or appid or APPID

    codes = 读取瓶盖码(params.get("codes") or params.get("sn") or params.get("value") or "")
    if not codes:
        return {"ok": False, "stage": "params", "error": "请输入康师傅瓶盖码链接"}

    # 代理：优先本次表单填写的 proxyUrl；留空则回退到该微信账号扫码时绑定的地区代理，
    # 让康师傅业务接口与账号同地区出网，避免异地 IP 触发风控。
    _acc = db.query_one("SELECT proxy_url, nickname FROM accounts WHERE openid=?", (openid,))
    proxy_url = (params.get("proxyUrl") or "").strip() or ((_acc["proxy_url"] or "") if _acc else "")
    account_disp = 脱敏(_acc["nickname"] if (_acc and _acc["nickname"]) else openid)

    # 先取 code（会员登录种子）；失败直接返回 get-code 错误
    from .codebridge import get_code_for_openid

    cr = await get_code_for_openid(openid, target_appid)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "取康师傅 wx.login code 失败"}

    loop = asyncio.get_running_loop()
    runner = KsfRunner(
        loop=loop,
        openid=openid,
        appid=target_appid,
        initial_code=str(cr["code"]),
        proxy_url=proxy_url,
        params=params,
    )
    if not runner.tls_ok:
        runner.log("[WARNING] 未安装 curl_cffi，已回退普通 TLS 指纹；如遇风控请安装 curl_cffi")

    try:
        summary = await asyncio.to_thread(runner.run, codes)
    except Exception as exc:
        # 兜底：把已产生的日志随错误一起带回，便于定位
        runner.log(f"[ERROR] 运行异常：{脱敏手机号(str(exc))}")
        return {"ok": False, "stage": "ksf", "error": str(exc), "cookie": "\n".join(runner.lines)}

    display = {
        "account": account_disp,
        "viaProxy": bool(proxy_url),
        "snCount": len(codes),
        "winnerCount": summary.get("winnerCount", 0),
        "winners": summary.get("winners", []),
    }
    return {
        "ok": True,
        "stage": "ksf",
        "account": account_disp,
        "viaProxy": bool(proxy_url),
        "snCount": len(codes),
        "ksfResult": json.dumps(display, ensure_ascii=False, indent=2),
        # 复用内置项目结果文本框：统一成 [LEVEL] 日志行
        "cookie": "\n".join(runner.lines),
    }
