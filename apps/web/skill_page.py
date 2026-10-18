"""用已安装的 Anatomy 技能做成一张 HTML 页面。不调用 generate_image。"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import threading

import image_run

_LOCK = threading.Lock()
_CARD_TITLE = "Dial indicator"
_NODE_MISSING = "本机没有可用的 Node.js，做不了这张等距图。"
_FAILED = "等距图没有做成。"


def build_page(session_id: str, name: str, subject: str, *, node: str | None = None) -> dict:
    if name != "anatomy":
        return _error("unknown_skill", "没有这个技能。")
    title = _subject(subject)
    if title is None:
        return _error("invalid_parameter", "subject 需要是 1 到 80 个字的机制名称。")
    executable = shutil.which("node") if node is None else (node or None)
    if not executable:
        return _error("node_missing", _NODE_MISSING)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    example = os.path.join(root, "skills", "anatomy", "examples", "dial-indicator")
    script = os.path.join(example, "build.mjs")
    written = os.path.join(example, "dial-indicator-light.html")
    if not os.path.isfile(script):
        return _error("skill_error", _FAILED)
    with _LOCK:
        try:
            completed = subprocess.run(
                [executable, script, "--light"],
                cwd=example,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _error("skill_error", _FAILED)
        if completed.returncode != 0 or not os.path.isfile(written):
            _remove(written)
            return _error("skill_error", _FAILED)
        try:
            page = _retitle(_read(written), title)
        finally:
            _remove(written)
    if page is None:
        return _error("skill_error", _FAILED)
    try:
        store = image_run.artifact_store()
        saved = store.save_html(session_id=session_id, html=page, title=title)
    except Exception:
        return _error("skill_error", _FAILED)
    return {
        "ok": True,
        "tool": "build_skill_page",
        "executed": True,
        "artifact_id": saved.get("artifact_id"),
        "type": saved.get("type") or "html",
        "media_type": saved.get("media_type") or "text/html; charset=utf-8",
        "url": saved.get("url"),
    }


def _subject(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 80:
        return None
    return cleaned


def _retitle(page: str, title: str) -> str | None:
    escaped = html.escape(title, quote=True)
    old_title = f"<title>{_CARD_TITLE}</title>"
    old_corner = f'data-corner="title">{_CARD_TITLE}</span>'
    if old_title not in page or old_corner not in page or "<svg" not in page:
        return None
    return page.replace(old_title, f"<title>{escaped}</title>", 1).replace(
        old_corner, f'data-corner="title">{escaped}</span>', 1
    )


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        return


def _error(code: str, message: str) -> dict:
    return {
        "ok": False,
        "tool": "build_skill_page",
        "executed": False,
        "error": {"code": code, "message": message},
    }
