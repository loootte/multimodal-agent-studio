"""观察、决定、调用工具或回答，直到结束或碰到预算。"""

from __future__ import annotations

import json
import inspect
import time
import uuid

from packages.contracts.events import (
    EVENT_MESSAGE_COMPLETED,
    EVENT_MESSAGE_DELTA,
    EVENT_RUN_FAILED,
    EVENT_RUN_STARTED,
    EVENT_TOOL_CONFIRM,
    EVENT_TOOL_REQUESTED,
    HUMAN,
    RETRYABLE,
)
from packages.contracts.request import message_parts, normalize
from services.gateway import GatewayError, describe, stream as gateway_stream
from services.gateway.openai_chat import reject_unsupported
from services.runtime.budget import Budget, Clock
from services import telemetry
from services.runtime import trace
from services.runtime.tools import execute_tool, openai_tools, tool_mutates, validate_arguments


def new_run_id() -> str:
    return "r_" + uuid.uuid4().hex


def run(
    request,
    *,
    api_key: str,
    cancel,
    timeout_sec: float = 30,
    budget=None,
    confirm=None,
    execute=None,
    mutating=None,
    take=None,
    resume=None,
    artifacts=None,
    local_bodies=None,
    base_url=None,
    run_id: str | None = None,
    validate=None,
):
    """产出统一事件。模型文本里说已经执行，不算执行。"""
    req = normalize(request)
    run_id = run_id or new_run_id()
    clock = Clock(budget if isinstance(budget, Budget) else Budget())
    execute = execute or execute_tool
    mutating = mutating or tool_mutates
    validate = validate or validate_arguments
    tools = req.get("tools")
    if tools is None:
        req = dict(req)
        req["tools"] = openai_tools()
    messages = [dict(item) for item in req.get("messages") or []]
    trace.start(run_id)
    if telemetry.owner() != "web":
        telemetry.bind(run_id, owner="runtime")
    yield {
        "type": EVENT_RUN_STARTED,
        "run_id": run_id,
        "model": req["model"],
        "ignored_params": list(req.get("ignored_params") or []),
    }
    try:
        reject_unsupported(req, describe(req["model"]))
    except GatewayError as exc:
        yield _failed(run_id, exc.code, exc.message)
        return
    while True:
        if cancel is not None and cancel.is_set():
            yield _failed(run_id, "cancelled", HUMAN["cancelled"])
            return
        reason = clock.check()
        if reason:
            yield _failed(run_id, "budget_exceeded", reason)
            return
        clock.add_step()
        step = {
            "model": req["model"],
            "system": req.get("system"),
            "messages": messages,
            "tools": req.get("tools"),
            "temperature": req.get("temperature"),
            "max_output": req.get("max_output"),
            "stop": req.get("stop"),
        }
        modalities = _modalities(messages)
        started = time.monotonic()
        tool_call = None
        text_parts: list[str] = []
        try:
            for event in gateway_stream(
                step,
                api_key=api_key,
                timeout_sec=timeout_sec,
                cancel=cancel,
                artifacts=artifacts,
                local_bodies=local_bodies,
                base_url=base_url,
            ):
                kind = event.get("type")
                if kind == EVENT_RUN_STARTED:
                    continue
                if kind == EVENT_MESSAGE_DELTA:
                    piece = event.get("text") or ""
                    if piece:
                        text_parts.append(piece)
                        yield {
                            "type": EVENT_MESSAGE_DELTA,
                            "text": piece,
                            "run_id": run_id,
                        }
                elif kind == EVENT_TOOL_REQUESTED and tool_call is None:
                    tool_call = event
                elif kind == EVENT_MESSAGE_COMPLETED:
                    continue
        except GatewayError as exc:
            yield _failed(run_id, exc.code, exc.message)
            return
        elapsed = int((time.monotonic() - started) * 1000)
        clock.tokens_used = _tokens_in(messages) + _tokens("".join(text_parts))
        trace.add(run_id, {
            "type": "model",
            "run_id": run_id,
            "model": req["model"],
            "modalities": modalities,
            "ms": elapsed,
            "step": clock.steps_used,
        })
        if tool_call is None:
            spoken = "".join(text_parts)
            # 模型没调工具就收尾时，调用方可以给一句续写指令（比如连续出图还没到张数），在同一个循环里接着做。
            note = resume(spoken) if callable(resume) else None
            if note:
                messages.append({"role": "assistant", "parts": [{"type": "text", "text": spoken or "（未完成）"}]})
                messages.append({"role": "user", "parts": [{"type": "text", "text": note}]})
                if spoken:
                    yield {"type": EVENT_MESSAGE_DELTA, "text": "\n", "run_id": run_id}
                continue
            yield {"type": EVENT_MESSAGE_COMPLETED, "run_id": run_id, "text": "".join(text_parts)}
            return
        if cancel is not None and cancel.is_set():
            yield _failed(run_id, "cancelled", HUMAN["cancelled"])
            return
        tool_started = time.monotonic()
        stopped = yield from _use_tool(
            run_id,
            messages,
            tool_call,
            "".join(text_parts),
            cancel,
            confirm,
            execute,
            mutating,
            take,
            validate,
        )
        # 时间预算只算模型思考：等人确认和工具执行（如一张图一分钟）不计入，免得连续出图做到一半被掐断。
        clock.started += time.monotonic() - tool_started
        if stopped:
            return


