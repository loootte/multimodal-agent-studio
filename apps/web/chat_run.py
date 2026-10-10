"""一次文字回答。页面断开后仍写完这一次，停止才会切断模型连接。"""

from __future__ import annotations

import sys
import threading
import time

from packages.contracts.events import (
    EVENT_MESSAGE_COMPLETED,
    EVENT_MESSAGE_DELTA,
    EVENT_RUN_FAILED,
    EVENT_RUN_STARTED,
    HUMAN,
    TERMINAL_EVENTS,
)
from services.gateway.openai_chat import GatewayError, stream_chat
from store import StoreError, new_id

SYSTEM = "你是一个有帮助的助手。用用户使用的语言回答。"
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

    def stop(self) -> None:
        # 不从这条线程关套接字。Windows 上那样会和 readline 卡住。
        self.cancel.set()

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
        parts: list[str] = []
        session = app.sessions.get(session_id)
        for delta in stream_chat(
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
            messages=_model_messages(session),
            timeout_sec=app.timeout_sec,
            cancel=run.cancel,
        ):
            parts.append(delta)
            current = "".join(parts)
            app.sessions.update_message(
                session_id, message_id, text=current, status="running", error=None
            )
            run.push({"type": EVENT_MESSAGE_DELTA, "text": delta})
        final = "".join(parts)
        app.sessions.update_message(
            session_id, message_id, text=final, status="completed", error=None
        )
        run.push({
            "type": EVENT_MESSAGE_COMPLETED,
            "text": final,
            "message_id": message_id,
            "run_id": run.run_id,
        })
        status = "completed"
    except GatewayError as exc:
        current = app.sessions.message_text(session_id, message_id)
        _fail(app, session_id, message_id, run, exc, current)
        status = "cancelled" if exc.code == "cancelled" else "failed"
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


def _model_messages(session: dict) -> list[dict]:
    items = []
    for message in session["messages"]:
        if message.get("status") == "running":
            continue
        if message.get("role") not in ("user", "assistant"):
            continue
        text = message.get("text") or ""
        if message.get("role") == "assistant" and not text.strip():
            error = message.get("error") or {}
            text = error.get("message") or ""
        if not text.strip():
            continue
        items.append({"role": message["role"], "content": text[:MAX_MESSAGE]})
    if len(items) > MAX_TURNS:
        items = items[-MAX_TURNS:]
    while items and sum(len(item["content"]) for item in items) > MAX_TOTAL:
        items.pop(0)
    return [{"role": "system", "content": SYSTEM}, *items]


def _log(session_id: str, model: str, status: str, elapsed: float) -> None:
    print(f"chat {session_id} {model} {status} {elapsed:.1f}s", file=sys.stderr)


