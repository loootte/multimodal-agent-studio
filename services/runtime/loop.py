"""观察、决定、调用工具或回答，直到结束或碰到预算。"""

from __future__ import annotations

import json
import re
import inspect
import time
import uuid

from packages.contracts.events import (
    EVENT_CONTEXT_COMPACTED,
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
from services.runtime import compaction as context, injection, trace
from services.runtime.tools import egress_preview, execute_tool, openai_tools, tool_mutates, validate_arguments


def new_run_id() -> str:
    return "r_" + uuid.uuid4().hex


def run(request, *args, run_id: str | None = None, **kwargs):
    """见 _run。外层只负责在这次运行期间把 run_id 绑到遥测上（ADR 0006）。"""
    run_id = run_id or new_run_id()
    previous = telemetry.enter_run(run_id)
    try:
        yield from _run(request, *args, run_id=run_id, **kwargs)
    finally:
        telemetry.leave_run(previous)


def _run(
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
    choose=None,
    compaction=None,
    archive=None,
):
    """产出统一事件。模型文本里说已经执行，不算执行。

    choose(messages) 可选：每一步之前返回 {"tool_choice": ..., "note": ...}，用来在运行时层面
    强制这一步调用某个工具（例如多智能体模式下编排者必须先 delegate）。供应商不接受强制时，
    这一步自动退回 auto 重试一次，之后不再强制。

    compaction（14.B #79）：None 用默认（模型 context_limit 的 70%），False 关闭，也可以传 Compaction / 设置 dict
    （非法项回退默认值，见 compaction.settings）。估算没超过阈值时请求和以前逐字节一致；超过时裁剪较早的大工具结果、
    把较早轮次压成摘要（默认模型生成，发前回后过本地正文检查，命中退回确定性抽取），整体再过一次检查，
    命中就退回不压缩。只记 context.compacted（条数、token 数），不记正文。发生过压缩以后工具契约里才多出
    session_query，按 ctx_N 引用取回被裁剪 / 折叠的原文（运行时自己执行，过本地正文检查，结果标 untrusted）。

    archive 可选：调用方传进来的 compaction.Archive（例如网页从本地会话记录读回的），压缩时往里加原文，
    session_query 也能取回之前运行存下的；调用方在运行后把 archive.added 写回本地。没传就只在本次运行内存里。
    """
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
    compactor = context.of(compaction)
    threshold = compactor.threshold(describe(req["model"])) if compactor else None
    fixed = context.tokens(req.get("system") or "") + context.tokens(
        json.dumps(req.get("tools") or [], ensure_ascii=False, sort_keys=True))
    archive = archive if isinstance(archive, context.Archive) else context.Archive()
    query_tool = None  # 发生过压缩、开着 session_query 以后才有（阈值以下请求不变）
    summarize = _summary_call(req, compactor, api_key=api_key, timeout_sec=timeout_sec, cancel=cancel,
                              artifacts=artifacts, local_bodies=local_bodies, base_url=base_url)
    forcing = callable(choose)
    while True:
        if cancel is not None and cancel.is_set():
            yield _failed(run_id, "cancelled", HUMAN["cancelled"])
            return
        reason = clock.check()
        if reason:
            yield _failed(run_id, "budget_exceeded", reason)
            return
        clock.add_step()
        if threshold:
            extra = context.tokens(json.dumps([query_tool], ensure_ascii=False, sort_keys=True)) if query_tool else 0
            event = _compact(run_id, messages, compactor, max(0, threshold - fixed - extra), local_bodies,
                             archive, summarize)
            if event:
                yield event
            if query_tool is None and compactor.session_query and len(archive) and not _offers(req, context.SESSION_QUERY):
                query_tool = context.session_query_contract()
        step = {
            "model": req["model"],
            "system": req.get("system"),
            "messages": messages,
            "tools": (list(req.get("tools") or []) + [query_tool]) if query_tool else req.get("tools"),
            "temperature": req.get("temperature"),
            "max_output": req.get("max_output"),
            "stop": req.get("stop"),
            "tool_choice": req.get("tool_choice"),
        }
        policy = choose(messages) if forcing else None
        if isinstance(policy, dict):
            if policy.get("tool_choice") is not None:
                step["tool_choice"] = policy["tool_choice"]
            if policy.get("note"):
                step["system"] = ((step.get("system") or "") + " " + policy["note"]).strip()
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
            forced = isinstance(policy, dict) and policy.get("tool_choice") not in (None, "auto")
            if forced and not text_parts and tool_call is None and exc.code not in ("cancelled", "timeout"):
                # 有的供应商（或推理模型）不接受强制 tool_choice：这一步退回 auto 重来，之后只靠调用方的续写提醒。
                forcing = False
                clock.steps_used -= 1
                telemetry.record("policy.fallback", reason="tool_choice", code=exc.code)
                continue
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
        if query_tool and tool_call.get("name") == context.SESSION_QUERY:
            yield {"type": EVENT_TOOL_REQUESTED, "run_id": run_id, "name": context.SESSION_QUERY,
                   "arguments": tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {},
                   "call_id": tool_call.get("call_id")}
            _query(run_id, messages, tool_call, "".join(text_parts), archive, local_bodies)
            clock.started += time.monotonic() - tool_started
            continue
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


def _offers(req, name) -> bool:
    for item in req.get("tools") or []:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict) and function.get("name") == name:
            return True
    return False


