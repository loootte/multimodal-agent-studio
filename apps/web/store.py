"""本机会话和供应商设置。

密钥只出现在 provider.json。会话记录里没有密钥。
"""

from __future__ import annotations

import base64
import calendar
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
from packages import keychain
from packages.artifacts import crypto as storage_crypto
from packages.contracts.events import HANDLE_ERRORS, HANDLE_PURPOSE_TOOLS, HANDLE_SCHEMA
from services.gateway.egress import LocalBodies
from services.runtime import injection
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
# 12.B（#58）：本地内容句柄的默认有效期。环境变量 MAS_HANDLE_TTL_SEC 可改，限定在 60 秒到 7 天。
HANDLE_TTL_DEFAULT = 4 * 3600
HANDLE_TTL_MIN = 60
HANDLE_TTL_MAX = 7 * 86400
# 宿主可以额外放开的消费工具（例如「定时出图」里用户收下的 keep 文字）。模型不能自己放开。
_GRANTABLE = ("generate_image",)
# 评测和检查可以替换时钟。
clock = time.time


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


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _epoch(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        return None


def handle_ttl() -> int:
    """句柄有效期（秒）。坏值回到默认值，越界的夹到范围内。"""
    raw = os.environ.get("MAS_HANDLE_TTL_SEC")
    try:
        value = int(raw) if raw not in (None, "") else HANDLE_TTL_DEFAULT
    except ValueError:
        value = HANDLE_TTL_DEFAULT
    return max(HANDLE_TTL_MIN, min(HANDLE_TTL_MAX, value))


def _purpose_of(record: dict) -> str:
    purpose = record.get("purpose")
    if purpose in _PURPOSES:
        return purpose
    return "image_prompt" if record.get("message_id") else "keep"


def _scope(record: dict, session_id: str, *, run_id: str | None = None, allowed_tools=(), single_use: bool = False) -> dict:
    """给一条本地内容记录签发能力令牌字段。正文和映射只在本机记录里，令牌本身是随机串。"""
    now = clock()
    purpose = _purpose_of(record)
    scope = {"session_id": session_id, "purpose": purpose}
    if run_id:
        scope["run_id"] = run_id
    record["handle_schema"] = HANDLE_SCHEMA
    record["scope"] = scope
    tools = set(HANDLE_PURPOSE_TOOLS.get(purpose, ()))
    tools.update(tool for tool in allowed_tools or () if tool in _GRANTABLE)
    record["allowed_tools"] = sorted(tools)
    record["issued_at"] = _iso(now)
    record["expires_at"] = _iso(now + handle_ttl())
    if single_use:
        record["single_use"] = True
    else:
        record.pop("single_use", None)
    record.pop("revoked_at", None)
    record.pop("used_at", None)
    return record


def handle_state(record: dict, now: float | None = None) -> str:
    """active / expired / revoked / used。旧记录（没有 expires_at）读时升级前算 active。"""
    if record.get("revoked_at"):
        return "revoked"
    if record.get("single_use") and record.get("used_at"):
        return "used"
    expires = _epoch(record.get("expires_at"))
    if record.get("expires_at") is not None and expires is None:
        return "expired"
    if expires is not None and (clock() if now is None else now) >= expires:
        return "expired"
    return "active"


def _check_handle(record: dict, session_id: str, tool: str | None, run_id: str | None) -> str:
    scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
    if scope.get("session_id") != session_id:
        return "handle_not_found"
    if handle_state(record) != "active":
        return "handle_expired"
    bound = scope.get("run_id")
    if bound and bound != run_id:
        return "handle_forbidden"
    if tool is not None and tool not in (record.get("allowed_tools") or []):
        return "handle_forbidden"
    return "ok"


def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def _keychain_get(entry: str) -> str | None:
    """读钥匙串；读失败当作没有（不把错误细节带进接口）。"""
    try:
        return keychain.get(entry)
    except keychain.KeychainError:
        return None


class ContextSettingsStore:
    """14.B（#79）上下文压缩设置（context.json）。读的时候逐项校验，非法项回退默认值；文件坏了整个用默认值。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def _saved(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return "corrupt"

    def current(self) -> dict:
        from services.runtime import compaction

        with self._lock:
            raw = self._saved()
        values, _warnings = compaction.settings(None if raw == "corrupt" else raw)
        return values

    def public(self) -> dict:
        from services.runtime import compaction

        with self._lock:
            raw = self._saved()
        values, warnings = compaction.settings(None if raw == "corrupt" else raw)
        if raw == "corrupt":
            warnings = ["*"]
        return {"values": values, "defaults": dict(compaction.DEFAULTS), "warnings": warnings}

    def update(self, body, reset: bool = False) -> dict:
        """只存校验通过的项（和已存的合并）；非法项不存，返回里列进 warnings，生效值是默认值。"""
        from services.runtime import compaction

        if not isinstance(body, dict):
            body = {}
        with self._lock:
            raw = self._saved()
            saved = raw if isinstance(raw, dict) and not reset else {}
            values, warnings = compaction.settings(body)
            accepted = {key: values[key] for key in body if key in compaction.DEFAULTS and key not in warnings}
            cleared = [key for key in body if key in compaction.DEFAULTS and body[key] in (None, "")]
            merged = {key: value for key, value in saved.items() if key in compaction.DEFAULTS and key not in cleared}
            merged.update(accepted)
            merged, _ = compaction.settings(merged)
            stored = {key: value for key, value in merged.items() if value != compaction.DEFAULTS[key]}
            _atomic_write(self.path, stored)
        view = self.public()
        view["warnings"] = sorted(set(view["warnings"]) | set(warnings))
        return view


class ProviderStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    def resolve(self) -> dict:
        with self._lock:
            data = self._read()
        if keychain.enabled():
            # 13.B 桌面壳：环境变量 > 系统钥匙串 > provider.json（旧值 / 钥匙串不可用时的退路）
            key, source = _env_key(data["base_url"])
            if not key:
                key = _usable(_keychain_get(keychain.PROVIDER_ENTRY) or "")
                source = "keychain" if key else None
            if not key:
                key = _usable(data.get("api_key") or "")
                source = "file" if key else None
        else:
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
        injection.refuse_config_write("provider")  # 12.G：配置只走设置 API，工具执行期间不能改
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
            if keychain.active() and (clear_key or api_key):
                # 13.B：新 key 进系统钥匙串，provider.json 里不留原值；清除时两边都清。
                try:
                    if clear_key:
                        keychain.delete(keychain.PROVIDER_ENTRY)
                    else:
                        keychain.set(keychain.PROVIDER_ENTRY, key)
                except keychain.KeychainError as exc:
                    raise ValueError(f"写系统钥匙串失败（{exc.code}），API key 没有保存。") from exc
                key = ""
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
    """公开字段：模型和页面都只见这些。没有正文、会话映射和运行绑定。"""
    kind = _kind_of(record)
    media = record.get("media_type")
    if not isinstance(media, str) or not media:
        media = "text/plain" if kind == "text" else "application/octet-stream"
    return {
        "handle": record.get("handle"),
        "type": kind,
        "kind": kind,
        "media_type": media,
        "size": record.get("size"),
        "purpose": _purpose_of(record),
        "expires_at": record.get("expires_at"),
        "state": handle_state(record),
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
    """会话记录。12.F（#62）：storage 决定落盘方式。

    - plaintext：和以前一样，`sessions/<id>.json` + 全局按 sha256 去重的 `artifacts/blobs/`。
    - encrypted：会话文件是 `mas.enc/1` 信封（AES-256-GCM，会话派生密钥，AAD 绑会话 id）；
      内容 / 附件字节进 `artifacts/sealed/<会话>/<HMAC 名>`，按会话加密，不跨会话去重。
    - memory：「仅内存会话」，不写 `sessions/` 和 `artifacts/`，重启即没。
    """

    def __init__(self, root: str, artifacts: ArtifactStore | None = None,
                 storage: "storage_crypto.Storage | None" = None):
        self.root = root
        parent = os.path.dirname(os.path.abspath(root))
        self.artifacts = artifacts if artifacts is not None else ArtifactStore(os.path.join(parent, "artifacts"))
        self.storage = storage if storage is not None else storage_crypto.Storage("plaintext")
        self.cipher = self.storage.cipher if self.storage.mode == "encrypted" else None
        self.memory = self.storage.mode == "memory"
        self._mem: dict[str, str] = {}
        self._mem_blobs: dict[tuple[str, str], bytes] = {}
        self.decrypt_failures = 0
        self._lock = threading.RLock()
        if not self.memory:
            os.makedirs(root, exist_ok=True)
        self._recover()

    # ---------- 12.F 落盘（#62） ----------

    def _names(self) -> list[str]:
        if self.memory:
            return [sid + ".json" for sid in self._mem]
        try:
            return os.listdir(self.root)
        except OSError:
            return []

    def _sealed_dir(self, session_id: str) -> str:
        return os.path.join(self.artifacts.root, "sealed", session_id)

    def _put_blob(self, session_id: str, checksum: str, data: bytes) -> None:
        if self.memory:
            self._mem_blobs[(session_id, checksum)] = bytes(data)
            return
        if self.cipher is None:
            self.artifacts.put(data)
            return
        target = os.path.join(self._sealed_dir(session_id), self.cipher.blob_name(session_id, checksum))
        if os.path.isfile(target):
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temporary = target + ".part"
        try:
            with open(temporary, "wb") as handle:
                handle.write(self.cipher.seal_blob(session_id, checksum, data))
            os.replace(temporary, target)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise ArtifactError("没能留下这个文件。") from exc

    def _get_blob(self, session_id: str, checksum: str) -> bytes | None:
        if self.memory:
            return self._mem_blobs.get((session_id, checksum))
        if self.cipher is not None:
            target = os.path.join(self._sealed_dir(session_id), self.cipher.blob_name(session_id, checksum))
            if os.path.isfile(target):
                try:
                    with open(target, "rb") as handle:
                        raw = handle.read()
                    return self.cipher.open_blob(session_id, checksum, raw)
                except (OSError, storage_crypto.StorageError) as exc:
                    self._decrypt_failed(session_id, getattr(exc, "detail", "") or "io")
                    return None
        # 明文（或迁移前留下的）全局字节
        return self.artifacts.get(checksum)

    def _decrypt_failed(self, session_id: str, reason: str) -> None:
        self.decrypt_failures += 1
        try:
            from services import telemetry

            telemetry.record("security.storage_decrypt_failed", trace_id="sto_" + uuid.uuid4().hex,
                             session_id=session_id, reason_code=str(reason or "tampered")[:32])
        except Exception:
            pass

    def blob_bytes(self, session_id: str, checksum: str) -> bytes | None:
        """按会话读字节（导出轨迹用）。加密时用会话密钥解。"""
        if not isinstance(checksum, str) or not checksum:
            return None
        return self._get_blob(session_id, checksum)

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
            for name in self._names():
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
            if self.memory:
                self._mem.pop(session_id, None)
                removed = 0
                for key in [k for k in self._mem_blobs if k[0] == session_id]:
                    self._mem_blobs.pop(key, None)
                    removed += 1
                return {"ok": True, "session_id": session_id, "blobs_removed": removed}
            os.remove(self._path(session_id))
            shutil.rmtree(os.path.join(self.root, session_id), ignore_errors=True)
            sealed = 0
            if self.cipher is not None:
                folder = self._sealed_dir(session_id)
                if os.path.isdir(folder):
                    sealed = len([n for n in os.listdir(folder) if not n.endswith(".part")])
                shutil.rmtree(folder, ignore_errors=True)
            removed = self._gc_blobs(checksums) + sealed
        return {"ok": True, "session_id": session_id, "blobs_removed": removed}

    def _gc_blobs(self, checksums: set, session_id: str | None = None) -> int:
        """删掉没有任何会话再引用的字节。调用方持锁。
        加密 / 仅内存时字节按会话存：只看这个会话自己还引用不引用。"""
        checksums = {c for c in checksums if isinstance(c, str) and c}
        if not checksums:
            return 0
        if session_id and (self.memory or self.cipher is not None):
            current = self._load_path(self._path(session_id)) or {}
            mine = set(checksums)
            for key in ("attachments", "contents"):
                for item in current.get(key) or []:
                    if isinstance(item, dict):
                        mine.discard(item.get("checksum"))
            dropped = 0
            for checksum in mine:
                if self.memory:
                    dropped += 1 if self._mem_blobs.pop((session_id, checksum), None) is not None else 0
                    continue
                target = os.path.join(self._sealed_dir(session_id), self.cipher.blob_name(session_id, checksum))
                if os.path.exists(target):
                    try:
                        os.remove(target)
                        dropped += 1
                    except OSError:
                        pass
            if self.memory:
                return dropped
            return dropped + self._gc_blobs(mine)
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
            for name in sorted(self._names()):
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
            self._gc_blobs({found[0].get("checksum")}, session_id)
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

    def append_egress(self, session_id: str, message_id: str, event: dict) -> None:
        """出站审计记录（#57）单独存一列，不混进 events，事件回放顺序不变。记录里没有正文。"""
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return
            for message in session["messages"]:
                if message.get("message_id") == message_id:
                    message.setdefault("egress", []).append(dict(event))
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
                    if self._renew_if_dead(session, item, session_id):
                        self._write(session)
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
            _scope(record, session_id)
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
                    if self._renew_if_dead(session, item, session_id):
                        self._write(session)
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
            _scope(record, session_id)
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
                    if self._renew_if_dead(session, item, session_id):
                        self._write(session)
                    return _public_content(item)
            try:
                self._put_blob(session_id, digest, data)
            except (ArtifactError, storage_crypto.StorageError) as exc:
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
            _scope(record, session_id)
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
                self._put_blob(session_id, digest, data)
            except (ArtifactError, storage_crypto.StorageError) as exc:
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
                self._gc_blobs({found.get("checksum")}, session_id)
        if kind == "file":
            try:
                os.remove(self._file_path(session_id, handle))
            except OSError:
                pass
        return "forgotten"

    # ---------- 12.B 作用域句柄（#58） ----------

    def _renew_if_dead(self, session: dict, item: dict, session_id: str) -> bool:
        """同一正文再次入库时：句柄还有效就复用；过期、撤销或用过就换一枚新令牌（旧串作废），不复活旧串。
        旧记录（没有 expires_at）就地签发作用域，句柄串不变（双读一期）。返回是否改了记录。"""
        if not item.get("expires_at"):
            _scope(item, session_id)
            item["migrated_at"] = _now()
            return True
        if handle_state(item) == "active":
            return False
        old = item.get("handle")
        fresh = new_id("c_")
        if _kind_of(item) == "file" and not item.get("checksum") and isinstance(old, str):
            try:
                os.replace(self._file_path(session_id, old), self._file_path(session_id, fresh))
            except OSError:
                pass
        tools = [tool for tool in item.get("allowed_tools") or [] if tool in _GRANTABLE]
        item["handle"] = fresh
        _scope(item, session_id, allowed_tools=tools)
        session["updated_at"] = _now()
        return True

    def resolve_content(self, session_id: str, handle: str, *, tool: str | None = None,
                        run_id: str | None = None, consume: bool = True) -> tuple[dict | None, str]:
        """宿主消费句柄的唯一入口。校验会话、有效期、撤销、运行绑定、用途允许的工具。
        返回 (本机记录副本, "ok") 或 (None, 错误码)。错误码只有 HANDLE_ERRORS 里三个。
        跨会话和伪造的句柄都是 handle_not_found，不暴露它在别处存在。旧记录读时升级。"""
        if not isinstance(handle, str) or not _ID.fullmatch(handle) or not handle.startswith("c_"):
            return None, "handle_not_found"
        try:
            check_id(session_id, "会话")
        except StoreError:
            return None, "handle_not_found"
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return None, "handle_not_found"
            item = next((i for i in session.get("contents") or []
                         if isinstance(i, dict) and i.get("handle") == handle), None)
            if item is None:
                return None, "handle_not_found"
            changed = False
            if not item.get("expires_at"):
                _scope(item, session_id)
                item["migrated_at"] = _now()
                changed = True
            code = _check_handle(item, session_id, tool, run_id)
            if code == "ok" and consume and item.get("single_use"):
                item["used_at"] = _now()
                changed = True
            if changed:
                self._write(session)
            if code != "ok":
                return None, code
            return dict(item), "ok"

    def revoke_content(self, session_id: str, handle: str) -> str:
        """撤销一枚句柄：正文还在本机（资源面板能看、能复用成新句柄），但这个串再也不能消费。"""
        if not isinstance(handle, str) or not _ID.fullmatch(handle):
            return "absent"
        try:
            check_id(session_id, "会话")
        except StoreError:
            return "absent"
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return "absent"
            for item in session.get("contents") or []:
                if isinstance(item, dict) and item.get("handle") == handle:
                    if not item.get("expires_at"):
                        _scope(item, session_id)
                    if item.get("revoked_at"):
                        return "revoked"
                    item["revoked_at"] = _now()
                    session["updated_at"] = _now()
                    self._write(session)
                    return "revoked"
        return "absent"

    def revoke_run(self, session_id: str, run_id: str) -> int:
        """运行结束时作废绑定到这次运行的句柄。返回作废几枚。"""
        try:
            check_id(session_id, "会话")
        except StoreError:
            return 0
        count = 0
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return 0
            for item in session.get("contents") or []:
                scope = item.get("scope") if isinstance(item, dict) else None
                if isinstance(scope, dict) and scope.get("run_id") == run_id and not item.get("revoked_at"):
                    item["revoked_at"] = _now()
                    count += 1
            if count:
                session["updated_at"] = _now()
                self._write(session)
        return count

    def allow_tool(self, session_id: str, handle: str, tool: str) -> bool:
        """宿主按用户意图放开一个消费工具（只限 _GRANTABLE）。模型没有这个入口。"""
        if tool not in _GRANTABLE or not isinstance(handle, str) or not _ID.fullmatch(handle):
            return False
        try:
            check_id(session_id, "会话")
        except StoreError:
            return False
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return False
            for item in session.get("contents") or []:
                if isinstance(item, dict) and item.get("handle") == handle:
                    if not item.get("expires_at"):
                        _scope(item, session_id)
                    tools = set(item.get("allowed_tools") or [])
                    if tool not in tools:
                        item["allowed_tools"] = sorted(tools | {tool})
                        self._write(session)
                    return True
        return False

    def bind_run(self, session_id: str, handle: str, run_id: str, *, single_use: bool = False) -> bool:
        """把一枚句柄收窄到某次运行（可选只能用一次）。只能收窄，不能放宽。"""
        if not isinstance(handle, str) or not _ID.fullmatch(handle) or not isinstance(run_id, str) or not run_id:
            return False
        try:
            check_id(session_id, "会话")
        except StoreError:
            return False
        with self._lock:
            session = self._load_path(self._path(session_id))
            if session is None:
                return False
            for item in session.get("contents") or []:
                if isinstance(item, dict) and item.get("handle") == handle:
                    if not item.get("expires_at"):
                        _scope(item, session_id)
                    scope = dict(item.get("scope") or {})
                    if scope.get("run_id") and scope["run_id"] != run_id:
                        return False
                    scope["run_id"] = run_id
                    item["scope"] = scope
                    if single_use:
                        item["single_use"] = True
                    self._write(session)
                    return True
        return False

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
        """本会话已登记的本地正文；附带「已入库指纹」（12.G #63），出口中间件据此禁止指纹外传。"""
        bodies = [text for text, _handle in self.secret_pairs(session_id)]
        return LocalBodies(bodies, self.stored_fingerprints(session_id))

    def stored_fingerprints(self, session_id: str) -> list[str]:
        """入库时记下的 sha256（文件字节 checksum、文本正文 sha256）。只给出口检查用，不进任何对外视图。"""
        try:
            session = self.get(session_id)
        except StoreError:
            return []
        found = []
        for item in session.get("contents") or []:
            if not isinstance(item, dict):
                continue
            checksum = item.get("checksum")
            if isinstance(checksum, str) and checksum:
                found.append(checksum)
            text = item.get("text")
            if isinstance(text, str) and text:
                found.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
        return found

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
            data = self._get_blob(session_id, checksum)
            if data is not None:
                return data
        if self.memory:
            return None
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
            for name in self._names():
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
        session_id = os.path.basename(path)[:-5] if path.endswith(".json") else ""
        if self.memory:
            raw = self._mem.get(session_id)
            data = json.loads(raw) if raw is not None else None
        else:
            if not os.path.exists(path):
                return None
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return None
            if storage_crypto.is_envelope(data):
                if self.cipher is None:
                    return None  # 明文模式读不了密文（不会悄悄解成明文）
                try:
                    data = self.cipher.open_json(session_id, data)
                except storage_crypto.StorageError as exc:
                    self._decrypt_failed(session_id, exc.detail)
                    return None
                if data.get("session_id") != session_id:
                    self._decrypt_failed(session_id, "wrong_scope")
                    return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        return data

    def _write(self, session: dict) -> None:
        session_id = session["session_id"]
        if self.memory:
            check_id(session_id, "会话")
            self._mem[session_id] = json.dumps(session, ensure_ascii=False)
            return
        if self.cipher is not None:
            _atomic_write(self._path(session_id), self.cipher.seal_json(session_id, session))
            return
        _atomic_write(self._path(session_id), session)