def _asks_confirm(mutating, name, arguments) -> bool:
    """确认回调可以只收工具名，也可以再收参数（清单 confirm=by_args 要看参数）。"""
    try:
        count = len(inspect.signature(mutating).parameters)
    except (TypeError, ValueError):
        count = 1
    return bool(mutating(name, arguments) if count >= 2 else mutating(name))


def _use_tool(run_id, messages, tool_call, spoken, cancel, confirm, execute, mutating, take=None, validate=None):
    name = tool_call.get("name") or ""
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    yield {
        "type": EVENT_TOOL_REQUESTED,
        "run_id": run_id,
        "name": name,
        "arguments": arguments,
        "call_id": tool_call.get("call_id"),
    }
    if _asks_confirm(mutating, name, arguments):
        yield {
            "type": EVENT_TOOL_CONFIRM,
            "run_id": run_id,
            "name": name,
            "arguments": arguments,
        }
        allowed = False
        if callable(confirm):
            allowed = bool(confirm(name, arguments))
        if cancel is not None and cancel.is_set():
            yield _failed(run_id, "cancelled", HUMAN["cancelled"])
            return True
        telemetry.record("tool.confirm", name=name, allowed=allowed)
        if not allowed:
            result = {
                "ok": False,
                "tool": name,
                "executed": False,
                "error": {"code": "rejected", "message": "这个工具需要确认后才能执行。"},
            }
            messages.append({"role": "assistant", "parts": [{"type": "text", "text": spoken or f"调用 {name}"}]})
            messages.append({
                "role": "user",
                "parts": [{"type": "text", "text": "工具结果 " + name + " " + json.dumps(result, ensure_ascii=False)}],
            })
            return False
    if callable(take) and take(name, arguments):
        return True
    problem = (validate or validate_arguments)(name, arguments)
    if problem:
        result = problem
        telemetry.record("tool", name=name, ms=0, ok=False, error_code=_code_of(problem), executed=False)
    else:
        if cancel is not None and cancel.is_set():
            yield _failed(run_id, "cancelled", HUMAN["cancelled"])
            return True
        started = time.monotonic()
        result = execute(name, arguments)
        trace.add(run_id, {
            "type": "tool",
            "run_id": run_id,
            "name": name,
            "ms": int((time.monotonic() - started) * 1000),
            "ok": bool(result.get("ok")),
        })
        telemetry.record(
            "tool",
            name=name,
            ms=int((time.monotonic() - started) * 1000),
            ok=bool(result.get("ok")),
            error_code=None if result.get("ok") else _code_of(result),
        )
    messages.append({"role": "assistant", "parts": [{"type": "text", "text": spoken or f"调用 {name}"}]})
    messages.append({
        "role": "user",
        "parts": [{"type": "text", "text": "工具结果 " + name + " " + json.dumps(result, ensure_ascii=False)}],
    })
    return False


def _code_of(result) -> str:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return "tool_failed"


def _failed(run_id: str, code: str, message: str) -> dict:
    return {
        "type": EVENT_RUN_FAILED,
        "run_id": run_id,
        "code": code,
        "message": message,
        "retryable": bool(RETRYABLE.get(code, False)),
    }


def _modalities(messages: list) -> list[str]:
    found = []
    for message in messages:
        for part in message_parts(message):
            kind = part.get("type")
            if kind and kind not in found:
                found.append(kind)
    return found or ["text"]


def _tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _tokens_in(messages: list) -> int:
    total = 0
    for message in messages:
        for part in message_parts(message):
            total += _tokens(part.get("text") or "")
    return total
