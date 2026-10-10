"""OpenAI Chat Completions 兼容的文字流。

默认基址 https://api.x.ai/v1 ，默认模型 grok-4.7。
请求是 POST {base_url}/chat/completions ，stream 为 true。
文档：https://docs.x.ai/developers/model-capabilities/text/streaming

适配器不保存密钥，也不把密钥写进日志。调用方把当次密钥传进来。
reasoning_content 不进入回答正文。
传入 tools 时，把供应商的工具调用收成名字加 JSON 对象。解析失败是 invalid_tool_call。
local_bodies 里长度不少于 8 的正文一旦出现在请求里，连接还没打开就失败。
工具契约本身不参与这道检查，避免说明文字被当成正文。
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.parse

from packages.contracts.events import HUMAN

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.7"
_MAX_LINE = 2_000_000


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def stream_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    timeout_sec: float,
    cancel,
    tools=None,
    local_bodies=None,
):
    """逐段产出回答文字。有工具调用时，最后再产出一个 tool_call。

    失败时抛 GatewayError。cancel 置位后，读循环会自己停。
    只有工具、没有文字时，不报「模型没有返回文字」。
    本地正文出现在载荷里时，不打开连接。
    """
    if not api_key:
        raise GatewayError("missing_key", HUMAN["missing_key"])
    payload_messages = _messages(messages)
    payload = {"model": model, "stream": True, "messages": payload_messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    _reject_local_content(payload, local_bodies or [])
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    deadline = time.monotonic() + timeout_sec
    conn = _open(base_url, min(10.0, timeout_sec))
    yielded = False
    calls = _ToolCalls()
    try:
        if cancel.is_set():
            raise GatewayError("cancelled", HUMAN["cancelled"])
        parts = urllib.parse.urlsplit(base_url)
        path = parts.path.rstrip("/") + "/chat/completions"
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Connection": "close",
                "User-Agent": "multimodal-agent-studio/web",
            },
        )
        response = conn.getresponse()
        if response.status != 200:
            raw = response.read(65536)
            raise _http_error(response.status, raw, api_key)
        for piece in _deltas(response, conn, cancel, deadline, api_key, calls):
            yielded = True
            yield piece
        call = calls.finish()
        if call is not None:
            yielded = True
            yield call
        if cancel.is_set():
            raise GatewayError("cancelled", HUMAN["cancelled"])
        if not yielded:
            raise GatewayError("provider_error", "模型没有返回文字。")
    except GatewayError:
        raise
    except (TimeoutError, socket.timeout):
        raise GatewayError("timeout", HUMAN["timeout"])
    except (OSError, http.client.HTTPException, AttributeError):
        # 另一个线程 shutdown 之后，readline 可能变成 AttributeError，而不是 OSError。
        if cancel.is_set():
            raise GatewayError("cancelled", HUMAN["cancelled"])
        if time.monotonic() >= deadline:
            raise GatewayError("timeout", HUMAN["timeout"])
        raise GatewayError("provider_error", "模型连接中断了。")
    finally:
        _close_conn(conn)


def _close_conn(conn) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _clear_read_timeout(response) -> None:
    # 一次读超时会把 SocketIO 标死，不清掉就读不了后面的增量。
    raw = getattr(getattr(response, "fp", None), "raw", None)
    if raw is not None:
        raw._timeout_occurred = False


def _open(base_url: str, timeout: float) -> http.client.HTTPConnection:
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname
    if not host:
        raise GatewayError("provider_error", "模型基址没有主机名。")
    if parts.scheme == "https":
        return http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout)
    if parts.scheme == "http":
        return http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
    raise GatewayError("provider_error", "模型基址只接受 http 或 https。")


def _reject_local_content(payload: dict, bodies: list[str]) -> None:
    scanned = {key: value for key, value in payload.items() if key != "tools"}
    for secret in bodies:
        if isinstance(secret, str) and len(secret) >= 8 and _value_has(scanned, secret):
            raise GatewayError("local_content", HUMAN["local_content"])


def _value_has(value, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_value_has(key, secret) or _value_has(item, secret) for key, item in value.items())
    if isinstance(value, list):
        return any(_value_has(item, secret) for item in value)
    return False


def _messages(messages: list[dict]) -> list[dict]:
    prepared = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role not in ("system", "user", "assistant") or not isinstance(content, str):
            raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
        prepared.append({"role": role, "content": content})
    return prepared


class _ToolCalls:
    def __init__(self):
        self._slots: dict[int, dict] = {}

    def add(self, calls) -> None:
        if not isinstance(calls, list):
            return
        for call in calls:
            if not isinstance(call, dict):
                continue
            index = call.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index > 7:
                raise GatewayError("invalid_tool_call", HUMAN["invalid_tool_call"])
            slot = self._slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if isinstance(call.get("id"), str):
                slot["id"] = call["id"]
            function = call.get("function") or {}
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str):
                slot["name"] += name
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                slot["arguments"] += arguments
            if len(slot["name"]) > 128 or len(slot["arguments"]) > 100_000:
                raise GatewayError("invalid_tool_call", HUMAN["invalid_tool_call"])

    def finish(self):
        if not self._slots:
            return None
        # 一条用户消息只执行第一个工具调用。
        slot = self._slots[sorted(self._slots)[0]]
        name = slot["name"].strip()
        raw = slot["arguments"].strip()
        if not name or not raw:
            raise GatewayError("invalid_tool_call", HUMAN["invalid_tool_call"])
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayError("invalid_tool_call", HUMAN["invalid_tool_call"]) from exc
        if not isinstance(arguments, dict):
            raise GatewayError("invalid_tool_call", HUMAN["invalid_tool_call"])
        return {
            "type": "tool_call",
            "name": name,
            "arguments": arguments,
            "call_id": slot["id"],
        }


def _deltas(response, conn, cancel, deadline: float, api_key: str, calls: _ToolCalls):
    if conn.sock is not None:
        conn.sock.settimeout(0.2)
    while True:
        if cancel.is_set():
            raise GatewayError("cancelled", HUMAN["cancelled"])
        if time.monotonic() >= deadline:
            raise GatewayError("timeout", HUMAN["timeout"])
        try:
            line = response.readline()
        except (TimeoutError, socket.timeout):
            _clear_read_timeout(response)
            continue
        if line == b"":
            return
        if len(line) > _MAX_LINE:
            raise GatewayError("payload_too_large", "模型返回的一段内容太长了。")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GatewayError("provider_error", "模型流无法解析。") from exc
        data = text.strip()
        if not data or data.startswith(":"):
            continue
        if not data.startswith("data:"):
            continue
        payload = data[5:].strip()
        if payload == "[DONE]":
            return
        yield from _content(payload, api_key, calls)


def _content(payload: str, api_key: str, calls: _ToolCalls):
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GatewayError("provider_error", "模型流无法解析。") from exc
    if not isinstance(event, dict):
        return
    error = event.get("error")
    if error:
        message = _error_message(error)
        raise GatewayError("provider_error", _redact(message or HUMAN["provider_error"], api_key))
    choices = event.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return
    choice = choices[0]
    delta = choice.get("delta") or {}
    if not isinstance(delta, dict):
        return
    calls.add(delta.get("tool_calls"))
    content = delta.get("content")
    if isinstance(content, str):
        if content:
            yield content
        return
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str) and part:
                yield part
            elif isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                yield part["text"]


def _http_error(status: int, raw: bytes, api_key: str) -> GatewayError:
    if status == 429:
        return GatewayError("rate_limited", HUMAN["rate_limited"])
    if status == 408:
        return GatewayError("timeout", HUMAN["timeout"])
    if status == 413:
        return GatewayError("payload_too_large", HUMAN["payload_too_large"])
    if status in (401, 403):
        return GatewayError("provider_error", "API key 被拒绝。请检查密钥是否正确。")
    detail = _redact(_error_message(_load_error(raw)), api_key)
    if detail and len(detail) <= 180 and api_key not in detail:
        return GatewayError("provider_error", f"模型服务返回错误：{detail}")
    return GatewayError("provider_error", HUMAN["provider_error"])


def _load_error(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeError, json.JSONDecodeError):
        return None


def _error_message(error) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message.strip()
        nested = error.get("error")
        if nested is not None and nested is not error:
            return _error_message(nested)
    if isinstance(error, str):
        return error.strip()
    return ""


def _redact(text: str, api_key: str) -> str:
    if api_key and text:
        text = text.replace(api_key, "[redacted]")
    return text
