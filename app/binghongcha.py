"""Built-in project: 康师傅冰红茶 1L「码上赢黄金」扫码抽奖。

将独立脚本 `冰红茶开盖赢奖.py` 适配到 app 的内置项目模型：用户选择已绑定的微信账号，
后端通过 codebridge 取到新鲜的 wx.login code，随后按源脚本的主链路登录冰红茶活动接口并扫码抽奖。

忠实复刻源脚本主链路（URL/headers/payload/常量逐字节照抄，不改写、不优化）：
  1. codebridge 取 wx.login code。
  2. 1l26.ksf.cn codeLogin 换活动 Authorization。
  3. updateMe 确认活动协议/隐私标记。
  4. lotteryMongo/lottery 扫码抽奖。
  5. lotteryMongo/findPrize 查询奖品记录。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - 运行环境应已安装 requests
    raise RuntimeError("缺少 requests，请先执行：pip install requests")

from .logsink import emit as _emit
from .maidong import _split_sns

# ───────────────── 源脚本常量（逐字节照抄，可用环境变量覆盖默认值） ─────────────────
APPID = "wx54f3e6a00f7973a7"
BASE_URL = "https://1l26.ksf.cn"
DEFAULT_ACTIVITY_ID = int(os.getenv("BHT_ACTIVITY_ID", "1"))
DEFAULT_LATITUDE = os.getenv("BHT_LATITUDE", "26.4199560546875")
DEFAULT_LONGITUDE = os.getenv("BHT_LONGITUDE", "112.87409369574652")
DEFAULT_DEVICE_TOKEN = os.getenv("BHT_DEVICE_TOKEN", "")
DEFAULT_ACCEPT_MARK = os.getenv("BHT_ACCEPT_MARK", "1l2026Ksf,1l2026Rule,1l2026Yszc")
DEFAULT_PAGE_TYPE = os.getenv("BHT_PAGE_TYPE", "my")

MINI_REFERER = f"https://servicewechat.com/{APPID}/848/page-frame.html"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.75(0x18004b35) NetType/WIFI Language/zh_CN"
)

REQUEST_TIMEOUT = int(os.getenv("BHT_TIMEOUT", "20"))

敏感键 = re.compile(
    r"(token|authorization|cookie|session|openid|unionid|phone|mobile|code|password|secret|"
    r"headimg|avatar|nickname|memberid|consumerid|userid|crmId|cemId|number|loginName|regCode)",
    re.I,
)


# ───────────────── 源脚本 脱敏 / 响应解析 助手（照搬） ─────────────────
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


def 规范瓶盖码(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    match = re.search(r"1L26\.KSF\.CN/([A-Z0-9]+)", text, re.I)
    if match:
        return f"https://1L26.ksf.cn/{match.group(1).upper()}"
    tail = text.rstrip("/").split("/")[-1].strip()
    if re.fullmatch(r"[A-Z0-9]{8,}", tail, re.I):
        return f"https://1L26.ksf.cn/{tail.upper()}"
    return text


def 展示瓶盖码(value: str) -> str:
    tail = str(value or "").rstrip("/").split("/")[-1]
    return 脱敏(tail, keep=4)


def 响应是否成功(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if str(obj.get("state") or "").upper() == "SUCCESS":
        return True
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
            for key in ("msg", "message", "errorMsg", "state", "awardName", "prizeName"):
                value = source.get(key)
                if value:
                    texts.append(str(value))
    for key in ("msg", "message", "errorMsg", "state"):
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
            for key in ("msg", "message", "errorMsg", "state", "awardName", "prizeName"):
                value = source.get(key)
                if value:
                    texts.append(str(value))
    return " ".join(texts)


def 是否限制结果(obj: Any) -> bool:
    text = 限制文本(obj)
    if not text:
        return False
    keywords = (
        "上限",
        "超过五次",
        "五次",
        "次数已达",
        "次数已用完",
        "今日已达",
        "每日限制",
        "机会已用完",
        "没有机会",
        "频繁",
        "风险",
        "限制",
    )
    return any(keyword in text for keyword in keywords)


def 是否已扫过或消耗(obj: Any) -> bool:
    text = 限制文本(obj)
    return any(keyword in text for keyword in ("已扫码", "已扫过", "已被扫", "已参与", "重复", "已使用", "二维码已使用"))


def 奖品名称(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    if data.get("awardName"):
        return str(data.get("awardName"))
    award = data.get("award")
    if isinstance(award, dict) and award.get("awardName"):
        return str(award.get("awardName"))
    prize = data.get("prize")
    if isinstance(prize, dict):
        return str(prize.get("prizeName") or prize.get("prizeDescription") or "")
    prizes = data.get("prizeList")
    if isinstance(prizes, list):
        names = []
        for item in prizes:
            if isinstance(item, dict):
                name = str(item.get("prizeName") or item.get("prizeDescription") or "")
                if name:
                    names.append(name)
        if names:
            return "、".join(names)
    return ""


def 结果摘要(obj: Any) -> str:
    if not isinstance(obj, dict):
        return str(obj)
    data = obj.get("data")
    msg = obj.get("msg") or obj.get("message") or ""
    state = str(obj.get("state") or "")
    if isinstance(data, dict):
        if isinstance(data.get("content"), list):
            return f"{len(data.get('content') or [])} 条奖品记录"
        name = 奖品名称(data)
        if name:
            parts = [name]
            prizes = data.get("prizeList")
            if isinstance(prizes, list):
                for item in prizes:
                    if not isinstance(item, dict):
                        continue
                    detail = str(item.get("prizeName") or item.get("prizeDescription") or "")
                    send_type = str(item.get("prizeSendType") or item.get("prizeType") or "")
                    deal_status = str(item.get("dealStatus") or "")
                    extra = "，".join(part for part in (send_type, deal_status) if part)
                    if detail and detail != name:
                        parts.append(detail + (f"({extra})" if extra else ""))
                    elif extra:
                        parts.append(extra)
            if data.get("lotteryTime"):
                parts.append(str(data.get("lotteryTime")))
            if data.get("codeStr"):
                parts.append("码=" + 展示瓶盖码(str(data.get("codeStr"))))
            return "；".join(parts)
        return json.dumps(脱敏对象(data), ensure_ascii=False)[:200]
    return str(msg or state or json.dumps(脱敏对象(obj), ensure_ascii=False)[:200])


def 提取奖品记录(obj: Any, account_name: str) -> list[dict[str, Any]]:
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        return []
    if not 奖品名称(data):
        return []
    records: list[dict[str, Any]] = []
    prizes = data.get("prizeList")
    if isinstance(prizes, list) and prizes:
        for item in prizes:
            if not isinstance(item, dict):
                continue
            records.append(
                {
                    "account_name": account_name,
                    "code": str(data.get("codeStr") or ""),
                    "award_name": str(data.get("awardName") or 奖品名称(data) or "冰红茶奖品"),
                    "prize_name": str(item.get("prizeName") or item.get("prizeDescription") or "冰红茶奖品"),
                    "prize_type": str(item.get("prizeType") or ""),
                    "send_type": str(item.get("prizeSendType") or ""),
                    "deal_status": str(item.get("dealStatus") or ""),
                    "order_code": str(item.get("orderCode") or item.get("orderTrace") or ""),
                    "lottery_time": str(data.get("lotteryTime") or ""),
                    "lottery_id": str(data.get("lotteryId") or ""),
                }
            )
        return records
    records.append(
        {
            "account_name": account_name,
            "code": str(data.get("codeStr") or ""),
            "award_name": str(data.get("awardName") or 奖品名称(data) or "冰红茶奖品"),
            "prize_name": str(奖品名称(data) or "冰红茶奖品"),
            "prize_type": "",
            "send_type": "",
            "deal_status": "",
            "order_code": "",
            "lottery_time": str(data.get("lotteryTime") or ""),
            "lottery_id": str(data.get("lotteryId") or ""),
        }
    )
    return records


def 是否现金奖品(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    text = " ".join(
        str(record.get(key) or "")
        for key in ("prize_name", "award_name", "prize_type", "send_type")
    )
    return "现金" in text or "红包" in text


# ───────────────── 同步 Runner（照搬源脚本 requests 逻辑，供 to_thread 调用） ─────────────────
class BingHongChaRunner:
    def __init__(self, proxy_url: str = "") -> None:
        self.session = requests.Session()
        self.token = ""
        self.user: dict[str, Any] = {}
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    def headers(self, token: str | None = None) -> dict[str, str]:
        auth = self.token if token is None else token
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": MINI_REFERER,
        }
        if auth:
            headers["Authorization"] = auth
        return headers

    def request_json(
        self,
        path: str,
        body: Any = None,
        token: str | None = None,
        allow_business_fail: bool = False,
    ) -> dict[str, Any]:
        url = BASE_URL.rstrip("/") + path
        resp = self.session.post(
            url,
            json=body or {},
            headers=self.headers(token=token),
            timeout=REQUEST_TIMEOUT,
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

    def 登录(self, wx_code: str) -> dict[str, Any]:
        obj = self.request_json(
            "/masterkong/user/unAuth/codeLogin", {"code": wx_code}, token="", allow_business_fail=False
        )
        data = obj.get("data") if isinstance(obj, dict) else {}
        if not isinstance(data, dict) or not data.get("token"):
            raise RuntimeError(f"codeLogin 未返回 token：{json.dumps(脱敏对象(obj), ensure_ascii=False)[:240]}")
        self.token = str(data.get("token") or "")
        self.user = data.get("user") if isinstance(data.get("user"), dict) else {}
        return obj

    def 确认协议(self, accept_mark: str) -> dict[str, Any]:
        return self.request_json(
            "/masterkong/user/updateMe",
            {"additional": accept_mark},
            allow_business_fail=True,
        )

    def 查询奖品(self, page: int = 1, records: int = 15) -> dict[str, Any]:
        return self.request_json(
            "/masterkong/lotteryMongo/findPrize",
            {
                "page": page,
                "records": records,
                "prizeTypeList": ["WS_AWARD", "LOGISTICS", "SHENG_HUO_POINT"],
            },
            allow_business_fail=True,
        )

    def 抽奖(
        self,
        code: str,
        activity_id: int,
        device_token: str,
        latitude: str,
        longitude: str,
        page_type: str,
    ) -> dict[str, Any]:
        return self.request_json(
            "/masterkong/lotteryMongo/lottery",
            {
                "pageType": page_type,
                "activityId": int(activity_id),
                "code": code,
                "deviceToken": device_token,
                "location": f"{latitude},{longitude}",
            },
            allow_business_fail=True,
        )


def _account_name(user: dict[str, Any], openid: str) -> str:
    if isinstance(user, dict):
        for key in ("nickname", "name", "loginName", "id", "consumerId", "memberId"):
            value = user.get(key)
            if value:
                return str(value)
    return openid


def _float_param(params: dict, key: str, default: float) -> float:
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


async def run_binghongcha_project(user_id: int, openid: str, appid: str, params: dict | None = None) -> dict:
    from .codebridge import get_code_for_openid
    from . import db

    params = params or {}
    target_appid = params.get("appid") or appid or APPID

    raw_codes = params.get("codes") or params.get("sn") or params.get("value")
    split = _split_sns(raw_codes)
    seen: set[str] = set()
    codes: list[str] = []
    for item in split:
        code = 规范瓶盖码(item)
        key = code.upper()
        if code and key not in seen:
            seen.add(key)
            codes.append(code)
    if not codes:
        return {"ok": False, "stage": "params", "error": "请输入冰红茶瓶盖码链接"}

    activity_id = params.get("activityId") or DEFAULT_ACTIVITY_ID
    try:
        activity_id = int(activity_id)
    except (TypeError, ValueError):
        activity_id = DEFAULT_ACTIVITY_ID
    latitude = str(params.get("latitude") or DEFAULT_LATITUDE)
    longitude = str(params.get("longitude") or DEFAULT_LONGITUDE)
    device_token = params.get("deviceToken")
    device_token = DEFAULT_DEVICE_TOKEN if device_token is None else str(device_token)
    accept_mark = str(params.get("acceptMark") or DEFAULT_ACCEPT_MARK)
    page_type = str(params.get("pageType") or DEFAULT_PAGE_TYPE)
    delay_between_scans = _float_param(params, "delayBetweenScans", 0.0)

    # 代理：优先本次表单填写的 proxyUrl；留空则回退到该微信账号扫码时绑定的地区代理，
    # 让冰红茶 1l26.ksf.cn 接口与账号同地区出网，避免异地 IP 触发风控。
    _acc = db.query_one("SELECT proxy_url FROM accounts WHERE openid=?", (openid,))
    proxy_url = (params.get("proxyUrl") or "").strip() or ((_acc["proxy_url"] or "") if _acc else "")

    cr = await get_code_for_openid(openid, target_appid or APPID)
    if not cr.get("success") or not cr.get("code"):
        return {"ok": False, "stage": "get-code", "error": cr.get("error") or "取冰红茶 wx.login code 失败"}

    runner = BingHongChaRunner(proxy_url=proxy_url)

    # 实时日志：边跑边推（前端可实时看到扫码进度与防风控等待）
    run_lines: list[str] = []

    def rlog(line: str) -> None:
        run_lines.append(line)
        _emit(line)

    try:
        login_obj = await asyncio.to_thread(runner.登录, cr["code"])
        account = _account_name(runner.user, openid)
        account_mask = 脱敏手机号(脱敏(account))
        rlog(f"[SUCCESS] ▶ 账号 {account_mask} 登录成功：{结果摘要(login_obj) or '已换取活动 Authorization'}")

        try:
            accept_obj = await asyncio.to_thread(runner.确认协议, accept_mark)
            rlog(f"[INFO] 协议确认：{结果摘要(accept_obj) or 'ok'}")
        except Exception as exc:
            rlog(f"[WARNING] 协议确认失败（继续扫码）：{脱敏手机号(str(exc))}")

        results: list[dict[str, Any]] = []
        winners: list[dict[str, Any]] = []
        limited = False
        for index, code in enumerate(codes, 1):
            if index > 1 and delay_between_scans > 0:
                rlog(f"[INFO] 防风控间隔，等待 {delay_between_scans:.0f} 秒…")
                await asyncio.sleep(delay_between_scans)
            head = f"码{index} {展示瓶盖码(code)}"
            try:
                lottery_obj = await asyncio.to_thread(
                    runner.抽奖, code, activity_id, device_token, latitude, longitude, page_type
                )
            except Exception as exc:
                results.append({"snIndex": index, "code": 展示瓶盖码(code), "error": 脱敏手机号(str(exc))})
                rlog(f"[ERROR] {head}：扫码异常 {脱敏手机号(str(exc))}")
                continue

            summary = 结果摘要(lottery_obj)
            item: dict[str, Any] = {"snIndex": index, "code": 展示瓶盖码(code), "summary": summary}

            if 是否限制结果(lottery_obj):
                item["limited"] = True
                results.append(item)
                rlog(f"[WARNING] {head}：{summary or '当日扫码次数已达上限'}，停止后续瓶盖码")
                limited = True
                break

            records = 提取奖品记录(lottery_obj, account_mask)
            if records:
                names = []
                for record in records:
                    label = record.get("prize_name") or record.get("award_name") or "冰红茶奖品"
                    status = record.get("deal_status") or record.get("send_type") or ""
                    suffix = f"（{status}）" if status else ""
                    names.append(f"{label}{suffix}")
                    rlog(f"[SUCCESS] {head} 中奖：{label}{suffix}")
                winners.extend(records)
                item["prizes"] = names
            elif 响应是否成功(lottery_obj) or 是否已扫过或消耗(lottery_obj):
                rlog(f"[INFO] {head}：{summary or '未中奖'}")
            else:
                rlog(f"[WARNING] {head}：{summary or '扫码未通过'}")
            results.append(item)

        # 汇总：查询活动奖品记录（源脚本 findPrize）
        try:
            prize_obj = await asyncio.to_thread(runner.查询奖品, 1, 15)
            prize_records = 提取奖品记录(prize_obj, account_mask)
            if prize_records:
                labels = [
                    (r.get("prize_name") or r.get("award_name") or "冰红茶奖品") for r in prize_records
                ]
                rlog(f"[INFO] 活动奖品记录（{len(labels)}）：{'、'.join(labels)}")
        except Exception as exc:
            rlog(f"[WARNING] 查询奖品记录失败：{脱敏手机号(str(exc))}")

        if winners:
            rlog(f"[SUCCESS] 本次共中奖 {len(winners)} 条")
        elif not limited:
            rlog("[INFO] 本次未匹配到中奖记录")
        rlog("[INFO] 运行结束")

        display = {
            "account": account_mask,
            "activityId": activity_id,
            "snCount": len(codes),
            "viaProxy": bool(proxy_url),
            "results": results,
            "winners": 脱敏对象(winners),
        }
        return {
            "ok": True,
            "stage": "binghongcha",
            "account": account_mask,
            "viaProxy": bool(proxy_url),
            "snCount": len(codes),
            "binghongchaResult": json.dumps(display, ensure_ascii=False, indent=2),
            "cookie": "\n".join(run_lines),
        }
    except Exception as exc:
        return {"ok": False, "stage": "binghongcha", "error": 脱敏手机号(str(exc))}
    finally:
        try:
            runner.session.close()
        except Exception:
            pass
