"""14.C（#80）规划：todo / goal 宿主工具、plan 模式（只读）和目标续跑。

这些工具由运行时自己执行（不是工具包、不出本进程）：
- todo_write：整份替换本轮任务清单（条目 + 状态）。
- goal_set / goal_done：设定 / 结束一个跨步、可跨轮的目标。
- plan_submit：只在 plan 模式提供，交出计划后这一轮结束，等用户在界面上确认才执行。

不变量：
- todo / goal / 计划文字对模型可见，写入前过本地正文检查（egress.inspect，含 local_fingerprint）；命中就拒绝、
  记 security.egress_denied（只有原因码），什么都不存。
- 清单里的数量不算数：出图张数只认 plan_images（运行时不解析 todo 文字）。
- plan 模式只给只读工具：见 read_only()。被拦下的调用不执行、不弹确认卡，结果是 plan_mode_readonly。
- 目标续跑默认关闭；打开时另有轮数、时间、token 三道预算，碰到就停并写明原因；从不自动确认任何调用。
- 不传 Planner 时运行时行为、请求和以前逐字节一致。
"""

from __future__ import annotations

import time
import uuid

from packages.contracts.events import EVENT_PLAN_PROPOSED, EVENT_PLANNING_GOAL, EVENT_PLANNING_TODO
from services import telemetry

TODO_WRITE = "todo_write"
GOAL_SET = "goal_set"
GOAL_DONE = "goal_done"
PLAN_SUBMIT = "plan_submit"
HOST_TOOLS = (TODO_WRITE, GOAL_SET, GOAL_DONE, PLAN_SUBMIT)

EVENT_TODO = EVENT_PLANNING_TODO
EVENT_GOAL = EVENT_PLANNING_GOAL
EVENT_PLAN = EVENT_PLAN_PROPOSED

STATUSES = ("pending", "in_progress", "completed", "cancelled")
GOAL_END = ("achieved", "blocked")
MAX_ITEMS = 20
ITEM_CHARS = 200
GOAL_CHARS = 400
CHANNEL = "planning"

# plan 模式额外拒绝的工具：清单上不算写操作，但会改宿主状态或把事交给别人做。
PLAN_DENY = frozenset({"stage_prompt", "delegate", "accept_local_content", "forget_local_content"})
# 只读工具不能要的权限。read_local_content 允许：只在本机按句柄读，正文不进模型。
PLAN_DENY_PERMISSIONS = frozenset({"network", "write_files", "ui", "send_local_to_model"})
# 运行时自己执行的只读工具。
RUNTIME_READ_ONLY = frozenset({"session_query"})

DEFAULTS = {
    # 单 Agent 模式也提供 todo / goal。默认关：单 Agent、不开 plan 模式时行为不变（#80 不变量）。
    "single_agent_tools": False,
    # 多智能体编排者先用 todo_write 拆分文字 / 研究任务再委托。
    "orchestrator_todo": True,
    # 目标没完成、模型想收尾时自动续跑。默认关（最严）：要用户打开。
    "goal_auto_continue": False,
    "goal_max_rounds": 3,
    "goal_time_sec": 600,
    "goal_tokens": 200_000,
}

STOP_TEXT = {
    "goal_rounds": "目标续跑轮数用完，已经停下。",
    "goal_time": "目标续跑时间预算用完，已经停下。",
    "goal_tokens": "目标续跑 token 预算用完，已经停下。",
    "budget_exceeded": "这一轮的运行预算用完，目标没完成，已经停下。",
    "cancelled": "已停止，目标没完成。",
    "auto_continue_off": "目标还没完成；自动续跑没打开，等你下一句再继续。",
}


