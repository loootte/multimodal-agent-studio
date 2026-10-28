"""不访问外网的聊天检查。假供应商只听 127.0.0.1。"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import struct
import subprocess
import socket
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
            usage = {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}
            self._piece(f"data: {json.dumps(usage)}\n\n".encode("utf-8"))
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
        if mode == "named":
            self._emit_tool("generate_image", {
                "content_handle": getattr(self.server, "named", "") or ("c_" + "cd" * 16),
                "prompt": "模型不该填写的描述",
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
        if mode == "character":
            self._character()
            return
        if mode == "imageskill":
            self._image_skill()
            return
        if mode == "localplan":
            # 文字里没有数字，张数由模型理解后用 plan_images 报出。
            count = self._tool_result_count("generate_image")
            if count >= 4:
                self._piece(_delta(content="四季都好了"))
                self._done()
                return
            handle = _content_handle(json.dumps(self.server.requests[-1]["body"], ensure_ascii=False))
            if not handle and self._tool_result_count("plan_images") == 0:
                self._emit_tool("plan_images", {"count": 4, "items": ["春", "夏", "秋", "冬"]})
                return
            if not handle:
                self._emit_tool("accept_local_content", {"purpose": "image_prompt", "kind": "text"})
                return
            if count == 1 and self._last_user().startswith("工具结果 generate_image"):
                self._piece(_delta(content="春天这张好了。"))
                self._done()
                return
            self._emit_tool("generate_image", {"content_handle": handle, "aspect_ratio": "1:1", "seed": 7})
            return
        if mode == "localask":
            # 复现真实模型：第一步只回文字，让用户把提示词贴进聊天，还追问变体。
            body = json.dumps(self.server.requests[-1]["body"], ensure_ascii=False)
            if "这是出图请求" not in body and self._tool_result_count("plan_images") == 0 and not _content_handle(body):
                self._piece(_delta(content="请把提示词直接发给我，我会先用 accept_local_content 留在本机。要同一提示词还是变体？"))
                self._done()
                return
            mode = "localmulti"
        if mode == "localmulti":
            if self._tool_result_count("generate_image") >= 5:
                self._piece(_delta(content="五张都好了"))
                self._done()
                return
            handle = _content_handle(json.dumps(self.server.requests[-1]["body"], ensure_ascii=False))
            if not handle and self._tool_result_count("plan_images") == 0:
                self._emit_tool("plan_images", {"count": 5})
                return
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
            if not handle and self._tool_result_count("plan_images") == 0:
                self._emit_tool("plan_images", {"count": 5})
                return
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
        if mode == "multi":
            messages = self.server.requests[-1]["body"].get("messages") or [{}]
            system = _user_text(messages[0].get("content")) if messages[0].get("role") == "system" else ""
            if "你是编排者" in system:
                if self._tool_result_count("delegate"):
                    self._piece(_delta(content="多智能体完成"))
                    self._done()
                    return
                self._emit_tool("delegate", {"tasks": [{"agent": "planner", "task": "规划两张图", "inputs": []}]})
                return
            if "你是规划 Agent" in system:
                if self._tool_result_count("plan_images"):
                    self._piece(_delta(content="计划好了：两张"))
                    self._done()
                    return
                self._emit_tool("plan_images", {"count": 2, "items": ["甲", "乙"]})
                return
            self._piece(_delta(content="不该被调用"))
            self._done()
            return
        if mode == "multitext":
            messages = self.server.requests[-1]["body"].get("messages") or [{}]
            system = _user_text(messages[0].get("content")) if messages[0].get("role") == "system" else ""
            if "你是编排者" in system:
                done = self._tool_result_count("delegate")
                order = ["planner", "writer", "reviewer"]
                if done >= len(order):
                    self._piece(_delta(content="# 终稿\n\n- 第一节\n- 第二节"))
                    self._done()
                    return
                self._emit_tool("delegate", {"tasks": [{"agent": order[done], "task": "写一篇短文：" + order[done]}]})
                return
            reply = {"你是规划 Agent": "- 提纲一\n- 提纲二", "你是写作 Agent": "## 草稿\n正文", "你是检查 Agent": "通过\n结构清楚"}
            for marker, text in reply.items():
                if marker in system:
                    self._piece(_delta(content=text))
                    self._done()
                    return
            self._piece(_delta(content="不该被调用"))
            self._done()
            return
        if mode == "lazy":
            # 想一步答完的编排者：只有被 tool_choice 点名强制时才委托，委托谁听运行时的提示。
            body = self.server.requests[-1]["body"]
            messages = body.get("messages") or [{}]
            system = _user_text(messages[0].get("content")) if messages[0].get("role") == "system" else ""
            if "你是编排者" in system:
                forced = (body.get("tool_choice") or {}) if isinstance(body.get("tool_choice"), dict) else {}
                hint = re.search(r"下一步委托 (\w+)", system)
                if forced.get("function", {}).get("name") == "delegate" and hint:
                    self._emit_tool("delegate", {"tasks": [{"agent": hint.group(1), "task": "城市骑行：" + hint.group(1)}]})
                    return
                self._piece(_delta(content="我直接回答：骑行很好。" if "你好" not in self._last_user() else "你好！"))
                self._done()
                return
            reply = {"你是规划 Agent": "- 提纲一", "你是写作 Agent": "## 草稿", "你是检查 Agent": "通过"}
            for marker, text in reply.items():
                if marker in system:
                    self._piece(_delta(content=text))
                    self._done()
                    return
            self._piece(_delta(content="不该被调用"))
            self._done()
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

    def _user_texts(self) -> list[str]:
        out = []
        for item in self.server.requests[-1]["body"].get("messages") or []:
            if isinstance(item, dict) and item.get("role") == "user":
                out.append(_user_text(item.get("content")))
        return out

    def _image_skill(self) -> None:
        """#49：按用户原话决定来源、后端、张数，像真实模型那样只用工具。"""
        texts = self._user_texts()
        ask = next((t for t in reversed(texts) if "工具结果" not in t and "已就绪" not in t and "这是出图请求" not in t), "")
        failed = any("工具结果 generate_image" in t and '"ok": false' in t for t in texts)
        planned = 3 if "三张" in ask else 0
        target = planned or 1
        if failed or self._tool_result_count("generate_image") >= target:
            self._piece(_delta(content="好了"))
            self._done()
            return
        if planned and self._tool_result_count("plan_images") == 0:
            self._emit_tool("plan_images", {"count": planned})
            return
        if "原样" in ask or "优化提示词" in ask:
            if self._tool_result_count("stage_prompt") == 0:
                if "原样" in ask:
                    visible = ask.split("：", 1)[-1].strip()
                    self._emit_tool("stage_prompt", {"visible_prompt": visible, "source": "chat"})
                else:
                    visible = "模型写的提示词：" + ask.split("：", 1)[-1].strip() + "，电影感光影"
                    if "洗白" in ask:
                        visible = ask.split("洗白", 1)[-1].strip()
                    self._emit_tool("stage_prompt", {"visible_prompt": visible, "source": "model"})
                return
            staged = next((t for t in reversed(texts) if "工具结果 stage_prompt" in t), "")
            if '"ok": false' in staged:
                self._piece(_delta(content="不能这样登记"))
                self._done()
                return
            handle = _content_handle(staged)
        else:
            handle = next((_content_handle(t) for t in reversed(texts) if _content_handle(t)), "")
            if not handle:
                self._emit_tool("accept_local_content", {"purpose": "image_prompt", "kind": "text"})
                return
        args = {"content_handle": handle or ("c_" + "00" * 16)}
        if "第三方" in ask:
            args["backend"] = "remote"
        self._emit_tool("generate_image", args)

    def _character(self) -> None:
        """#53：模型只填用户说到的字段，拿到句柄再出图。"""
        texts = self._user_texts()
        if self._tool_result_count("generate_image") >= 1:
            self._piece(_delta(content="好了"))
            self._done()
            return
        staged = next((t for t in reversed(texts) if "工具结果 character_image" in t), "")
        if not staged:
            self._emit_tool("character_image", {"hair_color": "红发", "outfit": "knight_armor"})
            return
        if '"ok": false' in staged:
            self._piece(_delta(content="不能这样画"))
            self._done()
            return
        self._emit_tool("generate_image", {"content_handle": _content_handle(staged)})

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
        _toolpack_cases()
        _agent_cases()
        _tool_pack_guide_cases()
        _live()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _agent_cases() -> None:
    """ADR 0005：清单校验、内置发现、安装导出、委托层数/成环/预算、越权、并行与后端串行、取消。"""
    import io
    import zipfile
    from services.runtime import agents

    known = agents.registered_tools()
    names = sorted(agent.name for agent in agents.discover())
    if not {"image", "orchestrator", "planner", "reviewer", "writer"} <= set(names):
        fail(f"内置 Agent 没有全部发现：{names}")
    good = {"schema": "mas.agent/1", "name": "critic", "version": "0.1.0", "description": "只读检查",
            "role": "reviewer", "instructions": "只读检查。", "tools": ["list_local_content"], "budget": {"steps": 4}}
    if agents.validate_manifest(good, known_tools=known):
        fail(f"合法清单被拒：{agents.validate_manifest(good, known_tools=known)}")
    bad_cases = {
        "装上编排者": dict(good, role="orchestrator"),
        "装上委托": dict(good, tools=["delegate"]),
        "子 Agent 弹本地内容": dict(good, tools=["accept_local_content"]),
        "未注册的工具": dict(good, tools=["rm_rf"]),
        "子 Agent 的树预算": dict(good, budget={"tree_steps": 10}),
        "预算越界": dict(good, budget={"steps": 999}),
        "多余字段": dict(good, permissions=["net"]),
        "坏名字": dict(good, name="Bad Name"),
    }
    for label, data in bad_cases.items():
        if not agents.validate_manifest(data, known_tools=known):
            fail(f"清单校验没拦住：{label}")
    # docs/agents.md 里的示例清单要能通过校验。
    guide = open(os.path.join(ROOT, "docs", "agents.md"), encoding="utf-8").read()
    blocks = re.findall(r"```json agent\n(.*?)```", guide, re.S)
    if not blocks:
        fail("docs/agents.md 没有可校验的示例清单")
    for block in blocks:
        problems = agents.validate_manifest(json.loads(block), known_tools=known)
        if problems:
            fail(f"docs/agents.md 的示例清单不合法：{problems}")
    # 安装、导出、卸载往返。
    saved = os.environ.get("MAS_AGENTS_DIR")
    os.environ["MAS_AGENTS_DIR"] = tempfile.mkdtemp(prefix="agents-")
    try:
        folder = tempfile.mkdtemp(prefix="agent-src-")
        with open(os.path.join(folder, "agent.json"), "w", encoding="utf-8") as handle:
            json.dump(good, handle, ensure_ascii=False)
        view = agents.inspect(folder)
        if view.get("name") != "critic":
            fail(f"检查 Agent 包不对：{view}")
        agents.install(folder)
        if not agents.find("critic"):
            fail("安装后找不到 Agent")
        blob = agents.export("critic")
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            if not any(item.endswith("agent.json") for item in archive.namelist()):
                fail("导出的包里没有 agent.json")
        agents.install(folder)  # 同名再装就是升级，不报错
        try:
            agents.uninstall("planner")
            fail("内置 Agent 能被卸载")
        except agents.AgentError:
            pass
        agents.uninstall("critic")
        if agents.find("critic"):
            fail("卸载后 Agent 还在")
    finally:
        if saved is None:
            os.environ.pop("MAS_AGENTS_DIR", None)
        else:
            os.environ["MAS_AGENTS_DIR"] = saved
    # 工具可见性：子 Agent 永远没有 accept_local_content；到最深一层没有 delegate。
    planner = agents.find("planner")
    lead = agents.find("orchestrator")
    if "accept_local_content" in agents.allowed_tools(planner, depth=1):
        fail("子 Agent 拿到了 accept_local_content")
    if "delegate" not in agents.allowed_tools(lead, depth=0) or "delegate" in agents.allowed_tools(lead, depth=agents.MAX_DEPTH):
        fail("delegate 的层数限制不对")
    if agents.not_allowed("generate_image", "planner")["error"]["code"] != "tool_not_allowed":
        fail("越权工具的错误码不对")
    # 委托：成环、层数、未知 Agent、只放句柄、预算池。
    calls = []

    def spawn(agent, text, **kw):
        calls.append((agent.name, kw["depth"], kw["parent_run_id"]))
        return {"status": "completed", "text": "好了" * 1000, "steps": 1, "artifacts": ["a_1"]}

    pool = agents.Pool(tree_steps=10, delegations=2)
    top = agents.Delegator(spawn=spawn, parent_run_id="r_top", pool=pool)
    out = top({"tasks": [{"agent": "planner", "task": "规划"}]})
    if not out["ok"] or calls[-1] != ("planner", 1, "r_top") or len(out["results"][0]["summary"]) > agents.SUMMARY_CHARS + 1:
        fail(f"委托结果不对：{out}")
    if top({"tasks": [{"agent": "orchestrator", "task": "x"}]})["error"]["code"] != "unknown_agent":
        fail("能委托给编排者")
    if top({"tasks": [{"agent": "nobody", "task": "x"}]})["error"]["code"] != "unknown_agent":
        fail("未知 Agent 没报错")
    if top({"tasks": [{"agent": "image", "task": "x", "inputs": ["一段 画面 描述"]}]})["error"]["code"] != "invalid_tool_call":
        fail("inputs 里塞了正文也放行了")
    if top({"tasks": [{"agent": "planner", "task": "x"}] * 5})["error"]["code"] != "invalid_tool_call":
        fail("一次委托超过任务上限也放行了")
    cyc = agents.Delegator(spawn=spawn, parent_run_id="r_c", pool=pool, chain=("orchestrator", "planner"), depth=1)
    if cyc({"tasks": [{"agent": "planner", "task": "x"}]})["error"]["code"] != "delegation_cycle":
        fail("委托成环没拦住")
    deep = agents.Delegator(spawn=spawn, parent_run_id="r_d", pool=pool, depth=agents.MAX_DEPTH)
    if deep({"tasks": [{"agent": "planner", "task": "x"}]})["error"]["code"] != "delegation_depth":
        fail("委托层数没拦住")
    top({"tasks": [{"agent": "reviewer", "task": "x"}]})
    spent = top({"tasks": [{"agent": "reviewer", "task": "x"}]})
    if spent["results"][0].get("error", {}).get("code") != "budget_exceeded":
        fail(f"委托次数预算没生效：{spent}")
    # 并行有上限，同一后端串行。
    live = {"now": 0, "max": 0, "backend_now": 0, "backend_max": 0}
    lock = threading.Lock()

    def slow_spawn(agent, text, **kw):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])

        def work():
            with lock:
                live["backend_now"] += 1
                live["backend_max"] = max(live["backend_max"], live["backend_now"])
            time.sleep(0.05)
            with lock:
                live["backend_now"] -= 1

        agents.serialized("generate_image", work)
        time.sleep(0.05)
        with lock:
            live["now"] -= 1
        return {"status": "completed", "text": "", "steps": 1}

    par = agents.Delegator(spawn=slow_spawn, parent_run_id="r_p", pool=agents.Pool(40, 10))
    out = par({"tasks": [{"agent": "image", "task": str(i)} for i in range(4)], "parallel": True})
    if not out["ok"] or live["max"] > agents.MAX_PARALLEL or live["max"] < 2 or live["backend_max"] != 1:
        fail(f"并行或后端串行不对：{live}")
    # 取消向下传：已取消时不再起子 Agent。
    cancel = threading.Event()
    cancel.set()
    stopped = agents.Delegator(spawn=spawn, parent_run_id="r_x", pool=agents.Pool(10, 10), cancel=cancel)
    before = len(calls)
    out = stopped({"tasks": [{"agent": "planner", "task": "x"}]})
    if len(calls) != before or out["results"][0]["status"] != "cancelled":
        fail("取消后还起了子 Agent")


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
        _gateway_egress(provider, upstream)
        _gateway_hardening(provider, upstream)
    finally:
        install_defaults()
        provider.shutdown()
        provider.server_close()


