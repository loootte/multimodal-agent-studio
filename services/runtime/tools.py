"""运行时的授权和转发。工具引擎在 services/tools。"""

from __future__ import annotations



def _ensure() -> None:
    """引擎是 services.tools，和本模块同在仓库根下的 services 包里，不用改 sys.path。"""


def authorize(name: str) -> dict | None:
    """12.D（#60）每次调用前的二次核对：工具要用的权限 ⊆ 清单声明 ⊆ 授权记录，第三方包的授权还要绑定包内容。

    返回 None 放行；否则是 permission_denied 的工具结果（不执行）。不认识的工具交给后面的 unknown_tool。
    网页宿主的 host 工具（_execute_web_tool）和这里的 python 工具都先过它。
    """
    _ensure()
    from services.tools import packs

    try:
        pack = packs.find_tool(name)
    except packs.PackError as exc:
        return {"ok": False, "tool": name, "executed": False, "error": {"code": exc.code, "message": exc.message}}
    if pack is None:
        return None
    return packs.authorize(pack)


def execute_tool(name: str, arguments: dict) -> dict:
    _ensure()
    from services.tools.registry import audience_tools, call_tool, validate_external, validate_tool

    denied = authorize(name)
    if denied:
        return denied
    if any(item.name == name for item in audience_tools("internal")):
        problem = validate_tool(name, arguments)
        if problem:
            return problem
        from services.gateway import egress

        from services.runtime import injection

        # 12.E：出站记录带上工具名，数据流图按它连边；12.G：工具执行期间不许写配置。
        with egress.note(tool=name), injection.tool_scope(name):
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
    from services.tools.api import tool_error
    from services.tools.registry import audience_tools
    from services.tools.schema import check as check_schema

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
    from services.tools import packs

    pack = packs.find_tool(name)
    if pack is None:
        return False
    return packs.needs_confirm(pack, arguments)


def confirm_every_call(name: str) -> bool:
    """12.E：联网 / 读本地内容的第三方工具每次调用都要确认，不复用同一轮里之前的同意。
    内置工具（如一轮连出 N 张图的 generate_image）仍然一轮问一次；本地提示词发第三方另有按句柄和主机的确认。"""
    _ensure()
    from services.tools import packs

    pack = packs.find_tool(name)
    if packs.is_mcp(pack):  # 14.A：远端工具做什么宿主看不见，一律每次确认
        return True
    return bool(pack is not None and not pack.builtin and packs.sensitive(pack))


def egress_preview(name: str, arguments=None) -> dict:
    """确认卡上的出站摘要（#57）：由宿主按工具清单的 permissions 生成，不用模型的解释文字。

    这里只给通用部分（会不会联网、会不会读本地内容）；网页宿主再补上目的地主机和句柄用途。
    """
    _ensure()
    from services.tools import packs

    pack = packs.find_tool(name)
    permissions = (pack.manifest.get("permissions") or {}) if pack is not None else {}
    return {
        "by": "host",
        "network": bool(permissions.get("network")),
        "reads_local": bool(permissions.get("read_local_content")),
        "sends_local_to_model": bool(permissions.get("send_local_to_model")),
        "sandboxed": bool(pack is not None and not pack.builtin),
        "sensitive": bool(pack is not None and packs.sensitive(pack)),
        **(_mcp_preview(pack) if packs.is_mcp(pack) else {}),
    }


def _mcp_preview(pack) -> dict:
    from services.tools import mcp_client

    facts = mcp_client.review_facts(pack.manifest.get("entry") or {})
    out = {"mcp": True, "mcp_server": getattr(pack, "mcp_server", None), "mcp_transport": facts["transport"],
           "sensitive": True}
    if facts.get("host"):
        out["mcp_host"] = facts["host"]
    return out


def openai_tools() -> list[dict]:
    _ensure()
    from services.tools.registry import external_contracts

    return external_contracts()
