"""本机会话和供应商设置。

密钥只出现在 provider.json。会话记录里没有密钥。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import re
import threading
import time
import urllib.parse
import uuid

from packages.artifacts import ArtifactError, ArtifactStore
from services.gateway.openai_chat import DEFAULT_BASE_URL, DEFAULT_MODEL

_ID = re.compile(r"^[a-z]_[0-9a-f]{32}$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,127}$")
_REF = re.compile(r"(?:[ac]_[0-9a-f]{32}|art_[0-9a-f]{32})")
_PURPOSES = ("image_prompt", "keep")
MAX_FILE = 262144
MAX_ATTACHMENT = 8 * 1024 * 1024
MEMORY_KEEP = 6
MEMORY_LINE = 80
MEMORY_TOTAL = 800


class StoreError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def check_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise StoreError(f"{label}不合法。")
    return value


def clean_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("基址需要是字符串。")
    raw = _with_scheme(value.strip())
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("基址只接受 http 或 https。")
    if parts.username or parts.password:
        raise ValueError("基址里不能带用户名或密码。")
    if parts.query or parts.fragment:
        raise ValueError("基址不要带问号后面的参数，只填写到版本路径。")
    host = parts.hostname
    if not host:
        raise ValueError("基址缺少主机名。")
    netloc = f"[{host}]" if ":" in host else host
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path.rstrip("/")
    suffix = "/chat/completions"
    if path.endswith(suffix):
        path = path[: -len(suffix)].rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def _with_scheme(raw: str) -> str:
    if not raw:
        raise ValueError("请填写 API 基址。")
    if "://" in raw:
        return raw
    hostish = raw.split("/", 1)[0]
    if "@" in hostish:
        hostish = hostish.rsplit("@", 1)[1]
    if hostish.startswith("["):
        name = hostish[1:].split("]", 1)[0]
    else:
        name = hostish.split(":", 1)[0]
    name = name.lower()
    local = name in ("localhost", "127.0.0.1", "::1") or name.endswith(".local")
    return ("http://" if local else "https://") + raw


def clean_model(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("模型标识需要是字符串。")
    model = value.strip()
    if not model or len(model) > 128 or any(ch.isspace() for ch in model):
        raise ValueError("模型标识不能为空，也不能包含空格。")
    return model


def clean_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("API key 需要是字符串。")
    key = value.strip()
    if any(ch in key for ch in "\r\n"):
        raise ValueError("API key 不能换行。")
    if not key or len(key) > 512:
        raise ValueError("请填写 API key。")
    return key


def _usable(key: str) -> str:
    try:
        return clean_key(key)
    except ValueError:
        return ""


def _env_key(base_url: str) -> tuple[str, str | None]:
    generic = _usable(os.environ.get("LLM_API_KEY", ""))
    if generic:
        return generic, "environment"
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    xai = _usable(os.environ.get("XAI_API_KEY", ""))
    if xai and host == "api.x.ai":
        return xai, "environment"
    return "", None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


class ProviderStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def resolve(self) -> dict:
        with self._lock:
            data = self._read()
        key = _usable(data.get("api_key") or "")
        source = "file" if key else None
        if not key:
            key, source = _env_key(data["base_url"])
        return {
            "base_url": data["base_url"],
            "model": data["model"],
            "api_key": key,
            "key_source": source,
        }

    def public(self) -> dict:
        resolved = self.resolve()
        key = resolved["api_key"]
        inputs = ["text"]
        outputs = ["text"]
        streaming = True
        try:
            from services.gateway import describe
            caps = describe(resolved["model"])
            raw_in = caps.get("input_modalities") or ("text",)
            raw_out = caps.get("output_modalities") or ("text",)
            inputs = [str(item) for item in raw_in]
            outputs = [str(item) for item in raw_out]
            streaming = bool(caps.get("streaming", True))
        except Exception:
            pass
        return {
            "base_url": resolved["base_url"],
            "model": resolved["model"],
            "configured": bool(key),
            "key_hint": key[-4:] if key else "",
            "key_source": resolved["key_source"],
            "input_modalities": inputs,
            "output_modalities": outputs,
            "streaming": streaming,
        }

    def update(self, *, base_url: str, model: str, api_key: str | None, clear_key: bool) -> dict:
        if clear_key and api_key:
            raise ValueError("不能同时填写和清除 API key。")
        cleaned_base = clean_base_url(base_url)
        cleaned_model = clean_model(model)
        with self._lock:
            current = self._read()
            if clear_key:
                key = ""
            elif api_key:
                key = clean_key(api_key)
            else:
                key = current.get("api_key") or ""
            _atomic_write(self.path, {
                "base_url": cleaned_base,
                "model": cleaned_model,
                "api_key": key,
            })
        return self.public()

    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {"base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL, "api_key": ""}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL, "api_key": ""}
        if not isinstance(data, dict):
            return {"base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL, "api_key": ""}
        try:
            base_url = clean_base_url(str(data.get("base_url") or DEFAULT_BASE_URL))
            model = clean_model(str(data.get("model") or DEFAULT_MODEL))
        except ValueError:
            base_url = DEFAULT_BASE_URL
            model = DEFAULT_MODEL
        api_key = data.get("api_key") if isinstance(data.get("api_key"), str) else ""
        return {"base_url": base_url, "model": model, "api_key": api_key}


def clean_purpose(value) -> str:
    if value in _PURPOSES:
        return value
    return "keep"


def clean_media_type(value) -> str:
    if not isinstance(value, str):
        return "application/octet-stream"
    media = value.split(";", 1)[0].strip().lower()
    if not _MEDIA.fullmatch(media):
        return "application/octet-stream"
    return media


def kind_from_media(media: str) -> str:
    if media.startswith("image/"):
        return "image"
    if media.startswith("audio/"):
        return "audio"
    if media.startswith("video/"):
        return "video"
    return "file"


def clean_filename(value) -> str:
    if not isinstance(value, str):
        return ""
    name = os.path.basename(value.replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name[:128]


class IdentityStore:
    """单用户开发登录。会话按这个用户归属。第 6 阶段再换成真正的账号。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def current(self) -> dict:
        with self._lock:
            data = self._read()
            if data is None:
                data = {"user_id": new_id("u_"), "display_name": "本机用户"}
                _atomic_write(self.path, data)
            return {
                "user_id": data["user_id"],
                "display_name": data.get("display_name") or "本机用户",
            }

    def _read(self) -> dict | None:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        user_id = data.get("user_id")
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            return None
        name = data.get("display_name")
        return {
            "user_id": user_id,
            "display_name": name if isinstance(name, str) and name.strip() else "本机用户",
        }


