"""FastAPI 应用：路由挂载、CSRF、统一错误、静态前端托管、启动播种/续期。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .logs import event, log, setup_logging

setup_logging()

from . import auth, codebridge, db, scheduler, yyblogin
from .config import ADMIN_TOKEN, API_HOST, API_PORT, DIST_ADMIN, DIST_USER
from .routers import accounts, admin, auth as auth_r, code, info, licenses, login, panels, projects, proxy, tasks
from .util import client_ip

# /api 访问日志跳过的高频路径（扫码轮询已由 checkQr 语义日志按状态变化打点，避免刷屏）
_ACCESS_LOG_SKIP = {"/api/login/status"}

# 瑞幸「幸运星期三」抽奖活动期数列表（最新一期在前）。抓到新一期后在此追加一项，
# 启动时会把该列表同步进项目 run_config.activities，并把默认活动设为最新一期。
# label 展示给用户；activityNo/activityId 为当期活动标识（抓包 baseInfoStatic 可得）。
LUCKIN_ACTIVITIES = [
    {"label": "幸运星期三 0708（7/8 正式）", "activityNo": "CJ202607029027751995", "activityId": 1367},
    {"label": "历史活动（1361）", "activityNo": "CJ202607613231400711", "activityId": 1361},
]


def _seed():
    if not auth.get_admin_password_hash():
        auth.set_admin_password(ADMIN_TOKEN)  # 初始管理员密码 = 管理员令牌
        db.execute("INSERT OR IGNORE INTO admin_config(key,value) VALUES('admin_created_at',?)", (str(db.now()),))
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%jd-code-login%'"):
        rc = {"builtin": "jd-code-login", "appid": "wx73247c7819d61796",
              "submitPanels": ["qinglong", "daidai"], "envName": "JD_COOKIE"}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("京东 Code 登录获取 Cookie", "用微信账号取京东小程序 code 换取京东 Cookie",
             "取京东小程序 wx.login code → 静默授权换 JD Cookie，可一键提交到青龙/呆呆面板。",
             "1. 先在「控制台」扫码添加微信号；2. 选账号运行获取 JD_COOKIE；3. 在面板设置配置青龙/呆呆后可一键提交。",
             "🛒", "on", "", json.dumps(rc, ensure_ascii=False), 100, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%eleme-code-login%'"):
        rc = {"builtin": "eleme-code-login", "appid": "wxece3a9a4c82f58c9",
              "submitPanels": ["qinglong", "daidai"], "envName": "ELEME_COOKIE"}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("饿了么 Code 登录获取 Cookie", "用微信账号取饿了么小程序 code 换取饿了么 Cookie",
             "取饿了么小程序 wx.login code → Havana 登录换饿了么 Cookie，可一键提交到青龙/呆呆面板。",
             "1. 先在「控制台」扫码添加微信号；2. 选账号运行获取 ELEME_COOKIE；3. 在面板设置配置青龙/呆呆后可一键提交。",
             "🍔", "on", "", json.dumps(rc, ensure_ascii=False), 99, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%mxbc-code-login%'"):
        rc = {"builtin": "mxbc-code-login", "appid": "wx7696c66d2245d107",
              "submitPanels": ["qinglong", "daidai"], "envName": "MXBC_TOKEN"}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("蜜雪冰城 Code 登录获取 Token", "用微信账号取蜜雪冰城小程序 code 换取蜜雪冰城 accessToken",
             "取蜜雪冰城小程序 wx.login code -> code2Session -> regByUnionid 换取 accessToken，可一键提交到青龙/呆呆面板。",
             "1. 先在「控制台」扫码添加微信号；2. 选择账号运行获取 MXBC_TOKEN；3. 在面板设置配置青龙/呆呆后可一键提交。",
             "🍦", "on", "", json.dumps(rc, ensure_ascii=False), 98, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%maidong-scan%'"):
        rc = {"builtin": "maidong-scan", "appid": "wxef2336428c3873d2",
              "activityCode": "UTC202603311335149717", "scan": True, "lottery": True,
              "prepareUser": False, "autoPrepareUser": True, "delayBetweenScans": 90,
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("脉动扫码抽奖", "选择微信账号并输入瓶盖码/SN，直接执行脉动扫码和抽奖",
             "使用已绑定微信账号获取脉动小程序 wx.login code，登录脉动活动接口后执行扫码；有剩余次数时自动抽奖。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号；3. 在 SN 输入框粘贴瓶盖码链接；4. 点击运行查看扫码/抽奖结果。",
             "🥤", "on", "", json.dumps(rc, ensure_ascii=False), 97, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%nongwu-tavern%'"):
        rc = {"builtin": "nongwu-tavern", "appid": "wxed3cf95a14b58a26",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("浓五的酒馆 签到抽奖", "选择微信账号，自动完成浓五的酒馆签到、积分抽奖、刮刮乐",
             "使用已绑定微信账号获取 dtmiller / wlnxjc 小程序 code，自动执行每日签到、浓友购积分抽奖与刮刮乐（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号（异地建议设代理防风控）；3. 点击运行查看签到/抽奖/刮刮乐结果。",
             "🍶", "on", "", json.dumps(rc, ensure_ascii=False), 96, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%yihetang-sign%'"):
        rc = {"builtin": "yihetang-sign", "appid": "wx4080846d0cec2fd5",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("益禾堂 积分签到", "选择微信账号，自动完成益禾堂每日积分签到",
             "使用已绑定微信账号获取益禾堂小程序 wx.login code，登录换 qm-user-token（未绑定手机号会自动授权），"
             "再换兑吧活动页登录态执行每日签到（签到 token 由服务端 JS 生成，需服务器可用 Node.js；异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号（异地建议设代理防风控）；3. 点击运行查看签到结果。",
             "🧋", "on", "", json.dumps(rc, ensure_ascii=False), 95, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%meituan-code-login%'"):
        rc = {"builtin": "meituan-code-login", "appid": "wxde8ac0a21135c07d",
              "submitPanels": ["qinglong", "daidai"], "envName": "MT_TOKEN"}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("美团 Code 登录获取 Token", "用微信账号取美团小程序 code 换取 userId / token / openId",
             "取美团小程序 wx.login code -> 本地纯算 mtgsig 签名 -> weappsilentlogin 换取 userId / token / openId / unionId，可一键提交到青龙/呆呆面板。",
             "1. 先在「控制台」扫码添加微信号；2. 选择账号运行获取 MT_TOKEN（token）；3. 在面板设置配置青龙/呆呆后可一键提交。",
             "🛵", "on", "", json.dumps(rc, ensure_ascii=False), 94, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%hongse-huojian%'"):
        rc = {"builtin": "hongse-huojian", "appid": "wx1b44c3ad181bde16",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("红色火箭 签到领红包", "选择微信账号，自动完成华泰基金指慧家签到、ROE 口令兑换与红包领取",
             "使用已绑定微信账号获取 wx.login code / 手机号授权 code / 云函数 encrypt_key，登录后执行每日签到、"
             "动态发现活动并按指数 ROE 生成口令 SM4 加密兑换，命中红包时自动走 H5 领取，最后汇总积分与红包（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号（异地建议设代理防风控）；3. 点击运行查看签到/兑换/红包结果。",
             "🚀", "on", "", json.dumps(rc, ensure_ascii=False), 93, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%luckin-draw%'"):
        _newest = LUCKIN_ACTIVITIES[0]
        rc = {"builtin": "luckin-draw", "appid": "wx21c7506e98a2fe75",
              "activityNo": _newest["activityNo"], "activityId": _newest["activityId"],
              "activities": LUCKIN_ACTIVITIES,
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("瑞幸咖啡 活动抽奖", "选择微信账号，自动完成瑞幸咖啡活动抽奖并汇总中奖记录",
             "使用已绑定微信账号获取瑞幸小程序 wx.login code（登录若需手机号授权会自动同步 iv/encryptedData），"
             "登录后获取 authCode 打开活动页校验，读取活动详情并执行抽奖，最后汇总本次结果与历史中奖记录（异地建议设代理防风控）。"
             "运行时可在下拉里选择要跑的活动期数（默认最新一期）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号与活动期数（异地建议设代理防风控）；3. 点击运行查看抽奖结果。",
             "☕", "on", "", json.dumps(rc, ensure_ascii=False), 92, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%yihetang-lottery%'"):
        rc = {"builtin": "yihetang-lottery", "appid": "wx4080846d0cec2fd5",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("益禾堂 抽奖", "选择微信账号，自动完成益禾堂活动抽奖并汇总中奖结果",
             "使用已绑定微信账号获取益禾堂小程序 wx.login code，登录换 qm-user-token / userId（未绑手机号会自动授权），"
             "再按活动签名（activityId 反转做 key + MD5）参与抽奖，汇总本次中奖 / 未中奖 / 剩余次数（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号（异地建议设代理防风控）；3. 点击运行查看抽奖结果。",
             "🎁", "on", "", json.dumps(rc, ensure_ascii=False), 91, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%hsay-sign%'"):
        rc = {"builtin": "hsay-sign", "appid": "wxd92a2d29f8022f40",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("沪上阿姨 签到", "选择微信账号，自动完成沪上阿姨小满活动每日签到",
             "使用已绑定微信账号获取沪上阿姨小程序 wx.login code，登录换会员（未注册会自动授权手机号注册），"
             "打开小满活动授权页刷新活动态，动态生成 tokenSign / xmSign 完成每日签到（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号（异地建议设代理防风控）；3. 点击运行查看签到结果。",
             "🫖", "on", "", json.dumps(rc, ensure_ascii=False), 90, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%binghongcha-scan%'"):
        rc = {"builtin": "binghongcha-scan", "appid": "wx54f3e6a00f7973a7",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("冰红茶 开盖赢奖", "选择微信账号并粘贴瓶盖码，自动完成康师傅冰红茶 1L 码上赢黄金扫码抽奖",
             "使用已绑定微信账号获取康师傅冰红茶小程序 wx.login code，codeLogin 换活动登录态，"
             "对每个瓶盖码执行扫码抽奖并查询奖品（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号；3. 在瓶盖码框粘贴一行一个的瓶盖码链接；4. 点击运行查看扫码/中奖结果。",
             "🧊", "on", "", json.dumps(rc, ensure_ascii=False), 89, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%ksf-scan%'"):
        rc = {"builtin": "ksf-scan", "appid": "wx54f3e6a00f7973a7",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("康师傅 开盖赢奖", "选择微信账号并粘贴瓶盖码，自动完成康师傅开盖扫码抽奖",
             "使用已绑定微信账号获取康师傅小程序 wx.login code，会员登录 + ciphertext 换活动登录态，"
             "对每个瓶盖码执行扫码抽奖，中奖自动核销并返回核销二维码信息（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号；3. 在瓶盖码框粘贴一行一个的瓶盖码链接；4. 点击运行查看扫码/中奖结果。",
             "🍜", "on", "", json.dumps(rc, ensure_ascii=False), 88, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%lehu-scan%'"):
        rc = {"builtin": "lehu-scan", "appid": "wx1e7ba839c6bc0a27",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("乐虎 开盖赢奖", "选择微信账号并粘贴瓶盖码，自动完成乐虎开盖赢红包扫码抽奖",
             "使用已绑定微信账号获取乐虎小程序 wx.login code，换达利小程序 Token 识别活动，"
             "对每个瓶盖码执行扫码抽奖，命中现金红包自动提现（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号；3. 在瓶盖码框粘贴一行一个的瓶盖码链接；4. 点击运行查看扫码/中奖/提现结果。",
             "⚡", "on", "", json.dumps(rc, ensure_ascii=False), 87, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%nongfu-scan%'"):
        rc = {"builtin": "nongfu-scan", "appid": "wx3723729dc4ac916c",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("农夫山泉 开盖赢奖", "选择微信账号并粘贴瓶盖码，自动完成农夫山泉开盖扫码抽奖",
             "使用已绑定微信账号获取农夫山泉小程序 wx.login code，登录后按活动 AES/MD5 参数算法开盖抽奖并汇总奖品记录"
             "（部分风控严格时可能需要 deviceToken；异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号；3. 在瓶盖码框粘贴一行一个的瓶盖码；4. 点击运行查看开盖/中奖结果。",
             "💧", "on", "", json.dumps(rc, ensure_ascii=False), 86, db.now(), db.now()),
        )
    if not db.query_one("SELECT id FROM projects WHERE run_config LIKE '%wanglaoji-scan%'"):
        rc = {"builtin": "wanglaoji-scan", "appid": "wxd25dc8ba975776e3",
              "submitPanels": [], "envName": ""}
        db.execute(
            "INSERT INTO projects(name,summary,intro,tutorial,icon,status,panel_type,run_config,sort_order,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("王老吉 开盖赢奖", "选择微信账号并粘贴瓶盖码，自动完成王老吉开盖扫码赢礼金",
             "使用已绑定微信账号获取王老吉小程序 wx.login code，会员登录换活动 Token（未注册会自动授权注册），"
             "对每个瓶盖码执行扫码抽奖，中奖自动领取礼金红包（异地建议设代理防风控）。",
             "1. 先在「控制台」扫码添加微信号；2. 进入本项目选择微信账号；3. 在瓶盖码框粘贴一行一个的瓶盖码链接；4. 点击运行查看扫码/中奖/领取结果。",
             "🥫", "on", "", json.dumps(rc, ensure_ascii=False), 85, db.now(), db.now()),
        )
    _sync_luckin_activities()


def _sync_luckin_activities():
    """把已存在的瑞幸项目 run_config 同步为最新的活动期数列表，并将默认活动设为最新一期。

    新一期活动只需在 LUCKIN_ACTIVITIES 里追加一项，重启后：activities 列表刷新给前端下拉，
    默认 activityNo / activityId 自动指向最新一期。保留 appid / submitPanels 等其它配置不动。
    """
    row = db.query_one("SELECT id, run_config FROM projects WHERE run_config LIKE '%luckin-draw%'")
    if not row:
        return
    try:
        rc = json.loads(row["run_config"] or "{}")
    except Exception:
        rc = {}
    newest = LUCKIN_ACTIVITIES[0]
    if (rc.get("activities") == LUCKIN_ACTIVITIES
            and rc.get("activityNo") == newest["activityNo"]
            and rc.get("activityId") == newest["activityId"]):
        return
    rc["activities"] = LUCKIN_ACTIVITIES
    rc["activityNo"] = newest["activityNo"]
    rc["activityId"] = newest["activityId"]
    db.execute("UPDATE projects SET run_config=?, updated_at=? WHERE id=?",
               (json.dumps(rc, ensure_ascii=False), db.now(), row["id"]))
    event("seed", "同步瑞幸活动期数", 最新活动=newest["label"], activityId=newest["activityId"])


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _seed()
    with contextlib.suppress(Exception):
        n = yyblogin.repair_account_text()  # 修复历史昵称/地区的 latin-1 误解码乱码
        if n:
            event("boot", "修复历史账号文本编码", 修复账号数=n)
    await codebridge.startup()

    async def renew_loop():
        from . import svc
        from .config import RECORD_RETENTION_DAYS
        last_purge = 0.0
        while True:
            await asyncio.sleep(180)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(yyblogin.auto_renew_tick)
            # 每天清理一次超期的调用记录/审计日志，防止单库无限膨胀
            if RECORD_RETENTION_DAYS > 0 and (time.time() - last_purge) > 86400:
                last_purge = time.time()
                with contextlib.suppress(Exception):
                    n = await asyncio.to_thread(svc.purge_old_records, RECORD_RETENTION_DAYS)
                    if n:
                        event("maintenance", "清理超期记录", 删除行数=n, 保留天数=RECORD_RETENTION_DAYS)

    task = asyncio.create_task(renew_loop())
    sched = asyncio.create_task(scheduler.scheduler_loop())
    event("boot", "服务已启动", 监听=f"{API_HOST}:{API_PORT}")
    yield
    task.cancel()
    sched.cancel()
    await codebridge.shutdown()


app = FastAPI(title="应用宝取码", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"success": False, "error": exc.detail})


@app.exception_handler(Exception)
async def any_exc(request: Request, exc: Exception):
    # 内部异常详情只记服务端日志，对外统一返回通用错误，避免泄漏路径/DB/堆栈信息。
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"success": False, "error": "internal error"})


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@app.middleware("http")
async def csrf(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api"):
        has_cookie = auth.USER_COOKIE in request.cookies or auth.ADMIN_COOKIE in request.cookies
        if has_cookie and request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return JSONResponse(status_code=403, content={"success": False, "error": "CSRF 校验失败：缺少 X-Requested-With"})
    resp = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


@app.middleware("http")
async def access_log(request: Request, call_next):
    """/api 请求访问日志：每个接口调用打一行（跳过高频扫码轮询与静态资源）。"""
    path = request.url.path
    t0 = time.time()
    resp = await call_next(request)
    if path.startswith("/api") and path not in _ACCESS_LOG_SKIP:
        event("http", f"{request.method} {path}", 状态=resp.status_code,
              耗时ms=int((time.time() - t0) * 1000), 来源=client_ip(request))
    return resp


# ---- API 路由 ----
app.include_router(auth_r.router, prefix="/api/auth")
app.include_router(admin.router, prefix="/api/admin")
app.include_router(licenses.router, prefix="/api/licenses")
app.include_router(login.router, prefix="/api/login")
app.include_router(accounts.router, prefix="/api/accounts")
app.include_router(code.router, prefix="/api/yyb")
app.include_router(code.wx_router, prefix="/wx")
app.include_router(projects.router, prefix="/api/projects")
app.include_router(tasks.router, prefix="/api/tasks")
app.include_router(panels.router, prefix="/api/panels")
app.include_router(proxy.router, prefix="/api/proxy")
app.include_router(proxy.admin_router, prefix="/api/admin/proxy")
app.include_router(info.router, prefix="/api")


# ---- 静态前端（SPA 回退）----
def _serve(dist, path: str):
    idx = dist / "index.html"
    if path:
        f = (dist / path).resolve()
        with contextlib.suppress(Exception):
            if f.is_file() and str(f).startswith(str(dist.resolve())):
                return FileResponse(f)
    if idx.is_file():
        return FileResponse(idx)
    return JSONResponse(status_code=503, content={"success": False, "error": "前端未构建（缺 dist/web）"})


@app.get("/admin")
@app.get("/admin/{path:path}")
async def admin_spa(path: str = ""):
    return _serve(DIST_ADMIN, path)


@app.get("/changelog")
async def changelog_redirect():
    # 兼容旧 history 地址；新版前端已改成 hash 路由，刷新只请求 /，不会再请求 /changelog。
    return RedirectResponse(url="/#/changelog", status_code=302)


@app.get("/{path:path}")
async def user_spa(path: str = ""):
    if path.startswith("api/") or path.startswith("wx/") or path in ("api", "wx"):
        return JSONResponse(status_code=404, content={"success": False, "error": f"not found: /{path}"})
    return _serve(DIST_USER, path)
