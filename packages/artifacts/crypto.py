"""本机落盘加密（12.F #62）。

- 会话记录（`sessions/<id>.json`，含 `contents` 正文、消息、记忆）和内容 / 附件字节都用 AES-256-GCM 加密。
- 每个会话一把派生密钥：HKDF-SHA256(主密钥, info="mas/session/<会话 id>")。附加数据（AAD）绑定会话 id，
  把 A 会话的密文挪到 B 会话名下一样解不开；会话密钥只在内存里，不落盘。
- 主密钥 32 字节随机数，放在数据目录和仓库之外（默认 `~/.mas/master.key`，或 `MAS_MASTER_KEY_FILE`，
  或环境变量 `MAS_MASTER_KEY`）。拷走 `data/` 没有主密钥就读不出正文。
- 备份：主密钥用口令（scrypt）包一层导出，丢了主密钥和备份数据就不可恢复。
- 不写任何正文、密钥、完整路径到日志或接口。

AES-GCM 来自 `cryptography`（只有打开加密时才需要）。没装就拒绝启动加密，绝不悄悄退回明文。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

ALG = "AES-256-GCM"
ENVELOPE_SCHEMA = "mas.enc/1"
STORAGE_SCHEMA = "mas.storage/1"
KEY_SCHEMA = "mas.masterkey/1"
BACKUP_SCHEMA = "mas.keybackup/1"
BLOB_MAGIC = b"MASE\x01"
MARKER_NAME = "storage.json"
KEY_BYTES = 32
NONCE_BYTES = 12
# 口令包装主密钥：scrypt 参数（约 64 MiB 内存）。
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
PASSPHRASE_MIN = 12
MODES = ("plaintext", "encrypted", "memory")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 固定错误码（白话说明）。接口和命令行只回这些码和说明，不回路径、密钥或正文。
STORAGE_ERRORS = {
    "storage_crypto_unavailable": "打开了落盘加密，但没装 cryptography（pip install cryptography）。不会退回明文。",
    "storage_key_missing": "数据目录已加密，但找不到主密钥。用备份导入主密钥后再启动（apps/web/storage_tool.py import-key）。",
    "storage_key_mismatch": "主密钥和数据目录不匹配（key_id 不同）。换回原来的主密钥，或从备份导入。",
    "storage_key_invalid": "主密钥格式不对（需要 32 字节，base64 编码）。",
    "storage_key_location": "主密钥不能放在数据目录或仓库里，否则拷走 data/ 或提交 Git 就等于连钥匙一起给出去。",
    "storage_key_exists": "主密钥已经存在，不覆盖。",
    "storage_migration_required": "数据目录里还有明文会话或文件。先运行 apps/web/storage_tool.py encrypt 迁移，再打开加密启动。",
    "storage_decrypt_failed": "密文解不开：主密钥不对、文件被改过，或密文不属于这个会话。",
    "storage_backup_invalid": "备份文件或口令不对。",
    "storage_passphrase_weak": f"备份口令至少 {PASSPHRASE_MIN} 个字符。",
    "storage_mode_invalid": "MAS_STORAGE_ENCRYPTION 只认 off / on / memory。",
    "storage_keychain_failed": "系统钥匙串读写失败。检查凭据管理器 / 钥匙串是否可用，或设 MAS_KEYCHAIN=off 改用主密钥文件。",
}


class StorageError(Exception):
    def __init__(self, code: str, detail: str = ""):
        message = STORAGE_ERRORS.get(code, code)
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("not str")
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def crypto_available() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception:
        return False
    return True


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:
        raise StorageError("storage_crypto_unavailable") from exc
    return AESGCM(key)


def hkdf(master: bytes, info: bytes, length: int = 32, salt: bytes = b"mas.storage/1") -> bytes:
    """RFC 5869 HKDF-SHA256（标准库实现）。"""
    prk = hmac.new(salt, master, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def key_id_of(master: bytes) -> str:
    """主密钥的公开指纹（不可逆），用来核对数据目录和密钥是否配套。"""
    return hmac.new(master, b"mas/key-id", hashlib.sha256).hexdigest()[:16]


SCOPES = ("session", "memory")  # 派生密钥的作用域（14.D #81 加 memory）


class Cipher:
    """按会话派生密钥的 AES-256-GCM。会话密钥只在内存里。"""

    def __init__(self, master: bytes):
        if not isinstance(master, bytes) or len(master) != KEY_BYTES:
            raise StorageError("storage_key_invalid")
        _aesgcm(master)  # 早失败：没装 cryptography 时在启动时就报
        self._master = master
        self.key_id = key_id_of(master)
        self._cache: dict[str, bytes] = {}

    def _session_key(self, session_id: str, scope: str = "session") -> bytes:
        if not isinstance(session_id, str) or not session_id or scope not in SCOPES:
            raise StorageError("storage_decrypt_failed", "bad_scope")
        cached = session_id if scope == "session" else scope + "/" + session_id
        key = self._cache.get(cached)
        if key is None:
            key = hkdf(self._master, b"mas/" + scope.encode("ascii") + b"/" + session_id.encode("ascii"))
            self._cache[cached] = key
        return key

    def _header(self, session_id: str, scope: str = "session") -> dict:
        return {"schema": ENVELOPE_SCHEMA, "alg": ALG, "key_id": self.key_id, "scope": scope,
                "session_id": session_id}

    @staticmethod
    def _aad(header: dict) -> bytes:
        return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def seal_json(self, session_id: str, payload: dict, scope: str = "session") -> dict:
        """scope=session：会话记录；scope=memory（14.D #81）：长期记忆，用 mas/memory/<id> 派生的独立密钥。"""
        header = self._header(session_id, scope)
        nonce = secrets.token_bytes(NONCE_BYTES)
        plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sealed = _aesgcm(self._session_key(session_id, scope)).encrypt(nonce, plain, self._aad(header))
        return dict(header, nonce=_b64e(nonce), ct=_b64e(sealed))

    def open_json(self, session_id: str, envelope: dict, scope: str = "session") -> dict:
        if not is_envelope(envelope):
            raise StorageError("storage_decrypt_failed", "not_envelope")
        if envelope.get("key_id") != self.key_id:
            raise StorageError("storage_decrypt_failed", "wrong_key")
        if envelope.get("session_id") != session_id or envelope.get("scope") != scope:
            raise StorageError("storage_decrypt_failed", "wrong_scope")
        header = self._header(session_id, scope)
        try:
            plain = _aesgcm(self._session_key(session_id, scope)).decrypt(
                _b64d(envelope.get("nonce")), _b64d(envelope.get("ct")), self._aad(header))
            data = json.loads(plain.decode("utf-8"))
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("storage_decrypt_failed", "tampered") from exc
        if not isinstance(data, dict):
            raise StorageError("storage_decrypt_failed", "tampered")
        return data

    def _blob_aad(self, session_id: str, checksum: str) -> bytes:
        return b"|".join((b"mas.blob/1", self.key_id.encode(), session_id.encode(), checksum.encode()))

    def seal_blob(self, session_id: str, checksum: str, data: bytes) -> bytes:
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = _aesgcm(self._session_key(session_id)).encrypt(nonce, data, self._blob_aad(session_id, checksum))
        return BLOB_MAGIC + nonce + sealed

    def open_blob(self, session_id: str, checksum: str, raw: bytes) -> bytes:
        if not isinstance(raw, bytes) or not raw.startswith(BLOB_MAGIC):
            raise StorageError("storage_decrypt_failed", "not_envelope")
        body = raw[len(BLOB_MAGIC):]
        try:
            data = _aesgcm(self._session_key(session_id)).decrypt(
                body[:NONCE_BYTES], body[NONCE_BYTES:], self._blob_aad(session_id, checksum))
        except Exception as exc:
            raise StorageError("storage_decrypt_failed", "tampered") from exc
        if hashlib.sha256(data).hexdigest() != checksum:
            raise StorageError("storage_decrypt_failed", "tampered")
        return data

    def blob_name(self, session_id: str, checksum: str) -> str:
        """密文文件名：会话密钥上的 HMAC，不暴露明文的 sha256（防「猜文件 → 比对哈希」）。"""
        name_key = hkdf(self._session_key(session_id), b"mas/blob-name")
        return hmac.new(name_key, checksum.encode("ascii"), hashlib.sha256).hexdigest()[:48]


