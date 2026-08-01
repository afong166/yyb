"""Built-in project: 浓五的酒馆（dtmiller 签到/刮刮乐 + wlnxjc 积分抽奖）。

把独立的 GFAN 青龙脚本移植到 app 的内置项目模型：用户选一个已绑定的微信账号，
后端用 codebridge 取两个小程序的 wx.login code，再执行签到 / 积分抽奖 / 刮刮乐。
取码沿用账号绑定的地区代理，业务接口同样走该代理（异地防风控）。

任务逻辑（URL/headers/payload）忠实移植自原脚本；仅把「从网站 API 取账号/取码」换成内部 codebridge，
并把 print 日志收集到 result 里复用内置项目的结果文本框。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 小程序配置 ====================
DTMILLER_APPID = "wxed3cf95a14b58a26"
WLNXJC_APPID = "wx99fa98e883130aa3"

# ==================== 抽奖 / 刮刮乐配置 ====================
MAX_DRAW_TIMES = 6            # 单日最大抽奖次数
MIN_DRAW_INTEGRAL = 50       # 最低抽奖积分
DRAW_INTERVAL = 5            # 抽奖间隔（秒）
DRAW_ACTIVITY_ID = "2059545046561783809"          # 浓友购积分抽奖活动ID（兜底）
DEFAULT_SIGN_PROMOTION_ID = "PI69eb321d37c48c000a05ee4e"  # 签到活动ID（兜底）

ENABLE_SCRATCH = True        # 是否开启刮刮乐
SCRATCH_USE_POINTS = False   # 免费次数用完后是否继续消耗积分
SCRATCH_NEED_POINTS = 20     # 刮刮乐每次所需积分
SCRATCH_MAX_PER_DAY = 1      # 单日最大刮刮乐次数
SCRATCH_PROMOTION_ID = "PI6a1907425d32b7000a1a1887"       # 刮刮乐活动ID

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
      "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
      "UnifiedPCWindowsWechat(0xf2541739) XWEB/18955")


def validate_jwt(token) -> bool:
    """验证 JWT 格式，支持 Bearer 前缀。"""
    if not isinstance(token, str):
        return False
    token = token.replace("Bearer ", "").replace("bearer ", "").strip()
    parts = token.split(".")
    if len(parts) != 3:
        return False
    return all(part.strip() for part in parts)


class NongwuRunner:
    """单账号运行器：持有本次运行的代理与日志/中奖统计，任务方法忠实移植自原脚本。"""

    def __init__(self, proxies: dict | None = None) -> None:
        self.proxies = proxies
        self.lines: list[str] = []
        self.cash_prizes: list[dict] = []
        self.scratch_prizes: list[dict] = []

    def log(self, label, message, level: str = "INFO") -> None:
        line = f"[{level}] [{label}] {message}" if label else f"[{level}] {message}"
        self.lines.append(line)
        from .logsink import emit
        emit(line)

    # ---------------- 登录换 token ----------------
    def get_token_dtmiller(self, code: str):
        try:
            res = requests.post(
                "https://stdcrm.dtmiller.com/std-weixin-mp-service/miniApp/custom/login",
                headers={"Content-Type": "application/json",
                         "referer": f"https://servicewechat.com/{DTMILLER_APPID}/243/page-frame.html"},
                json={"code": code, "appId": DTMILLER_APPID},
                proxies=self.proxies, timeout=10, verify=False,
            )
            data = res.json()
            if data.get("code") == 0:
                token_data = data.get("data")
                if isinstance(token_data, str):
                    return token_data.strip()
                if isinstance(token_data, dict):
                    token = token_data.get("token")
                    return token.strip() if token else None
            self.log(None, f"dtmiller 登录失败: {data}", "ERROR")
            return None
        except Exception as exc:
            self.log(None, f"dtmiller 登录异常: {exc}", "ERROR")
            return None

    def get_token_wlnxjc(self, code: str):
        try:
            res = requests.post(
                "https://www.wlnxjc.com:8088/app-api/member/auth/weixin-mini-app-login",
                headers={"Content-Type": "application/json;charset=UTF-8",
                         "platform": "WechatMiniProgram", "tenant-id": "1",
                         "referer": f"https://servicewechat.com/{WLNXJC_APPID}/25/page-frame.html"},
                json={"loginCode": code, "state": "default"},
                proxies=self.proxies, timeout=10, verify=False,
            )
            data = res.json()
            if data.get("code") == 0:
                token = data["data"]["accessToken"]
                return token.strip() if token else None
            self.log(None, f"wlnxjc 登录失败: {data}", "ERROR")
            return None
        except Exception as exc:
            self.log(None, f"wlnxjc 登录异常: {exc}", "ERROR")
            return None

    # ---------------- 签到（dtmiller） ----------------
    def _dtm_headers(self, token: str) -> dict:
        return {"User-Agent": UA, "authorization": "Bearer " + token, "xweb_xhr": "1",
                "content-type": "application/json",
                "referer": f"https://servicewechat.com/{DTMILLER_APPID}/242/page-frame.html"}

    def get_sign_promotion_id(self, token: str) -> str:
        try:
            response = requests.post(
                "https://stdcrm.dtmiller.com/scrm-promotion-service/mini/module/config/list",
                headers=self._dtm_headers(token), json={}, proxies=self.proxies, timeout=10, verify=False,
            )
            data = response.json()
            if data.get("code") != 0:
                return DEFAULT_SIGN_PROMOTION_ID
            for module in data.get("data", []):
                for detail in module.get("detailList", []):
                    detail_json = detail.get("detailJson", "")
                    if "signUp" not in detail_json or "promotionId=" not in detail_json:
                        continue
                    match = re.search(r"promotionId=(PI[0-9A-Za-z]+)", detail_json)
                    if match:
                        return match.group(1)
            return DEFAULT_SIGN_PROMOTION_ID
        except Exception:
            return DEFAULT_SIGN_PROMOTION_ID

    def sign(self, token: str, username: str) -> None:
        if not validate_jwt(token):
            self.log(username, "令牌格式错误，跳过签到", "ERROR")
            return
        promotion_id = self.get_sign_promotion_id(token)
        url = f"https://stdcrm.dtmiller.com/scrm-promotion-service/promotion/sign/today?promotionId={promotion_id}"
        try:
            response = requests.get(url, headers=self._dtm_headers(token), proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            code = data.get("code")
            msg = data.get("msg")
            if code == 0:
                self.log(username, "今日签到成功", "SUCCESS")
            elif "已签到" in str(msg):
                self.log(username, "今日已签到，无需重复操作", "SUCCESS")
            elif "signature does not match" in str(msg):
                self.log(username, "签到失败：令牌已失效", "ERROR")
            else:
                self.log(username, f"签到失败：{msg}", "ERROR")
        except Exception as exc:
            self.log(username, f"签到异常：{exc}", "ERROR")

    # ---------------- 积分 / 抽奖（wlnxjc） ----------------
    def _wlx_headers(self, auth: str) -> dict:
        return {"Host": "www.wlnxjc.com:8088", "User-Agent": UA, "authorization": auth, "xweb_xhr": "1",
                "content-type": "application/json;charset=UTF-8", "platform": "WechatMiniProgram",
                "referer": f"https://servicewechat.com/{WLNXJC_APPID}/23/page-frame.html"}

    def integral(self, auth: str) -> int:
        try:
            response = requests.get("https://www.wlnxjc.com:8088/app-api/member/integral/get",
                                    headers=self._wlx_headers(auth), proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            if data.get("code") == 0:
                return data["data"].get("integral", 0)
            return 0
        except Exception:
            return 0

    def get_draw_activity_id(self, auth: str) -> str:
        try:
            response = requests.get("https://www.wlnxjc.com:8088/app-api/promotion/activity/list",
                                    headers=self._wlx_headers(auth), proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            if data.get("code") != 0:
                return DRAW_ACTIVITY_ID
            activity_list = data.get("data") or []
            if not isinstance(activity_list, list):
                return DRAW_ACTIVITY_ID
            for item in activity_list:
                name = str(item.get("name", ""))
                activity_id = item.get("id") or item.get("activityId")
                if activity_id and item.get("type") == 1 and any(k in name for k in ("积分", "转盘", "抽奖")):
                    return str(activity_id)
            for item in activity_list:
                activity_id = item.get("id") or item.get("activityId")
                if activity_id and item.get("type") == 1:
                    return str(activity_id)
            return DRAW_ACTIVITY_ID
        except Exception:
            return DRAW_ACTIVITY_ID

    def draw(self, auth: str, username: str, activity_id: str | None = None):
        activity_id = activity_id or DRAW_ACTIVITY_ID
        url = f"https://www.wlnxjc.com:8088/app-api/promotion/activity/draw?activityId={activity_id}"
        payload = {"activityId": activity_id}
        try:
            time.sleep(DRAW_INTERVAL)
            response = requests.post(url, data=json.dumps(payload), headers=self._wlx_headers(auth),
                                     proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            if data.get("code") != 0:
                self.log(username, f"抽奖失败：{data.get('msg')}", "ERROR")
                return None
            lottery = data["data"].get("lottery", {})
            award_name = lottery.get("awardName", "未知")
            prize_name = lottery.get("prizeName", "未知")
            draw_integral = lottery.get("drawIntegral", 0)
            is_cash = "现金红包" in award_name or "现金红包" in prize_name
            if is_cash:
                cash_amount = prize_name if "现金红包" in prize_name else award_name
                self.log(username, f"恭喜抽中：{award_name}（金额：{cash_amount}）", "SUCCESS")
                self.cash_prizes.append({"账号": username, "奖项": award_name,
                                         "金额": cash_amount, "消耗积分": draw_integral})
            else:
                self.log(username, f"抽中：{award_name}（内容：{prize_name}）", "SUCCESS")
            self.log(username, f"当前剩余积分：{self.integral(auth)}", "INFO")
            return data["data"].get("recordId")
        except Exception as exc:
            self.log(username, f"抽奖异常：{exc}", "ERROR")
            return None

    def receive(self, auth: str, record_id, username: str) -> None:
        if not record_id:
            return
        try:
            response = requests.post("https://www.wlnxjc.com:8088/app-api/promotion/activity/receive",
                                     data=json.dumps({"id": record_id}), headers=self._wlx_headers(auth),
                                     proxies=self.proxies, timeout=10, verify=False)
            if response.json().get("code") == 0:
                self.log(username, "奖品领取成功", "SUCCESS")
            else:
                self.log(username, f"奖品领取失败：{response.json().get('msg')}", "ERROR")
        except Exception as exc:
            self.log(username, f"领取异常：{exc}", "ERROR")

    def record_page(self, auth: str) -> int:
        """查询今日抽奖次数。"""
        url = "https://www.wlnxjc.com:8088/app-api/promotion/activity/record-page?pageNo=1&pageSize=20&status=1"
        try:
            response = requests.get(url, headers=self._wlx_headers(auth), proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            if data.get("code") == 0 and data["data"]["list"]:
                today_0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                today_0_ts = int(today_0.timestamp() * 1000)
                return len([x for x in data["data"]["list"] if x.get("createTime", 0) >= today_0_ts])
            return 0
        except Exception:
            return 0

    def record_page_no(self, auth: str, username: str) -> None:
        url = "https://www.wlnxjc.com:8088/app-api/promotion/activity/record-page?pageNo=1&pageSize=8&status=0"
        try:
            response = requests.get(url, headers=self._wlx_headers(auth), proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            if data.get("code") == 0 and data["data"]["list"]:
                self.log(username, f"检测到 {len(data['data']['list'])} 个待领取奖品，开始领取", "INFO")
                for item in data["data"]["list"]:
                    self.receive(auth, item.get("id"), username)
            else:
                self.log(username, "暂无待领取奖品", "INFO")
        except Exception as exc:
            self.log(username, f"查询待领取奖品异常：{exc}", "ERROR")

    # ---------------- 刮刮乐（dtmiller） ----------------
    def get_scratch_info(self, token: str):
        url = ("https://stdcrm.dtmiller.com/scrm-promotion-service/promotion/draw/user/getluckdrawNum"
               f"?promotionId={SCRATCH_PROMOTION_ID}")
        try:
            response = requests.get(url, headers=self._dtm_headers(token), proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            if data.get("code") == 0:
                info = data.get("data", {}) or {}
                return info.get("freeDrawNum", 0), info.get("needPoints", SCRATCH_NEED_POINTS)
            return 0, SCRATCH_NEED_POINTS
        except Exception:
            return 0, SCRATCH_NEED_POINTS

    def scratch_draw(self, token: str, username: str, token_wlx: str | None = None, before_point=None):
        url = (f"https://stdcrm.dtmiller.com/scrm-promotion-service/promotion/draw/lottery"
               f"?promotionId={SCRATCH_PROMOTION_ID}")
        try:
            response = requests.get(url, headers=self._dtm_headers(token), proxies=self.proxies, timeout=10, verify=False)
            data = response.json()
            if data.get("code") != 0:
                self.log(username, f"刮刮乐失败：{data.get('msg')}", "ERROR")
                return None
            result = data.get("data", {}) or {}
            msg = result.get("msg", "谢谢参与")
            state = result.get("state", 0)
            if token_wlx and before_point is not None:
                time.sleep(1)
                after_point = self.integral(token_wlx)
                if after_point != before_point:
                    diff = after_point - before_point
                    self.log(username, f"积分变化：{before_point} -> {after_point}（{diff:+}）", "INFO")
                    if diff > 0 and (state == 0 or msg == "谢谢参与"):
                        msg = f"积分{diff:+}"
                        state = 1
            if state == 0 or msg == "谢谢参与":
                self.log(username, "刮刮乐：谢谢参与", "INFO")
            else:
                self.log(username, f"刮刮乐中奖：{msg}", "SUCCESS")
                self.scratch_prizes.append({"账号": username, "奖品": msg})
            return msg
        except Exception as exc:
            self.log(username, f"刮刮乐异常：{exc}", "ERROR")
            return None

    def process_scratch(self, token_dtm: str | None, token_wlx: str | None, username: str) -> None:
        if not ENABLE_SCRATCH:
            return
        if not token_dtm or not validate_jwt(token_dtm):
            self.log(username, "dtmiller 令牌无效，跳过刮刮乐", "WARNING")
            return
        free_num, need_points = self.get_scratch_info(token_dtm)
        self.log(username, f"刮刮乐免费次数：{free_num} | 每次需积分：{need_points} | 最大次数：{SCRATCH_MAX_PER_DAY}", "INFO")
        if free_num <= 0 and not SCRATCH_USE_POINTS:
            self.log(username, "无免费次数，跳过刮刮乐", "WARNING")
            return
        total_times = min(SCRATCH_MAX_PER_DAY, free_num)
        for i in range(total_times):
            before_point = self.integral(token_wlx) if token_wlx else None
            self.log(username, f"开始第 {i + 1} 次刮刮乐（免费）", "INFO")
            self.scratch_draw(token_dtm, username, token_wlx, before_point)
            time.sleep(random.uniform(1, 2))
        if not SCRATCH_USE_POINTS:
            return
        for i in range(SCRATCH_MAX_PER_DAY - total_times):
            before_point = self.integral(token_wlx) if token_wlx else None
            if before_point is not None and before_point < need_points:
                self.log(username, f"积分不足{need_points}，停止刮刮乐", "WARNING")
                break
            seq = total_times + i + 1
            self.log(username, f"开始第 {seq} 次刮刮乐（消耗{need_points}积分）", "INFO")
            self.scratch_draw(token_dtm, username, token_wlx, before_point)
            time.sleep(random.uniform(1, 2))

    # ---------------- 单账号编排 ----------------
    def run_account(self, username: str, code_dtm: str | None, code_wlx: str | None) -> bool:
        """执行单账号任务；返回是否至少成功登录到一个小程序（供上层如实上报成功/失败）。"""
        # 1. dtmiller 登录 + 签到
        token_dtm = None
        if code_dtm:
            token_dtm = self.get_token_dtmiller(code_dtm)
            if not token_dtm:
                self.log(username, "dtmiller Token 获取失败，跳过签到和刮刮乐", "WARNING")
            else:
                self.sign(token_dtm, username)
        else:
            self.log(username, "dtmiller Code 获取失败，跳过签到和刮刮乐", "WARNING")

        # 2. wlnxjc 登录
        if not code_wlx:
            self.log(username, "wlnxjc Code 获取失败，跳过抽奖", "WARNING")
            return bool(token_dtm)
        token_wlx = self.get_token_wlnxjc(code_wlx)
        if not token_wlx:
            self.log(username, "wlnxjc Token 获取失败，跳过抽奖", "WARNING")
            return bool(token_dtm)
        draw_activity_id = self.get_draw_activity_id(token_wlx)

        # 3. 积分校验
        current_point = self.integral(token_wlx)
        self.log(username, f"当前积分：{current_point} | 最低抽奖积分：{MIN_DRAW_INTEGRAL}", "INFO")
        if current_point < MIN_DRAW_INTEGRAL:
            self.log(username, f"积分不足{MIN_DRAW_INTEGRAL}，稍后跳过积分抽奖", "WARNING")

        # 4. 领取待领奖
        self.record_page_no(token_wlx, username)

        # 5. 刮刮乐
        self.process_scratch(token_dtm, token_wlx, username)

        # 6. 循环抽满单日最大次数
        drawn_times = self.record_page(token_wlx)
        self.log(username, f"今日已抽：{drawn_times} 次 | 单日最大可抽：{MAX_DRAW_TIMES} 次", "INFO")
        remaining_times = MAX_DRAW_TIMES - drawn_times
        if remaining_times > 0:
            self.log(username, f"开始执行剩余 {remaining_times} 次抽奖", "INFO")
            for i in range(remaining_times):
                current_point = self.integral(token_wlx)
                if current_point < MIN_DRAW_INTEGRAL:
                    self.log(username, f"积分不足{MIN_DRAW_INTEGRAL}，停止后续抽奖", "WARNING")
                    break
                draw_seq = drawn_times + i + 1
                self.log(username, f"开始第 {draw_seq} 次抽奖", "INFO")
                record_id = self.draw(token_wlx, username, draw_activity_id)
                self.receive(token_wlx, record_id, username)
        else:
            self.log(username, "今日抽奖次数已达上限，停止抽奖", "WARNING")
        return True


async def run_nongwu_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from .codebridge import get_code_for_openid
    from . import db

    params = params or {}
    # 代理：优先本次表单填的 proxyUrl；留空则回退到该微信账号绑定的地区代理，业务接口与取码同地区出网防风控。
    _acc = db.query_one("SELECT proxy_url, nickname FROM accounts WHERE openid=?", (openid,))
    from .shortproxy import project_proxy
    proxy_url = project_proxy(params, openid)
    username = (_acc["nickname"] if _acc and _acc["nickname"] else openid)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    # 两个小程序各取一次 code（都走账号自带代理）
    cr_dtm = await get_code_for_openid(openid, DTMILLER_APPID)
    cr_wlx = await get_code_for_openid(openid, WLNXJC_APPID)
    code_dtm = cr_dtm.get("code") if cr_dtm.get("success") else None
    code_wlx = cr_wlx.get("code") if cr_wlx.get("success") else None
    if not code_dtm and not code_wlx:
        return {"ok": False, "stage": "get-code",
                "error": cr_dtm.get("error") or cr_wlx.get("error") or "两个小程序 code 都取失败"}

    runner = NongwuRunner(proxies)
    # 任务全是同步 requests + sleep，丢到线程里跑，避免阻塞事件循环
    ok = bool(await asyncio.to_thread(runner.run_account, username, code_dtm, code_wlx))

    summary: dict[str, Any] = {
        "account": username,
        "viaProxy": bool(proxy_url),
        "cashPrizes": runner.cash_prizes,
        "scratchPrizes": runner.scratch_prizes,
        "log": runner.lines,
    }
    text = "\n".join(runner.lines) or "（无输出）"
    return {
        "ok": ok,
        "error": None if ok else "两个小程序均登录失败",
        "stage": "nongwu",
        "account": username,
        "viaProxy": bool(proxy_url),
        "cashPrizes": runner.cash_prizes,
        "scratchPrizes": runner.scratch_prizes,
        "nongwuResult": json.dumps(summary, ensure_ascii=False, indent=2),
        # 复用内置项目的结果文本框
        "cookie": text,
    }
