"""一次文生图。只调用 poc 里现成的 generate_image，不另写 Comfy 客户端。"""

from __future__ import annotations

import os
import sys
import threading

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(WEB_DIR, "..", ".."))
POC = os.path.join(ROOT, "poc", "comfyui")

_ENV_LOCK = threading.Lock()


def _ensure_poc() -> None:
    if POC not in sys.path:
        sys.path.insert(0, POC)


def style_names() -> set[str]:
    _ensure_poc()
    import agent_tools

    return set(agent_tools.load_styles())


def image_tools() -> list[dict]:
    """发给第三方的契约。参数里没有正文、字节、路径或文件名。"""
    _ensure_poc()
    poc_root = os.path.join(ROOT, "poc")
    if poc_root not in sys.path:
        sys.path.append(poc_root)
    from tools.registry import external_contracts

    return external_contracts()


def comfy_base() -> str | None:
    _ensure_poc()
    import comfy_poc

    try:
        config = comfy_poc.load_config()
    except comfy_poc.ComfyFailure:
        return None
    base = config.get("comfy_url")
    if not isinstance(base, str) or not base:
        return None
    return base


def generate(session_id: str, arguments: dict, on_event) -> dict:
    """把这一会话收成 COMFY_SESSION，避免和别的页面跑混工件。"""
    _ensure_poc()
    import agent_tools

    with _ENV_LOCK:
        previous = os.environ.get("COMFY_SESSION")
        os.environ["COMFY_SESSION"] = session_id
        try:
            with agent_tools.listen_run(on_event):
                return agent_tools.call_tool("generate_image", arguments)
        finally:
            if previous is None:
                os.environ.pop("COMFY_SESSION", None)
            else:
                os.environ["COMFY_SESSION"] = previous


def cancel(base: str, prompt_id: str) -> None:
    """另开一个客户端去取消这一次 prompt_id。不碰正在读模型的那条连接。"""
    _ensure_poc()
    import comfy_client

    comfy_client.ComfyClient(base).cancel(prompt_id)


def open_artifact(session_id: str, artifact_id: str) -> tuple[str | None, str | None]:
    _ensure_poc()
    import artifacts
    import comfy_poc

    try:
        config = comfy_poc.load_config()
    except comfy_poc.ComfyFailure:
        return None, None
    store, _session = artifacts.open_store(config)
    try:
        path = store.file_for(session_id, artifact_id)
    except artifacts.ArtifactError:
        return None, None
    if not path:
        return None, None
    record = store.get(session_id, artifact_id) or {}
    media = record.get("media_type")
    if not isinstance(media, str) or not media:
        media = "application/octet-stream"
    return path, media
