"""/api/projects/* — 用户侧项目（已上架）。run 支持内置 jd-code-login + 面板触发；submit 写面板环境变量。"""
from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, HTTPException, Request

from .. import db, deps, models, panels_ext, runstream, shortproxy
from ..logsink import log_sink
from ..jd import run_jd_code_login
from ..eleme import run_eleme_code_login
from ..mxbc import run_mxbc_code_login
from ..maidong import run_maidong_project
from ..nongwu import run_nongwu_project
from ..yihetang import run_yihetang_project
from ..yihetang_lottery import run_yihetang_lottery_project
from ..husheng import run_husheng_project
from ..binghongcha import run_binghongcha_project
from ..ksf import run_ksf_project
from ..lehu import run_lehu_project
from ..nongfu import run_nongfu_project
from ..wanglaoji import run_wanglaoji_project
from ..meituan import run_meituan_code_login
from ..hongse import run_hongse_project
from ..luckin import run_luckin_project
from ..logs import actor_name, event, mask_id
from ..util import get_body, client_ip
from .panels import get_panel_config

router = APIRouter()


async def _resolve_proxy_in(uid: int, openid: str, params: dict) -> dict:
    """解析 account/direct/long/short，并把实际代理一次性传给业务运行器。"""
    try:
        return await asyncio.to_thread(shortproxy.resolve_params, uid, openid, params or {}, "account")
    except (ValueError, RuntimeError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(400, detail)

# 内置项目运行器（模块级，供项目运行接口与定时任务调度器共用）
CODE_LOGIN_RUNNERS = {
    "jd-code-login": run_jd_code_login,
    "eleme-code-login": run_eleme_code_login,
    "mxbc-code-login": run_mxbc_code_login,
    "meituan-code-login": run_meituan_code_login,
}
# 执行类项目：取码后在服务端跑任务，入参用 run_config + 表单参数合并
ACTION_RUNNERS = {
    "maidong-scan": run_maidong_project,
    "nongwu-tavern": run_nongwu_project,
    "yihetang-sign": run_yihetang_project,
    "yihetang-lottery": run_yihetang_lottery_project,
    "hsay-sign": run_husheng_project,
    "binghongcha-scan": run_binghongcha_project,
    "ksf-scan": run_ksf_project,
    "lehu-scan": run_lehu_project,
    "nongfu-scan": run_nongfu_project,
    "wanglaoji-scan": run_wanglaoji_project,
    "hongse-huojian": run_hongse_project,
    "luckin-draw": run_luckin_project,
}


def _record(uid, action, result, openid="", project_id=0, ip="", appid="", code="", ms=0):
    ok = result.get("ok") or result.get("success")
    db.execute("INSERT INTO call_records(user_id,openid,project_id,appid,action,result,error,code,ms,ip,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (uid, openid, project_id, appid, action, "ok" if ok else "fail",
                (result.get("error") or "")[:500], (code or "")[:120] if ok else "", int(ms or 0), ip, db.now()))


def _panel_remark(uid: int, params: dict) -> str:
    """面板变量备注：非京东项目靠备注区分账号，避免同名变量覆盖。

    优先使用项目登录结果里的账号标识（如饿了么 username/userId、美团 userId、蜜雪手机号掩码）；
    没有时再用当前微信账号昵称 + openid 尾号，既能区分账号，又不暴露完整 openid。
    """
    for key in ("ptPin", "account", "username", "userId", "remarks", "remark"):
        value = str(params.get(key) or "").strip()
        if value:
            return value.replace("\r", " ").replace("\n", " ")[:80]
    openid = str(params.get("openid") or "").strip()
    if not openid or openid not in deps.account_openids(uid):
        return "自动提交"
    row = db.query_one("SELECT nickname FROM accounts WHERE openid=? AND user_id=?", (openid, uid))
    name = (row["nickname"] if row and row["nickname"] else "微信账号").strip()
    return f"{name}-{openid[-6:]}"[:80]


@router.get("")
async def list_projects(request: Request):
    deps.require_user(request)
    rows = db.query("SELECT * FROM projects WHERE status='on' ORDER BY sort_order DESC, id DESC")
    return {"success": True, "projects": [models.project_view(p) for p in rows]}


