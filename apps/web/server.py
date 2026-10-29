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
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(WEB_DIR, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if WEB_DIR not in sys.path:
    sys.path.insert(0, WEB_DIR)

import character
import chat_run
import image_run
import resources
import store
import tool_config
from packages.artifacts import ArtifactStore
from packages.artifacts import crypto as storage_crypto
from services import telemetry
from services.gateway import egress

_SESSION_MESSAGES = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/messages$")
_SESSION_CHARACTER = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/character$")
_SESSION_STOP = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/stop$")
_SESSION_ATTACHMENTS = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/attachments$")
_SESSION_ATTACHMENT = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/attachments/(a_[0-9a-f]{32})$")
_SESSION_CONTENT = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/contents/(c_[0-9a-f]{32})$")
_SESSION_DELETE = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})/delete$")
_SESSION = re.compile(r"^/api/sessions/(s_[0-9a-f]{32})$")
_RUN_EVENTS = re.compile(r"^/api/runs/(r_[0-9a-f]{32})/events$")
_RUN_CONTENT = re.compile(r"^/api/runs/(r_[0-9a-f]{32})/content$")
_RUN_CONFIRM = re.compile(r"^/api/runs/(r_[0-9a-f]{32})/confirm$")
_ARTIFACT = re.compile(r"^/sessions/(s_[0-9a-f]{32})/artifacts/(art_[0-9a-f]{32})$")
_STATIC = {
    "character.js": "text/javascript; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "markdown.js": "text/javascript; charset=utf-8",
    "resources.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
_MAX_BODY = 1_000_000
_MAX_ATTACHMENT_BODY = 12_000_000


class App:
    def __init__(self, data_dir: str, timeout_sec: float = 120.0, storage=None):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        telemetry.configure(os.path.join(data_dir, "telemetry.jsonl"))
        egress.configure(os.path.join(data_dir, "egress.jsonl"))
        self.timeout_sec = timeout_sec
        self.provider = store.ProviderStore(os.path.join(data_dir, "provider.json"))
        self.tools = tool_config.ToolConfigStore(os.path.join(data_dir, "tools.json"))
        tool_config.install(self.tools)
        self.identity = store.IdentityStore(os.path.join(data_dir, "identity.json"))
        self.artifacts = ArtifactStore(os.path.join(data_dir, "artifacts"))
        # 12.F（#62）：本地存储加密。打不开（缺密钥、密钥不对、要先迁移）就抛 StorageError，不悄悄退回明文。
        self.storage = storage if storage is not None else storage_crypto.open_storage(data_dir)
        if self.storage.created_key:
            telemetry.record("security.storage_key_created", trace_id="sto_" + uuid.uuid4().hex,
                             key_id=self.storage.key_id, key_source=self.storage.key_source)
        self.sessions = store.SessionStore(os.path.join(data_dir, "sessions"), self.artifacts, storage=self.storage)
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
    try:
        app = App(args.data_dir, timeout_sec=args.timeout)
    except storage_crypto.StorageError as exc:
        raise SystemExit(f"本地存储打不开（{exc.code}）：{exc.message}\n见 docs/storage-encryption.md。")
    if app.storage.created_key:
        print("已生成本地存储主密钥：" + storage_crypto.default_key_path()
              + "\n请马上备份（python apps/web/storage_tool.py export-key），丢了密钥数据无法恢复。", flush=True)
    elif app.storage.mode == "memory":
        print("仅内存会话：会话和文件不落盘，停止服务就没了。", flush=True)
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


def _trajectories(handler, app, query) -> None:
    """只读、仅本机：先增量导出，再按工具、Agent、技能、结局、时间查询（ADR 0006）。"""
    host = (handler.client_address or ("",))[0]
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        _send_json(handler, 403, {"code": "forbidden", "message": "轨迹只给本机看。"})
        return
    from services.meta.export import Exporter

    def one(name):
        value = (query.get(name) or [""])[0].strip()
        return value[:80] or None

    for name in ("tool", "agent", "skill", "outcome", "mode", "session"):
        value = one(name)
        if value and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
            _send_json(handler, 400, {"code": "invalid_query", "message": f"{name} 不合法。"})
            return
    try:
        limit = max(1, min(1000, int(one("limit") or 100)))
    except ValueError:
        limit = 100
    exporter = Exporter(app.data_dir, sessions=app.sessions)
    stats = exporter.export()
    items = exporter.query(tool=one("tool"), agent=one("agent"), skill=one("skill"), outcome=one("outcome"),
                           mode=one("mode"), session=one("session"), since=one("since"), until=one("until"),
                           limit=limit, full=one("brief") is None)
    _send_json(handler, 200, {"schema": "mas.trajectory/1", "export": stats, "trajectories": items})


def _audit_policy_change(tool: str, change: dict) -> None:
    """12.E：受保护配置项（如 remote_local_prompt）每次变更记一条遥测安全事件。只有名字和前后取值。"""
    try:
        telemetry.record(
            "security.policy_changed", trace_id="cfg_" + uuid.uuid4().hex,
            tool=tool, field=str(change.get("field") or ""),
            from_value=str(change.get("from")), to_value=str(change.get("to")), via="web",
        )
    except Exception as exc:  # 审计失败不回滚配置，但要看得见
        print(f"警告：配置变更审计写入失败 {type(exc).__name__}", file=sys.stderr)


def _storage_route(handler, app) -> None:
    """本地存储状态（12.F #62）：模式、算法、key_id、密钥来源、是否还有明文残留。
    不给密钥、不给完整路径；只给本机。导入 / 导出密钥只走命令行，不走 HTTP。"""
    host = (handler.client_address or ("",))[0]
    if host not in resources.LOOPBACK:
        _send_json(handler, 403, {"code": "forbidden", "message": "存储状态只给本机看。"})
        return
    body = app.storage.public()
    body["plaintext_left"] = (storage_crypto.plaintext_items(app.data_dir)
                              if app.storage.mode == "encrypted" else None)
    body["decrypt_failures"] = app.sessions.decrypt_failures
    body["crypto_available"] = storage_crypto.crypto_available()
    _send_json(handler, 200, body)


def _dataflow_route(handler, app, query) -> None:
    """会话级数据流图（12.E #61）：本地内容 → 句柄 → 工具 →（本机 ComfyUI | 第三方）。
    只读出站审计和句柄公开字段拼出来；和出站审计同一道回环检查。"""
    import dataflow

    host = (handler.client_address or ("",))[0]
    if host not in resources.LOOPBACK:
        _send_json(handler, 403, {"code": "forbidden", "message": "数据流图只给本机看。"})
        return
    session_id = ((query.get("session_id") or [""])[0]).strip()
    if not re.fullmatch(r"s_[0-9a-f]{32}", session_id):
        _send_json(handler, 400, {"code": "invalid_session_id", "message": "session_id 不合法。"})
        return
    try:
        contents = app.sessions.list_content(session_id)
    except store.StoreError:
        _send_json(handler, 404, {"code": "not_found", "message": "找不到这个会话。"})
        return
    _send_json(handler, 200, dataflow.build(session_id, egress.read(session_id, 5000), contents))


def _egress_route(handler, path, query) -> None:
    """出站审计（#57）：只读、仅本机，和本地资源面板同一道回环检查。记录里本来就没有正文、提示词、密钥和路径。"""
    host = (handler.client_address or ("",))[0]
    if host not in resources.LOOPBACK:
        _send_json(handler, 403, {"code": "forbidden", "message": "出站审计只给本机看。"})
        return
    session_id = ((query.get("session_id") or [""])[0]).strip()
    if session_id and not re.fullmatch(r"s_[0-9a-f]{32}", session_id):
        _send_json(handler, 400, {"code": "invalid_session_id", "message": "session_id 不合法。"})
        return
    try:
        limit = max(1, min(5000, int((query.get("limit") or ["2000"])[0])))
    except ValueError:
        limit = 2000
    items = egress.read(session_id or None, limit)
    payload = {
        "schema": "mas.egress.export/1",
        "session_id": session_id or None,
        "exported_at": round(time.time(), 3),
        "events": items,
        "summary": egress.summary(items),
        "write_failures": egress.failures().get("write", 0),
    }
    if path == "/api/egress":
        _send_json(handler, 200, payload)
        return
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    name = f"egress-{session_id or 'all'}.json"
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Disposition", f'attachment; filename="{name}"')
    handler.end_headers()
    handler.wfile.write(raw)


def _resource_route(handler, app, method, path, query) -> None:
    """本地资源面板（issue #51）。只给本机；返回值只进浏览器，不进模型。"""
    host = (handler.client_address or ("",))[0]
    if host not in resources.LOOPBACK:
        _send_json(handler, 403, {"code": "forbidden", "message": "本地资源只给本机看。"})
        return

    def one(name):
        return ((query.get(name) or [""])[0]).strip()[:200]

    try:
        if path == "/api/resources" and method == "GET":
            kind, session = one("type") or None, one("session") or None
            if kind and kind not in resources.TYPES:
                raise resources.ResourceError(400, "invalid_query", "type 不合法。")
            if session and not re.fullmatch(r"s_[0-9a-f]{32}", session):
                raise resources.ResourceError(400, "invalid_query", "session 不合法。")
            _send_json(handler, 200, resources.inventory(app, kind, session, one("q") or None))
            return
        if path == "/api/resources/preview" and method == "GET":
            _send_json(handler, 200, resources.preview_text(app, one("id")))
            return
        if path == "/api/resources/raw" and method == "GET":
            data, media, name = resources.raw(app, one("id"))
            handler.send_response(200)
            handler.send_header("Content-Type", media)
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Content-Security-Policy", "sandbox")
            disposition = "attachment" if one("download") else "inline"
            handler.send_header("Content-Disposition", f'{disposition}; filename="{name}"')
            handler.end_headers()
            handler.wfile.write(data)
            return
        if path == "/api/resources/label" and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, resources.set_label(app, body.get("id"), body.get("label") or ""))
            return
        if path == "/api/resources/delete" and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, resources.delete(app, body.get("ids"), bool(body.get("confirm"))))
            return
        if path == "/api/resources/revoke" and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, resources.revoke(app, body.get("id")))
            return
        if path == "/api/resources/reuse" and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, resources.reuse(app, body.get("id"), body.get("session_id") or ""))
            return
    except resources.ResourceError as exc:
        payload = {"code": exc.code, "message": exc.message}
        payload.update(exc.extra)
        _send_json(handler, exc.status, payload)
        return
    _send_json(handler, 404, {"code": "not_found", "message": "找不到。"})


