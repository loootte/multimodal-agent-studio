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
from services.gateway import openai_chat

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
        self._piece(b"")

    def _last_user(self) -> str:
        messages = self.server.requests[-1]["body"].get("messages") or []
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "user":
                return item.get("content") or ""
        return ""

    def _ready(self) -> bool:
        return "已就绪" in self._last_user()

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
        _saved_env_rules()
        _provider_input_rules()
        _recovery()
        _redact_unit()
        _redact_history()
        _live()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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
    if 'method="dialog"' in page or "settings-error" not in page:
        fail("设置保存失败时对话框会自己关掉")
    status, script = request("GET", base + "/static/app.js")
    if status != 200 or "message.delta" not in script or "tool_call" not in script or "Enter" not in script:
        fail("页面脚本没有接上流式事件")
    if "innerHTML" in script or "regenerate" in script or "use_as_reference" in script or "change_aspect" in script:
        fail("页面多做了卡片动作，或用 innerHTML 塞进了内容")
    if "content_request" not in script or "加入本地内容并继续" not in script:
        fail("页面没有本地内容输入")
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
    status, again = request("PUT", base + "/api/provider", {"base_url": upstream, "model": "demo-model"})
    if json.loads(again).get("key_hint") != "9876":
        fail("留空密钥时把已保存的密钥清掉了")

    provider.mode = "ok"
    events = _speak(base, session_id, "第一句")
    if _assistant_text(events) != "你好":
        fail("流式增量没有拼成同一条回答")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    if stored["messages"][-1]["text"] != "你好" or stored["messages"][-1]["status"] != "completed":
        fail("刷新后回答不在")
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

    _stop_case(base, session_id, provider)
    _hold_case(base, session_id, provider)
    _reject_key_case(base, session_id, provider, directory)
    _reject_file_case(base, session_id, provider)
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


def _post_content(base: str, run_id: str, text: str) -> None:
    status, body = request("POST", base + f"/api/runs/{run_id}/content", {"text": text})
    if status != 200:
        fail(f"提交本地内容失败：{status} {body}")


def _speak(base: str, session_id: str, text: str, timeout: float = 8, content: str | None = None):
    def on_event(event):
        if event.get("type") == "content_request":
            _post_content(base, event.get("run_id") or "", content if content is not None else text)

    status, raw, events = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        {"text": text},
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
    status, raw, events = read_sse(
        site_port(base),
        f"/api/sessions/{session_id}/messages",
        {"text": "看这张图", "attachments": [{"type": "image"}]},
    )
    if status != 200 or events[-1].get("code") != "modality_unsupported":
        fail("图片没有被拒绝")
    if len(provider.requests) != before:
        fail("不接受的附件仍然发给了模型")
    if raw and SECRET in raw:
        fail("事件流里出现了密钥")
    status, raw = request("POST", base + f"/api/sessions/{session_id}/messages", {"text": "  "})
    if status != 400:
        fail("空消息没有被拒绝")


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


def _assert_whitelist(submitted: dict, workflow_path: str, prompt: str) -> None:
    with open(workflow_path, encoding="utf-8") as handle:
        template = json.load(handle)
    graph = submitted.get("prompt")
    if not isinstance(graph, dict) or set(graph) != set(template):
        fail("提交的不是白名单工作流")
    for node_id, original in template.items():
        current = graph[node_id]
        if current.get("class_type") != original.get("class_type"):
            fail("提交时改了节点类型")
        if set((current.get("inputs") or {})) != set((original.get("inputs") or {})):
            fail("提交时改了节点的输入集合")
    if graph["2"]["inputs"]["text"] != prompt:
        fail("提示词没有写进白名单文本节点")
    if graph["4"]["inputs"]["width"] != 1024 or graph["4"]["inputs"]["height"] != 1024:
        fail("画幅没有按模板填写")
    if graph["5"]["inputs"]["seed"] != 7:
        fail("seed 没有写进模板")
    if graph["3"]["inputs"]["text"] != template["3"]["inputs"]["text"]:
        fail("没有要求的负向提示词被改了")
    if graph["5"]["inputs"]["steps"] != template["5"]["inputs"]["steps"]:
        fail("步数被改了")
    if graph["1"]["inputs"] != template["1"]["inputs"]:
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
    if accept is None or generate is None:
        fail(f"工具契约缺少本地内容工具：{names}")
    props = (generate.get("parameters") or {}).get("properties") or {}
    if "prompt" in props or "content_handle" not in props:
        fail(f"工具契约仍要模型填写正文：{sorted(props)}")
    accept_props = (accept.get("parameters") or {}).get("properties") or {}
    if any(name in accept_props for name in ("prompt", "text", "body", "content")):
        fail("接受本地内容的工具带了正文参数")
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
    graph = submitted.get("prompt")
    if not isinstance(graph, dict) or set(graph) != set(template):
        fail("审查后的提交不是白名单工作流")
    if graph["2"]["inputs"]["text"] != prompt:
        fail(f"提交的提示词不是用户原文：{graph['2']['inputs']['text']}")
    if graph["4"]["inputs"]["width"] != width:
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
