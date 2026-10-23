"""一次回答。纯文字不调用 ComfyUI。

本地内容由专用工具收集。正文和文件字节留在本机会话。
出图只把文字句柄注入已有的 generate_image。文件句柄不提交。
页面断开后仍写完这一次。停止只置位，并取消已经提交的那一次出图，
不从这条线程关模型连接。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import time

import image_run
import skill_page
import store
from packages.contracts.events import (
    EVENT_ARTIFACT,
    EVENT_CONTENT_REQUEST,
    EVENT_ERROR,
    EVENT_MESSAGE_COMPLETED,
    EVENT_MESSAGE_DELTA,
    EVENT_PROGRESS,
    EVENT_RUN_FAILED,
    EVENT_RUN_STARTED,
    EVENT_RUNTIME_STEP,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CONFIRM,
    EVENT_TOOL_REQUESTED,
    HUMAN,
    RETRYABLE,
    TERMINAL_EVENTS,
)
from services.gateway import GatewayError, stream as gateway_stream
from services.runtime import execute_tool, traces_for, validate_arguments
from services.runtime.budget import Budget
from services.runtime.loop import run as runtime_run
from services.runtime.skills import catalog_suffix
from store import StoreError, new_id

SYSTEM = (
    "你是一个有帮助的助手。用用户使用的语言回答。"
    "这一轮用户消息里已经带上的图片附件，你可以直接看图回答。"
    "用户要识别、描述图片，或根据图片编写提示词时，直接写出文字。"
    "不要为此调用 accept_local_content，也不要调用 generate_image。"
    "用户要留下、附上、引用，或在后续任务里使用应留在本机的内容时，调用 accept_local_content。"
    "purpose 只能是 image_prompt 或 keep。kind 只能是 text 或 file。不要填写正文、字节、路径或文件名。"
    "只有用户明确要求生成一张新图时，purpose 才填 image_prompt，kind 填 text。"
    "用户提交后你只会收到句柄、种类和大小。"
    "查看本会话内容调用 list_local_content。丢掉一枚句柄调用 forget_local_content。"
    "检索本会话附件、本地内容和生成结果调用 retrieve_local，传入 query。结果带来源标识和可打开的地址，没有正文。"
    "只读演示查询调用 lookup_demo，传入 topic。"
    "要用文字句柄出图时，再调用 generate_image。一次调用只生成一张。用户要连续多张时，对每一张分别再调用一次。"
    "已经留下的本地提示词直接用句柄，不要再收集正文，也不要填写画面描述。文件句柄不能出图。"
    "不要编造工作流。没有成片之前，不要说图片已经生成。"
)
_TASK_LINE = "用户要生成一张图片。请调用 accept_local_content，purpose 填 image_prompt。不要编写画面描述。"
_DRAFT_MARKERS = ("提示词", "内容是", "画面是", "画面：", "画面:")
_CONTENT_WAIT_SEC = 600.0
_SEED = re.compile(r"(?:seed|种子)\s*[:：=]?\s*(\d{1,10})")
_ASPECTS = ("1:1", "16:9", "9:16")
_IMAGE_MARKERS = (
    "生成图",
    "生成一幅",
    "生成一张",
    "再生成",
    "画一",
    "画个",
    "画张",
    "出一张",
    "来一张",
    "做一张",
    "文生图",
    "出图",
)
_VISION_MARKERS = (
    "识别",
    "描述",
    "看这",
    "看看这",
    "这是什么",
    "图里",
    "图中",
    "写提示词",
    "编写提示词",
    "逆向",
)
_SCHEDULE_MARKERS = (
    "连续",
    "用本地提示词",
    "已经留下",
    "已有提示词",
    "已留下",
)
MAX_INPUT = 32000
MAX_MESSAGE = 16000
MAX_TOTAL = 100000
_TAKE_TOOLS = ("accept_local_content",)
_WEB_TOOLS = (
    "accept_local_content",
    "list_local_content",
    "forget_local_content",
    "retrieve_local",
    "generate_image",
    "build_skill_page",
)
_SKILL_MARKERS = ("等距", "等轴测", "isometric")


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message





class Run:
    def __init__(self, session_id: str, message_id: str):
        self.run_id = new_id("r_")
        self.session_id = session_id
        self.message_id = message_id
        self.events: list[dict] = []
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.cond = threading.Condition()
        self._state_lock = threading.Lock()
        self.prompt_id: str | None = None
        self.comfy_base: str | None = None
        self._progress_key: tuple | None = None
        self._content_ready = threading.Event()
        self._content_lock = threading.Lock()
        self._content_value: dict | None = None
        self._expect_kind = "text"
        self.waiting_content = False
        self._confirm_ready = threading.Event()
        self._confirm_lock = threading.Lock()
        self._confirm_value: bool | None = None
        self.waiting_confirm = False

    def begin_content_wait(self, kind: str = "text") -> None:
        self._content_ready.clear()
        with self._content_lock:
            self._content_value = None
            self._expect_kind = "file" if kind == "file" else "text"
            self.waiting_content = True

    def offer(self, kind: str, payload: dict) -> str:
        with self._content_lock:
            if not self.waiting_content or self.cancel.is_set():
                return "not_waiting"
            if kind != self._expect_kind:
                return "kind"
            self._content_value = payload
            self.waiting_content = False
        self._content_ready.set()
        return "ok"

    def wait_content(self, timeout: float = _CONTENT_WAIT_SEC) -> dict | None:
        if self.cancel.is_set():
            with self._content_lock:
                self.waiting_content = False
            return None
        signaled = self._content_ready.wait(timeout)
        with self._content_lock:
            self.waiting_content = False
            if self.cancel.is_set() or not signaled:
                return None
            return self._content_value

    def begin_confirm_wait(self) -> None:
        self._confirm_ready.clear()
        with self._confirm_lock:
            self._confirm_value = None
            self.waiting_confirm = True

    def offer_confirm(self, allowed: bool) -> str:
        with self._confirm_lock:
            if not self.waiting_confirm or self.cancel.is_set() or self._confirm_value is not None:
                return "not_waiting"
            self._confirm_value = bool(allowed)
        self._confirm_ready.set()
        return "ok"

    def wait_confirm(self, timeout: float = _CONTENT_WAIT_SEC) -> bool:
        if self.cancel.is_set():
            with self._confirm_lock:
                self.waiting_confirm = False
            return False
        signaled = self._confirm_ready.wait(timeout)
        with self._confirm_lock:
            self.waiting_confirm = False
            if self.cancel.is_set() or not signaled:
                return False
            return bool(self._confirm_value)

    def note_comfy(self, prompt_id: str, base: str) -> None:
        with self._state_lock:
            self.prompt_id = prompt_id
            self.comfy_base = base

    def accept_progress(self, key: tuple) -> bool:
        with self._state_lock:
            if self.cancel.is_set() or key == self._progress_key:
                return False
            self._progress_key = key
            return True

    def stop(self) -> None:
        # 不从这条线程关模型套接字。Windows 上那样会和 readline 卡住。
        with self._state_lock:
            self.cancel.set()
            prompt_id = self.prompt_id
            base = self.comfy_base
        with self._content_lock:
            self.waiting_content = False
        self._content_ready.set()
        with self._confirm_lock:
            self.waiting_confirm = False
        self._confirm_ready.set()
        if not prompt_id or not base:
            return
        try:
            image_run.cancel(base, prompt_id)
        except Exception as exc:
            print(f"chat cancel {type(exc).__name__}", file=sys.stderr)

    def push(self, event: dict) -> None:
        with self.cond:
            self.events.append(event)
            if event.get("type") in TERMINAL_EVENTS:
                self.done.set()
            self.cond.notify_all()

    def listen(self, after: int):
        index = max(0, after)
        while True:
            with self.cond:
                while index >= len(self.events) and not self.done.is_set():
                    self.cond.wait(timeout=1.0)
                if index >= len(self.events):
                    return
                event = self.events[index]
                index += 1
            yield event
            if event.get("type") in TERMINAL_EVENTS:
                return


class RunHub:
    def __init__(self):
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def add(self, run: Run) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)


def start(app, session_id: str, text, attachments) -> Run:
    if not isinstance(text, str):
        raise RequestError(400, "invalid_json", "文字需要是字符串。")
    text = text.strip()
    records = _resolve_attachments(app, session_id, attachments)
    if not text and not records:
        raise RequestError(400, "empty", "请输入文字。")
    if len(text) > MAX_INPUT:
        raise RequestError(400, "payload_too_large", HUMAN["payload_too_large"])
    message_id = new_id("m_")
    run = Run(session_id, message_id)
    try:
        assistant = app.sessions.append_turn(session_id, text, run.run_id, records)
    except StoreError as exc:
        if exc.message == "busy":
            raise RequestError(409, "busy", "这一会话还在回答，先停止再发送。") from exc
        raise RequestError(404, "not_found", "找不到这个会话。") from exc
    run.message_id = assistant["message_id"]
    app.runs.add(run)
    run.push({
        "type": EVENT_RUN_STARTED,
        "run_id": run.run_id,
        "message_id": run.message_id,
        "session_id": session_id,
    })
    provider = app.provider.resolve()
    problem = None
    if not provider["api_key"]:
        problem = GatewayError("missing_key", HUMAN["missing_key"])
    elif records:
        problem = _modality_problem(provider["model"], records)
    if problem is not None:
        _fail(app, session_id, run.message_id, run, problem, "")
        _log(session_id, provider["model"], "failed", 0.0)
        return run
    threading.Thread(
        target=_generate,
        args=(app, session_id, run.message_id, run, provider),
        daemon=True,
    ).start()
    return run


def submit_content(app, run_id: str, text) -> dict:
    if not isinstance(text, str):
        raise RequestError(400, "invalid_json", "文字需要是字符串。")
    text = text.strip()
    if not text:
        raise RequestError(400, "empty", "请输入要留在本机的内容。")
    if len(text) > MAX_INPUT:
        raise RequestError(400, "payload_too_large", HUMAN["payload_too_large"])
    return _offer(app, run_id, "text", {"kind": "text", "text": text})


def skip_content(app, run_id: str) -> dict:
    run = app.runs.get(run_id)
    if run is None:
        raise RequestError(409, "not_waiting", "现在没有在等待本地内容。")
    kind = "file" if run._expect_kind == "file" else "text"
    return _offer(app, run_id, kind, {"kind": kind, "skip": True})


def submit_file(app, run_id: str, encoded, media_type) -> dict:
    if not isinstance(encoded, str) or not encoded.strip():
        raise RequestError(400, "empty", "请选择要留在本机的文件。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RequestError(400, "invalid_json", "文件内容无法读取。") from exc
    if not data:
        raise RequestError(400, "empty", "请选择要留在本机的文件。")
    if len(data) > store.MAX_FILE:
        raise RequestError(400, "payload_too_large", HUMAN["payload_too_large"])
    payload = {"kind": "file", "data": data, "media_type": store.clean_media_type(media_type)}
    return _offer(app, run_id, "file", payload)


def submit_confirm(app, run_id: str, allowed) -> dict:
    run = app.runs.get(run_id)
    if run is None:
        raise RequestError(409, "not_waiting", "现在没有在等待确认。")
    if run.offer_confirm(bool(allowed)) != "ok":
        raise RequestError(409, "not_waiting", "现在没有在等待确认。")
    return {"ok": True}


def add_attachment(app, session_id: str, encoded, media_type, name="") -> dict:
    if not isinstance(encoded, str) or not encoded.strip():
        raise RequestError(400, "empty", "请选择附件。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RequestError(400, "invalid_json", "文件内容无法读取。") from exc
    if not data:
        raise RequestError(400, "empty", "请选择附件。")
    if len(data) > store.MAX_ATTACHMENT:
        raise RequestError(400, "payload_too_large", HUMAN["payload_too_large"])
    try:
        return app.sessions.add_attachment(
            session_id, data, store.clean_media_type(media_type), store.clean_filename(name),
        )
    except StoreError as exc:
        if exc.message == "空文件。":
            raise RequestError(400, "empty", "请选择附件。") from exc
        if exc.message == "太大。":
            raise RequestError(400, "payload_too_large", HUMAN["payload_too_large"]) from exc
        if exc.message == "找不到这个会话。":
            raise RequestError(404, "not_found", "找不到这个会话。") from exc
        raise RequestError(400, "invalid_json", "没能留下这个附件。") from exc


def _resolve_attachments(app, session_id: str, attachments) -> list[dict]:
    if attachments in (None, []):
        return []
    if not isinstance(attachments, list):
        raise RequestError(400, "invalid_json", "附件需要是列表。")
    records = []
    seen = set()
    for item in attachments:
        handle = item.get("handle") if isinstance(item, dict) else item
        if not isinstance(handle, str) or not handle:
            raise RequestError(400, "invalid_json", "附件缺少句柄。")
        if handle in seen:
            continue
        seen.add(handle)
        info = app.sessions.attachment_info(session_id, handle)
        if info is None:
            raise RequestError(400, "invalid_json", "找不到这个附件。")
        records.append(info)
    return records


def _modality_problem(model: str, records: list[dict]):
    from services.gateway import describe
    caps = describe(model)
    accepted = set(caps.get("input_modalities") or ())
    max_bytes = caps.get("max_file_bytes")
    max_images = caps.get("max_images")
    images = 0
    for item in records:
        kind = item.get("kind")
        if kind not in accepted:
            return GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
        size = item.get("size") or 0
        if max_bytes is not None and size > max_bytes:
            return GatewayError("payload_too_large", HUMAN["payload_too_large"])
        if kind == "image":
            images += 1
    if max_images is not None and images > max_images:
        return GatewayError("payload_too_large", HUMAN["payload_too_large"])
    return None


def _offer(app, run_id: str, kind: str, payload: dict) -> dict:
    run = app.runs.get(run_id)
    if run is None:
        raise RequestError(409, "not_waiting", "现在没有在等待本地内容。")
    outcome = run.offer(kind, payload)
    if outcome == "ok":
        return {"ok": True}
    if outcome == "kind":
        message = "这次要留下的是文字。" if kind == "file" else "这次要留下的是文件。"
        raise RequestError(400, "invalid_json", message)
    raise RequestError(409, "not_waiting", "现在没有在等待本地内容。")


def stop(app, session_id: str) -> dict:
    try:
        session = app.sessions.get(session_id)
    except StoreError as exc:
        raise RequestError(404, "not_found", "找不到这个会话。") from exc
    run_id = None
    for message in reversed(session["messages"]):
        if message.get("status") == "running":
            run_id = message.get("run_id")
            break
    if not run_id:
        return {"ok": True, "running": False}
    run = app.runs.get(run_id)
    if run is not None:
        run.stop()
    return {"ok": True, "running": True}


def _generate(app, session_id: str, message_id: str, run: Run, provider: dict) -> None:
    started = time.monotonic()
    status = "failed"
    try:
        session = app.sessions.get(session_id)
        user_id, user_text, attachments = _latest_user(session)
        image_request = _image_request(user_text, attachments)
        if image_request and user_id:
            app.sessions.put_content(session_id, user_id, user_text)
            session = app.sessions.get(session_id)
        if image_request:
            status = _generate_image_turn(app, session_id, message_id, run, provider, session, user_id, user_text)
        else:
            status = _generate_text_turn(app, session_id, message_id, run, provider, session, user_text)
    except Exception as exc:
        current = app.sessions.message_text(session_id, message_id)
        _fail(
            app,
            session_id,
            message_id,
            run,
            GatewayError("provider_error", HUMAN["provider_error"]),
            current,
        )
        print(f"chat run failed {type(exc).__name__}", file=sys.stderr)
    finally:
        _log(session_id, provider["model"], status, time.monotonic() - started)


def _generate_image_turn(app, session_id, message_id, run, provider, session, user_id, user_text) -> str:
    messages = _turn_messages(app, session_id, session, replace_user_id=user_id, task=_TASK_LINE)
    try:
        tool_call, _text = _ask(app, session_id, message_id, run, provider, messages, True)
    except GatewayError as exc:
        if _blocks(exc, run):
            return _fail_status(app, session_id, message_id, run, exc)
        tool_call = None
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    block = _handle_block(app, session_id, tool_call)
    if block == "missing":
        return _error_finish(
            app, session_id, message_id, run, "invalid_tool_call", "这个内容句柄不属于本会话。", ""
        )
    if block == "file":
        return _refuse_file_image(app, session_id, message_id, run, tool_call)
    return _continue_after_content(
        app, session_id, message_id, run, provider, user_id, user_text,
        True, _picture_draft(user_text), "image_prompt", "text",
    )


def _generate_text_turn(app, session_id, message_id, run, provider, session, user_text) -> str:
    request = _gateway_request(provider, _turn_messages(app, session_id, session))
    spoken: list[str] = []
    seen = 0
    claimed: dict = {}

    def take(name, arguments):
        if name not in _TAKE_TOOLS:
            return False
        claimed["name"] = name
        claimed["arguments"] = arguments
        claimed["spoken"] = "".join(spoken)
        return True

    def confirm(name, arguments):
        return run.wait_confirm()

    def execute(name, arguments):
        if name in _WEB_TOOLS and name not in _TAKE_TOOLS:
            return _execute_web_tool(app, session_id, message_id, run, name, arguments)
        return execute_tool(name, arguments)

    def flush_traces() -> None:
        nonlocal seen
        items = traces_for(run.run_id)
        for entry in items[seen:]:
            seen += 1
            event = _emit_runtime(app, session_id, message_id, run, entry, persist=True)
            if event.get("kind") == "tool" and event.get("name"):
                _push_saved(app, session_id, message_id, run, {
                    "type": EVENT_TOOL_CALL,
                    "tool": event.get("name"),
                    "status": "failed" if event.get("ok") is False else "success",
                    "message_id": message_id,
                    "run_id": run.run_id,
                })

    _emit_runtime(
        app, session_id, message_id, run,
        {"kind": "status", "label": "正在询问模型"},
        persist=False,
    )
    try:
        for event in runtime_run(
            request,
            api_key=provider["api_key"],
            cancel=run.cancel,
            timeout_sec=app.timeout_sec,
            take=take,
            confirm=confirm,
            execute=execute,
            budget=Budget(steps=24, time_sec=max(120.0, float(app.timeout_sec))),  # 连续出图：列出句柄 + 多次 generate_image + 收尾
            run_id=run.run_id,
            artifacts=_session_artifacts(app, session_id, session),
            local_bodies=app.sessions.local_bodies(session_id),
            base_url=provider["base_url"],
        ):
            flush_traces()
            kind = event.get("type")
            if kind == EVENT_RUN_STARTED:
                continue
            if kind == EVENT_MESSAGE_DELTA:
                piece = event.get("text") or ""
                if not piece:
                    continue
                spoken.append(piece)
                app.sessions.update_message(
                    session_id, message_id, text="".join(spoken), status="running", error=None,
                )
                run.push({"type": EVENT_MESSAGE_DELTA, "text": piece})
            elif kind == EVENT_TOOL_REQUESTED:
                name = event.get("name") or ""
                args = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
                _emit_runtime(
                    app, session_id, message_id, run,
                    {"kind": "tool", "name": name, "label": "调用 " + name},
                    persist=False,
                )
                _push_saved(app, session_id, message_id, run, {
                    "type": EVENT_TOOL_CALL,
                    "tool": name,
                    "arguments": _public_args(args),
                    "status": "running",
                    "message_id": message_id,
                    "run_id": run.run_id,
                })
            elif kind == EVENT_TOOL_CONFIRM:
                run.begin_confirm_wait()
                name = event.get("name") or ""
                args = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
                _push_saved(app, session_id, message_id, run, {
                    "type": EVENT_TOOL_CONFIRM,
                    "name": name,
                    "tool": name,
                    "arguments": _public_args(args),
                    "message_id": message_id,
                    "run_id": run.run_id,
                })
                _push_saved(app, session_id, message_id, run, {
                    "type": EVENT_TOOL_CALL,
                    "tool": name,
                    "arguments": _public_args(args),
                    "status": "waiting_confirm",
                    "message_id": message_id,
                    "run_id": run.run_id,
                })
            elif kind == EVENT_MESSAGE_COMPLETED:
                flush_traces()
                _complete(app, session_id, message_id, run, event.get("text") or "".join(spoken))
                return "completed"
            elif kind == EVENT_RUN_FAILED:
                flush_traces()
                code = str(event.get("code") or "provider_error")
                message = str(event.get("message") or HUMAN.get(code) or HUMAN["provider_error"])
                retryable = event.get("retryable")
                error = GatewayError(code, message)
                if retryable is not None:
                    error.retryable = bool(retryable)
                _fail(app, session_id, message_id, run, error, "".join(spoken))
                return "cancelled" if code == "cancelled" else "failed"
        flush_traces()
        if claimed:
            tool_call = {
                "type": EVENT_TOOL_CALL,
                "name": claimed.get("name"),
                "arguments": claimed.get("arguments") if isinstance(claimed.get("arguments"), dict) else {},
            }
            return _dispatch_tool(
                app, session_id, message_id, run, provider, user_text, tool_call, claimed.get("spoken") or "",
            )
        _complete(app, session_id, message_id, run, "".join(spoken))
        return "completed"
    except GatewayError as exc:
        return _fail_status(app, session_id, message_id, run, exc)


def _dispatch_tool(app, session_id, message_id, run, provider, user_text, tool_call, text) -> str:
    name = tool_call.get("name") if isinstance(tool_call, dict) else ""
    if name == "accept_local_content":
        purpose, kind = _accept_args(tool_call)
        return _continue_after_content(
            app, session_id, message_id, run, provider, "", user_text, False, "", purpose, kind,
        )
    if name in ("list_local_content", "forget_local_content", "lookup_demo", "retrieve_local"):
        return _follow_local_tool(app, session_id, message_id, run, provider, user_text, tool_call)
    if name == "generate_image":
        return _run_bound(app, session_id, message_id, run, user_text, tool_call, False)
    if name == "build_skill_page":
        return _finish_skill_page(app, session_id, message_id, run, text, tool_call)
    return _finish_image(app, session_id, message_id, run, text, tool_call)


def _continue_after_content(
    app, session_id, message_id, run, provider, user_id, user_text, image, draft, purpose, kind,
) -> str:
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    _emit_runtime(
        app, session_id, message_id, run,
        {"kind": "tool", "name": "accept_local_content", "ms": 0, "ok": True},
        persist=True,
    )
    run.begin_content_wait(kind)
    hint = "请选择要留在本机的文件。" if kind == "file" else "请输入要留在本机的内容。"
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_CONTENT_REQUEST,
        "message_id": message_id,
        "run_id": run.run_id,
        "purpose": purpose,
        "kind": kind,
        "hint": hint,
        "draft": draft if kind == "text" else "",
    })
    payload = run.wait_content()
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    if payload and payload.get("skip"):
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", "已取消。"))
    if not payload:
        current = app.sessions.message_text(session_id, message_id)
        return _error_finish(app, session_id, message_id, run, "timeout", "没有收到要留在本机的内容。", current)
    try:
        if kind == "file":
            record = app.sessions.add_file(
                session_id, payload.get("data") or b"", payload.get("media_type") or "", purpose,
            )
            submitted = ""
        else:
            submitted = payload.get("text") or ""
            record = app.sessions.add_content(session_id, submitted, purpose)
    except StoreError:
        current = app.sessions.message_text(session_id, message_id)
        return _error_finish(app, session_id, message_id, run, "provider_error", HUMAN["provider_error"], current)
    handle = record.get("handle") or ""
    stored_kind = record.get("kind") or kind
    print(f"content {session_id} {handle} {stored_kind} {record.get('size')}", file=sys.stderr)
    session = app.sessions.get(session_id)
    ready = _ready_line(handle) if image else _kept_line(handle, stored_kind, record.get("size") or 0)
    messages = _turn_messages(
        app,
        session_id,
        session,
        replace_user_id=user_id if image else None,
        task=_TASK_LINE if image else None,
        extra=[{"role": "user", "content": ready}],
    )
    tool_call = None
    reply = ""
    try:
        tool_call, reply = _ask(app, session_id, message_id, run, provider, messages, image)
    except GatewayError as exc:
        if _blocks(exc, run) or not image:
            return _fail_status(app, session_id, message_id, run, exc)
        tool_call = None
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    if image:
        bound = _bind_submitted(submitted, tool_call)
        return _finish_image(app, session_id, message_id, run, "", bound)
    name = tool_call.get("name") if isinstance(tool_call, dict) else ""
    if name == "generate_image":
        if stored_kind == "file":
            return _refuse_file_image(app, session_id, message_id, run, tool_call)
        bound = _bind_submitted(submitted, tool_call)
        return _finish_image(app, session_id, message_id, run, reply, bound)
    if name in ("list_local_content", "forget_local_content", "lookup_demo", "retrieve_local"):
        return _follow_local_tool(app, session_id, message_id, run, provider, user_text, tool_call)
    _complete(app, session_id, message_id, run, reply)
    return "completed"


def _follow_local_tool(app, session_id, message_id, run, provider, user_text, tool_call) -> str:
    line = _apply_local_tool(app, session_id, message_id, run, tool_call)
    if line is None:
        current = app.sessions.message_text(session_id, message_id)
        return _error_finish(app, session_id, message_id, run, "invalid_tool_call", HUMAN["invalid_tool_call"], current)
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    session = app.sessions.get(session_id)
    messages = _turn_messages(
        app, session_id, session, extra=[{"role": "user", "content": line}],
    )
    try:
        follow, reply = _ask(app, session_id, message_id, run, provider, messages, False)
    except GatewayError as exc:
        return _fail_status(app, session_id, message_id, run, exc)
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    if isinstance(follow, dict) and follow.get("name") == "generate_image":
        return _run_bound(app, session_id, message_id, run, user_text, follow, False)
    _complete(app, session_id, message_id, run, reply)
    return "completed"


def _apply_local_tool(app, session_id, message_id, run, tool_call) -> str | None:
    name = tool_call.get("name") if isinstance(tool_call, dict) else ""
    if name == "list_local_content":
        try:
            items = app.sessions.list_content(session_id)
        except StoreError:
            items = []
        result = {"items": items}
        arguments = {}
    elif name == "retrieve_local":
        raw = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
        query = raw.get("query") if isinstance(raw.get("query"), str) else ""
        try:
            items = app.sessions.retrieve(session_id, query)
        except StoreError:
            items = []
        result = {"items": items}
        arguments = {"query": query}
    elif name == "forget_local_content":
        handle = _named_handle(tool_call)
        status = app.sessions.forget_content(session_id, handle) if handle else "absent"
        result = {"status": status}
        if handle:
            result["handle"] = handle
        arguments = {"content_handle": handle} if handle else {}
    elif name == "lookup_demo":
        arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
        result = execute_tool(name, arguments)
    else:
        return None
    _emit_runtime(
        app, session_id, message_id, run,
        {"kind": "tool", "name": name, "ms": 0, "ok": True},
        persist=True,
    )
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_TOOL_CALL,
        "tool": name,
        "arguments": arguments,
        "result": result,
        "status": "success",
        "message_id": message_id,
        "run_id": run.run_id,
    })
    return "本地工具结果 " + name + " " + json.dumps(result, ensure_ascii=False)


def _ask(app, session_id, message_id, run, provider, messages, hide_text: bool) -> tuple[dict | None, str]:
    parts: list[str] = []
    tool_call = None
    started = time.monotonic()
    _emit_runtime(
        app, session_id, message_id, run,
        {"kind": "status", "label": "正在询问模型"},
        persist=False,
    )
    session = app.sessions.get(session_id)
    for event in gateway_stream(
        _gateway_request(provider, messages),
        api_key=provider["api_key"],
        timeout_sec=app.timeout_sec,
        cancel=run.cancel,
        artifacts=_session_artifacts(app, session_id, session),
        local_bodies=app.sessions.local_bodies(session_id),
        base_url=provider["base_url"],
    ):
        kind = event.get("type")
        if kind == EVENT_MESSAGE_DELTA:
            piece = event.get("text") or ""
            if hide_text or not piece:
                continue
            parts.append(piece)
            app.sessions.update_message(
                session_id, message_id, text="".join(parts), status="running", error=None
            )
            run.push({"type": EVENT_MESSAGE_DELTA, "text": piece})
        elif kind == EVENT_TOOL_REQUESTED and tool_call is None:
            tool_call = {
                "type": EVENT_TOOL_CALL,
                "name": event.get("name"),
                "arguments": event.get("arguments") or {},
                "call_id": event.get("call_id"),
            }
    elapsed = int((time.monotonic() - started) * 1000)
    _emit_runtime(
        app, session_id, message_id, run,
        {"kind": "model", "name": provider.get("model") or "", "ms": elapsed},
        persist=True,
    )
    if tool_call:
        _emit_runtime(
            app, session_id, message_id, run,
            {"kind": "tool", "name": tool_call.get("name") or "", "label": "调用 " + (tool_call.get("name") or "")},
            persist=False,
        )
    return tool_call, "".join(parts)


def _gateway_request(provider: dict, messages: list[dict]) -> dict:
    system = None
    items = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system" and system is None and isinstance(content, str):
            system = content
            continue
        parts = [{"type": "text", "text": content if isinstance(content, str) else ""}]
        for item in message.get("attachments") or []:
            if not isinstance(item, dict) or item.get("kind") != "image":
                continue
            handle = item.get("handle")
            if not isinstance(handle, str) or not handle:
                continue
            parts.append({
                "type": "image",
                "artifact_id": handle,
                "media_type": item.get("media_type") or "image/png",
            })
        items.append({
            "role": role,
            "parts": parts,
        })
    return {
        "model": provider["model"],
        "system": system,
        "messages": items,
        "tools": image_run.image_tools(),
    }


def _blocks(exc: GatewayError, run: Run) -> bool:
    return exc.code == "local_content" or exc.code == "cancelled" or run.cancel.is_set()


def _fail_status(app, session_id, message_id, run, exc: GatewayError) -> str:
    current = app.sessions.message_text(session_id, message_id)
    _fail(app, session_id, message_id, run, exc, current)
    return "cancelled" if exc.code == "cancelled" else "failed"


def _complete(app, session_id, message_id, run, text: str) -> None:
    app.sessions.update_message(session_id, message_id, text=text, status="completed", error=None)
    app.sessions.refresh_memory(session_id)
    run.push({
        "type": EVENT_MESSAGE_COMPLETED,
        "text": text,
        "message_id": message_id,
        "run_id": run.run_id,
    })


def _fail(app, session_id: str, message_id: str, run: Run, error: GatewayError, text: str) -> None:
    state = "cancelled" if error.code == "cancelled" else "failed"
    retryable = bool(getattr(error, "retryable", RETRYABLE.get(error.code, False)))
    app.sessions.update_message(
        session_id,
        message_id,
        text=text,
        status=state,
        error={"code": error.code, "message": error.message, "retryable": retryable},
    )
    app.sessions.refresh_memory(session_id)
    run.push({
        "type": EVENT_RUN_FAILED,
        "code": error.code,
        "message": error.message,
        "retryable": retryable,
        "text": text,
        "message_id": message_id,
        "run_id": run.run_id,
    })


def _finish_image(app, session_id: str, message_id: str, run: Run, text: str, tool_call: dict) -> str:
    name = tool_call.get("name") if isinstance(tool_call.get("name"), str) else ""
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_TOOL_CALL,
        "tool": name,
        "arguments": arguments,
        "status": "running",
        "message_id": message_id,
        "run_id": run.run_id,
    })
    if run.cancel.is_set():
        return _error_finish(app, session_id, message_id, run, "cancelled", HUMAN["cancelled"], text)
    if name != "generate_image":
        return _error_finish(
            app, session_id, message_id, run, "invalid_tool_call", "这一版只能生成图片。", text
        )
    result = _run_image(app, session_id, message_id, run, arguments)
    if run.cancel.is_set():
        return _error_finish(app, session_id, message_id, run, "cancelled", HUMAN["cancelled"], text)
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error") if isinstance(result, dict) else None
        error = error if isinstance(error, dict) else {}
        code = str(error.get("code") or "comfyui_error")
        message = str(error.get("message") or "生成失败。")
        if "已经生成" in message:
            message = "生成失败。"
        return _error_finish(app, session_id, message_id, run, code, message, text)
    _push_saved(app, session_id, message_id, run, _artifact_event(message_id, result))
    _complete(app, session_id, message_id, run, text)
    return "completed"


def _execute_web_tool(app, session_id, message_id, run, name, arguments) -> dict:
    if name == "generate_image":
        return _execute_generate_image(app, session_id, message_id, run, arguments)
    tool_call = {"name": name, "arguments": arguments if isinstance(arguments, dict) else {}}
    line = _apply_local_tool(app, session_id, message_id, run, tool_call)
    if line is None:
        return {
            "ok": False,
            "tool": name,
            "error": {"code": "invalid_tool_call", "message": HUMAN["invalid_tool_call"]},
        }
    prefix = "本地工具结果 " + name + " "
    if line.startswith(prefix):
        try:
            parsed = json.loads(line[len(prefix):])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            if "ok" not in parsed:
                parsed["ok"] = True
            parsed.setdefault("tool", name)
            return parsed
    return {"ok": True, "tool": name}


def _execute_generate_image(app, session_id, message_id, run, arguments) -> dict:
    tool_call = {
        "name": "generate_image",
        "arguments": arguments if isinstance(arguments, dict) else {},
    }
    handle = _named_handle(tool_call)
    block = _handle_block(app, session_id, tool_call)
    if block == "file":
        return {
            "ok": False,
            "tool": "generate_image",
            "content_handle": handle,
            "error": {"code": "invalid_tool_call", "message": "文件内容不能用来生成图片。"},
        }
    bound = _bind_image(app, session_id, "", tool_call, False)
    if bound is None:
        return {
            "ok": False,
            "tool": "generate_image",
            "content_handle": handle,
            "error": {"code": "invalid_tool_call", "message": "这个内容句柄不属于本会话。"},
        }
    if run.cancel.is_set():
        return {
            "ok": False,
            "tool": "generate_image",
            "content_handle": handle,
            "error": {"code": "cancelled", "message": HUMAN["cancelled"]},
        }
    result = _run_image(app, session_id, message_id, run, bound.get("arguments") or {})
    public = _public_generate_result(result, handle)
    if public.get("ok"):
        _push_saved(app, session_id, message_id, run, _artifact_event(message_id, result))
        app.sessions.update_message(
            session_id, message_id, text=app.sessions.message_text(session_id, message_id), status="running", error=None,
        )
    return public


def _run_image(app, session_id, message_id, run, arguments) -> dict:
    base = image_run.comfy_base()
    with run._state_lock:
        run._progress_key = None

    def on_event(event: dict) -> None:
        if not isinstance(event, dict):
            return
        if event.get("type") == "submitted":
            prompt_id = event.get("prompt_id")
            if isinstance(prompt_id, str) and prompt_id and base:
                run.note_comfy(prompt_id, base)
            return
        if event.get("type") != "progress" or run.cancel.is_set():
            return
        progress = _public_progress(event)
        if progress is None:
            return
        key = (progress.get("value"), progress.get("max"), progress.get("percent"))
        if not run.accept_progress(key):
            return
        progress["message_id"] = message_id
        _push_saved(app, session_id, message_id, run, progress)

    started = time.monotonic()
    result = image_run.generate(session_id, arguments, on_event)
    _emit_runtime(
        app, session_id, message_id, run,
        {
            "kind": "tool",
            "name": "generate_image",
            "ms": int((time.monotonic() - started) * 1000),
            "ok": bool(isinstance(result, dict) and result.get("ok")),
        },
        persist=True,
    )
    return result if isinstance(result, dict) else {"ok": False}


def _artifact_event(message_id: str, result: dict) -> dict:
    return {
        "type": EVENT_ARTIFACT,
        "artifact_id": result.get("artifact_id"),
        "media": result.get("type") or "image",
        "media_type": result.get("media_type"),
        "width": result.get("width"),
        "height": result.get("height"),
        "url": result.get("url"),
        "message_id": message_id,
    }


def _public_generate_result(result: dict, handle: str) -> dict:
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error") if isinstance(result, dict) else None
        error = error if isinstance(error, dict) else {}
        code = str(error.get("code") or "comfyui_error")
        message = str(error.get("message") or "生成失败。")
        if "已经生成" in message:
            message = "生成失败。"
        public = {
            "ok": False,
            "tool": "generate_image",
            "error": {"code": code, "message": message},
        }
        if handle:
            public["content_handle"] = handle
        return public
    public = {
        "ok": True,
        "tool": "generate_image",
        "artifact_id": result.get("artifact_id"),
        "media": result.get("type") or "image",
        "media_type": result.get("media_type"),
        "width": result.get("width"),
        "height": result.get("height"),
        "url": result.get("url"),
    }
    if handle:
        public["content_handle"] = handle
    return public


def _finish_skill_page(app, session_id: str, message_id: str, run: Run, text: str, tool_call: dict) -> str:
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_TOOL_CALL,
        "tool": "build_skill_page",
        "arguments": _public_args(arguments),
        "status": "running",
        "message_id": message_id,
        "run_id": run.run_id,
    })
    if run.cancel.is_set():
        return _error_finish(app, session_id, message_id, run, "cancelled", HUMAN["cancelled"], text)
    problem = validate_arguments("build_skill_page", arguments)
    if problem:
        error = problem.get("error") if isinstance(problem.get("error"), dict) else {}
        return _error_finish(
            app, session_id, message_id, run, "invalid_tool_call",
            str(error.get("message") or HUMAN["invalid_tool_call"]), text,
        )
    started = time.monotonic()
    result = skill_page.build_page(session_id, str(arguments.get("name") or ""), arguments.get("subject"))
    _emit_runtime(
        app, session_id, message_id, run,
        {
            "kind": "tool",
            "name": "build_skill_page",
            "ms": int((time.monotonic() - started) * 1000),
            "ok": bool(isinstance(result, dict) and result.get("ok")),
        },
        persist=True,
    )
    if run.cancel.is_set():
        return _error_finish(app, session_id, message_id, run, "cancelled", HUMAN["cancelled"], text)
    if not isinstance(result, dict) or not result.get("ok") or result.get("tool") == "generate_image":
        error = result.get("error") if isinstance(result, dict) else None
        error = error if isinstance(error, dict) else {}
        return _error_finish(
            app, session_id, message_id, run,
            str(error.get("code") or "skill_error"),
            str(error.get("message") or "等距图没有做成。"),
            text,
        )
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_ARTIFACT,
        "artifact_id": result.get("artifact_id"),
        "media": result.get("type") or "html",
        "media_type": result.get("media_type"),
        "url": result.get("url"),
        "message_id": message_id,
    })
    reply = text.strip() or "等距图已经放进这个会话。"
    app.sessions.update_message(session_id, message_id, text=reply, status="completed", error=None)
    app.sessions.refresh_memory(session_id)
    run.push({
        "type": EVENT_MESSAGE_COMPLETED,
        "text": reply,
        "message_id": message_id,
        "run_id": run.run_id,
    })
    return "completed"


def _error_finish(app, session_id: str, message_id: str, run: Run, code: str, message: str, text: str) -> str:
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_ERROR,
        "code": code,
        "message": message,
        "message_id": message_id,
    })
    _fail(app, session_id, message_id, run, GatewayError(code, message), text)
    return "cancelled" if code == "cancelled" else "failed"


def _push_saved(app, session_id: str, message_id: str, run: Run, event: dict) -> None:
    app.sessions.append_event(session_id, message_id, event)
    run.push(event)


def _emit_runtime(app, session_id: str, message_id: str, run: Run, entry: dict, *, persist: bool) -> None:
    kind = entry.get("kind") or entry.get("type") or "status"
    if kind not in ("model", "tool", "status"):
        kind = "status"
    event = {
        "type": EVENT_RUNTIME_STEP,
        "run_id": run.run_id,
        "message_id": message_id,
        "kind": kind,
        "name": entry.get("name") or entry.get("model") or "",
        "ms": entry.get("ms"),
        "step": entry.get("step"),
        "ok": entry.get("ok"),
        "label": entry.get("label") or "",
    }
    if persist:
        app.sessions.append_runtime(session_id, message_id, event)
    run.push(event)
    return event


def _public_progress(event: dict) -> dict | None:
    progress = {"type": EVENT_PROGRESS}
    if event.get("max"):
        progress["value"] = int(round(float(event.get("value") or 0)))
        progress["max"] = int(round(float(event["max"])))
    if event.get("percent") is not None:
        progress["percent"] = int(event["percent"])
    if "value" not in progress and "percent" not in progress:
        return None
    return progress


def _latest_user(session: dict) -> tuple[str, str, list]:
    for message in reversed(session.get("messages") or []):
        if message.get("role") != "user":
            continue
        attachments = [
            item for item in (message.get("attachments") or [])
            if isinstance(item, dict)
        ]
        if (message.get("text") or "").strip() or attachments:
            return message.get("message_id") or "", message.get("text") or "", attachments
    return "", "", []


def _has_image_attachment(attachments) -> bool:
    for item in attachments or []:
        if isinstance(item, dict) and item.get("kind") == "image":
            return True
    return False


def _image_request(text: str, attachments=None) -> bool:
    if any(marker in text for marker in _SCHEDULE_MARKERS):
        return False
    if _skill_figure(text):
        return False
    if any(marker in text for marker in _VISION_MARKERS):
        if not any(marker in text for marker in _IMAGE_MARKERS) and "comfy" not in text.lower():
            return False
    if _has_image_attachment(attachments):
        return "comfy" in text.lower() or any(marker in text for marker in _IMAGE_MARKERS)
    if "comfy" in text.lower():
        return True
    if "图片" in text and any(word in text for word in ("生成", "画")):
        return True
    return any(marker in text for marker in _IMAGE_MARKERS)


def _skill_figure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SKILL_MARKERS)


def _handle_line(handle: str) -> str:
    return f"本地内容句柄 {handle}。正文留在本机。"


def _ready_line(handle: str) -> str:
    return f"本地内容句柄 {handle} 已就绪。请调用 generate_image，content_handle 填这个句柄。不要写画面描述。"


def _kept_line(handle: str, kind: str, size: int) -> str:
    return f"本地内容句柄 {handle} 已就绪。种类 {kind}，大小 {size}。不要索取正文或文件字节。"


def _accept_args(tool_call) -> tuple[str, str]:
    purpose = "keep"
    kind = "text"
    raw = tool_call.get("arguments") if isinstance(tool_call, dict) else None
    if isinstance(raw, dict):
        if raw.get("purpose") in ("image_prompt", "keep"):
            purpose = raw["purpose"]
        if raw.get("kind") in ("text", "file"):
            kind = raw["kind"]
    return purpose, kind


def _named_handle(tool_call) -> str:
    raw = tool_call.get("arguments") if isinstance(tool_call, dict) else None
    if not isinstance(raw, dict):
        return ""
    named = raw.get("content_handle")
    return named.strip() if isinstance(named, str) else ""


def _picture_draft(text: str) -> str:
    found = None
    for marker in _DRAFT_MARKERS:
        position = text.find(marker)
        if position >= 0 and (found is None or position < found[0]):
            found = (position, marker)
    draft = text if found is None else text[found[0] + len(found[1]):]
    return draft.strip().strip("\"'“”「」『』 \n\t")


def _handle_block(app, session_id: str, tool_call) -> str:
    if not isinstance(tool_call, dict) or tool_call.get("name") != "generate_image":
        return ""
    named = _named_handle(tool_call)
    if not named:
        return ""
    info = app.sessions.content_info(session_id, named)
    if info is None:
        return "missing"
    if info.get("kind") == "file":
        return "file"
    return ""


def _refuse_file_image(app, session_id, message_id, run, tool_call) -> str:
    handle = _named_handle(tool_call)
    arguments = {"content_handle": handle} if handle else {}
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_TOOL_CALL,
        "tool": "generate_image",
        "arguments": arguments,
        "status": "failed",
        "message_id": message_id,
        "run_id": run.run_id,
    })
    current = app.sessions.message_text(session_id, message_id)
    return _error_finish(
        app, session_id, message_id, run, "invalid_tool_call", "文件内容不能用来生成图片。", current,
    )


def _bind_submitted(submitted: str, tool_call) -> dict:
    """用对话框提交的正文填 prompt。模型写来的自由文本丢掉，对不上的句柄也忽略。"""
    source = {}
    if isinstance(tool_call, dict) and tool_call.get("name") == "generate_image":
        raw = tool_call.get("arguments")
        if isinstance(raw, dict):
            source = raw
    arguments = {"prompt": submitted}
    aspect = source.get("aspect_ratio")
    arguments["aspect_ratio"] = aspect if aspect in _ASPECTS else _aspect_from_text(submitted)
    seed = source.get("seed")
    if not (isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0):
        seed = _seed_from_text(submitted)
    if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0:
        arguments["seed"] = seed
    style = source.get("style")
    if isinstance(style, str) and style in image_run.style_names():
        arguments["style"] = style
    return {"type": "tool_call", "name": "generate_image", "arguments": arguments}


def _run_bound(app, session_id: str, message_id: str, run: Run, user_text: str, tool_call, allow_current: bool) -> str:
    if _handle_block(app, session_id, tool_call) == "file":
        return _refuse_file_image(app, session_id, message_id, run, tool_call)
    bound = _bind_image(app, session_id, user_text, tool_call, allow_current)
    if bound is None:
        return _error_finish(
            app, session_id, message_id, run, "invalid_tool_call", "这个内容句柄不属于本会话。", ""
        )
    return _finish_image(app, session_id, message_id, run, "", bound)


def _bind_image(app, session_id: str, user_text: str, tool_call, allow_current: bool) -> dict | None:
    """用本机会话里的正文填 prompt。模型写来的自由文本丢掉。句柄对不上会话则不调用。"""
    source = {}
    if isinstance(tool_call, dict) and tool_call.get("name") == "generate_image":
        raw = tool_call.get("arguments")
        if isinstance(raw, dict):
            source = raw
    named = source.get("content_handle")
    named = named.strip() if isinstance(named, str) else ""
    if named:
        prompt = app.sessions.content_text(session_id, named)
        if prompt is None:
            return None
    elif allow_current:
        prompt = user_text
    else:
        return None
    arguments = {"prompt": prompt}
    aspect = source.get("aspect_ratio")
    arguments["aspect_ratio"] = aspect if aspect in _ASPECTS else _aspect_from_text(prompt)
    seed = source.get("seed")
    if not (isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0):
        seed = _seed_from_text(prompt)
    if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0:
        arguments["seed"] = seed
    style = source.get("style")
    if isinstance(style, str) and style in image_run.style_names():
        arguments["style"] = style
    return {"type": "tool_call", "name": "generate_image", "arguments": arguments}


def _aspect_from_text(text: str) -> str:
    found = []
    for aspect in _ASPECTS:
        position = text.find(aspect)
        if position >= 0:
            found.append((position, aspect))
    if not found:
        return "1:1"
    found.sort()
    return found[0][1]


def _seed_from_text(text: str) -> int | None:
    match = _SEED.search(text)
    if match is None:
        return None
    return int(match.group(1))


def _turn_messages(app, session_id: str, session: dict, **kwargs) -> list[dict]:
    return _model_messages(session, extra_bodies=app.sessions.secret_pairs(session_id), **kwargs)


def _model_messages(
    session: dict,
    replace_user_id: str | None = None,
    task: str | None = None,
    extra: list | None = None,
    extra_bodies: list | None = None,
) -> list[dict]:
    handles = {}
    bodies = []
    for item in session.get("contents") or []:
        if not isinstance(item, dict):
            continue
        handle = item.get("handle")
        text = item.get("text")
        if item.get("message_id") and isinstance(handle, str):
            handles[item["message_id"]] = handle
        if isinstance(text, str) and len(text) >= 8 and isinstance(handle, str):
            bodies.append((text, handle))
    for text, handle in extra_bodies or []:
        if isinstance(text, str) and len(text) >= 8 and isinstance(handle, str):
            bodies.append((text, handle))
    bodies.sort(key=lambda pair: len(pair[0]), reverse=True)
    items = []
    for message in session["messages"]:
        if message.get("status") == "running":
            continue
        if message.get("role") not in ("user", "assistant"):
            continue
        text = message.get("text") or ""
        message_id = message.get("message_id")
        if replace_user_id and message_id == replace_user_id and task:
            text = task
        elif message.get("role") == "user" and message_id in handles:
            text = _handle_line(handles[message_id])
        else:
            if message.get("role") == "assistant" and not text.strip():
                error = message.get("error") or {}
                text = error.get("message") or ""
            text = _cover_bodies(text, bodies)
        attachments = [
            item for item in (message.get("attachments") or [])
            if isinstance(item, dict) and isinstance(item.get("handle"), str)
        ]
        if not text.strip() and not attachments:
            continue
        packed = {"role": message["role"], "content": (text[:MAX_MESSAGE] if text else "")}
        if attachments and message.get("role") == "user":
            packed["attachments"] = attachments
        items.append(packed)
    keep = store.MEMORY_KEEP
    if len(items) > keep:
        items = items[-keep:]
    while items and sum(len(item["content"]) for item in items) > MAX_TOTAL:
        items.pop(0)
    if extra:
        items.extend(extra)
    system = SYSTEM
    memory = session.get("memory") if isinstance(session.get("memory"), dict) else {}
    summary = memory.get("summary") if isinstance(memory.get("summary"), str) else ""
    refs = memory.get("refs") if isinstance(memory.get("refs"), list) else []
    if summary.strip() or refs:
        system += " 会话记忆：" + summary.strip()
        named = [item for item in refs if isinstance(item, str) and item]
        if named:
            system += " 引用：" + " ".join(named)
    system += catalog_suffix()
    return [{"role": "system", "content": system}, *items]


def _cover_bodies(text: str, bodies: list[tuple[str, str]]) -> str:
    for body, handle in bodies:
        if body in text:
            return _handle_line(handle)
    return text


def _session_artifacts(app, session_id: str, session: dict) -> dict:
    found = {}
    for message in session.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for item in message.get("attachments") or []:
            if not isinstance(item, dict) or item.get("kind") != "image":
                continue
            handle = item.get("handle")
            if not isinstance(handle, str) or handle in found:
                continue
            data = app.sessions.attachment_bytes(session_id, handle)
            if data:
                found[handle] = data
    return found


def _public_args(arguments: dict) -> dict:
    public = {}
    for key, value in arguments.items():
        label = str(key).lower()
        if any(word in label for word in ("key", "secret", "password", "token")):
            public[key] = "…"
            continue
        if isinstance(value, str) and len(value) > 80:
            public[key] = value[:80] + "…"
            continue
        public[key] = value
    return public


def _log(session_id: str, model: str, status: str, elapsed: float) -> None:
    print(f"chat {session_id} {model} {status} {elapsed:.1f}s", file=sys.stderr)


