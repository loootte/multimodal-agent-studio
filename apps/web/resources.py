"""本地资源面板（issue #51）：列出、预览、改标签、下载、删除、复用本机保存的东西。

四类资源：本地提示词/文字（会话 contents 里的 text）、本地文件（contents 里的 file）、
对话附件（attachments）、生成文件（工件库里的图片/视频/页面）。

隐私约束：
- 这里的接口只给本机（127.0.0.1），返回值只进浏览器，从不进入任何模型请求。
- 列表里没有正文；正文只在单独的预览接口里给本机浏览器。
- 删除复用 forget 的擦除逻辑，字节只在没有会话再引用时删除，然后重建这个会话的轨迹。
- 正在回答的会话里的资源拒绝删除。
"""
from __future__ import annotations

import json
import os
import re

import store

LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
TYPES = ("prompt", "text", "file", "attachment", "artifact")
_ID = re.compile(r"^(prompt|text|file|attachment|artifact):(s_[0-9a-f]{32}):((?:c|a)_[0-9a-f]{32}|art_[0-9a-f]{32})$")


class ResourceError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


def parse_id(value) -> tuple[str, str, str]:
    m = _ID.fullmatch(value or "") if isinstance(value, str) else None
    if not m:
        raise ResourceError(400, "invalid_id", "资源标识不合法。")
    return m.group(1), m.group(2), m.group(3)


def _content_type(item: dict) -> str:
    kind = store._kind_of(item)
    if kind == "file":
        return "file"
    return "prompt" if item.get("purpose") == "image_prompt" or item.get("message_id") else "text"


def _mentions(session: dict, handle: str) -> int:
    """这枚句柄在会话消息、事件里出现了几次（只数次数，不取正文）。"""
    count = 0
    for message in session.get("messages") or []:
        blob = json.dumps([message.get("text"), message.get("events"), message.get("runtime")], ensure_ascii=False)
        count += blob.count(handle)
    return count


def _artifact_root():
    import image_run

    try:
        return image_run.artifact_store()
    except Exception:
        return None


def _artifacts_of(gen_store, session_id: str) -> list[tuple[dict, str | None]]:
    if gen_store is None:
        return []
    try:
        folder = gen_store._session_dir(session_id)
    except Exception:
        return []
    records = os.path.join(folder, "records")
    out = []
    if not os.path.isdir(records):
        return out
    for name in sorted(os.listdir(records)):
        if not name.endswith(".json"):
            continue
        record = gen_store.get(session_id, name[:-5])
        if not record:
            continue
        try:
            path = gen_store.file_for(session_id, record["artifact_id"])
        except Exception:
            path = None
        out.append((record, path))
    return out


def _label_path(gen_store, session_id: str, artifact_id: str) -> str | None:
    try:
        return gen_store._record_path(session_id, artifact_id)
    except Exception:
        return None


