"""面板外部调用：青龙(qinglong)、呆呆(daidai)。鉴权取 token → 写环境变量 / 触发任务。"""
from __future__ import annotations

import re
from urllib.parse import unquote

import httpx

from .config import PANEL_TLS_VERIFY

TIMEOUT = 15.0


def _base(url: str) -> str:
    return (url or "").rstrip("/")


def _jd_pin(cookie: str) -> str:
    """从京东 cookie 解析 pt_pin（账号唯一标识）并 URL 解码；非京东 / 取不到返回空串。"""
    m = re.search(r"pt_pin=([^;]+)", cookie or "")
    return unquote(m.group(1)) if m else ""


def _match_account(arr, name: str, pin: str, remarks: str = ""):
    """在同名环境变量里定位「同一账号」：
    - 有 pin（京东 cookie）：按 pt_pin 精确匹配（pin 优先取已有条目 value，其次比对备注）；
      匹配不到即视为新账号 → 返回 None → 调用方新增，各账号互不覆盖。
    - 无 pin（非京东）：优先按备注匹配账号；备注不同就新增，避免饿了么等多账号互相覆盖。
      备注为空时才退回旧逻辑按变量名匹配，兼容老调用。"""
    if not isinstance(arr, list):
        return None
    same_name = [e for e in arr if isinstance(e, dict) and e.get("name") == name]
    if not pin:
        clean_remark = (remarks or "").strip()
        if clean_remark:
            for e in same_name:
                if (e.get("remarks") or e.get("remark") or "").strip() == clean_remark:
                    return e
            return None
        return same_name[0] if same_name else None
    for e in same_name:
        if _jd_pin(e.get("value", "")) == pin or (e.get("remarks") or e.get("remark") or "") == pin:
            return e
    return None


def _deep_find(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            r = _deep_find(v, keys)
            if r not in (None, ""):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_find(v, keys)
            if r not in (None, ""):
                return r
    return ""


# ---------------- 青龙 ----------------
async def _ql_token(c, base, cid, csecret):
    r = await c.get(f"{base}/open/auth/token", params={"client_id": cid, "client_secret": csecret})
    d = r.json() if r.content else {}
    if d.get("code") != 200 or not (d.get("data") or {}).get("token"):
        raise RuntimeError(f"青龙鉴权失败：{d.get('message') or ('HTTP ' + str(r.status_code))}")
    return d["data"]["token"]


async def _ql_test(config):
    base, cid, cs = _base(config["baseUrl"]), config.get("clientId", ""), config.get("clientSecret", "")
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=PANEL_TLS_VERIFY) as c:
        tok = await _ql_token(c, base, cid, cs)
        r = await c.get(f"{base}/open/envs", params={"searchValue": ""},
                        headers={"Authorization": f"Bearer {tok}"})
        d = r.json() if r.content else {}
        if d.get("code") != 200:
            raise RuntimeError("青龙接口校验失败")
    return {"ok": True, "message": "青龙连接成功"}


async def _ql_upsert_env(config, name, value, remarks):
    base, cid, cs = _base(config["baseUrl"]), config.get("clientId", ""), config.get("clientSecret", "")
    pin = _jd_pin(value)
    eff_remarks = pin or remarks  # 京东 cookie：备注填 pt_pin，保证每个账号唯一、不被覆盖
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=PANEL_TLS_VERIFY) as c:
        tok = await _ql_token(c, base, cid, cs)
        h = {"Authorization": f"Bearer {tok}"}
        r = await c.get(f"{base}/open/envs", params={"searchValue": name}, headers=h)
        arr = (r.json().get("data") or []) if r.content else []
        match = _match_account(arr, name, pin, eff_remarks)
        if match:
            await c.put(f"{base}/open/envs", json={"id": match["id"], "name": name, "value": value, "remarks": eff_remarks}, headers=h)
            await c.put(f"{base}/open/envs/enable", json=[match["id"]], headers=h)
            act = "已更新"
        else:
            await c.post(f"{base}/open/envs", json=[{"name": name, "value": value, "remarks": eff_remarks}], headers=h)
            act = "已新增"
    return {"ok": True, "message": f"青龙：{act} {name}" + (f"（{pin or eff_remarks}）" if (pin or eff_remarks) else "")}


# ---------------- 呆呆 ----------------
_DD_TOKEN_PATHS = ["/api/v1/open-api/token", "/api/open-api/token", "/open-api/token"]


async def _dd_token(c, base, app_key, app_secret):
    last = ""
    for p in _DD_TOKEN_PATHS:
        try:
            r = await c.post(f"{base}{p}", json={"app_key": app_key, "app_secret": app_secret})
            d = r.json() if r.content else {}
            tok = _deep_find(d, ["access_token", "token"])
            if tok:
                return tok
            last = d.get("message") or d.get("msg") or f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"呆呆面板鉴权失败：{last}（请确认面板地址、App Key / App Secret 正确）")


async def _dd_test(config):
    base = _base(config["baseUrl"])
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=PANEL_TLS_VERIFY) as c:
        tok = await _dd_token(c, base, config.get("clientId", ""), config.get("clientSecret", ""))
    return {"ok": bool(tok), "message": "呆呆面板连接成功"}


async def _dd_upsert_env(config, name, value, remarks):
    base = _base(config["baseUrl"])
    pin = _jd_pin(value)
    eff_remarks = pin or remarks  # 京东 cookie：备注填 pt_pin，保证每个账号唯一、不被覆盖
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=PANEL_TLS_VERIFY) as c:
        tok = await _dd_token(c, base, config.get("clientId", ""), config.get("clientSecret", ""))
        h = {"Authorization": f"Bearer {tok}"}
        r = await c.get(f"{base}/api/v1/envs", params={"search": name, "keyword": name}, headers=h)
        arr = _deep_find(r.json() if r.content else {}, ["list", "data", "items"]) or []
        match = _match_account(arr if isinstance(arr, list) else [], name, pin, eff_remarks)
        if match and match.get("id"):
            await c.put(f"{base}/api/v1/envs/{match['id']}",
                        json={"name": name, "value": value, "remarks": eff_remarks, "enabled": True}, headers=h)
            act = "已更新"
        else:
            await c.post(f"{base}/api/v1/envs", json={"name": name, "value": value, "remarks": eff_remarks}, headers=h)
            act = "已新增"
    return {"ok": True, "message": f"呆呆：{act} {name}" + (f"（{pin or eff_remarks}）" if (pin or eff_remarks) else "")}


# ---------------- 统一入口 ----------------
async def test_connection(panel_type, config):
    try:
        return await (_ql_test(config) if panel_type == "qinglong" else _dd_test(config))
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def submit_env(panel_type, config, env):
    try:
        fn = _ql_upsert_env if panel_type == "qinglong" else _dd_upsert_env
        return await fn(config, env["name"], env["value"], env.get("remarks", ""))
    except Exception as e:
        return {"ok": False, "error": str(e)}
