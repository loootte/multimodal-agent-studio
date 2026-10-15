"""OpenAI Chat Completions 兼容适配器。

默认基址 https://api.x.ai/v1 ，默认模型 grok-4.7。
请求是 POST {base_url}/chat/completions ，stream 为 true。
文档：https://docs.x.ai/developers/model-capabilities/text/streaming

适配器不保存密钥，也不把密钥写进日志。调用方把当次密钥传进来。
reasoning_content 不进入回答正文。
传入 tools 时，把供应商的工具调用收成名字加 JSON 对象。解析失败是 invalid_tool_call。
local_bodies 里长度不少于 8 的正文一旦出现在请求里，连接还没打开就失败。
调用方把已入库的文字、文件的文本和文件字节的 base64 放进这个列表。
工具契约本身不参与这道检查，避免说明文字被当成正文。
图像部件按工件标识解析，写成 data URL。音频和视频由能力声明拒绝。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import time
import urllib.parse

from packages.contracts.events import HUMAN, RETRYABLE
from packages.contracts.request import AUDIO, FILE, IMAGE, TEXT, VIDEO, capabilities, message_parts, normalize

DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-4.7"
_MAX_LINE = 2_000_000
_MAX_FILE = 20 * 1024 * 1024
_MAX_IMAGES = 16
_CONTEXT_LIMIT = 128000


class GatewayError(Exception):
    def __init__(self, code: str, message: str, retryable: bool | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if retryable is None:
            self.retryable = bool(RETRYABLE.get(code, False))
        else:
            self.retryable = bool(retryable)


class OpenAIAdapter:
    name = "openai"

    def capabilities(self, model: str) -> dict:
        return capabilities(
            model=model,
            input_modalities=(TEXT, IMAGE),
            output_modalities=(TEXT,),
            streaming=True,
            tools=True,
            context_limit=_CONTEXT_LIMIT,
            context_unit="tokens",
            max_file_bytes=_MAX_FILE,
            max_images=_MAX_IMAGES,
        )

    def stream(
        self,
        request,
        *,
        api_key: str,
        timeout_sec: float,
        cancel,
        artifacts=None,
        local_bodies=None,
        base_url=None,
    ):
        if not api_key:
            raise GatewayError("missing_key", HUMAN["missing_key"])
        req = normalize(request)
        caps = self.capabilities(req["model"])
        reject_unsupported(req, caps)
        payload_messages = translate_messages(req, artifacts or {}, caps)
        payload = {"model": req["model"], "stream": True, "messages": payload_messages}
        if req.get("tools"):
            payload["tools"] = req["tools"]
            payload["tool_choice"] = "auto"
        if _is_number(req.get("temperature")):
            payload["temperature"] = req["temperature"]
        if _is_int(req.get("max_output")):
            payload["max_tokens"] = req["max_output"]
        if isinstance(req.get("stop"), list):
            payload["stop"] = req["stop"]
        _reject_local_content(payload, local_bodies or [])
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        deadline = time.monotonic() + timeout_sec
        upstream = base_url or DEFAULT_BASE_URL
        conn = _open(upstream, min(10.0, timeout_sec))
        yielded = False
        calls = _ToolCalls()
        try:
            if cancel.is_set():
                raise GatewayError("cancelled", HUMAN["cancelled"])
            parts = urllib.parse.urlsplit(upstream)
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
                yield {"type": "message.delta", "text": piece}
            call = calls.finish()
            if call is not None:
                yielded = True
                yield {
                    "type": "tool.requested",
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "call_id": call["call_id"],
                }
            if cancel.is_set():
                raise GatewayError("cancelled", HUMAN["cancelled"])
            if not yielded:
                raise GatewayError("provider_error", "模型没有返回文字。")
        except GatewayError:
            raise
        except (TimeoutError, socket.timeout):
            raise GatewayError("timeout", HUMAN["timeout"])
        except (OSError, http.client.HTTPException, AttributeError):
            if cancel.is_set():
                raise GatewayError("cancelled", HUMAN["cancelled"])
            if time.monotonic() >= deadline:
                raise GatewayError("timeout", HUMAN["timeout"])
            raise GatewayError("provider_error", "模型连接中断了。")
        finally:
            _close_conn(conn)


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
    request = {
        "model": model,
        "messages": [_legacy_message(item) for item in messages],
        "tools": tools,
    }
    for event in OpenAIAdapter().stream(
        request,
        api_key=api_key,
        timeout_sec=timeout_sec,
        cancel=cancel,
        local_bodies=local_bodies,
        base_url=base_url,
    ):
        kind = event.get("type")
        if kind == "message.delta":
            yield event["text"]
        elif kind == "tool.requested":
            yield {
                "type": "tool_call",
                "name": event["name"],
                "arguments": event["arguments"],
                "call_id": event.get("call_id"),
            }


def _legacy_message(message: dict) -> dict:
    if not isinstance(message, dict):
        raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
    if isinstance(message.get("parts"), list):
        return message
    role = message.get("role")
    content = message.get("content")
    if role not in ("system", "user", "assistant") or not isinstance(content, str):
        raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
    return {"role": role, "parts": [{"type": TEXT, "text": content}]}


def reject_unsupported(request: dict, caps: dict) -> None:
    accepted = set(caps.get("input_modalities") or ())
    for message in request.get("messages") or []:
        role = message.get("role") if isinstance(message, dict) else None
        if role not in ("system", "user", "assistant"):
            raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
        parts = message_parts(message)
        if not parts:
            raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
        for part in parts:
            kind = part.get("type")
            if kind not in accepted:
                raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
            if kind == TEXT and not isinstance(part.get("text"), str):
                raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
            if kind in (IMAGE, AUDIO, VIDEO, FILE) and not _artifact_id(part.get("artifact_id")):
                raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])


def translate_messages(request: dict, artifacts: dict, caps: dict) -> list[dict]:
    items = []
    system = request.get("system")
    if isinstance(system, str) and system:
        items.append({"role": "system", "content": system})
    image_count = 0
    for message in request.get("messages") or []:
        role = message.get("role")
        parts = message_parts(message)
        image_count += sum(1 for part in parts if part.get("type") == IMAGE)
        limit = caps.get("max_images")
        if limit is not None and image_count > limit:
            raise GatewayError("payload_too_large", HUMAN["payload_too_large"])
        items.append({"role": role, "content": _openai_content(parts, artifacts, caps)})
    return items


def _openai_content(parts: list[dict], artifacts: dict, caps: dict):
    if all(part.get("type") == TEXT for part in parts):
        return "".join(part.get("text") or "" for part in parts)
    content = []
    for part in parts:
        kind = part.get("type")
        if kind == TEXT:
            content.append({"type": "text", "text": part.get("text") or ""})
            continue
        if kind == IMAGE:
            content.append(_image_url(part, artifacts, caps))
            continue
        raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
    return content


def _image_url(part: dict, artifacts: dict, caps: dict) -> dict:
    artifact_id = part.get("artifact_id")
    media_type = part.get("media_type") if isinstance(part.get("media_type"), str) else "image/png"
    if not media_type.startswith("image/"):
        raise GatewayError("modality_unsupported", HUMAN["modality_unsupported"])
    data = _artifact_bytes(artifacts, artifact_id)
    if data is None:
        raise GatewayError("provider_error", "找不到这个工件。", retryable=False)
    limit = caps.get("max_file_bytes")
    if limit is not None and len(data) > limit:
        raise GatewayError("payload_too_large", HUMAN["payload_too_large"])
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
    }


def _artifact_bytes(artifacts: dict, artifact_id: str):
    item = artifacts.get(artifact_id)
    if item is None:
        return None
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    if isinstance(item, dict):
        data = item.get("data")
        if data is None:
            data = item.get("bytes")
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        path = item.get("path")
        if isinstance(path, str) and os.path.isfile(path):
            with open(path, "rb") as handle:
                return handle.read()
        return None
    if isinstance(item, str) and os.path.isfile(item):
        with open(item, "rb") as handle:
            return handle.read()
    return None


def _artifact_id(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _close_conn(conn) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _clear_read_timeout(response) -> None:
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
        return GatewayError("provider_error", "API key 被拒绝。请检查密钥是否正确。", retryable=False)
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
