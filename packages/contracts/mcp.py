"""进程内的 MCP 消息形状。

只描述 tools/list 和 tools/call。不监听端口，也不起进程。
本机脚本和网页各有一份注册表，都用这里的同一种方言。
"""

from __future__ import annotations

import json

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def mcp_tool(name: str, description: str, parameters: dict) -> dict:
    """一条 MCP 工具。inputSchema 就是该用途已经写好的参数模式。"""
    return {
        "name": name,
        "description": description,
        "inputSchema": parameters,
    }


def tools_from_specs(specs: list[dict]) -> list[dict]:
    """本机注册表的条目是 name、description、parameters。"""
    listed = []
    for item in specs:
        listed.append(mcp_tool(
            item["name"],
            item.get("description") or "",
            item.get("parameters") or {"type": "object", "properties": {}},
        ))
    return listed


def tools_from_openai(tools: list[dict]) -> list[dict]:
    """网页注册表的条目是 OpenAI function。参数模式原样成为 inputSchema。"""
    listed = []
    for tool in tools:
        function = tool.get("function") or tool
        listed.append(mcp_tool(
            function["name"],
            function.get("description") or "",
            function.get("parameters") or {"type": "object", "properties": {}},
        ))
    return listed


def call_result(payload: dict) -> dict:
    """工具结果放进一段文本。ok 不为 true 时标记 isError。"""
    failed = not (isinstance(payload, dict) and payload.get("ok") is True)
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": failed,
    }


def dispatch(message, *, list_tools, call_tool):
    """处理一条 JSON-RPC 请求。没有 id 的通知不返回响应。"""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "请求必须是对象。")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _reply(message, _error(None, INVALID_REQUEST, "不是 JSON-RPC 请求。"))
    method = message["method"]
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _reply(message, _error(None, INVALID_PARAMS, "params 必须是对象。"))
    if method == "tools/list":
        return _reply(message, _ok({"tools": list_tools()}))
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _reply(message, _error(None, INVALID_PARAMS, "tools/call 需要字符串 name 和对象 arguments。"))
        try:
            payload = call_tool(name, arguments)
        except Exception:
            return _reply(message, _error(None, INTERNAL_ERROR, "工具调用失败。"))
        if not isinstance(payload, dict):
            return _reply(message, _error(None, INTERNAL_ERROR, "工具没有返回对象。"))
        return _reply(message, _ok(call_result(payload)))
    return _reply(message, _error(None, METHOD_NOT_FOUND, "没有这个方法。"))


def _ok(result: dict) -> dict:
    return {"jsonrpc": "2.0", "result": result}


def _error(request_id, code: int, message: str) -> dict:
    payload = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if request_id is not None:
        payload["id"] = request_id
    return payload


def _reply(message: dict, payload: dict) -> dict | None:
    if "id" not in message:
        return None
    payload["id"] = message["id"]
    return payload
