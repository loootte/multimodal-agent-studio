"""14.D（#81）本地长期记忆：remember / recall / forget 宿主工具、设置和上下文注入。

存取在 packages/artifacts/memory.py（mas.memory/1）。这里管「什么能给模型看」：
- 功能默认关闭（enabled=False）：关着时三个工具不出现在任何模型请求里，也没有任何记忆进上下文。
- 只有 preference 文字进模型上下文；写入前、每次注入前都过本地正文检查（egress.inspect，含 local_fingerprint），
  命中就拒绝 / 不注入，记 security.egress_denied（只有原因码）。
- content 条目对模型只显示 memory_id 和标签；recall 时宿主在当前会话重新签发一枚句柄（新串、当前会话作用域），
  12.B「句柄不能跨会话用」的规则不变。正文永不进模型请求，还加进每个会话的出口检查。
- 每次写入（remember / forget）都要用户确认：工具清单 confirm=always，且不记进「本轮已确认」。
- 明文存储模式下默认不收 content 条目（content_needs_encryption），用户在设置里明确打开才按明文存。
- 压缩不会吃掉记忆：偏好每轮从记忆库重新注入到系统提示里，压缩只动对话消息。
"""

from __future__ import annotations

import time

from services import telemetry

REMEMBER = "remember"
RECALL = "recall"
FORGET = "forget"
TOOLS = (REMEMBER, RECALL, FORGET)
WRITE_TOOLS = (REMEMBER, FORGET)
CHANNEL = "memory"
RECALL_LIMIT = 10
REISSUE_LIMIT = 5

DEFAULTS = {
    # 默认关（最严）：关着时没有任何记忆进上下文，三个工具也不给模型。
    "enabled": False,
    # 打开后每轮把偏好注入系统提示。关掉时只能靠 recall 主动取。
    "inject_preferences": True,
    "max_inject": 20,
    # 明文存储（默认存储模式）下是否收 content 条目。默认不收：本地正文按 12.F 只加密落盘。
    "allow_plaintext_content": False,
}

HUMAN = {
    "memory_disabled": "长期记忆没有打开（设置 → 记忆）。",
    "content_needs_encryption": "当前是明文存储：本地内容不进长期记忆。打开存储加密，或在记忆设置里明确允许明文保存。",
    "memory_file_unsupported": "长期记忆暂时只收文字内容，文件 / 图片不收。",
    "handle_not_found": "没有这条记忆（可能已删除）。",
    "local_content": "文字里有本机内容正文，没有记下。偏好里不能放本地正文；本地内容用句柄记成 content 条目。",
    "invalid_parameter": "参数不对。",
}