def is_envelope(data) -> bool:
    return isinstance(data, dict) and data.get("schema") == ENVELOPE_SCHEMA


# ---------- 主密钥 ----------

def _under(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    root = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def default_key_path() -> str:
    custom = os.environ.get("MAS_MASTER_KEY_FILE")
    if custom:
        return os.path.abspath(os.path.expanduser(custom))
    return os.path.join(os.path.expanduser("~"), ".mas", "master.key")


def key_dirs() -> list[str]:
    """放主密钥的目录。12.D 沙箱把它们和数据目录一样列为禁读。"""
    found = [os.path.dirname(default_key_path()), os.path.join(os.path.expanduser("~"), ".mas")]
    out = []
    for path in found:
        full = os.path.realpath(os.path.abspath(path))
        if full not in out:
            out.append(full)
    return out


def check_key_location(path: str, data_dir: str | None) -> None:
    if data_dir and _under(path, data_dir):
        raise StorageError("storage_key_location", "data_dir")
    if _under(path, ROOT):
        raise StorageError("storage_key_location", "repo")


def parse_master(text: str) -> bytes:
    try:
        raw = _b64d(text)
    except Exception as exc:
        raise StorageError("storage_key_invalid") from exc
    if len(raw) != KEY_BYTES:
        raise StorageError("storage_key_invalid")
    return raw


def _keychain():
    from packages import keychain

    return keychain


def load_master(data_dir: str | None = None) -> tuple[bytes | None, str]:
    """返回 (主密钥, 来源)。来源 env / keychain / file / none。

    顺序：环境变量 > 系统钥匙串（13.B，桌面壳启用时）> 主密钥文件。"""
    env = os.environ.get("MAS_MASTER_KEY")
    if env:
        return parse_master(env), "env"
    keychain = _keychain()
    if keychain.enabled():
        try:
            stored = keychain.get(keychain.MASTER_ENTRY)
        except keychain.KeychainError as exc:
            raise StorageError("storage_keychain_failed", exc.code) from exc
        if stored:
            return parse_master(stored), "keychain"
    return load_master_file(data_dir)


def load_master_file(data_dir: str | None = None) -> tuple[bytes | None, str]:
    """只看主密钥文件（迁移 / 回退命令用）。"""
    path = default_key_path()
    if not os.path.isfile(path):
        return None, "none"
    check_key_location(path, data_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise StorageError("storage_key_invalid") from exc
    if not isinstance(data, dict) or data.get("schema") != KEY_SCHEMA:
        raise StorageError("storage_key_invalid")
    master = parse_master(str(data.get("key") or ""))
    if data.get("key_id") and data["key_id"] != key_id_of(master):
        raise StorageError("storage_key_invalid")
    return master, "file"


def write_master(master: bytes, data_dir: str | None = None, path: str | None = None) -> str:
    """把主密钥写到数据目录和仓库之外（0600 / 0700，已存在不覆盖）。返回路径（只给本机命令行看）。"""
    path = path or default_key_path()
    check_key_location(path, data_dir)
    if os.path.exists(path):
        raise StorageError("storage_key_exists")
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    payload = json.dumps({
        "schema": KEY_SCHEMA, "alg": ALG, "key_id": key_id_of(master), "key": _b64e(master),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "MAS 本机主密钥。丢了它和备份，加密的会话就再也打不开；不要放进数据目录、仓库或网盘同步目录。",
    }, ensure_ascii=False, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return path


def store_master(master: bytes, data_dir: str | None = None) -> str:
    """新主密钥存哪：钥匙串启用且可用就进钥匙串（读回校验），否则写文件。返回来源 keychain / file。"""
    keychain = _keychain()
    if keychain.active():
        try:
            keychain.set(keychain.MASTER_ENTRY, _b64e(master))
        except keychain.KeychainError as exc:
            raise StorageError("storage_keychain_failed", exc.code) from exc
        return "keychain"
    write_master(master, data_dir)
    return "file"


def new_master() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def export_backup(master: bytes, passphrase: str) -> dict:
    if not isinstance(passphrase, str) or len(passphrase) < PASSPHRASE_MIN:
        raise StorageError("storage_passphrase_weak")
    salt = secrets.token_bytes(16)
    kek = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                         maxmem=128 * 1024 * 1024, dklen=32)
    header = {"schema": BACKUP_SCHEMA, "alg": ALG, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
              "key_id": key_id_of(master)}
    nonce = secrets.token_bytes(NONCE_BYTES)
    aad = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sealed = _aesgcm(kek).encrypt(nonce, master, aad)
    return dict(header, salt=_b64e(salt), nonce=_b64e(nonce), ct=_b64e(sealed),
                note="MAS 主密钥备份（口令加密）。恢复：python apps/web/storage_tool.py import-key --in <本文件>")


def import_backup(backup: dict, passphrase: str) -> bytes:
    if not isinstance(backup, dict) or backup.get("schema") != BACKUP_SCHEMA or backup.get("kdf") != "scrypt":
        raise StorageError("storage_backup_invalid")
    try:
        header = {key: backup[key] for key in ("schema", "alg", "kdf", "n", "r", "p", "key_id")}
        kek = hashlib.scrypt(str(passphrase).encode("utf-8"), salt=_b64d(backup["salt"]), n=int(backup["n"]),
                             r=int(backup["r"]), p=int(backup["p"]), maxmem=128 * 1024 * 1024, dklen=32)
        aad = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        master = _aesgcm(kek).decrypt(_b64d(backup["nonce"]), _b64d(backup["ct"]), aad)
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError("storage_backup_invalid") from exc
    if len(master) != KEY_BYTES or key_id_of(master) != backup.get("key_id"):
        raise StorageError("storage_backup_invalid")
    return master


# ---------- 数据目录标记与启动 ----------

def read_marker(data_dir: str) -> dict | None:
    path = os.path.join(data_dir, MARKER_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("schema") == STORAGE_SCHEMA:
        return data
    return None


def write_marker(data_dir: str, key_id: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, MARKER_NAME)
    temporary = path + ".part"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"schema": STORAGE_SCHEMA, "mode": "encrypted", "alg": ALG, "key_id": key_id,
                   "kdf": "HKDF-SHA256(session_id)",
                   "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, handle, indent=2)
    os.replace(temporary, path)


def remove_marker(data_dir: str) -> None:
    try:
        os.remove(os.path.join(data_dir, MARKER_NAME))
    except OSError:
        pass


def requested_mode() -> str | None:
    raw = (os.environ.get("MAS_STORAGE_ENCRYPTION") or "").strip().lower()
    if raw in ("", "off", "0", "false", "no", "plaintext"):
        return None if raw == "" else "plaintext"
    if raw in ("on", "1", "true", "yes", "encrypted"):
        return "encrypted"
    if raw == "memory":
        return "memory"
    raise StorageError("storage_mode_invalid")


def plaintext_items(data_dir: str) -> dict:
    """数一数数据目录里还有多少明文：会话文件、内容字节、旧版会话文件目录。"""
    sessions = os.path.join(data_dir, "sessions")
    counts = {"sessions": 0, "blobs": 0, "legacy_files": 0}
    if os.path.isdir(sessions):
        for name in os.listdir(sessions):
            full = os.path.join(sessions, name)
            if name.endswith(".json"):
                try:
                    with open(full, encoding="utf-8") as handle:
                        data = json.load(handle)
                except (OSError, ValueError):
                    continue
                if not is_envelope(data):
                    counts["sessions"] += 1
            elif os.path.isdir(os.path.join(full, "files")):
                counts["legacy_files"] += len(os.listdir(os.path.join(full, "files")))
    blobs = os.path.join(data_dir, "artifacts", "blobs")
    if os.path.isdir(blobs):
        for _root, _dirs, files in os.walk(blobs):
            counts["blobs"] += sum(1 for name in files if not name.endswith(".part"))
    return counts


class Storage:
    """启动时决定的存储模式。cipher 只有 encrypted 才有。"""

    def __init__(self, mode: str, cipher: Cipher | None = None, key_source: str = "none",
                 created_key: bool = False):
        self.mode = mode
        self.cipher = cipher
        self.key_source = key_source
        self.created_key = created_key

    @property
    def key_id(self) -> str | None:
        return self.cipher.key_id if self.cipher else None

    def public(self) -> dict:
        return {
            "schema": STORAGE_SCHEMA,
            "mode": self.mode,
            "alg": ALG if self.mode == "encrypted" else None,
            "key_id": self.key_id,
            "key_source": self.key_source if self.mode == "encrypted" else None,
            "session_isolation": "per_session_key" if self.mode == "encrypted" else None,
            "persisted": self.mode != "memory",
            "keychain": _keychain().status(),
        }


def open_storage(data_dir: str, *, allow_create: bool = True) -> Storage:
    """按环境变量和数据目录标记决定模式。

    - 已加密的数据目录（有 storage.json）永远按加密打开，环境变量关不掉（不悄悄降级）。
    - MAS_STORAGE_ENCRYPTION=on：空数据目录首次启动生成主密钥；有明文数据先要迁移。
    - MAS_STORAGE_ENCRYPTION=memory：会话只在内存里，不写 sessions/ 和 artifacts/。
    - 默认（不设）：明文，和以前一样。
    """
    requested = requested_mode()
    if requested == "memory":
        return Storage("memory")
    marker = read_marker(data_dir)
    if marker is None and requested != "encrypted":
        return Storage("plaintext")
    master, source = load_master(data_dir)
    created = False
    if marker is not None:
        if master is None:
            raise StorageError("storage_key_missing")
        if key_id_of(master) != marker.get("key_id"):
            raise StorageError("storage_key_mismatch")
        return Storage("encrypted", Cipher(master), source)
    left = plaintext_items(data_dir)
    if any(left.values()):
        raise StorageError("storage_migration_required")
    if master is None:
        if not allow_create:
            raise StorageError("storage_key_missing")
        if not crypto_available():
            raise StorageError("storage_crypto_unavailable")
        master = new_master()
        source, created = store_master(master, data_dir), True
    cipher = Cipher(master)
    write_marker(data_dir, cipher.key_id)
    return Storage("encrypted", cipher, source, created)
