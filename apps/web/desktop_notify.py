"""桌面通知（13.B / #75）：完成 / 待确认 / 待提供本机内容时给托盘发一条系统通知。

通知文本只由宿主模板拼出来：固定文案 + 工具名（白名单 + 正则）+ 状态 + 张数。
不读、不转发事件里的任何正文字段（text / message / arguments / hint / draft / card / egress / 文件名），
所以提示词、回答正文、本机内容、文件名都不可能出现在通知里。

只在桌面壳模式（有启动令牌）启用；MAS_DESKTOP_NOTIFY=0 关掉。启动器 / Tauri 轮询
GET /api/desktop/notifications?after=N 取新通知（同样要令牌）。
"""
from __future__ import annotations

import os
import re
import threading
from collections import deque

TITLE = "Multimodal Agent Studio"
MAX_ITEMS = 50
_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")
HOST_TOOLS = frozenset({"generate_image", "plan_images", "accept_local_content", "generate_video",
                        "edit_image", "describe_image", "web_search"})
GENERIC = "工具"

# kind -> (status, 模板)。模板里只有 {tool} 和 {count} 两个槽位。
TEMPLATES = {
    "confirm": ("waiting_confirm", "待确认：{tool} 等你确认后才会执行"),
    "content": ("waiting_content", "待提供：需要你在窗口里提供本机内容"),
    "done": ("completed", "已完成：{tool}"),
    "done_images": ("completed", "已完成：{tool}，共 {count} 张图"),
    "failed": ("failed", "未完成：{tool} 出错了，回窗口查看"),
}


def enabled() -> bool:
    return (os.environ.get("MAS_DESKTOP_NOTIFY") or "").strip().lower() not in ("0", "off", "false", "no")


_KNOWN: set | None = None


def known_tools() -> set:
    global _KNOWN
    if _KNOWN is None:
        names = set(HOST_TOOLS)
        try:
            from tools import packs

            names.update(pack.name for pack in packs.discover())
        except Exception:
            pass
        _KNOWN = {name for name in names if isinstance(name, str) and _NAME.fullmatch(name)}
    return _KNOWN


def safe_tool(name) -> str:
    """工具名只认已知清单里的、且符合命名规则的；其它一律写成「工具」。"""
    if isinstance(name, str) and _NAME.fullmatch(name) and name in known_tools():
        return name
    return GENERIC


def render(kind: str, tool=None, count: int = 0) -> dict:
    status, template = TEMPLATES[kind]
    count = max(0, min(int(count or 0), 999))
    body = template.format(tool=safe_tool(tool) if tool else "本轮回答", count=count)
    return {"title": TITLE, "body": body, "kind": kind, "status": status,
            "tool": safe_tool(tool) if tool else None, "count": count}


class NotifyHub:
    def __init__(self):
        self._items: deque = deque(maxlen=MAX_ITEMS)
        self._next = 1
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _emit(self, item: dict) -> None:
        with self._lock:
            item = dict(item, id=self._next)
            self._next += 1
            self._items.append(item)

    def observe(self, run_id: str, event: dict) -> None:
        """Run.push 的旁路观察者。只看 type / tool / name / status，不看正文。"""
        if not enabled() or not isinstance(event, dict):
            return
        kind = event.get("type")
        with self._lock:
            state = self._runs.setdefault(str(run_id), {"tool": None, "images": 0})
            if len(self._runs) > 200:
                self._runs.pop(next(iter(self._runs)))
        if kind == "tool_call":
            tool = event.get("tool") or event.get("name")
            if tool and tool != "accept_local_content":
                state["tool"] = tool
        elif kind == "artifact":
            state["images"] += 1
            state["tool"] = state["tool"] or "generate_image"
        elif kind == "tool.confirm":
            self._emit(render("confirm", event.get("tool") or event.get("name")))
        elif kind == "content_request":
            self._emit(render("content"))
        elif kind == "message.completed":
            if state["images"]:
                self._emit(render("done_images", state["tool"] or "generate_image", state["images"]))
            else:
                self._emit(render("done", state["tool"]))
            self._drop(run_id)
        elif kind == "run.failed":
            if event.get("code") != "cancelled":  # 用户自己点的停止，不打扰
                self._emit(render("failed", state["tool"]))
            self._drop(run_id)

    def _drop(self, run_id) -> None:
        with self._lock:
            self._runs.pop(str(run_id), None)

    def since(self, after: int = 0) -> dict:
        with self._lock:
            items = [dict(item) for item in self._items if item["id"] > after]
            return {"items": items, "last": self._next - 1, "enabled": enabled()}