def _summary_call(req, compactor, *, api_key, timeout_sec, cancel, artifacts, local_bodies, base_url):
    """模型摘要的调用：同一个网关（照常过出站检查），不带工具；没开模型摘要返回 None。"""
    if not compactor or compactor.summary != "model" or compactor.summarize:
        return None
    model = compactor.summary_model or req["model"]

    def call(request: dict) -> str:
        step = {
            "model": model,
            "system": request["system"],
            "messages": request["messages"],
            "tools": None,
            "temperature": None,
            "max_output": 800,
            "stop": None,
            "tool_choice": None,
        }
        started = time.monotonic()
        parts: list[str] = []
        ok = False
        try:
            for event in gateway_stream(step, api_key=api_key, timeout_sec=timeout_sec, cancel=cancel,
                                        artifacts=artifacts, local_bodies=local_bodies, base_url=base_url):
                if event.get("type") == EVENT_MESSAGE_DELTA:
                    parts.append(event.get("text") or "")
                elif event.get("type") == EVENT_TOOL_REQUESTED:
                    break
            ok = True
        finally:
            telemetry.record("context.summary_call", ms=int((time.monotonic() - started) * 1000), ok=ok,
                             chars=sum(len(part) for part in parts))
        return "".join(parts)

    return call


def _compact(run_id, messages, compactor, threshold, local_bodies, archive=None, summarize=None):
    """超过阈值就就地换成压缩后的消息，返回 context.compacted 事件；没压缩返回 None。"""
    try:
        compacted, info = context.compact(messages, compactor, threshold, local_bodies=local_bodies,
                                          archive=archive, call=summarize)
    except Exception as exc:  # 压缩出错不影响这一步：照原样发（网关照常检查）
        telemetry.record("context.compaction_failed", error_code=type(exc).__name__)
        return None
    from services.gateway import egress

    fallback = info.get("summary_fallback")
    if fallback:
        # 模型摘要没用上（发前 / 回后命中本地正文、调用出错或为空）：退回确定性抽取，记审计（只有原因码）。
        telemetry.record("context.summary_fallback", error_code=fallback)
        if fallback.startswith("local_"):
            egress.security_event(fallback, "context_summary")
    if info.get("blocked"):
        # 摘要或裁剪结果里出现本地正文：退回不压缩，记安全事件（只有原因码）。
        telemetry.record("context.compaction_blocked", error_code=info["blocked"],
                         messages=info.get("messages_before"), tokens=info.get("tokens_before"))
        egress.security_event(info["blocked"], "chat_completion")
        return None
    if compacted is None:
        return None
    messages[:] = compacted
    fields = {key: info.get(key) for key in context.EVENT_FIELDS}
    telemetry.record(EVENT_CONTEXT_COMPACTED, **fields)
    return {"type": EVENT_CONTEXT_COMPACTED, "run_id": run_id, **fields}


