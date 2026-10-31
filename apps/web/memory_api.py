"""14.D（#81）长期记忆的网页宿主：工具执行的会话钩子、出口检查用的正文集合、记忆面板 API。

面板 API 只给本机（loopback），写操作还要过 12.G 的同源检查并记审计（只有字段名）。返回值只进浏览器，不进模型。
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid

from packages.artifacts.memory import MemoryError, valid_id
from services import telemetry
from services.gateway.egress import LocalBodies
from services.runtime import memory as rules


def values(app) -> dict:
    return app.memory_settings.current()


def enabled(app) -> bool:
    try:
        return bool(values(app).get("enabled"))
    except Exception:
        return False


def tool_include(app) -> tuple:
    """记忆打开时放进模型请求的工具；关着时是空的（请求和以前逐字节一致）。"""
    return rules.TOOLS if enabled(app) else ()


def known_bodies(app, session_id: str | None = None) -> LocalBodies:
    """偏好文字要避开的本地正文：当前会话（含文件）、长期记忆里的正文、本机其它会话的文字内容。"""
    bodies: list[str] = []
    prints: list[str] = []
    if session_id:
        local = app.sessions.local_bodies(session_id)
        bodies.extend(local)
        prints.extend(getattr(local, "fingerprints", ()) or ())
    for text in app.memory.bodies():
        bodies.append(text)
        prints.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
    try:
        sessions = app.sessions.all_sessions()
    except Exception:
        sessions = []
    for session in sessions:
        if session.get("session_id") == session_id:
            continue
        for item in session.get("contents") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                bodies.append(text)
            checksum = item.get("checksum")
            if isinstance(checksum, str) and checksum:
                prints.append(checksum)
    seen = set()
    unique = [text for text in bodies if not (text in seen or seen.add(text))]
    return LocalBodies(unique, prints)


def egress_bodies(app, session_id: str):
    """运行时出口检查用：本会话的本地正文 + 长期记忆里的正文（记忆打开时 recall 会把它带进任何会话）。"""
    local = app.sessions.local_bodies(session_id)
    try:
        extra = app.memory.bodies() if enabled(app) else []
    except Exception:
        extra = []
    if not extra:
        return local
    prints = list(getattr(local, "fingerprints", ()) or ())
    prints.extend(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in extra)
    return LocalBodies(list(local) + [text for text in extra if text not in local], prints)


def context_note(app, session_id: str) -> str:
    current = values(app)
    if not current.get("enabled"):
        return ""
    return rules.context_note(app.memory, current, app.sessions.local_bodies(session_id))


def host(app, session_id: str, run_id: str | None) -> rules.MemoryHost:
    def resolve(handle):
        record, code = app.sessions.resolve_content(session_id, handle, run_id=run_id, consume=False)
        if record is None:
            return None, code, None
        if record.get("type") == "file" or record.get("kind") == "file":
            return None, "file", None
        text = record.get("text")
        purpose = (record.get("scope") or {}).get("purpose") or record.get("purpose") or "keep"
        return (text if isinstance(text, str) and text else None), ("ok" if text else "handle_not_found"), purpose

    def issue(text, purpose, memory_id):
        return app.sessions.add_content(session_id, text, purpose=purpose, memory_id=memory_id)["handle"]

    return rules.MemoryHost(app.memory, values(app), resolve=resolve, issue=issue,
                            revoke=app.sessions.revoke_memory, bodies=known_bodies(app, session_id))


# ---------- 面板 API ----------

def _audit(action: str, fields) -> None:
    try:
        telemetry.record("security.config_changed", trace_id="cfg_" + uuid.uuid4().hex, target="memory",
                         fields=sorted(str(item)[:48] for item in fields)[:20], via="memory_panel", action=action)
    except Exception as exc:
        print(f"警告：记忆审计失败 {type(exc).__name__}", file=sys.stderr)


class PanelError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def view(app) -> dict:
    settings = app.memory_settings.public()
    current = settings["values"]
    return {
        "settings": settings,
        "storage_mode": app.memory.mode,
        "content_allowed": rules.content_allowed(app.memory, current),
        "corrupt": app.memory.corrupt,
        "items": app.memory.list(),
    }


def _check(app, texts) -> None:
    reason = rules.check_text(texts, known_bodies(app))
    if reason:
        from services.gateway import egress

        egress.security_event(reason, rules.CHANNEL)
        raise PanelError(400, reason, rules.HUMAN["local_content"])


def create(app, body: dict) -> dict:
    if body.get("kind") != "preference":
        raise PanelError(400, "invalid_parameter", "面板里只能手写偏好；本地内容在对话里经句柄记。")
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    label = body.get("label") if isinstance(body.get("label"), str) else ""
    _check(app, [text, label])
    try:
        item = app.memory.add_preference(text, label, source="user")
    except MemoryError as exc:
        raise PanelError(400, exc.code, exc.message) from exc
    _audit("create", ["preference"])
    return item


def update(app, memory_id: str, body: dict) -> dict:
    if not valid_id(memory_id):
        raise PanelError(404, "handle_not_found", rules.HUMAN["handle_not_found"])
    text = body.get("text") if isinstance(body.get("text"), str) else None
    label = body.get("label") if isinstance(body.get("label"), str) else None
    _check(app, [text or "", label or ""])
    try:
        item = app.memory.update(memory_id, text=text, label=label)
    except MemoryError as exc:
        raise PanelError(400, exc.code, exc.message) from exc
    if item is None:
        raise PanelError(404, "handle_not_found", rules.HUMAN["handle_not_found"])
    _audit("update", [key for key in ("text", "label") if body.get(key) is not None])
    return item


def delete(app, memory_id: str) -> dict:
    try:
        removed = app.memory.delete(memory_id)
    except MemoryError as exc:
        raise PanelError(400, exc.code, exc.message) from exc
    if not removed:
        raise PanelError(404, "handle_not_found", rules.HUMAN["handle_not_found"])
    revoked = app.sessions.revoke_memory(memory_id)
    _audit("delete", ["entry"])
    return {"ok": True, "memory_id": memory_id, "revoked_handles": revoked}


def preview(app, memory_id: str) -> dict:
    """本机面板预览 content 正文（只进浏览器）。"""
    item = app.memory.get(memory_id) if valid_id(memory_id) else None
    if item is None:
        raise PanelError(404, "handle_not_found", rules.HUMAN["handle_not_found"])
    return {"memory_id": memory_id, "text": app.memory.body(memory_id) if item["kind"] == "content" else item.get("text")}


def export_bytes(app) -> bytes:
    _audit("export", ["entries"])
    return json.dumps(app.memory.export(), ensure_ascii=False, indent=2).encode("utf-8")