def _gateway_hardening(provider, upstream: str) -> None:
    """#59（12.C）：结构化出站策略、规范化/编码检测、短正文指纹、统一中间件强制。"""
    import base64
    import json as _json
    from packages.contracts import events as contract
    from services import telemetry
    from services.gateway import egress, registry, install_defaults
    from services.gateway import stream as gateway_stream

    for code in ("local_content", "local_content_encoded", "egress_policy_denied"):
        if code not in contract.ERROR_CODES or code not in contract.EGRESS_REASONS or code not in contract.HUMAN:
            fail(f"错误码 {code} 没有登记全")
        if contract.RETRYABLE.get(code) is not False:
            fail(f"{code} 不应可重试")
    for code in ("local_content_encoded", "egress_policy_denied"):
        if telemetry.FAILURE_CLASSES.get(code) != "policy":
            fail(f"{code} 的失败分类不对")
    for key in ("policy", "exempt_bytes"):
        if key not in contract.EGRESS_FIELDS:
            fail(f"出站记录缺字段 {key}")
    if set(contract.EGRESS_POLICIES) != set(contract.EGRESS_CHANNELS):
        fail("每个出站渠道都要有一条策略")
    if contract.EGRESS_POLICIES["other"] is not None:
        fail("其他出口默认应拒绝")
    if contract.EGRESS_POLICIES["remote_image"].get("max_images") != 1:
        fail("第三方出图一次只能一张")
    with open(os.path.join(ROOT, "apps", "web", "static", "app.js"), encoding="utf-8") as handle:
        js = handle.read()
    for word in ("local_content_encoded", "egress_policy_denied", "其他出口"):
        if word not in js:
            fail(f"前端没有 {word} 的说明")

    body = "私人日记 LOCAL-HARDEN-UNIT-77c2"
    raw = body.encode("utf-8")
    cases = {
        body.upper(): "local_content",
        "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in body): "local_content",
        "\u200b".join(body): "local_content",
        base64.b64encode(raw).decode(): "local_content_encoded",
        base64.urlsafe_b64encode(raw).decode().rstrip("="): "local_content_encoded",
        "".join(f"%{b:02x}" for b in raw): "local_content_encoded",
        base64.b64encode(base64.b64encode(raw)).decode(): "local_content_encoded",
        "画一只橘猫": None,
    }
    for text, expected in cases.items():
        got = egress.inspect({"messages": [{"role": "user", "content": text}]}, [body])
        if got != expected:
            fail(f"规范化/编码检测不对：期望 {expected}，得到 {got}")
    split = {"messages": [{"role": "user", "content": body[:6]}, {"role": "user", "content": body[6:]}]}
    if egress.inspect(split, [body]) != "local_content":
        fail("分片拼接没被拦下")
    tool_args = {"messages": [{"role": "assistant", "tool_calls": [{"function": {"arguments": _json.dumps({"q": body})}}]}]}
    if egress.inspect(tool_args, [body]) != "local_content":
        fail("工具参数里的本地正文没被拦下")
    tools = {"tools": [{"type": "function", "function": {"description": body}}], "messages": []}
    if egress.inspect(tools, [body], exempt=("tools",)) is not None:
        fail("工具 schema 应豁免")
    if egress.measure(tools, exempt=("tools",)).get("exempt_bytes", 0) <= 0:
        fail("豁免字节数没有记下")
    short = "蓝色茶壶"
    if egress.inspect({"messages": [{"role": "user", "content": short}]}, [short]) != "local_content":
        fail("短正文整段匹配没拦下")
    if egress.inspect({"messages": [{"role": "user", "content": "帮我画" + short}]}, [short]) is not None:
        fail("包含短正文的长句被误拦")
    if egress.inspect({"messages": [{"role": "user", "content": "好的"}]}, ["好的"]) is not None:
        fail("过短的常用词被误拦")
    fingerprint = egress.short_fingerprint(short)
    if short in fingerprint or egress.inspect({"messages": [{"role": "user", "content": short}]}, [],
                                                fingerprints=[fingerprint]) != "local_content":
        fail("短正文指纹不能还原正文且应能命中")

    saved = egress.path()
    directory = tempfile.mkdtemp(prefix="egress-harden-")
    try:
        log = os.path.join(directory, "egress.jsonl")
        egress.configure(log)
        denied = {
            "extra_field": egress.guard("chat_completion", upstream, {"model": "m", "messages": [], "logit_bias": {}}),
            "multi_image": egress.guard("remote_image", "https://img.example.com/v1", {"model": "m", "prompt": "p", "n": 2}),
            "other": egress.guard("other", "https://pack.example.com", {"q": 1}),
            "unknown": egress.guard("plugin_x", "https://pack.example.com", {"q": 1}),
        }
        for name, ticket in denied.items():
            if not ticket.hit or ticket.reason_code != "egress_policy_denied":
                fail(f"出站策略没拒绝 {name}")
        upload = egress.guard("comfyui_local", "http://127.0.0.1:8188/upload/image", None, raw_upload=True,
                              local_derived=True, modality=("image",))
        if upload.hit:
            fail("ComfyUI 原图上传被策略误拒")
        upload.sent(http_status=200)

        class Bare:
            def capabilities(self, model):
                return _ScriptAdapter(None).capabilities(model)

            def stream(self, request, **kwargs):
                yield {"type": "message.delta", "text": "绕过"}

        class Liar(Bare):
            audits_egress = True

        try:
            registry.register("bare-unit", Bare())
            fail("没挂出口中间件的适配器注册成功了")
        except ValueError:
            pass
        before = len(provider.requests)
        try:
            registry.register("liar-unit", Liar())
            registry.bind("liar-unit-1", "liar-unit")
            try:
                for event in gateway_stream({"model": "liar-unit-1", "messages": [
                        {"role": "user", "parts": [{"type": "text", "text": "hi"}]}]},
                        api_key=SECRET, timeout_sec=2, cancel=threading.Event()):
                    if event.get("type") == "message.delta":
                        fail("没走出口中间件的适配器输出被交出去了")
                fail("没走出口中间件的适配器没被拦下")
            except openai_chat.GatewayError as exc:
                if exc.code != "egress_policy_denied":
                    fail(f"没走中间件的错误码不对：{exc.code}")
        finally:
            install_defaults()
        try:
            for _event in gateway_stream({"model": "demo-model", "messages": [
                    {"role": "user", "parts": [{"type": "text", "text": base64.b64encode(raw).decode()}]}]},
                    api_key=SECRET, timeout_sec=2, cancel=threading.Event(), local_bodies=[body], base_url=upstream):
                pass
            fail("编码后的本地正文进了网关")
        except openai_chat.GatewayError as exc:
            if exc.code != "local_content_encoded":
                fail(f"网关预检的错误码不对：{exc.code}")
        if len(provider.requests) != before:
            fail("网关拦截后仍然打开了连接")
        for name, adapter in registry.adapters().items():
            if getattr(adapter, "audits_egress", False) is not True and getattr(adapter, "offline", False) is not True:
                fail(f"适配器 {name} 没挂出口中间件")
        with open(log, encoding="utf-8") as handle:
            text = handle.read()
        for secret in (body, "LOCAL-HARDEN-UNIT", short, SECRET, base64.b64encode(raw).decode()):
            if secret in text:
                fail("出站审计里出现了本地正文或密钥")
        for line in text.splitlines():
            if set(_json.loads(line)) - set(contract.EGRESS_FIELDS):
                fail("出站审计里出现了未登记字段")
    finally:
        egress.configure(saved)
        shutil.rmtree(directory, ignore_errors=True)


def _gateway_egress(provider, upstream: str) -> None:
    """#57：网关出口的审计记录。只有元数据；混进本地正文时记 blocked 且不发出；写失败不影响请求。"""
    import io
    import contextlib
    from packages.contracts import events as contract
    from services.gateway import egress
    from services.gateway.openai_chat import OpenAIAdapter

    if not getattr(OpenAIAdapter, "audits_egress", False):
        fail("OpenAI 适配器没有声明走出站审计")
    if egress.scope_of("127.0.0.1") != "local" or egress.scope_of("localhost") != "local":
        fail("本机地址的范围不对")
    if egress.scope_of("192.168.1.20") != "lan" or egress.scope_of("api.x.ai") != "third_party":
        fail("局域网/第三方范围不对")
    if egress.destination("https://user:pw@api.example.com:8443/v1/chat?k=v") != ("api.example.com", "third_party"):
        fail("目的地应只留主机名")
    saved = egress.path()
    directory = tempfile.mkdtemp(prefix="egress-unit-")
    try:
        log = os.path.join(directory, "egress.jsonl")
        egress.configure(log)
        marker = "LOCAL-EGRESS-MARKER-5b1e"
        before = len(provider.requests)
        gen = openai_chat.stream_chat(
            base_url=upstream, api_key=SECRET, model="demo-model",
            messages=[{"role": "user", "content": f"请画出{marker}"}],
            timeout_sec=2, cancel=threading.Event(), local_bodies=[marker],
        )
        try:
            next(gen)
            fail("含本地正文的请求被发给了模型")
        except openai_chat.GatewayError as exc:
            if exc.code != "local_content":
                fail(f"出站拦截的错误码不对：{exc.code}")
        finally:
            gen.close()
        if len(provider.requests) != before:
            fail("出站拦截后仍然打开了连接")
        provider.mode = "ok"
        _gateway_run({"model": "demo-model", "messages": [{"role": "user", "parts": [{"type": "text", "text": "你好审计"}]}]},
                     upstream)
        with open(log, encoding="utf-8") as handle:
            raw = handle.read()
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        kinds = [item["type"] for item in records]
        if kinds != ["egress.blocked", "egress.planned", "egress.sent"]:
            fail(f"出站记录顺序不对：{kinds}")
        if marker in raw or SECRET in raw or "你好审计" in raw or "/v1" in raw:
            fail("出站审计里出现了正文、密钥或路径")
        for item in records:
            if set(item) - set(contract.EGRESS_FIELDS):
                fail(f"出站记录多了字段：{set(item) - set(contract.EGRESS_FIELDS)}")
            if item["schema"] != contract.EGRESS_SCHEMA or item["channel"] != "chat_completion":
                fail("出站记录的 schema/渠道不对")
            if item["destination_host"] != "127.0.0.1" or item["destination_scope"] != "local":
                fail("出站记录的目的地不对")
        blocked, planned, sent = records
        if blocked.get("reason_code") != "local_content" or not blocked.get("contains_local_derived"):
            fail("拦截记录没有原因或没标本地派生")
        if blocked["reason_code"] not in contract.EGRESS_REASONS:
            fail("拦截原因没有白话说明")
        if planned["egress_id"] != sent["egress_id"] or sent.get("http_status") != 200:
            fail("planned/sent 没有共用 egress_id 或缺状态码")
        if not planned.get("byte_count") or len(planned.get("content_fingerprint") or "") != 16:
            fail("出站记录缺大小或指纹")
        if planned.get("message_count") != 1 or planned.get("modality") != ["text"] or planned.get("model") != "demo-model":
            fail(f"出站记录的消息数/模态/模型不对：{planned}")
        # 写不进去：请求照常，stderr 告警，计数加一。
        egress.configure(directory)  # 目录不能当文件写
        failures = egress.failures()["write"]
        warn = io.StringIO()
        with contextlib.redirect_stderr(warn):
            events = _gateway_run({"model": "demo-model", "messages": [{"role": "user", "parts": [{"type": "text", "text": "你好"}]}]},
                                  upstream)
        if events[-1].get("type") != "message.completed":
            fail("审计写失败拖垮了请求")
        if egress.failures()["write"] <= failures or "出站审计写入失败" not in warn.getvalue():
            fail("审计写失败没有告警")
        # 监听者出错也只告警。
        egress.configure(log)
        telemetry_mod = __import__("services.telemetry", fromlist=["bind"])
        trace = "t_" + "ee" * 16
        def bad_listener(_entry):
            raise RuntimeError("boom")
        egress.attach(trace, session_id="s_" + "ab" * 16, listener=bad_listener)
        telemetry_mod.bind(trace)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                egress.blocked("remote_image", "https://img.example.com/v1/images", "local_prompt_remote_refused",
                               local_derived=True, handle="c_0123456789abcdef")
        finally:
            telemetry_mod.bind(None)
            egress.detach(trace)
        last = egress.read("s_" + "ab" * 16)
        if not last or last[-1]["reason_code"] != "local_prompt_remote_refused" or last[-1]["handle_id_prefix"] != "c_0123":
            fail(f"宿主拦截记录不对：{last}")
        if last[-1]["destination_host"] != "img.example.com" or last[-1]["destination_scope"] != "third_party":
            fail("宿主拦截记录的目的地不对")
        if egress.summary(records)["sent_hosts"] != {"127.0.0.1": 1}:
            fail("出站汇总不对")
    finally:
        egress.configure(saved)
        shutil.rmtree(directory, ignore_errors=True)


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
    offline = True  # 测试替身，不联网（12.C 注册要求）
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
    offline = True  # 测试替身，不联网（12.C 注册要求）
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
    if _unsafe_inner_html(script) or "regenerate" in script or "use_as_reference" in script or "change_aspect" in script:
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
    if "数据出站" not in page or 'id="egress-log"' not in page or 'id="export-egress"' not in page:
        fail("运行面板没有数据出站时间线或导出按钮")
    for needle in ('event.type.startsWith("egress.")', "renderEgress", "egress-blocked", "EGRESS_REASONS",
                   "/api/egress/export", "egressSummaryNode", "notice: event.notice", "egress: event.egress"):
        if needle not in script:
            fail(f"页面脚本缺出站审计部分：{needle}")
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
    _multi_agent_turn(base, provider)
    _multi_text_turn(base, provider)
    _forced_delegation(base, provider)
    _markdown_checks(base)
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
    _image_cases(base, provider, directory, app)
    _trajectory_case(base, directory)
    _mining_case(base, directory)

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


def _check_secrets(directory: str) -> list[str]:
    """这次检查留下的所有本地正文、文件和附件字节（文本和 base64）、密钥和数据目录路径。"""
    import base64 as _b64
    found = [SECRET, os.path.abspath(directory), ROOT]
    with open(os.path.abspath(__file__), encoding="utf-8") as handle:
        found += re.findall(r'secret = "([^"]{8,})"', handle.read())
    sessions = os.path.join(directory, "sessions")
    for name in os.listdir(sessions):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(sessions, name), encoding="utf-8") as handle:
            data = json.load(handle)
        for item in data.get("contents") or []:
            if isinstance(item.get("text"), str) and len(item["text"]) >= 8:
                found.append(item["text"])
    for folder, _dirs, files in os.walk(directory):
        if "trajectories" in folder:
            continue
        for name in files:
            if name.endswith((".json", ".jsonl", ".tmp")):
                continue
            with open(os.path.join(folder, name), "rb") as handle:
                data = handle.read()
            if len(data) < 16:
                continue
            found.append(_b64.b64encode(data).decode("ascii")[:64])
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(text.strip()) >= 8:
                found.append(text.strip()[:200])
    return found


def _mining_case(base: str, directory: str) -> None:
    """#40：构造含 5 次重复工具序列 + 噪声的轨迹，挖掘出该模式；噪声不成候选；证据 run_id 真实；无泄漏；关模型也能出候选；文档示例通过校验。"""
    from services.meta.candidate import validate, validate_store
    from services.meta.mine import Miner, build_model_prompt

    fixture_dir = tempfile.mkdtemp(prefix="mine-")
    traj_dir = os.path.join(fixture_dir, "trajectories")
    os.makedirs(traj_dir)
    pattern_tools = ["plan_image", "generate_image", "generate_image"]
    noise_tools = [
        ["read_skill"],
        ["accept_local_content", "search_files"],
        ["delegate"],
        ["build_skill_page"],
    ]
    records = []
    pattern_run_ids = []
    # 5 次重复模式，分布在 3 个会话
    for i in range(5):
        sid = "s_" + f"{i % 3}" + ("a" * 31)
        rid = "r_pat" + f"{i:028d}"
        pattern_run_ids.append(rid)
        records.append(_synthetic_traj(rid, sid, pattern_tools, summary=f"批量出图模式场景 {i}", secret_bait=SECRET if i == 0 else None))
    # 噪声：每种只出现 1 次
    for i, tools in enumerate(noise_tools):
        rid = "r_noise" + f"{i:026d}"
        records.append(_synthetic_traj(rid, "s_noise" + ("b" * 26), tools, summary=f"噪声 {i}"))
    # 再加一条带绝对路径诱饵的噪声，确认候选里不会出现
    records.append(_synthetic_traj(
        "r_path" + ("c" * 26), "s_path" + ("d" * 26),
        ["unique_tool_xyz"], summary=r"路径诱饵 D:\comfyui\secret 和 /home/box/x",
    ))
    jsonl = os.path.join(traj_dir, "trajectories.jsonl")
    with open(jsonl, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    # 空索引，避免 Exporter 捣乱；mine 时 export_first=False
    with open(os.path.join(traj_dir, "index.json"), "w", encoding="utf-8") as handle:
        json.dump({"schema": "mas.trajectory-index/1", "runs": {}, "sessions": {}}, handle)
    miner = Miner(fixture_dir)
    # 直接喂 records，跳过 export（夹具没有会话文件）
    result = miner.mine(min_count=5, min_sessions=3, use_model=False, export_first=False, records=records)
    if result["candidates"] != 1:
        fail(f"应挖出恰好 1 个候选，实际 {result}")
    store = miner.load_store()
    problems = validate_store(store)
    if problems:
        fail(f"候选库校验失败：{problems}")
    cand = store["candidates"][0]
    if validate(cand):
        fail(f"候选校验失败：{validate(cand)}")
    if cand["pattern"]["steps"] != pattern_tools:
        fail(f"挖到的模式不对：{cand['pattern']}")
    if set(cand["evidence"]["run_ids"]) != set(pattern_run_ids):
        fail(f"证据 run_id 对不上：{cand['evidence']['run_ids']}")
    if cand["evidence"]["count"] != 5 or cand["evidence"]["sessions"] != 3:
        fail(f"次数/会话不对：{cand['evidence']}")
    # 噪声不应成为候选
    for item in store["candidates"]:
        if "unique_tool" in item["pattern"]["signature"] or item["pattern"]["signature"].startswith("read_skill"):
            fail(f"噪声产生了候选：{item['pattern']}")
    # 泄漏检查
    blob = json.dumps(store, ensure_ascii=False)
    if SECRET in blob or "sk-live" in blob:
        fail("候选里有密钥")
    if "D:\\comfyui" in blob or "/home/box" in blob or os.path.abspath(fixture_dir) in blob:
        fail("候选里有绝对路径")
    # 关模型时 model_assisted 应为 false
    if cand.get("model_assisted") is not True and cand.get("model_assisted") is not False:
        fail("缺 model_assisted")
    if cand["model_assisted"]:
        fail("未开模型却标记了 model_assisted")
    # 模型归纳输入脱敏
    prompt = build_model_prompt({
        "kind": "tool_sequence",
        "signature": ">".join(pattern_tools),
        "steps": tuple(pattern_tools),
        "count": 5,
        "sessions": 3,
        "success_rate": 1.0,
        "scenarios": [f"含密钥 {SECRET} 和路径 D:\\leak\\a 的摘要", "正常场景"],
        "run_ids": pattern_run_ids,
    })
    if SECRET in prompt or "sk-live" in prompt or "D:\\leak" in prompt or "/home/" in prompt:
        fail(f"模型归纳输入未脱敏：{prompt[:200]}")
    if "[secret]" not in prompt and "密钥" in prompt:
        # 密钥形态应被替换；若 scenarios 里只有 SECRET 常量应变成 [secret]
        pass
    if SECRET in prompt:
        fail("模型 prompt 仍含 SECRET")
    # 真实数据目录上也能跑通（基于 #39 已导出的轨迹），且 skills/ 未被改动
    before = _skills_fingerprint()
    live = Miner(directory)
    live_result = live.mine(min_count=3, min_sessions=2, use_model=False, export_first=False,
                            records=Exporter_records(directory))
    if live_result["records"] <= 0:
        fail("真实轨迹挖掘没有读到记录")
    after = _skills_fingerprint()
    if before != after:
        fail("挖掘改写了 skills/")
    # API
    status, body = request("POST", base + "/api/meta/mine?min_count=3&min_sessions=2")
    data = json.loads(body)
    if status != 200 or data.get("schema") != "mas.skill-candidates/1":
        fail(f"挖掘接口不对：{status} {body[:300]}")
    status, body = request("GET", base + "/api/meta/candidates")
    listed = json.loads(body)
    if status != 200 or not isinstance(listed.get("candidates"), list):
        fail(f"候选列表接口不对：{status}")
    if listed["candidates"]:
        cid = listed["candidates"][0]["id"]
        status, body = request("GET", base + f"/api/meta/candidates/{cid}")
        if status != 200 or json.loads(body).get("id") != cid:
            fail(f"候选详情接口不对：{status}")
    if SECRET in body or os.path.abspath(directory) in body:
        fail("候选接口带出了密钥或路径")
    _mining_doc_example()
    print(f"mining fixture 1 candidate from 5× pattern; live patterns_seen={live_result['patterns_seen']}")
    shutil.rmtree(fixture_dir, ignore_errors=True)


def Exporter_records(directory: str) -> list:
    from services.meta.export import Exporter
    return Exporter(directory).query(limit=0)


def _skills_fingerprint() -> list[tuple]:
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "skills")
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            out.append((os.path.relpath(path, root), st.st_mtime_ns, st.st_size))
    return sorted(out)


