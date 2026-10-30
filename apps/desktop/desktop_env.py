"""桌面版默认值与启动前检查（13.B / #75）。

- 数据目录：MAS_DESKTOP_DATA_DIR > CHAT_DATA_DIR > 每用户目录
  （Windows %LOCALAPPDATA%\\MultimodalAgentStudio\\data，macOS ~/Library/Application Support/MultimodalAgentStudio/data，
  Linux $XDG_DATA_HOME/multimodal-agent-studio/data）。不和开发用的 apps/web/data 混在一起。
- 默认 MAS_STORAGE_ENCRYPTION=on、MAS_KEYCHAIN=on；用户显式设过就尊重（显式 off 会给警告）。
- 启动前检查：数据目录里有明文会话 / 文件又没加密标记时，拒绝启动并给出 storage_tool.py 迁移命令，不悄悄转换。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_DIR_WIN = "MultimodalAgentStudio"
APP_DIR_UNIX = "multimodal-agent-studio"


def default_data_dir(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    for key in ("MAS_DESKTOP_DATA_DIR", "CHAT_DATA_DIR"):
        if env.get(key):
            return os.path.abspath(os.path.expanduser(env[key]))
    home = Path.home()
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or (home / "AppData" / "Local")) / APP_DIR_WIN
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support" / APP_DIR_WIN
    else:
        base = Path(env.get("XDG_DATA_HOME") or (home / ".local" / "share")) / APP_DIR_UNIX
    return str(base / "data")


def apply_defaults(env: dict) -> list[str]:
    """就地改 env（sidecar 子进程的环境），返回给用户看的提示（不含任何密钥）。"""
    notes = []
    if not (env.get("MAS_STORAGE_ENCRYPTION") or "").strip():
        env["MAS_STORAGE_ENCRYPTION"] = "on"
    elif env["MAS_STORAGE_ENCRYPTION"].strip().lower() in ("off", "0", "false", "no", "plaintext"):
        notes.append("storage_encryption_off: 你显式关了落盘加密（MAS_STORAGE_ENCRYPTION=off），桌面版数据目录会是明文。")
    if not (env.get("MAS_KEYCHAIN") or "").strip():
        env["MAS_KEYCHAIN"] = "on"
    elif env["MAS_KEYCHAIN"].strip().lower() in ("off", "0", "false", "no"):
        notes.append("keychain_off: 你显式关了系统钥匙串（MAS_KEYCHAIN=off），API key 和主密钥会存成文件。")
    return notes


class PreflightError(Exception):
    def __init__(self, code: str, message: str, commands: list[str] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.commands = commands or []

    def public(self) -> dict:
        return {"code": self.code, "message": self.message, "commands": self.commands}


def _with_env(env: dict):
    """临时把 env 里的存储 / 钥匙串开关套到本进程，用 crypto 的判断逻辑。"""
    keys = ("MAS_STORAGE_ENCRYPTION", "MAS_KEYCHAIN", "MAS_KEYCHAIN_NAMESPACE", "MAS_MASTER_KEY_FILE")

    class _Ctx:
        def __enter__(self):
            self.saved = {key: os.environ.get(key) for key in keys}
            for key in keys:
                if env.get(key) is not None:
                    os.environ[key] = env[key]
            return self

        def __exit__(self, *exc):
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return False

    return _Ctx()


def preflight(data_dir: str, env: dict) -> dict:
    """启动 sidecar 前看一眼数据目录。返回状态；需要用户动手时抛 PreflightError（不改任何文件）。"""
    from packages import keychain
    from packages.artifacts import crypto

    with _with_env(env):
        try:
            mode = crypto.requested_mode()
        except crypto.StorageError as exc:
            raise PreflightError(exc.code, exc.message) from exc
        marker = crypto.read_marker(data_dir) if os.path.isdir(data_dir) else None
        status = {"data_dir_encrypted": marker is not None, "requested_mode": mode,
                  "keychain": keychain.status(), "first_run": not os.path.isdir(data_dir)}
        if marker is not None or mode != "encrypted":
            return status
        if not crypto.crypto_available():
            raise PreflightError("storage_crypto_unavailable", crypto.STORAGE_ERRORS["storage_crypto_unavailable"],
                                 [f'"{sys.executable}" -m pip install cryptography'])
        left = crypto.plaintext_items(data_dir) if os.path.isdir(data_dir) else {}
        if any(left.values()):
            tool = ROOT / "apps" / "web" / "storage_tool.py"
            raise PreflightError(
                "storage_migration_required",
                "桌面版默认加密数据目录，但这里还有明文会话或文件"
                f"（会话 {left.get('sessions', 0)}、文件 {left.get('blobs', 0)}、旧版文件 {left.get('legacy_files', 0)}）。"
                "不会悄悄转换：请先关掉所有使用这个数据目录的服务，再运行下面的迁移命令（主密钥进系统钥匙串），"
                "然后马上 export-key 备份主密钥。也可以设 MAS_STORAGE_ENCRYPTION=off 继续明文使用（不推荐）。",
                [f'"{sys.executable}" "{tool}" --keychain --data-dir "{data_dir}" encrypt',
                 f'"{sys.executable}" "{tool}" --keychain --data-dir "{data_dir}" export-key --out <U 盘上的备份文件>'],
            )
        return status