def _meta_loopback(handler) -> bool:
    host = (handler.client_address or ("",))[0]
    if host not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        _send_json(handler, 403, {"code": "forbidden", "message": "Meta 接口只给本机看。"})
        return False
    return True


def _meta_mine(handler, app, query) -> None:
    """本机：跑一次技能挖掘，覆盖写入 data/meta/skill-candidates.json（#40）。"""
    if not _meta_loopback(handler):
        return
    from services.meta.mine import Miner

    def one(name, default=""):
        return (query.get(name) or [default])[0].strip()

    try:
        min_count = max(1, min(100, int(one("min_count", "3") or 3)))
        min_sessions = max(1, min(100, int(one("min_sessions", "2") or 2)))
    except ValueError:
        _send_json(handler, 400, {"code": "invalid_query", "message": "min_count / min_sessions 应为整数。"})
        return
    use_model = one("model") in ("1", "true", "yes")
    miner = Miner(app.data_dir, sessions=app.sessions)
    result = miner.mine(min_count=min_count, min_sessions=min_sessions, use_model=use_model)
    result.pop("model_prompts", None)
    store = miner.load_store()
    _send_json(handler, 200, {"schema": "mas.skill-candidates/1", "result": result, "candidates": store.get("candidates") or []})


def _meta_candidates(handler, app, query, candidate_id: str | None = None) -> None:
    """本机：列出或查看技能候选。"""
    if not _meta_loopback(handler):
        return
    from services.meta.mine import Miner

    miner = Miner(app.data_dir, sessions=app.sessions)
    if candidate_id:
        if not re.fullmatch(r"sc_[0-9a-f]{8,64}", candidate_id):
            _send_json(handler, 400, {"code": "invalid_id", "message": "候选 id 不合法。"})
            return
        item = miner.get(candidate_id)
        if not item:
            _send_json(handler, 404, {"code": "not_found", "message": "找不到候选。"})
            return
        _send_json(handler, 200, item)
        return
    items = miner.list_candidates()
    store = miner.load_store()
    _send_json(handler, 200, {
        "schema": "mas.skill-candidates/1",
        "mined_at": store.get("mined_at"),
        "params": store.get("params") or {},
        "candidates": items,
        "insufficient": store.get("insufficient") or [],
    })



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
        if method == "GET" and path == "/api/trajectories":
            _trajectories(handler, app, urllib.parse.parse_qs(parsed.query))
            return
        if path == "/api/resources" or path.startswith("/api/resources/"):
            _resource_route(handler, app, method, path, urllib.parse.parse_qs(parsed.query))
            return
        if method == "POST" and path == "/api/meta/mine":
            _meta_mine(handler, app, urllib.parse.parse_qs(parsed.query))
            return
        if method == "GET" and path == "/api/meta/candidates":
            _meta_candidates(handler, app, urllib.parse.parse_qs(parsed.query))
            return
        if method == "GET" and path.startswith("/api/meta/candidates/"):
            _meta_candidates(handler, app, urllib.parse.parse_qs(parsed.query), path[len("/api/meta/candidates/"):])
            return
        if method == "GET" and path in ("/api/egress", "/api/egress/export"):
            _egress_route(handler, path, urllib.parse.parse_qs(parsed.query))
            return
        if method == "GET" and path == "/api/storage":
            _storage_route(handler, app)
            return
        if method == "GET" and path == "/api/dataflow":
            _dataflow_route(handler, app, urllib.parse.parse_qs(parsed.query))
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
        if path.startswith("/api/toolpacks"):
            _toolpack_route(handler, app, method, path)
            return
        if path == "/api/agents" or path.startswith("/api/agents/"):
            _agent_route(handler, method, path)
            return
        if path.startswith("/api/tools/"):
            parts = path[len("/api/tools/"):].split("/")
            name = urllib.parse.unquote(parts[0]) if parts else ""
            if name not in tool_config.declarations():  # 工具名或后端名
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
                        acknowledge=body.get("acknowledge") if isinstance(body.get("acknowledge"), list) else None,
                    )
                except tool_config.AckRequired as exc:
                    # 12.E：放宽隐私默认值要先在界面上看到后果并勾选确认。
                    _send_json(handler, 409, {"code": "acknowledge_required", "message": str(exc),
                                              "errors": exc.errors, "warnings": exc.warnings})
                    return
                except tool_config.ConfigError as exc:
                    _send_json(handler, 400, {"code": "invalid_tool_config", "message": str(exc), "errors": exc.errors})
                    return
                for change in view.get("changed_guarded") or []:
                    _audit_policy_change(name, change)
                _send_json(handler, 200, view)
                return
            if parts[1:] == ["test"] and method == "POST":
                _send_json(handler, 200, tool_config.test_connection(app.tools, name))
                return
            _send_json(handler, 404, {"code": "not_found", "message": "找不到。"})
            return
        if path == "/api/character" and method == "GET":
            _send_json(handler, 200, character.catalog(chat_run._character_config(app)))
            return
        if path == "/api/character/preview" and method == "POST":
            body = _read_json(handler)
            try:
                _send_json(handler, 200, character.compose(body.get("fields") or {}, chat_run._character_config(app)))
            except character.CharacterError as exc:
                _send_json(handler, 400, exc.public())
            return
        matched = _SESSION_CHARACTER.fullmatch(path)
        if matched and method == "POST":
            session = _owned_session(app, matched.group(1))
            run = chat_run.start_character(app, session["session_id"], _read_json(handler))
            _send_sse(handler, run, 0)
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
            run = chat_run.start(app, session["session_id"], body.get("text"), body.get("attachments"), body.get("mode"))
            _send_sse(handler, run, 0)
            return
        matched = _SESSION_STOP.fullmatch(path)
        if matched and method == "POST":
            session = _owned_session(app, matched.group(1))
            _read_json(handler)
            _send_json(handler, 200, chat_run.stop(app, session["session_id"]))
            return
        matched = _SESSION_DELETE.fullmatch(path)
        if matched and method == "POST":
            session = _owned_session(app, matched.group(1))
            _read_json(handler)
            _send_json(handler, 200, chat_run.delete_session(app, session["session_id"]))
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


