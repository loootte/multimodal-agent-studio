"""一次回答。纯文字不调用 ComfyUI。

要图时，第三方只调用 accept_local_content。对话框收下的正文留在本机会话，
再注入到已有的 generate_image。模型写来的画面描述丢掉。
页面断开后仍写完这一次。停止只置位，并取消已经提交的那一次出图，
不从这条线程关模型连接。
"""

from __future__ import annotations

import re
import sys
import threading
import time

import image_run
from packages.contracts.events import (
    EVENT_ARTIFACT,
    EVENT_CONTENT_REQUEST,
    EVENT_ERROR,
    EVENT_MESSAGE_COMPLETED,
    EVENT_MESSAGE_DELTA,
    EVENT_PROGRESS,
    EVENT_RUN_FAILED,
    EVENT_RUN_STARTED,
    EVENT_TOOL_CALL,
    HUMAN,
    TERMINAL_EVENTS,
)
from services.gateway.openai_chat import GatewayError, stream_chat
from store import StoreError, new_id

SYSTEM = (
    "你是一个有帮助的助手。用用户使用的语言回答。"
    "本地文字由 accept_local_content 收集，正文留在本机。不要复述、改写或索取正文。"
    "接到出图任务时，先调用 accept_local_content，purpose 填 image_prompt。"
    "用户提交后你只会收到句柄。再用这个 content_handle 调用 generate_image。"
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
MAX_INPUT = 32000
MAX_MESSAGE = 16000
MAX_TURNS = 40
MAX_TOTAL = 100000


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
        self._content_value: str | None = None
        self.waiting_content = False

    def begin_content_wait(self) -> None:
        self._content_ready.clear()
        with self._content_lock:
            self._content_value = None
            self.waiting_content = True

    def submit_content(self, text: str) -> bool:
        with self._content_lock:
            if not self.waiting_content or self.cancel.is_set():
                return False
            self._content_value = text
            self.waiting_content = False
        self._content_ready.set()
        return True

    def wait_content(self, timeout: float = _CONTENT_WAIT_SEC) -> str | None:
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

    def note_comfy(self, prompt_id: str, base: str) -> None:
        with self._state_lock:
            if self.prompt_id is None:
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
    if not text:
        raise RequestError(400, "empty", "请输入文字。")
    if len(text) > MAX_INPUT:
        raise RequestError(400, "payload_too_large", HUMAN["payload_too_large"])
    if attachments:
        problem = GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
    else:
        problem = None
    message_id = new_id("m_")
    run = Run(session_id, message_id)
    try:
        assistant = app.sessions.append_turn(session_id, text, run.run_id)
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
    if problem is None and not provider["api_key"]:
        problem = GatewayError("missing_key", HUMAN["missing_key"])
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
    run = app.runs.get(run_id)
    if run is None or not run.submit_content(text):
        raise RequestError(409, "not_waiting", "现在没有在等待本地内容。")
    return {"ok": True}


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
        user_id, user_text = _latest_user(session)
        image_request = _image_request(user_text)
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
    messages = _model_messages(session, replace_user_id=user_id, task=_TASK_LINE)
    try:
        tool_call, _text = _ask(app, session_id, message_id, run, provider, messages, True)
    except GatewayError as exc:
        if _blocks(exc, run):
            return _fail_status(app, session_id, message_id, run, exc)
        tool_call = None
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    if _unresolved_handle(app, session_id, tool_call):
        return _error_finish(
            app, session_id, message_id, run, "invalid_tool_call", "这个内容句柄不属于本会话。", ""
        )
    return _continue_after_content(
        app, session_id, message_id, run, provider, user_id, user_text, True, _picture_draft(user_text)
    )


def _generate_text_turn(app, session_id, message_id, run, provider, session, user_text) -> str:
    try:
        tool_call, text = _ask(app, session_id, message_id, run, provider, _model_messages(session), False)
    except GatewayError as exc:
        return _fail_status(app, session_id, message_id, run, exc)
    if tool_call is None:
        _complete(app, session_id, message_id, run, text)
        return "completed"
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    name = tool_call.get("name") if isinstance(tool_call, dict) else ""
    if name == "accept_local_content":
        return _continue_after_content(
            app, session_id, message_id, run, provider, "", user_text, False, ""
        )
    if name == "generate_image":
        return _run_bound(app, session_id, message_id, run, user_text, tool_call, False)
    return _finish_image(app, session_id, message_id, run, text, tool_call)


def _continue_after_content(app, session_id, message_id, run, provider, user_id, user_text, image, draft) -> str:
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    run.begin_content_wait()
    _push_saved(app, session_id, message_id, run, {
        "type": EVENT_CONTENT_REQUEST,
        "message_id": message_id,
        "run_id": run.run_id,
        "purpose": "image_prompt",
        "hint": "请输入要留在本机的内容。",
        "draft": draft,
    })
    submitted = run.wait_content()
    if run.cancel.is_set():
        return _fail_status(app, session_id, message_id, run, GatewayError("cancelled", HUMAN["cancelled"]))
    if not submitted:
        current = app.sessions.message_text(session_id, message_id)
        return _error_finish(app, session_id, message_id, run, "timeout", "没有收到要留在本机的内容。", current)
    try:
        record = app.sessions.add_content(session_id, submitted)
    except StoreError:
        current = app.sessions.message_text(session_id, message_id)
        return _error_finish(app, session_id, message_id, run, "provider_error", HUMAN["provider_error"], current)
    handle = record.get("handle") or ""
    print(f"content {session_id} {handle} text {record.get('size')}", file=sys.stderr)
    session = app.sessions.get(session_id)
    messages = _model_messages(
        session,
        replace_user_id=user_id if image else None,
        task=_TASK_LINE if image else None,
        extra=[{"role": "user", "content": _ready_line(handle)}],
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
    if image or (isinstance(tool_call, dict) and tool_call.get("name") == "generate_image"):
        bound = _bind_submitted(submitted, tool_call)
        return _finish_image(app, session_id, message_id, run, "" if image else reply, bound)
    _complete(app, session_id, message_id, run, reply)
    return "completed"


def _ask(app, session_id, message_id, run, provider, messages, hide_text: bool) -> tuple[dict | None, str]:
    parts: list[str] = []
    tool_call = None
    for piece in stream_chat(
        base_url=provider["base_url"],
        api_key=provider["api_key"],
        model=provider["model"],
        messages=messages,
        timeout_sec=app.timeout_sec,
        cancel=run.cancel,
        tools=image_run.image_tools(),
        local_bodies=app.sessions.local_bodies(session_id),
    ):
        if isinstance(piece, str):
            if hide_text:
                continue
            parts.append(piece)
            app.sessions.update_message(
                session_id, message_id, text="".join(parts), status="running", error=None
            )
            run.push({"type": EVENT_MESSAGE_DELTA, "text": piece})
        elif isinstance(piece, dict) and piece.get("type") == "tool_call" and tool_call is None:
            tool_call = piece
    return tool_call, "".join(parts)


def _blocks(exc: GatewayError, run: Run) -> bool:
    return exc.code == "local_content" or exc.code == "cancelled" or run.cancel.is_set()


def _fail_status(app, session_id, message_id, run, exc: GatewayError) -> str:
    current = app.sessions.message_text(session_id, message_id)
    _fail(app, session_id, message_id, run, exc, current)
    return "cancelled" if exc.code == "cancelled" else "failed"


def _complete(app, session_id, message_id, run, text: str) -> None:
    app.sessions.update_message(session_id, message_id, text=text, status="completed", error=None)
    run.push({
        "type": EVENT_MESSAGE_COMPLETED,
        "text": text,
        "message_id": message_id,
        "run_id": run.run_id,
    })


def _fail(app, session_id: str, message_id: str, run: Run, error: GatewayError, text: str) -> None:
    state = "cancelled" if error.code == "cancelled" else "failed"
    app.sessions.update_message(
        session_id,
        message_id,
        text=text,
        status=state,
        error={"code": error.code, "message": error.message},
    )
    run.push({
        "type": EVENT_RUN_FAILED,
        "code": error.code,
        "message": error.message,
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
        "message_id": message_id,
    })
    if run.cancel.is_set():
        return _error_finish(app, session_id, message_id, run, "cancelled", HUMAN["cancelled"], text)
    if name != "generate_image":
        return _error_finish(
            app, session_id, message_id, run, "invalid_tool_call", "这一版只能生成图片。", text
        )
    base = image_run.comfy_base()

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

    result = image_run.generate(session_id, arguments, on_event)
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
    artifact = {
        "type": EVENT_ARTIFACT,
        "artifact_id": result.get("artifact_id"),
        "media": result.get("type") or "image",
        "media_type": result.get("media_type"),
        "width": result.get("width"),
        "height": result.get("height"),
        "url": result.get("url"),
        "message_id": message_id,
    }
    _push_saved(app, session_id, message_id, run, artifact)
    app.sessions.update_message(session_id, message_id, text=text, status="completed", error=None)
    run.push({
        "type": EVENT_MESSAGE_COMPLETED,
        "text": text,
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


def _latest_user(session: dict) -> tuple[str, str]:
    for message in reversed(session.get("messages") or []):
        if message.get("role") == "user" and (message.get("text") or "").strip():
            return message.get("message_id") or "", message.get("text") or ""
    return "", ""


def _image_request(text: str) -> bool:
    if "comfy" in text.lower():
        return True
    if "图片" in text and any(word in text for word in ("提示词", "生成", "画")):
        return True
    return any(marker in text for marker in _IMAGE_MARKERS)


def _handle_line(handle: str) -> str:
    return f"本地内容句柄 {handle}。正文留在本机。"


def _ready_line(handle: str) -> str:
    return f"本地内容句柄 {handle} 已就绪。请调用 generate_image，content_handle 填这个句柄。不要写画面描述。"


def _picture_draft(text: str) -> str:
    found = None
    for marker in _DRAFT_MARKERS:
        position = text.find(marker)
        if position >= 0 and (found is None or position < found[0]):
            found = (position, marker)
    draft = text if found is None else text[found[0] + len(found[1]):]
    return draft.strip().strip("\"'“”「」『』 \n\t")


def _unresolved_handle(app, session_id: str, tool_call) -> bool:
    if not isinstance(tool_call, dict) or tool_call.get("name") != "generate_image":
        return False
    raw = tool_call.get("arguments")
    if not isinstance(raw, dict):
        return False
    named = raw.get("content_handle")
    named = named.strip() if isinstance(named, str) else ""
    if not named:
        return False
    return app.sessions.content_text(session_id, named) is None


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


def _model_messages(session: dict, replace_user_id: str | None = None, task: str | None = None, extra: list | None = None) -> list[dict]:
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
        if not text.strip():
            continue
        items.append({"role": message["role"], "content": text[:MAX_MESSAGE]})
    if len(items) > MAX_TURNS:
        items = items[-MAX_TURNS:]
    while items and sum(len(item["content"]) for item in items) > MAX_TOTAL:
        items.pop(0)
    if extra:
        items.extend(extra)
    return [{"role": "system", "content": SYSTEM}, *items]


def _cover_bodies(text: str, bodies: list[tuple[str, str]]) -> str:
    for body, handle in bodies:
        if body in text:
            return _handle_line(handle)
    return text


def _log(session_id: str, model: str, status: str, elapsed: float) -> None:
    print(f"chat {session_id} {model} {status} {elapsed:.1f}s", file=sys.stderr)


