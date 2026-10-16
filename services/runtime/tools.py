"""运行时调用 poc/tools。不另建 services/tools。"""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POC = os.path.join(ROOT, "poc")


def _ensure() -> None:
    if POC not in sys.path:
        sys.path.append(POC)


def execute_tool(name: str, arguments: dict) -> dict:
    _ensure()
    from tools.registry import audience_tools, call_tool, validate_external, validate_tool

    if any(item.name == name for item in audience_tools("internal")):
        problem = validate_tool(name, arguments)
        if problem:
            return problem
        return call_tool(name, arguments)
    checked = validate_external(name, arguments)
    if not checked.get("ok"):
        return checked
    return {
        "ok": False,
        "tool": name,
        "executed": False,
        "error": {"code": "unknown_tool", "message": "这个工具没有本机实现。"},
    }


def validate_arguments(name: str, arguments) -> dict | None:
    _ensure()
    from tools.api import tool_error
    from tools.registry import audience_tools
    from tools.schema import check as check_schema

    if not isinstance(arguments, dict):
        return tool_error(name, "invalid_tool_call", "工具参数必须是对象。", executed=False)
    for audience in ("external", "internal"):
        for item in audience_tools(audience):
            if item.name == name:
                return check_schema(name, item.parameters, arguments)
    return tool_error(name, "unknown_tool", "没有已注册的工具。", executed=False)


def tool_mutates(name: str) -> bool:
    _ensure()
    from tools.registry import audience_tools

    for audience in ("internal", "external"):
        for item in audience_tools(audience):
            if item.name == name and item.mutating:
                return True
    return False


def openai_tools() -> list[dict]:
    _ensure()
    from tools.registry import external_contracts

    return external_contracts()