def _toolpack_route(handler: BaseHTTPRequestHandler, app, method: str, path: str) -> None:
    """工具包：检查 / 安装 / 卸载 / 导出。source 是本机目录或 .zip 路径（服务只听 127.0.0.1）。"""
    packs = tool_config._packs()
    try:
        if path == "/api/toolpacks/inspect" and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, packs.inspect(str(body.get("source") or "")))
            return
        if path == "/api/toolpacks/install" and method == "POST":
            body = _read_json(handler)
            summary = packs.install(str(body.get("source") or ""), body.get("accept_permissions"))
            _send_json(handler, 200, {"ok": True, "installed": summary})
            return
        parts = path[len("/api/toolpacks/"):].split("/")
        name = urllib.parse.unquote(parts[0]) if parts and parts[0] else ""
        if name and parts[1:] == ["uninstall"] and method == "POST":
            packs.uninstall(name)
            _send_json(handler, 200, {"ok": True, "removed": name})
            return
        if name and parts[1:] == ["export"] and method == "GET":
            raw = packs.export(name)
            handler.send_response(200)
            handler.send_header("Content-Type", "application/zip")
            handler.send_header("Content-Disposition", f'attachment; filename="{name}.zip"')
            handler.send_header("Content-Length", str(len(raw)))
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.end_headers()
            handler.wfile.write(raw)
            return
    except packs.PackError as exc:
        status = 404 if exc.code == "not_found" else 400
        _send_json(handler, status, exc.payload())
        return
    _send_json(handler, 404, {"code": "not_found", "message": "找不到。"})


