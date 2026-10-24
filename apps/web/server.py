"""本机聊天。只监听 127.0.0.1。

页面和接口在同一个进程里。密钥留在数据目录，不进响应。
文生图走 poc 里现成的工具。图片按 artifact_id 从本机会话里取。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(WEB_DIR, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if WEB_DIR not in sys.path:
    sys.path.insert(0, WEB_DIR)

import chat_run
import image_run
import store
import tool_config
from packages.artifacts import ArtifactStore
from services import telemetry

_SESSION_MESSAGES = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/messages$")
_SESSION_STOP = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/stop$")
_SESSION_ATTACHMENTS = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/attachments$")
_SESSION_ATTACHMENT = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/attachments/(a_[0-9a-f]{32})$")
_SESSION_CONTENT = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/contents/(c_[0-9a-f]{32})$")
_SESSION = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})$")
_RUN_EVENTS = re.compile(r"^/api/runs/(r_[0-9a-f]{32})/events$")
_RUN_CONTENT = re.compile(r"^/api/runs/(r_[0-9a-f]{32})/content$")
_RUN_CONFIRM = re.compile(r"^/api/runs/(r_[0-9a-f]{32})/confirm$")
_ARTIFACT = re.compile(r"^/sessions/(s_[0-9a-f]{32})/artifacts/(art_[0-9a-f]{32})$")
_STATIC = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
_MAX_BODY = 1_000_000
_MAX_ATTACHMENT_BODY = 12_000_000


class App:
    def __init__(self, data_dir: str, timeout_sec: float = 120.0):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        telemetry.configure(os.path.join(data_dir, "telemetry.jsonl"))
        self.timeout_sec = timeout_sec
        self.provider = store.ProviderStore(os.path.join(data_dir, "provider.json"))
        self.tools = tool_config.ToolConfigStore(os.path.join(data_dir, "tools.json"))
        tool_config.install(self.tools)
        self.identity = store.IdentityStore(os.path.join(data_dir, "identity.json"))
        self.artifacts = ArtifactStore(os.path.join(data_dir, "artifacts"))
        self.sessions = store.SessionStore(os.path.join(data_dir, "sessions"), self.artifacts)
        self.runs = chat_run.RunHub()


def bind_host(host: str) -> str:
    if host != "127.0.0.1":
        raise SystemExit("聊天服务只监听 127.0.0.1。")
    return host


class QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        print(f"chat http {type(exc).__name__}", file=sys.stderr)


def make_httpd(app: App, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    bind_host(host)
    handler = _handler(app)
    httpd = QuietHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="本机文字聊天")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("CHAT_DATA_DIR") or os.path.join(WEB_DIR, "data"),
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    app = App(args.data_dir, timeout_sec=args.timeout)
    httpd = make_httpd(app, "127.0.0.1", args.port)
    print(f"聊天页面 http://127.0.0.1:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("已停止。", flush=True)
    finally:
        httpd.server_close()


def _handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            _dispatch(self, app, "GET")

        def do_POST(self) -> None:
            _dispatch(self, app, "POST")

        def do_PUT(self) -> None:
            _dispatch(self, app, "PUT")

    return Handler


def _dispatch(handler: BaseHTTPRequestHandler, app: App, method: str) -> None:
    try:
        parsed = urllib.parse.urlparse(handler.path)
        path = parsed.path
        if method == "GET" and path == "/":
            _send_file(handler, os.path.join(WEB_DIR, "static", "index.html"), "text/html; charset=utf-8")
            return
        if method == "GET" and path.startswith("/static/"):
            name = path[len("/static/"):]
            if name not in _STATIC:
                _send_json(handler, 404, {"code": "not_found", "message": "找不到。"})
                return
            _send_file(handler, os.path.join(WEB_DIR, "static", name), _STATIC[name])
            return
        if method == "GET" and (path == "/api/telemetry" or path.startswith("/api/telemetry/")):
            query = urllib.parse.parse_qs(parsed.query)
            trace_id = path[len("/api/telemetry/"):] if path.startswith("/api/telemetry/") else query.get("trace_id", [""])[0]
            if trace_id and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", trace_id):
                _send_json(handler, 400, {"code": "invalid_trace_id", "message": "trace_id 不合法。"})
                return
            try:
                limit = max(1, min(5000, int(query.get("limit", ["500"])[0])))
            except ValueError:
                limit = 500
            events = telemetry.read(trace_id or None, limit)
            _send_json(handler, 200, {"trace_id": trace_id or None, "events": events, "summary": telemetry.summary(events)})
            return
        if path == "/api/identity" and method == "GET":
            _send_json(handler, 200, app.identity.current())
            return
        if path == "/api/provider" and method == "GET":
            _send_json(handler, 200, app.provider.public())
            return
        if path == "/api/provider" and method == "PUT":
            body = _read_json(handler)
            try:
                view = app.provider.update(
                    base_url=body.get("base_url", ""),
                    model=body.get("model", ""),
                    api_key=body.get("api_key"),
                    clear_key=bool(body.get("clear_key")),
                )
            except ValueError as exc:
                raise chat_run.RequestError(400, "invalid_provider", str(exc)) from exc
            _send_json(handler, 200, view)
            return
        if path == "/api/tools" and method == "GET":
            _send_json(handler, 200, app.tools.listing())
            return
        if path.startswith("/api/tools/"):
            parts = path[len("/api/tools/"):].split("/")
            name = urllib.parse.unquote(parts[0]) if parts else ""
            if name not in tool_config.declarations():
                _send_json(handler, 404, {"code": "not_found", "message": "没有这个工具。"})
                return
            if parts[1:] == ["config"] and method == "GET":
                _send_json(handler, 200, app.tools.public(name))
                return
            if parts[1:] == ["config"] and method == "PUT":
                body = _read_json(handler)
                try:
                    view = app.tools.update(
                        name,
                        body.get("values") or {},
                        reset=bool(body.get("reset")),
                        clear=body.get("clear") if isinstance(body.get("clear"), list) else None,
                    )
                except tool_config.ConfigError as exc:
                    _send_json(handler, 400, {"code": "invalid_tool_config", "message": str(exc), "errors": exc.errors})
                    return
                _send_json(handler, 200, view)
                return
            if parts[1:] == ["test"] and method == "POST":
                _send_json(handler, 200, tool_config.test_connection(app.tools, name))
                return
            _send_json(handler, 404, {"code": "not_found", "message": "找不到。"})
            return
        if path == "/api/sessions" and method == "GET":
            user = app.identity.current()
            _send_json(handler, 200, {"sessions": app.sessions.list_sessions(user["user_id"])})
            return
        if path == "/api/sessions" and method == "POST":
            _read_json(handler)
            user = app.identity.current()
            _send_json(handler, 200, app.sessions.create(user["user_id"]))
            return
        matched = _SESSION_MESSAGES.fullmatch(path)
        if matched and method == "POST":
            session = _owned_session(app, matched.group(1))
            body = _read_json(handler)
            run = chat_run.start(app, session["session_id"], body.get("text"), body.get("attachments"))
            _send_sse(handler, run, 0)
            return
        matched = _SESSION_STOP.fullmatch(path)
        if matched and method == "POST":
            session = _owned_session(app, matched.group(1))
            _read_json(handler)
            _send_json(handler, 200, chat_run.stop(app, session["session_id"]))
            return
        matched = _SESSION_ATTACHMENTS.fullmatch(path)
        if matched and method == "POST":
            session = _owned_session(app, matched.group(1))
            body = _read_json(handler, _MAX_ATTACHMENT_BODY)
            record = chat_run.add_attachment(
                app, session["session_id"], body.get("file"), body.get("media_type"), body.get("name") or "",
            )
            _send_json(handler, 200, record)
            return
        matched = _SESSION_ATTACHMENT.fullmatch(path)
        if matched and method == "GET":
            session = _owned_session(app, matched.group(1))
            info = app.sessions.attachment_info(session["session_id"], matched.group(2))
            data = app.sessions.attachment_bytes(session["session_id"], matched.group(2))
            if info is None or data is None:
                _send_json(handler, 404, {"code": "not_found", "message": "找不到这个附件。"})
                return
            _send_bytes(handler, data, info.get("media_type") or "application/octet-stream")
            return
        matched = _SESSION_CONTENT.fullmatch(path)
        if matched and method == "GET":
            session = _owned_session(app, matched.group(1))
            info = app.sessions.content_info(session["session_id"], matched.group(2))
            data = app.sessions.content_bytes(session["session_id"], matched.group(2))
            if info is None or data is None:
                _send_json(handler, 404, {"code": "not_found", "message": "找不到这个内容。"})
                return
            _send_bytes(handler, data, info.get("media_type") or "application/octet-stream")
            return
        matched = _SESSION.fullmatch(path)
        if matched and method == "GET":
            session = _owned_session(app, matched.group(1))
            _send_json(handler, 200, app.sessions.view(session))
            return
        matched = _RUN_CONTENT.fullmatch(path)
        if matched and method == "POST":
            body = _read_json(handler, _MAX_ATTACHMENT_BODY)
            if body.get("skip"):
                result = chat_run.skip_content(app, matched.group(1))
            elif body.get("file") is not None:
                result = chat_run.submit_file(
                    app, matched.group(1), body.get("file"), body.get("media_type"),
                )
            else:
                result = chat_run.submit_content(app, matched.group(1), body.get("text"))
            _send_json(handler, 200, result)
            return
        matched = _RUN_CONFIRM.fullmatch(path)
        if matched and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, chat_run.submit_confirm(app, matched.group(1), body.get("allowed")))
            return
        matched = _RUN_EVENTS.fullmatch(path)
        if matched and method == "GET":
            run = app.runs.get(matched.group(1))
            if run is None:
                _send_json(handler, 404, {"code": "not_found", "message": "找不到这次运行。"})
                return
            query = urllib.parse.parse_qs(parsed.query)
            after = _after(query.get("after", ["0"])[0])
            _send_sse(handler, run, after)
            return
        matched = _ARTIFACT.fullmatch(path)
        if matched and method == "GET":
            _send_artifact(handler, matched.group(1), matched.group(2))
            return
        _send_json(handler, 404, {"code": "not_found", "message": "找不到。"})
    except chat_run.RequestError as exc:
        _send_json(handler, exc.status, {"code": exc.code, "message": exc.message})
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        return
    except Exception as exc:
        print(f"chat http {type(exc).__name__}", file=sys.stderr)
        try:
            _send_json(handler, 500, {"code": "provider_error", "message": "本机服务出错了。"})
        except OSError:
            return


def _after(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _owned_session(app: App, session_id: str) -> dict:
    try:
        session = app.sessions.get(session_id)
    except store.StoreError as exc:
        raise chat_run.RequestError(404, "not_found", "找不到这个会话。") from exc
    user = app.identity.current()
    if not app.sessions.belongs_to(session, user["user_id"]):
        raise chat_run.RequestError(404, "not_found", "找不到这个会话。")
    return session


def _read_json(handler: BaseHTTPRequestHandler, max_body: int = _MAX_BODY) -> dict:
    content_type = handler.headers.get("Content-Type", "")
    if "application/json" not in content_type:
        raise chat_run.RequestError(415, "invalid_json", "请求需要 JSON。")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError as exc:
        raise chat_run.RequestError(400, "invalid_json", "请求不是 JSON。") from exc
    if length < 0 or length > max_body:
        raise chat_run.RequestError(413, "payload_too_large", "这条请求太长了。")
    raw = handler.rfile.read(length) if length else b""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise chat_run.RequestError(400, "invalid_json", "请求不是 JSON。") from exc
    if not isinstance(data, dict):
        raise chat_run.RequestError(400, "invalid_json", "请求不是 JSON 对象。")
    return data


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(raw)


def _send_file(handler: BaseHTTPRequestHandler, path: str, content_type: str) -> None:
    with open(path, "rb") as handle:
        raw = handle.read()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Content-Type-Options", "nosniff")
    if content_type.startswith("text/html"):
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'",
        )
        handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    handler.wfile.write(raw)


def _send_bytes(handler: BaseHTTPRequestHandler, raw: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(raw)


def _send_artifact(handler: BaseHTTPRequestHandler, session_id: str, artifact_id: str) -> None:
    path, media = image_run.open_artifact(session_id, artifact_id)
    if not path:
        _send_json(handler, 404, {"code": "not_found", "message": "找不到这张图片。"})
        return
    with open(path, "rb") as handle:
        raw = handle.read()
    handler.send_response(200)
    handler.send_header("Content-Type", media)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(raw)


def _send_sse(handler: BaseHTTPRequestHandler, run: chat_run.Run, after: int) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    try:
        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        for event in run.listen(after):
            block = (
                f"event: {event['type']}\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
            _chunk(handler, block)
        _chunk(handler, b"")
    except OSError:
        return


def _chunk(handler: BaseHTTPRequestHandler, block: bytes) -> None:
    handler.wfile.write(f"{len(block):x}\r\n".encode("ascii"))
    handler.wfile.write(block)
    handler.wfile.write(b"\r\n")
    handler.wfile.flush()


if __name__ == "__main__":
    main()