def _kind_of(record: dict) -> str:
    kind = record.get("kind") or record.get("type")
    return kind if kind in ("text", "file") else "text"


def _public_content(record: dict) -> dict:
    kind = _kind_of(record)
    media = record.get("media_type")
    if not isinstance(media, str) or not media:
        media = "text/plain" if kind == "text" else "application/octet-stream"
    purpose = record.get("purpose")
    if purpose not in _PURPOSES:
        purpose = "image_prompt" if record.get("message_id") else "keep"
    return {
        "handle": record.get("handle"),
        "type": kind,
        "kind": kind,
        "media_type": media,
        "size": record.get("size"),
        "purpose": purpose,
    }


def _file_secrets(data: bytes) -> list[str]:
    found = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    if text:
        found.append(text)
    encoded = base64.b64encode(data).decode("ascii")
    if encoded and encoded not in found:
        found.append(encoded)
    return found


def _scrub_text(text: str, secrets: list[str]) -> str:
    if any(len(secret) >= 8 and secret in text for secret in secrets):
        return "这条本地内容已经丢掉。"
    return text


def _scrub_value(value, secrets: list[str]):
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, dict):
        return {key: _scrub_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, secrets) for item in value]
    return value


def _public_attachment(record: dict) -> dict:
    media = record.get("media_type")
    if not isinstance(media, str) or not media:
        media = "application/octet-stream"
    kind = record.get("kind")
    if kind not in ("image", "audio", "video", "file"):
        kind = kind_from_media(media)
    name = record.get("name")
    if not isinstance(name, str):
        name = ""
    return {
        "handle": record.get("handle"),
        "kind": kind,
        "media_type": media,
        "size": record.get("size"),
        "name": name,
    }