def _int_in(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != int(value) or not low <= value <= high:
        return None
    return int(value)


def settings(raw) -> tuple[dict, list[str]]:
    """校验规划设置：返回 (完整取值, 警告字段)。缺的用默认值，非法的回退默认值并警告，不认识的键忽略并警告。"""
    values = dict(DEFAULTS)
    warnings: list[str] = []
    if raw is None:
        return values, warnings
    if not isinstance(raw, dict):
        return values, ["*"]
    checks = {
        "single_agent_tools": lambda v: v if isinstance(v, bool) else None,
        "orchestrator_todo": lambda v: v if isinstance(v, bool) else None,
        "goal_auto_continue": lambda v: v if isinstance(v, bool) else None,
        "goal_max_rounds": lambda v: _int_in(v, 1, 20),
        "goal_time_sec": lambda v: _int_in(v, 30, 3600),
        "goal_tokens": lambda v: _int_in(v, 1000, 2_000_000),
    }
    for key, value in raw.items():
        check = checks.get(key)
        if check is None:
            warnings.append(str(key)[:40])
            continue
        if value is None:
            warnings.append(key)
            continue
        checked = check(value)
        if checked is None:
            warnings.append(key)
            continue
        values[key] = checked
    return values, warnings


def read_only(name, arguments=None) -> bool:
    """plan 模式能用的工具：运行时只读工具，或者清单 mutating=false、不用确认、不要联网 / 写文件 / 界面 /
    发本地内容给模型、不在 PLAN_DENY 里的内置或已安装包。MCP 远端工具一律不算只读。找不到清单的不算。"""
    if name in RUNTIME_READ_ONLY:
        return True
    if not isinstance(name, str) or name in PLAN_DENY or name in HOST_TOOLS:
        return False
    try:
        from services.tools import packs

        pack = packs.find_tool(name)
        if pack is None or packs.is_mcp(pack) or packs.sensitive(pack):
            return False
        manifest = pack.manifest
        if manifest.get("mutating") is not False or manifest.get("confirm") != "never":
            return False
        if packs.needs_confirm(pack, arguments if isinstance(arguments, dict) else {}):
            return False
        wanted = set(pack.declared()) | set(packs.needed_permissions(pack))
        return not (wanted & PLAN_DENY_PERMISSIONS) and "__unlisted__" not in wanted
    except Exception:  # noqa: BLE001 判断不了就不放行
        return False


def tool_name(item) -> str | None:
    if not isinstance(item, dict):
        return None
    function = item.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return item.get("name") if isinstance(item.get("name"), str) else None


def _contract(name, description, properties, required) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": properties, "required": required},
        },
    }


TODO_CONTRACT = _contract(
    TODO_WRITE,
    "维护这一轮的任务清单：每次传完整清单（整份替换）。多步任务先列条目，做到哪一项就把它标 in_progress，"
    "做完标 completed。只写要做的事，不要抄本地内容正文（本地内容只用句柄）。清单里的数量不算数："
    "出图张数只由 plan_images 决定。",
    {"items": {"type": "array", "minItems": 1, "maxItems": MAX_ITEMS, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"id": {"type": "string", "maxLength": 40},
                       "text": {"type": "string", "minLength": 1, "maxLength": ITEM_CHARS},
                       "status": {"type": "string", "enum": list(STATUSES)}},
        "required": ["text", "status"]}}},
    ["items"],
)
GOAL_SET_CONTRACT = _contract(
    GOAL_SET,
    "设定一个要跨多步完成的目标（同一时间只有一个）。完成或做不到时调用 goal_done。不要写本地内容正文。",
    {"objective": {"type": "string", "minLength": 1, "maxLength": GOAL_CHARS},
     "done_when": {"type": "string", "maxLength": ITEM_CHARS}},
    ["objective"],
)
GOAL_DONE_CONTRACT = _contract(
    GOAL_DONE,
    "结束当前目标：achieved 表示完成，blocked 表示做不到（在 note 里说明原因）。",
    {"status": {"type": "string", "enum": list(GOAL_END)},
     "note": {"type": "string", "maxLength": ITEM_CHARS}},
    ["status"],
)
PLAN_CONTRACT = _contract(
    PLAN_SUBMIT,
    "plan 模式下交出执行计划：summary 一段话，steps 是要做的步骤。交出以后这一轮结束，用户确认后才会执行。",
    {"summary": {"type": "string", "minLength": 1, "maxLength": GOAL_CHARS},
     "steps": {"type": "array", "minItems": 1, "maxItems": MAX_ITEMS,
               "items": {"type": "string", "minLength": 1, "maxLength": ITEM_CHARS}}},
    ["summary", "steps"],
)

PLAN_NOTE = (
    "（运行时）现在是 plan 模式：只能用只读工具了解情况，不能出图、写文件、联网或委托。"
    "想好以后调用 plan_submit 交出计划，用户确认后才会执行。"
)
PLAN_NUDGE = "（运行时）plan 模式需要用 plan_submit 交出计划（summary + steps），用户确认后才执行。"


def _error(name, code, message) -> dict:
    return {"ok": False, "tool": name, "executed": False, "error": {"code": code, "message": message}}


def readonly_denied(name) -> dict:
    return _error(name, "plan_mode_readonly", "plan 模式只能用只读工具；这一步没有执行。先用 plan_submit 交计划。")


