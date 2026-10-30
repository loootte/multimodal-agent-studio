"""系统钥匙串（13.B #75）：第三方 API key 和 12.F 主密钥放进操作系统的凭据库。

- Windows：凭据管理器（advapi32 CredWriteW / CredReadW / CredDeleteW，ctypes 直调，不需要第三方包）。
- macOS / Linux：有 `keyring` 包且后端可用时走它（Keychain / Secret Service）；没有就判定不可用。
- 只有桌面壳（apps/desktop/host.py 设 MAS_KEYCHAIN=on）才启用；Web 模式默认 off，行为不变。
- 钥匙串只给 sidecar（Python）读；没有任何 HTTP 接口或 WebView JS 接口能取回原值，只给 hint（末 4 位）。
- 测试用后端：MAS_KEYCHAIN=memory 或 file:<路径>（只给自检/评测用，文件是明文，不要在真实环境用）。
- 不可用时退回原来的存放位置（主密钥文件 / tools.json / provider.json），status() 里标 degraded，界面和宿主提示。

条目名（不含值）：storage.master_key、provider.api_key、tool.<工具或后端名>.<字段>。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading

SERVICE = "multimodal-agent-studio"
MASTER_ENTRY = "storage.master_key"
PROVIDER_ENTRY = "provider.api_key"
_ENTRY = re.compile(r"[A-Za-z0-9_.-]{1,120}")
_MAX_SECRET = 2048  # Windows 凭据 blob 上限 2560 字节，留余量
_LOCK = threading.RLock()
_MEMORY: dict[str, str] = {}


class KeychainError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def tool_entry(tool: str, field: str) -> str:
    return f"tool.{tool}.{field}"


def requested() -> str:
    """off / system / memory / file。不认识的值按 off 处理（不悄悄写别处）。"""
    raw = (os.environ.get("MAS_KEYCHAIN") or "").strip()
    low = raw.lower()
    if low in ("on", "1", "true", "yes", "system"):
        return "system"
    if low == "memory":
        return "memory"
    if low.startswith("file:") and len(raw) > 5:
        return "file"
    return "off"


def _namespace() -> str:
    """凭据名前缀。默认 multimodal-agent-studio；冒烟测试可用 MAS_KEYCHAIN_NAMESPACE 隔开，避免碰真实条目。"""
    raw = (os.environ.get("MAS_KEYCHAIN_NAMESPACE") or "").strip()
    return raw if raw and _ENTRY.fullmatch(raw) else SERVICE


def _check_entry(entry: str) -> str:
    if not isinstance(entry, str) or not _ENTRY.fullmatch(entry):
        raise KeychainError("keychain_entry_invalid", "条目名不合法。")
    return entry


# ---------- 后端 ----------

class _Memory:
    name = "memory"

    def get(self, entry):
        return _MEMORY.get(entry)

    def set(self, entry, value):
        _MEMORY[entry] = value

    def delete(self, entry):
        return _MEMORY.pop(entry, None) is not None

    def entries(self):
        return sorted(_MEMORY)


class _File:
    """测试用：JSON 文件（0600）。"""
    name = "file"

    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self.path)

    def get(self, entry):
        value = self._load().get(entry)
        return value if isinstance(value, str) else None

    def set(self, entry, value):
        data = self._load()
        data[entry] = value
        self._save(data)

    def delete(self, entry):
        data = self._load()
        found = data.pop(entry, None) is not None
        if found:
            self._save(data)
        return found

    def entries(self):
        return sorted(self._load())


class _WinCred:
    """Windows 凭据管理器（通用凭据，CRED_PERSIST_LOCAL_MACHINE）。目标名 <namespace>/<entry>。"""
    name = "windows_credential_manager"

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        self.ctypes = ctypes
        self.CREDENTIAL = CREDENTIAL
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.CredWriteW = adv.CredWriteW
        self.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.CredWriteW.restype = wintypes.BOOL
        self.CredReadW = adv.CredReadW
        self.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.CredReadW.restype = wintypes.BOOL
        self.CredDeleteW = adv.CredDeleteW
        self.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.CredDeleteW.restype = wintypes.BOOL
        self.CredEnumerateW = adv.CredEnumerateW
        self.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                        ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))]
        self.CredEnumerateW.restype = wintypes.BOOL
        self.CredFree = adv.CredFree
        self.CredFree.argtypes = [ctypes.c_void_p]
        self.CredFree.restype = None

    def _target(self, entry):
        return f"{_namespace()}/{entry}"

    def get(self, entry):
        ctypes = self.ctypes
        pointer = ctypes.POINTER(self.CREDENTIAL)()
        if not self.CredReadW(self._target(entry), 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
                return None
            raise KeychainError("keychain_read_failed", f"CredReadW 失败（{ctypes.get_last_error()}）。")
        try:
            cred = pointer.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        finally:
            self.CredFree(pointer)
        return raw.decode("utf-8")

    def set(self, entry, value):
        ctypes = self.ctypes
        blob = value.encode("utf-8")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self.CREDENTIAL()
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = self._target(entry)
        cred.Comment = "Multimodal Agent Studio"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "mas"
        if not self.CredWriteW(ctypes.byref(cred), 0):
            raise KeychainError("keychain_write_failed", f"CredWriteW 失败（{ctypes.get_last_error()}）。")

    def delete(self, entry):
        ctypes = self.ctypes
        if self.CredDeleteW(self._target(entry), 1, 0):
            return True
        if ctypes.get_last_error() == 1168:
            return False
        raise KeychainError("keychain_delete_failed", f"CredDeleteW 失败（{ctypes.get_last_error()}）。")

    def entries(self):
        ctypes = self.ctypes
        from ctypes import wintypes

        count = wintypes.DWORD()
        array = ctypes.POINTER(ctypes.POINTER(self.CREDENTIAL))()
        prefix = _namespace() + "/"
        if not self.CredEnumerateW(prefix + "*", 0, ctypes.byref(count), ctypes.byref(array)):
            return []
        try:
            names = [array[i].contents.TargetName for i in range(count.value)]
        finally:
            self.CredFree(array)
        return sorted(name[len(prefix):] for name in names if name and name.startswith(prefix))


class _Keyring:
    """macOS Keychain / Linux Secret Service，经 keyring 包。"""

    def __init__(self, module):
        self.keyring = module
        self.name = "keyring:" + type(module.get_keyring()).__name__

    def get(self, entry):
        return self.keyring.get_password(_namespace(), entry)

    def set(self, entry, value):
        self.keyring.set_password(_namespace(), entry, value)

    def delete(self, entry):
        try:
            self.keyring.delete_password(_namespace(), entry)
            return True
        except Exception:
            return False

    def entries(self):
        return []  # keyring 没有通用的列举接口


def _system_backend():
    """返回 (后端, 不可用原因)。"""
    if sys.platform == "win32":
        try:
            return _WinCred(), None
        except Exception as exc:  # pragma: no cover - 只在坏掉的 Windows 上
            return None, f"windows_credential_manager_unavailable:{type(exc).__name__}"
    try:
        import keyring  # type: ignore
    except Exception:
        return None, "keyring_not_installed"
    try:
        current = keyring.get_keyring()
        module = type(current).__module__
        if module.startswith(("keyring.backends.fail", "keyring.backends.null")) or \
                type(current).__name__ in ("ChainerBackend",) and not getattr(current, "backends", None):
            return None, "no_keyring_backend"
    except Exception:
        return None, "no_keyring_backend"
    return _Keyring(keyring), None


def _backend():
    """当前启用的后端；off 或不可用时 None。"""
    mode = requested()
    if mode == "off":
        return None, "off"
    if mode == "memory":
        return _Memory(), None
    if mode == "file":
        return _File((os.environ.get("MAS_KEYCHAIN") or "")[5:]), None
    return _system_backend()


def enabled() -> bool:
    return requested() != "off"


def available() -> bool:
    backend, _reason = _backend()
    return backend is not None


def status() -> dict:
    """给 /api/storage 和宿主看：只有开关、后端名和不可用原因，不含值、不含条目名单。"""
    mode = requested()
    if mode == "off":
        return {"enabled": False, "available": False, "backend": None, "degraded": False, "reason": None}
    backend, reason = _backend()
    return {"enabled": True, "available": backend is not None, "backend": backend.name if backend else None,
            "degraded": backend is None, "reason": reason}


def get(entry: str) -> str | None:
    """读一个条目（只给 sidecar 内部用）。未启用 / 不可用 / 没有都返回 None。"""
    _check_entry(entry)
    backend, _ = _backend()
    if backend is None:
        return None
    with _LOCK:
        try:
            value = backend.get(entry)
        except KeychainError:
            raise
        except Exception as exc:
            raise KeychainError("keychain_read_failed", type(exc).__name__) from exc
    return value if value else None


def set(entry: str, value: str) -> None:  # noqa: A001 - 和 keyring 的命名一致
    _check_entry(entry)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_SECRET:
        raise KeychainError("keychain_value_invalid", "值为空或太长。")
    backend, reason = _backend()
    if backend is None:
        raise KeychainError("keychain_unavailable", reason or "off")
    with _LOCK:
        try:
            backend.set(entry, value)
            back = backend.get(entry)
        except KeychainError:
            raise
        except Exception as exc:
            raise KeychainError("keychain_write_failed", type(exc).__name__) from exc
    if back != value:
        raise KeychainError("keychain_write_failed", "写入后读回不一致。")


def delete(entry: str) -> bool:
    _check_entry(entry)
    backend, reason = _backend()
    if backend is None:
        raise KeychainError("keychain_unavailable", reason or "off")
    with _LOCK:
        try:
            return bool(backend.delete(entry))
        except KeychainError:
            raise
        except Exception as exc:
            raise KeychainError("keychain_delete_failed", type(exc).__name__) from exc


def entries() -> list[str]:
    """条目名（不含值），给命令行 status 用。"""
    backend, _ = _backend()
    if backend is None:
        return []
    try:
        return list(backend.entries())
    except Exception:
        return []


def active() -> bool:
    """启用且可用：写入应当进钥匙串。"""
    return enabled() and available()


def reset_memory() -> None:
    _MEMORY.clear()