def _cover_local(text: str, bodies: list[tuple[str, str]]) -> str:
    for body, handle in bodies:
        if body in text:
            return f"本地内容句柄 {handle}。正文留在本机。"
    return text


def _build_memory(session: dict) -> dict:
    messages = []
    for message in session.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("status") == "running":
            continue
        if message.get("role") not in ("user", "assistant"):
            continue
        messages.append(message)
    if len(messages) <= MEMORY_KEEP:
        return {"summary": "", "refs": []}
    handles = {}
    bodies = []
    for item in session.get("contents") or []:
        if not isinstance(item, dict):
            continue
        handle = item.get("handle")
        text = item.get("text")
        if item.get("message_id") and isinstance(handle, str):
            handles[item["message_id"]] = handle
        if isinstance(text, str) and len(text) >= 8 and isinstance(handle, str):
            bodies.append((text, handle))
    bodies.sort(key=lambda pair: len(pair[0]), reverse=True)
    refs = []
    lines = []
    for message in messages[:-MEMORY_KEEP]:
        text = message.get("text") or ""
        message_id = message.get("message_id")
        if message.get("role") == "user" and message_id in handles:
            text = f"本地内容句柄 {handles[message_id]}"
        else:
            if message.get("role") == "assistant" and not str(text).strip():
                error = message.get("error") or {}
                text = error.get("message") or ""
            text = _cover_local(text, bodies)
        text = str(text).replace("\n", " ").strip()[:MEMORY_LINE]
        if text:
            who = "用户" if message.get("role") == "user" else "助手"
            lines.append(f"{who}：{text}")
        for item in message.get("attachments") or []:
            handle = item.get("handle") if isinstance(item, dict) else None
            if isinstance(handle, str) and handle not in refs:
                refs.append(handle)
        for event in message.get("events") or []:
            if not isinstance(event, dict) or event.get("type") != "artifact":
                continue
            art_id = event.get("artifact_id")
            if isinstance(art_id, str) and art_id not in refs:
                refs.append(art_id)
        for found in _REF.findall(text):
            if found not in refs:
                refs.append(found)
    summary = "；".join(lines)
    if len(summary) > MEMORY_TOTAL:
        summary = summary[:MEMORY_TOTAL]
    return {"summary": summary, "refs": refs[:40]}


def _message(role: str, text: str, status: str, message_id: str, run_id: str | None, attachments=None) -> dict:
    items = []
    for item in attachments or []:
        if isinstance(item, dict) and isinstance(item.get("handle"), str):
            items.append(_public_attachment(item))
    return {
        "message_id": message_id,
        "role": role,
        "text": text,
        "created_at": _now(),
        "status": status,
        "run_id": run_id,
        "error": None,
        "events": [],
        "runtime": [],
        "attachments": items,
    }


class SessionRunning(StoreError):
    pass