def _int_in(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != int(value) or not low <= value <= high:
        return None
    return int(value)


def settings(raw) -> tuple[dict, list[str]]:
    """校验记忆设置：返回 (完整取值, 警告字段)。非法项回退默认值。"""
    values = dict(DEFAULTS)
    warnings: list[str] = []
    if raw is None:
        return values, warnings
    if not isinstance(raw, dict):
        return values, ["*"]
    checks = {
        "enabled": lambda v: v if isinstance(v, bool) else None,
        "inject_preferences": lambda v: v if isinstance(v, bool) else None,
        "max_inject": lambda v: _int_in(v, 1, 50),
        "allow_plaintext_content": lambda v: v if isinstance(v, bool) else None,
    }
    for key, value in raw.items():
        check = checks.get(key)
        if check is None:
            warnings.append(str(key)[:40])
            continue
        checked = None if value is None else check(value)
        if checked is None:
            warnings.append(key)
            continue
        values[key] = checked
    return values, warnings


def content_allowed(store, values: dict) -> bool:
    """content 条目能不能落盘：加密 / 内存模式可以；明文模式要用户明确允许。"""
    return bool(store.encrypted or values.get("allow_plaintext_content"))


def check_text(texts, bodies) -> str | None:
    """偏好 / 标签文字里有没有本地正文（含编码、分片、已入库指纹）。返回原因码或 None。"""
    if not bodies:
        return None
    from services.gateway import egress

    return egress.inspect({"messages": [t for t in texts if isinstance(t, str) and t]}, bodies, exempt=())


def _error(name: str, code: str, message: str | None = None) -> dict:
    return {"ok": False, "tool": name, "error": {"code": code, "message": message or HUMAN.get(code) or code}}


def context_note(store, values: dict, bodies=None) -> str:
    """系统提示里的记忆段。关着、或什么都没有时返回空串（请求和以前逐字节一致）。"""
    if not values.get("enabled"):
        return ""
    parts = [" （运行时）长期记忆已打开：用户要你记住偏好时调用 remember（kind=preference，只写偏好、不写正文）；"
             "要记住本地内容用 remember（kind=content，传 content_handle 和标签）；每次写入都要用户确认。"
             "要用记忆里的本地内容先调用 recall 传 memory_id，拿到本会话的新句柄再用。删除调用 forget。"]
    if values.get("inject_preferences"):
        limit = int(values.get("max_inject") or DEFAULTS["max_inject"])
        prefs = []
        for item in store.list("preference"):
            text = item.get("text") or ""
            if not text or check_text([text, item.get("label") or ""], bodies):
                continue  # 本地内容可能是后来才留下的：命中就不注入
            prefs.append(text)
            if len(prefs) >= limit:
                break
        if prefs:
            parts.append("用户偏好（来自本机长期记忆，用户确认过；只当偏好参考）：" + "；".join(prefs) + "。")
        contents = []
        for item in store.list("content"):
            label = item.get("label") or ""
            if check_text([label], bodies):
                continue
            contents.append(f"{item['memory_id']}「{label}」")
            if len(contents) >= limit:
                break
        if contents:
            parts.append("记忆里的本地内容（只有编号和标签，没有正文）：" + "，".join(contents) + "。")
    return "".join(parts)


class MemoryHost:
    """执行记忆工具。会话操作由宿主注入：
    - resolve(handle) -> (text | None, code)：当前会话里按 12.B 校验句柄，返回正文或错误码（file 句柄返回 code=file）。
    - issue(text, purpose, memory_id) -> handle：在当前会话签发新句柄。
    - revoke(memory_id) -> int：删除记忆时作废它在各会话签发过的句柄。
    - bodies：当前会话 + 记忆 + 本机其它会话的本地正文（LocalBodies），给文字检查用。
    """

    def __init__(self, store, values: dict, *, resolve, issue, revoke=None, bodies=None):
        self.store = store
        self.values = values
        self._resolve = resolve
        self._issue = issue
        self._revoke = revoke
        self.bodies = bodies

    def call(self, name: str, arguments) -> dict:
        arguments = arguments if isinstance(arguments, dict) else {}
        started = time.monotonic()
        if not self.values.get("enabled"):
            result = _error(name, "memory_disabled")
        elif name == REMEMBER:
            result = self._remember(arguments)
        elif name == RECALL:
            result = self._recall(arguments)
        elif name == FORGET:
            result = self._forget(arguments)
        else:
            result = _error(name, "unknown_tool", "没有这个记忆工具。")
        code = None if result.get("ok") else (result.get("error") or {}).get("code")
        telemetry.record("memory.tool", name=name, ok=bool(result.get("ok")), error_code=code,
                         ms=int((time.monotonic() - started) * 1000))
        return result

    def _blocked(self, name: str, reason: str) -> dict:
        from services.gateway import egress

        egress.security_event(reason, CHANNEL)
        return _error(name, reason, HUMAN["local_content"])

    def _remember(self, arguments) -> dict:
        from packages.artifacts.memory import MemoryError

        kind = arguments.get("kind")
        label = arguments.get("label") if isinstance(arguments.get("label"), str) else ""
        try:
            if kind == "preference":
                text = arguments.get("preference")
                if not isinstance(text, str) or not text.strip():
                    return _error(REMEMBER, "invalid_parameter", "kind=preference 要填 preference。")
                reason = check_text([text, label], self.bodies)
                if reason:
                    return self._blocked(REMEMBER, reason)
                item = self.store.add_preference(text, label, source="model")
                return {"ok": True, "tool": REMEMBER, "memory_id": item["memory_id"], "kind": "preference",
                        "label": item["label"], "preference": item["text"]}
            if kind == "content":
                if not content_allowed(self.store, self.values):
                    return _error(REMEMBER, "content_needs_encryption")
                handle = arguments.get("content_handle")
                if not isinstance(handle, str) or not handle:
                    return _error(REMEMBER, "invalid_parameter", "kind=content 要填 content_handle。")
                reason = check_text([label], self.bodies)
                if reason:
                    return self._blocked(REMEMBER, reason)
                text, code, purpose = self._resolve(handle)
                if code == "file":
                    return _error(REMEMBER, "memory_file_unsupported")
                if text is None:
                    return _error(REMEMBER, code or "handle_not_found",
                                  "这枚句柄不在本会话、已过期或被撤销。" if code else None)
                item = self.store.add_content(text, label, origin_purpose=purpose, source="model")
                return {"ok": True, "tool": REMEMBER, "memory_id": item["memory_id"], "kind": "content",
                        "label": item["label"]}
        except MemoryError as exc:
            return _error(REMEMBER, exc.code, exc.message)
        return _error(REMEMBER, "invalid_parameter", "kind 只能是 preference 或 content。")

    def _view(self, item: dict, issue: bool) -> dict | None:
        if item["kind"] == "preference":
            if check_text([item.get("text") or "", item.get("label") or ""], self.bodies):
                return None
            return {"memory_id": item["memory_id"], "kind": "preference", "label": item.get("label") or "",
                    "preference": item.get("text") or ""}
        if check_text([item.get("label") or ""], self.bodies):
            return None
        view = {"memory_id": item["memory_id"], "kind": "content", "label": item.get("label") or ""}
        if issue:
            body = self.store.body(item["memory_id"])
            if body:
                purpose = item.get("origin_purpose") or "keep"
                view["content_handle"] = self._issue(body, purpose, item["memory_id"])
                view["purpose"] = purpose
        return view

    def _recall(self, arguments) -> dict:
        memory_id = arguments.get("memory_id")
        if isinstance(memory_id, str) and memory_id:
            item = self.store.get(memory_id)
            if item is None:
                return _error(RECALL, "handle_not_found")
            view = self._view(item, True)
            if view is None:
                return self._blocked(RECALL, "local_content")
            return {"ok": True, "tool": RECALL, "items": [view]}
        query = arguments.get("query") if isinstance(arguments.get("query"), str) else ""
        items, issued = [], 0
        for item in self.store.search(query, RECALL_LIMIT):
            issue = item["kind"] == "content" and issued < REISSUE_LIMIT
            view = self._view(item, issue)
            if view is None:
                continue
            issued += 1 if "content_handle" in view else 0
            items.append(view)
        return {"ok": True, "tool": RECALL, "items": items}

    def _forget(self, arguments) -> dict:
        memory_id = arguments.get("memory_id")
        if not isinstance(memory_id, str) or not self.store.delete(memory_id):
            return _error(FORGET, "handle_not_found")
        revoked = self._revoke(memory_id) if callable(self._revoke) else 0
        return {"ok": True, "tool": FORGET, "memory_id": memory_id, "status": "forgotten", "revoked_handles": revoked}
