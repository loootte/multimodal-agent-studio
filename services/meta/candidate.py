"""mas.skill-candidate/1：一次挖掘出的技能候选。字段说明见 docs/mining.md。"""

from __future__ import annotations

import re

SCHEMA = "mas.skill-candidate/1"
STORE_SCHEMA = "mas.skill-candidates/1"
FORMS = ("implicit", "existing_skill")
STATUSES = ("candidate", "insufficient")
KINDS = ("tool_sequence", "agent_sequence")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_ID = re.compile(r"sc_[0-9a-f]{8,64}")
_RUN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")


def _opt(value, kinds) -> bool:
    return value is None or isinstance(value, kinds)


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(record) -> list[str]:
    """返回问题列表；空列表表示通过。读取方忽略不认识的字段。"""
    problems: list[str] = []

    def need(cond, what):
        if not cond:
            problems.append(what)

    if not isinstance(record, dict):
        return ["候选不是对象"]
    need(record.get("schema") == SCHEMA, "schema 不对")
    need(isinstance(record.get("id"), str) and _ID.fullmatch(record["id"] or ""), "id 不合法")
    need(isinstance(record.get("name"), str) and _NAME.fullmatch(record["name"] or ""), "name 不合法")
    need(isinstance(record.get("description"), str) and 1 <= len(record["description"]) <= 2000, "description 不对")
    need(record.get("status") in STATUSES, "status 不对")
    pattern = record.get("pattern")
    need(isinstance(pattern, dict), "缺 pattern")
    if isinstance(pattern, dict):
        need(pattern.get("kind") in KINDS, "pattern.kind 不对")
        need(isinstance(pattern.get("signature"), str) and pattern["signature"], "缺 signature")
        need(_int(pattern.get("n")) and pattern["n"] >= 1, "pattern.n 应为正整数")
        need(isinstance(pattern.get("steps"), list) and all(isinstance(s, str) for s in pattern["steps"]),
             "pattern.steps 应为字符串列表")
    evidence = record.get("evidence")
    need(isinstance(evidence, dict), "缺 evidence")
    if isinstance(evidence, dict):
        need(_int(evidence.get("count")) and evidence["count"] >= 1, "evidence.count 不对")
        need(_int(evidence.get("sessions")) and evidence["sessions"] >= 1, "evidence.sessions 不对")
        rate = evidence.get("success_rate")
        need(isinstance(rate, (int, float)) and 0 <= float(rate) <= 1, "success_rate 应在 0..1")
        runs = evidence.get("run_ids")
        need(isinstance(runs, list) and runs and all(isinstance(r, str) and _RUN.fullmatch(r) for r in runs),
             "evidence.run_ids 不对")
        need(isinstance(evidence.get("typical_scenarios"), list)
             and all(isinstance(s, str) and len(s) <= 200 for s in evidence["typical_scenarios"]),
             "typical_scenarios 不对")
        need(isinstance(evidence.get("outcomes"), dict), "缺 outcomes")
    existing = record.get("existing")
    need(isinstance(existing, dict), "缺 existing")
    if isinstance(existing, dict):
        need(existing.get("form") in FORMS, "existing.form 不对")
        overlaps = existing.get("overlap")
        need(isinstance(overlaps, list), "overlap 应为列表")
        for item in overlaps or []:
            need(isinstance(item, dict) and isinstance(item.get("name"), str)
                 and isinstance(item.get("score"), (int, float)) and isinstance(item.get("reason"), str),
                 "overlap 项结构不对")
        need(isinstance(existing.get("consolidation"), str), "缺 consolidation")
    draft = record.get("steps_draft")
    need(isinstance(draft, list) and all(isinstance(s, str) for s in draft), "steps_draft 应为字符串列表")
    need(isinstance(record.get("mined_at"), str) and _ISO.fullmatch(record["mined_at"] or ""), "mined_at 不对")
    need(isinstance(record.get("model_assisted"), bool), "model_assisted 应为布尔")
    need(_opt(record.get("model_prompt_chars"), int), "model_prompt_chars 应为整数")
    return problems


def validate_store(store) -> list[str]:
    if not isinstance(store, dict):
        return ["候选库不是对象"]
    problems = []
    if store.get("schema") != STORE_SCHEMA:
        problems.append("store schema 不对")
    if not isinstance(store.get("mined_at"), str):
        problems.append("缺 mined_at")
    if not isinstance(store.get("params"), dict):
        problems.append("缺 params")
    candidates = store.get("candidates")
    if not isinstance(candidates, list):
        return problems + ["candidates 应为列表"]
    for item in candidates:
        problems.extend(validate(item))
    ids = [c.get("id") for c in candidates if isinstance(c, dict)]
    if len(ids) != len(set(ids)):
        problems.append("候选 id 重复")
    return problems
