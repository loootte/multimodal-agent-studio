"""14.D（#81）本地长期记忆：mas.memory/1。

两类条目：
- preference：非正文的偏好文字（例如「默认竖图」）。这类文字会进模型上下文，所以写入前必须过本地正文检查
  （由调用方做：services/runtime/memory.check_text，含 local_fingerprint）。
- content：本地正文，只存句柄形态（purpose=memory）。模型只看到条目编号和标签；要用时宿主在当前会话里
  重新签发一枚新句柄（12.B 不能跨会话用句柄的规则不变）。正文只在本机。

落盘（12.F）：
- encrypted：整份记忆用 mas/memory/store 派生的独立密钥加密（AES-256-GCM，和会话密钥互不相同）。
- memory：只在内存里，进程退出就没了。
- plaintext：偏好照常存；content 条目默认拒绝（content_needs_encryption），用户在设置里明确打开
  allow_plaintext_content 才按明文存（由调用方判断，见 content_allowed）。

本模块只管存取，不判断文字能不能给模型看。
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time

from packages.artifacts import crypto as storage_crypto

SCHEMA = "mas.memory/1"
KINDS = ("preference", "content")
PURPOSE = "memory"
ID_RE = re.compile(r"m_[0-9a-f]{32}")
MAX_ENTRIES = 200
PREFERENCE_CHARS = 200
LABEL_CHARS = 40
CONTENT_CHARS = 32000
FILE_NAME = "memory.json"
SEAL_ID = "store"


class MemoryError(Exception):
    """稳定错误码 + 给人看的说明。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def clean_label(value) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)[:LABEL_CHARS]


def clean_preference(value) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(value or "")).strip()
    return text


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def _public(entry: dict) -> dict:
    out = {
        "memory_id": entry["memory_id"],
        "kind": entry["kind"],
        "label": entry.get("label") or "",
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "source": entry.get("source") or "user",
    }
    if entry["kind"] == "preference":
        out["text"] = entry.get("text") or ""
    else:
        out.update({
            "type": "text",
            "purpose": PURPOSE,
            "origin_purpose": entry.get("origin_purpose") or "keep",
            "size": len((entry.get("text") or "").encode("utf-8")),
        })
    return out


