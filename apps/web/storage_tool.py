"""本地存储加密的命令行（12.F #62）。

    python apps/web/storage_tool.py [--data-dir DIR] status
    python apps/web/storage_tool.py [--data-dir DIR] encrypt          # 明文 → 加密（没有主密钥就生成）
    python apps/web/storage_tool.py [--data-dir DIR] decrypt --yes    # 加密 → 明文（要主密钥）
    python apps/web/storage_tool.py [--data-dir DIR] verify           # 逐个解开会话和字节，报坏了几个
    python apps/web/storage_tool.py export-key --out FILE             # 口令加密的主密钥备份
    python apps/web/storage_tool.py import-key --in FILE              # 从备份恢复主密钥

迁移前先停掉聊天服务。口令从 MAS_BACKUP_PASSPHRASE 读，没有就交互输入（至少 12 个字符）。
密钥和备份不许放进数据目录或仓库。任何输出里都没有密钥本身。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import sys
import time
import uuid

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(WEB_DIR, "..", ".."))
for _path in (ROOT, WEB_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import store  # noqa: E402
from packages.artifacts import ArtifactStore  # noqa: E402
from packages.artifacts import crypto as C  # noqa: E402
from services import telemetry  # noqa: E402

CHECKSUM_KEYS = ("contents", "attachments")


def _stores(data_dir: str, storage: C.Storage) -> store.SessionStore:
    return store.SessionStore(os.path.join(data_dir, "sessions"), ArtifactStore(os.path.join(data_dir, "artifacts")),
                              storage=storage)


def _items(session: dict):
    for key in CHECKSUM_KEYS:
        for item in session.get(key) or []:
            if isinstance(item, dict):
                yield item
    for message in session.get("messages") or []:
        for item in message.get("attachments") or []:
            if isinstance(item, dict):
                yield item


def _raw_sessions(data_dir: str):
    folder = os.path.join(data_dir, "sessions")
    if not os.path.isdir(folder):
        return
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json") or not store._ID.fullmatch(name[:-5]):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        yield name[:-5], data


def _record(data_dir: str, kind: str, **fields) -> None:
    telemetry.configure(os.path.join(data_dir, "telemetry.jsonl"))
    telemetry.record(kind, trace_id="sto_" + uuid.uuid4().hex, **fields)


def migrate_encrypt(data_dir: str, master: bytes) -> dict:
    """明文会话、内容字节、旧版会话文件 → 按会话加密。先全部写好密文和标记，再删明文。"""
    cipher = C.Cipher(master)
    marker = C.read_marker(data_dir)
    if marker is not None and marker.get("key_id") != cipher.key_id:
        raise C.StorageError("storage_key_mismatch")
    plain = _stores(data_dir, C.Storage("plaintext"))
    sealed = _stores(data_dir, C.Storage("encrypted", cipher))
    stats = {"sessions": 0, "blobs": 0, "legacy_files": 0, "already": 0}
    for sid, data in list(_raw_sessions(data_dir)):
        if C.is_envelope(data):
            stats["already"] += 1
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            continue
        for item in _items(data):
            handle = item.get("handle") if isinstance(item.get("handle"), str) else ""
            checksum = item.get("checksum")
            if isinstance(checksum, str) and checksum:
                blob = plain.artifacts.get(checksum) or plain._read_file(sid, handle)
                if blob is None:
                    continue
                sealed._put_blob(sid, checksum, blob)
                stats["blobs"] += 1
            elif handle and not isinstance(item.get("text"), str):
                blob = plain._read_file(sid, handle)
                if blob is None:
                    continue
                checksum = hashlib.sha256(blob).hexdigest()
                sealed._put_blob(sid, checksum, blob)
                item["checksum"] = checksum
                stats["legacy_files"] += 1
        sealed._write(data)
        stats["sessions"] += 1
    C.write_marker(data_dir, cipher.key_id)
    # 密文和标记都落好了才删明文。注意：SSD / 写时复制文件系统上删除不等于擦除。
    shutil.rmtree(os.path.join(data_dir, "artifacts", "blobs"), ignore_errors=True)
    for sid, _data in _raw_sessions(data_dir):
        shutil.rmtree(os.path.join(data_dir, "sessions", sid, "files"), ignore_errors=True)
    _record(data_dir, "security.storage_migrated", direction="encrypt", key_id=cipher.key_id,
            sessions=stats["sessions"], blobs=stats["blobs"], legacy_files=stats["legacy_files"])
    return stats


def migrate_decrypt(data_dir: str, master: bytes) -> dict:
    cipher = C.Cipher(master)
    marker = C.read_marker(data_dir)
    if marker is not None and marker.get("key_id") != cipher.key_id:
        raise C.StorageError("storage_key_mismatch")
    sealed = _stores(data_dir, C.Storage("encrypted", cipher))
    stats = {"sessions": 0, "blobs": 0, "failed": 0}
    todo = []
    for sid, data in list(_raw_sessions(data_dir)):
        if not C.is_envelope(data):
            continue
        try:
            session = cipher.open_json(sid, data)
        except C.StorageError:
            stats["failed"] += 1
            continue
        todo.append((sid, session))
    if stats["failed"]:
        raise C.StorageError("storage_decrypt_failed", f"{stats['failed']} 个会话解不开，没有改动任何文件")
    for sid, session in todo:
        for item in _items(session):
            checksum = item.get("checksum")
            if isinstance(checksum, str) and checksum:
                blob = sealed._get_blob(sid, checksum)
                if blob is not None:
                    sealed.artifacts.put(blob)
                    stats["blobs"] += 1
        store._atomic_write(os.path.join(data_dir, "sessions", sid + ".json"), session)
        stats["sessions"] += 1
    C.remove_marker(data_dir)
    shutil.rmtree(os.path.join(data_dir, "artifacts", "sealed"), ignore_errors=True)
    _record(data_dir, "security.storage_migrated", direction="decrypt", key_id=cipher.key_id,
            sessions=stats["sessions"], blobs=stats["blobs"], legacy_files=0)
    return stats


def verify(data_dir: str, master: bytes) -> dict:
    cipher = C.Cipher(master)
    sealed = _stores(data_dir, C.Storage("encrypted", cipher))
    stats = {"sessions": 0, "blobs": 0, "failed_sessions": 0, "failed_blobs": 0, "plaintext_sessions": 0}
    for sid, data in _raw_sessions(data_dir):
        if not C.is_envelope(data):
            stats["plaintext_sessions"] += 1
            continue
        try:
            session = cipher.open_json(sid, data)
        except C.StorageError:
            stats["failed_sessions"] += 1
            continue
        stats["sessions"] += 1
        seen = set()
        for item in _items(session):
            checksum = item.get("checksum")
            if not isinstance(checksum, str) or not checksum or checksum in seen:
                continue
            seen.add(checksum)
            if sealed._get_blob(sid, checksum) is None:
                stats["failed_blobs"] += 1
            else:
                stats["blobs"] += 1
    return stats


def _passphrase(confirm: bool) -> str:
    value = os.environ.get("MAS_BACKUP_PASSPHRASE")
    if value:
        return value
    first = getpass.getpass("备份口令（至少 12 个字符）：")
    if confirm and getpass.getpass("再输一次：") != first:
        raise SystemExit("两次口令不一样。")
    return first


def _need_master(data_dir: str) -> bytes:
    master, _source = C.load_master(data_dir)
    if master is None:
        raise C.StorageError("storage_key_missing")
    return master


def _status(data_dir: str) -> dict:
    marker = C.read_marker(data_dir)
    out = {"data_dir_encrypted": marker is not None, "marker_key_id": (marker or {}).get("key_id"),
           "crypto_available": C.crypto_available(), "plaintext_left": C.plaintext_items(data_dir),
           "requested_mode": None, "key": None}
    try:
        out["requested_mode"] = C.requested_mode()
        master, source = C.load_master(data_dir)
        if master is not None:
            out["key"] = {"key_id": C.key_id_of(master), "source": source,
                          "matches_data_dir": marker is None or C.key_id_of(master) == marker.get("key_id")}
    except C.StorageError as exc:
        out["key_error"] = exc.code
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地存储加密（12.F）")
    parser.add_argument("--data-dir", default=os.environ.get("CHAT_DATA_DIR") or os.path.join(WEB_DIR, "data"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("encrypt")
    dec = sub.add_parser("decrypt")
    dec.add_argument("--yes", action="store_true", help="确认把会话写回明文")
    sub.add_parser("verify")
    exp = sub.add_parser("export-key")
    exp.add_argument("--out", required=True)
    imp = sub.add_parser("import-key")
    imp.add_argument("--in", dest="source", required=True)
    imp.add_argument("--key-file", default=None, help="写到哪里（默认 MAS_MASTER_KEY_FILE 或 ~/.mas/master.key）")
    args = parser.parse_args(argv)
    data_dir = os.path.abspath(args.data_dir)
    try:
        if args.command == "status":
            print(json.dumps(_status(data_dir), ensure_ascii=False, indent=2))
        elif args.command == "encrypt":
            master, _source = C.load_master(data_dir)
            created = None
            if master is None:
                master = C.new_master()
                created = C.write_master(master, data_dir)
                _record(data_dir, "security.storage_key_created", key_id=C.key_id_of(master), key_source="file")
            stats = migrate_encrypt(data_dir, master)
            print(json.dumps({"ok": True, "key_id": C.key_id_of(master), **stats}, ensure_ascii=False))
            if created:
                print("已生成主密钥：" + created + "\n马上备份：python apps/web/storage_tool.py export-key --out <U 盘或密码管理器>")
        elif args.command == "decrypt":
            if not args.yes:
                print("会把加密会话写回明文。确认请加 --yes。")
                return 2
            stats = migrate_decrypt(data_dir, _need_master(data_dir))
            print(json.dumps({"ok": True, **stats}, ensure_ascii=False))
        elif args.command == "verify":
            stats = verify(data_dir, _need_master(data_dir))
            print(json.dumps({"ok": not (stats["failed_sessions"] or stats["failed_blobs"]), **stats},
                             ensure_ascii=False))
            return 0 if not (stats["failed_sessions"] or stats["failed_blobs"]) else 1
        elif args.command == "export-key":
            out = os.path.abspath(args.out)
            C.check_key_location(out, data_dir)
            if os.path.exists(out):
                raise C.StorageError("storage_key_exists")
            backup = C.export_backup(_need_master(data_dir), _passphrase(confirm=True))
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(backup, handle, ensure_ascii=False, indent=2)
            print(json.dumps({"ok": True, "key_id": backup["key_id"], "out": out}, ensure_ascii=False))
        elif args.command == "import-key":
            with open(args.source, encoding="utf-8") as handle:
                backup = json.load(handle)
            master = C.import_backup(backup, _passphrase(confirm=False))
            target = C.write_master(master, data_dir, args.key_file)
            marker = C.read_marker(data_dir)
            print(json.dumps({"ok": True, "key_id": C.key_id_of(master), "key_file": target,
                              "matches_data_dir": marker is None or marker.get("key_id") == C.key_id_of(master)},
                             ensure_ascii=False))
    except C.StorageError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": exc.message, "detail": exc.detail},
                         ensure_ascii=False))
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "code": "io_error", "message": type(exc).__name__}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