def inventory(app, kind: str | None = None, session: str | None = None, q: str | None = None) -> dict:
    """列表。没有正文、没有路径。总占用按去重后的字节算。"""
    sessions = app.sessions.all_sessions()
    titles = {s["session_id"]: s.get("title") or "" for s in sessions}
    running = {s["session_id"]: app.sessions.view(s)["running"] for s in sessions}
    by_checksum: dict[str, set] = {}
    for s in sessions:
        for key in ("contents", "attachments"):
            for item in s.get(key) or []:
                if isinstance(item, dict) and item.get("checksum"):
                    by_checksum.setdefault(item["checksum"], set()).add(s["session_id"])
    items = []
    blob_sizes: dict[str, int] = {}
    text_bytes = 0
    gen_bytes = 0

    def ref_list(owner, extra=()):
        ids = [owner] + sorted(x for x in extra if x != owner)
        return [{"session_id": i, "title": titles.get(i, ""), "running": bool(running.get(i))} for i in ids]

    for s in sessions:
        sid = s["session_id"]
        for item in s.get("contents") or []:
            if not isinstance(item, dict) or not isinstance(item.get("handle"), str):
                continue
            public = store._public_content(item)
            t = _content_type(item)
            size = int(item.get("size") or 0)
            checksum = item.get("checksum") if t == "file" else None
            if checksum:
                blob_sizes[checksum] = size
            else:
                text_bytes += size
            shared = sorted(by_checksum.get(checksum, set())) if checksum else []
            items.append({
                "id": f"{t}:{sid}:{item['handle']}", "type": t, "handle": item["handle"],
                "session_id": sid, "media_type": public["media_type"], "size": size,
                "purpose": public["purpose"], "label": item.get("label") or "", "name": "",
                "created_at": item.get("created_at") or s.get("created_at"),
                "state": public["state"], "expires_at": public["expires_at"],
                "sessions": ref_list(sid, shared), "mentions": _mentions(s, item["handle"]),
                "shared": len(shared) > 1, "running": bool(running.get(sid)),
            })
        for item in s.get("attachments") or []:
            if not isinstance(item, dict) or not isinstance(item.get("handle"), str):
                continue
            public = store._public_attachment(item)
            checksum = item.get("checksum")
            size = int(item.get("size") or 0)
            if checksum:
                blob_sizes[checksum] = size
            shared = sorted(by_checksum.get(checksum, set())) if checksum else []
            items.append({
                "id": f"attachment:{sid}:{item['handle']}", "type": "attachment", "handle": item["handle"],
                "session_id": sid, "media_type": public["media_type"], "size": size, "kind": public["kind"],
                "purpose": "", "label": item.get("label") or "", "name": public["name"],
                "created_at": item.get("created_at") or s.get("created_at"),
                "sessions": ref_list(sid, shared), "mentions": _mentions(s, item["handle"]),
                "shared": len(shared) > 1, "running": bool(running.get(sid)),
            })
    gen_store = _artifact_root()
    for s in sessions:
        sid = s["session_id"]
        for record, path in _artifacts_of(gen_store, sid):
            size = os.path.getsize(path) if path and os.path.isfile(path) else 0
            gen_bytes += size
            created = None
            if path and os.path.isfile(path):
                import time as _t
                created = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(os.path.getmtime(path)))
            aid = record["artifact_id"]
            items.append({
                "id": f"artifact:{sid}:{aid}", "type": "artifact", "handle": aid, "session_id": sid,
                "media_type": record.get("media_type") or "application/octet-stream", "size": size,
                "kind": record.get("type") or "", "purpose": "", "label": record.get("label") or "",
                "name": record.get("title") or "", "created_at": created or s.get("created_at"),
                "sessions": ref_list(sid), "mentions": _mentions(s, aid), "shared": False,
                "running": bool(running.get(sid)),
            })
    total = {"bytes": sum(blob_sizes.values()) + text_bytes + gen_bytes,
             "blobs": sum(blob_sizes.values()), "texts": text_bytes, "generated": gen_bytes, "count": len(items)}
    by_type = {t: sum(1 for i in items if i["type"] == t) for t in TYPES}
    if kind:
        items = [i for i in items if i["type"] == kind]
    if session:
        items = [i for i in items if any(r["session_id"] == session for r in i["sessions"])]
    if q:
        needle = q.lower()
        items = [i for i in items if needle in (i["label"] + " " + i["name"] + " " + i["handle"] + " " + i["media_type"]).lower()]
    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return {"items": items, "total": total, "by_type": by_type,
            "sessions": [{"session_id": s["session_id"], "title": titles[s["session_id"]]} for s in sessions]}


def _find(app, rid: str) -> dict:
    kind, sid, handle = parse_id(rid)
    for item in inventory(app)["items"]:
        if item["id"] == rid:
            return item
    raise ResourceError(404, "not_found", "找不到这个资源。")