def _agent_route(handler: BaseHTTPRequestHandler, method: str, path: str) -> None:
    """Agent（ADR 0005）：列出 / 检查 / 安装 / 卸载 / 导出，和工具包同一套路。"""
    from services.runtime import agents

    try:
        if path == "/api/agents" and method == "GET":
            _send_json(handler, 200, {"agents": [agents.summary(a) for a in agents.discover()]})
            return
        if path == "/api/agents/inspect" and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, agents.inspect(str(body.get("source") or "")))
            return
        if path == "/api/agents/install" and method == "POST":
            body = _read_json(handler)
            _send_json(handler, 200, {"ok": True, "installed": agents.install(str(body.get("source") or ""))})
            return
        parts = path[len("/api/agents/"):].split("/")
        name = urllib.parse.unquote(parts[0]) if parts and parts[0] else ""
        if name and parts[1:] == ["uninstall"] and method == "POST":
            agents.uninstall(name)
            _send_json(handler, 200, {"ok": True, "removed": name})
            return
        if name and parts[1:] == ["export"] and method == "GET":
            raw = agents.export(name)
            handler.send_response(200)
            handler.send_header("Content-Type", "application/zip")
            handler.send_header("Content-Disposition", f'attachment; filename="{name}.zip"')
            handler.send_header("Content-Length", str(len(raw)))
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.end_headers()
            handler.wfile.write(raw)
            return
    except agents.AgentError as exc:
        _send_json(handler, 404 if exc.code == "not_found" else 400, exc.payload())
        return
    _send_json(handler, 404, {"code": "not_found", "message": "找不到。"})


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
