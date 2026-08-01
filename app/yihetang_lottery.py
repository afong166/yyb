"""Built-in project: 益禾堂（茶饮）抽奖。

把独立脚本「益禾堂抽奖.py」移植到 app 的内置项目模型：与「益禾堂签到」同品牌 qmai
（appid=wx4080846d0cec2fd5, store_id=203009）。用户选一个已绑定的微信账号，后端用
codebridge 取益禾堂小程序的 wx.login code，登录换 qm-user-token + userId；若账号未绑
定手机号导致登录/抽奖失败，再取一次 getPhoneNumber code 绑定手机号并重新登录，随后按
活动 ID 执行抽奖。

签名算法（generate_signature：activityId 反转做 key、参数排序、MD5 大写）与
takePartInLottery/getLotteryInfo/getPrizeList 的 URL、headers、payload 忠实照抄自原
脚本，未做任何改写。取码与业务接口沿用账号绑定的地区代理（异地防风控）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 小程序 / 活动配置 ====================
QMAI_BASE = "https://webapi.qmai.cn"
LOTTERY_BASE = "https://webapi.qmai.cn/web/cmk-center/lottery"
APPID = "wx4080846d0cec2fd5"
STORE_ID = "203009"
DEFAULT_ACTIVITY_ID = "1281673220168941569"
UA = (
    "Mozilla/5.0 (Linux; Android 16; PKG110 Build/UKQ1.231108.001; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.178 "
    "Mobile Safari/537.36 XWEB/1460217 MMWEBSDK/20260502 MMWEBID/9496 "
    "MicroMessenger/8.0.74.3120(0x28004A36) WeChat/arm64 Weixin NetType/WIFI "
    "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)


def _mask(value: Any) -> str:
    """脱敏 token/mobile/userId：保留头尾，中间打码。"""
    s = str(value or "")
    if len(s) <= 4:
        return "*" * len(s)
    if len(s) <= 8:
        return s[:2] + "*" * (len(s) - 3) + s[-1]
    return s[:4] + "*" * 4 + s[-4:]


class YihetangLotteryRunner:
    """单账号运行器：持有本次运行的代理与日志，任务方法忠实移植自原脚本。"""

    def __init__(self, proxies: dict | None = None, timeout: int = 15) -> None:
        self.proxies = proxies
        self.timeout = timeout
        self.lines: list[str] = []
        self.appid = APPID
        self.store_id = STORE_ID
        self.user_id: str = ""
        self.user_token: str = ""

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"[{level}] {message}"
        self.lines.append(line)
        from .logsink import emit
        emit(line)

    # ---------------- 登录 / 手机号授权（qmai） ----------------
    def login(self, wx_code: str) -> tuple[str, str, dict]:
        """wx.login code -> mini-app-login，换 qm-user-token + userId。返回 (token, user_id, data)。"""
        login_url = f"{QMAI_BASE}/web/account-center/oauth/mini-app-login"
        headers = {
            "Content-Type": "application/json",
            "Accept": "v=1.0",
            "Qm-From": "wechat",
            "Qm-From-Type": "catering",
            "store-id": self.store_id,
            "User-Agent": UA,
            "Referer": f"https://servicewechat.com/{self.appid}/539/page-frame.html",
        }
        payload = {"code": wx_code, "eVersion": "1.0", "appid": self.appid}
        response = requests.post(
            login_url, data=json.dumps(payload), headers=headers,
            proxies=self.proxies, timeout=self.timeout, verify=False,
        )
        try:
            result = response.json()
        except Exception as exc:
            raise RuntimeError(f"益禾堂登录返回非 JSON：HTTP {response.status_code} {response.text[:120]}") from exc

        if result.get("code") == 0 and result.get("status") is True:
            data = result.get("data") or {}
            token = (data.get("token") or "").strip()
            user = data.get("user") or {}
            user_id = str(user.get("id", "")).strip()
            if token and user_id:
                self.user_token = token
                self.user_id = user_id
                return token, user_id, data
            raise RuntimeError("益禾堂登录成功但缺少 token 或 userId")
        raise RuntimeError(result.get("message") or json.dumps(result, ensure_ascii=False)[:160])

    def bind_mobile(self, phone_code: str) -> None:
        """未绑定手机号时补授权（沿用签到脚本 bind-mobile 那套）。"""
        url = f"{QMAI_BASE}/web/account-center/oauth/bind-mobile"
        headers = {
            "Content-Type": "application/json",
            "Accept": "v=1.0",
            "Qm-From": "wechat",
            "Qm-From-Type": "catering",
            "store-id": self.store_id,
            "qm-user-token": self.user_token,
            "User-Agent": UA,
            "Referer": f"https://servicewechat.com/{self.appid}/539/page-frame.html",
        }
        body = {
            "code": phone_code,
            "reg_activity_source": 0,
            "is_update_mobile": 0,
            "channel_code": "",
            "flowScene": 1256,
            "eVersion": "1.0",
            "appid": self.appid,
        }
        r = requests.post(
            url, data=json.dumps(body), headers=headers,
            proxies=self.proxies, timeout=self.timeout, verify=False,
        )
        try:
            obj = r.json()
        except Exception as exc:
            raise RuntimeError(f"bind-mobile 返回非 JSON：HTTP {r.status_code} {r.text[:120]}") from exc
        if not obj.get("status"):
            raise RuntimeError(obj.get("message") or obj.get("msg") or json.dumps(obj, ensure_ascii=False)[:160])

    # ---------------- 抽奖签名与接口（忠实照抄原脚本） ----------------
    def generate_signature(self, activity_id: str, timestamp: str) -> str:
        """生成签名 - E函数算法 (v=1)：activityId 反转做 key，参数排序，MD5 大写。"""
        # activityId反转作为key
        key = activity_id[::-1]

        # 签名参数
        params = {
            "activityId": activity_id,
            "sellerId": self.store_id,
            "timestamp": timestamp,
            "userId": self.user_id,
        }

        # 按key字母排序
        sorted_params = dict(sorted(params.items()))

        # 拼接签名字符串
        sign_str = "&".join([f"{k}={v}" for k, v in sorted_params.items()])
        sign_str += f"&key={key}"

        # MD5计算并转大写
        return hashlib.md5(sign_str.encode()).hexdigest().upper()

    def build_headers(self) -> dict:
        """构建请求头（照抄原脚本）。"""
        return {
            'User-Agent': UA,
            'Accept': "v=1.0",
            'Content-Type': "application/json",
            'accept-language': "zh-CN",
            'qm-from': "wechat",
            'qm-from-type': "catering",
            'qm-user-token': self.user_token,
            'store-id': self.store_id,
            'work-staff-id': "",
            'work-staff-name': "",
            'work-wechat-userid': "",
            'charset': "utf-8",
            'referer': f"https://servicewechat.com/{self.appid}/539/page-frame.html",
        }

    def _lottery_payload(self, activity_id: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        signature = self.generate_signature(activity_id, timestamp)
        return {
            "activityId": activity_id,
            "appid": self.appid,
            "timestamp": timestamp,
            "signature": signature,
            "v": 1,
        }

    def _post(self, endpoint: str, activity_id: str) -> dict:
        payload = self._lottery_payload(activity_id)
        headers = self.build_headers()
        url = f"{LOTTERY_BASE}/{endpoint}"
        response = requests.post(
            url, data=json.dumps(payload), headers=headers,
            proxies=self.proxies, timeout=self.timeout, verify=False,
        )
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"{endpoint} 返回非 JSON：HTTP {response.status_code} {response.text[:120]}") from exc

    def take_part_in_lottery(self, activity_id: str) -> dict:
        return self._post("takePartInLottery", activity_id)

    def get_lottery_info(self, activity_id: str) -> dict:
        return self._post("getLotteryInfo", activity_id)

    def get_prize_list(self, activity_id: str) -> dict:
        return self._post("getPrizeList", activity_id)


def _extract_msg(obj: dict) -> str:
    if not isinstance(obj, dict):
        return str(obj)[:160]
    return str(obj.get("message") or obj.get("msg") or obj.get("desc") or "").strip()


def run_lottery_flow(runner: YihetangLotteryRunner, username: str, activity_id: str) -> dict:
    """同步执行：读取抽奖信息 -> 参与抽奖 -> 解析结果 -> 查询奖品记录。返回展示 dict。"""
    display: dict[str, Any] = {"activityId": activity_id}

    # 1) 读取抽奖信息（若接口可用，展示活动名 / 剩余次数）
    try:
        info = runner.get_lottery_info(activity_id)
        display["lotteryInfo"] = info
        if isinstance(info, dict) and info.get("code") == 0:
            data = info.get("data") or {}
            title = data.get("activityName") or data.get("title") or data.get("name") or ""
            remain = (data.get("remainCount") if data.get("remainCount") is not None
                      else data.get("residueCount") if data.get("residueCount") is not None
                      else data.get("surplusCount") if data.get("surplusCount") is not None
                      else data.get("lotteryCount"))
            parts = []
            if title:
                parts.append(f"活动：{title}")
            if remain is not None:
                parts.append(f"剩余抽奖次数：{remain}")
            runner.log(f"{username}：抽奖信息 - {('；'.join(parts)) if parts else '已获取'}")
        else:
            runner.log(f"{username}：getLotteryInfo 未返回有效数据：{_extract_msg(info) or '（略）'}", "WARNING")
    except Exception as exc:
        runner.log(f"{username}：读取抽奖信息失败（不影响抽奖）：{exc}", "WARNING")

    # 2) 参与抽奖
    result = runner.take_part_in_lottery(activity_id)
    display["takePartInLottery"] = result

    if not isinstance(result, dict):
        runner.log(f"{username}：抽奖返回异常：{str(result)[:160]}", "ERROR")
        return display

    msg = _extract_msg(result)
    if result.get("code") == 0 and result.get("status") in (True, None):
        data = result.get("data") or {}
        prize_name = (data.get("prizeName") or data.get("awardName")
                      or data.get("name") or data.get("prizeTitle") or "")
        # 常见中奖标志：isWin/winning/prizeType，或存在具体奖品名
        is_win = data.get("isWin")
        if is_win is None:
            is_win = data.get("winning")
        if is_win is None:
            is_win = bool(prize_name) and prize_name not in ("谢谢参与", "未中奖", "谢谢惠顾")
        if is_win:
            runner.log(f"{username}：抽奖中奖 - {prize_name or '已中奖（详见结果）'}", "SUCCESS")
        else:
            runner.log(f"{username}：抽奖完成 - 未中奖（谢谢参与）", "SUCCESS")
    else:
        # 次数用尽 / 未登录等
        if any(k in msg for k in ("次数", "已用完", "用尽", "不足", "没有机会", "无抽奖机会")):
            runner.log(f"{username}：抽奖机会已用尽：{msg}", "WARNING")
        elif any(k in msg for k in ("登录", "token", "授权", "手机号")):
            runner.log(f"{username}：抽奖被拒（可能需手机号/会员授权）：{msg}", "ERROR")
        else:
            runner.log(f"{username}：抽奖失败：{msg or json.dumps(result, ensure_ascii=False)[:160]}", "ERROR")

    # 3) 可选：查询奖品记录
    try:
        prizes = runner.get_prize_list(activity_id)
        display["prizeList"] = prizes
        if isinstance(prizes, dict) and prizes.get("code") == 0:
            data = prizes.get("data") or {}
            records = data.get("list") or data.get("records") or data.get("prizeList") or []
            if isinstance(records, list):
                runner.log(f"{username}：奖品记录条数 {len(records)}")
    except Exception as exc:
        runner.log(f"{username}：查询奖品记录失败（不影响结果）：{exc}", "WARNING")

    return display


async def run_yihetang_lottery_project(
    user_id: int, openid: str, appid: str, params: dict | None = None
) -> dict:
    from .codebridge import get_code_for_openid, get_phone_for_openid
    from . import db

    params = params or {}
    app_id = appid or APPID
    activity_id = str(params.get("activityId") or DEFAULT_ACTIVITY_ID)

    # 代理：优先本次表单填的 proxyUrl；留空则回退到该微信账号绑定的地区代理，业务接口与取码同地区出网防风控。
    _acc = db.query_one("SELECT proxy_url, nickname FROM accounts WHERE openid=?", (openid,))
    from .shortproxy import project_proxy
    proxy_url = project_proxy(params, openid)
    username = (_acc["nickname"] if _acc and _acc["nickname"] else openid)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    runner = YihetangLotteryRunner(proxies, timeout=int(params.get("timeout") or 15))

    # 1. 取 wx.login code
    cr = await get_code_for_openid(openid, app_id)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "取益禾堂小程序 code 失败"}

    try:
        # 2. 登录换 qm-user-token + userId
        token, uid, login_data = await asyncio.to_thread(runner.login, cr["code"])
        runner.log(f"{username}：登录成功，userId={_mask(uid)}，token={_mask(token)}")

        # 3. 未绑定手机号则补授权后重新登录（同签到脚本做法）
        user = (login_data or {}).get("user") or {}
        if not user.get("mobile"):
            pr = await get_phone_for_openid(openid, app_id)
            phone_code = pr.get("code") if pr.get("success") else ""
            if phone_code:
                await asyncio.to_thread(runner.bind_mobile, phone_code)
                cr2 = await get_code_for_openid(openid, app_id)
                if cr2.get("success") and cr2.get("code"):
                    token, uid, login_data = await asyncio.to_thread(runner.login, cr2["code"])
                    runner.log(f"{username}：已完成手机号授权并重新登录，userId={_mask(uid)}")
            else:
                runner.log(f"{username}：账号未绑定手机号且取手机号 code 失败，尝试直接抽奖", "WARNING")

        # 4. 执行抽奖（同步 requests，丢线程里跑避免阻塞事件循环）
        display = await asyncio.to_thread(run_lottery_flow, runner, username, activity_id)
    except Exception as exc:
        runner.log(f"{username}：抽奖异常：{exc}", "ERROR")
        return {"ok": False, "stage": "yihetang-lottery", "error": str(exc)}

    text = "\n".join(runner.lines) or "（无输出）"
    return {
        "ok": True,
        "stage": "yihetang-lottery",
        "account": _mask(runner.user_id) if runner.user_id else username,
        "viaProxy": bool(proxy_url),
        "yihetangLotteryResult": json.dumps(display, ensure_ascii=False, indent=2),
        # 复用内置项目的结果文本框
        "cookie": text,
    }