def preview_text(app, rid: str) -> dict:
    """文字正文，只回给本机浏览器。"""
    kind, sid, handle = parse_id(rid)
    if kind not in ("prompt", "text"):
        raise ResourceError(400, "not_text", "这个资源不是文字。")
    text = app.sessions.content_text(sid, handle)
    if text is None:
        raise ResourceError(404, "not_found", "找不到这个资源。")
    return {"id": rid, "text": text}


def raw(app, rid: str) -> tuple[bytes, str, str]:
    """字节、类型、下载文件名。"""
    kind, sid, handle = parse_id(rid)
    if kind in ("prompt", "text"):
        text = app.sessions.content_text(sid, handle)
        if text is None:
            raise ResourceError(404, "not_found", "找不到这个资源。")
        return text.encode("utf-8"), "text/plain; charset=utf-8", f"{handle}.txt"
    if kind == "file":
        info = app.sessions.content_info(sid, handle)
        data = app.sessions.content_bytes(sid, handle)
        media = (info or {}).get("media_type") or "application/octet-stream"
    elif kind == "attachment":
        info = app.sessions.attachment_info(sid, handle)
        data = app.sessions.attachment_bytes(sid, handle)
        media = (info or {}).get("media_type") or "application/octet-stream"
    else:
        gen_store = _artifact_root()
        path = None
        record = None
        if gen_store is not None:
            record = gen_store.get(sid, handle)
            try:
                path = gen_store.file_for(sid, handle)
            except Exception:
                path = None
        data = open(path, "rb").read() if path else None
        media = (record or {}).get("media_type") or "application/octet-stream"
        if path:
            return data, media, os.path.basename(path)
    if data is None:
        raise ResourceError(404, "not_found", "找不到这个资源。")
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
           "text/plain": ".txt", "application/pdf": ".pdf", "video/mp4": ".mp4", "audio/mpeg": ".mp3"}.get(media.split(";")[0], ".bin")
    return data, media, handle + ext


def set_label(app, rid: str, label: str) -> dict:
    kind, sid, handle = parse_id(rid)
    label = re.sub(r"[\x00-\x1f]", " ", str(label or "")).strip()[:80]
    if kind == "artifact":
        gen_store = _artifact_root()
        path = _label_path(gen_store, sid, handle) if gen_store else None
        record = gen_store.get(sid, handle) if gen_store else None
        if not path or not record:
            raise ResourceError(404, "not_found", "找不到这个资源。")
        if label:
            record["label"] = label
        else:
            record.pop("label", None)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle_out:
            json.dump(record, handle_out, ensure_ascii=False, indent=2)
            handle_out.write("\n")
        os.replace(tmp, path)
    elif not app.sessions.set_label(sid, handle, label):
        raise ResourceError(404, "not_found", "找不到这个资源。")
    return {"id": rid, "label": label}


def _warnings(item: dict) -> list[str]:
    out = []
    others = [r for r in item["sessions"] if r["session_id"] != item["session_id"]]
    if others:
        out.append(f"另外 {len(others)} 个会话用着同样的字节；只删本条记录，字节保留到没人用为止。")
    if item.get("mentions"):
        out.append(f"会话里有 {item['mentions']} 处提到它；删除后这些地方会被擦除或不再显示。")
    return out


