"""会话级数据流图（路线图 12.E #61）。

只从两样东西拼出来：12.A 的出站审计记录（egress.read，本来就只有元数据）和会话里本地内容句柄的公开字段
（store.list_content）。图里没有正文、提示词、密钥、路径，句柄只给前 6 位。

    本地内容 ──holds──▶ 句柄 ──uses──▶ 工具 ──egress──▶ 本机 ComfyUI | 局域网 | 第三方
                                       对话模型请求 ──egress──▶ 模型服务（只带句柄名，不带正文）

边上按 sent / blocked / failed 计数，blocked 和 failed 带原因码分布。
"""

from __future__ import annotations

import time

SCHEMA = "mas.dataflow/1"
PREFIX = 6
LANES = ("local", "handle", "tool", "destination")
TERMINAL = {"egress.sent": "sent", "egress.blocked": "blocked", "egress.failed": "failed"}
_CHANNEL_TOOL = {"chat_completion": "chat"}
_SCOPE_LABEL = {"local": "本机", "lan": "局域网", "third_party": "第三方"}
_PURPOSE_LABEL = {"image_prompt": "出图提示词", "keep": "留存"}


def _counts() -> dict:
    return {"sent": 0, "blocked": 0, "failed": 0}


def build(session_id: str, events: list[dict], contents: list[dict]) -> dict:
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def node(node_id: str, **fields) -> dict:
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, **fields}
        return nodes[node_id]

    def edge(src: str, dst: str, kind: str) -> dict:
        key = (src, dst, kind)
        if key not in edges:
            edges[key] = {"from": src, "to": dst, "kind": kind, "counts": _counts(), "reasons": {},
                          "local_derived": 0}
        return edges[key]

    node("local", kind="local", lane="local", label="本地内容（只在本机）")
    for item in contents or []:
        handle = str(item.get("handle") or "")
        if not handle:
            continue
        prefix = handle[:PREFIX]
        purpose = str(item.get("purpose") or "")
        node("h:" + prefix, kind="handle", lane="handle", label=f"句柄 {prefix}…", handle_id_prefix=prefix,
             purpose=purpose, purpose_label=_PURPOSE_LABEL.get(purpose, purpose), state=str(item.get("state") or ""),
             content_kind=str(item.get("kind") or ""))
        edge("local", "h:" + prefix, "holds")

    totals = _counts()
    local_to_third_party = 0
    for event in events or []:
        outcome = TERMINAL.get(event.get("type"))
        if outcome is None:
            continue
        totals[outcome] += 1
        channel = str(event.get("channel") or "other")
        tool = str(event.get("tool") or _CHANNEL_TOOL.get(channel) or "")
        if tool == "chat":
            tool_id = "t:chat"
            node(tool_id, kind="model", lane="tool", label="对话模型请求（只带句柄名）", tool="")
        elif tool:
            tool_id = "t:" + tool
            node(tool_id, kind="tool", lane="tool", label=tool, tool=tool)
        else:
            tool_id = "t:?" + channel
            node(tool_id, kind="tool", lane="tool", label=f"（未标工具：{channel}）", tool="")
        host = str(event.get("destination_host") or "")
        scope = str(event.get("destination_scope") or "third_party")
        if channel == "comfyui_local" and scope == "local":
            dest_id, dest_kind, label = "d:local_comfyui", "local_comfyui", f"本机 ComfyUI（{host or '未配置'}）"
        else:
            dest_kind = {"local": "local", "lan": "lan"}.get(scope, "third_party")
            dest_id = f"d:{dest_kind}:{host}"
            what = "ComfyUI" if channel == "comfyui_local" else {"chat_completion": "对话模型",
                                                                  "remote_image": "出图服务",
                                                                  "toolpack": "工具包连接"}.get(channel, "服务")
            label = f"{_SCOPE_LABEL[dest_kind]}{what}（{host or '未知主机'}）"
        dest = node(dest_id, kind="destination", lane="destination", label=label, destination=dest_kind,
                    destination_host=host, channels=[])
        if channel not in dest["channels"]:
            dest["channels"].append(channel)
        local = bool(event.get("contains_local_derived"))
        prefix = str(event.get("handle_id_prefix") or "")[:PREFIX]
        if prefix:
            if "h:" + prefix not in nodes:
                node("h:" + prefix, kind="handle", lane="handle", label=f"句柄 {prefix}…", handle_id_prefix=prefix,
                     purpose="", purpose_label="", state="gone", content_kind="")
                edge("local", "h:" + prefix, "holds")
            used = edge("h:" + prefix, tool_id, "uses")
            used["counts"][outcome] += 1
            if local:
                used["local_derived"] += 1
        out = edge(tool_id, dest_id, "egress")
        out["counts"][outcome] += 1
        if local:
            out["local_derived"] += 1
            if dest_kind == "third_party" and outcome == "sent":
                local_to_third_party += 1
        reason = event.get("reason_code")
        if reason and outcome != "sent":
            out["reasons"][str(reason)] = out["reasons"].get(str(reason), 0) + 1

    return {
        "schema": SCHEMA,
        "session_id": session_id,
        "generated_at": round(time.time(), 3),
        "lanes": list(LANES),
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "summary": {**totals, "local_derived_to_third_party": local_to_third_party,
                    "handles": sum(1 for n in nodes.values() if n["kind"] == "handle")},
    }
