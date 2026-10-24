"""不访问外网的聊天检查。假供应商只听 127.0.0.1。"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(WEB_DIR, "..", ".."))
sys.path.insert(0, WEB_DIR)
sys.path.insert(0, ROOT)

import image_run
import server
import store
from services.gateway import describe, install_defaults, openai_chat, registry, stream

SECRET = "sk-live-example-key-9876"
TERMINAL = {"message.completed", "run.failed"}


def fail(message: str) -> None:
    raise SystemExit(message)


class ProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.server.requests.append({
            "path": self.path.split("?", 1)[0],
            "auth": self.headers.get("Authorization", ""),
            "body": json.loads(raw.decode("utf-8")),
        })
        mode = self.server.mode
        if mode == "unauthorized":
            key = self.headers.get("Authorization", "").split(" ", 1)[-1].strip()
            payload = json.dumps({"error": {"message": f"bad key {key}"}}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            self._body(mode)
        except OSError:
            return

    def _body(self, mode: str) -> None:
        if mode == "reason":
            self._piece(_delta(reasoning="思考"))
            self._piece(_delta(content="答"))
            self._done()
            return
        if mode == "ok":
            self._piece(_delta(content="你"))
            self._piece(_delta(content="好"))
            self._done()
            return
        if mode == "hang":
            self.server.gate.wait(8)
            self._piece(b"")
            return
        if mode in ("slow", "hold"):
            self._piece(_delta(content="你"))
            gate = self.server.gate
            self.server.started.set()
            gate.wait(8)
            if mode == "hold":
                self._piece(_delta(content="好"))
                self._done()
            else:
                self._piece(_delta(content="不应该出现"))
                self._done()
            return
        if mode == "image":
            if not self._ready():
                self._emit_tool("accept_local_content", {"purpose": "image_prompt"})
                return
            user = self._last_user()
            self._emit_tool("generate_image", {
                "content_handle": _content_handle(user) or ("c_" + "00" * 16),
                "prompt": "模型不该填写的描述",
                "aspect_ratio": "1:1",
                "seed": 7,
            })
            return
        if mode == "refuse":
            self._piece(_delta(content="抱歉，这个提示词我无法生成。"))
            self._done()
            return
        if mode == "foreign":
            self._emit_tool("generate_image", {
                "content_handle": "c_" + "ab" * 16,
                "prompt": "别的画面",
                "aspect_ratio": "1:1",
                "seed": 7,
            })
            return
        if mode == "rewrite":
            if not self._ready():
                self._emit_tool("accept_local_content", {"purpose": "image_prompt"})
                return
            self._emit_tool("generate_image", {
                "prompt": "健康的咖啡馆",
                "aspect_ratio": "16:9",
                "seed": 7,
            })
            return
        if mode == "keep":
            if self._followed():
                self._piece(_delta(content="已记下。"))
                self._done()
                return
            if not self._ready():
                self._emit_tool("accept_local_content", {"purpose": "keep", "kind": "text"})
                return
            self._emit_tool("list_local_content", {})
            return
        if mode == "forget":
            if self._followed():
                self._piece(_delta(content="已忘掉。"))
                self._done()
                return
            if not self._ready():
                self._emit_tool("accept_local_content", {"purpose": "keep", "kind": "text"})
                return
            self._emit_tool("forget_local_content", {
                "content_handle": _content_handle(self._last_user()) or ("c_" + "00" * 16),
            })
            return
        if mode == "keepfile":
            if self._followed():
                self._piece(_delta(content="已保存文件。"))
                self._done()
                return
            if not self._ready():
                self._emit_tool("accept_local_content", {"purpose": "note-free", "kind": "file"})
                return
            self._emit_tool("list_local_content", {})
            return
        if mode == "retrieve":
            if self._followed():
                self._piece(_delta(content="检索完成"))
                self._done()
                return
            self._emit_tool("retrieve_local", {"query": "png"})
            return
        if mode == "forgetfile":
            if self._followed():
                self._piece(_delta(content="已丢掉文件。"))
                self._done()
                return
            if not self._ready():
                self._emit_tool("accept_local_content", {"purpose": "keep", "kind": "file"})
                return
            self._emit_tool("forget_local_content", {
                "content_handle": _content_handle(self._last_user()) or ("c_" + "00" * 16),
            })
            return
        if mode == "multiimage":
            if self._tool_result_count("generate_image") >= 5:
                self._piece(_delta(content="五张都好了"))
                self._done()
                return
            if self._tool_result_count("list_local_content") == 0:
                self._emit_tool("list_local_content", {})
                return
            handle = _content_handle(self._last_user()) or _content_handle(
                json.dumps(self.server.requests[-1]["body"], ensure_ascii=False)
            )
            self._emit_tool("generate_image", {
                "content_handle": handle or ("c_" + "00" * 16),
                "aspect_ratio": "1:1",
                "seed": 7,
            })
            return
        if mode == "localmulti":
            if self._tool_result_count("generate_image") >= 5:
                self._piece(_delta(content="五张都好了"))
                self._done()
                return
            handle = _content_handle(json.dumps(self.server.requests[-1]["body"], ensure_ascii=False))
            if not handle:
                self._emit_tool("accept_local_content", {"purpose": "image_prompt", "kind": "text"})
                return
            self._emit_tool("generate_image", {"content_handle": handle, "aspect_ratio": "1:1", "seed": 7})
            return
        if mode == "localstop":
            # 像真实模型一样：出了一两张就用文字收尾，靠服务端续写指令接着做。
            count = self._tool_result_count("generate_image")
            if count >= 5:
                self._piece(_delta(content="五张都好了"))
                self._done()
                return
            last = self._last_user()
            if count in (1, 2) and last.startswith("工具结果 generate_image"):
                self._piece(_delta(content=f"已生成第 {count} 张。继续生成第 {count + 1} 张。"))
                self._done()
                return
            handle = _content_handle(json.dumps(self.server.requests[-1]["body"], ensure_ascii=False))
            if not handle:
                self._emit_tool("accept_local_content", {"purpose": "image_prompt", "kind": "text"})
                return
            self._emit_tool("generate_image", {"content_handle": handle, "aspect_ratio": "1:1", "seed": 7})
            return
        if mode == "fileimage":
            if not self._ready():
                self._emit_tool("accept_local_content", {"purpose": "keep", "kind": "file"})
                return
            self._emit_tool("generate_image", {
                "content_handle": _content_handle(self._last_user()) or ("c_" + "00" * 16),
                "prompt": "不该用的文件描述",
            })
            return
        if mode == "demo":
            user = self._last_user()
            if "工具结果" in user or "studio-demo" in user:
                self._piece(_delta(content="演示完成"))
                self._done()
                return
            self._emit_tool("lookup_demo", {"topic": "runtime"})
            return
        if mode == "confirm":
            user = self._last_user()
            if "工具结果" in user or "需要确认" in user:
                self._piece(_delta(content="没有出图"))
                self._done()
                return
            self._emit_tool("generate_image", {
                "content_handle": "c_" + "ab" * 16,
                "aspect_ratio": "1:1",
            })
            return
        if mode == "anatomy":
            user = self._last_user()
            if "工具结果 read_skill" in user or "The object is the explanation" in user:
                self._emit_tool("build_skill_page", {"name": "anatomy", "subject": "令牌桶"})
                return
            self._emit_tool("read_skill", {"name": "anatomy"})
            return
        self._piece(b"")

    def _last_user(self) -> str:
        messages = self.server.requests[-1]["body"].get("messages") or []
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "user":
                return _user_text(item.get("content"))
        return ""

    def _ready(self) -> bool:
        return "已就绪" in self._last_user()

    def _followed(self) -> bool:
        return "工具结果" in self._last_user()

    def _tool_result_count(self, name: str) -> int:
        count = 0
        marker = "工具结果 " + name + " "
        local = "本地工具结果 " + name + " "
        for item in self.server.requests[-1]["body"].get("messages") or []:
            if not isinstance(item, dict):
                continue
            text = _user_text(item.get("content"))
            if marker in text or local in text:
                count += 1
        return count

    def _emit_tool(self, name: str, arguments: dict) -> None:
        args = json.dumps(arguments, ensure_ascii=False)
        mid = max(1, len(args) // 2)
        self._piece(_tool_delta(0, call_id="call_image", name=name, arguments=args[:mid]))
        self._piece(_tool_delta(0, arguments=args[mid:]))
        self._piece(_tool_delta(0, finish="tool_calls"))
        self._done()

    def _piece(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _done(self) -> None:
        self._piece(b"data: [DONE]\n\n")
        self._piece(b"")


def _tool_delta(index, call_id=None, name=None, arguments=None, finish=None) -> bytes:
    delta = {}
    if call_id is not None or name is not None or arguments is not None:
        function = {}
        if name is not None:
            function["name"] = name
        if arguments is not None:
            function["arguments"] = arguments
        call = {"index": index, "function": function}
        if call_id is not None:
            call["id"] = call_id
            call["type"] = "function"
        delta["tool_calls"] = [call]
    choice = {"delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return f"data: {json.dumps({'choices': [choice]}, ensure_ascii=False)}\n\n".encode("utf-8")


def _user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _content_handle(text: str) -> str:
    start = 0
    while True:
        found = text.find("c_", start)
        if found < 0:
            return ""
        chunk = text[found:found + 34]
        if len(chunk) == 34 and all(ch in "0123456789abcdef" for ch in chunk[2:]):
            return chunk
        start = found + 1


def _delta(*, content: str | None = None, reasoning: str | None = None) -> bytes:
    delta = {}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if content is not None:
        delta["content"] = content
    return f"data: {json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False)}\n\n".encode("utf-8")


def request(method: str, url: str, payload: dict | None = None, timeout: float = 5):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def read_sse(port: int, path: str, payload: dict | None, on_event=None, timeout: float = 8, stop_types=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request("POST" if payload is not None else "GET", path, body=body, headers=headers)
    response = conn.getresponse()
    if response.status != 200:
        raw = response.read().decode("utf-8")
        conn.close()
        return response.status, raw, []
    events = []
    frame: list[str] = []
    while True:
        line = response.readline()
        if line == b"":
            break
        if line in (b"\n", b"\r\n"):
            if frame:
                event = _frame(frame)
                frame = []
                if event is None:
                    continue
                events.append(event)
                if on_event is not None:
                    on_event(event)
                if event.get("type") in (stop_types or TERMINAL):
                    break
            continue
        frame.append(line.decode("utf-8"))
    conn.close()
    return 200, "", events


def _frame(lines: list[str]) -> dict | None:
    data = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].strip())
    if not data:
        return None
    return json.loads("\n".join(data))


def release(provider) -> None:
    provider.gate.set()
    time.sleep(0.2)
    provider.gate = threading.Event()
    provider.started.clear()


def serve(httpd: ThreadingHTTPServer) -> None:
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()


def self_check() -> None:
    saved = {name: os.environ.pop(name, None) for name in ("XAI_API_KEY", "LLM_API_KEY")}
    try:
        _mcp_projection()
        _saved_env_rules()
        _provider_input_rules()
        _recovery()
        _artifact_cases()
        _redact_unit()
        _redact_history()
        _gateway_cases()
        _runtime_cases()
        _skill_cases()
        _live()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _mcp_projection() -> None:
    poc_root = os.path.join(ROOT, "poc")
    if poc_root not in sys.path:
        sys.path.append(poc_root)
    from tools.mcp import projection_error

    message = projection_error()
    if message:
        fail(message)


def _saved_env_rules() -> None:
    directory = tempfile.mkdtemp(prefix="chat-env-")
    try:
        provider = store.ProviderStore(os.path.join(directory, "provider.json"))
        os.environ["XAI_API_KEY"] = "xai-env-key-1234"
        resolved = provider.resolve()
        if resolved["api_key"] != "xai-env-key-1234" or resolved["key_source"] != "environment":
            fail("api.x.ai 没有使用 XAI_API_KEY")
        provider.update(base_url="http://127.0.0.1:9/v1", model="local-model", api_key=None, clear_key=False)
        resolved = provider.resolve()
        if resolved["api_key"]:
            fail("XAI_API_KEY 被发给了其他主机")
        os.environ["LLM_API_KEY"] = "llm-env-key-1234"
        resolved = provider.resolve()
        if resolved["api_key"] != "llm-env-key-1234":
            fail("LLM_API_KEY 没有作为通用密钥")
        provider.update(
            base_url="http://127.0.0.1:9/v1",
            model="local-model",
            api_key="file-key-9876",
            clear_key=False,
        )
        if provider.resolve()["api_key"] != "file-key-9876":
            fail("本机文件里的密钥没有盖过环境变量")
        public = provider.public()
        if "api_key" in public or "file-key-9876" in json.dumps(public):
            fail("设置响应带出了完整密钥")
        try:
            store.clean_base_url("https://user:secret@example.com/v1")
        except ValueError:
            pass
        else:
            fail("带用户信息的基址没有被拒绝")
    finally:
        os.environ.pop("XAI_API_KEY", None)
        os.environ.pop("LLM_API_KEY", None)
        shutil.rmtree(directory, ignore_errors=True)


def _provider_input_rules() -> None:
    if store.clean_base_url("api.deepseek.com/v1") != "https://api.deepseek.com/v1":
        fail("没写协议的基址没有补上 https")
    if store.clean_base_url("localhost:11434/v1") != "http://localhost:11434/v1":
        fail("本机基址没有补上 http")
    if store.clean_base_url("http://127.0.0.1:11434/v1/chat/completions") != "http://127.0.0.1:11434/v1":
        fail("基址里的 /chat/completions 没有被去掉")
    directory = tempfile.mkdtemp(prefix="chat-key-")
    try:
        provider = store.ProviderStore(os.path.join(directory, "provider.json"))
        view = provider.update(
            base_url="api.example.com/v1",
            model="my-model",
            api_key="ollama",
            clear_key=False,
        )
        if view["base_url"] != "https://api.example.com/v1" or view["model"] != "my-model":
            fail("短基址没有保存下来")
        if view["configured"] is not True or view["key_hint"] != "lama":
            fail("较短的 API key 没有保存")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _recovery() -> None:
    directory = tempfile.mkdtemp(prefix="chat-recover-")
    try:
        root = os.path.join(directory, "sessions")
        os.makedirs(root)
        session_id = "s_" + "ab" * 16
        with open(os.path.join(root, session_id + ".json"), "w", encoding="utf-8") as handle:
            json.dump({
                "session_id": session_id,
                "title": "中断",
                "created_at": "2026-10-10T00:00:00Z",
                "updated_at": "2026-10-10T00:00:00Z",
                "messages": [{
                    "message_id": "m_" + "cd" * 16,
                    "role": "assistant",
                    "text": "半句",
                    "status": "running",
                    "run_id": "r_" + "ef" * 16,
                    "error": None,
                }],
            }, handle)
        reloaded = store.SessionStore(root).get(session_id)
        message = reloaded["messages"][0]
        if message["status"] == "running" or "中断" not in message["error"]["message"]:
            fail("重启后仍把未完成的运行当成还在请求模型")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _artifact_cases() -> None:
    directory = tempfile.mkdtemp(prefix="chat-artifacts-")
    try:
        sessions = store.SessionStore(os.path.join(directory, "sessions"))
        user = "u_" + "ab" * 16
        session_id = sessions.create(user)["session_id"]
        first = sessions.add_attachment(session_id, _PNG, "image/png", "swatch.png")
        second = sessions.add_attachment(session_id, _PNG, "image/png", "copy.png")
        if first["handle"] != second["handle"]:
            fail("同一会话同一张图没有复用句柄")
        if first["handle"][2:] == hashlib.sha256(_PNG).hexdigest()[:32]:
            fail("附件句柄使用了字节的哈希")
        secret = "检索不该带出的本机正文甲乙丙丁"
        kept = sessions.add_content(session_id, secret, "keep")
        again = sessions.add_content(session_id, secret, "keep")
        if kept["handle"] != again["handle"]:
            fail("同一会话同一段文字没有复用句柄")
        other = sessions.create(user)["session_id"]
        foreign = sessions.add_attachment(other, _PNG, "image/png", "swatch.png")
        if foreign["handle"] == first["handle"]:
            fail("别的会话复用了附件句柄")
        if sessions.attachment_bytes(other, first["handle"]) is not None:
            fail("别的会话读到了这枚附件句柄")
        if sessions.attachment_bytes(session_id, first["handle"]) != _PNG:
            fail("复用后读不到原图")
        checksums = {
            item.get("checksum")
            for item in sessions.get(session_id).get("attachments") or []
            if isinstance(item, dict)
        }
        checksums.update(
            item.get("checksum")
            for item in sessions.get(other).get("attachments") or []
            if isinstance(item, dict)
        )
        if checksums != {hashlib.sha256(_PNG).hexdigest()}:
            fail("同一张图在工件库里存了多份")
        found = sessions.retrieve(session_id, "png")
        blob = json.dumps(found, ensure_ascii=False)
        if not any(item.get("source_id") == first["handle"] for item in found):
            fail("检索没有带来源标识")
        if secret in blob or any("text" in item for item in found):
            fail("检索结果带回了正文")
        url = next((item.get("url") for item in found if item.get("source_id") == first["handle"]), "")
        if not isinstance(url, str) or first["handle"] not in url:
            fail("检索结果没有可打开的地址")
        hits = sessions.retrieve(session_id, "甲乙丙")
        if not any(item.get("source_id") == kept["handle"] for item in hits):
            fail("检索没有按正文找到句柄")
        if secret in json.dumps(hits, ensure_ascii=False):
            fail("按正文检索时带回了正文")
        if sessions.retrieve(other, first["handle"]):
            fail("别的会话检索到了这枚句柄")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _redact_history() -> None:
    import chat_run

    body = "画面甲" * 20
    linked = "m_" + "33" * 16
    session = {
        "messages": [
            {
                "role": "user",
                "message_id": "m_" + "11" * 16,
                "text": "前缀" + body,
                "status": "completed",
            },
            {
                "role": "user",
                "message_id": "m_" + "22" * 16,
                "text": body + "后缀",
                "status": "completed",
            },
            {
                "role": "user",
                "message_id": linked,
                "text": body,
                "status": "completed",
            },
            {
                "role": "assistant",
                "message_id": "m_" + "44" * 16,
                "text": "",
                "status": "running",
            },
        ],
        "contents": [{
            "handle": "c_" + "ab" * 16,
            "type": "text",
            "size": len(body.encode("utf-8")),
            "text": body,
            "message_id": linked,
        }],
    }
    messages = chat_run._model_messages(session, replace_user_id=linked, task=chat_run._TASK_LINE)
    blob = json.dumps(messages, ensure_ascii=False)
    if body in blob:
        fail("较早的消息仍把本地正文带给模型")
    if chat_run._TASK_LINE not in blob:
        fail("出图任务没有换成不含正文的指令")


def _redact_unit() -> None:
    leaked = openai_chat._http_error(500, json.dumps({"error": {"message": f"nope {SECRET}"}}).encode(), SECRET)
    if SECRET in leaked.message or "[redacted]" not in leaked.message:
        fail("错误正文没有去掉密钥")
    denied = openai_chat._http_error(401, json.dumps({"error": {"message": SECRET}}).encode(), SECRET)
    if SECRET in denied.message:
        fail("拒绝密钥的提示里出现了密钥")


def _gateway_cases() -> None:
    provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    provider.mode = "ok"
    provider.requests = []
    provider.gate = threading.Event()
    provider.started = threading.Event()
    serve(provider)
    try:
        upstream = f"http://127.0.0.1:{provider.server_address[1]}/v1"
        _gateway_text(provider, upstream)
        _gateway_image(provider, upstream)
        _gateway_audio_rejected(provider, upstream)
        _gateway_video_rejected(provider, upstream)
        _gateway_mixed_not_dropped(provider, upstream)
        _gateway_capabilities()
        _gateway_switch_adapter(provider, upstream)
        _gateway_retryable()
        _gateway_ignored_params(provider, upstream)
        _gateway_legacy_stream_chat(provider, upstream)
    finally:
        install_defaults()
        provider.shutdown()
        provider.server_close()


def _gateway_text(provider, upstream: str) -> None:
    before = len(provider.requests)
    events = _gateway_run(
        {"model": "demo-model", "messages": [{"role": "user", "parts": [{"type": "text", "text": "你好"}]}]},
        upstream,
    )
    kinds = [event.get("type") for event in events]
    if kinds[0] != "run.started" or kinds[-1] != "message.completed" or kinds[1:-1] != ["message.delta", "message.delta"]:
        fail("统一文字流的事件顺序不对")
    if _gateway_text_of(events) != "你好":
        fail("统一文字流没有把增量译回")
    if len(provider.requests) != before + 1:
        fail("统一文字流没有发给适配器")
    body = provider.requests[-1]["body"]
    if body.get("model") != "demo-model" or body.get("stream") is not True:
        fail("统一请求没有带上模型标识")
    content = body["messages"][-1]["content"]
    if content != "你好":
        fail("纯文字没有保持字符串部件")


def _gateway_image(provider, upstream: str) -> None:
    before = len(provider.requests)
    artifact_id = "a_" + "ab" * 16
    events = _gateway_run(
        {
            "model": "demo-model",
            "messages": [{
                "role": "user",
                "parts": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image", "artifact_id": artifact_id, "media_type": "image/png"},
                ],
            }],
        },
        upstream,
        artifacts={artifact_id: {"data": _PNG, "media_type": "image/png"}},
    )
    if events[-1].get("type") != "message.completed":
        fail("图像输入没有完成")
    if len(provider.requests) != before + 1:
        fail("图像输入没有发给模型")
    content = provider.requests[-1]["body"]["messages"][-1]["content"]
    if not isinstance(content, list) or content[0].get("text") != "看这张图":
        fail("图像输入没有保留文字部件")
    url = content[1].get("image_url", {}).get("url") or ""
    marker = "data:image/png;base64,"
    if not url.startswith(marker):
        fail("图像输入没有写成 data URL")
    if url[len(marker):] != base64.b64encode(_PNG).decode("ascii"):
        fail("图像输入的字节不对")


def _gateway_audio_rejected(provider, upstream: str) -> None:
    _gateway_reject_part(provider, upstream, "audio", "音频")


def _gateway_video_rejected(provider, upstream: str) -> None:
    _gateway_reject_part(provider, upstream, "video", "视频")


def _gateway_reject_part(provider, upstream: str, kind: str, label: str) -> None:
    before = len(provider.requests)
    try:
        _gateway_run(
            {
                "model": "demo-model",
                "messages": [{
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": "听这段"},
                        {"type": kind, "artifact_id": "a_" + "cd" * 16, "media_type": kind + "/mpeg", "duration_sec": 1},
                    ],
                }],
            },
            upstream,
        )
        fail(f"{label}被静默接受了")
    except openai_chat.GatewayError as exc:
        if exc.code != "modality_unsupported" or exc.retryable:
            fail(f"{label}的错误码或可重试标记不对：{exc.code} {exc.retryable}")
    if len(provider.requests) != before:
        fail(f"{label}被静默丢掉后仍然发给了模型")


def _gateway_mixed_not_dropped(provider, upstream: str) -> None:
    before = len(provider.requests)
    try:
        _gateway_run(
            {
                "model": "demo-model",
                "messages": [{
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": "只要文字"},
                        {"type": "audio", "artifact_id": "a_" + "ef" * 16, "media_type": "audio/mpeg", "duration_sec": 2},
                    ],
                }],
            },
            upstream,
        )
        fail("混有音频的请求没有失败")
    except openai_chat.GatewayError as exc:
        if exc.code != "modality_unsupported":
            fail(f"混有音频时错误码不对：{exc.code}")
    if len(provider.requests) != before:
        fail("混有音频时把文字悄悄发给了模型")


def _gateway_capabilities() -> None:
    caps = describe("grok-4.7")
    if caps["model"] != "grok-4.7":
        fail("能力声明没有带上模型标识")
    if set(caps["input_modalities"]) != {"text", "image"}:
        fail("能力声明的输入模态不对")
    if "audio" in caps["input_modalities"] or "video" in caps["input_modalities"]:
        fail("能力声明把未实现的模态写进去了")
    if caps["output_modalities"] != ("text",) or caps["streaming"] is not True:
        fail("能力声明的输出或流式不对")
    if caps["context_unit"] != "tokens" or not caps["context_limit"]:
        fail("能力声明没有上下文上限")
    if caps["tools"] is not True:
        fail("能力声明没有工具调用")
    text_only = describe("deepseek-v4-pro")
    if "image" in text_only["input_modalities"]:
        fail("deepseek-v4-pro 不该声明能看图")
    if set(text_only["input_modalities"]) != {"text"}:
        fail("deepseek-v4-pro 的输入模态不对")
    flash = describe("deepseek-flash")
    if "image" not in flash["input_modalities"] or "text" not in flash["input_modalities"]:
        fail("deepseek-flash 没有声明能看图")


def _gateway_switch_adapter(provider, upstream: str) -> None:
    fake = _FakeAdapter()
    registry.register("fake", fake)
    registry.bind("echo-1", "fake")
    before = len(provider.requests)
    request = {
        "model": "echo-1",
        "messages": [{"role": "user", "parts": [{"type": "text", "text": "同一条"}]}],
    }
    events = _gateway_run(request, upstream)
    if fake.seen != ["echo-1"]:
        fail("换模型标识没有打到已接入的另一个适配器")
    if not any(event.get("text") == "from-fake" for event in events):
        fail("另一个适配器没有产出统一事件")
    if len(provider.requests) != before:
        fail("绑定了别的适配器仍然打到了 OpenAI 兼容适配器")
    other = dict(request)
    other["model"] = "demo-model"
    events = _gateway_run(other, upstream)
    if len(provider.requests) != before + 1:
        fail("同一条请求换回原模型标识没有发给原来的适配器")
    if _gateway_text_of(events) != "你好":
        fail("原来的适配器没有回答")


def _gateway_retryable() -> None:
    timeout = openai_chat.GatewayError("timeout", "模型超时了，没有返回完整回答。")
    if timeout.retryable is not True:
        fail("超时没有标成可重试")
    limited = openai_chat._http_error(429, b"{}", SECRET)
    if limited.retryable is not True or limited.code != "rate_limited":
        fail("限流没有标成可重试")
    denied = openai_chat._http_error(401, b"{}", SECRET)
    if denied.retryable is not False:
        fail("密钥被拒仍标成可重试")
    unsupported = openai_chat.GatewayError("modality_unsupported", "这个模型不接受这种输入。")
    if unsupported.retryable is not False:
        fail("不支持的模态标成了可重试")


def _gateway_ignored_params(provider, upstream: str) -> None:
    events = _gateway_run(
        {
            "model": "demo-model",
            "messages": [{"role": "user", "parts": [{"type": "text", "text": "你好"}]}],
            "foo": 1,
            "made_up": True,
            "temperature": 0.2,
        },
        upstream,
    )
    started = events[0]
    if started.get("type") != "run.started" or set(started.get("ignored_params") or []) != {"foo", "made_up"}:
        fail("未知生成参数没有记在 run.started")
    body = provider.requests[-1]["body"]
    if "foo" in body or "made_up" in body:
        fail("未知参数被假装生效了")
    if body.get("temperature") != 0.2:
        fail("认识的生成参数没有写进供应商请求")


def _gateway_legacy_stream_chat(provider, upstream: str) -> None:
    before = len(provider.requests)
    gen = openai_chat.stream_chat(
        base_url=upstream,
        api_key=SECRET,
        model="demo-model",
        messages=[{"role": "user", "content": "你好"}],
        timeout_sec=2,
        cancel=threading.Event(),
    )
    try:
        piece = next(gen)
    finally:
        gen.close()
    if piece != "你" or len(provider.requests) != before + 1:
        fail("旧的 stream_chat 包装坏了")


def _gateway_text_of(events: list[dict]) -> str:
    return "".join(event.get("text") or "" for event in events if event.get("type") == "message.delta")


def _gateway_run(request: dict, upstream: str, artifacts=None) -> list[dict]:
    events = []
    for event in stream(
        request,
        api_key=SECRET,
        timeout_sec=2,
        cancel=threading.Event(),
        artifacts=artifacts,
        base_url=upstream,
    ):
        events.append(event)
    return events


class _FakeAdapter:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def capabilities(self, model: str) -> dict:
        return {
            "model": model,
            "input_modalities": ("text",),
            "output_modalities": ("text",),
            "streaming": True,
            "tools": False,
            "context_limit": 1024,
            "context_unit": "tokens",
            "max_file_bytes": None,
            "max_duration_sec": None,
            "max_images": None,
        }

    def stream(self, request, **kwargs):
        self.seen.append(request["model"])
        yield {"type": "message.delta", "text": "from-fake"}


def _runtime_cases() -> None:
    from services.gateway import install_defaults, registry
    from services.runtime import Budget, run as runtime_run, traces_for
    from services.runtime.tools import execute_tool, tool_mutates

    try:
        _runtime_clock()
        _runtime_demo(registry, runtime_run, traces_for)
        _runtime_budget(registry, runtime_run, traces_for)
        _runtime_slow_tools(registry, runtime_run)
        _runtime_confirm(registry, runtime_run)
        _runtime_cancel(registry, runtime_run)
        _runtime_invalid_schema(registry, runtime_run)
        if not tool_mutates("generate_image") or tool_mutates("lookup_demo"):
            fail("写操作和只读演示的确认标记不对")
        fact = execute_tool("lookup_demo", {"topic": "runtime"})
        if not fact.get("ok") or "studio-demo: runtime" not in str(fact.get("fact")):
            fail("只读演示工具没有返回固定说明")
    finally:
        install_defaults()


def _runtime_clock() -> None:
    from services.runtime.budget import Budget, Clock

    timed = Clock(Budget(steps=8, time_sec=0, tokens=100000))
    if "时间" not in (timed.check() or ""):
        fail("超过时间预算没有可读的停止原因")
    tokens = Clock(Budget(steps=8, time_sec=120, tokens=1))
    tokens.tokens_used = 1
    if "token" not in (tokens.check() or ""):
        fail("超过 token 预算没有可读的停止原因")


def _runtime_demo(registry, runtime_run, traces_for) -> None:
    adapter = _ScriptAdapter([
        [{"type": "tool.requested", "name": "lookup_demo", "arguments": {"topic": "runtime"}}],
        [{"type": "message.delta", "text": "演示完成"}],
    ])
    _install_script(registry, "runtime-demo", adapter)
    events = _runtime_collect(runtime_run, "runtime-demo", "查一下演示")
    kinds = [event.get("type") for event in events]
    if kinds[0] != "run.started" or "tool.requested" not in kinds or kinds[-1] != "message.completed":
        fail(f"只读演示循环的事件不对：{kinds}")
    if _gateway_text_of(events) != "演示完成":
        fail("只读演示没有在工具返回后给出回答")
    if any(event.get("type") == "tool.confirm" for event in events):
        fail("只读演示被当成了写操作")
    run_id = events[0].get("run_id")
    items = traces_for(run_id)
    types = [item.get("type") for item in items]
    if types != ["model", "tool", "model"]:
        fail(f"追踪没有按一次运行串起来：{types}")
    if items[0].get("modalities") != ["text"] or items[1].get("name") != "lookup_demo":
        fail("追踪缺少模态或工具名")
    if len(adapter.calls) != 2:
        fail("只读演示的模型步数不对")
    blob = json.dumps(adapter.calls[1]["messages"], ensure_ascii=False)
    if "studio-demo: runtime" not in blob:
        fail("第二次模型请求没有带上工具结果")


def _runtime_budget(registry, runtime_run, traces_for) -> None:
    adapter = _ScriptAdapter(None)

    def always(request, **kwargs):
        adapter.calls.append(request)
        yield {"type": "tool.requested", "name": "lookup_demo", "arguments": {"topic": "loop"}}

    adapter.stream = always
    _install_script(registry, "runtime-budget", adapter)
    from services.runtime import Budget

    events = _runtime_collect(runtime_run, "runtime-budget", "一直查", budget=Budget(steps=2))
    if events[-1].get("type") != "run.failed" or events[-1].get("code") != "budget_exceeded":
        fail("超过步数预算没有失败")
    if "步数" not in (events[-1].get("message") or ""):
        fail("超过步数预算没有可读的停止原因")
    if len(adapter.calls) != 2:
        fail("超过步数预算后仍发出了新的模型请求")
    items = traces_for(events[0]["run_id"])
    if [item.get("type") for item in items] != ["model", "tool", "model", "tool"]:
        fail(f"步数预算用尽时追踪不对：{[item.get('type') for item in items]}")


def _runtime_slow_tools(registry, runtime_run) -> None:
    from services.runtime import Budget

    calls = {"n": 0}
    adapter = _ScriptAdapter(None)

    def script(request, **kwargs):
        adapter.calls.append(request)
        calls["n"] += 1
        if calls["n"] <= 5:
            yield {"type": "tool.requested", "name": "lookup_demo", "arguments": {"topic": "slow"}}
        else:
            yield {"type": "message.delta", "text": "五张都好了"}

    adapter.stream = script
    _install_script(registry, "runtime-slow", adapter)

    def execute(name, arguments):
        time.sleep(0.3)
        return {"ok": True, "tool": name}

    events = _runtime_collect(
        runtime_run, "runtime-slow", "连续出五张", execute=execute, budget=Budget(steps=24, time_sec=1.0),
    )
    if events[-1].get("type") != "message.completed" or _gateway_text_of(events) != "五张都好了":
        fail(f"慢工具（出图）把时间预算吃光，连续出图做到一半停下：{events[-1]}")


def _runtime_confirm(registry, runtime_run) -> None:
    asked = []
    executed = []
    adapter = _ScriptAdapter([
        [{"type": "tool.requested", "name": "generate_image", "arguments": {
            "content_handle": "c_" + "ab" * 16, "aspect_ratio": "1:1",
        }}],
        [{"type": "message.delta", "text": "没有出图"}],
    ])
    _install_script(registry, "runtime-confirm", adapter)

    def confirm(name, arguments):
        asked.append(name)
        return False

    def execute(name, arguments):
        executed.append(name)
        return {"ok": True, "tool": name}

    events = _runtime_collect(
        runtime_run, "runtime-confirm", "画一张", confirm=confirm, execute=execute,
    )
    if asked != ["generate_image"] or executed:
        fail("写操作没有停下来等人确认")
    if not any(event.get("type") == "tool.confirm" for event in events):
        fail("写操作没有发出确认事件")
    if _gateway_text_of(events) != "没有出图":
        fail("拒绝确认后没有继续回答")


def _runtime_cancel(registry, runtime_run) -> None:
    import threading
    from services.runtime.tools import execute_tool

    adapter = _ScriptAdapter([
        [{"type": "tool.requested", "name": "lookup_demo", "arguments": {"topic": "stop"}}],
        [{"type": "message.delta", "text": "不该出现"}],
    ])
    _install_script(registry, "runtime-cancel", adapter)
    cancel = threading.Event()

    def execute(name, arguments):
        cancel.set()
        return execute_tool(name, arguments)

    events = _runtime_collect(runtime_run, "runtime-cancel", "停下", cancel=cancel, execute=execute)
    if events[-1].get("code") != "cancelled":
        fail("取消后没有停下")
    if len(adapter.calls) != 1:
        fail("取消后仍发出了新的模型请求")
    if any(event.get("text") == "不该出现" for event in events):
        fail("取消后仍追加了回答")


def _runtime_invalid_schema(registry, runtime_run) -> None:
    executed = []
    adapter = _ScriptAdapter([
        [{"type": "tool.requested", "name": "lookup_demo", "arguments": {}}],
        [{"type": "message.delta", "text": "参数不对"}],
    ])
    _install_script(registry, "runtime-schema", adapter)

    def execute(name, arguments):
        executed.append(name)
        return {"ok": True}

    events = _runtime_collect(runtime_run, "runtime-schema", "查", execute=execute)
    if executed:
        fail("参数没通过校验仍然执行了工具")
    if _gateway_text_of(events) != "参数不对":
        fail("校验失败后没有把结果交回模型")


def _runtime_collect(runtime_run, model: str, text: str, **kwargs) -> list[dict]:
    import threading

    request = {
        "model": model,
        "messages": [{"role": "user", "parts": [{"type": "text", "text": text}]}],
        "tools": [],
    }
    cancel = kwargs.pop("cancel", threading.Event())
    events = []
    for event in runtime_run(
        request,
        api_key=SECRET,
        cancel=cancel,
        timeout_sec=2,
        **kwargs,
    ):
        events.append(event)
    return events


def _install_script(registry, model: str, adapter) -> None:
    from services.gateway import install_defaults

    install_defaults()
    registry.register("script", adapter)
    registry.bind(model, "script")


class _ScriptAdapter:
    def __init__(self, replies) -> None:
        self.replies = replies
        self.calls: list[dict] = []

    def capabilities(self, model: str) -> dict:
        return {
            "model": model,
            "input_modalities": ("text",),
            "output_modalities": ("text",),
            "streaming": True,
            "tools": True,
            "context_limit": 1024,
            "context_unit": "tokens",
            "max_file_bytes": None,
            "max_duration_sec": None,
            "max_images": None,
        }

    def stream(self, request, **kwargs):
        self.calls.append(request)
        index = len(self.calls) - 1
        replies = self.replies or []
        if index >= len(replies):
            yield {"type": "message.delta", "text": "额外一步"}
            return
        for event in replies[index]:
            yield event


def _live() -> None:
    directory = tempfile.mkdtemp(prefix="chat-live-")
    provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    provider.mode = "ok"
    provider.requests = []
    provider.gate = threading.Event()
    provider.started = threading.Event()
    app = server.App(directory, timeout_sec=5)
    try:
        server.make_httpd(app, "0.0.0.0", 0)
    except SystemExit as exc:
        if "127.0.0.1" not in str(exc):
            fail("非本机监听的拒绝语不对")
    else:
        fail("聊天服务接受了非本机地址")
    site = server.make_httpd(app, "127.0.0.1", 0)
    serve(provider)
    serve(site)
    base = f"http://127.0.0.1:{site.server_address[1]}"
    upstream = f"http://127.0.0.1:{provider.server_address[1]}/v1"
    try:
        _page_and_key(base, upstream, directory, provider, app)
    finally:
        release(provider)
        site.shutdown()
        provider.shutdown()
        site.server_close()
        provider.server_close()
        shutil.rmtree(directory, ignore_errors=True)


def _page_and_key(base: str, upstream: str, directory: str, provider, app) -> None:
    status, page = request("GET", base + "/")
    if status != 200 or "发送" not in page or "停止" not in page:
        fail("聊天页没有发送和停止")
    if "运行过程" not in page or 'id="runtime-log"' not in page:
        fail("聊天页没有运行过程窗口")
    if 'method="dialog"' in page or "settings-error" not in page:
        fail("设置保存失败时对话框会自己关掉")
    status, script = request("GET", base + "/static/app.js")
    status, css = request("GET", base + "/static/app.css")
    if status != 200 or "max-height: 100dvh" not in css or "overflow: hidden" not in css:
        fail("运行过程没有钉在视口里，长对话会把它滚走")
    if status != 200 or "message.delta" not in script or "tool_call" not in script or "Enter" not in script:
        fail("页面脚本没有接上流式事件")
    if "runtime.step" not in script or "renderRuntime" not in script:
        fail("页面脚本没有接上运行过程")
    if "innerHTML" in script or "regenerate" in script or "use_as_reference" in script or "change_aspect" in script:
        fail("页面多做了卡片动作，或用 innerHTML 塞进了内容")
    if "打开等距图" not in script:
        fail("页面没有等距图入口")
    if "retrieve_local" not in script or "source_id" not in script:
        fail("页面没有检索结果入口")
    if 'events.filter((entry) => entry.type === "artifact")' not in script:
        fail("页面没有渲染同一条消息上的多张图片")
    if "item.artifact_id === event.artifact_id" not in script:
        fail("页面把后来的图片当成重复事件丢掉了")
    if "content_request" not in script or "加入本地内容并继续" not in script:
        fail("页面没有本地内容输入")
    if "取消" not in script or "skip: true" not in script:
        fail("本地内容输入不能空着关掉")
    if 'kind === "file"' not in script:
        fail("页面没有文件形式的本地内容输入")
    if "本机用户" not in page or "/api/identity" not in script:
        fail("页面没有单用户身份")
    if "确认" not in script or "拒绝" not in script or "重试" not in script:
        fail("页面没有工具确认或失败重试")
    if "不能带附件" in script:
        fail("页面仍然拒绝所有附件")
    status, ident = request("GET", base + "/api/identity")
    if status != 200 or json.loads(ident).get("display_name") != "本机用户":
        fail("身份接口没有返回本机用户")
    status, body = request("POST", base + "/api/sessions", {})
    session_id = json.loads(body)["session_id"]
    before = len(provider.requests)
    status, _, events = read_sse(site_port(base), f"/api/sessions/{session_id}/messages", {"text": "你好"})
    if events[-1].get("code") != "missing_key" or len(provider.requests) != before:
        fail("没有密钥时仍然请求了模型，或没有提示填写 API key")
    status, saved = request("PUT", base + "/api/provider", {
        "base_url": upstream,
        "model": "demo-model",
        "api_key": SECRET,
    })
    view = json.loads(saved)
    if SECRET in saved or view.get("key_hint") != "9876" or view.get("configured") is not True:
        fail("保存后的设置响应带出了完整密钥")
    if "image" not in (view.get("input_modalities") or []) or "text" not in (view.get("input_modalities") or []):
        fail("设置没有带上模型接受的输入模态")
    status, again = request("PUT", base + "/api/provider", {"base_url": upstream, "model": "demo-model"})
    if json.loads(again).get("key_hint") != "9876":
        fail("留空密钥时把已保存的密钥清掉了")

    provider.mode = "ok"
    events = _speak(base, session_id, "第一句")
    if _assistant_text(events) != "你好":
        fail("流式增量没有拼成同一条回答")
    if not any(event.get("type") == "runtime.step" for event in events):
        fail("文字回答没有把运行过程推到页面")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    if stored["messages"][-1]["text"] != "你好" or stored["messages"][-1]["status"] != "completed":
        fail("刷新后回答不在")
    if not str(stored.get("user_id") or "").startswith("u_"):
        fail("会话没有归到本机用户")
    if not any(item.get("kind") == "model" for item in stored["messages"][-1].get("runtime") or []):
        fail("刷新后运行过程不在")
    provider.mode = "demo"
    demo_events = _speak(base, session_id, "查一下演示")
    if _assistant_text(demo_events) != "演示完成":
        fail("只读演示没有在工具返回后给出回答")
    demo_steps = [
        event.get("kind")
        for event in demo_events
        if event.get("type") == "runtime.step" and event.get("kind") in ("model", "tool") and event.get("ms") is not None
    ]
    if demo_steps != ["model", "tool", "model"]:
        fail(f"运行过程没有列出模型到工具再到回答：{demo_steps}")
    if not any(event.get("name") == "lookup_demo" for event in demo_events if event.get("type") == "runtime.step"):
        fail("运行过程没有写出 lookup_demo")
    provider.mode = "ok"
    call = provider.requests[-1]
    if call["path"] != "/v1/chat/completions" or call["auth"] != f"Bearer {SECRET}":
        fail("没有按 Chat Completions 带上密钥")
    if SECRET in call["path"] or call["body"].get("stream") is not True or call["body"].get("model") != "demo-model":
        fail("模型请求的路径或模型标识不对")

    provider.mode = "reason"
    events = _speak(base, session_id, "看推理")
    if _assistant_text(events) != "答" or "思考" in _assistant_text(events):
        fail("推理草稿进了回答正文")
    sent = provider.requests[-1]["body"]["messages"]
    answered = any(item.get("role") == "assistant" and item.get("content") == "你好" for item in sent)
    if not sent or sent[0].get("role") != "system" or not answered or sent[-1].get("content") != "看推理":
        brief = [(item.get("role"), len(item.get("content") or "")) for item in sent]
        fail(f"下一轮没有带上已经完成的对话：{brief}")
    system = sent[0].get("content") or ""
    if "anatomy" not in system or "four-tone shading" not in system:
        fail("普通回合的系统指令没有技能名字和短描述")
    if "The object is the explanation" in system or "iso-kit.mjs" in system:
        fail("普通回合的系统指令带上了技能正文")

    _skill_turn(base, provider)
    _stop_case(base, session_id, provider)
    _hold_case(base, session_id, provider)
    _reject_key_case(base, session_id, provider, directory)
    _reject_file_case(base, session_id, provider)
    _reject_image_text_model_case(base, session_id, provider, upstream)
    _ask_image_case(base, session_id, provider)
    _reuse_attachment_case(base, directory)
    _retrieve_case(base, provider, directory)
    _memory_case(base, provider)
    _vision_prompt_case(base, provider)
    _skip_content_case(base, provider)
    _confirm_reject_case(base, provider)
    _busy_case(base, session_id, provider)
    _timeout_case(base, session_id, provider, app)
    _image_cases(base, provider, directory)

    status, cleared = request("PUT", base + "/api/provider", {
        "base_url": upstream,
        "model": "demo-model",
        "clear_key": True,
    })
    if json.loads(cleared).get("configured") is not False or SECRET in cleared:
        fail("清除密钥后设置里还能看到密钥")
    before = len(provider.requests)
    events = _speak(base, session_id, "清除后再问")
    if events[-1].get("code") != "missing_key" or len(provider.requests) != before:
        fail("清除密钥后仍向模型发了请求")


def _skill_cases() -> None:
    path = os.path.join(ROOT, "skills", "anatomy", "SKILL.md")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if "name: anatomy" not in text:
        fail("技能正面没有名字 anatomy")
    if "upstream: https://github.com/wheresryan22/anatomy" not in text:
        fail("技能没有记下上游地址")
    if "commit: e9488296a44211c42701ff71705b7901470ec78f" not in text:
        fail("技能没有钉住提交")
    from services.runtime.skills import catalog_suffix, read_instructions

    catalog = catalog_suffix()
    if "anatomy" not in catalog or "four-tone shading" not in catalog:
        fail("系统指令没有带上技能名字和短描述")
    if "The object is the explanation" in catalog or "iso-kit.mjs" in catalog:
        fail("系统指令带上了技能正文或 kit")
    loaded = read_instructions("anatomy")
    blob = json.dumps(loaded, ensure_ascii=False)
    instructions = loaded.get("instructions") or ""
    if not loaded.get("ok") or "The object is the explanation" not in instructions:
        fail("读取技能没有返回做法")
    if "D:\\" in blob or "D:/" in blob or "multimodal-agent-studio" in blob:
        fail("读取技能带出了本机路径")
    import skill_page

    missing = skill_page.build_page("s_" + "ab" * 16, "anatomy", "令牌桶", node="")
    missing_blob = json.dumps(missing, ensure_ascii=False)
    message = (missing.get("error") or {}).get("message") or ""
    if missing.get("ok") or "Node" not in message:
        fail("没有 Node.js 时没有说明做不了等距图")
    if "generate_image" in missing_blob:
        fail("没有 Node.js 时落到了出图")


def _skill_turn(base: str, provider) -> None:
    secret = "隔离用的画面正文甲乙丙丁"
    saved_dir = os.environ.get("COMFY_ARTIFACT_DIR")
    os.environ["COMFY_ARTIFACT_DIR"] = tempfile.mkdtemp(prefix="skill-artifacts-")
    try:
        _skill_turn_body(base, provider, secret)
    finally:
        if saved_dir is None:
            os.environ.pop("COMFY_ARTIFACT_DIR", None)
        else:
            os.environ["COMFY_ARTIFACT_DIR"] = saved_dir


def _skill_turn_body(base: str, provider, secret: str) -> None:
    session_id = _fresh_session(base)
    provider.mode = "keep"
    noted = _speak(base, session_id, secret)
    if _assistant_text(noted) != "已记下。":
        fail("等距图之前没有把本会话正文记下")
    provider.mode = "anatomy"
    sent = len(provider.requests)
    events = _speak(base, session_id, "用一张等距图说明令牌桶怎么工作", timeout=90)
    new_requests = provider.requests[sent:]
    if len(new_requests) != 2:
        fail(f"等距图的供应商请求次数不对：{len(new_requests)}")
    first = json.dumps(new_requests[0]["body"], ensure_ascii=False)
    second = json.dumps(new_requests[1]["body"], ensure_ascii=False)
    system = ((new_requests[0]["body"].get("messages") or [{}])[0].get("content") or "")
    if "anatomy" not in system or "four-tone shading" not in system:
        fail("等距图回合的系统指令没有技能名字和短描述")
    if "The object is the explanation" in first:
        fail("还没读技能就把做法发给了模型")
    if "The object is the explanation" not in second:
        fail("读技能后没有把做法交给模型")
    for blob in (first, second):
        if secret in blob or "D:\\" in blob or "D:/" in blob or "multimodal-agent-studio" in blob:
            fail("技能回合把本会话正文或路径发给了模型")
    if any(event.get("tool") == "generate_image" or event.get("name") == "generate_image" for event in events):
        fail("等距图调用了 generate_image")
    artifact = next((event for event in events if event.get("type") == "artifact"), None)
    artifact_id = str((artifact or {}).get("artifact_id") or "")
    if artifact is None or not artifact_id.startswith("art_") or artifact.get("media") != "html":
        fail("等距图没有放进会话")
    status, raw = request_bytes("GET", base + artifact["url"], timeout=30)
    page = raw.decode("utf-8", errors="replace")
    if status != 200 or "<svg" not in page or "令牌桶" not in page:
        fail("等距图页面打不开")
    if _assistant_text(events) != "等距图已经放进这个会话。":
        fail("等距图完成后没有告诉用户页面已放进会话")


def _post_content(base: str, run_id: str, text: str) -> None:
    status, body = request("POST", base + f"/api/runs/{run_id}/content", {"text": text})
    if status != 200:
        fail(f"提交本地内容失败：{status} {body}")


def _post_confirm(base: str, run_id: str, allowed: bool = True) -> None:
    status, body = request("POST", base + f"/api/runs/{run_id}/confirm", {"allowed": allowed})
    if status != 200:
        fail(f"提交工具确认失败：{status} {body}")


def _speak(base: str, session_id: str, text: str, timeout: float = 8, content: str | None = None, attachments=None):
    def on_event(event):
        if event.get("type") == "tool.confirm":
            _post_confirm(base, event.get("run_id") or "", True)
        if event.get("type") == "content_request":
            _post_content(base, event.get("run_id") or "", content if content is not None else text)

    payload = {"text": text}
    if attachments:
        payload["attachments"] = attachments
    status, raw, events = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        payload,
        on_event=on_event,
        timeout=timeout,
    )
    if status != 200 or not events or events[-1].get("type") not in TERMINAL:
        fail("发送没有收到结束事件")
    if raw and SECRET in raw:
        fail("事件流里出现了密钥")
    blob = json.dumps(events, ensure_ascii=False)
    if SECRET in blob:
        fail("事件流里出现了密钥")
    return events


def _assistant_text(events: list[dict]) -> str:
    text = ""
    for event in events:
        if event.get("type") == "message.delta":
            text += event.get("text") or ""
        elif event.get("type") in TERMINAL and "text" in event:
            text = event.get("text") or ""
    return text


def _stop_case(base: str, session_id: str, provider) -> None:
    provider.mode = "slow"
    provider.started.clear()
    seen = threading.Event()
    box: dict = {}

    def watch(event):
        if event.get("type") == "message.delta":
            seen.set()

    def run():
        box["events"] = read_sse(
            site_port(base),
            f"/api/sessions/{session_id}/messages",
            {"text": "停一下"},
            on_event=watch,
            timeout=8,
        )

    started = time.monotonic()
    worker = threading.Thread(target=run)
    worker.start()
    if not seen.wait(3):
        fail("停止用例没有收到第一段增量")
    status, body = request("POST", base + f"/api/sessions/{session_id}/stop", {})
    if status != 200:
        fail("停止请求失败")
    worker.join(4)
    release(provider)
    events = box.get("events", (0, "", []))[2]
    elapsed = time.monotonic() - started
    text = _assistant_text(events)
    code = events[-1].get("code") if events else ""
    if code != "cancelled" or text != "你" or "不应该出现" in text or elapsed >= 3:
        fail(f"停止后仍在追加增量：{code} {elapsed:.2f}s {text}")


def _hold_case(base: str, session_id: str, provider) -> None:
    provider.mode = "hold"
    provider.started.clear()
    before = len(provider.requests)
    conn = http.client.HTTPConnection("127.0.0.1", site_port(base), timeout=5)
    body = json.dumps({"text": "断线"}).encode("utf-8")
    conn.request("POST", f"/api/sessions/{session_id}/messages", body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    if response.status != 200:
        conn.close()
        fail("断线用例没有打开事件流")
    # read(n) 会把当前短块和后面的块拼满 n 字节才返回。下一块还被挡住时，第一段增量到不了这里。
    seen = False
    started_at = time.monotonic()
    while time.monotonic() - started_at < 3:
        line = response.readline()
        if not line:
            break
        if "你".encode("utf-8") in line:
            seen = True
            break
    conn.close()
    if not seen:
        fail("断线用例没有收到第一段增量")
    if not provider.started.wait(3):
        fail("断线用例没有开始模型请求")
    release(provider)
    assistant = {}
    for _ in range(50):
        payload = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
        assistant = payload["messages"][-1]
        if assistant.get("status") == "completed":
            break
        time.sleep(0.05)
    if assistant.get("text") != "你好" or assistant.get("status") != "completed":
        fail(
            "页面断开后这次运行没有写完："
            + str(assistant.get("status"))
            + " "
            + repr(assistant.get("text"))
            + " "
            + str((assistant.get("error") or {}).get("code"))
            + " "
            + str((assistant.get("error") or {}).get("message"))
        )
    if len(provider.requests) != before + 1:
        fail("页面断开后又向模型提交了一次")


def _reject_key_case(base: str, session_id: str, provider, directory: str) -> None:
    provider.mode = "unauthorized"
    events = _speak(base, session_id, "坏密钥")
    if events[-1].get("code") != "provider_error":
        fail("错误密钥没有变成供应商错误")
    sessions = os.path.join(directory, "sessions")
    for name in os.listdir(sessions):
        raw = open(os.path.join(sessions, name), "rb").read()
        if SECRET.encode("utf-8") in raw:
            fail("会话记录里出现了密钥")


def _reject_file_case(base: str, session_id: str, provider) -> None:
    before = len(provider.requests)
    status, raw = request("POST", base + f"/api/sessions/{session_id}/messages", {"text": "  "})
    if status != 400:
        fail("空消息没有被拒绝")
    encoded = base64.b64encode(b"RIFF....WAVEfmt ").decode("ascii")
    status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", {
        "file": encoded,
        "media_type": "audio/wav",
        "name": "clip.wav",
    })
    if status != 200:
        fail(f"音频附件没有收下：{status} {body}")
    handle = json.loads(body)["handle"]
    status, raw, events = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        {"text": "听这段", "attachments": [{"handle": handle}]},
    )
    if status != 200 or events[-1].get("code") != "modality_unsupported":
        fail("音频没有按模型能力拒绝")
    if events[-1].get("retryable") is not False:
        fail("不支持的模态标成了可重试")
    if events[-1].get("message") != "这个模型不接受这种输入。":
        fail("不支持的模态没有用人话提示")
    if len(provider.requests) != before:
        fail("不接受的附件仍然发给了模型")
    if raw and SECRET in raw:
        fail("事件流里出现了密钥")


def _reject_image_text_model_case(base: str, session_id: str, provider, upstream: str) -> None:
    before = len(provider.requests)
    status, saved = request("PUT", base + "/api/provider", {
        "base_url": upstream,
        "model": "deepseek-v4-pro",
        "api_key": SECRET,
    })
    if status != 200:
        fail(f"没能改成纯文本模型：{status} {saved}")
    view = json.loads(saved)
    if "image" in (view.get("input_modalities") or []):
        fail("deepseek-v4-pro 的设置仍显示能看图")
    try:
        encoded = base64.b64encode(_PNG).decode("ascii")
        status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", {
            "file": encoded,
            "media_type": "image/png",
            "name": "swatch.png",
        })
        if status != 200:
            fail(f"纯文本模型下图片附件没有收下：{status} {body}")
        handle = json.loads(body)["handle"]
        status, raw, events = read_sse(
            site_port(base),
            f"/api/sessions/{session_id}/messages",
            {"text": "看这张图", "attachments": [{"handle": handle}]},
        )
        if status != 200 or events[-1].get("code") != "modality_unsupported":
            fail("纯文本模型没有按能力拒绝图片")
        if events[-1].get("message") != "这个模型不接受这种输入。":
            fail("纯文本模型拒绝图片没有用人话提示")
        if len(provider.requests) != before:
            fail("纯文本模型仍然把图片发给了模型")
        if raw and SECRET in raw:
            fail("事件流里出现了密钥")
    finally:
        request("PUT", base + "/api/provider", {
            "base_url": upstream,
            "model": "demo-model",
            "api_key": SECRET,
        })


def _ask_image_case(base: str, session_id: str, provider) -> None:
    provider.mode = "ok"
    before = len(provider.requests)
    encoded = base64.b64encode(_PNG).decode("ascii")
    status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", {
        "file": encoded,
        "media_type": "image/png",
        "name": "swatch.png",
    })
    if status != 200:
        fail(f"图片附件没有收下：{status} {body}")
    record = json.loads(body)
    handle = record["handle"]
    if record.get("kind") != "image" or not handle.startswith("a_"):
        fail("图片附件的句柄或种类不对")
    status, raw = request_bytes("GET", base + f"/api/sessions/{session_id}/attachments/{handle}")
    if status != 200 or raw != _PNG:
        fail("附件预览读不到原图")
    events = _speak(base, session_id, "看这张图", attachments=[{"handle": handle}])
    if _assistant_text(events) != "你好":
        fail("带图片提问没有完成这一轮")
    if len(provider.requests) != before + 1:
        fail("带图片提问没有发给模型")
    blob = _provider_blob(provider.requests[-1])
    if "image_url" not in blob or "data:image/png;base64," not in blob:
        fail("图片没有按工件标识进入供应商请求")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    user = stored["messages"][-2]
    if user.get("role") != "user" or not any(item.get("handle") == handle for item in user.get("attachments") or []):
        fail("图片没有留在会话消息里")


def _reuse_attachment_case(base: str, directory: str) -> None:
    session_id = _fresh_session(base)
    encoded = base64.b64encode(_PNG).decode("ascii")
    payload = {"file": encoded, "media_type": "image/png", "name": "swatch.png"}
    status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", payload)
    if status != 200:
        fail(f"复用检查没有收下附件：{status} {body}")
    first = json.loads(body)
    status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", payload)
    second = json.loads(body)
    if first.get("handle") != second.get("handle"):
        fail("同一会话第二次上传同一张图没有复用句柄")
    other = _fresh_session(base)
    status, body = request("POST", base + f"/api/sessions/{other}/attachments", payload)
    third = json.loads(body)
    if third.get("handle") == first.get("handle"):
        fail("别的会话复用了附件句柄")
    status, raw = request_bytes("GET", base + f"/api/sessions/{session_id}/attachments/{first['handle']}")
    if status != 200 or raw != _PNG:
        fail("复用后的附件读不到原图")
    status, _raw = request_bytes("GET", base + f"/api/sessions/{other}/attachments/{first['handle']}")
    if status != 404:
        fail("别的会话用这枚句柄读到了附件")
    saved = json.loads(open(os.path.join(directory, "sessions", session_id + ".json"), encoding="utf-8").read())
    checksums = {item.get("checksum") for item in saved.get("attachments") or []}
    if len(checksums) != 1:
        fail("同一会话同一张图写了多份记录")


def _retrieve_case(base: str, provider, directory: str) -> None:
    provider.mode = "retrieve"
    session_id = _fresh_session(base)
    encoded = base64.b64encode(_PNG).decode("ascii")
    status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", {
        "file": encoded,
        "media_type": "image/png",
        "name": "swatch.png",
    })
    if status != 200:
        fail(f"检索检查没有收下附件：{status} {body}")
    handle = json.loads(body)["handle"]
    secret = "检索接口不该带出的本机正文甲乙"
    sessions = store.SessionStore(os.path.join(directory, "sessions"))
    sessions.add_content(session_id, secret, "keep")
    events = _speak(base, session_id, "帮我找一下 png")
    if _assistant_text(events) != "检索完成":
        fail("检索工具没有写完回答")
    listed = [event for event in events if event.get("tool") == "retrieve_local"]
    blob = json.dumps(listed, ensure_ascii=False)
    if handle not in blob or "source_id" not in blob:
        fail("检索事件没有带来源标识")
    if secret in blob or secret in json.dumps(events, ensure_ascii=False):
        fail("检索结果带回了本地正文")
    items = []
    for event in listed:
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        items.extend(result.get("items") or [])
    url = next((item.get("url") for item in items if item.get("source_id") == handle), "")
    if not isinstance(url, str) or not url:
        fail("检索结果没有可打开的地址")
    status, raw = request_bytes("GET", base + url)
    if status != 200 or raw != _PNG:
        fail("检索地址打不开原图")
    visible = request("GET", base + f"/api/sessions/{session_id}")[1]
    if secret in visible:
        fail("会话接口带回了本地正文")


def _memory_case(base: str, provider) -> None:
    provider.mode = "ok"
    session_id = _fresh_session(base)
    oldest = "最早的一句不会整段进入近期对话的闲聊甲乙丙丁戊己庚辛"
    _speak(base, session_id, oldest)
    for index in range(7):
        _speak(base, session_id, f"垫话{index}号")
    events = _speak(base, session_id, "现在问")
    if _assistant_text(events) != "你好":
        fail("长会话没有回答")
    messages = provider.requests[-1]["body"].get("messages") or []
    dialog = [item for item in messages if item.get("role") in ("user", "assistant")]
    if len(dialog) > 6:
        fail("长会话仍把全部历史发给了模型")
    system = messages[0].get("content") if messages else ""
    if "会话记忆" not in (system or ""):
        fail("长会话没有带上会话记忆")
    if oldest in json.dumps(dialog, ensure_ascii=False):
        fail("最早的整段对话仍在近期消息里")


def _vision_prompt_case(base: str, provider) -> None:
    import chat_run

    if chat_run._image_request("识别这张图片并编写提示词"):
        fail("根据图片写提示词被当成了出图")
    if chat_run._image_request("看这张图写提示词", [{"kind": "image"}]):
        fail("带图写提示词被当成了出图")
    if not chat_run._image_request("画一只茶壶"):
        fail("出图关键词没有认出画一只茶壶")
    if chat_run._image_request("用本地提示词连续生成5张图片"):
        fail("连续出图被关键词切片截走")
    if chat_run._image_request("连续生成5张图片"):
        fail("带连续的出图要求被关键词切片截走")
    if not chat_run._image_request("用本地comfyui生成一幅图片"):
        fail("单次出图关键词没有认出用本地comfyui")
    provider.mode = "ok"
    session_id = _fresh_session(base)
    encoded = base64.b64encode(_PNG).decode("ascii")
    status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", {
        "file": encoded,
        "media_type": "image/png",
        "name": "swatch.png",
    })
    if status != 200:
        fail(f"识图附件没有收下：{status} {body}")
    handle = json.loads(body)["handle"]
    events = _speak(base, session_id, "识别这张图片并编写提示词", attachments=[{"handle": handle}])
    if any(event.get("type") == "content_request" for event in events):
        fail("识图写提示词弹出了本地内容窗口")
    if _assistant_text(events) != "你好":
        fail("识图写提示词没有完成这一轮")


def _skip_content_case(base: str, provider) -> None:
    provider.mode = "image"
    session_id = _fresh_session(base)

    def on_event(event):
        if event.get("type") != "content_request":
            return
        status, body = request("POST", base + f"/api/runs/{event.get('run_id')}/content", {"skip": True})
        if status != 200:
            fail(f"取消本地内容失败：{status} {body}")

    status, raw, events = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        {"text": "画一只茶壶"},
        on_event=on_event,
        timeout=8,
    )
    if status != 200 or not events or events[-1].get("type") not in TERMINAL:
        fail("取消本地内容没有收到结束事件")
    if events[-1].get("code") != "cancelled" or events[-1].get("message") != "已取消。":
        fail("取消本地内容没有关掉这一轮")
    if any(event.get("type") == "artifact" for event in events):
        fail("取消本地内容后仍然出图")


def _confirm_reject_case(base: str, provider) -> None:
    provider.mode = "confirm"
    session_id = _fresh_session(base)
    confirmed = []

    def on_event(event):
        if event.get("type") != "tool.confirm":
            return
        confirmed.append(event.get("name"))
        _post_confirm(base, event.get("run_id") or "", False)

    status, raw, events = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        {"text": "请调用写入工具"},
        on_event=on_event,
        timeout=8,
    )
    if status != 200 or not events or events[-1].get("type") not in TERMINAL:
        fail("拒绝确认没有收到结束事件")
    if confirmed != ["generate_image"]:
        fail("写操作没有在页面上等待确认")
    if _assistant_text(events) != "没有出图":
        fail("拒绝确认后没有继续回答")
    if not any(event.get("type") == "tool.confirm" for event in events):
        fail("写操作没有发出确认事件")
    if any(event.get("type") == "artifact" for event in events):
        fail("拒绝确认后仍然出图")


def _busy_case(base: str, session_id: str, provider) -> None:
    provider.mode = "slow"
    provider.started.clear()
    box: dict = {}

    def run():
        box["events"] = read_sse(
            site_port(base),
            f"/api/sessions/{session_id}/messages",
            {"text": "占着"},
            timeout=8,
        )

    worker = threading.Thread(target=run)
    worker.start()
    if not provider.started.wait(3):
        fail("占用用例没有开始")
    before = len(provider.requests)
    status, body = request("POST", base + f"/api/sessions/{session_id}/messages", {"text": "再来一条"})
    if status != 409 or len(provider.requests) != before:
        fail("会话还在回答时又发了一次模型请求")
    request("POST", base + f"/api/sessions/{session_id}/stop", {})
    worker.join(4)
    release(provider)


def _timeout_case(base: str, session_id: str, provider, app) -> None:
    provider.mode = "hang"
    app.timeout_sec = 1.2
    started = time.monotonic()
    events = _speak(base, session_id, "等太久")
    elapsed = time.monotonic() - started
    app.timeout_sec = 5
    release(provider)
    if events[-1].get("code") != "timeout" or elapsed > 4:
        fail(f"超时没有在网关侧停下来：{events[-1].get('code')} {elapsed:.2f}s")
    if events[-1].get("retryable") is not True:
        fail("超时失败没有标成可重试")


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_COMFY_ENV = (
    "COMFY_CONFIG",
    "COMFY_URL",
    "COMFY_IMAGE_WORKFLOW",
    "COMFY_VIDEO_WORKFLOW",
    "COMFY_OUTPUT_DIR",
    "COMFY_ARTIFACT_DIR",
    "COMFY_SESSION",
)


def _png(width: int = 8, height: int = 8) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes((20, 90, 70)) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


_PNG = _png()


def _history_entry(kind: str) -> dict:
    if kind == "error":
        messages = [["execution_error", {"exception_message": "采样失败。"}]]
        status = "error"
        outputs = {}
    elif kind == "interrupted":
        messages = [["execution_interrupted", {}]]
        status = "error"
        outputs = {}
    elif kind == "success":
        messages = [["execution_success", {}]]
        status = "success"
        outputs = {
            "7": {"images": [{"filename": "poc-image_00001_.png", "subfolder": "", "type": "output"}]}
        }
    else:
        messages = []
        status = "running"
        outputs = {}
    return {
        "status": {"status_str": status, "completed": kind == "success", "messages": messages},
        "outputs": outputs,
    }


class ComfyState:
    def __init__(self):
        self.cv = threading.Condition()
        self.mode = "ok"
        self.prompts = []
        self.ids = []
        self.pending = []
        self.progress_sent = set()
        self.interrupts = []
        self.deletes = []
        self.history_hits = {}
        self.release = threading.Event()

    def set_mode(self, mode: str) -> None:
        with self.cv:
            self.mode = mode
            self.release.clear()
            self.cv.notify_all()

    def add_prompt(self, body: dict) -> str:
        with self.cv:
            prompt_id = f"p{len(self.ids) + 1}"
            self.prompts.append(body)
            self.ids.append(prompt_id)
            self.pending.append(prompt_id)
            self.cv.notify_all()
            return prompt_id

    def wait_prompt(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        with self.cv:
            while not self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cv.wait(remaining)
            return self.pending.pop(0)

    def mark_progress(self, prompt_id: str) -> None:
        with self.cv:
            self.progress_sent.add(prompt_id)

    def add_interrupt(self, prompt_id: str) -> None:
        with self.cv:
            self.interrupts.append(prompt_id)
            self.cv.notify_all()

    def wait_interrupt(self, prompt_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.cv:
            while prompt_id not in self.interrupts:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.cv.wait(remaining)
            return True

    def entry(self, prompt_id: str) -> dict:
        with self.cv:
            self.history_hits[prompt_id] = self.history_hits.get(prompt_id, 0) + 1
            hits = self.history_hits[prompt_id]
            mode = self.mode
            sent = prompt_id in self.progress_sent
            interrupted = prompt_id in self.interrupts
            released = self.release.is_set()
        if mode == "fail":
            return _history_entry("error")
        if interrupted:
            return _history_entry("interrupted")
        if mode in ("hold", "interrupt") and not released:
            return _history_entry("running")
        if not sent or hits < 2:
            return _history_entry("running")
        return _history_entry("success")


class ComfyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/ws":
            self._websocket()
            return
        if parsed.path.startswith("/history/"):
            prompt_id = urllib.parse.unquote(parsed.path[len("/history/"):])
            self._json({prompt_id: self.server.box.entry(prompt_id)})
            return
        if parsed.path == "/queue":
            self._json({"queue_running": [], "queue_pending": []})
            return
        if parsed.path == "/view":
            self._bytes(_PNG, "image/png")
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {}
        path = urllib.parse.urlparse(self.path).path
        box = self.server.box
        if path == "/prompt":
            prompt_id = box.add_prompt(body if isinstance(body, dict) else {})
            self._json({"prompt_id": prompt_id, "number": 1, "node_errors": {}})
            return
        if path == "/interrupt":
            prompt_id = body.get("prompt_id") if isinstance(body, dict) else None
            box.add_interrupt(prompt_id if isinstance(prompt_id, str) else "")
            self._json({})
            return
        if path == "/queue":
            deleted = body.get("delete") if isinstance(body, dict) else None
            box.deletes.append(deleted)
            self._json({})
            return
        self._json({"error": "not found"}, status=404)

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self._bytes(raw, "application/json", status)

    def _bytes(self, raw: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _websocket(self) -> None:
        self.close_connection = True
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        self.wfile.flush()
        box = self.server.box
        prompt_id = box.wait_prompt(5)
        if not prompt_id or box.mode == "fail":
            return
        self._frame({
            "type": "progress",
            "data": {"value": 1, "max": 20, "prompt_id": prompt_id, "node": "5"},
        })
        box.mark_progress(prompt_id)
        if box.mode != "interrupt":
            time.sleep(0.1)
            return
        if not box.wait_interrupt(prompt_id, 6):
            return
        self._frame({
            "type": "progress",
            "data": {"value": 2, "max": 20, "prompt_id": prompt_id, "node": "5"},
        })
        time.sleep(0.2)

    def _frame(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = bytearray([0x81, len(data)])
        self.wfile.write(header + data)
        self.wfile.flush()


def _image_cases(base: str, provider, directory: str) -> None:
    saved = {name: os.environ.get(name) for name in _COMFY_ENV}
    comfy = ThreadingHTTPServer(("127.0.0.1", 0), ComfyHandler)
    comfy.daemon_threads = True
    comfy.box = ComfyState()
    port = comfy.server_address[1]
    config_path = os.path.join(directory, "comfy-config.json")
    workflow = os.path.join(ROOT, "poc", "comfyui", "workflows", "image_api.json")
    fields = os.path.join(ROOT, "poc", "comfyui", "workflows", "image.fields.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump({
            "comfy_url": f"http://127.0.0.1:{port}",
            "output_dir": os.path.join(directory, "comfy-output"),
            "artifact_dir": os.path.join(directory, "comfy-artifacts"),
            "session_id": "local",
            "image": {"workflow": workflow, "fields": fields, "timeout_sec": 20},
        }, handle)
    for name in _COMFY_ENV:
        os.environ.pop(name, None)
    os.environ["COMFY_CONFIG"] = config_path
    os.environ["COMFY_URL"] = f"http://127.0.0.1:{port}"
    serve(comfy)
    try:
        status, body = request("POST", base + "/api/sessions", {})
        session_id = json.loads(body)["session_id"]
        _image_text_only(base, session_id, provider, comfy.box)
        _image_success(base, session_id, provider, comfy.box, workflow, directory)
        kept = [
            "画一只茶壶",
            "用本地comfyui生成一幅图片，内容是女仆咖啡厅",
            "再生成一张图片，提示词是繁忙咖啡馆里的女仆",
            "画一张跨会话的图",
        ]
        _image_uncensored(base, session_id, provider, comfy.box, workflow, directory)
        _image_foreign(base, session_id, provider, comfy.box, directory)
        _image_history(base, session_id, provider, comfy.box, kept)
        _image_failure(base, session_id, provider, comfy.box)
        _image_stop(base, session_id, provider, comfy.box)
        _image_disconnect(base, session_id, provider, comfy.box)
        _egress_blocks(provider)
        _local_keep(base, provider, comfy.box)
        _local_forget(base, provider, directory)
        _local_file(base, provider, comfy.box, directory)
        _local_forget_file(base, provider, comfy.box, directory)
        _local_file_image(base, provider, comfy.box)
        _egress_file(provider)
        _multi_image_case(base, provider, comfy.box, directory, workflow)
        _local_multi_image_case(base, provider, comfy.box, workflow)
        _local_early_stop_case(base, provider, comfy.box)
        provider.mode = "ok"
    finally:
        comfy.shutdown()
        comfy.server_close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _image_text_only(base: str, session_id: str, provider, box: ComfyState) -> None:
    provider.mode = "ok"
    before = len(box.prompts)
    events = _speak(base, session_id, "只聊天")
    if _assistant_text(events) != "你好" or len(box.prompts) != before:
        fail("纯文字回答仍然调用了 ComfyUI")


def _image_success(base: str, session_id: str, provider, box: ComfyState, workflow_path: str, directory: str) -> None:
    provider.mode = "image"
    box.set_mode("ok")
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak(base, session_id, "画一只茶壶", timeout=12)
    kinds = [event.get("type") for event in events]
    wanted = ["content_request", "tool_call", "progress", "artifact", "message.completed"]
    try:
        positions = [kinds.index(name) for name in wanted]
    except ValueError:
        fail(f"同一次出图的事件不齐：{kinds}")
    if positions != sorted(positions):
        fail(f"同一次出图的事件顺序不对：{kinds}")
    message_ids = {
        event.get("message_id")
        for event in events
        if event.get("type") in {"run.started", "content_request", "tool_call", "progress", "artifact", "message.completed"}
    }
    if len(message_ids) != 1:
        fail("工具、进度和成片不在同一条消息上")
    if len(box.prompts) != before + 1:
        fail("文生图没有恰好提交一次")
    new_requests = provider.requests[sent:]
    if len(new_requests) != 2:
        fail("出图的供应商请求次数不对")
    for item in new_requests:
        if "画一只茶壶" in json.dumps(item["body"], ensure_ascii=False):
            fail("画面描述进入了第三方请求")
    _assert_whitelist(box.prompts[-1], workflow_path, "画一只茶壶")
    _assert_bound(directory, session_id, provider.requests[-1]["body"], "画一只茶壶")
    if "模型不该填写的描述" in json.dumps(events, ensure_ascii=False):
        fail("模型填写的描述出现在页面上")
    artifact = next(event for event in events if event.get("type") == "artifact")
    status, raw = request_bytes("GET", base + artifact["url"])
    if status != 200 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        fail("检查或页面拿不到 artifact_id 对应的图片")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    message = stored["messages"][-1]
    if [event.get("type") for event in message.get("events") or []] != ["content_request", "tool_call", "progress", "artifact"]:
        fail(f"刷新后成片不在同一条消息上：{message.get('events')}")
    if message.get("status") != "completed":
        fail("出图完成后消息状态不对")
    missing, _ = request("GET", base + f"/sessions/{session_id}/artifacts/art_{'ab' * 16}")
    if missing != 404:
        fail("不存在的图片没有返回 404")


def _image_fields(workflow_path: str) -> dict:
    fields_path = os.path.join(os.path.dirname(workflow_path), "image.fields.json")
    with open(fields_path, encoding="utf-8") as handle:
        return json.load(handle)


def _field_input(graph: dict, fields: dict, name: str):
    spec = fields[name]
    return graph[str(spec["node"])]["inputs"][spec["input"]]


def _assert_whitelist(submitted: dict, workflow_path: str, prompt: str) -> None:
    with open(workflow_path, encoding="utf-8") as handle:
        template = json.load(handle)
    fields = _image_fields(workflow_path)
    graph = submitted.get("prompt")
    if not isinstance(graph, dict) or set(graph) != set(template):
        fail("提交的不是白名单工作流")
    for node_id, original in template.items():
        current = graph[node_id]
        if current.get("class_type") != original.get("class_type"):
            fail("提交时改了节点类型")
        if set((current.get("inputs") or {})) != set((original.get("inputs") or {})):
            fail("提交时改了节点的输入集合")
    if _field_input(graph, fields, "positive_prompt") != prompt:
        fail("提示词没有写进白名单文本节点")
    if _field_input(graph, fields, "width") != 1024 or _field_input(graph, fields, "height") != 1024:
        fail("画幅没有按模板填写")
    if _field_input(graph, fields, "seed") != 7:
        fail("seed 没有写进模板")
    neg = fields["negative_prompt"]
    if _field_input(graph, fields, "negative_prompt") != template[str(neg["node"])]["inputs"][neg["input"]]:
        fail("没有要求的负向提示词被改了")
    steps = fields["steps"]
    if _field_input(graph, fields, "steps") != template[str(steps["node"])]["inputs"][steps["input"]]:
        fail("步数被改了")
    writable = {
        (str(fields[name]["node"]), fields[name]["input"])
        for name in ("positive_prompt", "width", "height", "seed")
    }
    for node_id, original in template.items():
        current_inputs = graph[node_id].get("inputs") or {}
        for key, value in (original.get("inputs") or {}).items():
            if (str(node_id), key) in writable:
                continue
            if current_inputs.get(key) != value:
                fail("白名单之外的节点被改了")


def _image_uncensored(base: str, session_id: str, provider, box: ComfyState, workflow_path: str, directory: str) -> None:
    provider.mode = "refuse"
    box.set_mode("ok")
    before = len(box.prompts)
    text = "用本地comfyui生成一幅图片，内容是女仆咖啡厅"
    events = _speak(base, session_id, text, timeout=12)
    blob = json.dumps(events, ensure_ascii=False)
    if "无法生成" in blob or any(event.get("type") == "message.delta" for event in events):
        fail("模型拒绝出图的话被写进了页面")
    if len(box.prompts) != before + 1:
        fail("模型拒绝后没有用用户原文调用出图")
    _assert_user_prompt(box.prompts[-1], workflow_path, text, 1024)
    _assert_bound(directory, session_id, provider.requests[-1]["body"], text)
    if "画一只茶壶" in json.dumps(provider.requests[-1]["body"], ensure_ascii=False):
        fail("上一张图的描述又被发给了模型")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    if "无法生成" in json.dumps(stored["messages"][-1], ensure_ascii=False):
        fail("刷新后仍能看到模型的拒绝")

    provider.mode = "rewrite"
    box.set_mode("ok")
    before = len(box.prompts)
    text = "再生成一张图片，提示词是繁忙咖啡馆里的女仆"
    events = _speak(base, session_id, text, timeout=12)
    if len(box.prompts) != before + 1:
        fail("模型改写提示词后提交次数不对")
    _assert_user_prompt(box.prompts[-1], workflow_path, text, 1344)
    _assert_bound(directory, session_id, provider.requests[-1]["body"], text)
    sent = json.dumps(provider.requests[-1]["body"], ensure_ascii=False)
    if "健康的咖啡馆" in sent or "画一只茶壶" in sent:
        fail("改写或上一张图的描述进入了第三方请求")
    if "健康的咖啡馆" in json.dumps(events, ensure_ascii=False):
        fail("模型改写后的提示词出现在页面上")


def _image_foreign(base: str, session_id: str, provider, box: ComfyState, directory: str) -> None:
    provider.mode = "foreign"
    box.set_mode("ok")
    before = len(box.prompts)
    sent = len(provider.requests)
    text = "画一张跨会话的图"
    events = _speak(base, session_id, text, timeout=12)
    if len(box.prompts) != before:
        fail("跨会话句柄仍然提交了 ComfyUI")
    if len(provider.requests) != sent + 1:
        fail("跨会话句柄又向模型请求了一次")
    if any(event.get("type") == "content_request" for event in events):
        fail("跨会话句柄不该再向用户收集内容")
    _assert_bound(directory, session_id, provider.requests[-1]["body"], text, expect_handle=False)
    blob = json.dumps(events, ensure_ascii=False)
    if "别的画面" in blob or any(event.get("type") == "artifact" for event in events):
        fail("跨会话句柄被当成了出图")
    if not events or events[-1].get("type") != "run.failed":
        fail("跨会话句柄没有让这次运行失败")


def _image_history(base: str, session_id: str, provider, box: ComfyState, prompts: list[str]) -> None:
    provider.mode = "ok"
    before = len(box.prompts)
    events = _speak(base, session_id, "只聊天")
    if _assistant_text(events) != "你好" or len(box.prompts) != before:
        fail("隔离之后的普通聊天调用了 ComfyUI")
    blob = json.dumps(provider.requests[-1]["body"], ensure_ascii=False)
    if "只聊天" not in blob:
        fail("普通聊天没有把这句对话发给模型")
    for prompt in prompts:
        if prompt in blob:
            fail("普通聊天把先前的画面描述又发给了模型")


def _egress_blocks(provider) -> None:
    before = len(provider.requests)
    secret = "只属于本机的画面描述-出口检查"
    upstream = f"http://127.0.0.1:{provider.server_address[1]}/v1"
    gen = openai_chat.stream_chat(
        base_url=upstream,
        api_key=SECRET,
        model="demo-model",
        messages=[{"role": "user", "content": f"请画出{secret}"}],
        timeout_sec=2,
        cancel=threading.Event(),
        local_bodies=[secret],
    )
    try:
        next(gen)
        fail("含正文的请求被发给了模型")
    except openai_chat.GatewayError as exc:
        if exc.code != "local_content":
            fail(f"出口检查的错误码不对：{exc.code}")
    finally:
        gen.close()
    if len(provider.requests) != before:
        fail("出口检查失败后仍然把正文发给了供应商")
    phrase = "不要填写画面描述。"
    provider.mode = "ok"
    before = len(provider.requests)
    gen = openai_chat.stream_chat(
        base_url=upstream,
        api_key=SECRET,
        model="demo-model",
        messages=[{"role": "user", "content": "只聊天，不要出图"}],
        timeout_sec=2,
        cancel=threading.Event(),
        tools=image_run.image_tools(),
        local_bodies=[phrase],
    )
    try:
        next(gen)
    except openai_chat.GatewayError as exc:
        fail(f"工具说明里的句子被当成了本地正文：{exc.code}")
    finally:
        gen.close()
    if len(provider.requests) != before + 1:
        fail("工具说明里的句子拦住了本来可以发出的请求")
    before = len(provider.requests)
    gen = openai_chat.stream_chat(
        base_url=upstream,
        api_key=SECRET,
        model="demo-model",
        messages=[{"role": "user", "content": f"请画出{phrase}"}],
        timeout_sec=2,
        cancel=threading.Event(),
        tools=image_run.image_tools(),
        local_bodies=[phrase],
    )
    try:
        next(gen)
        fail("用户消息里的正文被发给了模型")
    except openai_chat.GatewayError as exc:
        if exc.code != "local_content":
            fail(f"用户消息里的正文没有被拦住：{exc.code}")
    finally:
        gen.close()
    if len(provider.requests) != before:
        fail("正文在用户消息里时仍然发给了供应商")


def _assert_bound(directory: str, session_id: str, body: dict, prompt: str, expect_handle: bool = True) -> None:
    blob = json.dumps(body, ensure_ascii=False)
    if prompt in blob:
        fail("画面描述进入了第三方请求")
    tools = body.get("tools") or []
    names = []
    generate = None
    accept = None
    for tool in tools:
        function = tool.get("function") or {}
        names.append(function.get("name"))
        if function.get("name") == "generate_image":
            generate = function
        if function.get("name") == "accept_local_content":
            accept = function
    if not {"accept_local_content", "list_local_content", "forget_local_content", "retrieve_local", "generate_image"}.issubset(names):
        fail(f"工具契约缺少本地内容工具：{names}")
    props = (generate.get("parameters") or {}).get("properties") or {}
    if "prompt" in props or "content_handle" not in props:
        fail(f"工具契约仍要模型填写正文：{sorted(props)}")
    if "n" in props or "count" in props:
        fail("generate_image 契约带了张数")
    blocked = {"prompt", "text", "body", "content", "bytes", "path", "filename"}
    for tool in tools:
        function = tool.get("function") or {}
        tool_props = (function.get("parameters") or {}).get("properties") or {}
        leaked = blocked.intersection(tool_props)
        if leaked:
            fail(f"工具契约带了正文或文件参数：{function.get('name')} {sorted(leaked)}")
    path = os.path.join(directory, "sessions", session_id + ".json")
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)
    matches = [item for item in saved.get("contents") or [] if item.get("text") == prompt]
    if not matches:
        fail("画面描述没有先落成本地内容")
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    for record in matches:
        handle_id = record.get("handle") or ""
        if record.get("type") != "text":
            fail("本地内容类型不对")
        if record.get("size") != len(prompt.encode("utf-8")):
            fail("本地内容没有记下大小")
        if handle_id[2:] == digest[:32]:
            fail("句柄使用了正文的哈希")
    if expect_handle and not any((record.get("handle") or "") in blob for record in matches):
        fail("请求里的句柄不是这条本地内容")
    if expect_handle and "已就绪" not in blob:
        fail("继续执行时没有带上句柄已就绪")


def _assert_user_prompt(submitted: dict, workflow_path: str, prompt: str, width: int) -> None:
    with open(workflow_path, encoding="utf-8") as handle:
        template = json.load(handle)
    fields = _image_fields(workflow_path)
    graph = submitted.get("prompt")
    if not isinstance(graph, dict) or set(graph) != set(template):
        fail("审查后的提交不是白名单工作流")
    written = _field_input(graph, fields, "positive_prompt")
    if written != prompt:
        fail(f"提交的提示词不是用户原文：{written}")
    if _field_input(graph, fields, "width") != width:
        fail("画幅没有按这次调用填写")
    for node_id, original in template.items():
        current = graph[node_id]
        if current.get("class_type") != original.get("class_type"):
            fail("提交时改了节点类型")


def _image_failure(base: str, session_id: str, provider, box: ComfyState) -> None:
    provider.mode = "image"
    box.set_mode("fail")
    before = len(box.prompts)
    events = _speak(base, session_id, "画一张失败", timeout=12)
    blob = json.dumps(events, ensure_ascii=False)
    kinds = [event.get("type") for event in events]
    if "artifact" in kinds or "message.completed" in kinds or "已经生成" in blob:
        fail("失败仍有成片，或说已经生成")
    if "tool_call" not in kinds or "error" not in kinds or events[-1].get("type") != "run.failed":
        fail(f"失败没有以 error 结束：{kinds}")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    message = stored["messages"][-1]
    stored_blob = json.dumps(message, ensure_ascii=False)
    stored_kinds = [event.get("type") for event in message.get("events") or []]
    if "artifact" in stored_kinds or "已经生成" in stored_blob or message.get("status") == "completed":
        fail("失败的消息被存成了成功")
    if len(box.prompts) != before + 1:
        fail("失败用例的提交次数不对")


def _image_stop(base: str, session_id: str, provider, box: ComfyState) -> None:
    provider.mode = "image"
    box.set_mode("interrupt")
    before = len(box.prompts)
    seen = threading.Event()
    caught = {}

    def watch(event):
        if event.get("type") == "content_request":
            _post_content(base, event.get("run_id") or "", "画一张停")
        if event.get("type") == "progress":
            seen.set()

    def run():
        caught["events"] = read_sse(
            site_port(base),
            f"/api/sessions/{session_id}/messages",
            {"text": "画一张停"},
            on_event=watch,
            timeout=12,
        )

    worker = threading.Thread(target=run)
    worker.start()
    if not seen.wait(5):
        fail("停止出图没有等到第一段进度")
    status, _body = request("POST", base + f"/api/sessions/{session_id}/stop", {})
    if status != 200:
        fail("停止出图的请求失败")
    worker.join(8)
    events = caught.get("events", (0, "", []))[2]
    progress = [event for event in events if event.get("type") == "progress"]
    if len(progress) != 1 or any(event.get("type") == "artifact" for event in events):
        fail(f"停止后仍在追加进度或记成成功：{[event.get('type') for event in events]}")
    if not events or events[-1].get("code") != "cancelled":
        fail(f"停止出图没有取消：{events[-1] if events else ''}")
    if "已经生成" in json.dumps(events, ensure_ascii=False):
        fail("停止后仍说已经生成")
    if len(box.prompts) != before + 1 or not box.ids:
        fail("停止造成了重复提交")
    prompt_id = box.ids[-1]
    if prompt_id not in box.interrupts:
        fail("停止没有取消这一次 prompt_id")
    if not any(isinstance(item, list) and prompt_id in item for item in box.deletes):
        fail("停止没有把这一次从队列里去掉")


def _image_disconnect(base: str, session_id: str, provider, box: ComfyState) -> None:
    provider.mode = "image"
    box.set_mode("hold")
    before = len(box.prompts)
    def watch(event):
        if event.get("type") == "content_request":
            _post_content(base, event.get("run_id") or "", "画一张断线")

    _status, _raw, early = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        {"text": "画一张断线"},
        on_event=watch,
        timeout=12,
        stop_types={"progress", "error", "run.failed", "message.completed"},
    )
    early_kinds = [event.get("type") for event in early]
    if "progress" not in early_kinds or "message.completed" in early_kinds or "artifact" in early_kinds:
        fail(f"断开前这次运行已经结束或没有进度：{early_kinds}")
    run_id = next(event.get("run_id") for event in early if event.get("run_id"))
    caught = {}

    def follow():
        caught["events"] = read_sse(
            site_port(base),
            f"/api/runs/{run_id}/events?after={len(early)}",
            None,
            timeout=12,
        )

    worker = threading.Thread(target=follow)
    worker.start()
    time.sleep(0.15)
    box.release.set()
    worker.join(8)
    later = caught.get("events", (0, "", []))[2]
    kinds = [event.get("type") for event in later]
    if "artifact" not in kinds or "message.completed" not in kinds:
        fail(f"接回同一次运行没有写完：{kinds}")
    if len(box.prompts) != before + 1:
        fail("页面断开后又向 ComfyUI 提交了一次")
    artifact = next(event for event in later if event.get("type") == "artifact")
    status, raw = request_bytes("GET", base + artifact["url"])
    if status != 200 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        fail("接回后拿不到这张图片")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    message = stored["messages"][-1]
    if [event.get("type") for event in message.get("events") or []] != ["content_request", "tool_call", "progress", "artifact"]:
        fail("断开后刷新，成片不在原来的消息上")


def _fresh_session(base: str) -> str:
    status, body = request("POST", base + "/api/sessions", {})
    if status != 200:
        fail("没能开一个新会话")
    return json.loads(body)["session_id"]


def _provider_blob(item: dict) -> str:
    return json.dumps(item.get("body"), ensure_ascii=False)


def _local_keep(base: str, provider, box: ComfyState) -> None:
    import chat_run

    provider.mode = "keep"
    session_id = _fresh_session(base)
    instruction = "请把一段话留在本机，稍后我再使用。"
    secret = "语义留下的本机正文甲"
    if chat_run._image_request(instruction):
        fail("语义唤起被当成了出图关键词")
    if chat_run._image_request("识别这张图片并编写提示词"):
        fail("识别图片写提示词被当成了出图关键词")
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak(base, session_id, instruction, timeout=12, content=secret)
    if not any(event.get("type") == "content_request" and event.get("kind") != "file" for event in events):
        fail("语义唤起没有出现本地文字输入")
    if _assistant_text(events) != "已记下。":
        fail("列出本地内容后没有写完回答")
    if len(box.prompts) != before:
        fail("只留下文字时调用了 ComfyUI")
    new_requests = provider.requests[sent:]
    if len(new_requests) != 3:
        fail("语义唤起的供应商请求次数不对")
    for item in new_requests:
        if secret in _provider_blob(item):
            fail("提交的文字进入了第三方请求")
    listed = json.dumps(
        [event for event in events if event.get("tool") == "list_local_content"],
        ensure_ascii=False,
    )
    handle = _content_handle(listed)
    if not handle or secret in listed:
        fail("list_local_content 没有只返回句柄")
    if secret in json.dumps(events, ensure_ascii=False):
        fail("提交的文字出现在事件里")
    visible = request("GET", base + f"/api/sessions/{session_id}")[1]
    if secret in visible:
        fail("会话接口带回了本地正文")


def _local_forget(base: str, provider, directory: str) -> None:
    provider.mode = "forget"
    session_id = _fresh_session(base)
    secret = "忘掉之后不该再读到的正文"
    sent = len(provider.requests)
    events = _speak(base, session_id, "请把一句说明留在本机，用完就丢掉。", timeout=12, content=secret)
    if _assistant_text(events) != "已忘掉。":
        fail("丢掉本地内容后没有写完回答")
    for item in provider.requests[sent:]:
        if secret in _provider_blob(item):
            fail("丢掉之前把正文发给了模型")
    if secret in json.dumps(events, ensure_ascii=False):
        fail("丢掉的正文出现在事件里")
    sessions = store.SessionStore(os.path.join(directory, "sessions"))
    if any(item.get("text") == secret for item in _saved_contents(directory, session_id)):
        fail("forget_local_content 之后还能读到正文")
    if sessions.content_text(session_id, _content_handle(json.dumps(events, ensure_ascii=False)) or "c_" + "00" * 16) is not None:
        fail("忘掉的句柄还能读到正文")


def _local_file(base: str, provider, box: ComfyState, directory: str) -> None:
    provider.mode = "keepfile"
    session_id = _fresh_session(base)
    data = "文件字节密文-甲乙丙".encode("utf-8")
    filename = "secret-name.txt"
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak_file(base, session_id, "我要上传一份资料留在本机。", data, "text/plain", filename)
    if _assistant_text(events) != "已保存文件。":
        fail("上传文件后没有写完回答")
    if len(box.prompts) != before:
        fail("上传文件时调用了 ComfyUI")
    blob = json.dumps(events, ensure_ascii=False)
    if data.decode("utf-8") in blob or filename in blob or base64.b64encode(data).decode("ascii") in blob:
        fail("文件字节或原始文件名出现在事件里")
    for item in provider.requests[sent:]:
        raw = _provider_blob(item)
        if data.decode("utf-8") in raw or filename in raw or base64.b64encode(data).decode("ascii") in raw:
            fail("文件字节或原始文件名进入了第三方请求")
    saved = _saved_contents(directory, session_id)
    matches = [item for item in saved if item.get("kind") == "file" or item.get("type") == "file"]
    if len(matches) != 1:
        fail("上传的文件没有落成本地内容")
    record = matches[0]
    handle = record.get("handle") or ""
    if record.get("purpose") != "keep" or "note-free" in json.dumps(record, ensure_ascii=False):
        fail("自由填写的用途没有被丢掉")
    if filename in json.dumps(record, ensure_ascii=False) or "text" in record:
        fail("文件记录里留下了文件名或字节")
    digest = hashlib.sha256(data).hexdigest()
    if handle[2:] == digest[:32]:
        fail("文件句柄使用了字节的哈希")
    disk = os.path.join(directory, "sessions", session_id + ".json")
    raw_session = open(disk, encoding="utf-8").read()
    if data.decode("utf-8") in raw_session or filename in raw_session:
        fail("会话记录里留下了文件字节或原始文件名")
    sessions = store.SessionStore(os.path.join(directory, "sessions"))
    if sessions.content_bytes(session_id, handle) != data:
        fail("本会话读不到刚留下的文件")
    listed = [item.get("handle") for item in sessions.list_content(session_id)]
    if handle not in listed:
        fail("list_local_content 看不到这个文件句柄")
    other = sessions.create("u_" + "ab" * 16)["session_id"]
    if handle in [item.get("handle") for item in sessions.list_content(other)]:
        fail("别的会话列出了这个文件")
    if sessions.content_bytes(other, handle) is not None or sessions.content_text(other, handle) is not None:
        fail("别的会话读到了这个文件")
    if sessions.forget_content(other, handle) != "absent":
        fail("别的会话丢掉了这个文件")
    if sessions.content_bytes(session_id, handle) != data:
        fail("别的会话把这个文件删掉了")
    checksum = record.get("checksum")
    if not isinstance(checksum, str) or len(checksum) != 64:
        fail("文件没有记下校验和")
    if sessions.artifacts.get(checksum) != data:
        fail("文件字节不在工件库")
    path = os.path.join(directory, "sessions", session_id, "files", handle)
    if os.path.isfile(path):
        fail("文件字节仍写在会话目录")


def _local_forget_file(base: str, provider, box: ComfyState, directory: str) -> None:
    provider.mode = "forgetfile"
    session_id = _fresh_session(base)
    data = b"forget-file-secret-xyz"
    before = len(box.prompts)
    events = _speak_file(base, session_id, "请收下文件再从本机会话丢掉。", data, "application/octet-stream", "gone.bin")
    if _assistant_text(events) != "已丢掉文件。":
        fail("丢掉文件后没有写完回答")
    if len(box.prompts) != before:
        fail("丢掉文件时调用了 ComfyUI")
    handle = _content_handle(json.dumps(events, ensure_ascii=False))
    sessions = store.SessionStore(os.path.join(directory, "sessions"))
    if not handle or sessions.content_bytes(session_id, handle) is not None:
        fail("forget_local_content 之后还能读到文件字节")
    if any(item.get("handle") == handle for item in sessions.list_content(session_id)):
        fail("忘掉的文件句柄还在列表里")
    if handle and os.path.exists(os.path.join(directory, "sessions", session_id, "files", handle)):
        fail("忘掉的文件还在本会话目录")
    raw = open(os.path.join(directory, "sessions", session_id + ".json"), encoding="utf-8").read()
    if data.decode("ascii") in raw:
        fail("忘掉之后会话记录里还有文件字节")


def _local_file_image(base: str, provider, box: ComfyState) -> None:
    provider.mode = "fileimage"
    session_id = _fresh_session(base)
    data = "不能拿去出图的文件字节".encode("utf-8")
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak_file(base, session_id, "请收下一份文件留在本机。", data, "application/octet-stream", "picture.bin")
    if len(box.prompts) != before:
        fail("文件句柄仍然提交了 ComfyUI")
    kinds = [event.get("type") for event in events]
    if "artifact" in kinds or events[-1].get("type") != "run.failed":
        fail("文件句柄出图没有失败")
    blob = json.dumps(events, ensure_ascii=False)
    if "文件内容不能用来生成图片。" not in blob or "不该用的文件描述" in blob:
        fail("文件句柄出图的结果不对")
    for item in provider.requests[sent:]:
        raw = _provider_blob(item)
        if data.decode("utf-8") in raw or base64.b64encode(data).decode("ascii") in raw:
            fail("文件字节进入了出图前的供应商请求")


def _multi_image_case(base: str, provider, box: ComfyState, directory: str, workflow_path: str) -> None:
    import chat_run

    if chat_run._image_request("用本地提示词连续生成5张图片"):
        fail("连续出图被关键词切片截走")
    if not chat_run._image_request("画一只茶壶"):
        fail("单次出图关键词没有认出画一只茶壶")
    provider.mode = "multiimage"
    box.set_mode("ok")
    session_id = _fresh_session(base)
    secret = "连续出图本机提示词-不应外发"
    sessions = store.SessionStore(os.path.join(directory, "sessions"))
    record = sessions.add_content(session_id, secret, "image_prompt")
    handle = record.get("handle") or ""
    if not handle:
        fail("连续出图没有先留下文字句柄")
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak(base, session_id, "用本地提示词连续生成5张图片", timeout=25)
    kinds = [event.get("type") for event in events]
    arts = [event for event in events if event.get("type") == "artifact"]
    gens = [event for event in events if event.get("type") == "tool_call" and event.get("tool") == "generate_image"]
    if any(event.get("type") == "content_request" for event in events):
        fail("已有提示词还去收集正文")
    if len(arts) != 5:
        fail(f"连续出图没有五张成片：{len(arts)} {kinds}")
    if len(gens) < 5:
        fail(f"连续出图没有五次 generate_image：{len(gens)}")
    if len(box.prompts) != before + 5:
        fail("连续出图没有恰好提交五次")
    art_at = [index for index, kind in enumerate(kinds) if kind == "artifact"]
    done_at = [index for index, kind in enumerate(kinds) if kind == "message.completed"]
    if not art_at or not done_at:
        fail("连续出图缺少成片或结束事件")
    if done_at[0] < art_at[1]:
        fail("第一张图后运行就结束了")
    if _assistant_text(events) != "五张都好了":
        fail("连续出图没有等模型调度完再回答")
    if len(provider.requests) != sent + 7:
        fail(f"连续出图的供应商请求次数不对：{len(provider.requests) - sent}")
    ids = {event.get("artifact_id") for event in arts}
    if len(ids) != 5 or any(not item for item in ids):
        fail("连续出图的成片标识重复或缺失")
    message_ids = {event.get("message_id") for event in arts if event.get("message_id")}
    if len(message_ids) != 1:
        fail("连续出图的成片不在同一条消息上")
    for item in provider.requests[sent:]:
        blob = _provider_blob(item)
        if secret in blob:
            fail("连续出图把提示词正文发给了模型")
        tools = item["body"].get("tools") or []
        for tool in tools:
            function = tool.get("function") or {}
            if function.get("name") != "generate_image":
                continue
            props = (function.get("parameters") or {}).get("properties") or {}
            if "n" in props or "count" in props or "prompt" in props:
                fail(f"连续出图的工具契约带了张数或正文：{sorted(props)}")
    _assert_whitelist(box.prompts[-1], workflow_path, secret)
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    message = stored["messages"][-1]
    stored_arts = [event for event in (message.get("events") or []) if event.get("type") == "artifact"]
    if len(stored_arts) != 5:
        fail("刷新后连续出图的成片不齐")
    if message.get("status") != "completed":
        fail("连续出图完成后消息状态不对")
    if secret in json.dumps(events, ensure_ascii=False) or secret in json.dumps(message, ensure_ascii=False):
        fail("连续出图把提示词正文写进了页面")
    if handle:
        for item in provider.requests[sent:]:
            if handle in _provider_blob(item):
                break
        else:
            fail("连续出图没有把文字句柄交给模型")


def _local_multi_image_case(base: str, provider, box: ComfyState, workflow_path: str) -> None:
    provider.mode = "localmulti"
    box.set_mode("ok")
    session_id = _fresh_session(base)
    secret = "当场留下的连续出图提示词-不应外发"
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak(base, session_id, "用本地提示词连续生成5张图片", timeout=25, content=secret)
    kinds = [event.get("type") for event in events]
    if "content_request" not in kinds:
        fail(f"没有句柄时连续出图没有先询问本地提示词：{kinds}")
    asked = kinds.index("content_request")
    if any(event.get("type") == "tool.confirm" and event.get("name") == "accept_local_content" for event in events):
        fail("收集本地提示词还要先过工具确认，输入框会被挡住")
    if any(event.get("status") == "waiting_confirm" for event in events[:asked]):
        fail("收集本地提示词前留下了等待确认的工具调用")
    script = request("GET", base + "/static/app.js")[1]
    if 'lastIndexOf("content_request")' not in script or "events.slice(askedAt + 1)" not in script:
        fail("页面的输入框条件还会被收集前的工具调用挡住")
    arts = [index for index, kind in enumerate(kinds) if kind == "artifact"]
    if len(arts) != 5 or arts[0] < asked:
        fail(f"收下本地提示词后没有连续五张成片：{len(arts)} {kinds}")
    if len(box.prompts) != before + 5:
        fail("收下本地提示词后没有恰好提交五次")
    confirms = [event for event in events if event.get("type") == "tool.confirm" and event.get("name") == "generate_image"]
    if len(confirms) > 1:
        fail(f"连续出图每张都要确认，第二张会卡在等待确认：{len(confirms)}")
    if 'includes(entry.type));' not in script:
        fail("成片之后页面还会显示上一张的旧进度")
    for submitted in box.prompts[before:]:
        _assert_whitelist(submitted, workflow_path, secret)
    if _assistant_text(events) != "五张都好了":
        fail("收下本地提示词后没有等模型调度完再回答")
    if events[-1].get("type") != "message.completed":
        fail("收下本地提示词后连续出图没有正常结束")
    if len(provider.requests) != sent + 7:
        fail(f"收下本地提示词后供应商请求次数不对：{len(provider.requests) - sent}")
    for item in provider.requests[sent:]:
        if secret in _provider_blob(item):
            fail("收下本地提示词后把正文发给了模型")
    if secret in json.dumps(events, ensure_ascii=False):
        fail("收下本地提示词后把正文写进了页面")


def _local_early_stop_case(base: str, provider, box: ComfyState) -> None:
    import chat_run

    for text, want in (("连续生成5张图片", 5), ("生成两张图", 2), ("画十张", 10), ("画一只猫", 0), ("来十二幅", 12)):
        if chat_run._requested_count(text) != want:
            fail(f"读不出要几张：{text} -> {chat_run._requested_count(text)}")
    provider.mode = "localstop"
    box.set_mode("ok")
    session_id = _fresh_session(base)
    secret = "模型半路收尾的本地提示词-不应外发"
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak(base, session_id, "帮我连续生成5张图片，先输入本地提示词然后生成", timeout=30, content=secret)
    kinds = [event.get("type") for event in events]
    arts = [kind for kind in kinds if kind == "artifact"]
    if len(arts) != 5 or len(box.prompts) != before + 5:
        fail(f"模型出一两张就收尾时没有续到五张：{len(arts)} {len(box.prompts) - before} {kinds}")
    if events[-1].get("type") != "message.completed":
        fail(f"模型半路收尾后连续出图没有正常结束：{events[-1]}")
    blob = json.dumps([item.get("body") for item in provider.requests[sent:]], ensure_ascii=False)
    if "已生成 1/5 张" not in blob or "images_remaining" not in blob:
        fail("没有把进度和续写指令告诉模型")
    if secret in blob:
        fail("续写时把本地正文发给了模型")


def _speak_file(base: str, session_id: str, text: str, data: bytes, media_type: str, filename: str):
    def on_event(event):
        if event.get("type") == "tool.confirm":
            _post_confirm(base, event.get("run_id") or "", True)
            return
        if event.get("type") != "content_request":
            return
        if event.get("kind") != "file":
            fail("文件收集没有标明种类")
        status, body = request("POST", base + f"/api/runs/{event.get('run_id')}/content", {
            "file": base64.b64encode(data).decode("ascii"),
            "media_type": media_type,
            "filename": filename,
        })
        if status != 200:
            fail(f"提交本地文件失败：{status} {body}")

    status, raw, events = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        {"text": text},
        on_event=on_event,
        timeout=12,
    )
    if status != 200 or not events or events[-1].get("type") not in TERMINAL:
        fail("发送文件收集没有收到结束事件")
    return events


def _saved_contents(directory: str, session_id: str) -> list:
    path = os.path.join(directory, "sessions", session_id + ".json")
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)
    return saved.get("contents") or []


def _egress_file(provider) -> None:
    import chat_run

    directory = tempfile.mkdtemp(prefix="chat-file-egress-")
    try:
        sessions = store.SessionStore(os.path.join(directory, "sessions"))
        session_id = sessions.create("u_" + "cd" * 16)["session_id"]
        data = b"file-bytes-secret-xyz"
        sessions.add_file(session_id, data, "text/plain", "keep")
        bodies = sessions.local_bodies(session_id)
        encoded = base64.b64encode(data).decode("ascii")
        if data.decode("ascii") not in bodies or encoded not in bodies:
            fail("文件字节没有进入出口检查")
        session = sessions.get(session_id)
        session["messages"] = [{
            "role": "user",
            "message_id": "m_" + "12" * 16,
            "text": "附上 " + data.decode("ascii") + " " + encoded,
            "status": "completed",
        }]
        messages = chat_run._model_messages(session, extra_bodies=sessions.secret_pairs(session_id))
        history = json.dumps(messages, ensure_ascii=False)
        if data.decode("ascii") in history or encoded in history:
            fail("文件字节进入了发给模型的历史")
        upstream = f"http://127.0.0.1:{provider.server_address[1]}/v1"
        for secret in (data.decode("ascii"), encoded):
            before = len(provider.requests)
            gen = openai_chat.stream_chat(
                base_url=upstream,
                api_key=SECRET,
                model="demo-model",
                messages=[{"role": "user", "content": secret}],
                timeout_sec=2,
                cancel=threading.Event(),
                local_bodies=bodies,
            )
            try:
                next(gen)
                fail("含文件字节的请求被发给了模型")
            except openai_chat.GatewayError as exc:
                if exc.code != "local_content":
                    fail(f"文件出口检查的错误码不对：{exc.code}")
            finally:
                gen.close()
            if len(provider.requests) != before:
                fail("文件出口检查失败后仍然把字节发给了供应商")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def request_bytes(method: str, url: str, timeout: float = 5):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def site_port(base: str) -> int:
    return int(base.rsplit(":", 1)[1])


def main() -> None:
    self_check()
    print("check ok")


if __name__ == "__main__":
    main()
