"""统一代理选择与 51代理短效 SOCKS5 提取。

51 API 由管理员前端提交并明文保存在 admin_config。51 的 SOCKS5 地址回收后会变更，
因此短效模式每次开始一项业务操作都重新提取，不在数据库或账号记录中保存地址。
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

from . import db
from .util import guard_public_url, normalize_proxy_url

CONFIG_KEY = "short_proxy_51_api_url"
AREA_URL = "https://51daili.com/index/api/area.html"
LOCAL_REGION_CODE = "150100"
LOCAL_REGION_NAME = "呼和浩特"
REQUEST_TIMEOUT = 15
# 51 API 的固定时长编号：1=30秒，11=3分钟。
TIME_CODE_RENEW = "1"
TIME_CODE_QR_LOGIN = "11"
MAX_RENEW_PROXY_ATTEMPTS = 5
_area_cache: dict[str, tuple[float, list[dict]]] = {}


def is_local_region(region_code: str = "", region_name: str = "") -> bool:
    return str(region_code or "").strip() == LOCAL_REGION_CODE or LOCAL_REGION_NAME in str(region_name or "")


def _validate_api_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("51代理 API URL 不能为空")
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host:
        raise ValueError("51代理 API URL 必须是 http/https 完整地址")
    if host != "51daili.com" and not host.endswith(".51daili.com"):
        raise ValueError("仅允许配置 51daili.com 官方 API 地址")
    if not p.path:
        raise ValueError("51代理 API URL 缺少接口路径")
    # 保持 51 页面生成的原始 http/https，不擅自改写协议。
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def configured_api_url() -> tuple[str, str]:
    row = db.query_one("SELECT value FROM admin_config WHERE key=?", (CONFIG_KEY,))
    if row and row["value"]:
        return _validate_api_url(row["value"]), "db"
    return "", "none"


def save_api_url(url: str) -> None:
    value = _validate_api_url(url)
    db.execute("INSERT INTO admin_config(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CONFIG_KEY, value))


def clear_api_url() -> None:
    db.execute("DELETE FROM admin_config WHERE key=?", (CONFIG_KEY,))


def mask_api_url(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    hidden = {"accessname", "accesspassword", "uid", "rid", "key", "token", "appkey"}
    items = [(k, "***" if k.lower() in hidden else v) for k, v in parse_qsl(p.query, keep_blank_values=True)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(items), ""))


def settings_view() -> dict:
    url, source = configured_api_url()
    return {"configured": bool(url), "source": source, "maskedApiUrl": mask_api_url(url),
            "localRegionCode": LOCAL_REGION_CODE, "localRegionName": LOCAL_REGION_NAME}


def regions(parent_code: str = "") -> list[dict]:
    key = str(parent_code or "").strip()
    cached = _area_cache.get(key)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    payload: dict[str, object] = {"packid": 2, "type": "-1"}
    if key:
        payload["pRegionCode"] = int(key)
    session = requests.Session()
    session.trust_env = False
    response = session.post(AREA_URL, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    raw = data.get("city") if key else data.get("provinceList")
    result = [{"regionCode": str(x.get("regionCode") or ""),
               "regionName": str(x.get("regionName") or ""),
               "parentCode": str(x.get("parentCode") or "")}
              for x in (raw or []) if x.get("regionCode") and x.get("regionName")]
    _area_cache[key] = (time.time(), result)
    return result


def _query_set(items: list[tuple[str, str]], key: str, value: str) -> list[tuple[str, str]]:
    return [(k, v) for k, v in items if k.lower() != key.lower()] + [(key, value)]


def _query_remove(items: list[tuple[str, str]], key: str) -> list[tuple[str, str]]:
    return [(k, v) for k, v in items if k.lower() != key.lower()]


def _provider_url(api_url: str, region_code: str, time_code: str = "") -> tuple[str, dict[str, str]]:
    p = urlsplit(_validate_api_url(api_url))
    items = parse_qsl(p.query, keep_blank_values=True)
    # 51 的 field=ipport 会改变 JSON 中 IP/端口的表现形式。管理员粘贴的
    # 原始链接不需要该参数；统一移除它，并兼容历史返回格式。
    items = _query_remove(items, "field")
    for k, v in (("qty", "1"), ("port", "2"), ("format", "json"),
                 ("regionCode", str(region_code))):
        items = _query_set(items, k, v)
    if time_code:
        items = _query_set(items, "time", str(time_code))
    if any(k.lower() == "rid" for k, _ in items):
        items = _query_set(items, "rid", f"{int(time.time() * 1000):x}{secrets.token_hex(6)}")
    query = {k.lower(): v for k, v in items}
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(items), "")), query


def _extract_item(payload: object) -> dict:
    if isinstance(payload, dict):
        if payload.get("code") not in (None, 0, "0") or payload.get("success") is False:
            raise RuntimeError(str(payload.get("msg") or payload.get("message") or "51代理返回失败"))
        data = payload.get("data", payload)
    else:
        data = payload
    if isinstance(data, list):
        if not data:
            raise RuntimeError("51代理未返回可用 IP")
        data = data[0]
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        line = next((x.strip() for x in data.splitlines() if x.strip()), "")
        host, sep, port = line.rpartition(":")
        if sep:
            return {"IP": host, "Port": port}
    raise RuntimeError("51代理返回格式无法识别")


def _proxy_endpoint(item: dict) -> tuple[str, int]:
    """兼容 51 JSON 的 IP/Port、IP:Port 和 ipport 三种形式。"""
    low = {str(k).lower(): v for k, v in item.items()}
    raw_host = str(low.get("ip") or low.get("host") or low.get("ipport")
                   or low.get("proxy") or "").strip()
    raw_port = str(low.get("port") or "").strip()

    # 少数返回会给完整的 scheme://host:port。
    if "://" in raw_host:
        parsed = urlsplit(raw_host)
        raw_host = parsed.hostname or ""
        if not raw_port:
            try:
                raw_port = str(parsed.port or "")
            except ValueError:
                raw_port = ""

    if raw_host:
        # 纯 IP（特别是未加方括号的 IPv6）应先按 IP 处理，避免误拆最后一段。
        try:
            ipaddress.ip_address(raw_host.strip("[]"))
        except ValueError:
            if raw_host.startswith("[") and "]:" in raw_host:
                closing = raw_host.find("]")
                raw_port = raw_port or raw_host[closing + 2:]
                raw_host = raw_host[1:closing]
            else:
                host_part, sep, port_part = raw_host.rpartition(":")
                if sep and host_part and port_part:
                    raw_host, raw_port = host_part, raw_port or port_part

    host = raw_host.strip().strip("[]")
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        port = 0
    return host, port


def fetch_proxy(region_code: str, time_code: str = "") -> dict:
    if is_local_region(region_code):
        return {"proxyUrl": "", "direct": True}
    api_url = configured_api_url()[0]
    if not api_url:
        raise RuntimeError("管理员尚未配置 51代理短效 API")
    url, query = _provider_url(api_url, region_code, time_code)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        # requests 异常包含完整 URL，禁止把明文 API 凭据透传给普通用户或日志。
        raise RuntimeError(f"51代理 API 请求失败：{type(exc).__name__}") from None
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = response.text
    item = _extract_item(payload)
    low = {str(k).lower(): v for k, v in item.items()}
    host, port = _proxy_endpoint(item)
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # 只提示字段名，不回显供应商响应内容或管理员 API 凭据。
        fields = ",".join(sorted(str(k) for k in item.keys())) or "无"
        raise RuntimeError(f"51代理返回了非法 IP（返回字段：{fields}）") from None
    if not addr.is_global or not (1 <= port <= 65535):
        raise RuntimeError("51代理返回了不可用的内网 IP 或端口")
    password_auth = query.get("ct") == "1" and query.get("skey", "").lower() != "autoaddwhiteip"
    user = query.get("accessname", "") if password_auth else ""
    password = query.get("accesspassword", "") if password_auth else ""
    auth_part = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    url_host = f"[{host}]" if addr.version == 6 else host
    return {"proxyUrl": normalize_proxy_url(f"socks5h://{auth_part}{url_host}:{port}"),
            "direct": False, "address": str(low.get("ipaddress") or ""),
            "isp": str(low.get("isp") or "")}


def acquire(user_id: int, region_code: str, region_name: str = "",
            time_code: str = "") -> dict:
    """提取一个新的短效代理；短效 SOCKS 地址不落库、不复用。"""
    region_code, region_name = str(region_code or "").strip(), str(region_name or "").strip()
    if not region_code:
        raise ValueError("请选择短效代理地区")
    if is_local_region(region_code, region_name):
        return {"proxyUrl": "", "regionCode": LOCAL_REGION_CODE,
                "regionName": region_name or LOCAL_REGION_NAME, "direct": True}
    got = fetch_proxy(region_code, time_code)
    return {**got, "regionCode": region_code, "regionName": region_name,
            "direct": False}


def account_mode(account) -> str:
    mode = (account["proxy_mode"] if "proxy_mode" in account.keys() else "") or ""
    if mode in ("direct", "long", "short"):
        return mode
    return "long" if (account["proxy_url"] or "") else "direct"


def resolve_account_proxy(openid: str, time_code: str = "") -> str:
    account = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
    if not account:
        raise ValueError("account not found")
    mode = account_mode(account)
    if mode == "direct":
        return ""
    if mode == "long":
        return normalize_proxy_url(account["proxy_url"] or "")
    return acquire(account["user_id"], account["proxy_region_code"] or "",
                   account["proxy_region_name"] or "", time_code)["proxyUrl"]


class RenewalProxyBudget:
    """一次完整续期共享的代理预算：代理模式总计最多 5 次，直连不重试。"""

    def __init__(self, openid: str, max_attempts: int = MAX_RENEW_PROXY_ATTEMPTS) -> None:
        account = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
        if not account:
            raise ValueError("account not found")
        self.openid = openid
        self.mode = account_mode(account)
        region_code = account["proxy_region_code"] if "proxy_region_code" in account.keys() else ""
        region_name = account["proxy_region_name"] if "proxy_region_name" in account.keys() else ""
        if self.mode == "short" and is_local_region(region_code or "", region_name or ""):
            self.mode = "direct"
        self.max_attempts = max(1, int(max_attempts)) if self.mode != "direct" else 1
        self.attempts = 0
        self.proxy_url = ""

    def _next_proxy(self) -> None:
        self.proxy_url = resolve_account_proxy(self.openid, time_code=TIME_CODE_RENEW)
        self.attempts += 1

    def call(self, operation):
        """用当前代理执行请求；网络失败才消耗下一次代理尝试。"""
        if self.attempts == 0:
            self._next_proxy()
        while True:
            try:
                return operation(self.proxy_url), self.proxy_url
            except requests.RequestException:
                if self.attempts >= self.max_attempts:
                    raise
                self._next_proxy()


def resolve_params(user_id: int, openid: str, params: dict, default_mode: str = "account",
                   time_code: str = "") -> dict:
    out = dict(params or {})
    raw_mode = str(out.get("proxyMode") or out.get("proxy_mode") or "").strip().lower()
    mode = raw_mode or ("long" if out.get("proxyUrl") or out.get("proxy_url") else default_mode)
    if mode not in ("account", "direct", "long", "short"):
        raise ValueError("proxyMode 仅支持 account/direct/long/short")
    region_code = str(out.get("proxyRegionCode") or out.get("proxy_region_code") or "").strip()
    region_name = str(out.get("proxyRegionName") or out.get("proxy_region_name") or "").strip()
    if mode == "account":
        proxy_url = resolve_account_proxy(openid, time_code=time_code)
        account = db.query_one("SELECT * FROM accounts WHERE openid=?", (openid,))
        if account:
            region_code = account["proxy_region_code"] or ""
            region_name = account["proxy_region_name"] or ""
    elif mode == "direct":
        proxy_url = ""
    elif mode == "long":
        proxy_url = normalize_proxy_url(str(out.get("proxyUrl") or out.get("proxy_url") or "").strip())
        if not proxy_url:
            raise ValueError("长效代理模式必须填写 proxyUrl")
        guard_public_url(proxy_url, allow_schemes=("http", "https", "socks5", "socks5h", "socks4", "socks4a"))
    else:
        got = acquire(user_id, region_code, region_name, time_code=time_code)
        proxy_url, region_name = got["proxyUrl"], got["regionName"]
    out.update({"proxyMode": mode, "proxyUrl": proxy_url, "proxyRegionCode": region_code,
                "proxyRegionName": region_name, "_proxyResolved": True})
    return out


def project_proxy(params: dict, openid: str) -> str:
    if isinstance(params, dict) and params.get("_proxyResolved"):
        return normalize_proxy_url(str(params.get("proxyUrl") or "").strip())
    explicit = normalize_proxy_url(str((params or {}).get("proxyUrl") or "").strip())
    return explicit or resolve_account_proxy(openid)


def safe_proxy_label(proxy_url: str) -> str:
    if not proxy_url:
        return "直连"
    try:
        parsed = urlsplit(proxy_url)
        return (f"{parsed.scheme}://***@{parsed.hostname}:{parsed.port}" if parsed.username
                else f"{parsed.scheme}://{parsed.hostname}:{parsed.port}")
    except ValueError:
        return "已配置代理"
