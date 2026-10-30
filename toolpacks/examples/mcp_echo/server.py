"""示例 stdio MCP server（只用标准库）。每行一条 JSON-RPC 消息，读到 EOF 就退出。

宿主在 12.D 沙箱里跑它：拿不到环境变量，不能启动别的程序，不能联网（清单没声明 network）。"""

import json
import sys

TOOLS = [
    {"name": "echo", "description": "原样返回 message。",
     "inputSchema": {"type": "object", "properties": {"message": {"type": "string", "description": "要回显的话"}},
                     "required": ["message"]}},
    {"name": "reverse", "description": "把 message 倒过来返回。",
     "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
]


def reply(rpc_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")


def handle(message):
    method, rpc_id = message.get("method"), message.get("id")
    if rpc_id is None:  # 通知不回
        return
    if method == "initialize":
        reply(rpc_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                       "serverInfo": {"name": "mcp_echo", "version": "0.1.0"}})
    elif method == "tools/list":
        reply(rpc_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = message.get("params") or {}
        text = str((params.get("arguments") or {}).get("message", ""))
        if params.get("name") == "echo":
            reply(rpc_id, {"content": [{"type": "text", "text": text}], "isError": False})
        elif params.get("name") == "reverse":
            reply(rpc_id, {"content": [{"type": "text", "text": text[::-1]}], "isError": False})
        else:
            reply(rpc_id, {"content": [{"type": "text", "text": "unknown tool"}], "isError": True})
    else:
        reply(rpc_id, error={"code": -32601, "message": "method not found"})


for line in sys.stdin:
    line = line.strip()
    if line:
        try:
            handle(json.loads(line))
        except ValueError:
            continue