def _counts(items) -> dict:
    out = {status: 0 for status in STATUSES}
    for item in items:
        out[item["status"]] = out.get(item["status"], 0) + 1
    return out


class Planner:
    """一次运行（以及网页重进循环）的规划状态。mode 是 execute 或 plan。"""

    def __init__(self, *, mode="execute", values=None, todos=None, goal=None, local_bodies=None, seed_note=None):
        self.mode = "plan" if mode == "plan" else "execute"
        self.values, _ = settings(values)
        self.todos = [dict(item) for item in (todos or []) if isinstance(item, dict)]
        self.goal = dict(goal) if isinstance(goal, dict) and goal.get("status") == "active" else None
        self.local_bodies = local_bodies
        self.seed_note = seed_note
        self.plan = None
        self.rounds = 0
        self.tokens = 0
        self.started = time.monotonic()
        self.nudged = False
        self.pending: list[dict] = []  # 运行时在下一次 yield 时发出的事件

    # ---------- 契约 ----------

    def contracts(self) -> list[dict]:
        tools = [TODO_CONTRACT, GOAL_SET_CONTRACT, GOAL_DONE_CONTRACT]
        if self.mode == "plan":
            tools.append(PLAN_CONTRACT)
        return tools

    def handles(self, name) -> bool:
        return name in (TODO_WRITE, GOAL_SET, GOAL_DONE) or (name == PLAN_SUBMIT and self.mode == "plan")

    def allows(self, name, arguments=None) -> bool:
        """plan 模式下能不能用这个工具（宿主工具另算）。execute 模式什么都不拦。"""
        if self.mode != "plan" or self.handles(name):
            return True
        return read_only(name, arguments)

    def filter_tools(self, tools):
        if tools is None:
            return None
        if self.mode != "plan":
            return list(tools)
        return [item for item in tools if self.allows(tool_name(item))]

    def system_note(self) -> str:
        parts = []
        if self.mode == "plan":
            parts.append(PLAN_NOTE)
        if self.seed_note:
            parts.append(self.seed_note)
        if self.goal:
            line = self._clean(self.goal.get("objective"))
            if line:
                parts.append(f"（运行时）当前目标：{line}。完成后调用 goal_done。")
        if self.todos and any(item["status"] in ("pending", "in_progress") for item in self.todos):
            listing = "；".join(f"[{item['status']}] {item['text']}" for item in self.todos if self._clean(item["text"]))
            if listing:
                parts.append(f"（运行时）当前任务清单：{listing}。进展变了就用 todo_write 更新。")
        return " ".join(parts)

    # ---------- 检查 ----------

    def _hit(self, texts) -> str | None:
        if not self.local_bodies:
            return None
        from services.gateway import egress

        return egress.inspect({"messages": [t for t in texts if t]}, self.local_bodies, exempt=())

    def _clean(self, text) -> str:
        """重新注入前再查一次（本地内容可能是后来才留下的）；命中就不注入。"""
        if not isinstance(text, str) or not text.strip():
            return ""
        return "" if self._hit([text]) else text.strip()

    # ---------- 执行 ----------

    def call(self, name, arguments) -> tuple[dict, dict | None]:
        """执行宿主规划工具，返回 (给模型的结果, 给界面的事件或 None)。"""
        arguments = arguments if isinstance(arguments, dict) else {}
        started = time.monotonic()
        if name == TODO_WRITE:
            result, event = self._todo(arguments)
        elif name == GOAL_SET:
            result, event = self._goal_set(arguments)
        elif name == GOAL_DONE:
            result, event = self._goal_done(arguments)
        elif name == PLAN_SUBMIT and self.mode == "plan":
            result, event = self._plan(arguments)
        else:
            result, event = _error(name, "unknown_tool", "没有这个规划工具。"), None
        code = None if result.get("ok") else (result.get("error") or {}).get("code")
        telemetry.record("planning.tool", name=name, ok=bool(result.get("ok")), error_code=code,
                         ms=int((time.monotonic() - started) * 1000))
        return result, event

    def _blocked(self, name, reason) -> dict:
        from services.gateway import egress

        egress.security_event(reason, CHANNEL)
        return _error(name, reason, "文字里有本机内容正文，没有记下。本地内容只用句柄指代。")

    def _todo(self, arguments):
        raw = arguments.get("items")
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ITEMS:
            return _error(TODO_WRITE, "invalid_arguments", f"items 要有 1 到 {MAX_ITEMS} 项。"), None
        items = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                return _error(TODO_WRITE, "invalid_arguments", "每一项要有 text 和 status。"), None
            text = entry.get("text")
            status = entry.get("status")
            if not isinstance(text, str) or not text.strip() or len(text) > ITEM_CHARS or status not in STATUSES:
                return _error(TODO_WRITE, "invalid_arguments",
                              f"text 1 到 {ITEM_CHARS} 字，status 只能是 {'/'.join(STATUSES)}。"), None
            ident = entry.get("id")
            ident = ident[:40] if isinstance(ident, str) and ident.strip() else f"t{index + 1}"
            items.append({"id": ident, "text": text.strip(), "status": status})
        reason = self._hit([item["text"] for item in items] + [item["id"] for item in items])
        if reason:
            return self._blocked(TODO_WRITE, reason), None
        self.todos = items
        counts = _counts(items)
        result = {"ok": True, "tool": TODO_WRITE, "executed": True, "items": len(items), "counts": counts}
        return result, self.todo_event()

    def todo_event(self) -> dict:
        return {"type": EVENT_TODO, "items": [dict(item) for item in self.todos], "counts": _counts(self.todos)}

    def _goal_set(self, arguments):
        objective = arguments.get("objective")
        done_when = arguments.get("done_when")
        if not isinstance(objective, str) or not objective.strip() or len(objective) > GOAL_CHARS:
            return _error(GOAL_SET, "invalid_arguments", f"objective 1 到 {GOAL_CHARS} 字。"), None
        if done_when is not None and (not isinstance(done_when, str) or len(done_when) > ITEM_CHARS):
            return _error(GOAL_SET, "invalid_arguments", f"done_when 最多 {ITEM_CHARS} 字。"), None
        reason = self._hit([objective, done_when or ""])
        if reason:
            return self._blocked(GOAL_SET, reason), None
        self.goal = {"id": "g_" + uuid.uuid4().hex[:12], "objective": objective.strip(),
                     "done_when": (done_when or "").strip(), "status": "active"}
        self.rounds = 0
        return ({"ok": True, "tool": GOAL_SET, "executed": True, "status": "active"},
                self.goal_event())

    def _goal_done(self, arguments):
        status = arguments.get("status")
        note = arguments.get("note")
        if status not in GOAL_END:
            return _error(GOAL_DONE, "invalid_arguments", "status 只能是 achieved 或 blocked。"), None
        if note is not None and (not isinstance(note, str) or len(note) > ITEM_CHARS):
            return _error(GOAL_DONE, "invalid_arguments", f"note 最多 {ITEM_CHARS} 字。"), None
        if not self.goal:
            return _error(GOAL_DONE, "no_goal", "现在没有进行中的目标。"), None
        reason = self._hit([note or ""])
        if reason:
            return self._blocked(GOAL_DONE, reason), None
        self.goal = dict(self.goal, status=status, note=(note or "").strip())
        event = self.goal_event()
        self.goal = None
        return {"ok": True, "tool": GOAL_DONE, "executed": True, "status": status}, event

    def goal_event(self, reason=None) -> dict:
        goal = self.goal or {}
        event = {"type": EVENT_GOAL, "id": goal.get("id"), "objective": goal.get("objective") or "",
                 "done_when": goal.get("done_when") or "", "status": goal.get("status") or "active",
                 "rounds": self.rounds}
        if goal.get("note"):
            event["note"] = goal["note"]
        if reason:
            event["reason"] = reason
            event["message"] = STOP_TEXT.get(reason, reason)
        return event

    def _plan(self, arguments):
        summary = arguments.get("summary")
        steps = arguments.get("steps")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > GOAL_CHARS:
            return _error(PLAN_SUBMIT, "invalid_arguments", f"summary 1 到 {GOAL_CHARS} 字。"), None
        if (not isinstance(steps, list) or not 1 <= len(steps) <= MAX_ITEMS
                or any(not isinstance(s, str) or not s.strip() or len(s) > ITEM_CHARS for s in steps)):
            return _error(PLAN_SUBMIT, "invalid_arguments", f"steps 要有 1 到 {MAX_ITEMS} 项，每项 1 到 {ITEM_CHARS} 字。"), None
        reason = self._hit([summary] + steps)
        if reason:
            return self._blocked(PLAN_SUBMIT, reason), None
        self.plan = {"plan_id": "p_" + uuid.uuid4().hex[:12], "summary": summary.strip(),
                     "steps": [s.strip() for s in steps]}
        return ({"ok": True, "tool": PLAN_SUBMIT, "executed": True, "status": "waiting_user"},
                {"type": EVENT_PLAN, **self.plan})

    def plan_text(self) -> str:
        if not self.plan:
            return ""
        lines = [f"计划：{self.plan['summary']}", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(self.plan["steps"], 1)]
        lines += ["", "确认后才会执行。"]
        return "\n".join(lines)

    # ---------- 推进 ----------

    def advance(self, ok: bool) -> dict | None:
        """多智能体：一次委托做完，把清单里第一项没完成的标成 completed（失败不动），返回新的清单事件。"""
        if not ok or not self.todos:
            return None
        for item in self.todos:
            if item["status"] in ("in_progress", "pending"):
                item["status"] = "completed"
                break
        else:
            return None
        for item in self.todos:
            if item["status"] == "pending":
                item["status"] = "in_progress"
                break
        return self.todo_event()

    def note_tokens(self, count: int) -> None:
        self.tokens += max(0, int(count or 0))

    def resume(self, spoken: str):
        """模型没调工具想收尾时：plan 模式提醒一次交计划；目标没完成且开了续跑，就在预算内续一轮。
        返回续写指令或 None；停下时把带原因的目标事件放进 pending。"""
        if self.mode == "plan":
            if self.plan is None and not self.nudged:
                self.nudged = True
                return PLAN_NUDGE
            return None
        if not self.goal:
            return None
        if not self.values["goal_auto_continue"]:
            self.pending.append(self.goal_event("auto_continue_off"))
            return None
        reason = None
        if self.rounds >= self.values["goal_max_rounds"]:
            reason = "goal_rounds"
        elif time.monotonic() - self.started >= self.values["goal_time_sec"]:
            reason = "goal_time"
        elif self.tokens >= self.values["goal_tokens"]:
            reason = "goal_tokens"
        if reason:
            self.goal = dict(self.goal, status="stopped")
            self.pending.append(self.goal_event(reason))
            telemetry.record("planning.goal_stopped", error_code=reason, rounds=self.rounds)
            self.goal = None
            return None
        self.rounds += 1
        self.pending.append(self.goal_event())
        objective = self._clean(self.goal.get("objective")) or "上面设定的目标"
        return (f"（运行时）目标还没完成：{objective}。接着做下一步；完成后调用 goal_done(status=achieved)，"
                "做不到就调用 goal_done(status=blocked) 说明原因。")

    def stop(self, reason: str) -> dict | None:
        """运行被预算 / 取消截断时：目标没完成就记下停止原因。"""
        if not self.goal:
            return None
        self.goal = dict(self.goal, status="stopped")
        event = self.goal_event(reason)
        self.goal = None
        return event

    def drain(self) -> list[dict]:
        out, self.pending = self.pending, []
        return out