def _synthetic_traj(run_id: str, session_id: str, tools: list[str], summary: str, secret_bait: str | None = None) -> dict:
    from services.meta.trajectory import SCHEMA
    steps = [{"kind": "tool", "name": name, "ok": True, "ms": 1, "at": "2026-10-24T12:00:00Z",
              "args": {"keys": ["prompt"]}} for name in tools]
    text = summary
    if secret_bait:
        text = summary + " " + secret_bait
    # 摘要侧在挖掘时还会再 scrub；这里模拟已导出的干净摘要，诱饵只放进会再被检查的 prompt 构造里
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "parent_run_id": None,
        "trace_id": run_id,
        "session_id": session_id,
        "turn_id": "m_" + run_id[2:34].ljust(32, "0")[:32],
        "agent": "assistant",
        "role": "assistant",
        "mode": "single",
        "depth": 0,
        "model": "demo-vision",
        "started_at": "2026-10-24T12:00:00Z",
        "ended_at": "2026-10-24T12:00:01Z",
        "ms": 1,
        "input": {"summary": text[:120], "chars": len(text), "local_handle": None, "attachments": []},
        "output": {"summary": "完成", "chars": 2, "artifacts": []},
        "outcome": {"status": "completed", "error_code": None, "failure_class": None},
        "feedback": {"retried": False, "reseeded": False, "stopped": False},
        "steps": steps,
        "tools": list(tools),
        "skills": [],
        "budget": {"steps": len(steps), "tokens": {"estimated": 1}},
        "delegations": [],
        "exported_at": "2026-10-24T12:00:02Z",
    }


def _mining_doc_example() -> None:
    """docs/mining.md 里的示例必须能通过 validate。"""
    from services.meta.candidate import validate

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "mining.md")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    if not match:
        fail("docs/mining.md 缺少 json 示例")
    problems = validate(json.loads(match.group(1)))
    if problems:
        fail(f"docs/mining.md 示例没通过校验：{problems}")



def _trajectory_case(base: str, directory: str) -> None:
    """#39 / ADR 0006：在存过本地正文和文件、跑过生图、多智能体、技能回合之后导出轨迹。
    每条都通过 schema 校验；搜不到本地正文、文件字节、密钥和绝对路径；重复导出幂等；委托关系能还原成树。"""
    from services.meta.export import Exporter
    from services.meta.trajectory import build_tree, validate

    exporter = Exporter(directory)
    first = exporter.export()
    # 本地资源删除（#51）已经顺手导出过一次，所以这里可能 written == 0，但文件里得有轨迹。
    if first["total"] <= 0 or first["dropped"]:
        fail(f"轨迹导出不对：{first}")
    second = exporter.export()
    if second["written"] != 0 or second["total"] != first["total"]:
        fail(f"重复导出不幂等：{first} → {second}")
    path = os.path.join(directory, "trajectories", "trajectories.jsonl")
    with open(path, encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    records = [json.loads(line) for line in lines]
    if len(records) != first["total"] or len({r["run_id"] for r in records}) != len(records):
        fail("轨迹文件里有重复记录")
    for record in records:
        problems = validate(record)
        if problems:
            fail(f"轨迹没通过校验：{problems}")
    blob = "".join(lines)
    for secret in _check_secrets(directory):
        if secret and secret in blob:
            fail(f"轨迹里出现了本地内容、密钥或路径（{len(secret)} 字）")
    if "D:\\" in blob or "/tmp/" in blob or "/home/" in blob:
        fail("轨迹里出现了绝对路径")
    modes = {r["mode"] for r in records}
    if modes != {"single", "multi"}:
        fail(f"单 Agent 和多智能体的运行没有都导出：{modes}")
    children = [r for r in records if r["parent_run_id"]]
    by_id = {r["run_id"]: r for r in records}
    if not children or any(c["parent_run_id"] not in by_id for c in children):
        fail("子 Agent 的轨迹找不到父运行")
    roots = build_tree(records)
    tree = next((n for n in roots if [c["run"]["agent"] for c in n["children"]] == ["planner", "writer", "reviewer"]), None)
    if tree is None or tree["run"]["agent"] != "orchestrator" or len(tree["run"]["delegations"]) != 3:
        fail("委托关系没能从 parent_run_id 还原成树")
    if not any(s["kind"] == "model" for c in tree["children"] for s in c["run"]["steps"]):
        fail("子 Agent 的模型调用没有归到子运行")
    if not exporter.query(tool="generate_image") or not exporter.query(agent="writer"):
        fail("按工具或 Agent 查不到轨迹")
    if not exporter.query(skill="anatomy"):
        fail("按技能查不到读过 anatomy 的轨迹")
    if exporter.query(outcome="completed", until="2000-01-01") or not exporter.query(outcome="failed"):
        fail("按结局和时间查询不对")
    if not any(r["input"] and r["input"].get("local_handle") for r in records):
        fail("本地正文的回合没有只留句柄")
    status, body = request("GET", base + "/api/trajectories?agent=planner&limit=5")
    data = json.loads(body)
    if status != 200 or not data["trajectories"] or any(t["agent"] != "planner" for t in data["trajectories"]):
        fail(f"轨迹接口不对：{status}")
    if SECRET in body or os.path.abspath(directory) in body:
        fail("轨迹接口带出了密钥或路径")
    _trajectory_scrub_units()
    _trajectory_doc_example()
    print(f"trajectories {first['total']} runs, {len(children)} child runs, modes {sorted(modes)}")


def _trajectory_doc_example() -> None:
    """docs/trajectory.md 里的示例必须能通过 validate。"""
    from services.meta.trajectory import validate

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "trajectory.md")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    if not match:
        fail("docs/trajectory.md 缺少 json 示例")
    problems = validate(json.loads(match.group(1)))
    if problems:
        fail(f"docs/trajectory.md 示例没通过校验：{problems}")


