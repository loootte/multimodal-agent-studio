"""运行时调用 poc/tools。不另建 services/tools。"""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
POC = os.path.join(ROOT, "poc")


def _ensure() -> None:
    if POC not in sys.path:
        sys.path.append(POC)


def authorize(name: str) -> dict | None:
    """12.D（#60）每次调用前的二次核对：工具要用的权限 ⊆ 清单声明 ⊆ 授权记录，第三方包的授权还要绑定包内容。

    返回 None 放行；否则是 permission_denied 的工具结果（不执行）。不认识的工具交给后面的 unknown_tool。
    网页宿主的 host 工具（_execute_web_tool）和这里的 python 工具都先过它。
    """
    _ensure()
    from tools import packs

    try:
        pack = packs.find(name)
    except packs.PackError as exc:
        return {"ok": False, "tool": name, "executed": False, "error": {"code": exc.code, "message": exc.message}}
    if pack is None:
        return None
    return packs.authorize(pack)


def execute_tool(name: str, arguments: dict) -> dict:
    _ensure()
    from tools.registry import audience_tools, call_tool, validate_external, validate_tool

    denied = authorize(name)
    if denied:
        return denied
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


def tool_mutates(name: str, arguments=None) -> bool:
    """按清单的确认策略：always 要确认，never 不要，by_args 看是否带了 confirm_args 里的参数。"""
    _ensure()
    from tools import packs

    pack = packs.find(name)
    if pack is None:
        return False
    return packs.needs_confirm(pack, arguments)


def egress_preview(name: str, arguments=None) -> dict:
    """确认卡上的出站摘要（#57）：由宿主按工具清单的 permissions 生成，不用模型的解释文字。

    这里只给通用部分（会不会联网、会不会读本地内容）；网页宿主再补上目的地主机和句柄用途。
    """
    _ensure()
    from tools import packs

    pack = packs.find(name)
    permissions = (pack.manifest.get("permissions") or {}) if pack is not None else {}
    return {
        "by": "host",
        "network": bool(permissions.get("network")),
        "reads_local": bool(permissions.get("read_local_content")),
        "sends_local_to_model": bool(permissions.get("send_local_to_model")),
        "sandboxed": bool(pack is not None and not pack.builtin),
    }


def openai_tools() -> list[dict]:
    _ensure()
    from tools.registry import external_contracts

    return external_contracts()
