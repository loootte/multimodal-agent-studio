"""检查和评测用的假 HTTP MCP server（14.A #78）：替换 services.tools.mcp_client.HTTP_POST，不开端口、不联网。

tools 可以随时改（模拟 server 改了工具说明 / 参数）；calls 记下收到的每条 JSON-RPC 方法；
result_text 是 tools/call 返回的文字（可以放注入文字，测结果不可信）。tools/call 用 SSE 回，别的用 JSON 回。"""

import json

URL = "https://203.0.113.10/mcp"  # TEST-NET-3 文档地址：不是回环，不用解析 DNS


class FakeMcpHttp:
    def __init__(self, result_text="lookup ok"):
        self.tools = [{"name": "lookup", "description": "查一个词。",
                       "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                                       "required": ["query"]}}]
        self.calls = []
        self.bodies = []
        self.result_text = result_text

    def __call__(self, url, body, headers, timeout):
        message = json.loads(body.decode("utf-8"))
        method = message.get("method")
        self.calls.append(method)
        self.bodies.append(body.decode("utf-8"))
        if "id" not in message:
            return 202, {}, b""
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
            return 200, {"content-type": "application/json", "mcp-session-id": "sess-1"}, \
                json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        if headers.get("Mcp-Session-Id") != "sess-1":
            return 400, {}, b""
        if method == "tools/list":
            return 200, {"content-type": "application/json"}, \
                json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": self.tools}}).encode()
        if method == "tools/call":
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "result": {"content": [{"type": "text", "text": self.result_text},
                                            {"type": "image", "data": "AAAA", "mimeType": "image/png"}],
                                "isError": False}}
            return 200, {"content-type": "text/event-stream"}, \
                ("event: message\ndata: " + json.dumps(reply, ensure_ascii=False) + "\n\n").encode("utf-8")
        return 200, {"content-type": "application/json"}, \
            json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "no"}}).encode()


def manifest(name="mcp_fake", url=URL):
    return {"schema": "mas.tool/1", "name": name, "version": "0.1.0", "description": "假 HTTP MCP server。",
            "order": 920, "parameters": None, "mutating": True, "confirm": "always",
            "entry": {"kind": "mcp", "transport": "http", "url": url},
            "requires": {"backends": [], "python": []},
            "permissions": {"read_local_content": False, "network": True, "write_files": False,
                            "send_local_to_model": False}}


def write_pack(folder, name="mcp_fake", url=URL):
    import os

    target = os.path.join(folder, name)
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "tool.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest(name, url), handle, ensure_ascii=False)
    return target


def load():
    """按路径装载本模块（evals/fixtures 不是包）。"""
    import importlib.util
    import os
    import sys

    name = "mas_mcp_fake"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.abspath(__file__))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
