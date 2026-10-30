"""Windows 可双击启动器（13.A 补充）：拉起 host.py sidecar，用 WebView2 开窗口。

用本机 Python 运行（快捷方式 / start-mas.cmd / build-launcher.ps1），不要 PyInstaller onefile 内嵌 python312.dll。
不依赖 Rust/MSVC。完整 Tauri 壳仍见 npm run build。
需要：本机 Python 3、仓库布局、可选 pywebview（有则内嵌窗口，无则系统浏览器）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _repo_root() -> Path:
    env = os.environ.get("MAS_REPO_ROOT")
    if env:
        p = Path(env).resolve()
        if (p / "apps/desktop/host.py").is_file():
            return p
    seeds = []
    if getattr(sys, "frozen", False):
        seeds.append(Path(sys.executable).resolve().parent)
    seeds.append(Path(__file__).resolve().parent)
    seeds.append(Path.cwd())
    for seed in seeds:
        cur = seed
        for _ in range(10):
            if (cur / "apps/desktop/host.py").is_file() and (cur / "apps/web/server.py").is_file():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
    raise SystemExit(
        "找不到仓库根（需含 apps/desktop/host.py）。"
        "请把本程序放在仓库的 apps/desktop/dist/ 下，或设置 MAS_REPO_ROOT。"
    )


def _console_python(path: str) -> str:
    """pythonw.exe -> 同目录 python.exe（sidecar 需要可用 stdout 管道）。"""
    p = Path(path)
    if p.name.lower() == "pythonw.exe":
        sib = p.with_name("python.exe")
        if sib.is_file():
            return str(sib)
    return str(p)


def _python_bin(root: Path) -> str:
    # 与入口脚本一致：MAS_PYTHON 优先，否则用运行本启动器的同一解释器，
    # 不再回落到 PATH 上的 python（用户 PATH 里可能没有，或是另一套环境）。
    env = os.environ.get("MAS_PYTHON")
    if env and Path(env).is_file():
        return _console_python(env)
    return _console_python(sys.executable)


class HostStartError(Exception):
    def __init__(self, message: str, commands: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.commands = commands or []


def _spawn_host(root: Path, python: str):
    host = root / "apps/desktop/host.py"
    creation = 0
    if sys.platform == "win32":
        creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        [python, str(host)],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation,
    )
    assert proc.stdout is not None
    deadline = time.time() + 45
    url = base = token = None
    server_pid = None
    data_dir = None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if line.startswith("MAS_ERROR "):
            # 13.B：宿主预检失败（例如数据目录还是明文、要先迁移）。不在这里改数据，只把提示给用户。
            import json

            try:
                info = json.loads(line[len("MAS_ERROR ") :])
            except ValueError:
                info = {}
            proc.wait(timeout=10)
            raise HostStartError(str(info.get("message") or "桌面宿主启动失败。"),
                                 [str(c) for c in info.get("commands") or []])
        if line.startswith("MAS_READY "):
            import json

            info = json.loads(line[len("MAS_READY ") :])
            # server.py 也会打 MAS_READY，但没有 base/server_pid；只认宿主完整行
            if not info.get("base") or not info.get("url"):
                continue
            url = info["url"]
            base = info["base"]
            token = url.split("desktop_token=", 1)[-1].split("&", 1)[0]
            server_pid = int(info.get("server_pid") or 0)
            data_dir = info.get("data_dir") or None
            if not token:
                continue
            break
    if not url or not token:
        proc.kill()
        raise SystemExit("sidecar 未在超时前就绪（无 MAS_READY）")
    return proc, url, base, token, server_pid, data_dir


def _http_stop(python: str, base: str, token: str) -> None:
    script = (
        "import urllib.request\n"
        f"req=urllib.request.Request('{base.rstrip('/')}/api/desktop/stop', data=b'{{}}', method='POST',\n"
        f"headers={{'X-MAS-Desktop-Token':'{token}','Content-Type':'application/json'}})\n"
        "urllib.request.urlopen(req, timeout=2)\n"
    )
    try:
        subprocess.run([python, "-c", script], timeout=5, check=False)
    except Exception:
        pass


def _force_kill(pid: int) -> None:
    if not pid:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def main() -> int:
    root = _repo_root()
    python = _python_bin(root)
    sys.path.insert(0, str(root / "apps/desktop"))
    import integration  # 13.B：原生对话框 / 通知 / 托盘
    import wizard  # 13.C：首次运行向导
    import updater  # 13.C：可选更新检查（默认关）
    import shell_guard  # 13.D：导航白名单 / 外链确认 / IPC 拒绝审计

    # 首次运行：选数据目录 / 确认加密 / 探测 ComfyUI / 可选 API key
    if (os.environ.get("MAS_SKIP_WIZARD") or "").strip().lower() not in ("1", "on", "true", "yes"):
        try:
            if wizard.needs_wizard():
                wizard.ensure_done(gui=True)
        except wizard.WizardError as exc:
            integration.show_error("首次运行向导", exc.message)
            return 2
        except Exception as exc:
            # 无显示设备等：退回非 GUI 默认（确认加密、跳过 key）
            try:
                if wizard.needs_wizard():
                    wizard.apply(encryption_confirmed=True, skip_api_key=True, probe=False)
            except Exception:
                integration.show_error("首次运行向导", str(exc))
                return 2

    # 自动更新默认关；开启时只发 version+platform
    try:
        updater.maybe_check()
    except Exception:
        pass

    try:
        proc, url, base, token, server_pid, data_dir = _spawn_host(root, python)
    except HostStartError as exc:
        text = exc.message + ("\n\n" + "\n".join(exc.commands) if exc.commands else "")
        integration.show_error("Multimodal Agent Studio 无法启动", text)
        return 2
    tray_ref: list = [None]

    def notify(title: str, body: str) -> None:
        icon = tray_ref[0]
        if icon is not None:
            try:
                icon.notify(body, title)
                return
            except Exception:
                pass
        integration.system_notify(title, body)

    poller = integration.NotifyPoller(base, token, notify)
    if (os.environ.get("MAS_DESKTOP_NOTIFY") or "").strip().lower() not in ("0", "off", "false", "no"):
        poller.start()

    def cleanup():
        poller.stop()
        if tray_ref[0] is not None:
            try:
                tray_ref[0].stop()
            except Exception:
                pass
        _http_stop(python, base, token)
        if server_pid:
            _force_kill(server_pid)
        if proc.poll() is None:
            _force_kill(proc.pid)
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

    use_webview = False
    try:
        import webview  # type: ignore

        use_webview = True
    except Exception:
        use_webview = False

    try:
        if use_webview:
            import webbrowser

            window_ref: list = [None]
            open_dialog, save_dialog = integration.webview_dialogs(webview, window_ref)
            audit = shell_guard.Audit(base, token)
            links = shell_guard.ExternalLinks(integration.webview_confirm(window_ref), webbrowser.open, audit)

            def current_url():
                window = window_ref[0]
                return window.get_current_url() if window is not None else None

            # js_api 只有白名单方法（选本机文件 / 另存出站审计 / 外链确认 / 打开数据目录）；
            # 令牌等都是私有属性，JS 拿不到；别的调用拒绝并审计。
            api = integration.DesktopApi(base, token, open_dialog, save_dialog, audit=audit, links=links,
                                         current_url=current_url,
                                         open_folder=(lambda: integration.open_folder(data_dir)) if data_dir else None)
            # 外链由页面里的点击拦截交给 open_external（宿主确认卡）。pywebview 自带的
            # OPEN_EXTERNAL_LINKS_IN_BROWSER 保持默认开：万一拦截没生效，target=_blank 也只会去系统浏览器，
            # 不会在新的 WebView 窗口里打开外站。
            window = webview.create_window(
                "Multimodal Agent Studio",
                url,
                width=1280,
                height=860,
                js_api=api,
            )
            window_ref[0] = window
            guard = shell_guard.NavigationGuard(base, audit, lambda home: window.load_url(home))

            def on_loaded(*_args):
                try:
                    guard.check(window.get_current_url())
                except Exception:
                    pass

            try:
                window.events.loaded += on_loaded
            except Exception:
                pass
            quitting = {"now": False}

            def on_show():
                try:
                    window.show()
                    window.restore()
                except Exception:
                    pass

            def on_quit():
                quitting["now"] = True
                try:
                    window.destroy()
                except Exception:
                    pass

            def start_tray():
                tray_ref[0] = integration.start_tray(on_show, on_quit, poller)

            def on_closing():
                # 托盘常驻：关窗口只是隐藏，从托盘「退出」才真正结束。没有托盘就照常退出。
                if tray_ref[0] is not None and not quitting["now"]:
                    try:
                        window.hide()
                        return False
                    except Exception:
                        return True
                return True

            try:
                window.events.closing += on_closing
            except Exception:
                pass
            webview.start(start_tray, debug=shell_guard.devtools_enabled(root))
        else:
            import webbrowser

            webbrowser.open(url)
            print("已在系统浏览器打开：", url)
            print("本机服务运行中。关闭本窗口/按 Ctrl+C 结束。")
            try:
                while proc.poll() is None:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
