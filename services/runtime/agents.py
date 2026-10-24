"""多智能体：Agent 清单（mas.agent/1）、发现、安装、导出，以及编排者的 delegate 委托器。

见 docs/adr/0005-multi-agent.md 和 docs/agents.md。
子 Agent 也是 loop.run 的一次运行；这里只决定它能用哪些工具、拿多少预算、和谁并行。
Agent 之间只传任务说明、句柄和摘要，不传本地正文。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

from services import telemetry
from services.runtime import trace
from services.runtime.budget import Budget

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AGENTS = os.path.join(ROOT, "agents")
BUILTIN = os.path.join(AGENTS, "builtin")
SCHEMA = "mas.agent/1"
MANIFEST = "agent.json"
ORCHESTRATOR = "orchestrator"
DELEGATE = "delegate"
ROLES = ("orchestrator", "planner", "image", "writer", "reviewer", "worker")
# 只有面对用户的编排者能弹本地内容对话框。
ORCHESTRATOR_ONLY = ("accept_local_content",)
MAX_DEPTH = 2
MAX_TASKS = 4
MAX_PARALLEL = 2
SUMMARY_CHARS = 1500
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_HANDLE = re.compile(r"^[A-Za-z0-9_\-:.]{1,80}$")
_FIELDS = {"schema", "name", "version", "description", "role", "instructions", "model", "tools", "skills", "budget"}
_BUDGET = {"steps": (1, 64), "time_sec": (5, 1800), "tokens": (1000, 400000), "images": (0, 64),
           "tree_steps": (1, 256), "delegations": (1, 32)}
_ORCHESTRATOR_BUDGET = ("tree_steps", "delegations")


class AgentError(Exception):
    def __init__(self, code: str, message: str, problems: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.problems = problems or []

    def payload(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.problems:
            out["problems"] = self.problems
        return out


def installed_dir() -> str:
    return os.environ.get("MAS_AGENTS_DIR") or os.path.join(AGENTS, "installed")


# ---------- 校验 ----------

def validate_manifest(data, *, builtin: bool = False, known_tools=None) -> list[str]:
    """返回问题列表；空列表表示合格。known_tools 给了就检查引用的工具都已注册。"""
    if not isinstance(data, dict):
        return ["清单必须是 JSON 对象。"]
    problems = []
    if data.get("schema") != SCHEMA:
        problems.append(f"schema 必须是 {SCHEMA}。")
    extra = sorted(set(data) - _FIELDS)
    if extra:
        problems.append(f"不认识的字段：{', '.join(extra)}。")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        problems.append("name 必须是小写字母开头的 2-48 位 [a-z0-9_]。")
    if not isinstance(data.get("version"), str) or not _VERSION.match(data.get("version") or ""):
        problems.append("version 必须是 主.次.修 三段数字。")
    if not isinstance(data.get("description"), str) or not data.get("description", "").strip():
        problems.append("description 必填。")
    role = data.get("role")
    if role not in ROLES:
        problems.append(f"role 只能是 {'、'.join(ROLES)}。")
    elif role == "orchestrator" and not builtin:
        problems.append("只有内置 Agent 可以是 orchestrator。")
    instructions = data.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 4000:
        problems.append("instructions 必填，最多 4000 字。")
    model = data.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 120):
        problems.append("model 要么不写（跟会话模型），要么是模型名字符串。")
    tools = data.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools) or len(set(tools)) != len(tools or []):
        problems.append("tools 必须是不重复的工具名列表。")
    else:
        if role != "orchestrator":
            for tool in ORCHESTRATOR_ONLY:
                if tool in tools:
                    problems.append(f"只有编排者能用 {tool}（本地内容对话框只能由面对用户的那一层发起）。")
        if DELEGATE in tools and not builtin:
            problems.append("第三方 Agent 不能再委托（delegate 只给内置 Agent）。")
        if known_tools is not None:
            unknown = sorted(set(tools) - set(known_tools) - {DELEGATE})
            if unknown:
                problems.append(f"引用了没有注册的工具：{'、'.join(unknown)}。")
    skills = data.get("skills", [])
    if not isinstance(skills, list) or not all(isinstance(s, str) and s for s in skills):
        problems.append("skills 必须是技能名列表。")
    budget = data.get("budget", {})
    if not isinstance(budget, dict):
        problems.append("budget 必须是对象。")
    else:
        for key, value in budget.items():
            if key not in _BUDGET:
                problems.append(f"budget 不认识 {key}。")
                continue
            low, high = _BUDGET[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
                problems.append(f"budget.{key} 要在 {low}-{high} 之间。")
            elif key in _ORCHESTRATOR_BUDGET and role != "orchestrator":
                problems.append(f"budget.{key} 只给编排者。")
    return problems


# ---------- 发现 ----------

class Agent:
    def __init__(self, manifest: dict, folder: str, builtin: bool):
        self.manifest = manifest
        self.folder = folder
        self.builtin = builtin

    @property
    def name(self) -> str:
        return self.manifest["name"]

    @property
    def role(self) -> str:
        return self.manifest["role"]

    def tools(self) -> list[str]:
        return list(self.manifest.get("tools") or [])

    def budget_value(self, key: str, default):
        return (self.manifest.get("budget") or {}).get(key, default)

    def model(self, session_model: str) -> str:
        override = os.environ.get("MAS_AGENT_MODEL_" + self.name.upper())
        return (override or self.manifest.get("model") or session_model).strip()


def _read(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def registered_tools() -> list[str]:
    from services.runtime.tools import _ensure

    _ensure()
    from tools import packs

    return [pack.name for pack in packs.discover()]


def _scan(folder: str, builtin: bool, known) -> list[Agent]:
    found = []
    if not os.path.isdir(folder):
        return found
    for entry in sorted(os.listdir(folder)):
        path = os.path.join(folder, entry, MANIFEST)
        if entry.startswith((".", "_")) or not os.path.isfile(path):
            continue
        try:
            data = _read(path)
        except ValueError as exc:
            raise AgentError("invalid_manifest", f"{path} 不是合法 JSON：{exc}") from exc
        problems = validate_manifest(data, builtin=builtin, known_tools=known)
        if problems:
            raise AgentError("invalid_manifest", f"{entry}：{'；'.join(problems)}", problems)
        if data["name"] != entry:
            raise AgentError("invalid_manifest", f"{entry}：目录名要和 name 一致。")
        found.append(Agent(data, os.path.join(folder, entry), builtin))
    return found


def discover() -> list[Agent]:
    known = registered_tools()
    agents = _scan(BUILTIN, True, known) + _scan(installed_dir(), False, known)
    seen = set()
    for agent in agents:
        if agent.name in seen:
            raise AgentError("duplicate_agent", f"Agent {agent.name} 重名了。")
        seen.add(agent.name)
    return agents


def find(name: str) -> Agent | None:
    for agent in discover():
        if agent.name == name:
            return agent
    return None


def summary(agent: Agent) -> dict:
    m = agent.manifest
    return {
        "name": agent.name, "version": m["version"], "description": m["description"], "role": m["role"],
        "model": m.get("model") or "", "tools": agent.tools(), "skills": list(m.get("skills") or []),
        "budget": dict(m.get("budget") or {}), "builtin": agent.builtin,
    }


# ---------- 安装和导出（和工具包同一套：目录或 zip，导出不带本机记录） ----------

def _unpack(source: str) -> tuple[str, str | None]:
    if os.path.isdir(source):
        return source, None
    if os.path.isfile(source) and source.lower().endswith(".zip"):
        temp = tempfile.mkdtemp(prefix="agentpack-")
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                target = os.path.normpath(os.path.join(temp, info.filename))
                if not target.startswith(temp + os.sep) and target != temp:
                    shutil.rmtree(temp, ignore_errors=True)
                    raise AgentError("invalid_package", "压缩包里有越界路径。")
            archive.extractall(temp)
        if os.path.isfile(os.path.join(temp, MANIFEST)):
            return temp, temp
        inner = [d for d in os.listdir(temp) if os.path.isfile(os.path.join(temp, d, MANIFEST))]
        if len(inner) == 1:
            return os.path.join(temp, inner[0]), temp
        shutil.rmtree(temp, ignore_errors=True)
        raise AgentError("invalid_package", "压缩包里找不到 agent.json。")
    raise AgentError("invalid_package", "要给一个 Agent 目录或 .zip 文件。")


def inspect(source: str) -> dict:
    folder, temp = _unpack(source)
    try:
        path = os.path.join(folder, MANIFEST)
        if not os.path.isfile(path):
            raise AgentError("invalid_package", "目录里没有 agent.json。")
        try:
            data = _read(path)
        except ValueError as exc:
            raise AgentError("invalid_manifest", f"agent.json 不是合法 JSON：{exc}") from exc
        problems = validate_manifest(data, known_tools=registered_tools())
        if not problems:
            existing = find(data["name"])
            if existing is not None and existing.builtin:
                problems.append("和内置 Agent 重名。")
        return {"name": data.get("name"), "version": data.get("version"), "description": data.get("description"),
                "role": data.get("role"), "tools": data.get("tools") or [], "problems": problems}
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def install(source: str) -> dict:
    """装到 installed_dir()。清单里没有代码，用到的工具各自走工具包的权限和确认。"""
    info = inspect(source)
    if info["problems"]:
        raise AgentError("invalid_manifest", "Agent 不合格：" + "；".join(info["problems"]), info["problems"])
    folder, temp = _unpack(source)
    try:
        target = os.path.join(installed_dir(), info["name"])
        os.makedirs(installed_dir(), exist_ok=True)
        staging = target + ".installing"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(folder, staging, ignore=_export_ignore)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
    return info


def uninstall(name: str) -> None:
    agent = find(name)
    if agent is None:
        raise AgentError("not_found", "没有这个 Agent。")
    if agent.builtin:
        raise AgentError("builtin_agent", "内置 Agent 不能卸载。")
    shutil.rmtree(agent.folder)


def _export_ignore(_folder, names):
    return [n for n in names if n.startswith(".") or n in ("__pycache__", "tools.json") or n.endswith(".pyc")]


def export(name: str) -> bytes:
    agent = find(name)
    if agent is None:
        raise AgentError("not_found", "没有这个 Agent。")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for base, dirs, files in os.walk(agent.folder):
            dirs[:] = [d for d in dirs if d not in _export_ignore(base, dirs)]
            for filename in files:
                if filename in _export_ignore(base, [filename]):
                    continue
                full = os.path.join(base, filename)
                archive.write(full, os.path.join(name, os.path.relpath(full, agent.folder)))
    return buffer.getvalue()


# ---------- 工具集合 ----------

def _contract_name(item) -> str:
    if not isinstance(item, dict):
        return ""
    inner = item.get("function") if isinstance(item.get("function"), dict) else item
    return str(inner.get("name") or "")


def delegate_contract(choices: list[str]) -> dict:
    return {"type": "function", "function": _delegate_function(choices)}


def _delegate_function(choices: list[str]) -> dict:
    return {
        "name": DELEGATE,
        "description": (
            "把子任务交给一个或几个子 Agent。task 写清要做什么；inputs 只放句柄（内容句柄、工件标识），"
            "不要写画面描述或本地正文。结果只有状态、摘要和产出的句柄。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_TASKS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "agent": {"type": "string", "enum": choices or ["planner"]},
                            "task": {"type": "string", "maxLength": 1000},
                            "inputs": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 80}},
                        },
                        "required": ["agent", "task"],
                    },
                },
                "parallel": {"type": "boolean"},
            },
            "required": ["tasks"],
        },
    }


def allowed_tools(agent: Agent, *, depth: int, registered=None) -> set[str]:
    """清单允许 ∩ 已注册。子 Agent 永远拿不到 accept_local_content；到了深度上限拿不到 delegate。"""
    names = set(agent.tools())
    names &= set(registered if registered is not None else registered_tools()) | {DELEGATE}
    if depth > 0:
        names -= set(ORCHESTRATOR_ONLY)
    if depth >= MAX_DEPTH:
        names.discard(DELEGATE)
    return names


def contracts_for(agent: Agent, source: list[dict], *, depth: int, choices: list[str]) -> list[dict]:
    allowed = allowed_tools(agent, depth=depth, registered=[_contract_name(item) for item in source])
    out = [item for item in source if _contract_name(item) in allowed]
    if DELEGATE in allowed:
        out.append(delegate_contract(choices))
    return out


def not_allowed(name: str, agent: str) -> dict:
    return {
        "ok": False, "tool": name, "executed": False,
        "error": {"code": "tool_not_allowed", "message": f"Agent {agent} 不能调用这个工具。"},
    }


# ---------- 按后端串行 ----------

_backend_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def backends_of(tool: str) -> list[str]:
    from services.runtime.tools import _ensure

    _ensure()
    from tools import packs

    try:
        pack = packs.find(tool)
    except Exception:
        return []
    if pack is None:
        return []
    return sorted((pack.manifest.get("requires") or {}).get("backends") or [])


def backend_lock(name: str) -> threading.Lock:
    with _locks_guard:
        return _backend_locks.setdefault(name, threading.Lock())


def serialized(tool: str, call):
    """按工具声明的后端加锁（排好序，避免死锁）。"""
    locks = [backend_lock(name) for name in backends_of(tool)]
    for lock in locks:
        lock.acquire()
    try:
        return call()
    finally:
        for lock in reversed(locks):
            lock.release()


# ---------- 委托 ----------

class Pool:
    """整轮委托池：子 Agent 步数之和、委托次数。"""

    def __init__(self, tree_steps: int, delegations: int):
        self.tree_steps = int(tree_steps)
        self.delegations = int(delegations)
        self.steps_used = 0
        self.delegations_used = 0
        self._lock = threading.Lock()

    def take_delegation(self) -> int | None:
        """占一次委托，返回这次能用的步数上限；没有余量返回 None。"""
        with self._lock:
            left = self.tree_steps - self.steps_used
            if self.delegations_used >= self.delegations or left <= 0:
                return None
            self.delegations_used += 1
            return left

    def reserve(self, steps: int) -> int:
        with self._lock:
            grant = max(0, min(steps, self.tree_steps - self.steps_used))
            self.steps_used += grant
            return grant

    def refund(self, steps: int) -> None:
        with self._lock:
            self.steps_used = max(0, self.steps_used - max(0, steps))


def new_child_id() -> str:
    return "r_" + uuid.uuid4().hex


def summarize(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS] + "…"


class Delegator:
    """处理 delegate 调用。spawn 真正跑子 Agent：

    spawn(agent, task_text, *, run_id, parent_run_id, depth, budget, chain) -> {"status", "text", "artifacts", "steps", "error"}
    on_event(dict) 用来把 agent 节点推给界面（可选）。
    """

    def __init__(self, *, spawn, parent_run_id: str, pool: Pool, cancel=None, chain=(ORCHESTRATOR,),
                 depth: int = 0, on_event=None, agents_source=None):
        self.spawn = spawn
        self.parent_run_id = parent_run_id
        self.pool = pool
        self.cancel = cancel
        self.chain = tuple(chain)
        self.depth = depth
        self.on_event = on_event
        self._agents = agents_source

    def _find(self, name: str):
        agents = self._agents() if callable(self._agents) else discover()
        for agent in agents:
            if agent.name == name:
                return agent
        return None

    def choices(self) -> list[str]:
        agents = self._agents() if callable(self._agents) else discover()
        return [a.name for a in agents if a.role != "orchestrator"]

    def __call__(self, arguments) -> dict:
        tasks = arguments.get("tasks") if isinstance(arguments, dict) else None
        if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
            return _delegate_error("invalid_tool_call", f"tasks 要有 1-{MAX_TASKS} 个任务。")
        if self.depth + 1 > MAX_DEPTH:
            return _delegate_error("delegation_depth", "委托层数到上限了，自己完成或直接回答。")
        plans = []
        for item in tasks:
            if not isinstance(item, dict):
                return _delegate_error("invalid_tool_call", "每个任务要是对象。")
            name = str(item.get("agent") or "")
            agent = self._find(name)
            if agent is None or agent.role == "orchestrator":
                return _delegate_error("unknown_agent", f"没有可委托的 Agent：{name or '（空）'}。")
            if name in self.chain:
                return _delegate_error("delegation_cycle", f"{' → '.join(self.chain + (name,))} 成环了。")
            inputs = item.get("inputs") or []
            if not isinstance(inputs, list) or not all(isinstance(h, str) and _HANDLE.match(h) for h in inputs):
                return _delegate_error("invalid_tool_call", "inputs 只能放句柄。")
            plans.append((agent, str(item.get("task") or "").strip(), inputs))
        parallel = bool(arguments.get("parallel")) and len(plans) > 1
        if parallel:
            with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
                trace_id = telemetry.current()
                futures = [executor.submit(self._bound, trace_id, *plan) for plan in plans]
                results = [future.result() for future in futures]
        else:
            results = []
            for plan in plans:
                results.append(self._one(*plan))
                if self.cancel is not None and self.cancel.is_set():
                    break
        ok = all(item.get("status") == "completed" for item in results)
        return {"ok": ok, "tool": DELEGATE, "executed": True, "results": results}

    def _bound(self, trace_id, agent, task, inputs):
        telemetry.bind(trace_id, owner="web") if trace_id else None
        try:
            return self._one(agent, task, inputs)
        finally:
            if trace_id:
                telemetry.bind(None)

    def _one(self, agent, task, inputs) -> dict:
        if self.cancel is not None and self.cancel.is_set():
            return {"agent": agent.name, "status": "cancelled", "summary": "", "artifacts": [], "steps": 0}
        left = self.pool.take_delegation()
        if left is None:
            return {"agent": agent.name, "status": "failed", "summary": "", "artifacts": [], "steps": 0,
                    "error": {"code": "budget_exceeded", "message": "这一轮的委托预算用完了。"}}
        steps = self.pool.reserve(min(int(agent.budget_value("steps", 8)), left))
        budget = Budget(
            steps=max(1, steps),
            time_sec=float(agent.budget_value("time_sec", 300)),
            tokens=int(agent.budget_value("tokens", 60000)),
        )
        run_id = new_child_id()
        depth = self.depth + 1
        text = task
        if inputs:
            text += "\n可用句柄：" + "、".join(inputs)
        node = {"type": "agent", "run_id": run_id, "parent_run_id": self.parent_run_id, "agent": agent.name,
                "role": agent.role, "depth": depth}
        if callable(self.on_event):
            self.on_event(dict(node, status="running"))
        started = time.monotonic()
        try:
            out = self.spawn(agent, text, run_id=run_id, parent_run_id=self.parent_run_id, depth=depth,
                             budget=budget, chain=self.chain + (agent.name,))
        except Exception as exc:
            out = {"status": "failed", "text": "", "artifacts": [], "steps": 0,
                   "error": {"code": "agent_error", "message": f"子 Agent 出错（{type(exc).__name__}）。"}}
        used = int(out.get("steps") or 0)
        self.pool.refund(steps - min(used, steps))
        ms = int((time.monotonic() - started) * 1000)
        status = out.get("status") or "failed"
        span = dict(node, model=out.get("model") or "", ms=ms, status=status, steps=used)
        trace.add(self.parent_run_id, span)
        telemetry.record("agent.run", agent=agent.name, role=agent.role, depth=depth, run_id=run_id,
                         parent_run_id=self.parent_run_id, model=out.get("model") or "", ms=ms,
                         ok=status == "completed", status=status,
                         error_code=(out.get("error") or {}).get("code") if status != "completed" else None)
        if callable(self.on_event):
            self.on_event(span)
        result = {"agent": agent.name, "run_id": run_id, "status": status, "summary": summarize(out.get("text") or ""),
                  "artifacts": list(out.get("artifacts") or []), "steps": used}
        if out.get("error"):
            result["error"] = out["error"]
        return result


def _delegate_error(code: str, message: str) -> dict:
    return {"ok": False, "tool": DELEGATE, "executed": False, "error": {"code": code, "message": message}}


def run_child(agent: Agent, task: str, *, model: str, api_key: str, base_url, cancel, budget: Budget,
              run_id: str, depth: int, tool_source: list[dict], execute, mutating, confirm,
              delegator_factory=None, on_event=None, artifacts=None, local_bodies=None,
              timeout_sec: float = 30, system_suffix: str = "", choices=None) -> dict:
    """跑一个子 Agent。工具越权在运行时拒绝；accept_local_content 子 Agent 永远拿不到。"""
    from packages.contracts.events import (
        EVENT_MESSAGE_COMPLETED, EVENT_MESSAGE_DELTA, EVENT_RUN_FAILED, EVENT_TOOL_CONFIRM, EVENT_TOOL_REQUESTED,
    )
    from services.runtime.loop import run as runtime_run
    from services.runtime.tools import validate_arguments

    choices = list(choices or [])
    tools = contracts_for(agent, tool_source, depth=depth, choices=choices)
    allowed = {_contract_name(item) for item in tools}
    images = {"used": 0}
    cap = int(agent.budget_value("images", 0 if "generate_image" not in allowed else 8))
    delegator = delegator_factory(run_id, depth) if callable(delegator_factory) and DELEGATE in allowed else None

    def guard_validate(name, arguments):
        if name not in allowed:
            return not_allowed(name, agent.name)
        if name == DELEGATE:
            return None if isinstance(arguments, dict) else not_allowed(name, agent.name)
        return validate_arguments(name, arguments)

    def guard_mutating(name, arguments=None):
        if name not in allowed or name == DELEGATE:
            return False
        return mutating(name, arguments)

    def guard_execute(name, arguments):
        if name not in allowed:
            return not_allowed(name, agent.name)
        if name == DELEGATE:
            return delegator(arguments)
        if name == "generate_image":
            if images["used"] >= cap:
                return {"ok": False, "tool": name, "executed": False,
                        "error": {"code": "budget_exceeded", "message": "这个 Agent 的出图张数预算用完了。"}}
            images["used"] += 1
        return serialized(name, lambda: execute(name, arguments))

    request = {
        "model": model,
        "system": agent.manifest["instructions"] + system_suffix,
        "messages": [{"role": "user", "parts": [{"type": "text", "text": task}]}],
        "tools": tools,
    }
    text, status, error, steps = "", "failed", None, 0
    for event in runtime_run(
        request, api_key=api_key, cancel=cancel, timeout_sec=timeout_sec, budget=budget,
        confirm=confirm, execute=guard_execute, mutating=guard_mutating, validate=guard_validate,
        artifacts=artifacts, local_bodies=local_bodies, base_url=base_url, run_id=run_id,
    ):
        kind = event.get("type")
        if kind == EVENT_MESSAGE_DELTA:
            text += event.get("text") or ""
        elif kind == EVENT_MESSAGE_COMPLETED:
            text = event.get("text") or text
            status = "completed"
        elif kind == EVENT_RUN_FAILED:
            status = "cancelled" if event.get("code") == "cancelled" else "failed"
            error = {"code": event.get("code"), "message": event.get("message")}
        if callable(on_event) and kind in (EVENT_TOOL_REQUESTED, EVENT_TOOL_CONFIRM):
            on_event(event)
    steps = sum(1 for entry in trace.for_run(run_id) if entry.get("type") == "model")
    out = {"status": status, "text": text, "steps": steps, "model": model, "artifacts": []}
    if error:
        out["error"] = error
    return out
