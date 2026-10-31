"""14.E（#82）调度设置（scheduler.json，经 store.ContextSettingsStore 存取）和任务预算的校验规则。

默认最严：调度总开关关着——不触发任何任务，四个 schedule_* 工具不给模型，请求和以前逐字节一致。
"""

from __future__ import annotations

TOOLS = ("schedule_create", "schedule_list", "schedule_update", "schedule_delete")
WRITE_TOOLS = ("schedule_create", "schedule_update", "schedule_delete")  # 每次调用都确认，不记进「本轮已确认」

DEFAULTS = {
    "enabled": False,
    # 到点后等用户点「开始本次」、以及运行中等确认的最长时间；超时按 confirm_timeout 停下。
    "confirm_timeout_min": 30,
    "max_jobs": 20,
    # 明文存储（默认存储模式）下收不收出图任务（提示词是本地内容）。默认不收：按 12.F 只加密落盘。
    "allow_plaintext_image": False,
}

# 每个任务自己的上限（面板里可改）。默认偏严；超出就停下并写明原因。
BUDGET_DEFAULTS = {"steps": 12, "time_min": 10, "tokens": 50000, "daily_runs": 4}
BUDGET_LIMITS = {"steps": (1, 24), "time_min": (1, 120), "tokens": (1000, 200000), "daily_runs": (1, 48)}
MAX_IMAGES = 4


def _int_in(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != int(value) or not low <= value <= high:
        return None
    return int(value)


def settings(raw) -> tuple[dict, list[str]]:
    values = dict(DEFAULTS)
    warnings: list[str] = []
    if raw is None:
        return values, warnings
    if not isinstance(raw, dict):
        return values, ["*"]
    checks = {
        "enabled": lambda v: v if isinstance(v, bool) else None,
        "confirm_timeout_min": lambda v: _int_in(v, 1, 1440),
        "max_jobs": lambda v: _int_in(v, 1, 100),
        "allow_plaintext_image": lambda v: v if isinstance(v, bool) else None,
    }
    for key, value in raw.items():
        check = checks.get(key)
        if check is None:
            warnings.append(str(key)[:40])
            continue
        checked = None if value is None else check(value)
        if checked is None:
            warnings.append(key)
            continue
        values[key] = checked
    return values, warnings


def budget(raw, base=None) -> tuple[dict, list[str]]:
    """任务预算：返回 (完整取值, 不合法的字段)。不合法的项保持 base（或默认值）。"""
    values = dict(base or BUDGET_DEFAULTS)
    bad: list[str] = []
    if raw is None:
        return values, bad
    if not isinstance(raw, dict):
        return values, ["budget"]
    for key, value in raw.items():
        if key not in BUDGET_LIMITS:
            bad.append(str(key)[:40])
            continue
        checked = _int_in(value, *BUDGET_LIMITS[key])
        if checked is None:
            bad.append(key)
            continue
        values[key] = checked
    return values, bad