class MemoryStore:
    """本机长期记忆。所有方法线程安全；坏文件当空、不覆盖（留给用户处理），写入用临时文件 + 替换。"""

    def __init__(self, data_dir: str, storage: "storage_crypto.Storage | None" = None):
        self.dir = os.path.join(data_dir, "memory")
        self.path = os.path.join(self.dir, FILE_NAME)
        self.storage = storage if storage is not None else storage_crypto.Storage("plaintext")
        self.mode = self.storage.mode
        self.cipher = self.storage.cipher if self.mode == "encrypted" else None
        self._lock = threading.RLock()
        self._volatile: dict | None = None  # memory 模式
        self.corrupt = False

    # ---------- 底层 ----------

    @property
    def encrypted(self) -> bool:
        return self.mode in ("encrypted", "memory")

    def _empty(self) -> dict:
        return {"schema": SCHEMA, "entries": []}

    def _load(self) -> dict:
        if self.mode == "memory":
            if self._volatile is None:
                self._volatile = self._empty()
            return copy.deepcopy(self._volatile)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            self.corrupt = False
            return self._empty()
        except (OSError, ValueError):
            self.corrupt = True
            return self._empty()
        try:
            if storage_crypto.is_envelope(raw):
                if self.cipher is None:
                    raise storage_crypto.StorageError("storage_key_missing")
                raw = self.cipher.open_json(SEAL_ID, raw, scope="memory")
            elif self.cipher is not None:
                raise storage_crypto.StorageError("storage_decrypt_failed", "not_envelope")
        except storage_crypto.StorageError:
            self.corrupt = True
            return self._empty()
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("entries"), list):
            self.corrupt = True
            return self._empty()
        self.corrupt = False
        entries = [item for item in raw["entries"] if isinstance(item, dict) and valid_id(item.get("memory_id"))
                   and item.get("kind") in KINDS]
        return {"schema": SCHEMA, "entries": entries}

    def _save(self, doc: dict) -> None:
        if self.corrupt:
            raise MemoryError("memory_corrupt", "记忆文件读不出来（坏了或密钥不对），没有覆盖它。")
        if self.mode == "memory":
            self._volatile = copy.deepcopy(doc)
            return
        payload = self.cipher.seal_json(SEAL_ID, doc, scope="memory") if self.cipher is not None else doc
        os.makedirs(self.dir, exist_ok=True)
        temp = self.path + ".part"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    def _find(self, doc: dict, memory_id: str) -> dict | None:
        if not valid_id(memory_id):
            return None
        return next((item for item in doc["entries"] if item.get("memory_id") == memory_id), None)

    # ---------- 读 ----------

    def list(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            doc = self._load()
        return [_public(item) for item in doc["entries"] if kind is None or item["kind"] == kind]

    def get(self, memory_id: str) -> dict | None:
        with self._lock:
            item = self._find(self._load(), memory_id)
        return _public(item) if item else None

    def body(self, memory_id: str) -> str | None:
        """content 条目的正文。只给宿主重新签发句柄和本机面板预览用，不进任何模型请求。"""
        with self._lock:
            item = self._find(self._load(), memory_id)
        if not item or item["kind"] != "content":
            return None
        text = item.get("text")
        return text if isinstance(text, str) else None

    def bodies(self) -> list[str]:
        """所有 content 正文：加进每个会话的出口检查（模型请求里出现就拦）。"""
        with self._lock:
            doc = self._load()
        return [item["text"] for item in doc["entries"]
                if item["kind"] == "content" and isinstance(item.get("text"), str) and item["text"]]

    def search(self, query: str = "", limit: int = 10) -> list[dict]:
        needle = (query or "").strip().lower()
        found = []
        for item in self.list():
            haystack = (item.get("label") or "") + " " + (item.get("text") or "")
            if not needle or needle in haystack.lower():
                found.append(item)
            if len(found) >= limit:
                break
        return found

    # ---------- 写 ----------

    def _append(self, entry: dict) -> dict:
        with self._lock:
            doc = self._load()
            if len(doc["entries"]) >= MAX_ENTRIES:
                raise MemoryError("memory_full", f"记忆最多 {MAX_ENTRIES} 条，先删掉一些。")
            for item in doc["entries"]:  # 同样的条目不重复记
                if item["kind"] == entry["kind"] and item.get("text") == entry.get("text"):
                    if entry.get("label") and entry["label"] != item.get("label"):
                        item["label"] = entry["label"]
                        item["updated_at"] = _now()
                        self._save(doc)
                    return _public(item)
            doc["entries"].append(entry)
            self._save(doc)
        return _public(entry)

    def add_preference(self, text: str, label: str = "", source: str = "user") -> dict:
        text = clean_preference(text)
        if not text:
            raise MemoryError("invalid_parameter", "偏好不能为空。")
        if len(text) > PREFERENCE_CHARS:
            raise MemoryError("invalid_parameter", f"偏好最多 {PREFERENCE_CHARS} 字。")
        now = _now()
        return self._append({
            "memory_id": "m_" + secrets.token_hex(16), "kind": "preference", "text": text,
            "label": clean_label(label), "source": source if source in ("user", "model") else "user",
            "created_at": now, "updated_at": now,
        })

    def add_content(self, text: str, label: str, origin_purpose: str = "keep", source: str = "model") -> dict:
        if not isinstance(text, str) or not text:
            raise MemoryError("invalid_parameter", "内容为空。")
        if len(text) > CONTENT_CHARS:
            raise MemoryError("invalid_parameter", f"内容最多 {CONTENT_CHARS} 字。")
        label = clean_label(label)
        if not label:
            raise MemoryError("invalid_parameter", "本地内容要起一个标签。")
        now = _now()
        return self._append({
            "memory_id": "m_" + secrets.token_hex(16), "kind": "content", "text": text, "label": label,
            "purpose": PURPOSE, "origin_purpose": origin_purpose if origin_purpose in ("image_prompt", "keep") else "keep",
            "source": source if source in ("user", "model") else "model", "created_at": now, "updated_at": now,
        })

    def update(self, memory_id: str, *, text=None, label=None) -> dict | None:
        with self._lock:
            doc = self._load()
            item = self._find(doc, memory_id)
            if item is None:
                return None
            if text is not None:
                if item["kind"] != "preference":
                    raise MemoryError("invalid_parameter", "本地内容条目只能改标签。")
                text = clean_preference(text)
                if not text or len(text) > PREFERENCE_CHARS:
                    raise MemoryError("invalid_parameter", f"偏好要 1–{PREFERENCE_CHARS} 字。")
                item["text"] = text
            if label is not None:
                label = clean_label(label)
                if item["kind"] == "content" and not label:
                    raise MemoryError("invalid_parameter", "本地内容要起一个标签。")
                item["label"] = label
            item["updated_at"] = _now()
            self._save(doc)
            return _public(item)

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            doc = self._load()
            item = self._find(doc, memory_id)
            if item is None:
                return False
            doc["entries"] = [entry for entry in doc["entries"] if entry is not item]
            self._save(doc)
            return True

    def export(self) -> dict:
        """导出：偏好原文 + content 的元数据（没有正文；正文只在本机加密的记忆里）。"""
        return {"schema": SCHEMA, "exported_at": _now(), "entries": self.list(), "bodies_included": False}