class SessionStore:
    def __init__(self, root: str, artifacts: ArtifactStore | None = None):
        self.root = root
        parent = os.path.dirname(os.path.abspath(root))
        self.artifacts = artifacts if artifacts is not None else ArtifactStore(os.path.join(parent, "artifacts"))
        self._lock = threading.RLock()
        os.makedirs(root, exist_ok=True)
        self._recover()

    def create(self, user_id: str) -> dict:
        check_id(user_id, "用户")
        now = _now()
        session = {
            "session_id": new_id("s_"),
            "user_id": user_id,
            "title": "新对话",
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "memory": {"summary": "", "refs": []},
        }
        with self._lock:
            self._write(session)
        return self.view(session)

    def list_sessions(self, user_id: str) -> list[dict]:
        check_id(user_id, "用户")
        with self._lock:
            found = []
            for name in os.listdir(self.root):
                if not name.endswith(".json"):
                    continue
                session = self._load_path(os.path.join(self.root, name))
                if session is None:
                    continue
                owner = session.get("user_id")
                if owner and owner != user_id:
                    continue
                found.append(self.summary(session))
        found.sort(key=lambda item: item["updated_at"], reverse=True)
        return found

    def get(self, session_id: str) -> dict:
        check_id(session_id, "会话")
        with self._lock:
            session = self._load_path(self._path(session_id))
        if session is None:
            raise StoreError("找不到这个会话。")
        return session

    def view(self, session: dict) -> dict:
        running_id = None
        for message in reversed(session["messages"]):
            if message.get("status") == "running":
                running_id = message.get("run_id")
                break
        public = {
            "session_id": session["session_id"],
            "user_id": session.get("user_id") or "",
            "title": session["title"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "running": running_id is not None,
            "run_id": running_id,
            "messages": session["messages"],
        }
        return public

    def summary(self, session: dict) -> dict:
        viewed = self.view(session)
        return {
            "session_id": viewed["session_id"],
            "title": viewed["title"],
            "updated_at": viewed["updated_at"],
            "running": viewed["running"],
        }

    def delete(self, session_id: str) -> dict:
        """删掉会话文件、会话目录，以及别的会话不再引用的附件和内容字节。
        还在回答的会话拒绝删除，抛 SessionRunning。"""
        check_id(session_id, "会话")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            if self.view(session)["running"]:
                raise SessionRunning("这个会话还在回答，先停下再删。")
            checksums = set()
            for key in ("attachments", "contents"):
                for item in session.get(key) or []:
                    if isinstance(item, dict) and isinstance(item.get("checksum"), str) and item["checksum"]:
                        checksums.add(item["checksum"])
            os.remove(self._path(session_id))
            shutil.rmtree(os.path.join(self.root, session_id), ignore_errors=True)
            removed = self._gc_blobs(checksums)
        return {"ok": True, "session_id": session_id, "blobs_removed": removed}

    def _gc_blobs(self, checksums: set) -> int:
        """删掉没有任何会话再引用的字节。调用方持锁。"""
        checksums = {c for c in checksums if isinstance(c, str) and c}
        if not checksums:
            return 0
        for other in self.all_sessions():
            for key in ("attachments", "contents"):
                for item in other.get(key) or []:
                    if isinstance(item, dict):
                        checksums.discard(item.get("checksum"))
        removed = 0
        for checksum in checksums:
            try:
                path = self.artifacts.path_for(checksum)
            except Exception:
                path = None
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return removed

    def all_sessions(self) -> list[dict]:
        """本机所有会话的原始记录（含本地正文）。只给本机资源面板用，不进模型。"""
        found = []
        with self._lock:
            for name in sorted(os.listdir(self.root)):
                if name.endswith(".json"):
                    session = self._load_path(os.path.join(self.root, name))
                    if session is not None:
                        found.append(session)
        return found

    def set_label(self, session_id: str, handle: str, label: str) -> bool:
        """给本地内容或附件起个标签。标签只在本机面板显示。"""
        check_id(session_id, "会话")
        label = re.sub(r"[\x00-\x1f]", " ", str(label or "")).strip()[:80]
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return False
            for key in ("contents", "attachments"):
                for item in session.get(key) or []:
                    if isinstance(item, dict) and item.get("handle") == handle:
                        if label:
                            item["label"] = label
                        else:
                            item.pop("label", None)
                        self._write(session)
                        return True
        return False

    def forget_attachment(self, session_id: str, handle: str) -> str:
        """删掉一个附件：记录、消息里的引用，以及没人再用的字节。"""
        if not isinstance(handle, str) or not _ID.fullmatch(handle):
            return "absent"
        check_id(session_id, "会话")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return "absent"
            items = session.get("attachments") or []
            found = [i for i in items if isinstance(i, dict) and i.get("handle") == handle]
            if not found:
                return "absent"
            session["attachments"] = [i for i in items if not (isinstance(i, dict) and i.get("handle") == handle)]
            for message in session.get("messages") or []:
                if isinstance(message.get("attachments"), list):
                    message["attachments"] = [a for a in message["attachments"]
                                              if not (isinstance(a, dict) and a.get("handle") == handle)]
            session["updated_at"] = _now()
            self._write(session)
            self._gc_blobs({found[0].get("checksum")})
        return "forgotten"

    def drop_artifact_events(self, session_id: str, artifact_id: str) -> int:
        """生成文件删掉后，会话里不再留它的展示事件。"""
        check_id(session_id, "会话")
        dropped = 0
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return 0
            for message in session.get("messages") or []:
                events = message.get("events")
                if isinstance(events, list):
                    kept = [e for e in events if not (isinstance(e, dict) and e.get("artifact_id") == artifact_id)]
                    dropped += len(events) - len(kept)
                    message["events"] = kept
            if dropped:
                session["updated_at"] = _now()
                self._write(session)
        return dropped

    def belongs_to(self, session: dict, user_id: str) -> bool:
        owner = session.get("user_id")
        if not owner:
            return True
        return owner == user_id

    def append_turn(self, session_id: str, user_text: str, run_id: str, attachments=None) -> dict:
        check_id(session_id, "会话")
        check_id(run_id, "运行")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            if any(message.get("status") == "running" for message in session["messages"]):
                raise StoreError("busy")
            user = _message("user", user_text, "completed", new_id("m_"), None, attachments)
            assistant = _message("assistant", "", "running", new_id("m_"), run_id)
            session["messages"].extend((user, assistant))
            if session.get("title") in (None, "", "新对话"):
                title = user_text.replace("\n", " ").strip()
                if not title and attachments:
                    title = "图片"
                session["title"] = title[:24] or "新对话"
            session["updated_at"] = _now()
            self._write(session)
            return assistant

    def update_message(
        self,
        session_id: str,
        message_id: str,
        *,
        text: str,
        status: str,
        error: dict | None,
    ) -> None:
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return
            for message in session["messages"]:
                if message.get("message_id") == message_id:
                    message["text"] = text
                    message["status"] = status
                    message["error"] = error
                    break
            session["updated_at"] = _now()
            self._write(session)

    def append_event(self, session_id: str, message_id: str, event: dict) -> None:
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return
            for message in session["messages"]:
                if message.get("message_id") == message_id:
                    message.setdefault("events", []).append(dict(event))
                    break
            session["updated_at"] = _now()
            self._write(session)

    def append_runtime(self, session_id: str, message_id: str, event: dict) -> None:
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return
            for message in session["messages"]:
                if message.get("message_id") == message_id:
                    message.setdefault("runtime", []).append(dict(event))
                    break
            session["updated_at"] = _now()
            self._write(session)

    def put_content(self, session_id: str, message_id: str, text: str) -> dict:
        """画面描述先入库。句柄是随机标识，不编码正文。同一条消息只记一次。"""
        check_id(session_id, "会话")
        check_id(message_id, "消息")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            contents = session.setdefault("contents", [])
            for item in contents:
                if item.get("message_id") == message_id:
                    return _public_content(item)
            record = {
                "handle": new_id("c_"),
                "type": "text",
                "kind": "text",
                "media_type": "text/plain",
                "size": len(text.encode("utf-8")),
                "purpose": "image_prompt",
                "text": text,
                "message_id": message_id,
                "created_at": _now(),
            }
            contents.append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_content(record)

    def add_content(self, session_id: str, text: str, purpose: str = "keep") -> dict:
        """对话框提交的正文。同一会话里相同正文和用途复用句柄。"""
        check_id(session_id, "会话")
        purpose = clean_purpose(purpose)
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            for item in session.get("contents") or []:
                if (
                    isinstance(item, dict)
                    and _kind_of(item) == "text"
                    and item.get("purpose") == purpose
                    and item.get("text") == text
                ):
                    return _public_content(item)
            record = {
                "handle": new_id("c_"),
                "type": "text",
                "kind": "text",
                "media_type": "text/plain",
                "size": len(text.encode("utf-8")),
                "purpose": purpose,
                "text": text,
                "message_id": "",
                "created_at": _now(),
            }
            session.setdefault("contents", []).append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_content(record)

    def add_file(self, session_id: str, data: bytes, media_type: str, purpose: str = "keep") -> dict:
        """文件字节进工件库。记录里没有路径、原始文件名和字节。同一会话相同字节复用句柄。"""
        check_id(session_id, "会话")
        if not isinstance(data, bytes) or not data:
            raise StoreError("空文件。")
        if len(data) > MAX_FILE:
            raise StoreError("太大。")
        purpose = clean_purpose(purpose)
        media = clean_media_type(media_type)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            for item in session.get("contents") or []:
                if (
                    isinstance(item, dict)
                    and _kind_of(item) == "file"
                    and item.get("purpose") == purpose
                    and item.get("checksum") == digest
                ):
                    return _public_content(item)
            try:
                self.artifacts.put(data)
            except ArtifactError as exc:
                raise StoreError("没能留下这个文件。") from exc
            record = {
                "handle": new_id("c_"),
                "type": "file",
                "kind": "file",
                "media_type": media,
                "size": len(data),
                "purpose": purpose,
                "checksum": digest,
                "message_id": "",
                "created_at": _now(),
            }
            session.setdefault("contents", []).append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_content(record)

    def add_attachment(self, session_id: str, data: bytes, media_type: str, name: str = "") -> dict:
        """对话附件。字节进工件库。同一会话相同字节复用句柄。"""
        check_id(session_id, "会话")
        if not isinstance(data, bytes) or not data:
            raise StoreError("空文件。")
        if len(data) > MAX_ATTACHMENT:
            raise StoreError("太大。")
        media = clean_media_type(media_type)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            for item in session.get("attachments") or []:
                if isinstance(item, dict) and item.get("checksum") == digest:
                    return _public_attachment(item)
            try:
                self.artifacts.put(data)
            except ArtifactError as exc:
                raise StoreError("没能留下这个文件。") from exc
            record = {
                "handle": new_id("a_"),
                "kind": kind_from_media(media),
                "media_type": media,
                "size": len(data),
                "name": clean_filename(name),
                "checksum": digest,
                "created_at": _now(),
            }
            session.setdefault("attachments", []).append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_attachment(record)

    def attachment_info(self, session_id: str, handle: str) -> dict | None:
        item = self._find_attachment(session_id, handle)
        if item is None:
            return None
        return _public_attachment(item)

    def attachment_bytes(self, session_id: str, handle: str) -> bytes | None:
        item = self._find_attachment(session_id, handle)
        if item is None:
            return None
        return self._read_blob(item, session_id, handle)

    def _find_attachment(self, session_id: str, handle: str) -> dict | None:
        if not isinstance(handle, str) or not _ID.fullmatch(handle):
            return None
        try:
            session = self.get(session_id)
        except StoreError:
            return None
        for item in session.get("attachments") or []:
            if isinstance(item, dict) and item.get("handle") == handle:
                return item
        return None

    def list_content(self, session_id: str) -> list[dict]:
        session = self.get(session_id)
        items = []
        for item in session.get("contents") or []:
            if isinstance(item, dict) and isinstance(item.get("handle"), str):
                items.append(_public_content(item))
        return items

    def forget_content(self, session_id: str, handle: str) -> str:
        """丢掉本会话的一枚句柄。别的会话调用时像没有这枚句柄。"""
        if not isinstance(handle, str) or not _ID.fullmatch(handle):
            return "absent"
        try:
            check_id(session_id, "会话")
        except StoreError:
            return "absent"
        secrets = []
        kind = ""
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return "absent"
            found = None
            kept = []
            for item in session.get("contents") or []:
                if isinstance(item, dict) and item.get("handle") == handle:
                    found = item
                    continue
                kept.append(item)
            if found is None:
                return "absent"
            kind = _kind_of(found)
            if kind == "text":
                text = found.get("text")
                if isinstance(text, str) and text:
                    secrets.append(text)
            else:
                data = self._read_blob(found, session_id, handle)
                if data:
                    secrets.extend(_file_secrets(data))
            for message in session.get("messages") or []:
                if isinstance(message.get("text"), str):
                    message["text"] = _scrub_text(message["text"], secrets)
                if isinstance(message.get("events"), list):
                    message["events"] = _scrub_value(message["events"], secrets)
                if isinstance(message.get("runtime"), list):
                    message["runtime"] = _scrub_value(message["runtime"], secrets)
            if isinstance(session.get("memory"), dict):
                session["memory"] = _scrub_value(session["memory"], secrets)
            session["contents"] = kept
            session["updated_at"] = _now()
            self._write(session)
            if kind == "file":
                self._gc_blobs({found.get("checksum")})
        if kind == "file":
            try:
                os.remove(self._file_path(session_id, handle))
            except OSError:
                pass
        return "forgotten"

    def content_info(self, session_id: str, handle: str) -> dict | None:
        item = self._find_content(session_id, handle)
        if item is None:
            return None
        return _public_content(item)

    def content_text(self, session_id: str, handle: str) -> str | None:
        item = self._find_content(session_id, handle)
        if item is None or _kind_of(item) != "text":
            return None
        text = item.get("text")
        return text if isinstance(text, str) else None

    def content_bytes(self, session_id: str, handle: str) -> bytes | None:
        item = self._find_content(session_id, handle)
        if item is None or _kind_of(item) != "file":
            return None
        return self._read_blob(item, session_id, handle)

    def retrieve(self, session_id: str, query: str) -> list[dict]:
        """本会话检索。结果只有公开字段和可打开的地址，没有正文或字节。"""
        check_id(session_id, "会话")
        needle = query.strip().lower() if isinstance(query, str) else ""
        if not needle:
            return []
        session = self.get(session_id)
        found = []
        seen = set()

        def take(item: dict) -> None:
            source = item.get("source_id")
            if not isinstance(source, str) or source in seen:
                return
            seen.add(source)
            found.append(item)

        for item in session.get("contents") or []:
            if not isinstance(item, dict):
                continue
            public = _public_content(item)
            hay = " ".join(
                str(public.get(key) or "")
                for key in ("handle", "kind", "type", "media_type", "purpose")
            )
            body = item.get("text") if isinstance(item.get("text"), str) else ""
            if needle not in hay.lower() and needle not in body.lower():
                continue
            record = {
                "source_id": public.get("handle"),
                "kind": public.get("kind"),
                "media_type": public.get("media_type"),
                "size": public.get("size"),
                "purpose": public.get("purpose"),
            }
            if public.get("kind") == "file" and isinstance(public.get("handle"), str):
                record["url"] = f"/api/sessions/{session_id}/contents/{public['handle']}"
            take(record)
        for item in session.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            public = _public_attachment(item)
            hay = " ".join(
                str(public.get(key) or "")
                for key in ("handle", "kind", "media_type", "name")
            )
            if needle not in hay.lower():
                continue
            handle = public.get("handle")
            record = {
                "source_id": handle,
                "kind": public.get("kind"),
                "media_type": public.get("media_type"),
                "size": public.get("size"),
                "name": public.get("name"),
            }
            if isinstance(handle, str):
                record["url"] = f"/api/sessions/{session_id}/attachments/{handle}"
            take(record)
        for message in session.get("messages") or []:
            if not isinstance(message, dict):
                continue
            for event in message.get("events") or []:
                if not isinstance(event, dict) or event.get("type") != "artifact":
                    continue
                art_id = event.get("artifact_id")
                if not isinstance(art_id, str) or not art_id:
                    continue
                hay = " ".join(
                    (
                        art_id,
                        str(event.get("media") or ""),
                        str(event.get("media_type") or ""),
                    )
                )
                if needle not in hay.lower():
                    continue
                url = event.get("url")
                if not isinstance(url, str) or not url:
                    url = f"/sessions/{session_id}/artifacts/{art_id}"
                take({
                    "source_id": art_id,
                    "kind": event.get("media") or "image",
                    "media_type": event.get("media_type") or "image/png",
                    "url": url,
                })
        return found

    def refresh_memory(self, session_id: str) -> dict:
        check_id(session_id, "会话")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return {"summary": "", "refs": []}
            memory = _build_memory(session)
            session["memory"] = memory
            session["updated_at"] = _now()
            self._write(session)
            return memory

    def secret_pairs(self, session_id: str) -> list[tuple[str, str]]:
        try:
            session = self.get(session_id)
        except StoreError:
            return []
        pairs = []
        for item in session.get("contents") or []:
            if not isinstance(item, dict):
                continue
            handle = item.get("handle")
            if not isinstance(handle, str):
                continue
            if _kind_of(item) == "file":
                data = self._read_blob(item, session_id, handle)
                if data:
                    pairs.extend((secret, handle) for secret in _file_secrets(data))
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                pairs.append((text, handle))
        return pairs

    def local_bodies(self, session_id: str) -> list[str]:
        return [text for text, _handle in self.secret_pairs(session_id)]

    def _find_content(self, session_id: str, handle: str) -> dict | None:
        if not isinstance(handle, str) or not _ID.fullmatch(handle):
            return None
        try:
            session = self.get(session_id)
        except StoreError:
            return None
        for item in session.get("contents") or []:
            if isinstance(item, dict) and item.get("handle") == handle:
                return item
        return None

    def _file_path(self, session_id: str, handle: str) -> str:
        return os.path.join(self.root, session_id, "files", handle)

    def _read_blob(self, item: dict, session_id: str, handle: str) -> bytes | None:
        checksum = item.get("checksum")
        if isinstance(checksum, str) and checksum:
            data = self.artifacts.get(checksum)
            if data is not None:
                return data
        return self._read_file(session_id, handle)

    def _read_file(self, session_id: str, handle: str) -> bytes | None:
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            return None
        if not isinstance(handle, str) or not _ID.fullmatch(handle):
            return None
        try:
            with open(self._file_path(session_id, handle), "rb") as raw:
                return raw.read()
        except OSError:
            return None

    def message_text(self, session_id: str, message_id: str) -> str:
        try:
            session = self.get(session_id)
        except StoreError:
            return ""
        for message in session["messages"]:
            if message.get("message_id") == message_id:
                return message.get("text") or ""
        return ""

    def _recover(self) -> None:
        with self._lock:
            for name in os.listdir(self.root):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(self.root, name)
                session = self._load_path(path)
                if session is None:
                    continue
                changed = False
                for message in session["messages"]:
                    if message.get("status") == "running":
                        message["status"] = "failed"
                        message["error"] = {
                            "code": "cancelled",
                            "message": "上次运行已经中断，没有继续向模型请求。",
                        }
                        changed = True
                if changed:
                    self._write(session)

    def _path(self, session_id: str) -> str:
        check_id(session_id, "会话")
        return os.path.join(self.root, session_id + ".json")

    def _load_path(self, path: str) -> dict | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        return data

    def _write(self, session: dict) -> None:
        _atomic_write(self._path(session["session_id"]), session)
