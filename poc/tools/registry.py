"""扫描 poc/tools/internal 和 poc/tools/external。符合规范的文件自动可用。"""

from __future__ import annotations

import importlib.util
import os
import sys

from tools.api import Tool, ToolSpecError, tool_error
from tools.schema import check as check_schema

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKED_EXTERNAL = {"prompt", "text", "body", "content", "bytes", "path", "filename"}
_modules = {}


def tool_names(audience: str = "internal") -> tuple[str, ...]:
    return tuple(item.name for item in audience_tools(audience))


def tool_specs(audience: str = "internal") -> list[dict]:
    return [_bare(item) for item in audience_tools(audience)]


def external_contracts() -> list[dict]:
    return [_openai(item) for item in audience_tools("external")]


def module(audience: str, stem: str):
    audience_tools(audience)
    return sys.modules[f"tools.{audience}.{stem}"]


def validate_tool(name: str, arguments: dict) -> dict | None:
    """本机工具提交前的校验。通过时返回 None。"""
    tool = _find("internal", name)
    if tool is None:
        return _unknown("internal", name)
    if not isinstance(arguments, dict):
        return tool_error(name, "invalid_parameter", "工具参数必须是对象。")
    return tool.validate(arguments)


def call_tool(name: str, arguments: dict) -> dict:
    """执行已注册的本机工具。"""
    from tools._support import ensure_comfy_path, public_comfy_message

    ensure_comfy_path()
    import artifacts
    import comfy_poc
    import template_fill

    tool = _find("internal", name)
    if tool is None:
        return _unknown("internal", name)
    if not isinstance(arguments, dict):
        return tool_error(name, "invalid_parameter", "工具参数必须是对象。")
    try:
        with comfy_poc.talk_off():
            return tool.call(arguments)
    except template_fill.TemplateError as exc:
        return tool_error(name, "template_error", exc.message)
    except artifacts.ArtifactError as exc:
        return tool_error(name, "artifact_error", exc.message)
    except comfy_poc.ComfyFailure as exc:
        return tool_error(name, "comfyui_error", public_comfy_message(exc))


def validate_external(name: str, arguments) -> dict:
    """外部调用只对照参数模式。通过时标明尚未执行。"""
    if not isinstance(arguments, dict):
        return tool_error(name, "invalid_parameter", "工具参数必须是对象。", executed=False)
    tool = _find("external", name)
    if tool is None:
        return _unknown("external", name, executed=False)
    problem = check_schema(name, tool.parameters, arguments)
    if problem:
        return problem
    return {"ok": True, "tool": name, "executed": False}


def load_modules(folder: str, prefix: str) -> list:
    """装载目录里每个非下划线开头的模块。调用方再取 tools()。"""
    modules = []
    for path in sorted(entry for entry in os.listdir(folder) if entry.endswith(".py")):
        stem = path[:-3]
        if stem.startswith("_") or stem == "__init__":
            continue
        full_path = os.path.join(folder, path)
        full_name = f"{prefix}.{stem}"
        existing = sys.modules.get(full_name)
        if existing is not None and os.path.abspath(getattr(existing, "__file__", "")) == os.path.abspath(full_path):
            modules.append(existing)
            continue
        spec = importlib.util.spec_from_file_location(full_name, full_path)
        if spec is None or spec.loader is None:
            raise ToolSpecError(f"无法装载 {path}。")
        loaded = importlib.util.module_from_spec(spec)
        loaded.__package__ = prefix
        sys.modules[full_name] = loaded
        try:
            spec.loader.exec_module(loaded)
        except Exception:
            sys.modules.pop(full_name, None)
            raise
        if not callable(getattr(loaded, "tools", None)):
            sys.modules.pop(full_name, None)
            raise ToolSpecError(f"{path} 没有 tools()。")
        modules.append(loaded)
    return modules


def collect(modules: list, audience: str) -> list[Tool]:
    found = []
    for loaded in modules:
        exported = loaded.tools()
        if not isinstance(exported, list):
            raise ToolSpecError(f"{os.path.basename(loaded.__file__)} 的 tools() 必须返回列表。")
        for item in exported:
            _require_tool(item, os.path.basename(loaded.__file__), audience)
            found.append(item)
    found.sort(key=lambda item: (item.order, item.name))
    seen = {}
    for item in found:
        if item.name in seen:
            raise ToolSpecError(f"工具 {item.name} 在 {audience} 里重复了。")
        seen[item.name] = item
    return found


def audience_tools(audience: str) -> list[Tool]:
    if audience not in ("internal", "external"):
        raise ToolSpecError(f"没有这个受众：{audience}。")
    if audience not in _modules:
        from tools._support import ensure_comfy_path

        ensure_comfy_path()
        folder = os.path.join(HERE, audience)
        _modules[audience] = load_modules(folder, f"tools.{audience}")
    return collect(_modules[audience], audience)


def _find(audience: str, name: str) -> Tool | None:
    for item in audience_tools(audience):
        if item.name == name:
            return item
    return None


def _unknown(audience: str, name: str, *, executed=None) -> dict:
    names = " 或 ".join(item.name for item in audience_tools(audience))
    message = f"只能调用 {names}。" if names else "没有已注册的工具。"
    return tool_error(name, "unknown_tool", message, executed=executed)


def _require_tool(item, filename: str, audience: str) -> None:
    if not isinstance(item, Tool):
        raise ToolSpecError(f"{filename} 的 tools() 必须返回 Tool。")
    if not isinstance(item.name, str) or not item.name:
        raise ToolSpecError(f"{filename} 的工具缺少名字。")
    if not isinstance(item.description, str):
        raise ToolSpecError(f"{item.name} 缺少说明。")
    parameters = item.parameters
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ToolSpecError(f"{item.name} 的参数必须是 object。")
    if parameters.get("additionalProperties") is not False:
        raise ToolSpecError(f"{item.name} 必须拒绝未声明的参数。")
    if not isinstance(item.order, int) or isinstance(item.order, bool):
        raise ToolSpecError(f"{item.name} 的 order 必须是整数。")
    if audience == "internal":
        if not callable(item.validate) or not callable(item.call):
            raise ToolSpecError(f"本机工具 {item.name} 必须提供 validate 和 call。")
        return
    if item.validate is not None or item.call is not None:
        raise ToolSpecError(f"外部工具 {item.name} 只声明参数，不能提供 validate 或 call。")
    leaked = BLOCKED_EXTERNAL.intersection(parameters.get("properties") or {})
    if leaked:
        raise ToolSpecError(f"外部工具 {item.name} 的参数里有 {'、'.join(sorted(leaked))}。")


def _bare(item: Tool) -> dict:
    return {"name": item.name, "description": item.description, "parameters": item.parameters}


def _openai(item: Tool) -> dict:
    return {
        "type": "function",
        "function": {
            "name": item.name,
            "description": item.description,
            "parameters": item.parameters,
        },
    }