def _trajectory_scrub_units() -> None:
    """去敏单元：密钥形态、路径、助手文本里出现的本地正文、被忘掉的本地内容重新生成。"""
    from services.meta.export import Exporter, Scrubber

    scrub = Scrubber([("本机正文不出门甲乙丙丁戊", "c_" + "1" * 32)], ["文件里的字节内容-ABCDEFG"], [SECRET])
    for raw, bad in [
        ("密钥 sk-abcdefghijklmnop 在这", "sk-abcdefghijklmnop"),
        ("ghp_ABCDEFGHIJKLMNOPQRST", "ghp_ABCDEFGHIJKLMNOPQRST"),
        ("Authorization: Bearer abc.def.ghi-123", "abc.def.ghi-123"),
        ("api_key=hunter2hunter2", "hunter2hunter2"),
        (r"路径 D:\comfyui\multimodal-agent-studio\apps 在此", "comfyui"),
        ("看 /home/box/secret.txt 和 /Users/le/x", "/home/box"),
        ("回答里引用了本机正文不出门甲乙丙丁戊", "甲乙丙丁戊"),
        ("文件里的字节内容-ABCDEFG 被复述", "ABCDEFG"),
        ("配置里的 " + SECRET, SECRET),
    ]:
        if bad in scrub.text(raw):
            fail(f"去敏没有挡住：{bad}")
    if "https://example.com/data" not in scrub.text("见 https://example.com/data"):
        fail("去敏误伤了网址")
    directory = tempfile.mkdtemp(prefix="traj-")
    os.makedirs(os.path.join(directory, "sessions"))
    sid, handle = "s_" + "a" * 32, "c_" + "b" * 32
    body = "会被忘掉的本地正文庚辛壬癸"
    session = {"session_id": sid, "title": "t", "created_at": "2026-10-24T00:00:00Z", "updated_at": "2026-10-24T00:00:01Z",
               "contents": [{"handle": handle, "message_id": "m_" + "1" * 32, "kind": "text", "text": body}],
               "messages": [
                   {"message_id": "m_" + "1" * 32, "role": "user", "text": body, "status": "completed"},
                   {"message_id": "m_" + "2" * 32, "role": "assistant", "text": "复述：" + body, "status": "completed",
                    "run_id": "r_" + "3" * 32, "created_at": "2026-10-24T00:00:01Z", "events": [], "runtime": []},
               ]}
    path = os.path.join(directory, "sessions", sid + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(session, fh, ensure_ascii=False)
    exporter = Exporter(directory)
    if exporter.export(keep_days=0)["written"] != 1:
        fail("合成会话没有导出")
    with open(exporter.jsonl, encoding="utf-8") as fh:
        text = fh.read()
    if body in text or json.loads(text)["input"]["local_handle"] != handle:
        fail("合成会话的本地正文进了轨迹")
    session["contents"] = []
    session["messages"][0]["text"] = "这条本地内容已经丢掉。"
    session["messages"][1]["text"] = "这条本地内容已经丢掉。"
    session["updated_at"] = "2026-10-24T00:00:02Z"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(session, fh, ensure_ascii=False)
    stats = exporter.export(keep_days=0)
    if stats["removed"] != 1 or stats["written"] != 1 or stats["total"] != 1:
        fail(f"忘掉本地内容后没有重新生成轨迹：{stats}")
    os.remove(path)
    if exporter.export(keep_days=0)["total"] != 0:
        fail("删掉会话后轨迹还在")
    session["updated_at"] = "2000-01-01T00:00:00Z"
    session["messages"][1]["created_at"] = "2000-01-01T00:00:00Z"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(session, fh, ensure_ascii=False)
    if exporter.export(keep_days=90)["total"] != 0:
        fail("超过保留期的轨迹还被导出")


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


def _multi_agent_turn(base: str, provider) -> None:
    """ADR 0005：勾了多智能体才走编排者；子 Agent 只拿句柄，本地正文不出本机；单 Agent 默认不变。"""
    secret = "多智能体隔离正文戊己庚辛"
    session_id = _fresh_session(base)
    provider.mode = "keep"
    _speak(base, session_id, secret)
    provider.mode = "multi"
    sent = len(provider.requests)
    events = _speak(base, session_id, "帮我规划两张图", timeout=30, mode="multi")
    if _assistant_text(events) != "多智能体完成":
        fail(f"多智能体回合没有收尾：{_assistant_text(events)}")
    new_requests = provider.requests[sent:]
    blob = json.dumps([item["body"] for item in new_requests], ensure_ascii=False)
    if secret in blob:
        fail("多智能体把本地正文发给了模型")
    systems = [_user_text((item["body"].get("messages") or [{}])[0].get("content")) for item in new_requests]
    if not any("你是规划 Agent" in text for text in systems):
        fail("编排者没有把任务委托给 planner")
    for item in new_requests:
        names = [tool.get("function", {}).get("name") for tool in item["body"].get("tools") or []]
        system = _user_text((item["body"].get("messages") or [{}])[0].get("content"))
        if "你是规划 Agent" in system and ("delegate" in names or "accept_local_content" in names
                                          or "generate_image" in names):
            fail(f"planner 拿到了不该有的工具：{names}")
        if "你是编排者" in system and "delegate" not in names:
            fail("编排者没有 delegate 工具")
    nodes = [event for event in events if event.get("type") == "agent"]
    if not nodes or nodes[0].get("agent") != "planner" or nodes[-1].get("status") != "completed":
        fail(f"界面没有收到子 Agent 节点：{nodes}")
    if not all(node.get("parent_run_id") for node in nodes):
        fail("子 Agent 节点缺 parent_run_id")
    tagged = [e for e in events if e.get("type") == "tool_call" and e.get("tool") == "plan_images"]
    if not tagged or tagged[-1].get("agent") != "planner" or not tagged[-1].get("agent_run_id"):
        fail("子 Agent 的工具调用没有标上是哪个 Agent")
    audited = _egress_since(base, events, (secret,))
    sent_chat = [item for item in audited if item["type"] == "egress.sent" and item["channel"] == "chat_completion"]
    if len(sent_chat) != len(new_requests) or any(item.get("session_id") != session_id for item in audited):
        fail(f"多智能体的出站记录数或会话不对：{len(sent_chat)} / {len(new_requests)}")
    if not any(item.get("agent_run_id") for item in sent_chat):
        fail("子 Agent 的出站记录没有标 agent_run_id")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    if not any(e.get("type") == "agent" for e in stored["messages"][-1].get("events") or []):
        fail("刷新后子 Agent 树不在")
    status, listing = request("GET", base + "/api/agents")
    names = {item.get("name") for item in json.loads(listing).get("agents") or []}
    if status != 200 or not {"orchestrator", "planner", "image", "reviewer", "writer"} <= names:
        fail(f"/api/agents 没列出内置 Agent：{status} {listing[:200]}")
    status, raw = request("POST", base + "/api/agents/planner/uninstall", {})
    if status != 400:
        fail("接口能卸载内置 Agent")
    provider.mode = "ok"
    sent = len(provider.requests)
    _speak(base, session_id, "普通一句")
    plain = provider.requests[sent:]
    if len(plain) != 1:
        fail("单 Agent 默认回合的请求次数变了")
    names = [tool.get("function", {}).get("name") for tool in plain[0]["body"].get("tools") or []]
    if "delegate" in names or "你是编排者" in json.dumps(plain[0]["body"], ensure_ascii=False):
        fail("没勾多智能体时也走了编排者")


def _multi_text_turn(base: str, provider) -> None:
    """勾了多智能体的文字任务也走编排：planner 提纲 → writer 起草 → reviewer 审稿 → 编排者汇总。"""
    session_id = _fresh_session(base)
    provider.mode = "multitext"
    sent = len(provider.requests)
    events = _speak(base, session_id, "写一篇关于城市骑行的短文", timeout=30, mode="multi")
    if not _assistant_text(events).startswith("# 终稿"):
        fail(f"多智能体文字任务没有由编排者汇总：{_assistant_text(events)}")
    nodes = [e for e in events if e.get("type") == "agent" and e.get("status") != "running"]
    if [n.get("agent") for n in nodes] != ["planner", "writer", "reviewer"]:
        fail(f"文字任务的 Agent 树不对：{[n.get('agent') for n in nodes]}")
    systems = [_user_text((r["body"].get("messages") or [{}])[0].get("content")) for r in provider.requests[sent:]]
    if not any("你是写作 Agent" in text for text in systems):
        fail("文字任务没有委托给 writer")
    lead = next(text for text in systems if "你是编排者" in text)
    if "writer" not in lead or "文字任务" not in lead:
        fail("编排者的系统指令里没有文字任务的委托规则")
    if not any(n.get("agent") == "writer" and n.get("summary") for n in nodes):
        fail("Agent 树的节点没有带子 Agent 摘要")


def _forced_delegation(base: str, provider) -> None:
    """#38：编排者想一步答完也会被运行时强制委托（planner → writer → reviewer）；寒暄照常直接回答；单 Agent 仍是 auto。"""
    session_id = _fresh_session(base)
    provider.mode = "lazy"
    sent = len(provider.requests)
    events = _speak(base, session_id, "讨论一下城市骑行对通勤的影响，给出你的看法", timeout=30, mode="multi")
    nodes = [e.get("agent") for e in events if e.get("type") == "agent" and e.get("status") != "running"]
    if nodes != ["planner", "writer", "reviewer"]:
        fail(f"编排者一步答完没有被强制委托：{nodes}")
    first = provider.requests[sent]["body"]
    if first.get("tool_choice") != {"type": "function", "function": {"name": "delegate"}}:
        fail(f"多智能体文字任务第一步没有强制 delegate：{first.get('tool_choice')}")
    sent = len(provider.requests)
    events = _speak(base, session_id, "你好", timeout=30, mode="multi")
    if _assistant_text(events) != "你好！" or any(e.get("type") == "agent" for e in events):
        fail(f"寒暄不该委托：{_assistant_text(events)}")
    if any(r["body"].get("tool_choice") != "auto" for r in provider.requests[sent:]):
        fail("寒暄回合被强制了 tool_choice")
    sent = len(provider.requests)
    _speak(base, _fresh_session(base), "讨论一下城市骑行", timeout=30)
    single = provider.requests[sent:]
    if not single or any(r["body"].get("tool_choice") != "auto" for r in single):
        fail("单 Agent 模式的 tool_choice 变了")


def _unsafe_inner_html(script: str) -> bool:
    """app.js 只允许把 markdownHtml(...) 的结果放进 innerHTML（以及兜底时读转义后的 innerHTML）。"""
    allowed = (".innerHTML = markdownHtml(", "div.innerHTML.replace(")
    rest = script
    for pattern in allowed:
        rest = rest.replace(pattern, "")
    return "innerHTML" in rest


_MD_CASES = [
    # (输入, 必须出现, 不得出现)
    ("# 标题\n\n## 二级", ["<h1>标题</h1>", "<h2>二级</h2>"], []),
    ("**粗** *斜* `a<b`", ["<strong>粗</strong>", "<em>斜</em>", "<code>a&lt;b</code>"], []),
    ("- 甲\n- 乙\n\n1. 一\n2. 二", ["<ul><li>甲</li><li>乙</li></ul>", "<ol><li>一</li><li>二</li></ol>"], []),
    ("> 引用", ["<blockquote><p>引用</p></blockquote>"], []),
    ("|a|b|\n|-|-|\n|1|2|", ["<table>", "<th>a</th>", "<td>2</td>"], []),
    ("```py\nprint('<x>')\n```", ['<pre><code class="language-py">print(&#39;&lt;x&gt;&#39;)</code></pre>'], []),
    ("```\n流式中未闭合", ["<pre><code>流式中未闭合</code></pre>"], []),
    ("[官网](https://example.com/?a=1&b=2) [信](mailto:a@b.c)",
     ['<a href="https://example.com/?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">官网</a>', 'href="mailto:a@b.c"'], []),
    ("<script>alert(1)</script>", ["&lt;script&gt;"], ["<script"]),
    ("<img src=x onerror=alert(1)>", ["&lt;img"], ["<img"]),
    ("[x](javascript:alert(1))", [], ["href", "<a"]),
    ("[x](JavaScript:alert`1`)", [], ["href"]),
    ("[x](data:text/html,<b>1</b>)", [], ["href", "<b>"]),
    ("[x](vbscript:msgbox)", [], ["href"]),
    ('[x](https://e.com"onmouseover="alert(1))', [], ["onmouseover=\"", "href"]),
    ("**<svg onload=alert(1)>**", ["<strong>&lt;svg"], ["<svg"]),
    ("`<iframe>`", ["<code>&lt;iframe&gt;</code>"], ["<iframe"]),
    ("| <b>x</b> | y |\n|---|---|\n| <i>1</i> | 2 |", ["&lt;b&gt;"], ["<b>", "<i>"]),
    ("```js\n</code></pre><script>alert(1)</script>\n```", ["&lt;/code&gt;"], ["<script"]),
    ("snake_case_name", ["snake_case_name"], ["<em>"]),
]


def _markdown_checks(base: str) -> None:
    status, page = request("GET", base + "/")
    if page.find("/static/markdown.js") < 0 or page.find("/static/markdown.js") > page.find("/static/app.js"):
        fail("聊天页没有在 app.js 之前加载本地 Markdown 渲染器")
    if "cdn" in page.lower() or "unpkg" in page.lower() or "jsdelivr" in page.lower():
        fail("聊天页从 CDN 加载了脚本")
    status, md = request("GET", base + "/static/markdown.js")
    if status != 200 or "renderMarkdown" not in md:
        fail("取不到 /static/markdown.js")
    script = request("GET", base + "/static/app.js")[1]
    if "markdownHtml(message.text" not in script or "markdownHtml(node.summary)" not in script:
        fail("助手回复或子 Agent 摘要没有按 Markdown 渲染")
    if 'message.role === "user"' not in script or "text.textContent = message.text" not in script:
        fail("用户消息没有保持纯文本显示")
    node = shutil.which("node")
    if not node:
        print("check: 跳过 Markdown 渲染用例（没有 node）")
        return
    path = os.path.join(WEB_DIR, "static", "markdown.js")
    driver = (
        "const {renderMarkdown}=require(process.argv[1]);"
        "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
        "process.stdout.write(JSON.stringify(JSON.parse(s).map(renderMarkdown)));});"
    )
    done = subprocess.run([node, "-e", driver, path], input=json.dumps([c[0] for c in _MD_CASES]),
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    if done.returncode != 0:
        fail(f"Markdown 渲染器跑不起来：{done.stderr[-300:]}")
    for (source, must, never), html in zip(_MD_CASES, json.loads(done.stdout)):
        for piece in must:
            if piece not in html:
                fail(f"Markdown 渲染缺少 {piece!r}：{source!r} → {html!r}")
        for piece in never:
            if piece in html:
                fail(f"Markdown 渲染不安全或不对，出现了 {piece!r}：{source!r} → {html!r}")


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


def _speak(base: str, session_id: str, text: str, timeout: float = 8, content: str | None = None, attachments=None,
           mode=None):
    def on_event(event):
        if event.get("type") == "tool.confirm":
            _post_confirm(base, event.get("run_id") or "", True)
        if event.get("type") == "content_request":
            _post_content(base, event.get("run_id") or "", content if content is not None else text)

    payload = {"text": text}
    if attachments:
        payload["attachments"] = attachments
    if mode:
        payload["mode"] = mode
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

    summaries = []

    def on_event(event):
        if event.get("type") != "tool.confirm":
            return
        confirmed.append(event.get("name"))
        summaries.append(event.get("egress"))
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
    summary = summaries[0] if summaries else None
    if not isinstance(summary, dict) or summary.get("by") != "host" or not summary.get("text"):
        fail(f"确认卡没有宿主生成的出站摘要：{summary}")
    if summary.get("destination") not in ("comfyui", "third_party_image", "none", "unknown"):
        fail(f"出站摘要的目的地不对：{summary}")
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
        self.hold_after = None
        self.missing_nodes = set()
        self.auth = []

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
        with self.cv:
            held = self.hold_after is not None and prompt_id in self.ids and self.ids.index(prompt_id) >= self.hold_after
        if held:
            return _history_entry("running")
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
        self.server.box.auth.append((parsed.path.split("/")[1] if "/" in parsed.path else parsed.path, self.headers.get("Authorization", "")))
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
        if parsed.path == "/system_stats":
            self._json({"system": {"os": "fake"}, "devices": []})
            return
        if parsed.path == "/object_info":
            box = self.server.box
            self._json({name: {} for name in _workflow_classes() if name not in box.missing_nodes})
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
        box.auth.append((path.strip("/"), self.headers.get("Authorization", "")))
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


def _image_cases(base: str, provider, directory: str, app=None) -> None:
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
        _handle_scope_case(base, provider, comfy.box, app)
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
        _egress_http_case(base, provider, directory)
        _multi_image_case(base, provider, comfy.box, directory, workflow)
        _local_multi_image_case(base, provider, comfy.box, workflow)
        _local_early_stop_case(base, provider, comfy.box)
        _local_plan_case(base, provider, comfy.box)
        _image_skill_cases(base, provider, comfy.box, directory)
        _local_ask_case(base, provider, comfy.box)
        _batch_stop_case(base, provider, comfy.box)
        _tool_config_case(base, provider, comfy.box, directory, port)
        _toolpack_http_case(base, directory)
        _delete_session_case(base, directory)
        _resources_case(base, provider, directory)
        _character_cases(base, provider, comfy.box, directory)
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
    trace = _telemetry_of(base, events)
    providers = [item for item in trace["events"] if item.get("kind") == "provider"]
    if not providers or providers[-1].get("usage", {}).get("total_tokens") != 12:
        fail(f"遥测没有记下供应商返回的用量：{providers}")
    if trace["summary"]["kinds"].get("provider", {}).get("tokens") != 12:
        fail("遥测汇总的 token 不对")


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
    if [event.get("type") for event in message.get("events") or []] != ["content_request", "tool_call", "progress", "artifact", "image_config"]:
        fail(f"刷新后成片不在同一条消息上：{message.get('events')}")
    if message.get("status") != "completed":
        fail("出图完成后消息状态不对")
    missing, _ = request("GET", base + f"/sessions/{session_id}/artifacts/art_{'ab' * 16}")
    if missing != 404:
        fail("不存在的图片没有返回 404")
    _telemetry_image(base, events, box)


def _telemetry_of(base: str, events: list[dict], attempts: int = 40) -> dict:
    run_id = next((event.get("run_id") for event in events if event.get("run_id")), "")
    if not run_id:
        fail("事件里没有 run_id，遥测无从关联")
    for _ in range(attempts):
        status, raw = request("GET", base + f"/api/telemetry/{run_id}")
        if status != 200:
            fail(f"遥测接口返回 {status}")
        body = json.loads(raw)
        if any(item.get("kind") == "run" for item in body["events"]):
            break
        time.sleep(0.05)
    else:
        fail("遥测里没有这次运行的结束记录")
    if any(item.get("trace_id") != run_id for item in body["events"]):
        fail("遥测按 trace_id 查询混进了别的运行")
    blob = json.dumps(body, ensure_ascii=False)
    if SECRET in blob:
        fail("遥测里出现了密钥")
    return body


def _telemetry_image(base: str, events: list[dict], box: ComfyState) -> None:
    trace = _telemetry_of(base, events)
    kinds = [item.get("kind") for item in trace["events"]]
    for wanted in ("web.request", "provider", "tool", "comfyui.submit", "comfyui.run", "run"):
        if wanted not in kinds:
            fail(f"同一个 trace 缺少 {wanted}：{kinds}")
    if kinds[0] != "web.request" or kinds[-1] != "run":
        fail(f"遥测顺序不对：{kinds}")
    submitted = [item.get("prompt_id") for item in trace["events"] if item.get("kind") == "comfyui.submit"]
    if not submitted or not submitted[-1] or submitted[-1] not in box.ids:
        fail(f"遥测里的 ComfyUI prompt_id 对不上：{submitted}")
    if "画一只茶壶" in json.dumps(trace, ensure_ascii=False):
        fail("画面描述写进了遥测")
    end = trace["events"][-1]
    if end.get("ok") is not True or not isinstance(end.get("ms"), int):
        fail(f"运行结束记录不对：{end}")
    status, raw = request("GET", base + "/api/telemetry?limit=5")
    if status != 200 or len(json.loads(raw)["events"]) > 5:
        fail("遥测列表接口不对")
    status, _ = request("GET", base + "/api/telemetry/..%2Fx")
    if status not in (400, 404):
        fail("不合法的 trace_id 没有被拒绝")


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


def _handle_units() -> None:
    """12.B（#58）句柄作用域的单元检查：会话、用途、过期、撤销、运行绑定、一次性、旧记录读时升级、伪造。"""
    import packages.contracts.events as contract

    root = tempfile.mkdtemp(prefix="handles-")
    real_clock = store.clock
    saved_ttl = os.environ.pop("MAS_HANDLE_TTL_SEC", None)
    try:
        sessions = store.SessionStore(os.path.join(root, "sessions"))
        user = "u_" + "1" * 32
        sid = sessions.create(user)["session_id"]
        other = sessions.create(user)["session_id"]
        secret = "句柄单元检查的本机正文"
        image = sessions.add_content(sid, secret, "image_prompt")
        keep = sessions.add_content(sid, "只存不用的正文", "keep")
        for field in ("expires_at", "state"):
            if field not in image:
                fail(f"句柄公开字段缺 {field}")
        if set(image) - {"handle", "type", "kind", "media_type", "size", "purpose", "expires_at", "state"}:
            fail(f"句柄公开字段多了东西：{sorted(image)}")
        if secret in json.dumps(sessions.list_content(sid), ensure_ascii=False) or other in json.dumps(image):
            fail("句柄公开字段带出了正文或会话映射")

        def code(session, handle, **kw):
            kw.setdefault("tool", "generate_image")
            return sessions.resolve_content(session, handle, **kw)[1]

        if code(sid, image["handle"]) != "ok":
            fail("有效的 image_prompt 句柄不能出图")
        if code(sid, keep["handle"]) != "handle_forbidden":
            fail("keep 句柄能拿来出图（越权 purpose）")
        if code(other, image["handle"]) != "handle_not_found":
            fail("跨会话句柄没有当成不存在")
        for forged in ("c_" + "ab" * 16, "c_xyz", "", "x_" + "0" * 32, image["handle"].upper(), None):
            if code(sid, forged) != "handle_not_found":
                fail(f"伪造句柄没有被拒：{forged!r}")
        if not set(contract.HANDLE_ERRORS) <= set(contract.ERROR_CODES) or not set(contract.HANDLE_ERRORS) <= set(contract.EGRESS_REASONS):
            fail("句柄错误码没有登记到 ERROR_CODES / EGRESS_REASONS")
        os.environ["MAS_HANDLE_TTL_SEC"] = "5"
        if store.handle_ttl() != store.HANDLE_TTL_MIN:
            fail("过小的 TTL 没有夹到下限")
        os.environ["MAS_HANDLE_TTL_SEC"] = "abc"
        if store.handle_ttl() != store.HANDLE_TTL_DEFAULT:
            fail("坏的 TTL 没有回到默认值")
        os.environ.pop("MAS_HANDLE_TTL_SEC", None)
        now = real_clock()
        store.clock = lambda: now + store.HANDLE_TTL_DEFAULT + 1
        if code(sid, image["handle"]) != "handle_expired":
            fail("过期句柄还能出图")
        if sessions.content_info(sid, image["handle"])["state"] != "expired":
            fail("过期句柄的状态不是 expired")
        public = set(sessions.content_info(sid, image["handle"]))
        if public != {"handle", "type", "kind", "media_type", "size", "purpose", "expires_at", "state"}:
            fail(f"句柄公开字段超出白名单：{sorted(public)}")
        if secret not in sessions.local_bodies(sid):
            fail("过期句柄的正文不再参与出口检查")
        fresh = sessions.add_content(sid, secret, "image_prompt")
        if fresh["handle"] == image["handle"] or fresh["state"] != "active":
            fail("同一正文再次收下没有换一枚新句柄")
        if code(sid, image["handle"]) == "ok" or code(sid, fresh["handle"]) != "ok":
            fail("换句柄之后新旧句柄的有效性不对")
        store.clock = real_clock
        if sessions.revoke_content(sid, fresh["handle"]) != "revoked" or code(sid, fresh["handle"]) != "handle_expired":
            fail("撤销后的句柄还能出图")
        if sessions.revoke_content(other, fresh["handle"]) != "absent":
            fail("别的会话能撤销这枚句柄")
        bound = sessions.add_content(sid, "绑在一次运行上的正文", "image_prompt")
        if not sessions.bind_run(sid, bound["handle"], "r_" + "1" * 32, single_use=True):
            fail("绑定运行失败")
        if code(sid, bound["handle"], run_id="r_" + "2" * 32) != "handle_forbidden":
            fail("别的运行能用绑定到这次运行的句柄（子 Agent / 运行作用域）")
        if code(sid, bound["handle"], run_id="r_" + "1" * 32) != "ok":
            fail("本次运行不能用自己的句柄")
        if code(sid, bound["handle"], run_id="r_" + "1" * 32) != "handle_expired":
            fail("一次性句柄能用第二次")
        if sessions.bind_run(sid, bound["handle"], "r_" + "3" * 32):
            fail("运行绑定能被改到别的运行（只能收窄）")
        if sessions.revoke_run(sid, "r_" + "1" * 32) != 1:
            fail("按运行撤销句柄的数目不对")
        if not sessions.allow_tool(sid, keep["handle"], "generate_image") or code(sid, keep["handle"]) != "ok":
            fail("宿主放开 keep 句柄的 generate_image 失败")
        if sessions.allow_tool(sid, keep["handle"], "rm_rf"):
            fail("宿主能放开未登记的工具")
        # 旧记录（12.B 之前的 c_ 句柄）：读时升级，句柄串不变。
        legacy = "c_" + "e" * 32
        path = sessions._path(sid)
        with open(path, encoding="utf-8") as handle_file:
            data = json.load(handle_file)
        data.setdefault("contents", []).append({
            "handle": legacy, "type": "text", "kind": "text", "media_type": "text/plain", "size": 3,
            "purpose": "image_prompt", "text": "旧正文", "message_id": "", "created_at": "2026-01-01T00:00:00Z",
        })
        with open(path, "w", encoding="utf-8") as handle_file:
            json.dump(data, handle_file, ensure_ascii=False)
        if code(sid, legacy) != "ok":
            fail("旧句柄没有读时升级")
        upgraded = sessions.content_info(sid, legacy)
        if not upgraded.get("expires_at") or upgraded["state"] != "active":
            fail("旧句柄升级后没有过期时间")
        if code(other, legacy) != "handle_not_found":
            fail("旧句柄升级后能跨会话")
    finally:
        store.clock = real_clock
        if saved_ttl is not None:
            os.environ["MAS_HANDLE_TTL_SEC"] = saved_ttl
        shutil.rmtree(root, ignore_errors=True)


def _handle_scope_case(base: str, provider, box: ComfyState, app) -> None:
    """12.B（#58）端到端：模型点名过期、越权 purpose、跨会话、已撤销的句柄出图，都被拒并记进出站审计。"""
    if app is None:
        return
    _handle_units()
    real_clock = store.clock
    secret = "作用域检查的本机正文-丙"
    session_id = _fresh_session(base)
    other = _fresh_session(base)
    keep = app.sessions.add_content(session_id, secret + "keep", "keep")["handle"]
    image = app.sessions.add_content(session_id, secret, "image_prompt")["handle"]
    revoked = app.sessions.add_content(session_id, secret + "revoked", "image_prompt")["handle"]
    status, body = request("POST", base + "/api/resources/revoke", {"id": f"prompt:{session_id}:{revoked}"})
    if status != 200 or json.loads(body).get("state") != "revoked":
        fail(f"资源面板撤销句柄失败：{status} {body}")
    listed = json.loads(request("GET", base + f"/api/resources?session={session_id}")[1])["items"]
    states = {item["handle"]: item.get("state") for item in listed}
    if states.get(revoked) != "revoked" or states.get(image) != "active":
        fail(f"资源面板的句柄状态不对：{states}")
    cases = [
        ("handle_forbidden", session_id, keep, None),
        ("handle_expired", session_id, image, store.HANDLE_TTL_DEFAULT + 5),
        ("handle_not_found", other, image, None),
        ("handle_expired", session_id, revoked, None),
    ]
    provider.mode = "named"
    box.set_mode("ok")
    try:
        for expect, sid, handle, skew in cases:
            provider.named = handle
            before = len(box.prompts)
            sent = len(provider.requests)
            if skew:
                now = real_clock()
                store.clock = lambda: now + skew
            try:
                events = _speak(base, sid, "画一张作用域检查的图", timeout=12)
            finally:
                store.clock = real_clock
            if len(box.prompts) != before:
                fail(f"{expect}：被拒的句柄仍然提交了 ComfyUI")
            if any(secret in _provider_blob(item) for item in provider.requests[sent:]):
                fail(f"{expect}：本地正文进了模型请求")
            errors = [event for event in events if event.get("type") == "error"]
            if not errors or errors[-1].get("code") != expect:
                fail(f"{expect}：错误码不对：{[e.get('code') for e in errors]}")
            if not events or events[-1].get("type") != "run.failed":
                fail(f"{expect}：被拒的句柄没有让这次运行失败")
            if any(event.get("type") == "artifact" for event in events):
                fail(f"{expect}：被拒的句柄出了图")
            status, raw = request("GET", base + "/api/egress?limit=5000")
            if secret in raw or handle in raw:
                fail(f"{expect}：出站审计里出现了正文或完整句柄")
            rows = [row for row in json.loads(raw)["events"]
                    if row.get("type") == "egress.blocked" and row.get("reason_code") == expect
                    and row.get("handle_id_prefix") == handle[:6]]
            if not rows:
                fail(f"{expect}：句柄被拒没有记进出站审计")
    finally:
        provider.named = ""
        provider.mode = "ok"
    print("handle scope ok")


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
    if [event.get("type") for event in message.get("events") or []] != ["content_request", "tool_call", "progress", "artifact", "image_config"]:
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
    if len(provider.requests) != sent + 8:
        fail(f"收下本地提示词后供应商请求次数不对：{len(provider.requests) - sent}")
    for item in provider.requests[sent:]:
        if secret in _provider_blob(item):
            fail("收下本地提示词后把正文发给了模型")
    if secret in json.dumps(events, ensure_ascii=False):
        fail("收下本地提示词后把正文写进了页面")


def _local_early_stop_case(base: str, provider, box: ComfyState) -> None:
    import chat_run

    if hasattr(chat_run, "_requested_count"):
        fail("张数不该由本地正则判断，应由模型调用 plan_images 报出")
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
    if not any(event.get("type") == "tool_call" and event.get("tool") == "plan_images" for event in events):
        fail("多张出图前没有走 plan_images")


def _local_ask_case(base: str, provider, box: ComfyState) -> None:
    provider.mode = "localask"
    box.set_mode("ok")
    session_id = _fresh_session(base)
    secret = "模型先追问时的本地提示词-不应外发"
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak(base, session_id, "帮我连续生成5张图片", timeout=30, content=secret)
    kinds = [event.get("type") for event in events]
    order = [
        event.get("tool") if event.get("type") == "tool_call" else event.get("type")
        for event in events
        if (event.get("type") == "tool_call" and event.get("status") == "running") or event.get("type") == "content_request"
    ]
    if order[:3] != ["plan_images", "accept_local_content", "content_request"]:
        fail(f"模型先回文字追问后，没有被纠正成 plan_images → 收集提示词：{order}")
    arts = [kind for kind in kinds if kind == "artifact"]
    if len(arts) != 5 or len(box.prompts) != before + 5:
        fail(f"纠正后没有做满五张：{len(arts)} {len(box.prompts) - before} {kinds}")
    if events[-1].get("type") != "message.completed":
        fail(f"纠正后连续出图没有正常结束：{events[-1]}")
    blob = json.dumps([item.get("body") for item in provider.requests[sent:]], ensure_ascii=False)
    if blob.count("这是出图请求") < 1 or secret in blob:
        fail("纠正指令没有发给模型，或者把本地正文发给了模型")
    tools = json.dumps(provider.requests[sent].get("body", {}).get("tools") or [], ensure_ascii=False)
    if "弹出输入框" not in tools or "绝不要让用户把提示词贴到聊天里" not in tools:
        fail("accept_local_content 的说明没有讲清楚是本机输入框")


def _workflow_classes() -> set:
    found = set()
    folder = os.path.join(ROOT, "poc", "comfyui", "workflows")
    for name in os.listdir(folder):
        if name.endswith("_api.json"):
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                graph = json.load(handle)
            found.update(node.get("class_type") for node in graph.values() if isinstance(node, dict))
    return found


def _tool_config_case(base: str, provider, box: ComfyState, directory: str, port: int) -> None:
    import chat_run

    tools_path = os.path.join(directory, "tools.json")
    if not os.path.isfile(os.path.join(directory, "provider.json")):
        fail("工具配置用例找不到 provider.json 的目录")
    status, body = request("GET", base + "/api/tools")
    listing = json.loads(body)
    if status != 200 or [item["name"] for item in listing.get("backends") or []] != ["comfyui"]:
        fail(f"后端列表不对：{body}")
    if "comfyui" in [tool["name"] for tool in listing.get("tools") or []]:
        fail("下拉框的工具组里出现了后端名 comfyui")
    view = json.loads(request("GET", base + "/api/tools/comfyui/config")[1])
    fields = {field["name"]: field for field in view.get("fields") or []}
    for name in ("base_url", "auth_token", "output_dir", "image_workflow", "image_fields", "video_i2v_workflow",
                 "image_timeout_sec", "default_aspect_ratio", "steps", "seed_policy", "seed"):
        if name not in fields:
            fail(f"ComfyUI 配置声明缺 {name}")
    for field in fields.values():
        if field["type"] not in ("string", "int", "enum", "path", "secret") or not {"default", "required", "secret", "description"} <= set(field):
            fail(f"配置声明格式不对：{field}")
    if view["values"]["base_url"] != {"source": "env", "value": f"http://127.0.0.1:{port}"}:
        fail(f"环境变量 COMFY_URL 没有标成 source=env：{view['values']['base_url']}")
    if view["values"]["image_workflow"].get("source") != "default" or view["values"]["image_timeout_sec"].get("value") != 20:
        fail(f"现有 poc 配置没有迁成默认值：{view['values']['image_workflow']} {view['values']['image_timeout_sec']}")
    if os.path.exists(tools_path):
        fail("没保存过就写出了 tools.json")
    status, body = request("PUT", base + "/api/tools/comfyui/config", {"values": {
        "base_url": "ftp://nowhere", "steps": "many", "image_workflow": "workflows/none_api.json",
        "seed_policy": "sometimes", "bogus": 1,
    }})
    errors = json.loads(body).get("errors") or {}
    if status != 400 or set(errors) != {"base_url", "steps", "image_workflow", "seed_policy", "bogus"}:
        fail(f"工具配置校验没有按字段报错：{status} {body}")
    status, body = request("PUT", base + "/api/tools/comfyui/config", {"values": {"image_fields": "workflows/video.fields.json"}})
    if status != 400 or "image_fields" not in (json.loads(body).get("errors") or {}):
        fail(f"模板和字段对照不匹配时没有拒绝：{body}")
    if os.path.exists(tools_path):
        fail("校验失败仍写了 tools.json")
    token = "comfy-token-ABCD9876"
    status, body = request("PUT", base + "/api/tools/comfyui/config", {"values": {
        "auth_token": token, "steps": 13, "base_url": "http://127.0.0.1:1",
    }})
    if status != 200 or token in body:
        fail(f"保存工具配置失败或回传了保密原值：{status}")
    saved = json.loads(body)
    if saved["values"]["auth_token"] != {"source": "saved", "configured": True, "hint": "9876"}:
        fail(f"保密字段没有掩码提示：{saved['values']['auth_token']}")
    if saved["values"]["base_url"]["source"] != "env":
        fail("保存的地址盖过了环境变量")
    with open(tools_path, encoding="utf-8") as handle:
        stored = json.load(handle)
    if stored.get("comfyui", {}).get("auth_token") != token or stored["comfyui"].get("steps") != 13:
        fail(f"tools.json 内容不对：{stored}")
    status, body = request("PUT", base + "/api/tools/comfyui/config", {"values": {"steps": 13}})
    if status != 200 or json.loads(body)["values"]["auth_token"].get("configured") is not True:
        fail("不带保密字段的保存把原值清掉了")
    if token in request("GET", base + "/api/tools/comfyui/config")[1]:
        fail("读取工具配置时回传了保密原值")
    # 运行时每次现读：不重启，下一次出图就用新步数；地址仍以环境变量为准。
    provider.mode = "image"
    box.set_mode("ok")
    fields_map = _image_fields(os.path.join(ROOT, "poc", "comfyui", "workflows", "image_api.json"))
    session_id = _fresh_session(base)
    before = len(box.prompts)
    events = _speak(base, session_id, "画一只配置茶壶", timeout=12)
    if events[-1].get("type") != "message.completed" or len(box.prompts) != before + 1:
        fail(f"保存工具配置后出图失败：{events[-1]}")
    if _field_input(box.prompts[-1]["prompt"], fields_map, "steps") != 13:
        fail("出图没有用保存的步数")
    bearer = "Bearer " + token
    seen = {name for name, value in box.auth[-40:] if value == bearer}
    for name in ("prompt", "history", "view", "ws"):
        if name not in seen:
            fail(f"ComfyUI 请求 /{name} 没有带保存的令牌：{sorted(seen)}")
    _auth_cancel_case(box, port, bearer)
    _video_defaults_case(base, box)
    status, body = request("PUT", base + "/api/tools/comfyui/config", {"values": {"steps": 0}})
    _speak(base, session_id, "画一只默认茶壶", timeout=12)
    with open(os.path.join(ROOT, "poc", "comfyui", "workflows", "image_api.json"), encoding="utf-8") as handle:
        template_steps = _field_input(json.load(handle), fields_map, "steps")
    if _field_input(box.prompts[-1]["prompt"], fields_map, "steps") != template_steps:
        fail("改回默认步数后下一次出图没有恢复模板值")
    request("PUT", base + "/api/tools/comfyui/config", {"values": {"default_aspect_ratio": "16:9"}})
    if chat_run._aspect_from_text("画一只猫") != "16:9":
        fail("默认尺寸没有每次现读")
    # 测试连接：能连上、缺节点、连不上。
    status, body = request("POST", base + "/api/tools/comfyui/test")
    result = json.loads(body)
    if status != 200 or result.get("ok") is not True:
        fail(f"测试连接没有通过：{body}")
    box.missing_nodes = {"KSampler"}
    try:
        result = json.loads(request("POST", base + "/api/tools/comfyui/test")[1])
    finally:
        box.missing_nodes = set()
    if result.get("code") != "missing_nodes" or "KSampler" not in result.get("missing", {}).get("image", []):
        fail(f"测试连接没有指出缺少的节点：{result}")
    saved_url = os.environ["COMFY_URL"]
    os.environ["COMFY_URL"] = "http://127.0.0.1:1"
    try:
        result = json.loads(request("POST", base + "/api/tools/comfyui/test", timeout=10)[1])
    finally:
        os.environ["COMFY_URL"] = saved_url
    if result.get("code") != "unreachable":
        fail(f"测试连接没有区分连不上：{result}")
    status, body = request("PUT", base + "/api/tools/comfyui/config", {"reset": True, "values": {}})
    view = json.loads(body)
    with open(tools_path, encoding="utf-8") as handle:
        if "comfyui" in json.load(handle):
            fail("恢复默认值后 tools.json 仍有保存值")
    if view["values"]["steps"]["source"] != "default" or view["values"]["auth_token"].get("configured"):
        fail("恢复默认值后视图不对")
    script = request("GET", base + "/static/app.js")[1]
    page = request("GET", base + "/")[1]
    if "/api/tools/" not in script or 'id="tool-settings"' not in page or "恢复默认值" not in page or "测试连接" not in page:
        fail("页面没有工具配置表单")


def _auth_cancel_case(box: ComfyState, port: int, bearer: str) -> None:
    import comfy_client

    box.auth.clear()
    comfy_client.ComfyClient(f"http://127.0.0.1:{port}").cancel("p-auth", interrupt=True)
    got = {name: value for name, value in box.auth}
    if got.get("queue") != bearer or got.get("interrupt") != bearer:
        fail(f"取消请求没有带令牌：{got}")
    box.auth.clear()
    comfy_client.ComfyClient(f"http://127.0.0.1:{port}", token="").queued("p-auth")
    if box.auth and box.auth[-1][1]:
        fail("显式空令牌时仍发了 Authorization")


def _video_defaults_case(base: str, box: ComfyState) -> None:
    """网页里没有视频入口；视频工具读同一份配置：固定种子、步数、超时。"""
    sys.path.insert(0, os.path.join(ROOT, "poc"))
    from tools.internal import video

    status, body = request("PUT", base + "/api/tools/comfyui/config", {"values": {
        "video_workflow": "workflows/video_api.json", "video_fields": "workflows/video.fields.json",
        "video_timeout_sec": 15, "seed_policy": "fixed", "seed": 4242, "steps": 6,
    }})
    if status != 200:
        fail(f"保存视频配置失败：{body}")
    box.set_mode("ok")
    before = len(box.prompts)
    result = video.generate({"prompt": "配置视频", "duration_sec": 1, "aspect_ratio": "16:9"})
    if len(box.prompts) != before + 1:
        fail(f"视频工具没有提交：{result}")
    with open(os.path.join(ROOT, "poc", "comfyui", "workflows", "video.fields.json"), encoding="utf-8") as handle:
        fields_map = json.load(handle)
    prompt = box.prompts[-1]["prompt"]

    def values(name):
        specs = fields_map[name] if isinstance(fields_map[name], list) else [fields_map[name]]
        return {prompt[str(item["node"])]["inputs"][item["input"]] for item in specs}

    if values("seed") != {4242} or values("steps") != {6}:
        fail(f"视频没有用固定种子或保存的步数：{result}")
    import comfy_poc
    import template_fill

    if template_fill.load_job(comfy_poc.load_config(), "video")[2] != 15:
        fail("视频超时没有读工具配置")
    request("PUT", base + "/api/tools/comfyui/config", {"values": {
        "video_workflow": None, "video_fields": None, "video_timeout_sec": None, "seed_policy": None, "seed": None,
    }})


def _batch_stop_case(base: str, provider, box: ComfyState) -> None:
    provider.mode = "localmulti"
    box.set_mode("ok")
    session_id = _fresh_session(base)
    before = len(box.prompts)
    box.hold_after = before + 2
    arts = []
    caught = {}

    def watch(event):
        if event.get("type") == "tool.confirm":
            _post_confirm(base, event.get("run_id") or "", True)
        if event.get("type") == "content_request":
            _post_content(base, event.get("run_id") or "", "批量中断用的本地提示词")
        if event.get("type") == "artifact":
            arts.append(event)

    def run():
        caught["events"] = read_sse(
            site_port(base), f"/api/sessions/{session_id}/messages",
            {"text": "用本地提示词连续生成5张图片"}, on_event=watch, timeout=20,
        )

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(box.prompts) < before + 3:
        time.sleep(0.05)
    try:
        if len(box.prompts) < before + 3 or len(arts) != 2:
            fail(f"批量中断没有等到第三张在画：{len(box.prompts) - before} {len(arts)}")
        third = box.ids[before + 2]
        asked = len(provider.requests)
        status, _body = request("POST", base + f"/api/sessions/{session_id}/stop", {})
        if status != 200:
            fail("批量中断的停止请求失败")
        worker.join(10)
    finally:
        box.hold_after = None
    events = caught.get("events", (0, "", []))[2]
    if not events or events[-1].get("type") != "run.failed" or events[-1].get("code") != "cancelled":
        fail(f"批量中断没有以取消结束：{events[-1] if events else ''}")
    if "已停止，完成 2/5 张" not in (events[-1].get("message") or ""):
        fail(f"批量中断的状态行不对：{events[-1].get('message')}")
    if len([event for event in events if event.get("type") == "artifact"]) != 2:
        fail("批量中断后保留的成片不是两张")
    if third not in box.interrupts or not any(isinstance(item, list) and third in item for item in box.deletes):
        fail("批量中断没有取消 ComfyUI 里正在画的第三张")
    time.sleep(0.5)
    if len(box.prompts) != before + 3:
        fail(f"批量中断后还在提交：{len(box.prompts) - before}")
    if len(provider.requests) > asked + 1:
        fail(f"批量中断后还在追问模型（续写）：{len(provider.requests) - asked}")
    status, saved = request("GET", base + f"/api/sessions/{session_id}")
    data = json.loads(saved) if isinstance(saved, (str, bytes)) else saved
    last = (data.get("messages") or [])[-1]
    if last.get("status") != "cancelled" or len([e for e in last.get("events") or [] if e.get("type") == "artifact"]) != 2:
        fail(f"批量中断后保存的消息不对：{last.get('status')}")
    script = request("GET", base + "/static/app.js")[1]
    if 'failure.code === "cancelled")) {' not in script:
        fail("有成片时页面不显示已停止的状态行")


class RemoteImageHandler(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容图片服务：只认 /images/generations，记录请求。"""

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.calls.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization") or ""})
        if self.path != "/v1/images/generations":
            self._reply(404, {"error": {"message": "not found"}})
            return
        if self.headers.get("Authorization") != "Bearer " + self.server.key:
            self._reply(401, {"error": {"message": "bad key"}})
            return
        self._reply(200, {"data": [{"b64_json": base64.b64encode(_png(16, 16)).decode("ascii")}]})

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_REMOTE_ENV = ("MAS_IMAGE_API_BASE", "MAS_IMAGE_API_KEY", "MAS_IMAGE_API_MODEL")


def _config_event(events: list[dict]) -> dict:
    found = [event for event in events if event.get("type") == "image_config"]
    if len(found) != 1:
        fail(f"出图后没有一条配置说明：{[event.get('type') for event in events]}")
    return found[0]


def _gen_results(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") == "tool_call" and event.get("tool") == "generate_image"
            and event.get("status") in ("success", "failed")]


def _image_skill_cases(base: str, provider, box: ComfyState, directory: str) -> None:
    """#49：3 种来源 × 2 种后端 × 张数（指定/默认），隐私、歧义、错误和配置说明。"""
    saved = {name: os.environ.get(name) for name in _REMOTE_ENV}
    remote = ThreadingHTTPServer(("127.0.0.1", 0), RemoteImageHandler)
    remote.daemon_threads = True
    remote.calls = []
    remote.key = "sk-fake-image-key-49"
    threading.Thread(target=remote.serve_forever, daemon=True).start()
    os.environ["MAS_IMAGE_API_BASE"] = f"http://127.0.0.1:{remote.server_address[1]}/v1"
    os.environ["MAS_IMAGE_API_KEY"] = remote.key
    os.environ["MAS_IMAGE_API_MODEL"] = "fake-image-1"
    provider.mode = "imageskill"
    box.set_mode("ok")
    try:
        _image_skill_matrix(base, provider, box, remote)
        existed = os.path.exists(os.path.join(directory, "tools.json"))
        _image_skill_local_remote(base, provider, box, remote)
        if not existed:
            os.remove(os.path.join(directory, "tools.json"))
        _image_skill_errors(base, provider, remote)
        _image_skill_launder()
    finally:
        remote.shutdown()
        remote.server_close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    print("image skill matrix ok")


def _image_run(base, provider, box, remote, text, content=None):
    session_id = _fresh_session(base)
    sent, boxed, called = len(provider.requests), len(box.prompts), len(remote.calls)
    events = _speak(base, session_id, text, timeout=40, content=content)
    if events[-1].get("type") != "message.completed":
        fail(f"出图没有正常结束：{text} {events[-1]}")
    return events, provider.requests[sent:], box.prompts[boxed:], remote.calls[called:]


def _image_skill_matrix(base, provider, box, remote) -> None:
    secret = "本机雪山提示词-第三方不可见-49"
    cases = [
        # (原话, 本地正文, 来源, 后端, 张数, 是否指定张数, 出图服务应收到的文字)
        ("原样出图：月光茶壶水墨", None, "chat", "local", 1, False, "月光茶壶水墨"),
        ("用第三方原样出三张图：赛博朋克猫", None, "chat", "remote", 3, True, "赛博朋克猫"),
        ("帮我优化提示词，出三张图：雨夜街景", None, "model", "local", 3, True, "模型写的提示词：雨夜街景，电影感光影"),
        ("优化提示词后用第三方出图：山间小屋", None, "model", "remote", 1, False, "模型写的提示词：山间小屋，电影感光影"),
        ("来三张风景，先输入本地提示词", secret, "local", "local", 3, True, secret),
        ("来一张风景，先输入本地提示词", secret, "local", "local", 1, False, secret),
    ]
    for text, content, source, backend, count, specified, expect in cases:
        events, requests, prompts, calls = _image_run(base, provider, box, remote, text, content)
        arts = [event for event in events if event.get("type") == "artifact"]
        results = _gen_results(events)
        if len(arts) != count or (results and len(results) != count):
            fail(f"张数不对：{text} 期望 {count}，成片 {len(arts)}，调用 {len(results)}")
        if backend == "remote":
            if prompts or len(calls) != count or any(call["body"].get("prompt") != expect for call in calls):
                fail(f"第三方后端没有按提示词出图：{text} {[c['body'] for c in calls]} {len(prompts)}")
            if any(call["body"].get("n") != 1 or call["body"].get("model") != "fake-image-1" for call in calls):
                fail(f"第三方请求不是一次一张或模型不对：{calls[0]['body']}")
        else:
            if calls or len(prompts) != count or not all(expect in json.dumps(p, ensure_ascii=False) for p in prompts):
                fail(f"本机后端没有按提示词出图：{text} {len(calls)} {len(prompts)}")
        config = _config_event(events)
        if config.get("prompt_source") != [source] or config.get("backend") != [backend] or config.get("count") != count:
            fail(f"配置说明不对：{text} {config}")
        summary = config.get("summary") or ""
        if "提示词来源" not in summary or "出图" not in summary or f"{count} 张" not in summary:
            fail(f"配置说明缺维度：{summary}")
        if specified != ("计划" in summary) or (not specified and "默认 1 张" not in summary):
            fail(f"张数默认值没有说明：{text} {summary}")
        if backend == "local" and "本地 ComfyUI（默认）" not in summary:
            fail(f"没说明默认用本机后端：{summary}")
        if backend == "remote" and "fake-image-1" not in summary:
            fail(f"没说明第三方模型：{summary}")
        blob = json.dumps([item.get("body") for item in requests], ensure_ascii=False)
        if secret in blob:
            fail(f"本地提示词进入了对话模型请求：{text}")
        if remote.key in json.dumps(events, ensure_ascii=False) or remote.key in blob:
            fail("第三方 key 出现在事件或模型请求里")


def _image_skill_local_remote(base, provider, box, remote) -> None:
    secret = "本机海边提示词-默认不发第三方-49"
    events, requests, prompts, calls = _image_run(
        base, provider, box, remote, "用第三方出图，提示词我在本机输入", secret)
    results = _gen_results(events)
    error = (results[-1].get("result") or {}).get("error") if results else None
    blob = json.dumps(events, ensure_ascii=False)
    if calls or prompts or "local_prompt_remote_refused" not in blob or "remote_local_prompt" not in blob:
        fail(f"本地提示词默认没有拒绝发给第三方：{len(calls)} {error}")
    if any(event.get("type") == "artifact" for event in events):
        fail("拒绝后不该有成片")
    if secret in json.dumps([item.get("body") for item in requests], ensure_ascii=False):
        fail("本地提示词进入了对话模型请求（拒绝路径）")
    refused = _egress_since(base, events, (secret,))
    if not any(item["type"] == "egress.blocked" and item.get("reason_code") == "local_prompt_remote_refused"
               and item["channel"] == "remote_image" and item.get("contains_local_derived") for item in refused):
        fail(f"本地提示词被拒没有记出站拦截：{[(i['type'], i.get('reason_code')) for i in refused]}")
    status, body = request("PUT", base + "/api/tools/generate_image/config", {"values": {"remote_local_prompt": "confirm"}})
    if status != 200:
        fail(f"改不了 remote_local_prompt：{status} {body}")
    try:
        events, requests, prompts, calls = _image_run(
            base, provider, box, remote, "用第三方出图，提示词我在本机输入", secret)
        notices = [event for event in events if event.get("type") == "tool.confirm" and event.get("notice")]
        if not notices or "不会发给对话模型" not in notices[0]["notice"] or secret in notices[0]["notice"]:
            fail(f"发第三方前没有专门的确认说明：{notices}")
        if len(calls) != 1 or calls[0]["body"].get("prompt") != secret or prompts:
            fail(f"确认后本地提示词没有只发给第三方出图服务：{[c['body'] for c in calls]}")
        if secret in json.dumps([item.get("body") for item in requests], ensure_ascii=False):
            fail("确认后本地提示词进入了对话模型请求")
        config = _config_event(events)
        if config.get("prompt_source") != ["local"] or config.get("backend") != ["remote"]:
            fail(f"确认路径的配置说明不对：{config}")
        summary = notices[0].get("egress") or {}
        if summary.get("destination") != "third_party_image" or summary.get("handle_source") != "local" \
                or summary.get("destination_scope") != "local" or secret in json.dumps(summary, ensure_ascii=False):
            fail(f"第三方出图确认卡的出站摘要不对：{summary}")
        sent = _egress_since(base, events, (secret, remote.key))
        remote_sent = [item for item in sent if item["channel"] == "remote_image" and item["type"] == "egress.sent"]
        if len(remote_sent) != 1 or not remote_sent[0].get("contains_local_derived") or not remote_sent[0].get("handle_id_prefix"):
            fail(f"本地提示词发第三方没有记出站：{sent}")
    finally:
        request("PUT", base + "/api/tools/generate_image/config", {"values": {"remote_local_prompt": "refuse"}})


def _egress_since(base: str, events: list[dict], secrets=()) -> list[dict]:
    """这一轮（按事件流里推出来的 egress_id）在审计文件里的记录；顺带确认没有正文和密钥。"""
    ids = {event.get("egress_id") for event in events if str(event.get("type", "")).startswith("egress.")}
    status, body = request("GET", base + "/api/egress?limit=5000")
    if status != 200:
        fail(f"读出站审计失败：{status}")
    if SECRET in body or any(secret in body for secret in secrets):
        fail("出站审计里出现了密钥或本地正文")
    return [item for item in json.loads(body)["events"] if item.get("egress_id") in ids]


def _image_skill_errors(base, provider, remote) -> None:
    def attempt(expect: str) -> str:
        session_id = _fresh_session(base)
        events = _speak(base, session_id, "用第三方原样出图：小猫", timeout=30)
        blob = json.dumps(events, ensure_ascii=False)
        if expect not in blob or '"hint"' not in blob:
            fail(f"第三方出图错误没有给出 {expect} 和建议：{[e.get('type') for e in events]}")
        if remote.key in blob:
            fail("错误信息里出现了 key")
        _config_event(events)
        return blob

    good = os.environ["MAS_IMAGE_API_BASE"]
    os.environ["MAS_IMAGE_API_KEY"] = ""
    attempt("remote_missing_key")
    os.environ["MAS_IMAGE_API_KEY"] = "sk-wrong"
    attempt("remote_auth")
    os.environ["MAS_IMAGE_API_KEY"] = remote.key
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    os.environ["MAS_IMAGE_API_BASE"] = f"http://127.0.0.1:{port}/v1"
    attempt("remote_unreachable")
    os.environ["MAS_IMAGE_API_BASE"] = ""
    attempt("remote_not_configured")
    os.environ["MAS_IMAGE_API_BASE"] = good


def _image_skill_launder() -> None:
    import chat_run

    class Sessions:
        def local_bodies(self, session_id):
            return ["本机正文不能洗白成可见提示词"]

    class App:
        sessions = Sessions()

    class FakeRun:
        run_id = "r_test"

    run = FakeRun()
    refused = chat_run._execute_stage_prompt(App(), "s", run, {"visible_prompt": "请画：本机正文不能洗白成可见提示词", "source": "model"})
    if refused.get("ok") or refused["error"]["code"] != "local_content":
        fail(f"stage_prompt 让本地正文换成了可见句柄：{refused}")
    ok = chat_run._execute_stage_prompt(App(), "s", run, {"visible_prompt": "一只橘猫", "source": "chat"})
    again = chat_run._execute_stage_prompt(App(), "s", run, {"visible_prompt": "一只橘猫", "source": "chat"})
    if not ok.get("ok") or ok["content_handle"] != again["content_handle"] or not ok["content_handle"].startswith("c_"):
        fail(f"stage_prompt 没有返回稳定句柄：{ok} {again}")
    bad = chat_run._execute_stage_prompt(App(), "s", run, {"visible_prompt": "x", "source": "local"})
    if bad.get("ok"):
        fail("stage_prompt 接受了非法来源")


def _local_plan_case(base: str, provider, box: ComfyState) -> None:
    provider.mode = "localplan"
    box.set_mode("ok")
    session_id = _fresh_session(base)
    secret = "四季风景的本地提示词-不应外发"
    before = len(box.prompts)
    sent = len(provider.requests)
    events = _speak(base, session_id, "来一组四季风景，先输入本地提示词", timeout=30, content=secret)
    kinds = [event.get("type") for event in events]
    if "content_request" not in kinds:
        fail(f"没有数字的多图请求没有先收本地提示词：{kinds}")
    arts = [kind for kind in kinds if kind == "artifact"]
    if len(arts) != 4 or len(box.prompts) != before + 4:
        fail(f"模型计划四张时没有做满四张：{len(arts)} {len(box.prompts) - before} {kinds}")
    if events[-1].get("type") != "message.completed" or _assistant_text(events) != "四季都好了":
        fail(f"按计划出图没有正常结束：{events[-1]} {_assistant_text(events)!r}")
    blob = json.dumps([item.get("body") for item in provider.requests[sent:]], ensure_ascii=False)
    if "已生成 1/4 张" not in blob or 'images_total\\": 4' not in blob:
        fail("没有按 plan_images 的张数报进度和续写")
    if secret in blob:
        fail("按计划出图时把本地正文发给了模型")


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


def _egress_http_case(base: str, provider, directory: str) -> None:
    """#57：一次真实聊天留下出站记录；接口只给本机、校验会话 id、导出带附件头；消息上留一份给页面回放。"""
    import io
    import server as web_server

    provider.mode = "ok"
    session_id = _fresh_session(base)
    events = _speak(base, session_id, "只聊天，随便说一句")
    pushed = [event for event in events if str(event.get("type", "")).startswith("egress.")]
    if [event["type"] for event in pushed] != ["egress.planned", "egress.sent"]:
        fail(f"事件流里没有推出站记录：{[event.get('type') for event in pushed]}")
    status, body = request("GET", base + f"/api/egress?session_id={session_id}")
    data = json.loads(body)
    if status != 200 or data.get("schema") != "mas.egress.export/1":
        fail(f"出站审计接口不对：{status}")
    kinds = [item["type"] for item in data["events"]]
    if kinds != ["egress.planned", "egress.sent"] or any(item.get("session_id") != session_id for item in data["events"]):
        fail(f"出站审计没按会话归档：{kinds}")
    if data["events"][0].get("run_id") != pushed[0].get("run_id") or not data["events"][0].get("trace_id"):
        fail("出站记录缺运行标识")
    if SECRET in body or "随便说一句" in body:
        fail("出站审计接口里出现了密钥或正文")
    if os.path.join(directory, "egress.jsonl") != __import__("services.gateway.egress", fromlist=["path"]).path():
        fail("出站审计没写进数据目录")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    if [item["type"] for item in stored["messages"][-1].get("egress") or []] != ["egress.planned", "egress.sent"]:
        fail("消息上没有留出站记录，刷新后时间线会丢")
    if [item.get("type") for item in stored["messages"][-1].get("events") or []] != []:
        fail("出站记录混进了消息事件")
    req = urllib.request.Request(base + f"/api/egress/export?session_id={session_id}")
    with urllib.request.urlopen(req, timeout=5) as response:
        disposition = response.headers.get("Content-Disposition") or ""
        exported = json.loads(response.read().decode("utf-8"))
    if f"egress-{session_id}.json" not in disposition or len(exported["events"]) != 2:
        fail("导出没有附件头或内容不对")
    status, _ = request("GET", base + "/api/egress?session_id=../x")
    if status != 400:
        fail("坏的 session_id 没有被拒")

    class _Fake:
        client_address = ("10.0.0.5", 1234)
        def __init__(self):
            self.status = None
            self.wfile = io.BytesIO()
        def send_response(self, code):
            self.status = code
        def send_header(self, *_a):
            pass
        def end_headers(self):
            pass
    fake = _Fake()
    web_server._egress_route(fake, "/api/egress", {})
    if fake.status != 403 or b"egress.sent" in fake.wfile.getvalue():
        fail("非本机能读出站审计")


# ---------- #31 工具包 ----------

BUILTIN_TOOLS = {"accept_local_content", "list_local_content", "forget_local_content", "retrieve_local", "plan_images",
                 "generate_image", "generate_video", "build_skill_page", "read_skill", "lookup_demo", "stage_prompt", "character_image"}


def _packs_module():
    poc_root = os.path.join(ROOT, "poc")
    if poc_root not in sys.path:
        sys.path.append(poc_root)
    from tools import packs

    return packs


def _write_pack(folder: str, manifest: dict, code: str | None = None) -> str:
    target = os.path.join(folder, manifest["name"])
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "tool.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)
    if code is not None:
        with open(os.path.join(target, "tool.py"), "w", encoding="utf-8") as handle:
            handle.write(code)
    return target


def _pack_manifest(name: str, **extra) -> dict:
    manifest = {
        "schema": "mas.tool/1", "name": name, "version": "1.0.0", "description": f"{name} 测试工具。",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {"n": {"type": "integer"}}},
        "mutating": False, "confirm": "never", "entry": {"kind": "python", "module": "tool.py"},
        "config": [], "requires": {"backends": [], "python": []},
        "permissions": {"read_local_content": False, "network": False, "write_files": False, "send_local_to_model": False},
    }
    manifest.update(extra)
    return manifest


_PACK_CODE = "def call(arguments, config):\n    return {'ok': True, 'executed': True, 'n': arguments.get('n'), 'config': sorted(config)}\n"


def _tool_pack_guide_cases() -> None:
    """指南里的每份示例清单都要过真实校验；dice_roll 的示例和仓库里的一致；示例代码能编译；入口抛异常变成 tool_error。"""
    import re as _re

    packs = _packs_module()
    from services.runtime.tools import execute_tool

    guide = open(os.path.join(ROOT, "docs", "tool-pack-guide.md"), encoding="utf-8").read()
    blocks = _re.findall(r"<!-- validate:manifest -->\s*```json\n(.*?)```", guide, _re.S)
    if len(blocks) < 4:
        fail(f"指南里标记校验的示例清单太少：{len(blocks)}")
    names = []
    for raw in blocks:
        data = json.loads(raw)
        problems = packs.validate_manifest(data)
        if problems:
            fail(f"指南示例 {data.get('name')} 过不了校验：{problems}")
        names.append(data["name"])
    with open(os.path.join(ROOT, "toolpacks", "examples", "dice_roll", "tool.json"), encoding="utf-8") as handle:
        if json.loads(blocks[0]) != json.load(handle):
            fail("指南里的 dice_roll 清单和 toolpacks/examples/dice_roll/tool.json 不一致")
    for code in _re.findall(r"```python\n(.*?)```", guide, _re.S):
        if code.lstrip().startswith(("def ", "import ", "from ")):
            compile(code, "tool-pack-guide.md", "exec")
    for message in ("只有内置工具可以用 entry.kind=host。", "第三方工具必须声明 parameters。",
                    "改外部状态的工具不能 confirm=never。", "send_local_to_model 必须是 false"):
        if message not in guide:
            fail(f"指南的错误表和校验原文对不上：{message}")
    folder = tempfile.mkdtemp(prefix="guide-packs-")
    source = os.path.join(folder, "src", "guide_boom")
    os.makedirs(source)
    manifest = json.loads(blocks[0])
    manifest["name"] = "guide_boom"
    with open(os.path.join(source, "tool.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    with open(os.path.join(source, "tool.py"), "w", encoding="utf-8") as handle:
        handle.write("def call(arguments, config):\n    raise RuntimeError('secret-detail')\n")
    saved = os.environ.get("MAS_TOOLPACKS_DIR")
    os.environ["MAS_TOOLPACKS_DIR"] = os.path.join(folder, "installed")
    try:
        packs.install(source, [])
        result = execute_tool("guide_boom", {})
        if result.get("ok") is not False or (result.get("error") or {}).get("code") != "tool_error" \
                or "secret-detail" in json.dumps(result, ensure_ascii=False):
            fail(f"入口抛异常没有变成 tool_error：{result}")
    finally:
        if saved is None:
            os.environ.pop("MAS_TOOLPACKS_DIR", None)
        else:
            os.environ["MAS_TOOLPACKS_DIR"] = saved
        shutil.rmtree(folder, ignore_errors=True)


def _toolpack_cases() -> None:
    """清单校验、发现、依赖缺失、权限拒绝、确认策略。"""
    packs = _packs_module()
    from services.runtime.loop import _asks_confirm
    from services.runtime.tools import execute_tool, openai_tools, tool_mutates
    from tools.registry import audience_tools

    found = {pack.name: pack for pack in packs.discover()}
    if set(found) != BUILTIN_TOOLS or not all(pack.builtin for pack in found.values()):
        fail(f"内置工具清单不全：{sorted(found)}")
    for pack in found.values():
        problems = packs.validate_manifest(pack.manifest, builtin=True)
        if problems:
            fail(f"{pack.name} 的清单不合格：{problems}")
    exposed = [item["function"]["name"] for item in openai_tools()]
    if set(exposed) != BUILTIN_TOOLS - {"generate_video"}:
        fail(f"给模型的工具不是按清单来的：{exposed}")
    image = next(item for item in openai_tools() if item["function"]["name"] == "generate_image")["function"]
    if "prompt" in image["parameters"]["properties"] or "count" in image["parameters"]["properties"] or "n" in image["parameters"]["properties"]:
        fail("generate_image 的模型参数里出现了 prompt/count/n")
    if not image["parameters"]["properties"].get("style", {}).get("enum"):
        fail("generate_image 的 style 白名单没有从 parameter_sources 填进去")
    plan = found["plan_images"].manifest["parameters"]["properties"]["count"]
    if plan.get("minimum") != 1 or plan.get("maximum") != 20:
        fail("plan_images 的张数范围变了")
    if [item.name for item in audience_tools("internal")] != ["generate_image", "lookup_demo", "read_skill", "generate_video"] and \
            {item.name for item in audience_tools("internal")} != {"generate_image", "lookup_demo", "read_skill", "generate_video"}:
        fail("本机实现没有按清单的 entry 装载")
    for name, pack in found.items():
        if tool_mutates(name) != (pack.manifest["confirm"] != "never"):
            fail(f"{name} 的确认策略没有按清单生效")
    if not tool_mutates("generate_image") or tool_mutates("plan_images") or not tool_mutates("accept_local_content"):
        fail("内置工具的确认策略变了")
    if found["generate_image"].manifest["permissions"]["send_local_to_model"]:
        fail("generate_image 不能把本地内容交给模型")

    bad = {
        "send_local_to_model": _pack_manifest("bad_one", permissions={"send_local_to_model": True}),
        "prompt": _pack_manifest("bad_two", parameters={"type": "object", "additionalProperties": False, "properties": {"prompt": {"type": "string"}}}),
        "host": _pack_manifest("bad_three", entry={"kind": "host"}),
        "confirm=never": _pack_manifest("bad_four", mutating=True, confirm="never"),
        "version": _pack_manifest("bad_five", version="1"),
        "默认值": _pack_manifest("bad_six", config=[{"name": "key", "type": "secret", "default": "leak", "required": False, "secret": True, "description": ""}]),
        "相对路径": _pack_manifest("bad_seven", entry={"kind": "python", "module": "../evil.py"}),
        "confirm_args": _pack_manifest("bad_eight", confirm="by_args"),
    }
    for word, manifest in bad.items():
        problems = packs.validate_manifest(manifest)
        if not any(word in item for item in problems):
            fail(f"清单校验没有拒绝 {word}：{problems}")
    if packs.validate_manifest(_pack_manifest("good_one")):
        fail("合格的清单被拒绝了")

    folder = tempfile.mkdtemp(prefix="toolpacks-")
    source = tempfile.mkdtemp(prefix="toolpack-src-")
    saved = os.environ.get("MAS_TOOLPACKS_DIR")
    os.environ["MAS_TOOLPACKS_DIR"] = folder
    try:
        # 依赖缺失：稳定错误码 dependency_missing，不执行。
        needy = _write_pack(source, _pack_manifest("needs_backend", requires={"backends": ["nowhere"], "python": ["no_such_pkg_31"]}), _PACK_CODE)
        info = packs.inspect(needy)
        if info["problems"] or len(info.get("missing") or []) != 2:
            fail(f"检查工具包时没有报出缺的依赖：{info}")
        packs.install(needy, [])
        result = execute_tool("needs_backend", {"n": 1})
        if result.get("error", {}).get("code") != "dependency_missing" or result.get("executed") is not False:
            fail(f"缺依赖时没有稳定错误码：{result}")
        # 权限：安装要确认；授权记录被改掉后调用 permission_denied。
        netty = _write_pack(source, _pack_manifest("needs_network", permissions={"network": True}), _PACK_CODE)
        try:
            packs.install(netty, [])
        except packs.PackError as exc:
            if exc.code != "permission_not_accepted":
                fail(f"没确认权限就安装的错误码不对：{exc.code}")
        else:
            fail("没确认权限也装上了")
        packs.install(netty, ["network"])
        if execute_tool("needs_network", {"n": 2}).get("n") != 2:
            fail("授权后的工具没有执行")
        with open(os.path.join(folder, "needs_network", ".granted.json"), "w", encoding="utf-8") as handle:
            json.dump({"permissions": []}, handle)
        result = execute_tool("needs_network", {"n": 2})
        if result.get("error", {}).get("code") != "permission_denied":
            fail(f"没授权的权限没有被拒绝：{result}")
        # 确认策略 by_args：带 overwrite 才确认。
        argy = _write_pack(source, _pack_manifest("by_args_tool", mutating=True, confirm="by_args", confirm_args=["overwrite"],
                                                  parameters={"type": "object", "additionalProperties": False,
                                                              "properties": {"overwrite": {"type": "boolean"}}}), _PACK_CODE)
        packs.install(argy, [])
        if tool_mutates("by_args_tool", {}) or not tool_mutates("by_args_tool", {"overwrite": True}) or not tool_mutates("by_args_tool"):
            fail("confirm=by_args 没有按参数生效")
        if _asks_confirm(tool_mutates, "by_args_tool", {}) or not _asks_confirm(tool_mutates, "by_args_tool", {"overwrite": True}):
            fail("运行时没有把参数交给确认策略")
        if not _asks_confirm(lambda name: True, "by_args_tool", {}):
            fail("只收工具名的确认回调不再生效")
        # 重名：不能和内置工具或后端重名。
        clash = _write_pack(source, _pack_manifest("plan_images"), _PACK_CODE)
        if not packs.inspect(clash)["problems"]:
            fail("和内置工具重名的包没有被拒绝")
        backend = _write_pack(source, _pack_manifest("comfyui"), _PACK_CODE)
        if not packs.inspect(backend)["problems"]:
            fail("和后端重名的包没有被拒绝")
        # 坏清单会让发现失败，而不是悄悄跳过。
        broken = os.path.join(folder, "broken_pack")
        os.makedirs(broken)
        with open(os.path.join(broken, "tool.json"), "w", encoding="utf-8") as handle:
            json.dump(_pack_manifest("broken_pack", permissions={"send_local_to_model": True}), handle)
        try:
            packs.discover()
        except packs.PackError as exc:
            if exc.code != "invalid_manifest":
                fail(f"坏清单的错误码不对：{exc.code}")
        else:
            fail("坏清单没有被发现阶段拒绝")
        shutil.rmtree(broken)
        try:
            packs.uninstall("generate_image")
        except packs.PackError as exc:
            if exc.code != "builtin_tool":
                fail(f"卸载内置工具的错误码不对：{exc.code}")
        else:
            fail("内置工具被卸载了")
    finally:
        if saved is None:
            os.environ.pop("MAS_TOOLPACKS_DIR", None)
        else:
            os.environ["MAS_TOOLPACKS_DIR"] = saved
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)
    if set(item["function"]["name"] for item in openai_tools()) != BUILTIN_TOOLS - {"generate_video"}:
        fail("测试装的工具包没有清干净")


def _resources_case(base: str, provider, directory: str) -> None:
    """#51 本地资源面板：列表/预览/标签/下载/复用不进模型；删除后正文从磁盘和轨迹消失；
    共享字节按引用保留；运行中拒删；需要确认；非本机拒绝；页面有入口。"""
    import server as web_server
    from services.meta.export import Exporter

    prompt_secret = "资源面板里的本地提示词正文丙丁"
    text_secret = "资源面板里留下的本机文字戊己"
    # 本地提示词 + 生成图：出图回合。
    provider.mode = "image"
    a = _fresh_session(base)
    events = _speak(base, a, "用本地comfyui生成一幅图片", timeout=20, content=prompt_secret)
    art = next((e for e in events if e.get("type") == "artifact"), None)
    if not art or not art.get("artifact_id"):
        fail("资源用例没生成工件")
    sessions = store.SessionStore(os.path.join(directory, "sessions"))
    text_rec = sessions.add_content(a, text_secret, "keep")
    b, c = _fresh_session(base), _fresh_session(base)
    shared = b"RIFF....WAVEfmt resources-shared"
    handles = {}
    for sid in (b, c):
        status, body = request("POST", base + f"/api/sessions/{sid}/attachments", {
            "file": base64.b64encode(shared).decode("ascii"), "media_type": "audio/wav", "name": "r.wav"})
        if status != 200:
            fail(f"资源用例附件没收下：{status}")
        handles[sid] = json.loads(body)["handle"]
    Exporter(directory).export()
    sent = len(provider.requests)

    status, body = request("GET", base + "/api/resources")
    listing = json.loads(body)
    if status != 200 or prompt_secret in body or text_secret in body or os.path.abspath(directory) in body:
        fail(f"资源列表不对或带出了正文/路径：{status}")
    ids = {i["id"]: i for i in listing["items"]}
    prompt_id = next((i for i in ids if i.startswith(f"prompt:{a}:")), None)
    text_id = f"text:{a}:{text_rec['handle']}"
    art_id = f"artifact:{a}:{art['artifact_id']}"
    att_b, att_c = f"attachment:{b}:{handles[b]}", f"attachment:{c}:{handles[c]}"
    for rid in (prompt_id, text_id, art_id, att_b, att_c):
        if rid not in ids:
            fail(f"资源列表少了一项：{rid}")
    if not ids[att_b]["shared"] or len(ids[att_b]["sessions"]) != 2:
        fail("共享字节的附件没有标出引用会话")
    if ids[art_id]["size"] <= 0 or listing["total"]["bytes"] <= 0 or not ids[prompt_id]["created_at"]:
        fail("资源没有大小、时间或总占用")
    status, body = request("GET", base + "/api/resources?type=attachment&session=" + c)
    got = sorted(i["id"] for i in json.loads(body)["items"])
    if got != sorted([att_b, att_c]):  # 按会话筛选包括引用它字节的别的会话的条目
        fail(f"按类型和会话筛选不对：{got}")
    status, body = request("GET", base + "/api/resources?session=" + a)
    if {i["type"] for i in json.loads(body)["items"]} != {"prompt", "text", "artifact"}:
        fail("按会话筛选混进了别的会话")
    status, body = request("GET", base + "/api/resources?type=bogus")
    if status != 400:
        fail("非法类型没有被拒")
    texts = []
    for pid in [i for i in ids if i.startswith(f"prompt:{a}:")]:
        status, body = request("GET", base + "/api/resources/preview?id=" + urllib.parse.quote(pid))
        texts.append(json.loads(body).get("text") if status == 200 else None)
    if prompt_secret not in texts:
        fail("本机预览拿不到提示词正文")
    prompt_id = [i for i in ids if i.startswith(f"prompt:{a}:")][texts.index(prompt_secret)]
    status, body = request("POST", base + "/api/resources/label", {"id": att_b, "label": "资源标签甲"})
    if status != 200:
        fail(f"改标签失败：{status}")
    status, body = request("POST", base + "/api/resources/label", {"id": art_id, "label": "茶壶图"})
    status, body = request("GET", base + "/api/resources?q=" + urllib.parse.quote("资源标签甲"))
    if [i["id"] for i in json.loads(body)["items"]] != [att_b]:
        fail("按标签搜不到")
    status, raw = request_bytes("GET", base + "/api/resources/raw?download=1&id=" + urllib.parse.quote(att_c))
    if status != 200 or raw != shared:
        fail("下载附件不对")
    status, raw = request_bytes("GET", base + "/api/resources/raw?id=" + urllib.parse.quote(art_id))
    if status != 200 or not raw.startswith(b"\x89PNG"):
        fail("下载生成文件不对")
    if len(provider.requests) != sent:
        fail("列表、预览、标签、下载触发了模型请求")

    # 非本机：403。
    class _Fake:
        client_address = ("10.0.0.5", 1234)
        def __init__(self):
            self.status = None
            self.wfile = io.BytesIO()
        def send_response(self, code):
            self.status = code
        def send_header(self, *_a):
            pass
        def end_headers(self):
            pass
    fake = _Fake()
    web_server._resource_route(fake, None, "GET", "/api/resources", {})
    if fake.status != 403 or prompt_secret.encode() in fake.wfile.getvalue():
        fail("非本机请求没有被拒")

    # 复用：模型只见句柄。
    d = _fresh_session(base)
    status, body = request("POST", base + "/api/resources/reuse", {"id": prompt_id, "session_id": d})
    reused = json.loads(body)
    if status != 200 or prompt_secret in body or reused["handle"] not in reused["snippet"]:
        fail(f"复用没有只返回句柄：{status}")
    if sessions.content_text(d, reused["handle"]) != prompt_secret:
        fail("复用后当前会话里没有这段本地正文")
    provider.mode = "ok"
    sent = len(provider.requests)
    _speak(base, d, reused["snippet"])
    blobs = [_provider_blob(item) for item in provider.requests[sent:]]
    if not blobs or not any(reused["handle"] in x for x in blobs) or any(prompt_secret in x for x in blobs):
        fail("复用后模型请求里没有句柄，或者出现了正文")
    status, body = request("POST", base + "/api/resources/reuse", {"id": att_b, "session_id": d})
    if status != 400:
        fail("附件不该能复用成本地内容")

    # 运行中拒删。
    path = os.path.join(directory, "sessions", a + ".json")
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)
    running = json.loads(json.dumps(saved))
    running["messages"].append({"message_id": "m_" + "1" * 32, "role": "assistant", "text": "",
                                "status": "running", "run_id": "r_" + "1" * 32})
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(running, handle)
    status, body = request("POST", base + "/api/resources/delete", {"ids": [text_id], "confirm": True})
    result = json.loads(body)
    if status != 200 or result["deleted"] or [r["id"] for r in result["refused"]] != [text_id]:
        fail(f"运行中的会话里的资源被删了：{status} {body}")
    if sessions.content_text(a, text_rec["handle"]) != text_secret:
        fail("拒删时内容丢了")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(saved, handle)

    # 共享字节：要确认；删一条后字节留着，另一会话仍能读；两条都删后字节消失。
    blob_path = sessions.artifacts.path_for(hashlib.sha256(shared).hexdigest())
    status, body = request("POST", base + "/api/resources/delete", {"ids": [att_b]})
    if status != 409 or json.loads(body).get("code") != "needs_confirm" or not json.loads(body).get("warnings"):
        fail(f"共享字节的删除没有要求确认：{status}")
    status, body = request("POST", base + "/api/resources/delete", {"ids": [att_b], "confirm": True})
    if status != 200 or json.loads(body)["deleted"] != [att_b]:
        fail(f"确认后没删掉：{status} {body}")
    if not os.path.exists(blob_path) or sessions.attachment_bytes(c, handles[c]) != shared:
        fail("删一个会话的附件误删了共用字节")
    status, body = request("POST", base + "/api/resources/delete", {"ids": [att_c], "confirm": True})
    if status != 200 or os.path.exists(blob_path):
        fail("最后一个引用删掉后附件字节还在")

    # 批量删本地提示词、文字和生成文件：磁盘、会话、轨迹里都没了。
    art_file = None
    import image_run
    art_file = image_run.artifact_store().file_for(a, art["artifact_id"])
    status, body = request("POST", base + "/api/resources/delete",
                           {"ids": [prompt_id, text_id, art_id], "confirm": True})
    result = json.loads(body)
    if status != 200 or sorted(result["deleted"]) != sorted([prompt_id, text_id, art_id]):
        fail(f"批量删除不对：{status} {body}")
    if art_file and os.path.exists(art_file):
        fail("删除后生成文件还在磁盘上")
    status, _raw = request_bytes("GET", base + art["url"])
    if status != 404:
        fail("删除后生成文件还能打开")
    session_raw = open(path, encoding="utf-8").read()
    if art["artifact_id"] in session_raw:
        fail("删除后会话里还留着生成文件的事件")
    for folder, _dirs, files in os.walk(directory):
        for name in files:
            full = os.path.join(folder, name)
            if d in name:
                continue  # 复用到的会话本来就留着一份
            with open(full, "rb") as handle:
                data = handle.read()
            for secret in (prompt_secret, text_secret):
                if secret.encode("utf-8") in data:
                    fail(f"删除后正文还在磁盘上：{os.path.relpath(full, directory)}")
    index = Exporter(directory).load_index()
    if any(h in json.dumps(index["sessions"].get(a, {})) for h in (text_rec["handle"],)):
        fail("轨迹索引还记着删掉的句柄")
    status, body = request("GET", base + "/api/resources")
    if any(rid in body for rid in (prompt_id, text_id, art_id, att_b, att_c)):
        fail("删除后列表里还有")

    status, page = request("GET", base + "/")
    status_js, script = request("GET", base + "/static/resources.js")
    if 'id="open-resources"' not in page or "resources.js" not in page or status_js != 200:
        fail("页面没有本地资源入口")
    if _unsafe_inner_html(script) or "innerHTML" in script:
        fail("资源面板用了 innerHTML")
    print(f"resources {len(listing['items'])} items, {listing['total']['bytes']} bytes")


def _delete_session_case(base: str, directory: str) -> None:
    """删除对话：确认后删会话、附件字节；还在回答的拒绝；别的会话共用的字节留着；页面有删除按钮。"""
    sessions_dir = os.path.join(directory, "sessions")

    def make() -> str:
        status, body = request("POST", base + "/api/sessions", {})
        if status != 200:
            fail(f"建会话失败：{status}")
        return json.loads(body)["session_id"]

    def attach(session_id: str, raw: bytes) -> str:
        status, body = request("POST", base + f"/api/sessions/{session_id}/attachments", {
            "file": base64.b64encode(raw).decode("ascii"), "media_type": "audio/wav", "name": "d.wav",
        })
        if status != 200:
            fail(f"删除用例的附件没收下：{status} {body}")
        return json.loads(body)["handle"]

    victim = make()
    keeper = make()
    own = b"RIFF....WAVEfmt delete-only"
    shared = b"RIFF....WAVEfmt delete-shared"
    attach(victim, own)
    attach(victim, shared)
    keep_handle = attach(keeper, shared)
    from packages.artifacts import ArtifactStore
    blobs = ArtifactStore(os.path.join(directory, "artifacts"))
    own_path = blobs.path_for(hashlib.sha256(own).hexdigest())
    if not own_path or not os.path.exists(own_path):
        fail("附件字节没有落盘")
    os.makedirs(os.path.join(sessions_dir, victim, "files"), exist_ok=True)

    # 还在回答：拒绝，不替用户停。
    path = os.path.join(sessions_dir, keeper + ".json")
    with open(path, encoding="utf-8") as handle:
        saved = json.load(handle)
    running = json.loads(json.dumps(saved))
    running["messages"].append({"message_id": "m_" + "0" * 32, "role": "assistant", "text": "",
                                "status": "running", "run_id": "r_" + "0" * 32})
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(running, handle)
    status, body = request("POST", base + f"/api/sessions/{keeper}/delete", {})
    if status != 409 or json.loads(body).get("code") != "session_running":
        fail(f"还在回答的会话被删了：{status} {body}")
    if not os.path.exists(path):
        fail("拒绝删除时会话文件丢了")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(saved, handle)

    status, body = request("POST", base + f"/api/sessions/{victim}/delete", {})
    if status != 200 or not json.loads(body).get("ok"):
        fail(f"删除会话失败：{status} {body}")
    if os.path.exists(os.path.join(sessions_dir, victim + ".json")) or os.path.exists(os.path.join(sessions_dir, victim)):
        fail("删除后会话文件或目录还在")
    if os.path.exists(own_path):
        fail("删除后只属于这个会话的附件字节还在")
    status, _body = request("GET", base + f"/api/sessions/{keeper}/attachments/{keep_handle}")
    if status != 200:
        fail("删除一个会话误删了别的会话共用的附件")
    status, body = request("GET", base + "/api/sessions")
    if victim in [item["session_id"] for item in json.loads(body)["sessions"]]:
        fail("删除后会话还在列表里")
    status, _body = request("GET", base + f"/api/sessions/{victim}")
    if status != 404:
        fail("删除后还能打开会话")
    status, _body = request("POST", base + f"/api/sessions/{victim}/delete", {})
    if status != 404:
        fail("重复删除没有返回 404")
    status, _body = request("POST", base + f"/api/sessions/{keeper}/delete", {})
    if status != 200:
        fail("停下以后的会话删不掉")
    script = open(os.path.join(ROOT, "apps", "web", "static", "app.js"), encoding="utf-8").read()
    for needle in ("session-delete", "window.confirm", "/delete`", "await createSession(false)"):
        if needle not in script:
            fail(f"页面缺少删除对话的部分：{needle}")


def _toolpack_http_case(base: str, directory: str) -> None:
    """下拉框按工具名列出；通过网页接口安装、配置、调用、导出、在另一份目录装上、卸载。"""
    import io
    import zipfile

    from services.runtime.tools import execute_tool, openai_tools

    status, body = request("GET", base + "/api/tools")
    listing = json.loads(body)
    names = [tool["name"] for tool in listing.get("tools") or []]
    if status != 200 or set(names) != BUILTIN_TOOLS:
        fail(f"下拉框没有按实际工具名列出：{names}")
    for tool in listing["tools"]:
        if not {"version", "permissions", "backends", "builtin", "ready", "exposed"} <= set(tool):
            fail(f"工具列表缺字段：{tool}")
    view = json.loads(request("GET", base + "/api/tools/generate_image/config")[1])
    if view.get("kind") != "tool" or [b["name"] for b in view["pack"]["backend_status"]] != ["comfyui"]:
        fail(f"generate_image 的页面没有显示依赖的后端：{view.get('pack')}")
    names = [field.get("name") for field in view["fields"]]
    if names != ["remote_base_url", "remote_api_key", "remote_model", "remote_local_prompt", "remote_timeout_sec"]:
        fail(f"generate_image 只该有第三方出图的配置项：{names}")
    key_field = next(field for field in view["fields"] if field.get("name") == "remote_api_key")
    if not key_field.get("secret"):
        fail(f"第三方出图的 API key 没有按密钥处理：{key_field}")
    if json.loads(request("GET", base + "/api/tools/comfyui/config")[1]).get("kind") != "backend":
        fail("ComfyUI 没有标成后端")
    script = open(os.path.join(WEB_DIR, "static", "app.js"), encoding="utf-8").read()
    for needle in ("optgroup", "tool.name", "/api/toolpacks/install", "/api/toolpacks/inspect", "/export", "/uninstall", "backend_status"):
        if needle not in script:
            fail(f"工具对话框缺 {needle}")

    folder = tempfile.mkdtemp(prefix="toolpacks-http-")
    other = tempfile.mkdtemp(prefix="toolpacks-other-")
    saved = os.environ.get("MAS_TOOLPACKS_DIR")
    os.environ["MAS_TOOLPACKS_DIR"] = folder
    example = os.path.join(ROOT, "toolpacks", "examples", "dice_roll")
    try:
        status, body = request("POST", base + "/api/toolpacks/inspect", {"source": example})
        info = json.loads(body)
        if status != 200 or info.get("name") != "dice_roll" or info.get("problems") or info.get("permissions") != []:
            fail(f"检查示例工具包失败：{body}")
        status, body = request("POST", base + "/api/toolpacks/install", {"source": example})
        if status != 400 or json.loads(body).get("code") != "permission_not_accepted":
            fail(f"没确认权限的安装没有被拒绝：{status} {body}")
        status, body = request("POST", base + "/api/toolpacks/install", {"source": example, "accept_permissions": []})
        if status != 200:
            fail(f"安装示例工具包失败：{body}")
        listing = json.loads(request("GET", base + "/api/tools")[1])
        mine = next((t for t in listing["tools"] if t["name"] == "dice_roll"), None)
        if not mine or mine["builtin"] or not mine["configurable"] or not mine["ready"]:
            fail(f"装上的工具没有出现在下拉框：{mine}")
        if "dice_roll" not in [item["function"]["name"] for item in openai_tools()]:
            fail("装上的工具没有提供给模型")
        secret = "dice-secret-WXYZ4321"
        status, body = request("PUT", base + "/api/tools/dice_roll/config", {"values": {"default_sides": 1000, "api_key": secret}})
        if status != 200 or secret in body:
            fail(f"保存工具自己的配置失败：{status} {body}")
        status, body = request("PUT", base + "/api/tools/dice_roll/config", {"values": {"default_sides": 1}})
        if status != 400:
            fail("工具配置没有按清单的范围校验")
        result = execute_tool("dice_roll", {"times": 3})
        if not result.get("ok") or result.get("sides") != 1000 or len(result.get("rolls") or []) != 3 or result.get("has_key") is not True:
            fail(f"装上的工具没有用到它的配置：{result}")
        if execute_tool("dice_roll", {"sides": 1}).get("error", {}).get("code") != "invalid_parameter":
            fail("装上的工具没有按清单校验参数")
        status, raw = request_bytes("GET", base + "/api/toolpacks/dice_roll/export")
        if status != 200:
            fail("导出工具包失败")
        archive = zipfile.ZipFile(io.BytesIO(raw))
        listed = sorted(archive.namelist())
        if listed != ["dice_roll/tool.json", "dice_roll/tool.py"]:
            fail(f"导出的包内容不对：{listed}")
        if any(secret.encode() in archive.read(n) for n in listed):
            fail("导出的包带走了密钥")
        status, _raw = request_bytes("GET", base + "/api/toolpacks/generate_image/export")
        if status != 400:
            fail("内置工具也被导出了")
        zipped = os.path.join(other, "dice_roll.zip")
        with open(zipped, "wb") as handle:
            handle.write(raw)
        # 拷到另一份目录：从 zip 装上并调用；配置不随包走，回到清单默认值。
        os.environ["MAS_TOOLPACKS_DIR"] = os.path.join(other, "installed")
        packs = _packs_module()
        saved_source = packs.CONFIG_SOURCE
        packs.CONFIG_SOURCE = None
        try:
            packs.install(zipped, [])
            result = execute_tool("dice_roll", {})
            if not result.get("ok") or result.get("sides") != 6 or result.get("has_key"):
                fail(f"另一份目录里装上的包不能用，或带走了配置：{result}")
        finally:
            packs.CONFIG_SOURCE = saved_source
        os.environ["MAS_TOOLPACKS_DIR"] = folder
        status, body = request("POST", base + "/api/toolpacks/generate_image/uninstall")
        if status != 400 or json.loads(body).get("code") != "builtin_tool":
            fail(f"内置工具被卸载了：{body}")
        status, body = request("POST", base + "/api/toolpacks/dice_roll/uninstall")
        if status != 200 or os.path.isdir(os.path.join(folder, "dice_roll")):
            fail(f"卸载失败：{body}")
        names = [tool["name"] for tool in json.loads(request("GET", base + "/api/tools")[1])["tools"]]
        if "dice_roll" in names:
            fail("卸载后仍在下拉框里")
    finally:
        if saved is None:
            os.environ.pop("MAS_TOOLPACKS_DIR", None)
        else:
            os.environ["MAS_TOOLPACKS_DIR"] = saved
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


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



def _character_units() -> None:
    """#53：清单、默认值、预设拼接、自定义覆盖、配置覆盖预设、安全规则。"""
    import character
    sys.path.insert(0, os.path.join(ROOT, "poc", "tools"))
    import packs

    with open(os.path.join(ROOT, "toolpacks", "builtin", "character_image", "tool.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    packs.validate_manifest(manifest, builtin=True)
    props = manifest["parameters"]["properties"]
    if set(character.FIELDS) - set(props):
        fail("character_image 清单缺字段")
    base = character.compose({}, {})
    if base["used_defaults"] != list(character.FIELDS) or base["minor"]:
        fail(f"默认值不对：{base['used_defaults']}")
    for part in ("anime style", "woman", "young adult", "East Asian features", "long straight hair", "black hair", "brown eyes", "safe for work"):
        if part not in base["prompt"]:
            fail(f"默认提示词缺 {part}：{base['prompt']}")
    if "inspired by" in base["prompt"]:
        fail(f"空参考不应进提示词：{base['prompt']}")
    if base["fields"]["reference"]["key"] != "none" or base["fields"]["art_style"]["key"] != "anime":
        fail(f"默认字段不对：{base['fields']}")
    by_label = character.compose({"hair_color": "红发", "outfit": "骑士铠甲", "gender": "male"}, {})
    if "red hair" not in by_label["prompt"] or "knight" not in by_label["prompt"] or " man," not in by_label["prompt"]:
        fail(f"预设（键或中文标签）没拼进去：{by_label['prompt']}")
    custom = character.compose({"hair_color": "薄荷绿挑染", "background": "雨夜的霓虹小巷"}, {})
    if "薄荷绿挑染" not in custom["prompt"] or "雨夜的霓虹小巷" not in custom["prompt"]:
        fail("自定义文字没有覆盖预设")
    if not custom["fields"]["hair_color"]["custom"] or custom["fields"]["gender"]["custom"]:
        fail("自定义字段没标出来")
    styled = character.compose({"ethnicity": "european", "art_style": "watercolor"}, {})
    if "European features" not in styled["prompt"] or "watercolor painting" not in styled["prompt"]:
        fail(f"人种/画面风格没拼进去：{styled['prompt']}")
    if not styled["prompt"].startswith("watercolor painting"):
        fail(f"画面风格应在提示词最前：{styled['prompt']}")
    refed = character.compose({"reference": "fantasy knight heroine"}, {})
    if 'inspired by "fantasy knight heroine"' not in refed["prompt"] or "original character" not in refed["prompt"]:
        fail(f"自定义参考没包灵感/原创：{refed['prompt']}")
    none_ref = character.compose({"reference": "none"}, {})
    if "inspired by" in none_ref["prompt"] or none_ref["fields"]["reference"]["label"] != "无":
        fail(f"reference=none 应跳过提示词：{none_ref['prompt']}")
    config = {"default_hair_color": "silver", "presets_json": json.dumps({"hair_color": [
        {"key": "silver", "label": "银发", "prompt": "glossy platinum silver hair"},
        {"key": "mint", "label": "薄荷绿", "prompt": "mint green hair"}]}, ensure_ascii=False)}
    changed = character.compose({}, config)
    if "glossy platinum silver hair" not in changed["prompt"]:
        fail("配置里的预设没覆盖同名预设或默认值")
    labels = [o["key"] for f in character.catalog(config)["fields"] if f["name"] == "hair_color" for o in f["options"]]
    if "mint" not in labels:
        fail("配置里新增的预设没出现在表单里")
    if character.check_config({"presets_json": "{bad"}) == {}:
        fail("坏的 presets_json 没被拦下")
    if character.check_config({"presets_json": json.dumps({"outfit": [{"key": "x", "label": "x", "prompt": "nude"}]})}) == {}:
        fail("露骨预设没被配置检查拦下")
    for fields, code in (
        ({"age": "child", "outfit": "swimsuit"}, "minor_unsafe"),
        ({"age": "teen", "pose": "lying"}, "minor_unsafe"),
        ({"age": "12岁", "pose": "躺在床上"}, "minor_unsafe"),
        ({"outfit": "nude"}, "unsafe_content"),
        ({"reference": "sexy nude warrior"}, "unsafe_content"),
    ):
        try:
            character.compose(fields, {})
        except character.CharacterError as exc:
            if exc.code != code:
                fail(f"{fields} 应是 {code}，得到 {exc.code}")
        else:
            fail(f"{fields} 没被拦下")
    kid = character.compose({"age": "child", "outfit": "school_uniform"}, {})
    if not kid["minor"] or "non-sexualized" not in kid["prompt"] or " woman" in kid["prompt"]:
        fail(f"未成年角色没加保护性描述：{kid['prompt']}")
    adult_swim = character.compose({"outfit": "swimsuit"}, {})
    if "tasteful" not in adult_swim["prompt"]:
        fail("成年泳装没有加得体描述")
    for name, field in character.base_presets().items():
        for option in field["options"]:
            if option.get("minor") and name != "age":
                fail("只有年龄预设能标未成年")
    print("character units ok")


def _character_form(base: str, session_id: str, payload: dict, timeout: float = 15):
    status, raw, events = read_sse(site_port(base), f"/api/sessions/{session_id}/character", payload, timeout=timeout)
    return status, raw, events


def _character_cases(base: str, provider, box: ComfyState, directory: str) -> None:
    import chat_run

    _character_units()
    box.set_mode("ok")
    status, body = request("GET", base + "/api/character")
    catalog = json.loads(body)
    if status != 200 or catalog["defaults"]["gender"] != "female" or catalog["defaults"].get("ethnicity") != "east_asian" or catalog["defaults"].get("art_style") != "anime" or catalog["defaults"].get("reference") != "none" or len(catalog["fields"]) != 12:
        fail(f"/api/character 不对：{status}")
    status, body = request("POST", base + "/api/character/preview", {"fields": {"hair_color": "红发"}})
    if status != 200 or "red hair" not in json.loads(body)["prompt"]:
        fail("预览没拼出提示词")
    status, body = request("POST", base + "/api/character/preview", {"fields": {"age": "child", "outfit": "swimsuit"}})
    if status != 400 or json.loads(body).get("code") != "minor_unsafe":
        fail(f"预览没拦未成年+泳装：{status} {body}")

    # 表单：N 张就提交 N 次，不调对话模型。
    session_id = _fresh_session(base)
    sent, before = len(provider.requests), len(box.prompts)
    status, raw, events = _character_form(base, session_id, {"fields": {"hair_color": "红发"}, "count": 2, "seed": 7})
    if status != 200 or events[-1].get("type") != "message.completed":
        fail(f"表单出图没完成：{status} {[e.get('type') for e in events]}")
    if len(box.prompts) != before + 2 or len([e for e in events if e.get("type") == "artifact"]) != 2:
        fail("表单两张图没有恰好提交两次")
    if len(provider.requests) != sent:
        fail("表单出图调用了对话模型")
    if "red hair" not in json.dumps(box.prompts[-1]):
        fail("表单提示词没送到 ComfyUI")
    stored = json.loads(request("GET", base + f"/api/sessions/{session_id}")[1])
    if "定制角色出图（2 张" not in stored["messages"][0]["text"]:
        fail("表单没写进聊天记录")
    status, raw, events = _character_form(base, session_id, {"fields": {"age": "child", "pose": "lying"}})
    if status != 400:
        fail("表单没拦未成年+不当姿势")
    for bad in ({"count": 0}, {"count": 21}, {"privacy": "x"}, {"backend": "x"}):
        if _character_form(base, session_id, bad)[0] != 400:
            fail(f"表单没拦坏参数 {bad}")

    # 本地提示词：自定义文字不进聊天记录，第三方默认拒绝。
    session_id = _fresh_session(base)
    secret = "只留本机的角色描述丙"
    before = len(box.prompts)
    status, raw, events = _character_form(base, session_id, {"fields": {"outfit": secret}, "privacy": "local"})
    if status != 200 or events[-1].get("type") != "message.completed" or len(box.prompts) != before + 1:
        fail("本地提示词模式没出图")
    visible = request("GET", base + f"/api/sessions/{session_id}")[1]
    if secret in visible or secret in raw:
        fail("本地提示词模式把自定义文字写进了会话或事件")
    if secret not in json.dumps(box.prompts[-1], ensure_ascii=False):
        fail("本地提示词没送到本机 ComfyUI")
    saved = {name: os.environ.get(name) for name in ("MAS_IMAGE_API_BASE", "MAS_IMAGE_API_KEY")}
    os.environ["MAS_IMAGE_API_BASE"] = "http://127.0.0.1:9/v1"
    os.environ["MAS_IMAGE_API_KEY"] = "sk-fake-character-53"
    try:
        status, raw, events = _character_form(base, session_id, {"fields": {"outfit": secret}, "privacy": "local", "backend": "remote"})
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    blob = json.dumps(events, ensure_ascii=False)
    if events[-1].get("type") == "message.completed" or "local_prompt_remote_refused" not in blob:
        fail(f"本地提示词默认没拒绝第三方：{blob[-300:]}")

    # 对话：模型调 character_image，没说的字段用默认值，再 generate_image。
    if not chat_run._open_prompt_request("画一个红发女骑士角色"):
        fail("“角色”没打开对话出图")
    provider.mode = "character"
    session_id = _fresh_session(base)
    before, sent = len(box.prompts), len(provider.requests)
    events = _speak(base, session_id, "画一个红发女骑士角色", timeout=15)
    if _assistant_text(events) != "好了" or len(box.prompts) != before + 1:
        fail(f"对话角色出图不对：{_assistant_text(events)} {len(box.prompts) - before}")
    submitted = json.dumps(box.prompts[-1])
    for part in ("red hair", "knight", "woman", "long straight hair", "brown eyes"):
        if part not in submitted:
            fail(f"对话角色提示词缺 {part}")
    result = next((e for e in events if e.get("type") == "tool_result" and e.get("tool") == "character_image"), None)
    blob = json.dumps(events, ensure_ascii=False)
    if "used_defaults" not in json.dumps(provider.requests[sent + 1]["body"], ensure_ascii=False) and result is None:
        fail(f"character_image 结果没回到模型：{blob[:300]}")
    provider.mode = "ok"
    print("character ok")


if __name__ == "__main__":
    main()

