"""Built-in project: 瑞幸咖啡活动抽奖。

把独立脚本「瑞幸咖啡.py」移植到 app 的内置项目模型：用户选一个已绑定的微信账号，
后端用 codebridge 取 wx.login code（登录若需手机号授权，再取一次 getPhoneNumber 的
iv/encryptedData），登录换 uid/userId/openid -> getAuthCode -> H5 open/check ->
活动详情 -> 抽奖 -> 查询中奖记录 -> 汇总。

业务链路（AES-ECB q 加密、md5 sign、capi/mkt 接口）忠实照搬原脚本；仅把取码从外部服务
换成内部 codebridge，AES 改用 cryptography（已是项目依赖，免装 pycryptodome）。原脚本是
同步 requests，这里保留其结构放到线程里跑，异步 codebridge 通过事件循环桥回。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import string
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import requests
import urllib3
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

APPID = "wx21c7506e98a2fe75"
APP_VERSION = "916"
MINI_VERSION = "5572"
AKV = "lk-wxmp-v5.3.22"
API_KEY = "CJQjAc1hYieC4QYb"
CID = "230101"
DK = 1

# 默认活动（幸运星期三 0708）。实际运行时优先用项目 run_config / 表单传入的 activityNo/activityId，
# 这里只是 run_config 未带活动参数时的兜底，见 main.py:LUCKIN_ACTIVITIES。
ACTIVITY_NO = os.getenv("LK_ACTIVITY_NO", "CJ202607029027751995")
ACTIVITY_ID = int(os.getenv("LK_ACTIVITY_ID", "1367"))
BRAND_TYPE = "LK001"

CAPI_BASE = "https://capi.lkcoffee.com"
MKT_BASE = "https://mkt.lkcoffee.com"

UA_CAPI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b34) NetType/WIFI Language/zh_CN"
)
UA_MKT = UA_CAPI + f" miniProgram/{APPID}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mask_phone(text: Any) -> str:
    text = str(text or "")
    return re.sub(r"(1\d{2})\d{4}(\d{4})", r"\1****\2", text)


# ---------------- 加密 / 签名（AES-ECB + MD5，忠实移植） ----------------
def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    data = data + bytes([pad_len]) * pad_len
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(data) + dec.finalize()
    pad_len = out[-1]
    return out[:-pad_len]


def aes_encrypt_urlsafe(text: str) -> str:
    raw = _aes_ecb_encrypt(text.encode("utf-8"), API_KEY.encode("utf-8"))
    return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_")


def aes_decrypt_urlsafe(text: str) -> str:
    s = str(text or "").replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    raw = _aes_ecb_decrypt(base64.b64decode(s), API_KEY.encode("utf-8"))
    return raw.decode("utf-8")


def md5_words(text: Any) -> str:
    digest = hashlib.md5(str(text).encode("utf-8")).digest()
    parts = []
    for i in range(0, 16, 4):
        n = int.from_bytes(digest[i : i + 4], "big", signed=True)
        parts.append(str(abs(n)))
    return "".join(parts)


def build_payload(data: dict | None, uid: str = "") -> dict:
    body = dict(data or {})
    if "miniversion" not in body:
        body["miniversion"] = MINI_VERSION
    plain = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    q = aes_encrypt_urlsafe(plain)
    payload = {"cid": CID, "q": q, "dk": DK}
    sign_parts = [f"cid={CID}", f"dk={DK}", f"q={q}"]
    if uid:
        payload["uid"] = str(uid)
        sign_parts.append(f"uid={uid}")
    sign_input = ";".join(sign_parts) + API_KEY
    payload["sign"] = md5_words(sign_input)
    return payload


def decrypt_capi_resp(text: str) -> dict:
    text = str(text or "")
    if not text:
        return {}
    if text.lstrip().startswith("{"):
        return json.loads(text)
    try:
        return json.loads(aes_decrypt_urlsafe(text))
    except Exception as exc:
        raise RuntimeError(f"瑞幸响应解密失败：{text[:80]}") from exc


def _rand(chars: str, length: int) -> str:
    return "".join(random.choice(chars) for _ in range(length))


def _gen_blackbox(prefix: str = "uMPHR") -> str:
    return f"{prefix}{int(time.time())}{_rand(string.ascii_letters + string.digits, 12)}"


def _gen_did() -> str:
    return _rand(string.ascii_lowercase + string.digits, 32)


def _gen_device_id() -> str:
    return _rand(string.ascii_letters + string.digits + "+/", 48)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _history_summary(records: Any, error: str = "") -> str:
    if error:
        return f"历史中奖记录查询失败：{_mask_phone(error)}"
    if not isinstance(records, list) or not records:
        return "历史中奖记录：暂无"
    parts = []
    for item in records[:3]:
        if not isinstance(item, dict):
            continue
        prize = (item.get("prizeName") or item.get("couponName") or item.get("name")
                 or item.get("prizeDesc") or item.get("title"))
        when = item.get("createTime") or item.get("receiveTime") or item.get("winTime") or ""
        if prize:
            parts.append(f"{prize}({when})" if when else str(prize))
    if not parts:
        return f"历史中奖记录：{len(records)} 条，未识别奖品名"
    suffix = f" 等 {len(records)} 条" if len(records) > len(parts) else ""
    return "历史中奖记录：" + "；".join(parts) + suffix


def _draw_summary(message: str, records=None, records_error: str = "") -> str:
    msg = _clean(message)
    history = _history_summary(records or [], records_error)
    if not msg:
        return history
    if "很遗憾" in msg or "没有抽中" in msg:
        return f"未中奖：{msg}\n{history}"
    if "次数上限" in msg or "不可再参加" in msg:
        return f"今日已达参与次数上限：{msg}\n{history}"
    return f"抽奖结果：{msg}\n{history}"


class LuckinRunner:
    """单账号运行器：持有带代理的 Session、事件循环桥、日志；业务逻辑照搬原脚本。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, openid: str, proxy_url: str = "",
                 params: dict | None = None) -> None:
        self.loop = loop
        self.openid = openid
        self.params = params or {}
        self.timeout = int(self.params.get("timeout") or 15)
        self.lines: list[str] = []
        self.activity_no = str(self.params.get("activityNo") or ACTIVITY_NO)
        self.activity_id_default = int(self.params.get("activityId") or ACTIVITY_ID)
        self.s = requests.Session()
        self.s.verify = False
        if proxy_url:
            self.s.proxies.update({"http": proxy_url, "https": proxy_url})
        self._reset_identity()

    def _reset_identity(self) -> None:
        self.csid = str(uuid.uuid4())
        self.blackbox = _gen_blackbox("uMPHR")
        self.did = _gen_did()
        self.h5_blackbox = _gen_blackbox("uWPHA")
        self.device_id = _gen_device_id()
        self.user_id = ""
        self.uid = ""
        self.openid_lk = ""
        self.auth_code = ""

    def log(self, text: str) -> None:
        self.lines.append(text)
        from .logsink import emit
        emit(text)

    def _await(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    # ---------------- codebridge 桥 ----------------
    def get_wx_code(self) -> str:
        from . import codebridge

        r = self._await(codebridge.get_code_for_openid(self.openid, APPID))
        if r.get("success") and r.get("code"):
            return str(r["code"])
        raise RuntimeError(f"wx.login失败: {r.get('error') or json.dumps(r, ensure_ascii=False)[:200]}")

    def get_phone_info(self) -> dict:
        from . import codebridge

        r = self._await(codebridge.get_phone_for_openid(self.openid, APPID))
        iv = r.get("iv") or ""
        enc = r.get("encryptedData") or ""
        if not iv or not enc:
            raise RuntimeError(f"获取手机号授权失败: {r.get('error') or json.dumps(r, ensure_ascii=False)[:200]}")
        return {"iv": str(iv), "encryptedData": str(enc)}

    # ---------------- capi（小程序主接口） ----------------
    def capi_headers(self, mid: str = "") -> dict:
        headers = {
            "User-Agent": UA_CAPI,
            "Referer": f"https://servicewechat.com/{APPID}/{APP_VERSION}/page-frame.html",
            "content-type": "application/x-www-form-urlencoded",
            "X-LK-CSID": self.csid,
            "X-LK-AKV": AKV,
            "x-lkwx-sdkversion": "3.16.1",
            "x-lkwx-ostype": "ios",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if mid:
            headers["X-LK-MID"] = str(mid)
        return headers

    def capi_request(self, method: str, path: str, data=None, mid: str = "", uid: str = "") -> dict:
        url = CAPI_BASE + path
        payload = build_payload(data or {}, uid=uid)
        kwargs = {"headers": self.capi_headers(mid), "timeout": self.timeout}
        if method.upper() == "GET":
            kwargs["params"] = payload
        else:
            kwargs["data"] = payload
        r = self.s.request(method, url, **kwargs)
        obj = decrypt_capi_resp(r.text)
        if obj.get("code") != 1:
            msg = obj.get("msg") or obj.get("busiCode") or json.dumps(obj, ensure_ascii=False)[:160]
            raise RuntimeError(f"瑞幸接口失败：{msg}")
        return obj

    def login(self, wx_code: str, phone_info: dict | None = None) -> dict:
        data = {
            "code": wx_code,
            "isAuthorization": False,
            "blackBox": self.blackbox,
            "did": self.did,
            "deptId": "",
        }
        if phone_info:
            data["iv"] = phone_info.get("iv") or ""
            data["encryptedData"] = phone_info.get("encryptedData") or ""
        obj = self.capi_request("POST", "/resource/m/user/wxminilogin", data)
        content = obj.get("content") or {}
        if content.get("needAuthorized"):
            raise RuntimeError("瑞幸登录需要手机号授权：needAuthorized")
        self.uid = str(obj.get("uid") or "")
        self.user_id = str(content.get("userId") or "")
        self.openid_lk = str(content.get("openid") or "")
        if not self.user_id or not self.openid_lk:
            msg = content.get("msg") or obj.get("msg") or "未返回 userId/openid"
            raise RuntimeError(f"瑞幸登录失败：{msg}")
        return content

    def get_auth_code(self) -> str:
        origin_url = (
            f"{MKT_BASE}/ladder/draw-series/11rgg68x"
            f"?activityNo={self.activity_no}&miniversion={MINI_VERSION}"
            f"&frommini=mini&brandType={BRAND_TYPE}&origin=27&userId={self.user_id}"
        )
        data = {
            "originUrl": origin_url,
            "openAuthRms": {"openId": self.openid_lk, "blackBox": self.blackbox, "longitude": "", "latitude": ""},
        }
        obj = self.capi_request("GET", "/resource/m/open/getAuthCode", data, mid=self.user_id, uid=self.uid)
        code = str(((obj.get("content") or {}).get("code") or "")).strip()
        if not code:
            raise RuntimeError("getAuthCode 未返回 authCode")
        self.auth_code = code
        return code

    # ---------------- mkt（H5 活动接口） ----------------
    def h5_url(self) -> str:
        return (
            f"{MKT_BASE}/ladder/draw-series/11rgg68x"
            f"?activityNo={self.activity_no}&miniversion={MINI_VERSION}"
            f"&frommini=mini&brandType={BRAND_TYPE}&origin=27"
            f"&userId={self.user_id}&authCode={self.auth_code}&userType=0"
        )

    def mkt_headers(self) -> dict:
        return {
            "User-Agent": UA_MKT,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.h5_url(),
        }

    def mkt_request(self, path: str, query: dict) -> dict:
        query = dict(query or {})
        query["_"] = _now_ms()
        query_str = json.dumps(query, ensure_ascii=False, separators=(",", ":"))
        url = f"{MKT_BASE}{path}?{urlencode({'queryParamsStr': query_str})}"
        r = self.s.get(url, headers=self.mkt_headers(), timeout=self.timeout)
        try:
            obj = r.json()
        except Exception as exc:
            raise RuntimeError(f"H5 接口未返回 JSON：HTTP {r.status_code} {r.text[:120]}") from exc
        if obj.get("code") not in (1, None) and not obj.get("success"):
            raise RuntimeError(obj.get("msg") or obj.get("busiCode") or json.dumps(obj, ensure_ascii=False)[:160])
        return obj

    def open_and_check(self) -> dict:
        self.s.get(self.h5_url(), headers={"User-Agent": UA_MKT}, timeout=self.timeout)
        obj = self.mkt_request("/ladder/capi/resource/m/open/check", {"loading": False, "code": self.auth_code})
        content = obj.get("content") or {}
        if not content.get("checked"):
            raise RuntimeError(obj.get("msg") or "open/check 未通过")
        return content

    def activity_detail(self, activity_id: str = "") -> dict:
        obj = self.mkt_request(
            "/ladder/skcapi/resource/bff/v2/lotteryDraw/detail",
            {"activityId": activity_id, "activityNo": self.activity_no, "handleMsg": False},
        )
        return obj.get("content") or {}

    def draw(self, activity_id) -> dict:
        obj = self.mkt_request(
            "/ladder/skcapi/resource/m/lotteryDraw/action",
            {
                "blackBox": self.h5_blackbox,
                "deviceId": self.device_id,
                "activityId": int(activity_id),
                "activityNo": self.activity_no,
                "origin": 14,
                "handleMsg": False,
                "version": int(MINI_VERSION),
            },
        )
        return obj.get("content") or {}

    def my_records(self) -> list:
        obj = self.mkt_request(
            "/ladder/skcapi/resource/bff/lotteryDraw/memberLotteryRecord",
            {"activityNo": self.activity_no, "pageIndex": 0, "pageSize": 100},
        )
        return obj.get("content") or []

    def safe_records(self):
        try:
            return self.my_records(), ""
        except Exception as exc:
            return [], str(exc)

    # ---------------- 单次抽奖流程 ----------------
    def _draw_flow(self, wx_code: str, phone_info: dict | None = None) -> str:
        login_info = self.login(wx_code, phone_info=phone_info)
        self.log(f"[INFO] 登录成功{('，手机号授权已同步' if phone_info else '')}")
        self.get_auth_code()
        self.log("[INFO] 已获取 authCode")
        check_info = self.open_and_check()
        self.log("[INFO] 活动校验通过")
        detail = self.activity_detail("")
        activity_id = detail.get("activityId") or self.activity_id_default
        status = detail.get("activityLotteryStatus")
        self.log(f"[INFO] 开始抽奖（activityId={activity_id}，状态={status}）")

        result = self.draw(activity_id)
        message = result.get("prizeName") or result.get("notHitPrizeReasonMsg") or ""
        records, records_error = self.safe_records()
        if message:
            return _draw_summary(message, records, records_error)
        if records:
            return _history_summary(records)
        mobile = _mask_phone(login_info.get("mobile") or check_info.get("mobile") or "")
        suffix = f"；账号 {mobile}" if mobile else ""
        return f"抽奖完成，但未识别奖品，活动状态 {status}{suffix}；{_history_summary(records, records_error)}"

    def _log_summary(self, summary: str) -> None:
        lines = [ln for ln in str(summary).split("\n") if ln.strip()]
        if not lines:
            self.log("[INFO] （无结果）")
            return
        head = lines[0]
        if "未中奖" in head or "上限" in head:
            self.log(f"[WARNING] {head}")
        elif "失败" in head or "异常" in head:
            self.log(f"[ERROR] {head}")
        else:
            self.log(f"[SUCCESS] {head}")
        for ln in lines[1:]:
            self.log(f"[INFO] {ln}")

    def run_account(self, display: str) -> dict[str, Any] | None:
        display = display or self.openid or "账号"
        self.log(f"[INFO] ▶ 账号：{display}")
        try:
            wx_code = self.get_wx_code()
            self.log("[INFO] 已获取 wx.login code")
            try:
                summary = self._draw_flow(wx_code, phone_info=None)
            except Exception as first_error:
                msg = str(first_error)
                if "信息异常" not in msg and "需要手机号授权" not in msg and "needAuthorized" not in msg:
                    raise
                self.log("[WARNING] 普通登录失败，尝试同步手机号授权后重试…")
                phone_info = self.get_phone_info()
                wx_code = self.get_wx_code()
                self._reset_identity()
                summary = self._draw_flow(wx_code, phone_info=phone_info)
            self._log_summary(summary)
            return {"success": True, "display": display, "summary": summary}
        except Exception as exc:
            self.log(f"[ERROR] 抽奖异常：{_mask_phone(exc)}")
            return None


async def run_luckin_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from . import db

    params = params or {}
    loop = asyncio.get_running_loop()
    _acc = db.query_one("SELECT proxy_url, nickname FROM accounts WHERE openid=?", (openid,))
    proxy_url = (params.get("proxyUrl") or "").strip() or ((_acc["proxy_url"] or "") if _acc else "")
    display = (_acc["nickname"] if _acc and _acc["nickname"] else openid)

    runner = LuckinRunner(loop=loop, openid=openid, proxy_url=proxy_url, params=params)
    result = await asyncio.to_thread(runner.run_account, display)

    summary = result or {"success": False, "display": display}
    text = "\n".join(runner.lines) or "（无输出）"
    return {
        "ok": True,
        "stage": "luckin",
        "account": display,
        "viaProxy": bool(proxy_url),
        "luckinResult": json.dumps(summary, ensure_ascii=False, indent=2),
        # 复用内置项目的结果文本框
        "cookie": text,
    }
