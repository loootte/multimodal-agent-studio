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
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
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
            if not token:
                continue
            break
    if not url or not token:
        proc.kill()
        raise SystemExit("sidecar 未在超时前就绪（无 MAS_READY）")
    return proc, url, base, token, server_pid


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
    proc, url, base, token, server_pid = _spawn_host(root, python)

    def cleanup():
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
            window = webview.create_window(
                "Multimodal Agent Studio",
                url,
                width=1280,
                height=860,
            )
            webview.start()
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
