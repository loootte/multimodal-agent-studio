"""mas.trajectory/1：一次运行一条记录。字段说明见 docs/trajectory.md。"""

from __future__ import annotations

import re

SCHEMA = "mas.trajectory/1"
MODES = ("single", "multi")
STATUSES = ("completed", "failed", "cancelled", "unknown")
STEP_KINDS = ("model", "tool")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_RUN = re.compile(r"[A-Za-z0-9_.-]{1,80}")


def _opt(value, kinds) -> bool:
    return value is None or isinstance(value, kinds)


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(record) -> list[str]:
    """返回问题列表；空列表表示通过。读取方忽略不认识的字段，所以这里只查已知字段。"""
    problems: list[str] = []

    def need(cond, what):
        if not cond:
            problems.append(what)

    if not isinstance(record, dict):
        return ["记录不是对象"]
    need(record.get("schema") == SCHEMA, "schema 不对")
    need(isinstance(record.get("run_id"), str) and _RUN.fullmatch(record["run_id"] or ""), "run_id 不合法")
    parent = record.get("parent_run_id")
    need(parent is None or (isinstance(parent, str) and _RUN.fullmatch(parent)), "parent_run_id 不合法")
    need(isinstance(record.get("trace_id"), str) and record["trace_id"], "缺 trace_id")
    for key in ("session_id", "turn_id", "agent", "role", "model"):
        need(isinstance(record.get(key), str), f"{key} 应为字符串")
    need(record.get("mode") in MODES, "mode 只能是 single 或 multi")
    need(_int(record.get("depth")) and record["depth"] >= 0, "depth 应为非负整数")
    need(parent is None or record.get("depth", 0) > 0, "子运行的 depth 应大于 0")
    for key in ("started_at", "ended_at"):
        value = record.get(key)
        need(value is None or (isinstance(value, str) and _ISO.fullmatch(value)), f"{key} 应为 UTC ISO 时间")
    need(isinstance(record.get("started_at"), str), "缺 started_at")
    need(_opt(record.get("ms"), int), "ms 应为整数")
    text = record.get("input")
    if text is not None:
        need(isinstance(text, dict) and isinstance(text.get("summary"), str) and _int(text.get("chars")), "input 结构不对")
        need(isinstance(text, dict) and _opt(text.get("local_handle"), str), "input.local_handle 应为字符串")
        for item in (text or {}).get("attachments") or []:
            need(isinstance(item, dict) and set(item) <= {"handle", "kind", "media_type", "size"}, "附件只能留句柄、种类、类型和大小")
    out = record.get("output")
    need(isinstance(out, dict) and isinstance(out.get("summary"), str) and _int(out.get("chars"))
         and isinstance(out.get("artifacts"), list), "output 结构不对")
    steps = record.get("steps")
    need(isinstance(steps, list), "steps 应为列表")
    for step in steps or []:
        if not isinstance(step, dict):
            problems.append("step 不是对象")
            continue
        need(step.get("kind") in STEP_KINDS, "step.kind 只能是 model 或 tool")
        need(isinstance(step.get("name"), str), "step.name 应为字符串")
        need(_opt(step.get("ok"), bool), "step.ok 应为布尔")
        need(_opt(step.get("ms"), int), "step.ms 应为整数")
        need(_opt(step.get("args"), dict), "step.args 应为对象")
        need(_opt(step.get("tokens"), dict), "step.tokens 应为对象")
    for key in ("tools", "skills"):
        need(isinstance(record.get(key), list) and all(isinstance(x, str) for x in record[key]), f"{key} 应为字符串列表")
    delegations = record.get("delegations")
    need(isinstance(delegations, list) and all(isinstance(d, dict) and isinstance(d.get("run_id"), str)
                                                for d in delegations), "delegations 结构不对")
    budget = record.get("budget")
    need(isinstance(budget, dict) and _int(budget.get("steps")) and isinstance(budget.get("tokens"), dict), "budget 结构不对")
    outcome = record.get("outcome")
    need(isinstance(outcome, dict) and outcome.get("status") in STATUSES, "outcome.status 不对")
    need(isinstance(outcome, dict) and _opt(outcome.get("error_code"), str), "outcome.error_code 应为字符串")
    feedback = record.get("feedback")
    need(isinstance(feedback, dict) and all(isinstance(feedback.get(k), bool) for k in ("retried", "reseeded", "stopped")),
         "feedback 结构不对")
    return problems


def build_tree(records: list[dict]) -> list[dict]:
    """按 parent_run_id 还原委托树：返回根节点列表，每个节点 {"run": record, "children": [...]}。"""
    nodes = {r["run_id"]: {"run": r, "children": []} for r in records}
    roots = []
    for record in records:
        parent = nodes.get(record.get("parent_run_id") or "")
        (parent["children"] if parent else roots).append(nodes[record["run_id"]])
    return roots
