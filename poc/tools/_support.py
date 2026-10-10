"""本机生成工具共用的校验、监听和结果形状。这个文件不参与自注册。"""

from __future__ import annotations

import json
import os
import random
import sys
from contextlib import contextmanager

POC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY_DIR = os.path.join(POC_DIR, "comfyui")
SCRIPT_DIR = COMFY_DIR


def ensure_comfy_path() -> None:
    if COMFY_DIR not in sys.path:
        sys.path.append(COMFY_DIR)


ensure_comfy_path()

import comfy_client
import comfy_poc
import template_fill
FORBIDDEN_IN_SCHEMA = (
    "LoadImage",
    "KSampler",
    "CLIPTextEncode",
    "class_type",
    "UnetLoader",
    "SaveImage",
    "SaveVideo",
    "WanImageToVideo",
)
_run_listener = None


def load_styles() -> dict[str, str]:
    path = os.path.join(SCRIPT_DIR, "styles.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not data:
        comfy_poc.fail(f"风格表不可用：{path}")
    styles = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or not value.strip():
            comfy_poc.fail(f"风格表里的 {key!r} 不是一句可用的风格句。")
        styles[key] = value
    return styles


def tool_error(tool: str, code: str, message: str, parameter: str | None = None) -> dict:
    error = {"code": code, "message": message}
    if parameter:
        error["parameter"] = parameter
    return {"ok": False, "tool": tool, "error": error}


def public_comfy_message(exc: comfy_poc.ComfyFailure) -> str:
    text = comfy_client.original_error_text(exc.payload)
    if text:
        return text
    return exc.message


def unexpected(tool: str, arguments: dict) -> dict | None:
    allowed = {
        "generate_image": {"prompt", "aspect_ratio", "seed", "style"},
        "generate_video": {"prompt", "duration_sec", "aspect_ratio", "image_ref", "fps", "motion"},
    }[tool]
    extra = sorted(set(arguments) - allowed)
    if extra:
        return tool_error(tool, "unexpected_parameter", f"不能传这些参数：{', '.join(extra)}。", extra[0])
    return None


def require_prompt(tool: str, arguments: dict) -> dict | None:
    if "prompt" not in arguments:
        return tool_error(tool, "missing_prompt", "prompt 是必填项。", "prompt")
    prompt = arguments["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        return tool_error(tool, "missing_prompt", "prompt 必须是非空字符串。", "prompt")
    return None


def parse_aspect(tool: str, arguments: dict, allowed: set[str]) -> tuple[str | None, dict | None]:
    if "aspect_ratio" not in arguments:
        return None, tool_error(tool, "invalid_aspect_ratio", "aspect_ratio 是必填项。", "aspect_ratio")
    aspect = arguments["aspect_ratio"]
    if not isinstance(aspect, str) or aspect not in allowed:
        names = "、".join(comfy_poc.ASPECTS)
        return None, tool_error(tool, "invalid_aspect_ratio", f"aspect_ratio 只能是 {names}。", "aspect_ratio")
    return aspect, None


def parse_seed(tool: str, arguments: dict) -> tuple[int | None, dict | None]:
    if "seed" not in arguments or arguments["seed"] is None:
        return random.randrange(0, 2**32), None
    seed = arguments["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        return None, tool_error(tool, "invalid_seed", "seed 必须是大于等于 0 的整数。", "seed")
    return seed, None


def parse_style(tool: str, arguments: dict, styles: dict[str, str]) -> tuple[str | None, dict | None]:
    if "style" not in arguments or arguments["style"] is None:
        return None, None
    style = arguments["style"]
    if not isinstance(style, str) or style not in styles:
        names = "、".join(sorted(styles))
        return None, tool_error(tool, "unknown_style", f"style 只能是 {names}。", "style")
    return style, None


def values_match(tool: str, workflow: dict, fields: dict, prompt: str, aspect: str, seed: int, frames: int | None = None) -> dict | None:
    if template_fill.read_back(workflow, fields, "positive_prompt") != prompt:
        return tool_error(tool, "internal_error", "填进模板的提示词和参数不一致。")
    size = fields["aspects"][aspect]
    if template_fill.read_back(workflow, fields, "width") != int(size["width"]):
        return tool_error(tool, "internal_error", "填进模板的宽度和画幅不一致。")
    if template_fill.read_back(workflow, fields, "height") != int(size["height"]):
        return tool_error(tool, "internal_error", "填进模板的高度和画幅不一致。")
    if template_fill.read_back(workflow, fields, "seed") != seed:
        return tool_error(tool, "internal_error", "填进模板的 seed 和参数不一致。")
    if frames is not None and template_fill.read_back(workflow, fields, "frames") != frames:
        return tool_error(tool, "internal_error", "填进模板的帧数和时长不一致。")
    return None


def current_listener():
    return _run_listener


@contextmanager
def listen_run(callback):
    """生成过程中把提交和进度交给聊天记录。不改变工具参数。"""
    global _run_listener
    previous = _run_listener
    _run_listener = callback
    try:
        yield
    finally:
        _run_listener = previous


def published_result(tool: str, records: list[dict], extra: dict) -> dict:
    result = {"ok": True, "tool": tool}
    result.update(records[0])
    if len(records) > 1:
        result["artifact_ids"] = [item["artifact_id"] for item in records]
    result.update(extra)
    return result
