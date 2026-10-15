"""统一请求和模型能力。部件用工件标识，不带文件字节。"""

from __future__ import annotations

TEXT = "text"
IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"
FILE = "file"

INPUT_MODALITIES = (TEXT, IMAGE, AUDIO, VIDEO, FILE)
OUTPUT_MODALITIES = (TEXT,)
ROLES = ("system", "user", "assistant")
KNOWN_FIELDS = ("model", "system", "messages", "tools", "temperature", "max_output", "stop", "ignored_params")


def capabilities(
    *,
    model: str,
    input_modalities,
    output_modalities=OUTPUT_MODALITIES,
    streaming: bool = True,
    tools: bool = True,
    context_limit: int,
    context_unit: str = "tokens",
    max_file_bytes=None,
    max_duration_sec=None,
    max_images=None,
) -> dict:
    return {
        "model": model,
        "input_modalities": tuple(input_modalities),
        "output_modalities": tuple(output_modalities),
        "streaming": bool(streaming),
        "tools": bool(tools),
        "context_limit": int(context_limit),
        "context_unit": context_unit,
        "max_file_bytes": max_file_bytes,
        "max_duration_sec": max_duration_sec,
        "max_images": max_images,
    }


def normalize(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("请求需要是对象。")
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("缺少模型标识。")
    messages = raw.get("messages")
    if not isinstance(messages, list):
        raise ValueError("消息需要是列表。")
    extra = raw.get("ignored_params")
    if isinstance(extra, list):
        ignored = [str(item) for item in extra]
    else:
        ignored = sorted(str(key) for key in raw if key not in KNOWN_FIELDS)
    system = raw.get("system")
    return {
        "model": model.strip(),
        "system": system if isinstance(system, str) else None,
        "messages": messages,
        "tools": raw.get("tools"),
        "temperature": raw.get("temperature"),
        "max_output": raw.get("max_output"),
        "stop": raw.get("stop"),
        "ignored_params": ignored,
    }


def message_parts(message: dict) -> list[dict]:
    if not isinstance(message, dict):
        return []
    parts = message.get("parts")
    if isinstance(parts, list):
        return [part for part in parts if isinstance(part, dict)]
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": TEXT, "text": content}]
    return []
