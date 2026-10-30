"""系统钥匙串迁移 / 回退命令行（13.B / #75）。任何输出里都没有密钥本身，只有条目名、key_id 和末 4 位。

    python apps/desktop/keychain_tool.py status                 # 钥匙串是否可用、哪些秘密还在文件里
    python apps/desktop/keychain_tool.py migrate                # 文件 → 钥匙串（读回校验）；默认保留文件
    python apps/desktop/keychain_tool.py migrate --purge-files  # 校验通过后把文件里的原值清掉（主密钥文件删除）
    python apps/desktop/keychain_tool.py rollback               # 钥匙串 → 文件（回退）；默认保留钥匙串
    python apps/desktop/keychain_tool.py rollback --purge-keychain
    python apps/desktop/keychain_tool.py delete ENTRY           # 删一个条目（如 tool.generate_image.remote_api_key）

数据目录默认同桌面版（desktop_env.default_data_dir()）；--data-dir 可改。迁移前先关掉桌面窗口和服务。
主密钥迁移前请先 `apps/web/storage_tool.py export-key` 做口令备份：钥匙串和文件都丢了，加密数据就打不开。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
for _path in (str(ROOT), str(ROOT / "apps" / "web"), str(DESKTOP_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import desktop_env  # noqa: E402
from packages import keychain  # noqa: E402
from packages.artifacts import crypto as C  # noqa: E402


def _hint(value: str) -> str:
    return value[-4:] if value and len(value) >= 8 else ""


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def _secret_fields() -> dict:
    """{工具或后端名: [保密字段名]}。"""
    import tool_config

    out = {}
    for name, decl in tool_config.declarations().items():
        fields = [field["name"] for field in decl["fields"] if field.get("secret")]
        if fields:
            out[name] = fields
    return out


def _file_secrets(data_dir: str) -> list[tuple[str, str, str]]:
    """文件里还留着的秘密：(条目名, 来源文件, 值)。"""
    found = []
    provider = _load(os.path.join(data_dir, "provider.json"))
    if isinstance(provider.get("api_key"), str) and provider["api_key"].strip():
        found.append((keychain.PROVIDER_ENTRY, "provider.json", provider["api_key"].strip()))
    tools = _load(os.path.join(data_dir, "tools.json"))
    for name, fields in _secret_fields().items():
        section = tools.get(name) if isinstance(tools.get(name), dict) else {}
        for field in fields:
            value = section.get(field)
            if isinstance(value, str) and value.strip():
                found.append((keychain.tool_entry(name, field), "tools.json", value.strip()))
    return found


def status(data_dir: str) -> dict:
    master_file, _ = (None, None)
    try:
        master_file, _ = C.load_master_file(data_dir)
    except C.StorageError as exc:
        master_file = exc.code
    in_keychain = None
    try:
        stored = keychain.get(keychain.MASTER_ENTRY)
        in_keychain = C.key_id_of(C.parse_master(stored)) if stored else None
    except (keychain.KeychainError, C.StorageError) as exc:
        in_keychain = getattr(exc, "code", "error")
    marker = C.read_marker(data_dir)
    return {
        "keychain": keychain.status(),
        "data_dir": data_dir,
        "data_dir_key_id": (marker or {}).get("key_id"),
        "master_key": {
            "keychain_key_id": in_keychain,
            "file_key_id": C.key_id_of(master_file) if isinstance(master_file, bytes) else master_file,
            "file_path": C.default_key_path(),
        },
        "secrets_in_files": [{"entry": entry, "file": source, "hint": _hint(value)}
                             for entry, source, value in _file_secrets(data_dir)],
        "secrets_in_keychain": [entry for entry in _known_entries() if _kc_has(entry)],
    }


def _known_entries() -> list[str]:
    entries = [keychain.MASTER_ENTRY, keychain.PROVIDER_ENTRY]
    for name, fields in _secret_fields().items():
        entries.extend(keychain.tool_entry(name, field) for field in fields)
    return entries


def _kc_has(entry: str) -> bool:
    try:
        return bool(keychain.get(entry))
    except keychain.KeychainError:
        return False


def migrate(data_dir: str, purge: bool) -> dict:
    if not keychain.available():
        raise keychain.KeychainError("keychain_unavailable", str(keychain.status().get("reason")))
    moved, kept = [], []
    # 1) 主密钥
    master, _src = C.load_master_file(data_dir)
    if master is not None:
        current = keychain.get(keychain.MASTER_ENTRY)
        if current and C.key_id_of(C.parse_master(current)) != C.key_id_of(master):
            raise C.StorageError("storage_key_mismatch", "钥匙串里已有另一把主密钥，不覆盖。")
        if not current:
            keychain.set(keychain.MASTER_ENTRY, C._b64e(master))
        back = keychain.get(keychain.MASTER_ENTRY)
        if not back or C.key_id_of(C.parse_master(back)) != C.key_id_of(master):
            raise keychain.KeychainError("keychain_write_failed", "主密钥读回校验失败。")
        moved.append({"entry": keychain.MASTER_ENTRY, "key_id": C.key_id_of(master)})
        if purge:
            os.remove(C.default_key_path())
        else:
            kept.append("master.key 文件（--purge-files 才删除）")
    # 2) provider / 工具保密字段
    provider_path = os.path.join(data_dir, "provider.json")
    tools_path = os.path.join(data_dir, "tools.json")
    provider, tools = _load(provider_path), _load(tools_path)
    for entry, source, value in _file_secrets(data_dir):
        keychain.set(entry, value)  # set 内部读回校验
        moved.append({"entry": entry, "hint": _hint(value)})
        if not purge:
            kept.append(f"{source} 里的 {entry}")
            continue
        if source == "provider.json":
            provider["api_key"] = ""
        else:
            _, name, field = entry.split(".", 2)
            tools.get(name, {}).pop(field, None)
    if purge:
        if provider:
            _save(provider_path, provider)
        if tools:
            _save(tools_path, tools)
    return {"ok": True, "moved": moved, "kept_in_files": kept}


def rollback(data_dir: str, purge: bool) -> dict:
    if not keychain.available():
        raise keychain.KeychainError("keychain_unavailable", str(keychain.status().get("reason")))
    restored = []
    stored = keychain.get(keychain.MASTER_ENTRY)
    if stored:
        master = C.parse_master(stored)
        existing, _ = C.load_master_file(data_dir)
        if existing is None:
            C.write_master(master, data_dir)
        elif C.key_id_of(existing) != C.key_id_of(master):
            raise C.StorageError("storage_key_exists", "主密钥文件已存在且 key_id 不同，不覆盖。")
        restored.append({"entry": keychain.MASTER_ENTRY, "key_id": C.key_id_of(master),
                         "file": C.default_key_path()})
    provider_path = os.path.join(data_dir, "provider.json")
    tools_path = os.path.join(data_dir, "tools.json")
    provider, tools = _load(provider_path), _load(tools_path)
    for entry in _known_entries()[1:]:
        value = keychain.get(entry)
        if not value:
            continue
        if entry == keychain.PROVIDER_ENTRY:
            if not provider:
                import store

                provider = {"base_url": store.DEFAULT_BASE_URL, "model": store.DEFAULT_MODEL}
            provider["api_key"] = value
        else:
            _, name, field = entry.split(".", 2)
            tools.setdefault(name, {})[field] = value
        restored.append({"entry": entry, "hint": _hint(value)})
    os.makedirs(data_dir, exist_ok=True)
    if provider:
        _save(provider_path, provider)
    if tools:
        _save(tools_path, tools)
    if purge:
        for item in restored:
            keychain.delete(item["entry"])
    return {"ok": True, "restored": restored, "keychain_purged": purge}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="系统钥匙串迁移 / 回退（13.B）")
    parser.add_argument("--data-dir", default=None, help="默认桌面版数据目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    mig = sub.add_parser("migrate")
    mig.add_argument("--purge-files", action="store_true")
    rb = sub.add_parser("rollback")
    rb.add_argument("--purge-keychain", action="store_true")
    rm = sub.add_parser("delete")
    rm.add_argument("entry")
    args = parser.parse_args(argv)
    if not os.environ.get("MAS_KEYCHAIN"):
        os.environ["MAS_KEYCHAIN"] = "on"
    data_dir = os.path.abspath(args.data_dir or desktop_env.default_data_dir())
    try:
        if args.command == "status":
            out = status(data_dir)
        elif args.command == "migrate":
            out = migrate(data_dir, args.purge_files)
        elif args.command == "rollback":
            out = rollback(data_dir, args.purge_keychain)
        else:
            if args.entry not in _known_entries():
                print(json.dumps({"ok": False, "code": "keychain_entry_unknown"}, ensure_ascii=False))
                return 1
            out = {"ok": True, "deleted": keychain.delete(args.entry), "entry": args.entry}
    except (keychain.KeychainError, C.StorageError) as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": getattr(exc, "message", "")}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
