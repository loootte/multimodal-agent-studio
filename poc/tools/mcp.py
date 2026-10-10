"""两套受众各自的 MCP 投影。不监听端口。"""

from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

from packages.contracts.mcp import dispatch, tools_from_openai, tools_from_specs

from tools.registry import call_tool, external_contracts, tool_specs, validate_external

BLOCKED = {"prompt", "text", "body", "content", "bytes", "path", "filename"}


def internal_handle(message: dict) -> dict | None:
    return dispatch(
        message,
        list_tools=lambda: tools_from_specs(tool_specs("internal")),
        call_tool=call_tool,
    )


def external_handle(message: dict) -> dict | None:
    return dispatch(
        message,
        list_tools=lambda: tools_from_openai(external_contracts()),
        call_tool=validate_external,
    )


def projection_error() -> str | None:
    """离线检查用。外部投影必须和网页契约一致，而且不能执行。"""
    import image_run

    published_tools = image_run.image_tools()
    listed = external_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = (listed or {}).get("result", {}).get("tools") or []
    names = [item.get("function", {}).get("name") for item in published_tools]
    if [item.get("name") for item in tools] != names:
        return f"外部 MCP 工具列表不对：{[item.get('name') for item in tools]}"
    by_name = {item["function"]["name"]: item["function"] for item in published_tools}
    blob = json.dumps(listed, ensure_ascii=False)
    for tool in tools:
        function = by_name[tool["name"]]
        if tool["description"] != function["description"] or tool["inputSchema"] != function["parameters"]:
            return "外部 MCP inputSchema 和网页工具参数不一致。"
        leaked = BLOCKED.intersection((tool["inputSchema"].get("properties") or {}))
        if leaked:
            return f"外部 MCP 带了正文或文件参数：{tool['name']} {sorted(leaked)}"
    if "generate_video" in blob:
        return "外部 MCP 发布了 generate_video。"
    return _call_does_not_execute()


def _call_does_not_execute() -> str | None:
    from tools._support import ensure_comfy_path

    ensure_comfy_path()
    import agent_tools
    import comfy_client

    seen = {"call": 0, "run": 0}
    original_call = agent_tools.call_tool
    original_run = comfy_client.run

    def wrapped_call(*args, **kwargs):
        seen["call"] += 1
        return original_call(*args, **kwargs)

    def wrapped_run(*args, **kwargs):
        seen["run"] += 1
        return []

    agent_tools.call_tool = wrapped_call
    comfy_client.run = wrapped_run
    try:
        secret = "local-body-secret-xyz"
        bad = external_handle({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "generate_image", "arguments": {"content_handle": "c_abc", "body": secret}},
        })
        good = external_handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "generate_image", "arguments": {"content_handle": "c_abc"}},
        })
        listed = external_handle({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "list_local_content", "arguments": {}},
        })
        unknown = external_handle({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "generate_video", "arguments": {}},
        })
    finally:
        agent_tools.call_tool = original_call
        comfy_client.run = original_run
    if secret in json.dumps(bad, ensure_ascii=False):
        return "外部 MCP 回显了正文。"
    if not bad or bad.get("result", {}).get("isError") is not True:
        return "外部 MCP 接受了正文参数。"
    if seen["call"] or seen["run"]:
        return "外部 MCP 执行了工具。"
    good_body = json.loads(good["result"]["content"][0]["text"])
    if good.get("result", {}).get("isError") or good_body.get("executed") is not False or good_body.get("artifact_id"):
        return "外部 MCP 不应执行通过校验的调用。"
    listed_body = json.loads(listed["result"]["content"][0]["text"])
    if listed.get("result", {}).get("isError") or listed_body.get("executed") is not False:
        return "外部 MCP 把列出内容当成了执行。"
    if not unknown or unknown.get("result", {}).get("isError") is not True:
        return "外部 MCP 不应发布 generate_video。"
    return None