def _query(run_id, messages, tool_call, spoken, archive, local_bodies) -> None:
    """session_query 由运行时自己执行（只读本次运行的内存档案），结果走 untrusted 工具结果通道。"""
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    started = time.monotonic()
    result = context.session_query(archive, arguments, local_bodies=local_bodies)
    ms = int((time.monotonic() - started) * 1000)
    trace.add(run_id, {"type": "tool", "run_id": run_id, "name": context.SESSION_QUERY, "ms": ms,
                       "ok": bool(result.get("ok"))})
    telemetry.record("tool", name=context.SESSION_QUERY, ms=ms, ok=bool(result.get("ok")),
                     error_code=None if result.get("ok") else _code_of(result), args=arg_digest(arguments))
    messages.append({"role": "assistant", "parts": [{"type": "text", "text": spoken or f"调用 {context.SESSION_QUERY}"}]})
    messages.append(injection.tool_result_message(context.SESSION_QUERY, result))


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
        try:
            summary = egress_preview(name, arguments)
        except Exception:  # 摘要拿不到时照样要确认，只是卡上少一块
            summary = None
        # 12.G：确认卡由宿主模板生成（工具名、参数、出站摘要），不带模型这一步说的话。
        card = injection.confirm_card(name, arguments, summary)
        yield {
            "type": EVENT_TOOL_CONFIRM,
            "run_id": run_id,
            "name": name,
            "arguments": arguments,
            "egress": summary,
            "card": card,
        }
        allowed = False
        if callable(confirm):
            answer = confirm(name, arguments)
            # 高危组合（读本地 + 联网）要二次确认：回调必须明确回 high_risk，只回 True 不算数。
            allowed = answer == injection.HIGH_RISK_ACK if card.get("requires_ack") else bool(answer)
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
            messages.append(injection.tool_result_message(name, result))
            return False
    if callable(take) and take(name, arguments):
        return True
    problem = (validate or validate_arguments)(name, arguments)
    if problem:
        result = problem
        telemetry.record("tool", name=name, ms=0, ok=False, error_code=_code_of(problem), executed=False,
                         args=arg_digest(arguments))
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
            args=arg_digest(arguments),
        )
    messages.append({"role": "assistant", "parts": [{"type": "text", "text": spoken or f"调用 {name}"}]})
    # 12.G：工具结果通道标 untrusted，只当数据回传（user 角色，不进系统指令）。
    messages.append(injection.tool_result_message(name, result))
    return False


_HANDLE = re.compile(r"(c|a|art|s|m|r)_[0-9a-f]{8,64}")


def arg_digest(arguments) -> dict | None:
    """工具参数摘要（ADR 0006）：句柄、数字、布尔原样；字符串只记长度；read_skill 等的 name 保留。不含正文。"""
    if not isinstance(arguments, dict):
        return None
    out = {}
    for key, value in list(arguments.items())[:20]:
        key = str(key)[:40]
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            if _HANDLE.fullmatch(value) or (key == "ref" and re.fullmatch(r"ctx_[0-9]{1,6}", value)) or (key == "name" and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", value)):
                out[key] = value
            else:
                out[key] = {"chars": len(value)}
        elif isinstance(value, list):
            out[key] = {"items": len(value)}
        elif isinstance(value, dict):
            out[key] = {"keys": sorted(str(k)[:40] for k in value)[:10]}
    return out


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
