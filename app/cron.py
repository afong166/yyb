"""极简 6 段 cron 支持：秒 分 时 日 月 周（0/7=周日）。

用于定时任务：校验表达式、按当前时间算下一次运行、生成人类可读描述。
支持 `*`、`*/n`、`a-b`、`a-b/n`、`a,b,c` 组合。分钟粒度前推匹配（上限 366 天），
命中分钟后再在该分钟内取满足秒字段的最小秒；简单可靠，性能足够。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 定时任务统一按中国（上海）时区计算：中国全年 UTC+8、无夏令时，用固定偏移即等价 Asia/Shanghai，
# 且不依赖系统 tzdata。这样无论服务器（常见 Docker/海外机为 UTC）本地时区如何，
# 「每天 08:00」都稳定表示北京时间 08:00。
CN_TZ = timezone(timedelta(hours=8))

# field index -> (lo, hi)：秒 分 时 日 月 周
_BOUNDS = {
    0: (0, 59),   # second
    1: (0, 59),   # minute
    2: (0, 23),   # hour
    3: (1, 31),   # day of month
    4: (1, 12),   # month
    5: (0, 6),    # day of week (0/7 = Sunday)
}


def _field_match(expr: str, value: int, lo: int, hi: int) -> bool:
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        rng = part
        if "/" in part:
            rng, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                continue
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(rng)
        if start > end:
            continue
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def _norm_dow(expr: str) -> str:
    # cron 里 7 也表示周日，统一成 0
    return ",".join("0" if tok == "7" else tok for tok in expr.replace(" ", "").split(","))


def _minute_matches(fields: list[str], dt: datetime) -> bool:
    """匹配 分/时/日/月/周（不含秒），秒在 next_run 里单独处理。"""
    _sec, minute, hour, dom, month, dow = fields
    if not _field_match(minute, dt.minute, 0, 59):
        return False
    if not _field_match(hour, dt.hour, 0, 23):
        return False
    if not _field_match(month, dt.month, 1, 12):
        return False
    cron_dow = (dt.weekday() + 1) % 7  # python Mon=0..Sun=6 → cron Sun=0..Sat=6
    d_dom = _field_match(dom, dt.day, 1, 31)
    d_dow = _field_match(_norm_dow(dow), cron_dow, 0, 6)
    # 周日既是 0 也是 7：范围写法(如 5-7)里的 7 不会被 _norm_dow 归一，这里对周日补测一次 7。
    if not d_dow and cron_dow == 0:
        d_dow = _field_match(dow, 7, 0, 7)
    if dom.strip() == "*" or dow.strip() == "*":
        return d_dom and d_dow
    return d_dom or d_dow


def validate(expr: str) -> list[str]:
    """校验 6 段 cron 表达式（含数值范围）；合法返回字段列表，否则抛 ValueError。"""
    fields = (expr or "").split()
    if len(fields) != 6:
        raise ValueError("cron 表达式需为 6 段：秒 分 时 日 月 周")
    for i, f in enumerate(fields):
        lo, hi = _BOUNDS[i]
        vhi = 7 if i == 5 else hi   # 周字段允许 7 表示周日
        for part in f.split(","):
            part = part.strip()
            if not part:
                raise ValueError(f"第 {i + 1} 段为空")
            rng = part
            if "/" in part:
                rng, step_s = part.split("/", 1)
                if not step_s.isdigit() or int(step_s) <= 0:
                    raise ValueError(f"第 {i + 1} 段步长非法：{part}")
            try:
                if rng == "*":
                    continue
                if "-" in rng:
                    a, b = rng.split("-", 1)
                    a, b = int(a), int(b)
                    if a > b or a < lo or b > vhi:
                        raise ValueError
                else:
                    v = int(rng)
                    if v < lo or v > vhi:
                        raise ValueError
            except ValueError:
                raise ValueError(f"第 {i + 1} 段超出范围[{lo}-{hi}]或非法：{part}")
    return fields


def next_run(expr: str, after: datetime | None = None) -> datetime | None:
    """算 after 之后的下一次触发时间（中国上海时区，aware，秒级）。无匹配返回 None。

    返回带 CN_TZ 时区的 datetime，调用方 .timestamp() 即得到正确的 UTC 毫秒，
    不受服务器本地时区影响。传入的 naive `after` 一律按中国时区解释。
    """
    fields = validate(expr)
    sec_f = fields[0]
    base = after or datetime.now(CN_TZ)
    if base.tzinfo is None:
        base = base.replace(tzinfo=CN_TZ)
    else:
        base = base.astimezone(CN_TZ)
    start = base.replace(microsecond=0) + timedelta(seconds=1)
    minute_dt = start.replace(second=0)
    for _ in range(366 * 24 * 60):
        if _minute_matches(fields, minute_dt):
            for s in range(60):
                cand = minute_dt.replace(second=s)
                if cand >= start and _field_match(sec_f, s, 0, 59):
                    return cand
        minute_dt += timedelta(minutes=1)
        start = minute_dt  # 之后的分钟整分开放，所有秒都可考虑
    return None


_WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


def describe(expr: str) -> str:
    """尽力生成人类可读描述，无法识别的复杂表达式回退为原文。"""
    try:
        sec, minute, hour, dom, month, dow = validate(expr)
    except ValueError:
        return expr
    # 每 N 秒 / 每 N 分
    if sec.startswith("*/") and minute == "*" and hour == "*" and dom == "*" and dow == "*":
        return f"每 {sec[2:]} 秒"
    if sec.isdigit() and minute.startswith("*/") and hour == "*" and dom == "*" and dow == "*":
        return f"每 {minute[2:]} 分钟"
    if sec.isdigit() and minute.isdigit() and hour.startswith("*/") and dom == "*" and dow == "*":
        return f"每 {hour[2:]} 小时"
    if sec.isdigit() and minute.isdigit() and hour.isdigit():
        hm = f"{int(hour):02d}:{int(minute):02d}"
        if int(sec) > 0:
            hm += f":{int(sec):02d}"
        if dom == "*" and dow == "*" and month == "*":
            return f"每天 {hm}"
        if dow != "*" and dom == "*":
            days = "、".join(_WEEK[int(d) % 7] for d in _norm_dow(dow).split(",") if d.isdigit())
            return f"每 {days} {hm}" if days else expr
        if dom != "*" and dow == "*":
            return f"每月 {dom} 号 {hm}"
    return expr
