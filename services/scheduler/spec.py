"""14.E（#82）时间表：once / every / daily / weekly / cron（5 段），全部按本机本地时间（不带时区的 datetime）。

只做校验和「下一次什么时候」的计算，不碰存储和运行。
"""

from __future__ import annotations

import datetime as _dt
import re

KINDS = ("once", "every", "daily", "weekly", "cron")
MIN_EVERY = 1
MAX_EVERY = 7 * 24 * 60
_HM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ]([01]\d|2[0-3]):([0-5]\d)")
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))  # 分 时 日 月 周（0=周日，7 也算周日）
CRON_HORIZON_DAYS = 366 * 5


class SpecError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = "invalid_schedule"
        self.message = message


def _hm(value) -> tuple[int, int]:
    if not isinstance(value, str) or not _HM.fullmatch(value.strip()):
        raise SpecError("时间要写成 HH:MM（24 小时制）。")
    hour, minute = value.strip().split(":")
    return int(hour), int(minute)


def _cron_field(text: str, low: int, high: int, dow: bool = False) -> frozenset:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise SpecError("cron 表达式有空字段。")
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) <= 0:
                raise SpecError("cron 的步长要是正整数。")
            step = int(raw_step)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise SpecError("cron 的范围要是数字。")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = int(part)
            end = high if step > 1 else start
        else:
            raise SpecError("cron 只支持数字、*、范围、列表和步长。")
        top = 7 if dow else high
        if start < low or end > top or start > end:
            raise SpecError("cron 字段超出范围。")
        values.update(range(start, end + 1, step))
    if dow and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


def parse_cron(expr) -> tuple:
    if not isinstance(expr, str):
        raise SpecError("cron 要是字符串。")
    parts = expr.split()
    if len(parts) != 5:
        raise SpecError("cron 要 5 段：分 时 日 月 周。")
    fields = tuple(_cron_field(parts[i], *_FIELDS[i], dow=(i == 4)) for i in range(5))
    restricted = (parts[2] != "*", parts[4] != "*")
    return fields + (restricted,)


def validate(raw) -> dict:
    """返回规范化的时间表；不合法抛 SpecError。"""
    if not isinstance(raw, dict):
        raise SpecError("时间表要是对象。")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise SpecError("时间表类型只能是 once / every / daily / weekly / cron。")
    if kind == "once":
        at = raw.get("at")
        if not isinstance(at, str) or not _AT.fullmatch(at.strip()):
            raise SpecError("一次性任务要写 at=YYYY-MM-DDTHH:MM（本地时间）。")
        try:
            moment = _dt.datetime.strptime(at.strip().replace(" ", "T"), "%Y-%m-%dT%H:%M")
        except ValueError as exc:
            raise SpecError("日期不存在。") from exc
        return {"kind": "once", "at": moment.strftime("%Y-%m-%dT%H:%M")}
    if kind == "every":
        minutes = raw.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not MIN_EVERY <= minutes <= MAX_EVERY:
            raise SpecError(f"间隔分钟数要在 {MIN_EVERY}–{MAX_EVERY} 之间。")
        return {"kind": "every", "minutes": minutes}
    if kind == "daily":
        hour, minute = _hm(raw.get("time"))
        return {"kind": "daily", "time": f"{hour:02d}:{minute:02d}"}
    if kind == "weekly":
        hour, minute = _hm(raw.get("time"))
        days = raw.get("days")
        if not isinstance(days, list) or not days or any(isinstance(d, bool) or not isinstance(d, int)
                                                        or not 0 <= d <= 6 for d in days):
            raise SpecError("每周任务要写 days（0=周一 … 6=周日）。")
        return {"kind": "weekly", "days": sorted(set(days)), "time": f"{hour:02d}:{minute:02d}"}
    expr = raw.get("expr")
    parse_cron(expr)
    return {"kind": "cron", "expr": " ".join(expr.split())}


def _cron_next(expr: str, after: _dt.datetime) -> _dt.datetime | None:
    minutes, hours, doms, months, dows, (dom_set, dow_set) = parse_cron(expr)
    start = (after + _dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    day = start.date()
    for _ in range(CRON_HORIZON_DAYS):
        if day.month in months:
            dom_ok = day.day in doms
            dow_ok = ((day.weekday() + 1) % 7) in dows  # cron：0=周日
            if dom_set and dow_set:
                matched = dom_ok or dow_ok
            else:
                matched = dom_ok and dow_ok
            if matched:
                for hour in sorted(hours):
                    for minute in sorted(minutes):
                        moment = _dt.datetime(day.year, day.month, day.day, hour, minute)
                        if moment >= start:
                            return moment
        day += _dt.timedelta(days=1)
    return None


def next_fire(spec: dict, after: _dt.datetime, anchor: _dt.datetime | None = None) -> _dt.datetime | None:
    """严格晚于 after 的下一次触发时间（本地时间，秒为 0）。once 过了就是 None。
    every 以 anchor（创建 / 改动时间）为起点，按整数倍间隔对齐。"""
    kind = spec.get("kind")
    after = after.replace(microsecond=0)
    if kind == "once":
        moment = _dt.datetime.strptime(spec["at"], "%Y-%m-%dT%H:%M")
        return moment if moment > after else None
    if kind == "every":
        step = _dt.timedelta(minutes=int(spec["minutes"]))
        base = (anchor or after).replace(second=0, microsecond=0)
        if base > after:
            return base
        count = int((after - base) / step) + 1
        return base + step * count
    if kind in ("daily", "weekly"):
        hour, minute = _hm(spec["time"])
        days = set(spec.get("days") or range(7)) if kind == "weekly" else set(range(7))
        day = after.date()
        for _ in range(8):
            moment = _dt.datetime(day.year, day.month, day.day, hour, minute)
            if moment > after and day.weekday() in days:
                return moment
            day += _dt.timedelta(days=1)
        return None
    if kind == "cron":
        return _cron_next(spec["expr"], after)
    return None


def describe(spec: dict) -> str:
    """给人看的一句话（只用于面板和工具结果）。"""
    kind = spec.get("kind")
    if kind == "once":
        return "一次：" + spec["at"].replace("T", " ")
    if kind == "every":
        return f"每 {spec['minutes']} 分钟"
    if kind == "daily":
        return "每天 " + spec["time"]
    if kind == "weekly":
        names = "一二三四五六日"
        return "每周" + "、".join(names[d] for d in spec["days"]) + " " + spec["time"]
    return "cron：" + spec.get("expr", "")