def carried(messages) -> tuple[list, dict | None]:
    """从会话里之前的助手消息事件取回最近的清单（还有没完成的项）和进行中的目标，给下一轮接着用。"""
    todos, goal = [], None
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for event in message.get("events") or []:
            if not isinstance(event, dict):
                continue
            if event.get("type") == EVENT_TODO and isinstance(event.get("items"), list):
                todos = [item for item in event["items"] if isinstance(item, dict)]
            elif event.get("type") == EVENT_GOAL:
                goal = event if event.get("status") == "active" or event.get("reason") == "auto_continue_off" else None
    if todos and not any(item.get("status") in ("pending", "in_progress") for item in todos):
        todos = []
    if goal is not None:
        goal = {"id": goal.get("id"), "objective": goal.get("objective"), "done_when": goal.get("done_when"),
                "status": "active"}
    return todos, goal


def approved_plan(messages, message_id) -> dict | None:
    """用户在界面上确认的计划：只认会话里那条助手消息上真的有 plan.proposed 事件。"""
    for message in messages or []:
        if not isinstance(message, dict) or message.get("message_id") != message_id or message.get("role") != "assistant":
            continue
        for event in reversed(message.get("events") or []):
            if isinstance(event, dict) and event.get("type") == EVENT_PLAN and isinstance(event.get("steps"), list):
                return event
    return None


def seed_from_plan(plan: dict) -> tuple[list, str]:
    steps = [s for s in plan.get("steps") or [] if isinstance(s, str) and s.strip()][:MAX_ITEMS]
    todos = [{"id": f"t{i}", "text": s[:ITEM_CHARS], "status": "in_progress" if i == 1 else "pending"}
             for i, s in enumerate(steps, 1)]
    note = "（运行时）用户已确认上面的计划，按清单逐项执行，做完一项用 todo_write 标 completed。"
    return todos, note
