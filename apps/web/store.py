"""本机会话和供应商设置。

密钥只出现在 provider.json。会话记录里没有密钥。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import uuid

from services.gateway.openai_chat import DEFAULT_BASE_URL, DEFAULT_MODEL

_ID = re.compile(r"^[a-z]_[0-9a-f]{32}$")


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


def _public_content(record: dict) -> dict:
    return {
        "handle": record.get("handle"),
        "type": record.get("type"),
        "size": record.get("size"),
    }


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
                "size": len(text.encode("utf-8")),
                "text": text,
                "message_id": message_id,
            }
            contents.append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_content(record)

    def add_content(self, session_id: str, text: str) -> dict:
        """对话框提交的正文。每次都是新的随机句柄，不覆盖已有记录。"""
        check_id(session_id, "会话")
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                raise StoreError("找不到这个会话。")
            record = {
                "handle": new_id("c_"),
                "type": "text",
                "size": len(text.encode("utf-8")),
                "text": text,
                "message_id": "",
            }
            session.setdefault("contents", []).append(record)
            session["updated_at"] = _now()
            self._write(session)
            return _public_content(record)

    def content_text(self, session_id: str, handle: str) -> str | None:
        if not isinstance(handle, str) or not _ID.fullmatch(handle):
            return None
        try:
            session = self.get(session_id)
        except StoreError:
            return None
        for item in session.get("contents") or []:
            if isinstance(item, dict) and item.get("handle") == handle:
                text = item.get("text")
                return text if isinstance(text, str) else None
        return None

    def local_bodies(self, session_id: str) -> list[str]:
        try:
            session = self.get(session_id)
        except StoreError:
            return []
        bodies = []
        for item in session.get("contents") or []:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text:
                bodies.append(text)
        return bodies

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
