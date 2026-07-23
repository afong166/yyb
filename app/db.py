"""SQLite 数据层：单库 data/app.db，覆盖 用户/授权码/微信账号/调用记录/项目/面板/审计。
sqlite3 + 行工厂 + 写锁；本地磁盘、访问很快，直接在处理函数里同步调用即可。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .config import DB_PATH

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_config (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  role TEXT NOT NULL DEFAULT 'user',
  note TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  approved_at INTEGER DEFAULT 0,
  last_login_at INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS licenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER UNIQUE NOT NULL,
  license_key TEXT UNIQUE NOT NULL COLLATE NOCASE,
  max_users INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',
  note TEXT DEFAULT '',
  expires_at INTEGER DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  openid TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  accesstoken TEXT DEFAULT '', refreshtoken TEXT DEFAULT '', guid TEXT DEFAULT '',
  appid TEXT DEFAULT '', scope TEXT DEFAULT '', expires_in INTEGER DEFAULT 0, expire_at INTEGER DEFAULT 0,
  nickname TEXT DEFAULT '', head_img_url TEXT DEFAULT '', unionid TEXT DEFAULT '',
  sex INTEGER DEFAULT 0, country TEXT DEFAULT '', province TEXT DEFAULT '', city TEXT DEFAULT '',
  proxy_url TEXT DEFAULT '', status TEXT DEFAULT 'active', status_error TEXT DEFAULT '',
  is_current INTEGER DEFAULT 0, logged_at INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS call_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, openid TEXT DEFAULT '', project_id INTEGER DEFAULT 0, appid TEXT DEFAULT '',
  action TEXT DEFAULT '', result TEXT DEFAULT '', error TEXT DEFAULT '', instance TEXT DEFAULT '',
  code TEXT DEFAULT '', ms INTEGER DEFAULT 0, ip TEXT DEFAULT '', created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS yyb_sessions (
  openid TEXT NOT NULL,
  proxy_url TEXT NOT NULL DEFAULT '',
  blob TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(openid, proxy_url)
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, summary TEXT DEFAULT '', intro TEXT DEFAULT '', tutorial TEXT DEFAULT '',
  icon TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'off', panel_type TEXT DEFAULT '',
  run_config TEXT DEFAULT '', sort_order INTEGER DEFAULT 0,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS panels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL, panel_type TEXT NOT NULL,
  base_url TEXT DEFAULT '', client_id TEXT DEFAULT '', client_secret_enc TEXT DEFAULT '',
  last_test_at INTEGER DEFAULT 0, last_test_ok INTEGER DEFAULT 0,
  created_at INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0,
  UNIQUE(user_id, panel_type)
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id INTEGER DEFAULT 0, action TEXT NOT NULL, target_type TEXT DEFAULT '', target_id TEXT DEFAULT '',
  detail TEXT DEFAULT '', ip TEXT DEFAULT '', created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT DEFAULT '',
  task_type TEXT NOT NULL DEFAULT 'project',   -- 'project'（运行项目）| 'code'（获取Code）
  project_id INTEGER DEFAULT 0,
  appid TEXT DEFAULT '',
  openids TEXT DEFAULT '[]',                    -- JSON 数组：批量账号 openid
  params TEXT DEFAULT '{}',                     -- JSON：项目参数(sn/proxyUrl) + 提交配置(envName/panels)
  cron TEXT DEFAULT '',                         -- 5 段 cron 表达式
  enabled INTEGER DEFAULT 1,
  next_run_at INTEGER DEFAULT 0,
  last_run_at INTEGER DEFAULT 0,
  last_status TEXT DEFAULT '',                  -- ok / fail / partial / running
  last_result TEXT DEFAULT '',                  -- 运行日志文本
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_calls_user ON call_records(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_yyb_sessions_expires ON yyb_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON scheduled_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON scheduled_tasks(enabled, next_run_at);
"""


def init_db() -> None:
    global _conn
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    # 并发/性能调优：WAL 允许读写并发；NORMAL 在 WAL 下安全且写更快；
    # busy_timeout 让偶发锁竞争自动等待而非报错；加大页缓存、临时表放内存加速排序/分组。
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.execute("PRAGMA cache_size=-16000")   # ~16MB 页缓存
    _conn.execute("PRAGMA temp_store=MEMORY")
    _conn.executescript(SCHEMA)
    _migrate(_conn)
    _conn.commit()


def _migrate(c: sqlite3.Connection) -> None:
    """向后兼容的列级迁移：为存量库补齐新增列（ADD COLUMN 是安全的、不动已有数据）。"""
    # call_records.code —— 记录本次调用产出的凭据（wx.login code / 脱敏手机号等），供管理端展示
    cols = {r["name"] for r in c.execute("PRAGMA table_info(call_records)").fetchall()}
    if "code" not in cols:
        c.execute("ALTER TABLE call_records ADD COLUMN code TEXT DEFAULT ''")
    # 脉动扫码节奏由 65s 提升到 90s（对齐新版防风控节奏）：把存量项目里 <90 的间隔一次性抬到 90
    for row in c.execute("SELECT id, run_config FROM projects WHERE run_config LIKE '%maidong-scan%'").fetchall():
        try:
            rc = json.loads(row["run_config"] or "{}")
        except Exception:
            continue
        if float(rc.get("delayBetweenScans") or 0) < 90:
            rc["delayBetweenScans"] = 90
            c.execute("UPDATE projects SET run_config=? WHERE id=?",
                      (json.dumps(rc, ensure_ascii=False), row["id"]))


def conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    return _conn


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        return cur.lastrowid


def now() -> int:
    return int(time.time() * 1000)  # 毫秒，和前端 new Date(ts) 对齐