@router.get("/{pid}")
async def get_project(request: Request, pid: int):
    deps.require_user(request)
    p = db.query_one("SELECT * FROM projects WHERE id=? AND status='on'", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在或未上架")
    return {"success": True, "project": models.project_view(p, full=True)}


@router.post("/{pid}/run")
async def run_project(request: Request, pid: int):
    uid = deps.require_user(request)
    p = db.query_one("SELECT * FROM projects WHERE id=? AND status='on'", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在或未上架")
    rc = {}
    try:
        rc = json.loads(p["run_config"] or "{}")
    except Exception:
        rc = {}
    b = await get_body(request)
    params = b.get("params") or {}
    ip = client_ip(request)

    builtin = rc.get("builtin")
    if builtin in CODE_LOGIN_RUNNERS or builtin in ACTION_RUNNERS:
        openid = params.get("openid") or ""
        if not openid:
            raise HTTPException(400, "请选择要使用的微信账号")
        if openid not in deps.account_openids(uid):
            raise HTTPException(403, "该微信账号不属于你")
        params = await _resolve_proxy_in(uid, openid, params)
        appid = rc.get("appid") or ""
        t0 = time.time()
        if builtin in ACTION_RUNNERS:
            result = await ACTION_RUNNERS[builtin](uid, openid, appid, {**rc, **params})
        else:
            result = await CODE_LOGIN_RUNNERS[builtin](uid, openid, appid, params.get("proxyUrl") or "")
        _record(uid, builtin, result, openid, pid, ip,
                appid=appid, code=result.get("sn") or result.get("code") or "", ms=int((time.time() - t0) * 1000))
        event("runProject", "运行项目", project=actor_name(uid), 来源=ip, 项目=p["name"],
              openid=mask_id(openid), 结果="成功" if result.get("ok") else "失败")
        if not result.get("ok"):
            raise HTTPException(400, result.get("error") or "运行失败")
        return {"success": True, "result": result}

    # 面板触发型
    ptype = p["panel_type"]
    if not ptype:
        raise HTTPException(400, "该项目未配置面板运行方式")
    cfg = get_panel_config(uid, ptype)
    if not cfg:
        raise HTTPException(400, f"请先在「面板设置」中配置并测试 {ptype} 面板")
    raise HTTPException(400, "该项目类型暂不支持一键运行")


async def _run_builtin(uid: int, rc: dict, builtin: str, params: dict):
    openid = params.get("openid") or ""
    params = await _resolve_proxy_in(uid, openid, params)
    appid = rc.get("appid") or ""
    if builtin in ACTION_RUNNERS:
        result = await ACTION_RUNNERS[builtin](uid, openid, appid, {**rc, **params})
    else:
        result = await CODE_LOGIN_RUNNERS[builtin](uid, openid, appid, params.get("proxyUrl") or "")
    return result


@router.post("/{pid}/run-start")
async def run_start(request: Request, pid: int):
    """启动一次后台运行，立即返回 runId；前端用 /run-poll 增量拉取实时日志与最终结果。"""
    uid = deps.require_user(request)
    p = db.query_one("SELECT * FROM projects WHERE id=? AND status='on'", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在或未上架")
    try:
        rc = json.loads(p["run_config"] or "{}")
    except Exception:
        rc = {}
    builtin = rc.get("builtin")
    if builtin not in CODE_LOGIN_RUNNERS and builtin not in ACTION_RUNNERS:
        raise HTTPException(400, "该项目暂不支持一键运行")
    b = await get_body(request)
    params = b.get("params") or {}
    openid = params.get("openid") or ""
    if not openid:
        raise HTTPException(400, "请选择要使用的微信账号")
    if openid not in deps.account_openids(uid):
        raise HTTPException(403, "该微信账号不属于你")

    ip = client_ip(request)
    pname, appid = p["name"], rc.get("appid") or ""
    run_id = runstream.new_run(uid)

    async def _job():
        runstream.push(run_id, f"[INFO] 开始运行：{pname}")
        token = log_sink.set(lambda line: runstream.push(run_id, line))
        t0 = time.time()
        try:
            result = await _run_builtin(uid, rc, builtin, params)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        finally:
            log_sink.reset(token)
        try:
            _record(uid, builtin, result, openid, pid, ip, appid=appid,
                    code=result.get("sn") or result.get("code") or "", ms=int((time.time() - t0) * 1000))
            event("runProject", "运行项目", project=actor_name(uid), 来源=ip, 项目=pname,
                  openid=mask_id(openid), 结果="成功" if result.get("ok") else "失败")
        except Exception:
            pass
        if not result.get("ok"):
            runstream.push(run_id, f"[ERROR] {result.get('error') or '运行失败'}")
        runstream.finish(run_id, result)

    asyncio.create_task(_job())
    return {"success": True, "runId": run_id}


@router.get("/{pid}/run-poll")
async def run_poll(request: Request, pid: int, runId: str = "", cursor: int = 0):
    uid = deps.require_user(request)
    st = runstream.poll(runId, uid, cursor)
    if st is None:
        raise HTTPException(404, "运行会话不存在或已过期")
    return {"success": True, **st}


@router.post("/{pid}/submit")
async def submit_project(request: Request, pid: int):
    uid = deps.require_user(request)
    p = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在")
    rc = {}
    try:
        rc = json.loads(p["run_config"] or "{}")
    except Exception:
        rc = {}
    b = await get_body(request)
    params = b.get("params") or b
    env_name = params.get("envName") or ""
    value = params.get("value") or ""
    want = params.get("panels") or rc.get("submitPanels") or []
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", env_name):
        raise HTTPException(400, "环境变量名不合法")
    if not value:
        raise HTTPException(400, "缺少要提交的值")
    allowed = set(rc.get("submitPanels") or [])
    targets = [pt for pt in want if pt in allowed and get_panel_config(uid, pt)]
    if not targets:
        raise HTTPException(400, "未选择有效的提交面板")
    ip = client_ip(request)
    remarks = _panel_remark(uid, params)
    results = []
    for pt in targets:
        t0 = time.time()
        r = await panels_ext.submit_env(pt, get_panel_config(uid, pt),
                                        {"name": env_name, "value": value, "remarks": remarks})
        _record(uid, "submit-panel", r, "", pid, ip, ms=int((time.time() - t0) * 1000))
        results.append({"panel": pt, "ok": bool(r.get("ok")),
                        **({"message": r["message"]} if r.get("ok") else {"error": r.get("error")})})
    ok = sum(1 for r in results if r["ok"])
    event("submitProject", "提交面板", project=actor_name(uid), 来源=ip, 项目=p["name"],
          环境变量=env_name, 结果=f"{ok}/{len(results)}")
    return {"success": True, "results": results}