def delete(app, ids: list, confirm: bool = False) -> dict:
    """批量删除。需要确认的先返回 409 needs_confirm；运行中的会话里的逐条拒绝。"""
    if not isinstance(ids, list) or not ids or len(ids) > 500:
        raise ResourceError(400, "invalid_ids", "要删的资源列表不合法。")
    listing = {i["id"]: i for i in inventory(app)["items"]}
    found, missing = [], []
    for rid in ids:
        parse_id(rid)
        (found if rid in listing else missing).append(rid)
    refused = [{"id": rid, "code": "session_running", "message": "这个会话还在回答，先停下再删。"}
               for rid in found if listing[rid]["running"]]
    refused_ids = {r["id"] for r in refused}
    todo = [listing[rid] for rid in found if rid not in refused_ids]
    warnings = {i["id"]: _warnings(i) for i in todo if _warnings(i)}
    if warnings and not confirm:
        raise ResourceError(409, "needs_confirm", "有资源被会话引用，确认后再删。",
                            {"warnings": warnings, "refused": refused})
    deleted, touched = [], set()
    for item in todo:
        kind, sid, handle = parse_id(item["id"])
        if kind in ("prompt", "text", "file"):
            status = app.sessions.forget_content(sid, handle)
        elif kind == "attachment":
            status = app.sessions.forget_attachment(sid, handle)
        else:
            status = _forget_artifact(app, sid, handle)
        if status == "forgotten":
            deleted.append(item["id"])
            touched.add(sid)
        else:
            missing.append(item["id"])
    trajectories = {}
    if touched:
        from services.meta.export import Exporter
        exporter = Exporter(app.data_dir, sessions=app.sessions)
        for sid in sorted(touched):
            trajectories = exporter.forget_session(sid)
    return {"deleted": deleted, "refused": refused, "missing": missing, "trajectories": trajectories}


def _forget_artifact(app, sid: str, artifact_id: str) -> str:
    gen_store = _artifact_root()
    if gen_store is None:
        return "absent"
    record = gen_store.get(sid, artifact_id)
    if not record:
        return "absent"
    try:
        path = gen_store.file_for(sid, artifact_id)
    except Exception:
        path = None
    record_path = _label_path(gen_store, sid, artifact_id)
    for target in (path, record_path):
        if target:
            try:
                os.remove(target)
            except OSError:
                pass
    app.sessions.drop_artifact_events(sid, artifact_id)
    return "forgotten"


def revoke(app, rid: str) -> dict:
    """撤销本地提示词/文字/文件的句柄（12.B #58）：正文留在本机，这个句柄串以后不能再消费。
    要再用就「复用」，会换一枚新句柄。"""
    kind, sid, handle = parse_id(rid)
    if kind not in ("prompt", "text", "file"):
        raise ResourceError(400, "not_revocable", "只有本地提示词、文字和文件有句柄可以撤销。")
    if app.sessions.revoke_content(sid, handle) != "revoked":
        raise ResourceError(404, "not_found", "找不到这个资源。")
    return {"id": rid, "state": "revoked"}


def reuse(app, rid: str, target_session: str) -> dict:
    """把本地提示词/文字/文件复制到当前会话，只回句柄。正文不经过模型。"""
    kind, sid, handle = parse_id(rid)
    try:
        store.check_id(target_session, "会话")
        app.sessions.get(target_session)
    except store.StoreError as exc:
        raise ResourceError(404, "not_found", "找不到当前会话。") from exc
    if kind in ("prompt", "text"):
        info = app.sessions.content_info(sid, handle)
        text = app.sessions.content_text(sid, handle)
        if text is None or info is None:
            raise ResourceError(404, "not_found", "找不到这个资源。")
        record = app.sessions.add_content(target_session, text, "image_prompt" if kind == "prompt" else info.get("purpose") or "keep")
    elif kind == "file":
        info = app.sessions.content_info(sid, handle)
        data = app.sessions.content_bytes(sid, handle)
        if data is None or info is None:
            raise ResourceError(404, "not_found", "找不到这个资源。")
        record = app.sessions.add_file(target_session, data, info.get("media_type") or "application/octet-stream", info.get("purpose") or "keep")
    else:
        raise ResourceError(400, "not_reusable", "只有本地提示词、文字和文件可以复用。")
    new = record["handle"]
    if kind == "prompt":
        snippet = f"用本地提示词 {new} 出图（content_handle 填 {new}）。"
    else:
        snippet = f"本地内容句柄 {new}。"
    return {"handle": new, "session_id": target_session, "type": kind, "snippet": snippet}
