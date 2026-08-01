"""Built-in project: 红色火箭（华泰基金指慧家）签到 + ROE 口令兑换 + 红包领取。

把独立脚本「红色火箭.py」移植到 app 的内置项目模型：用户选一个已绑定的微信账号，
后端用 codebridge 取 wx.login code / 手机号授权 code / 云函数 encrypt_key（原脚本走外部
YYB 服务，这里换成内部 codebridge）。业务链路（登录→签到→动态发现活动 pageId→查 ROE→
SM4 加密 doExchange→红包 H5 领取→汇总）忠实照搬原脚本。

原脚本是同步 requests + sleep + 多处交叉取码；这里保留其同步结构放到线程里跑，通过
asyncio.run_coroutine_threadsafe 把异步 codebridge 调用桥回主事件循环。业务请求统一走
requests.Session（带账号绑定的地区代理，异地防风控）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://index.amcfortune.com"
APPID = "wx1b44c3ad181bde16"
ACTIVITY_PAGE_ID = os.getenv("HSJJ_ACTIVITY_PAGE_ID", "7541")
H5_OAUTH_APPID = "wx80226ec03be5ab6c"
H5_URLTAG = "cfyh"
AUTO_CLAIM_H5_RED_PACKET = os.getenv("HSJJ_AUTO_CLAIM_H5") != "0"
DEBUG = os.getenv("debug", "0") not in ("", "0", "false", "False")
TIMEOUT = 30

# 日志统一：已带 [LEVEL] 前缀的行原样保留；否则按行首表情判级别并剥掉装饰表情
_LEVELED_RE = re.compile(r"^\[[A-Z]+\]")
_LEAD_EMOJI = re.compile("^[℀-➿⬀-⯿️\U0001F000-\U0001FAFF\\s]+")

SM4_SBOX = [
    214, 144, 233, 254, 204, 225, 61, 183, 22, 182, 20, 194, 40, 251, 44, 5,
    43, 103, 154, 118, 42, 190, 4, 195, 170, 68, 19, 38, 73, 134, 6, 153,
    156, 66, 80, 244, 145, 239, 152, 122, 51, 84, 11, 67, 237, 207, 172, 98,
    228, 179, 28, 169, 201, 8, 232, 149, 128, 223, 148, 250, 117, 143, 63, 166,
    71, 7, 167, 252, 243, 115, 23, 186, 131, 89, 60, 25, 230, 133, 79, 168,
    104, 107, 129, 178, 113, 100, 218, 139, 248, 235, 15, 75, 112, 86, 157,
    53, 30, 36, 14, 94, 99, 88, 209, 162, 37, 34, 124, 59, 1, 33, 120, 135,
    212, 0, 70, 87, 159, 211, 39, 82, 76, 54, 2, 231, 160, 196, 200, 158,
    234, 191, 138, 210, 64, 199, 56, 181, 163, 247, 242, 206, 249, 97, 21,
    161, 224, 174, 93, 164, 155, 52, 26, 85, 173, 147, 50, 48, 245, 140, 177,
    227, 29, 246, 226, 46, 130, 102, 202, 96, 192, 41, 35, 171, 13, 83, 78,
    111, 213, 219, 55, 69, 222, 253, 142, 47, 3, 255, 106, 114, 109, 108, 91,
    81, 141, 27, 175, 146, 187, 221, 188, 127, 17, 217, 92, 65, 31, 16, 90,
    216, 10, 193, 49, 136, 165, 205, 123, 189, 45, 116, 208, 18, 184, 229,
    180, 176, 137, 105, 151, 74, 12, 150, 119, 126, 101, 185, 241, 9, 197,
    110, 198, 132, 24, 240, 125, 236, 58, 220, 77, 32, 121, 238, 95, 62, 215,
    203, 57, 72,
]

SM4_CK = [
    462357, 472066609, 943670861, 1415275113, 1886879365, 2358483617,
    2830087869, 3301692121, 3773296373, 4228057617, 404694573, 876298825,
    1347903077, 1819507329, 2291111581, 2762715833, 3234320085, 3705924337,
    4177462797, 337322537, 808926789, 1280531041, 1752135293, 2223739545,
    2695343797, 3166948049, 3638552301, 4110090761, 269950501, 741554753,
    1213159005, 1684763257,
]
SM4_MK = [2746333894, 1453994832, 1736282519, 2993693404]


@dataclass
class SessionInfo:
    token: str
    open_id: str
    union_id: str
    user_id: str
    encrypt_user_id: str = ""
    is_register: str = ""


def sm4_rotl32(value: int, n: int) -> int:
    return ((value << n) | (value >> (32 - n))) & 0xFFFFFFFF


def sm4_sbox(value: int) -> int:
    return (
        (SM4_SBOX[(value >> 24) & 255] & 255) << 24
        | (SM4_SBOX[(value >> 16) & 255] & 255) << 16
        | (SM4_SBOX[(value >> 8) & 255] & 255) << 8
        | (SM4_SBOX[value & 255] & 255)
    )


def sm4_l(value: int) -> int:
    return (value ^ sm4_rotl32(value, 2) ^ sm4_rotl32(value, 10) ^ sm4_rotl32(value, 18) ^ sm4_rotl32(value, 24)) & 0xFFFFFFFF


def sm4_l2(value: int) -> int:
    return (value ^ sm4_rotl32(value, 13) ^ sm4_rotl32(value, 23)) & 0xFFFFFFFF


def sm4_key_exp(key: bytes) -> list[int]:
    k = [0] * 36
    for i in range(4):
        k[i] = int.from_bytes(key[4 * i : 4 * i + 4], "big") ^ SM4_MK[i]
    rk: list[int] = []
    for i in range(32):
        tmp = (k[i + 1] ^ k[i + 2] ^ k[i + 3] ^ SM4_CK[i]) & 0xFFFFFFFF
        k[i + 4] = (k[i] ^ sm4_l2(sm4_sbox(tmp))) & 0xFFFFFFFF
        rk.append(k[i + 4])
    return rk


def sm4_encrypt_block(block: bytes, rk: list[int]) -> bytes:
    x = [int.from_bytes(block[4 * i : 4 * i + 4], "big") for i in range(4)]
    for i in range(0, 32, 4):
        tmp = (x[1] ^ x[2] ^ x[3] ^ rk[i]) & 0xFFFFFFFF
        x[0] = (x[0] ^ sm4_l(sm4_sbox(tmp))) & 0xFFFFFFFF
        tmp = (x[2] ^ x[3] ^ x[0] ^ rk[i + 1]) & 0xFFFFFFFF
        x[1] = (x[1] ^ sm4_l(sm4_sbox(tmp))) & 0xFFFFFFFF
        tmp = (x[3] ^ x[0] ^ x[1] ^ rk[i + 2]) & 0xFFFFFFFF
        x[2] = (x[2] ^ sm4_l(sm4_sbox(tmp))) & 0xFFFFFFFF
        tmp = (x[0] ^ x[1] ^ x[2] ^ rk[i + 3]) & 0xFFFFFFFF
        x[3] = (x[3] ^ sm4_l(sm4_sbox(tmp))) & 0xFFFFFFFF
    return b"".join(x[i].to_bytes(4, "big") for i in range(3, -1, -1))


def sm4_pkcs7_pad(data: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


def sm4_encrypt(plaintext: str, key: bytes) -> str:
    rk = sm4_key_exp(key)
    padded = sm4_pkcs7_pad(plaintext.encode("utf-8"))
    encrypted = b"".join(sm4_encrypt_block(padded[i : i + 16], rk) for i in range(0, len(padded), 16))
    return encrypted.hex()


def decode_encrypt_key(s: str) -> bytes:
    """稳健解码 encryptKey：兼容 URL-safe base64 与缺失的 `=` 填充（云函数返回常不带填充，
    直接 b64decode 会报 Incorrect padding，导致退化成明文提交后被服务端 7005 拒绝）。"""
    s = str(s or "").strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def key_bytes_from(ek: str) -> bytes:
    """把 encrypt_key 解成原始字节：优先 base64（含 url-safe / 缺填充），再尝试 hex；失败返回空。"""
    ek = str(ek or "").strip()
    if not ek:
        return b""
    try:
        b = decode_encrypt_key(ek)
        if b:
            return b
    except Exception:
        pass
    try:
        if len(ek) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in ek):
            return bytes.fromhex(ek)
    except Exception:
        pass
    return b""


def build_signature(params: dict[str, Any]) -> str:
    sorted_str = "&".join(f"{key}={params[key]}" for key in sorted(params))
    md5_hex = hashlib.md5(sorted_str.encode("utf-8")).hexdigest()
    return base64.b64encode(md5_hex.encode("utf-8")).decode("utf-8")


def build_headers(
    data: dict[str, Any],
    token: str = "",
    encrypt_ver: str = "",
    open_id: str = "",
    user_id: str = "",
    app_secret: str = "",
    register_channel: str = "",
) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    nonce = str(round(1_000_000 * random.random())) + str(round(1_000_000 * random.random()))
    sign_params = {**data, "nonce": nonce, "timestamp": timestamp, "appSecret": app_secret or "", "ticket": token or ""}
    return {
        "Content-Type": "application/json",
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": build_signature(sign_params),
        "key_version": encrypt_ver or "",
        "openid": open_id or "",
        "pro": "RedRocket",
        "Bank-Type": "main",
        "pla": "rr_windows",
        "ver": "1.48.7",
        "mini_program": "wechat",
        "register_channel": register_channel or "",
        "click_id": "",
        "user_id": user_id or "",
        "Referer": f"https://servicewechat.com/{APPID}/205/page-frame.html",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
        "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows "
        "WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254186b) XWEB/19841",
        "ticket": token or "",
    }


def format_money(value: Any) -> str:
    amount = round(float(value or 0), 2)
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _to_time_str(v: Any) -> str:
    """把时间戳(秒/毫秒)或日期字符串统一格式化为 可读时间；无法识别则原样返回。"""
    try:
        if isinstance(v, bool):
            return ""
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().lstrip("-").isdigit()):
            n = int(float(v))
            if n > 10 ** 12:      # 毫秒
                n //= 1000
            if n < 10 ** 9:       # 不像时间戳，原样
                return str(v)
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n))
    except Exception:
        pass
    return str(v or "")


def pick_time(item: Any) -> str:
    """从红包/兑换记录里挑一个时间字段（领取/中奖/兑换/创建），best-effort。"""
    if not isinstance(item, dict):
        return ""
    for k in ("receiveTime", "winTime", "exchangeTime", "drawTime", "createTime", "gmtCreate", "getTime"):
        if item.get(k):
            return _to_time_str(item[k])
    for k, v in item.items():
        kl = str(k).lower()
        if v and ("time" in kl or "date" in kl):
            return _to_time_str(v)
    return ""


def today_mmdd() -> str:
    return time.strftime("%m%d")


def build_daily_register_channel() -> str:
    return f"&s=red&m=daily&c={today_mmdd()}"


def find_deep_value(node: Any, key_patterns: tuple[str, ...], value_pattern: str | None = None, depth: int = 0) -> str:
    if depth > 10 or node is None:
        return ""
    if isinstance(node, str):
        if value_pattern and re.fullmatch(value_pattern, node):
            return node
        try:
            return find_deep_value(json.loads(node), key_patterns, value_pattern, depth + 1)
        except ValueError:
            return ""
    if isinstance(node, list):
        for item in node:
            found = find_deep_value(item, key_patterns, value_pattern, depth + 1)
            if found:
                return found
        return ""
    if isinstance(node, dict):
        for key, value in node.items():
            key_lower = str(key).lower()
            if any(pattern in key_lower for pattern in key_patterns):
                if isinstance(value, str) and (not value_pattern or re.fullmatch(value_pattern, value)):
                    return value
            found = find_deep_value(value, key_patterns, value_pattern, depth + 1)
            if found:
                return found
    return ""


def extract_fan_activity(data: Any) -> dict[str, str]:
    candidates: list[str] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            if re.search(r"fanactivity", node, re.I) and re.search(r"s=red", node, re.I) and re.search(r"m=daily", node, re.I):
                candidates.append(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(data)
    today = today_mmdd()
    preferred = next((item for item in candidates if re.search(rf"c\s*=\s*{today}", item)), candidates[0] if candidates else "")
    id_match = re.search(r"[?&]id\s*=\s*([0-9]+)", preferred, re.I)
    c_match = re.search(r"[?&]c\s*=\s*([0-9]{4})", preferred, re.I)
    return {
        "page_id": id_match.group(1) if id_match else "",
        "register_channel": f"&s=red&m=daily&c={c_match.group(1)}" if c_match else build_daily_register_channel(),
        "skip_addr": preferred,
    }


class HongseRunner:
    """单账号运行器：持有本次运行的代理 Session、事件循环桥、日志；业务逻辑照搬原脚本。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, openid: str, proxy_url: str = "",
                 params: dict | None = None) -> None:
        self.loop = loop
        self.openid = openid
        self.params = params or {}
        self.lines: list[str] = []
        self.session = requests.Session()
        self.session.verify = False
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
        self.auto_claim = AUTO_CLAIM_H5_RED_PACKET

    # ---------------- 基础工具 ----------------
    def log(self, text: str) -> None:
        """统一成 [LEVEL] 前缀：按行首表情判级别并剥掉装饰表情，与其他项目日志一致；同时实时推送。"""
        t = str(text).strip()
        if not t:
            return
        if _LEVELED_RE.match(t):
            line = t
        else:
            if "❌" in t:
                lvl = "ERROR"
            elif "⚠️" in t or "⚠" in t or "🔄" in t:
                lvl = "WARNING"
            elif "✅" in t or "🎉" in t:
                lvl = "SUCCESS"
            else:
                lvl = "INFO"
            line = f"[{lvl}] {_LEAD_EMOJI.sub('', t).strip()}"
        self.lines.append(line)
        from .logsink import emit
        emit(line)

    def sleep_random(self, min_ms: int, max_ms: int) -> None:
        time.sleep(random.randint(min_ms, max_ms) / 1000)

    def _await(self, coro):
        """把异步 codebridge 调用桥回主事件循环并同步等待结果（本方法运行在工作线程）。"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    # ---------------- codebridge 桥（取码/手机号/云函数） ----------------
    def get_wx_code(self, appid: str = APPID) -> str:
        from . import codebridge

        r = self._await(codebridge.get_code_for_openid(self.openid, appid))
        if r.get("success") and r.get("code"):
            return str(r["code"])
        raise RuntimeError(f"wx.login失败: {r.get('error') or json.dumps(r, ensure_ascii=False)[:200]}")

    def get_phone_code_info(self) -> tuple[str, str]:
        from . import codebridge

        r = self._await(codebridge.get_phone_for_openid(self.openid, APPID))
        phone_code = str(r.get("code") or "") or find_deep_value(r, ("phonecode", "code"), r"[0-9a-fA-F]{32,256}")
        mobile = str(r.get("mobile") or "") or find_deep_value(r, ("mobile", "phone"), r"1\d{10}")
        if not phone_code:
            raise RuntimeError(f"获取手机号code失败: {r.get('error') or json.dumps(r, ensure_ascii=False)[:200]}")
        return phone_code, mobile

    def get_encrypt_key(self) -> tuple[str, str]:
        from . import codebridge

        raw = self._await(codebridge.invoke_cloud_for_openid(
            self.openid, APPID,
            json.dumps({"api_name": "webapi_getuserencryptkey", "with_credentials": True}, separators=(",", ":")),
        ))
        # 递归收集所有 encrypt_key 候选（兼容嵌套 / key_info_list / 字符串里再套 JSON）
        candidates: list[tuple[str, str]] = []

        def collect(node):
            if isinstance(node, str):
                s = node.strip()
                if s.startswith("{") or s.startswith("["):
                    try:
                        collect(json.loads(s))
                    except (ValueError, json.JSONDecodeError):
                        pass
                return
            if isinstance(node, list):
                for x in node:
                    collect(x)
                return
            if isinstance(node, dict):
                ek = node.get("encrypt_key") or node.get("encryptKey")
                if ek:
                    ver = node.get("version") or node.get("ver") or node.get("keyVersion")
                    candidates.append((str(ek), str(ver or "")))
                for v in node.values():
                    collect(v)

        collect(raw)
        # 优先选能解出 16 字节（SM4/AES-128 密钥）的候选，避免取到 iv/短串等错字段
        for ek, ver in candidates:
            if len(key_bytes_from(ek)) == 16:
                return ek, ver or "1"
        if candidates:
            # 没有 16 字节候选：打印原始响应帮助定位（发我这行即可精确取字段）
            self.log(f"  🔎 未取到 16 字节 encryptKey，候选数={len(candidates)}；"
                     f"原始响应：{(raw.get('respJson') or json.dumps(raw, ensure_ascii=False))[:400]}")
            return candidates[0][0], candidates[0][1] or "1"
        raise RuntimeError(f"获取加密密钥失败：响应无 encrypt_key：{json.dumps(raw, ensure_ascii=False)[:300]}")

    # ---------------- 业务请求（走带代理的 Session） ----------------
    def request_json(self, method: str, url: str, *, json_body=None, data=None, headers=None) -> Any:
        resp = self.session.request(method, url, json=json_body, data=data, headers=headers, timeout=TIMEOUT)
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def api_request(self, method: str, path: str, data: dict[str, Any] | None, token: str = "",
                    encrypt_ver: str = "", open_id: str = "", user_id: str = "", app_secret: str = "",
                    retries: int = 1, register_channel: str = "") -> dict[str, Any] | None:
        payload = data or {}
        headers = build_headers(payload, token, encrypt_ver, open_id, user_id, app_secret, register_channel)
        url = BASE_URL + path
        for attempt in range(retries + 1):
            try:
                if method.upper() == "GET":
                    params = {**payload, "key": int(time.time() * 1000)}
                    resp = self.session.get(url, params=params, headers=headers, timeout=TIMEOUT)
                else:
                    resp = self.session.post(url, json=payload, headers=headers, timeout=TIMEOUT)
                body = resp.json()
                if DEBUG:
                    self.log(f"[API] {path} => {json.dumps(body, ensure_ascii=False)[:300]}")
                text = json.dumps(body, ensure_ascii=False)
                if resp.status_code == 429 or re.search(r"访问过于频繁|请稍后再试|系统繁忙|请求过于频繁|操作过于频繁", text):
                    if attempt < retries:
                        wait = 5 + random.random() * 2
                        self.log(f"  ⚠️ 频率限制，{round(wait)}秒后重试: {path}")
                        time.sleep(wait)
                        continue
                return body if isinstance(body, dict) else None
            except (requests.RequestException, ValueError) as exc:
                if attempt < retries:
                    self.log(f"  请求失败，重试... {exc}")
                    time.sleep(1)
                    continue
                self.log(f"  请求失败: {path} - {exc}")
                return None
        return None

    # ---------------- 登录链路 ----------------
    def get_openid_and_unionid(self, code: str) -> tuple[str, str]:
        headers = build_headers({"code": code})
        data = self.request_json("POST", f"{BASE_URL}/fundex-uc/uc/v1/getWxOpenIdAndUnionId",
                                 json_body={"code": code}, headers=headers)
        if isinstance(data, dict) and data.get("code") == "200" and data.get("data", {}).get("openId"):
            return str(data["data"].get("openId", "")), str(data["data"].get("unionId", ""))
        raise RuntimeError(f"获取openId失败: {json.dumps(data, ensure_ascii=False)[:300]}")

    def login(self, phone_code: str, open_id: str, union_id: str) -> SessionInfo | None:
        data = self.api_request("POST", "/fundex-uc/uc/v1/login", {
            "loginWay": "miniprogram",
            "platform": "mini_fundex",
            "code": phone_code,
            "openId": open_id,
            "unionId": union_id,
            "signAgreement": "阅读并同意用户协议、隐私政策，未注册的手机号认证后自动创建新账户",
            "registerChannel": "",
        })
        if data and data.get("code") == "200" and data.get("data", {}).get("token"):
            item = data["data"]
            return SessionInfo(
                token=str(item.get("token", "")),
                open_id=open_id,
                union_id=union_id,
                user_id=str(item.get("userId", "")),
                encrypt_user_id=str(item.get("encryptUserId", "")),
                is_register=str(item.get("isRegister", "")),
            )
        self.log("  登录失败: " + str(data.get("message") if isinstance(data, dict) else data))
        return None

    def protocol_login(self) -> tuple[SessionInfo, str]:
        code = self.get_wx_code()
        self.log("  ✅ wx.login code获取成功")
        open_id, union_id = self.get_openid_and_unionid(code)
        self.log(f"  ✅ openId: {open_id[:10]}...")
        phone_code, mobile = self.get_phone_code_info()
        self.log("  ✅ 手机号授权code获取成功" + (f"，手机号：{mobile}" if mobile else ""))
        session = self.login(phone_code, open_id, union_id)
        if not session:
            raise RuntimeError("登录失败")
        self.log(f"  ✅ 登录成功, userId: {session.user_id}")
        return session, mobile

    # ---------------- 签到 ----------------
    def do_sign(self, session: SessionInfo, retry_login: bool = True) -> bool:
        self.log("  📝 执行签到...")
        record = self.api_request("GET", "/fundex-activity/point/sign/getRecordList", {}, session.token, "", session.open_id, session.user_id)
        if record and record.get("code") == "200":
            today = record.get("data", {}).get("today")
            records = record.get("data", {}).get("signRecordList") or []
            today_record = next((item for item in records if item.get("signDate") == today), None)
            if today_record and today_record.get("signIn"):
                self.log(f"  ✅ 今日已签到，连续{record.get('data', {}).get('continuousDays', 0)}天")
                return True

        try:
            encrypt_key, version = self.get_encrypt_key()
            self.log(f"  🔑 签到encryptKey刷新成功, version={version}")
        except RuntimeError as exc:
            self.log(f"  ⚠️ 签到encryptKey刷新失败: {exc}")
            encrypt_key, version = "", ""

        resp = self.api_request("POST", "/fundex-activity/point/sign/userSignIn",
                                {"submitCode": "", "requestId": ""}, session.token, version,
                                session.open_id, session.user_id, encrypt_key, retries=3)
        if resp and resp.get("code") == "200":
            point = resp.get("data", {}).get("point", 0)
            days = resp.get("data", {}).get("continuousDays", 0)
            self.log(f"  ✅ 签到成功: +{point}积分，连续{days}天")
            return True
        if resp and str(resp.get("code")) == "7005" and retry_login:
            self.log("  🔄 签到7005，重新登录后重试...")
            try:
                new_session, _ = self.protocol_login()
                session.token = new_session.token
                session.open_id = new_session.open_id
                session.union_id = new_session.union_id
                session.user_id = new_session.user_id
                session.encrypt_user_id = new_session.encrypt_user_id
                self.sleep_random(1000, 2000)
                return self.do_sign(session, retry_login=False)
            except RuntimeError as exc:
                self.log(f"  ❌ 重新登录失败: {exc}")
        self.log("  ⚠️ 签到失败: " + json.dumps(resp, ensure_ascii=False))
        return False

    # ---------------- 活动发现 / 积分 ----------------
    def discover_activity_entry(self, session: SessionInfo) -> dict[str, str]:
        resp = self.api_request("GET", "/fundex-activity/opportunity/v3/findPageContent",
                                {"orderBy": "changePercent", "classA": "02", "order": "desc", "platform": "", "openId": session.open_id},
                                session.token, "", session.open_id, session.user_id, retries=1)
        found = extract_fan_activity((resp or {}).get("data") or {})
        if found["page_id"]:
            self.log(f"  🧭 动态活动入口: pageId={found['page_id']} channel={found['register_channel']}")
            return found
        flat = json.dumps((resp or {}).get("data") or {}, ensure_ascii=False)
        hit = re.search(r'fanactivity[^\"]*?[?&]id\s*=\s*([0-9]+)[^\"]*?[&?]s\s*=\s*red[^\"]*?[&?]m\s*=\s*daily[^\"]*?[&?]c\s*=\s*([0-9]{4})', flat, re.I)
        if hit:
            page_id = hit.group(1)
            channel = f"&s=red&m=daily&c={hit.group(2)}"
            self.log(f"  🧭 动态活动入口(宽松匹配): pageId={page_id} channel={channel}")
            return {"page_id": page_id, "register_channel": channel, "skip_addr": ""}
        self.log(f"  ⚠️ 未从首页发现活动入口，兜底 pageId={ACTIVITY_PAGE_ID}")
        return {"page_id": ACTIVITY_PAGE_ID, "register_channel": build_daily_register_channel(), "skip_addr": ""}

    def get_total_point(self, session: SessionInfo) -> int:
        resp = self.api_request("GET", "/fundex-activity/point/account/getTotalPoint", {}, session.token, "", session.open_id, session.user_id)
        if resp and resp.get("code") == "200" and resp.get("data"):
            return int(float(resp["data"].get("totalPoint") or 0))
        self.log("  ⚠️ 查询当前积分失败")
        return 0

    def update_red_packet_get_status(self, session: SessionInfo, packet: dict[str, Any]) -> bool:
        payload = {
            "requestId": packet.get("requestId", ""),
            "activityId": packet.get("activityId", ""),
            "activityType": packet.get("activityType") or "4",
        }
        resp = self.api_request("POST", "/fundex-activity/redPacket/updateGetStatus", payload, session.token, "", session.open_id, session.user_id)
        return bool(resp and resp.get("code") == "200")

    # ---------------- 红包 H5 领取 ----------------
    def resolve_h5_openid(self, ticket_code: str) -> str:
        if not ticket_code:
            return ""
        try:
            code = self.get_wx_code(H5_OAUTH_APPID)
            if not code:
                return ""
            ua = build_headers({})["User-Agent"] + f" miniProgram/{APPID}"
            auth_url = f"https://www.mktzb.com/mktadmin/wcode/baseWxAuth/{H5_URLTAG}?code={code}&state={ticket_code}"
            resp = self.session.get(auth_url, headers={"User-Agent": ua, "Referer": f"https://www.mktzb.com/mktadmin/wcode/baseWxAuth/{H5_URLTAG}?prjCode={ticket_code}"}, timeout=TIMEOUT)
            for cookie in resp.headers.get("Set-Cookie", "").split(","):
                match = re.search(rf"{H5_URLTAG}=([^;]+)", cookie)
                if match:
                    return match.group(1)
            match = re.search(r"id=[\"']openid[\"'][^>]*value=[\"']([^\"']*)", resp.text, re.I)
            return match.group(1) if match else ""
        except (RuntimeError, requests.RequestException):
            return ""

    def claim_red_packet(self, request_id: str, ticket_code: str, activity_id: str, activity_type: str) -> dict[str, Any]:
        prj_code = ticket_code or request_id
        if not prj_code:
            return {"success": False, "message": "缺少ticketCode/prjCode", "amount": 0}
        ua = build_headers({})["User-Agent"] + f" miniProgram/{APPID}"
        page_url = f"https://www.mktzb.com/mktadmin/wcode/baseWxAuth/{H5_URLTAG}?prjCode={prj_code}"
        try:
            page_resp = self.session.get(page_url, headers={"User-Agent": ua}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            return {"success": False, "message": f"红包页请求失败: {exc}", "amount": 0}

        html = page_resp.text or ""
        page_openid = (re.search(r"id=[\"']openid[\"'][^>]*value=[\"']([^\"']*)", html, re.I) or [None, ""])[1]
        page_openid = page_openid or os.getenv("HSJJ_MKTZB_OPENID", "")
        prj_urltag = (re.search(r"id=[\"']prj_urltag[\"'][^>]*value=[\"']([^\"']*)", html, re.I) or [None, H5_URLTAG])[1]
        page_prj_code = (re.search(r"id=[\"']prjCode[\"'][^>]*value=[\"']([^\"']*)", html, re.I) or [None, prj_code])[1]
        if not page_openid and self.auto_claim:
            self.log("  🔄 HTML未获取到openid，通过桥接服务解析H5 openid...")
            page_openid = self.resolve_h5_openid(prj_code)
        if not page_openid:
            return {"success": False, "message": "红包页未获取到mktzb公众号openid，可设置 HSJJ_MKTZB_OPENID", "amount": 0}

        form = urlencode({"prj_code": page_prj_code, "prj_urltag": prj_urltag, "openid": page_openid})
        form_headers = {
            "User-Agent": ua,
            "Referer": page_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            info = self.session.post("https://www.mktzb.com/mktadmin/wcode/getCodeBaseInfo", data=form, headers=form_headers, timeout=TIMEOUT).json()
            code_info = ((info.get("value") or {}).get("activityPrjCode") or {}) if isinstance(info, dict) else {}
            amount = float(code_info.get("user_bonus") or 0) / 100
            if str(code_info.get("if_used")) == "2":
                return {"success": True, "data": info, "amount": amount, "message": "红包发放中"}
            if str(code_info.get("if_used")) == "3":
                return {"success": True, "data": info, "amount": amount, "message": "红包已领取"}
            claim = self.session.post("https://www.mktzb.com/mktadmin/wcode/checkCodeBatchRepeat", data=form, headers=form_headers, timeout=TIMEOUT).json()
            if isinstance(claim, dict) and claim.get("success"):
                return {"success": True, "data": claim, "amount": amount}
            if isinstance(claim, dict) and claim.get("message") == "c0004":
                return {"success": True, "data": claim, "amount": amount, "message": "红包发放中"}
            return {"success": False, "message": json.dumps(claim, ensure_ascii=False), "amount": amount}
        except (requests.RequestException, ValueError) as exc:
            return {"success": False, "message": f"红包领取请求失败: {exc}", "amount": 0}

    def claim_existing_watch_rewards(self, session: SessionInfo, activity: dict[str, Any]) -> dict[str, Any]:
        rewards = activity.get("exchangeRequestVoList") or []
        claimed = 0
        amount_total = 0.0
        for item in rewards:
            request_id = item.get("requestId") or item.get("redPacketRequestId") or ""
            ticket_code = item.get("ticketCode") or item.get("tickCode") or ""
            amount = float(item.get("rewardAmount") or 0)
            status = str(item.get("receiveStatus") or "")
            if not request_id:
                continue
            when = pick_time(item)
            self.log(f"  📦 发现历史红包: {format_money(amount)}元, 券码: {ticket_code or '无'}, "
                     f"receiveStatus={status or '-'}" + (f", 时间: {when}" if when else ""))
            if not self.auto_claim or not ticket_code:
                continue
            self.update_red_packet_get_status(session, {"requestId": request_id, "activityId": item.get("activityId", ""), "activityType": item.get("activityType") or "4"})
            result = self.claim_red_packet(request_id, ticket_code, str(item.get("activityId") or ""), str(item.get("activityType") or "4"))
            if result.get("success"):
                claimed += 1
                amount_total += float(result.get("amount") or amount)
                self.log(f"  ✅ 历史红包领取成功: {format_money(result.get('amount') or amount)}元" + (f" ({result.get('message')})" if result.get("message") else ""))
            else:
                self.log(f"  ⚠️ 历史红包领取失败: {result.get('message') or '未知'}")
            self.sleep_random(500, 1500)
        return {"claimed": claimed, "amount": amount_total}

    # ---------------- ROE 口令兑换 ----------------
    def do_roe_reward(self, session: SessionInfo, activity_entry: dict[str, str]) -> dict[str, Any]:
        self.log("  🔍 查询ROE...")
        try:
            encrypt_key, version = self.get_encrypt_key()
            self.log(f"  🔑 encryptKey刷新成功, version={version}")
        except RuntimeError as exc:
            self.log(f"  ⚠️ encryptKey刷新失败: {exc}")
            encrypt_key, version = "", ""

        page_id = activity_entry.get("page_id") or ACTIVITY_PAGE_ID
        act_resp = self.api_request("GET", "/fundex-activity/financial/getActivityInfoV2", {"id": page_id}, session.token, "", session.open_id, session.user_id)
        activity = (act_resp or {}).get("data") or {}
        activity_status = str((activity.get("activitystatusResponseVo") or {}).get("status") or activity.get("status") or "")
        if not act_resp or act_resp.get("code") != "200" or not activity:
            self.log(f"  ⚠️ 活动接口无数据（code={(act_resp or {}).get('code', '-')}）")
            return {"success": False, "existing_claim": {"claimed": 0, "amount": 0}}
        if activity_status and activity_status != "1":
            self.log(f"  ⚠️ 活动已结束（status={activity_status}）")
            return {"success": False, "existing_claim": {"claimed": 0, "amount": 0}, "activity": activity}
        self.log(f"  📋 活动: {activity.get('title', '')} | status={activity_status or '-'}")

        existing_claim = {"claimed": 0, "amount": 0.0}
        if self.auto_claim:
            self.log("  💰 检查历史未领取红包...")
            existing_claim = self.claim_existing_watch_rewards(session, activity)
            if existing_claim["claimed"] > 0:
                self.log(f"  ✅ 历史红包补领完成: {existing_claim['claimed']}个, 共{format_money(existing_claim['amount'])}元")

        skip_link = activity.get("skipLink") or ""
        code_match = re.search(r"[?&]code=([^&]+)", skip_link, re.I)
        security_code = code_match.group(1) if code_match else "930707.CSI"
        self.log(f"  📊 指数: {security_code}")

        roe_resp = self.api_request("GET", "/fundex-quote/security/info/queryMustSee", {"securityCode": security_code, "isCapital": False}, session.token, "", session.open_id, session.user_id)
        roe = ((roe_resp or {}).get("data") or {}).get("roe") or {}
        if not roe_resp or roe_resp.get("code") != "200" or roe.get("value") is None:
            self.log("  ⚠️ 查询ROE失败")
            return {"success": False, "existing_claim": existing_claim, "activity": activity}
        roe_value = float(roe["value"])
        roe_answer = f"{roe_value:.2f}%"
        self.log(f"  📈 ROE: {roe_value} -> 口令: {roe_answer}")

        exchange_activity_id = (activity.get("activitystatusResponseVo") or {}).get("activityId") or activity.get("id")
        payload: dict[str, Any] = {"watchword": roe_answer, "openId": session.open_id, "activityId": exchange_activity_id}
        self.log(f"  🎯 兑换活动ID: {exchange_activity_id}")
        if encrypt_key:
            try:
                key_bytes = key_bytes_from(encrypt_key)
                if len(key_bytes) == 16:
                    payload = {"msg": sm4_encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), key_bytes)}
                    self.log("  🔐 SM4加密完成")
                else:
                    self.log(f"  ⚠️ encryptKey长度异常({len(key_bytes)}bytes，原文 {encrypt_key[:8]}…/{len(encrypt_key)}字符)，明文提交")
            except (ValueError, Exception) as exc:
                self.log(f"  ⚠️ SM4加密失败: {exc}，明文提交")
        else:
            self.log("  ⚠️ 无encryptKey，明文提交")

        resp = self.api_request("POST", "/fundex-activity/watchWordCustom/doExchange", payload, session.token, version, session.open_id, session.user_id, encrypt_key, retries=3, register_channel="")
        if resp and str(resp.get("code")) == "7005":
            self.log("  🔄 兑换7005，重新登录后重试...")
            try:
                new_session, _ = self.protocol_login()
                session.token = new_session.token
                session.open_id = new_session.open_id
                session.union_id = new_session.union_id
                session.user_id = new_session.user_id
                session.encrypt_user_id = new_session.encrypt_user_id
                self.sleep_random(1000, 2000)
                try:
                    encrypt_key, version = self.get_encrypt_key()
                except RuntimeError:
                    pass
                if encrypt_key:
                    try:
                        key_bytes = key_bytes_from(encrypt_key)
                        if len(key_bytes) == 16:
                            roe_payload = {"watchword": roe_answer, "openId": session.open_id, "activityId": exchange_activity_id}
                            payload = {"msg": sm4_encrypt(json.dumps(roe_payload, ensure_ascii=False, separators=(",", ":")), key_bytes)}
                    except (ValueError, Exception):
                        pass
                resp = self.api_request("POST", "/fundex-activity/watchWordCustom/doExchange", payload, session.token, version, session.open_id, session.user_id, encrypt_key, retries=3, register_channel="")
            except RuntimeError as exc:
                self.log(f"  ❌ 重新登录失败: {exc}")
        if not resp or resp.get("code") != "200":
            self.log("  ⚠️ 提交失败: " + json.dumps(resp, ensure_ascii=False))
            return {"success": False, "existing_claim": existing_claim, "activity": activity}

        data = resp.get("data") or {}
        if not data.get("rewardAmount"):
            self.log("  ⚠️ " + json.dumps(data, ensure_ascii=False))
            return {"success": False, "existing_claim": existing_claim, "activity": activity}

        reward_amount = float(data.get("rewardAmount") or 0)
        is_red_packet = str(data.get("rewardType") or "") == "2" or data.get("link") or data.get("tickCode") or data.get("ticketCode")
        if not is_red_packet:
            self.log(f"  🎉 兑换成功! 标题: {data.get('title') or roe_answer} 积分: {reward_amount}")
            return {"success": True, "type": "point", "amount": reward_amount, "data": data, "existing_claim": existing_claim, "activity": activity}

        self.log(f"  🎉 兑换成功! 标题: {data.get('title') or roe_answer} 红包: {reward_amount}元")
        claim_result: dict[str, Any] | None = None
        if self.auto_claim:
            request_id = data.get("redPacketRequestId") or data.get("requestId") or ""
            ticket_code = data.get("tickCode") or data.get("ticketCode") or ""
            if request_id and ticket_code:
                self.log("  💰 开始自动领取新红包...")
                self.update_red_packet_get_status(session, {"requestId": request_id, "activityId": exchange_activity_id, "activityType": "4"})
                claim_result = self.claim_red_packet(request_id, ticket_code, str(exchange_activity_id), "4")
                if claim_result.get("success"):
                    self.log(f"  ✅ 新红包自动领取成功: {format_money(claim_result.get('amount') or reward_amount)}元" + (f" ({claim_result.get('message')})" if claim_result.get("message") else ""))
                else:
                    self.log(f"  ⚠️ 新红包自动领取失败: {claim_result.get('message') or '未知'}")
        return {"success": True, "type": "redPacket", "amount": reward_amount, "data": data, "claim_result": claim_result, "existing_claim": existing_claim, "activity": activity}

    # ---------------- 口令红包（手动口令兑换，如「中证半导」） ----------------
    def do_watchword_redpacket(self, session: SessionInfo, activity_entry: dict[str, str], watchword: str) -> dict[str, Any]:
        """用户手动输入口令兑换口令红包：POST /fundex-activity/redPacket/exchangeRedPacket。

        明文结构照搬小程序 redPkt-password 组件 realExchange：
        {watchword, openId, activityId, activityTemplateId, registerChannel}（需图形验证码时另带
        submitCode/requestId）。与 ROE doExchange 同一套 SM4(encryptKey) 加密，密文包成 {"msg": ...}。
        """
        watchword = str(watchword or "").strip()
        if not watchword:
            return {"success": False, "amount": 0.0}
        self.log(f"  🎟️ 口令红包：用口令「{watchword}」兑换...")
        try:
            encrypt_key, version = self.get_encrypt_key()
            self.log(f"  🔑 encryptKey刷新成功, version={version}")
        except RuntimeError as exc:
            self.log(f"  ⚠️ encryptKey刷新失败: {exc}")
            encrypt_key, version = "", ""

        page_id = activity_entry.get("page_id") or ACTIVITY_PAGE_ID
        act_resp = self.api_request("GET", "/fundex-activity/financial/getActivityInfoV2", {"id": page_id}, session.token, "", session.open_id, session.user_id)
        activity = (act_resp or {}).get("data") or {}
        status_vo = activity.get("activitystatusResponseVo") or {}
        activity_id = status_vo.get("activityId") or activity.get("id")
        activity_status = str(status_vo.get("status") or activity.get("status") or "")
        if not activity_id:
            self.log(f"  ⚠️ 口令红包：未取到 activityId（code={(act_resp or {}).get('code', '-')}），跳过")
            return {"success": False, "amount": 0.0}
        if activity_status and activity_status != "1":
            self.log(f"  ⚠️ 口令红包：活动未开始/已结束（status={activity_status}），跳过")
            return {"success": False, "amount": 0.0}

        # 图形验证码开关：本期若开启（verifyCodeSwitch=true），纯协议暂不自动过码
        verify = self.api_request("GET", "/fundex-activity/redPacket/getVerificationCode", {}, session.token, "", session.open_id, session.user_id)
        if bool(((verify or {}).get("data") or {}).get("verifyCodeSwitch")):
            self.log("  ⚠️ 口令红包：本期开启了图形验证码，纯协议暂不支持自动过码，跳过")
            return {"success": False, "amount": 0.0, "need_captcha": True}

        register_channel = (activity_entry.get("register_channel") or "").lstrip("&")  # channelCode 不带前导 &
        plain: dict[str, Any] = {
            "watchword": watchword,
            "openId": session.open_id,
            "activityId": activity_id,
            "activityTemplateId": int(page_id) if str(page_id).isdigit() else page_id,
            "registerChannel": register_channel,
        }
        payload: dict[str, Any] = plain
        if encrypt_key:
            try:
                key_bytes = key_bytes_from(encrypt_key)
                if len(key_bytes) == 16:
                    payload = {"msg": sm4_encrypt(json.dumps(plain, ensure_ascii=False, separators=(",", ":")), key_bytes)}
                    self.log("  🔐 口令红包 SM4加密完成")
                else:
                    self.log(f"  ⚠️ encryptKey长度异常({len(key_bytes)}bytes)，明文提交")
            except (ValueError, Exception) as exc:
                self.log(f"  ⚠️ SM4加密失败: {exc}，明文提交")
        else:
            self.log("  ⚠️ 无encryptKey，明文提交")

        resp = self.api_request("POST", "/fundex-activity/redPacket/exchangeRedPacket", payload, session.token, version, session.open_id, session.user_id, encrypt_key, retries=2)
        if not resp or resp.get("code") != "200":
            msg = (resp or {}).get("msg") or (resp or {}).get("message") or "兑换失败"
            self.log(f"  ⚠️ 口令兑换失败: {msg}")
            return {"success": False, "amount": 0.0, "message": msg}

        data = resp.get("data") or {}
        reward_amount = float(data.get("rewardAmount") or 0)
        self.log(f"  🎉 口令兑换成功! {data.get('title') or watchword}" + (f" 红包: {format_money(reward_amount)}元" if reward_amount else ""))
        request_id = data.get("redPacketRequestId") or data.get("requestId") or ""
        ticket_code = data.get("tickCode") or data.get("ticketCode") or ""
        claimed = 0.0
        if self.auto_claim and request_id and ticket_code:
            self.log("  💰 开始自动领取口令红包...")
            self.update_red_packet_get_status(session, {"requestId": request_id, "activityId": activity_id, "activityType": data.get("activityType") or "4"})
            claim_result = self.claim_red_packet(request_id, ticket_code, str(activity_id), str(data.get("activityType") or "4"))
            if claim_result.get("success"):
                claimed = float(claim_result.get("amount") or reward_amount)
                self.log(f"  ✅ 口令红包领取成功: {format_money(claimed)}元" + (f" ({claim_result.get('message')})" if claim_result.get("message") else ""))
            else:
                self.log(f"  ⚠️ 口令红包领取失败（可在未领取红包里补领）: {claim_result.get('message') or '未知'}")
        return {"success": True, "amount": reward_amount, "claimed": claimed, "request_id": str(request_id or "")}

    def get_activity_list(self, session: SessionInfo) -> list[dict[str, Any]]:
        resp = self.api_request("GET", "/fundex-activity/redPacket/getActivityList", {"openId": session.open_id}, session.token, "", session.open_id, session.user_id)
        return resp.get("data") if resp and resp.get("code") == "200" and isinstance(resp.get("data"), list) else []

    def get_request_page(self, session: SessionInfo, receive_status: int) -> list[dict[str, Any]]:
        resp = self.api_request("GET", "/fundex-activity/redPacket/getRequestPage", {"receiveStatus": receive_status, "pageNo": 1, "pageSize": 100}, session.token, "", session.open_id, session.user_id)
        data = (resp or {}).get("data") or {}
        return data.get("dataList") if isinstance(data.get("dataList"), list) else []

    # ---------------- 单账号编排 ----------------
    def run_account(self, display: str) -> dict[str, Any] | None:
        display = display or self.openid or "账号"
        self.log(f"[INFO] ▶ 账号：{display}")
        try:
            session, mobile = self.protocol_login()
            if mobile:
                self.log(f"  📱 手机号：{mobile}")
            self.sleep_random(1000, 2000)
            self.do_sign(session)
            self.sleep_random(1000, 2000)
            activity_entry = self.discover_activity_entry(session)
            roe_result = self.do_roe_reward(session, activity_entry)
            self.sleep_random(1000, 2000)

            red_packet_amount = 0.0
            pending_red_packet_amount = 0.0
            roe_reward = 0.0
            claimed_amount = 0.0
            claimed_request_ids: set[str] = set()

            existing_claim = roe_result.get("existing_claim") if isinstance(roe_result, dict) else None
            if existing_claim:
                claimed_amount += float(existing_claim.get("amount") or 0)
                for item in ((roe_result.get("activity") or {}).get("exchangeRequestVoList") or []):
                    rid = item.get("requestId") or item.get("redPacketRequestId") or ""
                    if rid:
                        claimed_request_ids.add(rid)
            claim_result = roe_result.get("claim_result") if isinstance(roe_result, dict) else None
            if claim_result and claim_result.get("success"):
                claimed_amount += float(claim_result.get("amount") or 0)
                rid = (roe_result.get("data") or {}).get("redPacketRequestId") or (roe_result.get("data") or {}).get("requestId") or ""
                if rid:
                    claimed_request_ids.add(rid)

            # 口令红包：用户在表单里填了口令（如「中证半导」）才执行；兑换到的红包并入本次自动领取
            watchword = str(self.params.get("watchword") or "").strip()
            if watchword:
                wr = self.do_watchword_redpacket(session, activity_entry, watchword)
                if wr and wr.get("request_id"):
                    claimed_request_ids.add(wr["request_id"])
                if wr and wr.get("claimed"):
                    claimed_amount += float(wr.get("claimed") or 0)
                self.sleep_random(1000, 2000)

            activities = self.get_activity_list(session)
            if activities:
                self.log(f"  📋 红包活动列表: {len(activities)}个")
                for activity in activities:
                    if activity.get("id") and activity.get("title"):
                        self.log(f"  📌 {activity.get('title')} (ID: {activity.get('id')})")

            unclaimed = self.get_request_page(session, 0)
            if unclaimed:
                self.log(f"  🎁 未领取红包: {len(unclaimed)}个")
                for packet in unclaimed:
                    amount = float(packet.get("amount") or 0)
                    pending_red_packet_amount += amount
                    ticket_code = packet.get("ticketCode") or ""
                    request_id = packet.get("requestId") or ""
                    when = pick_time(packet)
                    self.log(f"  🎯 未领红包: {packet.get('describe', '')} 金额: {format_money(amount)}元 "
                             f"(ticketCode: {ticket_code or request_id})" + (f", 时间: {when}" if when else ""))
                    if self.auto_claim and ticket_code and request_id and request_id not in claimed_request_ids:
                        claimed_request_ids.add(request_id)
                        self.log("  💰 自动领取未领红包...")
                        self.update_red_packet_get_status(session, {"requestId": request_id, "activityId": packet.get("activityId", ""), "activityType": packet.get("activityType") or "4"})
                        result = self.claim_red_packet(request_id, ticket_code, str(packet.get("activityId") or ""), str(packet.get("activityType") or "4"))
                        if result.get("success"):
                            claimed_amount += float(result.get("amount") or amount)
                            self.log(f"  ✅ 未领红包领取成功: {format_money(result.get('amount') or amount)}元" + (f" ({result.get('message')})" if result.get("message") else ""))
                        else:
                            self.log(f"  ⚠️ 未领红包领取失败: {result.get('message') or '未知'}")
                        self.sleep_random(500, 1500)
            else:
                self.log("  ℹ️ 没有未领取的红包")

            history_amount = 0.0
            history = self.get_request_page(session, 1)
            if history:
                self.log(f"  📊 历史已领取红包: {len(history)}条（非本次收益）")
                history_amount = sum(float(item.get("amount") or 0) for item in history)
                self.log(f"  💰 历史已领取红包累计: {format_money(history_amount)}元（非本次收益）")

            current_point = self.get_total_point(session)
            self.log(f"  💎 当前积分: {current_point}")
            if roe_result and roe_result.get("success"):
                if roe_result.get("type") == "redPacket":
                    red_packet_amount += float(roe_result.get("amount") or 0)
                elif roe_result.get("type") == "point":
                    roe_reward += float(roe_result.get("amount") or 0)
            if claimed_amount > 0:
                self.log(f"  💰 本次自动提现领取: {format_money(claimed_amount)}元")
            return {
                "success": True,
                "display": display,
                "current_point": current_point,
                "roe_reward": roe_reward,
                "red_packet_amount": red_packet_amount,
                "history_red_packet_amount": history_amount,
                "pending_red_packet_amount": pending_red_packet_amount,
                "claimed_amount": claimed_amount,
            }
        except Exception as exc:  # 不止 RuntimeError：任何上游异常都要收敛为失败，勿逃逸成 500
            self.log(f"❌ 异常: {exc}")
            return None


async def run_hongse_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from . import db

    params = params or {}
    loop = asyncio.get_running_loop()
    _acc = db.query_one("SELECT proxy_url, nickname FROM accounts WHERE openid=?", (openid,))
    from .shortproxy import project_proxy
    proxy_url = project_proxy(params, openid)
    display = (_acc["nickname"] if _acc and _acc["nickname"] else openid)

    runner = HongseRunner(loop=loop, openid=openid, proxy_url=proxy_url, params=params)
    try:
        result = await asyncio.to_thread(runner.run_account, display)
    finally:
        try:
            runner.session.close()  # 显式释放连接，避免多账号连跑积累 socket/FD
        except Exception:
            pass

    summary = result or {"success": False, "display": display}
    ok = bool(summary.get("success"))
    text = "\n".join(runner.lines) or "（无输出）"
    return {
        "ok": ok,
        "error": None if ok else (summary.get("error") or "红色任务执行失败"),
        "stage": "hongse",
        "account": display,
        "viaProxy": bool(proxy_url),
        "hongseResult": json.dumps(summary, ensure_ascii=False, indent=2),
        # 复用内置项目的结果文本框
        "cookie": text,
    }
