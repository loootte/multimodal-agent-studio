"""工具自带的网页面板（ui.panel）：GET /api/tools/<name>/ui/<file>。见 docs/tool-code-contract.md「网页面板」。

规则（宿主强制，与工具代码无关）：
- 只给清单 ui.panel 里声明的 script / style 两个文件（白名单）；别的文件名、子目录、..、编码过的路径一律 404。
- 文件解开符号链接以后必须还在工具目录的 ui/ 里（packs.panel_files 检查），不超过 packs.PANEL_MAX_BYTES。
- ui 权限必须生效：内置工具清单里声明；已安装的工具还要过 packs.gate（授权记录在、包内容没被改过）。
- 内置面板：按 text/javascript / text/css 同源给，页面用 <script src> / <link> 加载。
- 已安装（第三方）面板：文件本身只按 text/plain 给（加 nosniff，浏览器不会当脚本执行，只能看）；
  真正运行走 /api/tools/<name>/ui/frame：宿主拼一个文档，把宿主的桥接脚本、面板脚本和样式内联进去，
  响应头 CSP 带 sandbox allow-scripts（不同源、不能弹窗、不能提交表单、不能导航顶层页面），
  default-src 'none'（不能联网、不能取同源接口），脚本和样式只认这几段内容的 sha256。
  面板只能经 postMessage 调宿主的面板接口（apps/web/static/panels.js）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import urllib.parse

import tool_config

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
FRAME_BRIDGE = os.path.join(WEB_DIR, "static", "panel_frame.js")
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
# 沙箱 iframe 里的基础样式（宿主的，和面板样式一起按哈希放行）。
FRAME_BASE_CSS = (
    "html,body{margin:0;padding:0;font:14px/1.45 system-ui,sans-serif;color:#222;background:#fff}"
    "#mas-panel-root{padding:0.2rem}button,input,select{font:inherit}"
    "button{padding:0.3rem 0.7rem;cursor:pointer}"
)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PanelError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _not_found() -> PanelError:
    return PanelError(404, "not_found", "找不到这个面板文件。")


def _pack_and_files(name: str):
    import module_tools  # 晚加载：server 先装好 sys.path
    packs = tool_config._packs()
    pack = module_tools.get(name)
    if pack is None or not packs.panel_spec(pack.manifest):
        raise _not_found()
    if not pack.builtin:
        problem = packs.gate(pack)
        if problem:
            raise PanelError(403, "permission_denied", (problem.get("error") or {}).get("message") or "没有授权。")
    if "ui" not in pack.effective():
        raise PanelError(403, "permission_denied", "这个工具的网页面板没有授权（ui）。")
    try:
        files = packs.panel_files(pack.folder, pack.manifest)
    except packs.PackError as exc:
        raise PanelError(404, "not_found", exc.message) from exc
    return pack, packs.panel_spec(pack.manifest), files


def resolve(name: str, rest: list[str]) -> tuple[bytes, dict]:
    """返回 (内容, 响应头)。不合格抛 PanelError。"""
    if not rest or not all(_SAFE_SEGMENT.match(part or "") and ".." not in part for part in rest):
        raise _not_found()
    if urllib.parse.unquote("/".join(rest)) != "/".join(rest):
        raise _not_found()
    pack, spec, files = _pack_and_files(name)
    if rest == ["frame"]:
        if pack.builtin:
            raise _not_found()  # 内置面板同源加载，不走 iframe
        return frame(pack, spec, files)
    rel = "ui/" + "/".join(rest)
    if rel not in files:  # 只给声明了的文件
        raise _not_found()
    with open(files[rel], "rb") as handle:
        raw = handle.read()
    if pack.builtin:
        return raw, {"Content-Type": TYPES[os.path.splitext(rel)[1]]}
    # 第三方面板文件只给人看，不能在页面里当脚本或样式用。
    return raw, {"Content-Type": "text/plain; charset=utf-8", "Content-Security-Policy": "sandbox; default-src 'none'"}


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _inline_script(text: str) -> str:
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.I).replace("<!--", "<\\!--")


def _inline_style(text: str) -> str:
    return re.sub(r"</(style)", r"<\\/\1", text, flags=re.I)


def frame(pack, spec: dict, files: dict) -> tuple[bytes, dict]:
    with open(FRAME_BRIDGE, encoding="utf-8") as handle:
        bridge = _inline_script(handle.read())
    with open(files[spec["script"]], encoding="utf-8") as handle:
        script = _inline_script(handle.read())
    style = ""
    if spec.get("style"):
        with open(files[spec["style"]], encoding="utf-8") as handle:
            style = _inline_style(handle.read())
    base = FRAME_BASE_CSS
    html = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<style>{base}</style><style>{style}</style></head>"
            "<body><div id=\"mas-panel-root\"></div>"
            f"<script>{bridge}</script><script>{script}</script></body></html>")
    csp = ("sandbox allow-scripts; default-src 'none'; "
           f"script-src {_hash(bridge)} {_hash(script)}; style-src {_hash(base)} {_hash(style)}; "
           "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
    return html.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": csp,
                                  "Referrer-Policy": "no-referrer"}
