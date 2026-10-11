"""本机会话和供应商设置。

密钥只出现在 provider.json。会话记录里没有密钥。
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.parse
import uuid

from services.gateway.openai_chat import DEFAULT_BASE_URL, DEFAULT_MODEL

_ID = re.compile(r"^[a-z]_[0-9a-f]{32}$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,127}$")
_PURPOSES = ("image_prompt", "keep")
MAX_FILE = 262144


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
        return {
            "base_url": resolved["base_url"],
            "model": resolved["model"],
            "configured": bool(key),
            "key_hint": key[-4:] if key else "",
            "key_source": resolved["key_source"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "streaming": True,
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


def _message(role: str, text: str, status: str, message_id: str, run_id: str | None) -> dict:
    return {
        "message_id": message_id,
        "role": role,
        "text": text,
        "created_at": _now(),
        "status": status,
        "run_id": run_id,
        "error": None,
        "events": [],
    }


class SessionStore:
    def __init__(self, root: str):
        self.root = root
        self._lock = threading.RLock()
        os.makedirs(root, exist_ok=True)
        self._recover()

    def create(self) -> dict:
        now = _now()
        session = {
            "session_id": new_id("s_"),
            "title": "新对话",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        with self._lock:
            self._write(session)
        return self.view(session)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            found = []
            for name in os.listdir(self.root):
                if not name.endswith(".json"):
                    continue
                session = self._load_path(os.path.join(self.root, name))
                if session is None:
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

    def append_turn(self, session_id: str, user_text: str, run_id: str) -> dict:
        check_id(session_id, "会话")
        check_id(run_id, "运行")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            if any(message.get("status") == "running" for message in session["messages"]):
                raise StoreError("busy")
            user = _message("user", user_text, "completed", new_id("m_"), None)
            assistant = _message("assistant", "", "running", new_id("m_"), run_id)
            session["messages"].extend((user, assistant))
            if session.get("title") in (None, "", "新对话"):
                title = user_text.replace("\n", " ").strip()
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
            }
            contents.append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_content(record)

    def add_content(self, session_id: str, text: str, purpose: str = "keep") -> dict:
        """对话框提交的正文。每次都是新的随机句柄，不覆盖已有记录。"""
        check_id(session_id, "会话")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            record = {
                "handle": new_id("c_"),
                "type": "text",
                "kind": "text",
                "media_type": "text/plain",
                "size": len(text.encode("utf-8")),
                "purpose": clean_purpose(purpose),
                "text": text,
                "message_id": "",
            }
            session.setdefault("contents", []).append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_content(record)

    def add_file(self, session_id: str, data: bytes, media_type: str, purpose: str = "keep") -> dict:
        """文件字节写在本会话目录。记录里没有路径、原始文件名和字节。"""
        check_id(session_id, "会话")
        if not isinstance(data, bytes) or not data:
            raise StoreError("空文件。")
        if len(data) > MAX_FILE:
            raise StoreError("太大。")
        handle = new_id("c_")
        directory = os.path.join(self.root, session_id, "files")
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, handle)
        temporary = target + ".part"
        try:
            with open(temporary, "wb") as raw:
                raw.write(data)
            os.replace(temporary, target)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise StoreError("没能留下这个文件。") from exc
        record = {
            "handle": handle,
            "type": "file",
            "kind": "file",
            "media_type": clean_media_type(media_type),
            "size": len(data),
            "purpose": clean_purpose(purpose),
            "message_id": "",
        }
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                try:
                    os.remove(target)
                except OSError:
                    pass
                raise StoreError("找不到这个会话。")
            session.setdefault("contents", []).append(record)
            session["updated_at"] = _now()
            self._write(session)
        return _public_content(record)

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
                data = self._read_file(session_id, handle)
                if data:
                    secrets.extend(_file_secrets(data))
            for message in session.get("messages") or []:
                if isinstance(message.get("text"), str):
                    message["text"] = _scrub_text(message["text"], secrets)
                if isinstance(message.get("events"), list):
                    message["events"] = _scrub_value(message["events"], secrets)
            session["contents"] = kept
            session["updated_at"] = _now()
            self._write(session)
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
        return self._read_file(session_id, handle)

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
                data = self._read_file(session_id, handle)
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
