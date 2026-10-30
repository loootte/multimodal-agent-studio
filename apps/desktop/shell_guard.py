"""13.D（#77）桌面壳加固：WebView 导航白名单、外链确认、IPC 拒绝审计、devtools 开关。

不变量：
- WebView 只显示本机 sidecar（http://127.0.0.1:<port>）的页面；别的地址一律不导航，记 security.desktop_navigation_blocked。
- 模型或工具结果里的链接只当不可信数据：只收 http / https / mailto，打开前由宿主确认卡显示完整地址
  （文字是这里的固定模板，不用模型文案、不用链接文字），用户确认后才交给系统浏览器。记 security.desktop_external_link_confirm。
- js_api 只有白名单方法；其余调用（以及外站页面里的调用）全部拒绝，记 security.desktop_ipc_denied。
- 审计只记事件名、原因码、方法名、scheme 和主机名；不记完整地址、路径、查询串、链接文字。
- 审计经 POST /api/desktop/security_event 写服务端遥测，只认 X-MAS-Desktop-Token 请求头（不认 Cookie），
  WebView 里的脚本拿不到令牌，伪造不了审计。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EVENTS = (
    "security.desktop_navigation_blocked",
    "security.desktop_ipc_denied",
    "security.desktop_external_link_confirm",
)
EXTERNAL_SCHEMES = ("http", "https", "mailto")
MAX_URL = 2048
_HOST = re.compile(r"[A-Za-z0-9.\-\[\]:]{1,255}")
_CODE = re.compile(r"[a-z_]{1,40}")
_METHOD = re.compile(r"[A-Za-z0-9_.]{1,64}")
_CTRL = re.compile(r"[\x00-\x20\x7f<>\"'`\\]")


def _origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme in ("http", "https") and port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def navigation_allowed(url, base: str) -> bool:
    """只放行 sidecar 同源页面（scheme/host/port 都相同，host 必须是 127.0.0.1）。about:blank 放行；
    blob: 只放行同源 blob。其余（外站、file:、data:、javascript:、别的本机端口）都拒绝。"""
    if not isinstance(url, str) or not url or len(url) > 8192:
        return False
    if url == "about:blank":
        return True
    if url.lower().startswith("blob:"):
        url = url[5:]
    want = _origin(base)
    got = _origin(url)
    if not want or not got or want[1] != "127.0.0.1":
        return False
    return got == want


def external_target(url) -> dict | None:
    """外链是否可以交给系统浏览器：只收 http/https/mailto，长度有限，不含控制字符和引号。"""
    if not isinstance(url, str):
        return None
    if not url or len(url) > MAX_URL or _CTRL.search(url):  # 不 strip：带空白 / 换行的一律不收
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in EXTERNAL_SCHEMES:
        return None
    if scheme == "mailto":
        host = (parts.path.rpartition("@")[2] or "").lower()
    else:
        host = (parts.hostname or "").lower()
        if not host or parts.username or parts.password:
            return None  # user:pass@host 常用来伪装目标主机，直接不收
    if not host or not _HOST.fullmatch(host):
        return None
    return {"url": url, "scheme": scheme, "host": host}


def confirm_text(target: dict) -> tuple[str, str]:
    """宿主确认卡（12.G 模板）：标题和正文都是固定文字 + 完整地址 + 主机名，不取模型文案或链接文字。"""
    title = "打开外部链接？"
    body = ("这个链接来自模型或工具结果（不可信）。确认后会用系统浏览器打开，不会在本窗口里打开。\n\n"
            f"完整地址：\n{target['url']}\n\n"
            f"主机：{target['host']}\n\n"
            "不认识这个地址就选「取消」。")
    return title, body


def _clean(event: str, fields: dict) -> dict:
    out: dict = {}
    for key in ("reason", "outcome"):
        value = fields.get(key)
        if isinstance(value, str) and _CODE.fullmatch(value):
            out[key] = value
    method = fields.get("method")
    if isinstance(method, str):
        out["method"] = method if _METHOD.fullmatch(method) else "invalid"
    scheme = fields.get("scheme")
    if isinstance(scheme, str) and re.fullmatch(r"[a-z][a-z0-9+.\-]{0,15}", scheme):
        out["scheme"] = scheme
    host = fields.get("host")
    if isinstance(host, str) and _HOST.fullmatch(host):
        out["host"] = host.lower()
    return out


def clean_event(body) -> tuple[str, dict] | None:
    """服务端 /api/desktop/security_event 用：事件名在白名单里，字段只留白名单。"""
    if not isinstance(body, dict):
        return None
    event = body.get("event")
    if event not in EVENTS:
        return None
    return event, _clean(event, body)


def url_scheme_host(url) -> dict:
    got = _origin(url) if isinstance(url, str) else None
    if not got:
        return {"scheme": "invalid"}
    return {"scheme": got[0] or "none", "host": got[1] or None}


class Audit:
    """把桌面壳安全事件交给 sidecar 写遥测。发不出去就留在内存里（评测也读这里）。条数有上限，防刷屏。"""

    LIMIT = 200

    def __init__(self, base: str | None = None, token: str | None = None):
        self._base = base
        self._token = token
        self.events: list[dict] = []

    def record(self, event: str, **fields) -> dict:
        if event not in EVENTS:
            raise ValueError(event)
        entry = {"event": event, **_clean(event, fields)}
        if len(self.events) >= self.LIMIT:
            return entry
        self.events.append(entry)
        if self._base and self._token:
            data = json.dumps(entry).encode("utf-8")
            req = urllib.request.Request(self._base.rstrip("/") + "/api/desktop/security_event", data=data,
                                         method="POST", headers={"X-MAS-Desktop-Token": self._token,
                                                                 "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass
        return entry


class NavigationGuard:
    """WebView 每次加载完都核对当前地址；不在白名单就记一条并拉回 sidecar 首页。"""

    def __init__(self, base: str, audit: Audit, go_home):
        self._base = base.rstrip("/") + "/"
        self._audit = audit
        self._go_home = go_home

    def allowed(self, url) -> bool:
        return navigation_allowed(url, self._base)

    def check(self, url) -> bool:
        if self.allowed(url):
            return True
        self._audit.record("security.desktop_navigation_blocked", reason="not_sidecar_origin",
                           **url_scheme_host(url))
        try:
            self._go_home(self._base)
        except Exception:
            pass
        return False


class ExternalLinks:
    """外链：校验 → 宿主确认卡 → 系统浏览器。confirm(title, body) -> bool；open_browser(url)。"""

    def __init__(self, confirm, open_browser, audit: Audit):
        self._confirm = confirm
        self._open = open_browser
        self._audit = audit

    def open(self, url) -> dict:
        target = external_target(url)
        if target is None:
            self._audit.record("security.desktop_external_link_confirm", outcome="rejected",
                               reason="scheme_or_format", **url_scheme_host(url if isinstance(url, str) else ""))
            return {"ok": False, "code": "link_not_allowed"}
        title, body = confirm_text(target)
        try:
            yes = bool(self._confirm(title, body))
        except Exception:
            yes = False
        if not yes:
            self._audit.record("security.desktop_external_link_confirm", outcome="declined",
                               scheme=target["scheme"], host=target["host"])
            return {"ok": False, "code": "cancelled"}
        try:
            self._open(target["url"])
        except Exception:
            self._audit.record("security.desktop_external_link_confirm", outcome="open_failed",
                               scheme=target["scheme"], host=target["host"])
            return {"ok": False, "code": "open_failed"}
        self._audit.record("security.desktop_external_link_confirm", outcome="confirmed",
                           scheme=target["scheme"], host=target["host"])
        return {"ok": True}


def is_release(root: Path | None = None) -> bool:
    """发布构建：打包脚本在 apps/desktop/RELEASE 写标记，或环境变量 MAS_DESKTOP_RELEASE=1。"""
    if (os.environ.get("MAS_DESKTOP_RELEASE") or "").strip().lower() in ("1", "on", "true", "yes"):
        return True
    root = root or Path(__file__).resolve().parent.parent.parent
    return (root / "apps" / "desktop" / "RELEASE").is_file()


def devtools_enabled(root: Path | None = None) -> bool:
    """devtools 默认关；只有开发时 MAS_DESKTOP_DEVTOOLS=1 且不是发布构建才开。"""
    if is_release(root):
        return False
    return (os.environ.get("MAS_DESKTOP_DEVTOOLS") or "").strip().lower() in ("1", "on", "true", "yes")
