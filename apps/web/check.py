"""不访问外网的聊天检查。假供应商只听 127.0.0.1。"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(WEB_DIR, "..", ".."))
sys.path.insert(0, WEB_DIR)
sys.path.insert(0, ROOT)

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
        self._piece(b"")

    def _piece(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _done(self) -> None:
        self._piece(b"data: [DONE]\n\n")
        self._piece(b"")


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


def read_sse(port: int, path: str, payload: dict | None, on_event=None, timeout: float = 8):
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
                if event.get("type") in TERMINAL:
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
    if status != 200 or "message.delta" not in script or "Enter" not in script:
        fail("页面脚本没有接上流式事件")
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


def _speak(base: str, session_id: str, text: str):
    status, raw, events = read_sse(site_port(base), f"/api/sessions/{session_id}/messages", {"text": text})
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


def site_port(base: str) -> int:
    return int(base.rsplit(":", 1)[1])


def main() -> None:
    self_check()
    print("check ok")


if __name__ == "__main__":
    main()
