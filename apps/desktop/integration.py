"""桌面系统集成（13.B / #75）：原生文件对话框（pywebview js_api）、系统通知、托盘。

不变量：
- WebView JS 只能调用 DesktopApi 的白名单方法；令牌、地址、对话框函数都是下划线私有属性，
  pywebview 不会暴露给 JS。没有任何方法返回 API key、主密钥、启动令牌或本机路径。
- 原生选择的文件：Python 读字节，走现有 POST /api/runs/<id>/content（和网页 file input 同一个接口），
  服务端存进本地工件库、只给模型一个句柄；返回给 JS 的只有 ok / 错误码 / 字节数，不含路径和文件名，
  上传里也不带文件名。
- 通知文本只来自 apps/web/desktop_notify.py 的宿主模板（工具名 + 状态 + 张数）。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

RUN_ID = re.compile(r"r_[0-9a-f]{32}")
SESSION_ID = re.compile(r"s_[0-9a-f]{32}")
MAX_LOCAL_FILE = 262144  # 与 apps/web/store.MAX_FILE 一致
JS_METHODS = ("capabilities", "pick_local_file", "save_egress_export")


def _request(base: str, token: str, method: str, path: str, body: dict | None = None,
             timeout: float = 10.0) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"X-MAS-Desktop-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:
        return 0, b""


def _code(raw: bytes, default: str) -> str:
    try:
        code = json.loads(raw.decode("utf-8")).get("code")
    except Exception:
        code = None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else default


class DesktopApi:
    """pywebview js_api。公开方法 = JS_METHODS；其余全部私有。"""

    def __init__(self, base: str, token: str, open_dialog=None, save_dialog=None):
        self._base = base
        self._token = token
        self._open_dialog = open_dialog
        self._save_dialog = save_dialog

    def capabilities(self) -> dict:
        return {"native_file_dialog": self._open_dialog is not None,
                "native_save_dialog": self._save_dialog is not None}

    def pick_local_file(self, run_id) -> dict:
        """弹系统「打开文件」对话框，把选中的文件作为本机内容提交给等待中的运行。"""
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            return {"ok": False, "code": "invalid_run_id"}
        if self._open_dialog is None:
            return {"ok": False, "code": "dialog_unavailable"}
        try:
            path = self._open_dialog()
        except Exception:
            return {"ok": False, "code": "dialog_failed"}
        if not path:
            return {"ok": False, "code": "cancelled"}
        try:
            size = os.path.getsize(path)
            if size <= 0:
                return {"ok": False, "code": "empty"}
            if size > MAX_LOCAL_FILE:
                return {"ok": False, "code": "payload_too_large"}
            with open(path, "rb") as handle:
                data = handle.read(MAX_LOCAL_FILE + 1)
        except OSError:
            return {"ok": False, "code": "read_failed"}
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        # 只发字节和媒体类型；不发文件名和路径（服务端 submit_file 也不收）。
        status, raw = _request(self._base, self._token, "POST", f"/api/runs/{run_id}/content",
                               {"file": base64.b64encode(data).decode("ascii"), "media_type": media_type})
        if status != 200:
            return {"ok": False, "code": _code(raw, "upload_failed")}
        return {"ok": True, "bytes": len(data)}

    def save_egress_export(self, session_id=None) -> dict:
        """出站审计导出用系统「另存为」对话框（只导出这一种，白名单）。"""
        if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)):
            return {"ok": False, "code": "invalid_session_id"}
        if self._save_dialog is None:
            return {"ok": False, "code": "dialog_unavailable"}
        query = f"?session_id={session_id}" if session_id else ""
        status, raw = _request(self._base, self._token, "GET", f"/api/egress/export{query}")
        if status != 200:
            return {"ok": False, "code": _code(raw, "export_failed")}
        try:
            path = self._save_dialog(f"egress-{session_id or 'all'}.json")
        except Exception:
            return {"ok": False, "code": "dialog_failed"}
        if not path:
            return {"ok": False, "code": "cancelled"}
        try:
            with open(path, "wb") as handle:
                handle.write(raw)
        except OSError:
            return {"ok": False, "code": "write_failed"}
        return {"ok": True}


def public_surface(api: DesktopApi) -> dict:
    """pywebview 会暴露给 JS 的东西：不以下划线开头的属性 / 方法。评测用。"""
    out = {}
    for name in dir(api):
        if name.startswith("_"):
            continue
        out[name] = getattr(api, name)
    return out


def webview_dialogs(webview_module, window_ref: list):
    """用 pywebview 的原生对话框。window_ref[0] 在窗口创建后填入。"""
    file_dialog = getattr(webview_module, "FileDialog", None)
    open_kind = getattr(file_dialog, "OPEN", None) if file_dialog else None
    save_kind = getattr(file_dialog, "SAVE", None) if file_dialog else None
    if open_kind is None:
        open_kind = getattr(webview_module, "OPEN_DIALOG", 10)
    if save_kind is None:
        save_kind = getattr(webview_module, "SAVE_DIALOG", 30)

    def open_dialog():
        window = window_ref[0]
        result = window.create_file_dialog(open_kind, allow_multiple=False)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def save_dialog(default_name: str):
        window = window_ref[0]
        result = window.create_file_dialog(save_kind, save_filename=default_name)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    return open_dialog, save_dialog


# ---------- 通知 ----------

_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:MAS_NOTIFY_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:MAS_NOTIFY_BODY)) > $null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


def system_notify(title: str, body: str) -> bool:
    """没有托盘时的系统通知。文本经环境变量传给子进程，不拼进命令行。"""
    env = dict(os.environ, MAS_NOTIFY_TITLE=title, MAS_NOTIFY_BODY=body)
    try:
        if sys.platform == "win32":
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_PS], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        if sys.platform == "darwin":
            script = 'display notification (system attribute "MAS_NOTIFY_BODY") with title (system attribute "MAS_NOTIFY_TITLE")'
            subprocess.run(["osascript", "-e", script], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
            return True
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "--", title, body], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
            return True
    except Exception:
        return False
    return False


class NotifyPoller:
    """轮询 /api/desktop/notifications，交给 show(title, body)。只转发服务端模板生成的 title / body。"""

    def __init__(self, base: str, token: str, show, interval: float = 1.5):
        self._base = base
        self._token = token
        self._show = show
        self._interval = interval
        self._after = 0
        self._stop = threading.Event()
        self.enabled = True
        self._thread: threading.Thread | None = None

    def poll_once(self) -> list[dict]:
        status, raw = _request(self._base, self._token, "GET", f"/api/desktop/notifications?after={self._after}",
                               timeout=3.0)
        if status != 200:
            return []
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return []
        items = [item for item in data.get("items") or [] if isinstance(item, dict)]
        shown = []
        for item in items:
            self._after = max(self._after, int(item.get("id") or 0))
            title, body = str(item.get("title") or "")[:80], str(item.get("body") or "")[:200]
            if self.enabled and title and body:
                try:
                    self._show(title, body)
                except Exception:
                    pass
                shown.append({"title": title, "body": body})
        return shown

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self._interval):
                self.poll_once()

        self._thread = threading.Thread(target=loop, name="mas-notify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# ---------- 托盘 ----------

def tray_wanted() -> bool:
    return (os.environ.get("MAS_DESKTOP_TRAY") or "").strip().lower() not in ("0", "off", "false", "no")


def start_tray(on_show, on_quit, poller: NotifyPoller):
    """常驻托盘（可选，依赖 pystray + Pillow）。返回 icon 或 None。MAS_DESKTOP_TRAY=0 关掉。"""
    if not tray_wanted():
        return None
    try:
        import pystray  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:
        return None
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(37, 99, 235, 255))
    draw.text((18, 20), "MA", fill=(255, 255, 255, 255))

    def toggle(_icon, _item):
        poller.enabled = not poller.enabled

    menu = pystray.Menu(
        pystray.MenuItem("显示窗口", lambda _i, _m: on_show(), default=True),
        pystray.MenuItem("通知", toggle, checked=lambda _item: poller.enabled),
        pystray.MenuItem("退出", lambda _i, _m: on_quit()),
    )
    icon = pystray.Icon("multimodal-agent-studio", image, "Multimodal Agent Studio", menu)
    try:
        icon.run_detached()
    except Exception:
        threading.Thread(target=icon.run, name="mas-tray", daemon=True).start()
    return icon


def show_error(title: str, message: str) -> None:
    """启动失败时给用户看（pythonw 没有控制台）。"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
            return
        except Exception:
            pass
    print(f"{title}: {message}", file=sys.stderr)
