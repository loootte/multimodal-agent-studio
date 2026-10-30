"""桌面壳持久配置（13.C / #76）。

位置：数据目录的上一级目录下的 desktop.json
（默认 %LOCALAPPDATA%\\MultimodalAgentStudio\\desktop.json）。
不含 API key 原值；密钥只进钥匙串。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import desktop_env  # noqa: E402

SCHEMA = "mas.desktop.config/1"
DEFAULTS = {
    "schema": SCHEMA,
    "wizard_completed": False,
    "auto_update": False,  # 13.C：默认关闭
    "update_url": "",
    "update_pubkey": "",  # hex 编码的 Ed25519 公钥；空 = 未配置，开启更新时拒绝拉取
    "comfy_url": "http://127.0.0.1:8188",
    "encryption_confirmed": False,
    "local_only": False,  # 向导跳过 API key → 只用本机能力
}


def config_path(data_dir: str | None = None) -> Path:
    data = Path(data_dir or desktop_env.default_data_dir())
    return data.parent / "desktop.json"


def load(data_dir: str | None = None) -> dict:
    path = config_path(data_dir)
    cfg = dict(DEFAULTS)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            for key in DEFAULTS:
                if key in raw:
                    cfg[key] = raw[key]
    # 环境变量可覆盖更新相关项（便于测试 / 企业部署）
    if os.environ.get("MAS_UPDATE_URL"):
        cfg["update_url"] = os.environ["MAS_UPDATE_URL"].strip()
    if os.environ.get("MAS_UPDATE_PUBKEY"):
        cfg["update_pubkey"] = os.environ["MAS_UPDATE_PUBKEY"].strip()
    auto = (os.environ.get("MAS_AUTO_UPDATE") or "").strip().lower()
    if auto in ("1", "on", "true", "yes"):
        cfg["auto_update"] = True
    elif auto in ("0", "off", "false", "no"):
        cfg["auto_update"] = False
    return cfg


def save(cfg: dict, data_dir: str | None = None) -> Path:
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in cfg:
            out[key] = cfg[key]
    out["schema"] = SCHEMA
    # 绝不把密钥写进这个文件
    for banned in ("api_key", "token", "password", "secret", "master_key"):
        out.pop(banned, None)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def app_version() -> str:
    pkg = DESKTOP_DIR / "package.json"
    if pkg.is_file():
        try:
            return str(json.loads(pkg.read_text(encoding="utf-8")).get("version") or "0.0.0")
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return "0.0.0"
