"""桌面壳 sidecar 宿主（13.A / #74）。

负责：单实例锁、挑空闲端口、生成启动令牌、拉起 apps/web/server.py（只绑 127.0.0.1）、
关停时先 /api/desktop/stop 再杀进程。Tauri 壳和本文件的无窗口冒烟入口都走这里。

用法（仓库根目录）：

    python apps/desktop/host.py                  # 前台跑，Ctrl+C 退出
    python apps/desktop/host.py --ready-file r.json --daemon-wait
    python apps/desktop/check.py                 # 无 GUI 冒烟
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
WEB_SERVER = ROOT / "apps" / "web" / "server.py"
DEFAULT_LOCK = Path(os.environ.get("MAS_DESKTOP_LOCK") or (Path.home() / ".mas" / "desktop.lock"))
TOKEN_BYTES = 32


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def new_token() -> str:
    return os.urandom(TOKEN_BYTES).hex()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows：pid 不存在时常是 OSError
        if sys.platform == "win32":
            try:
                import ctypes

                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:
                return False
        return False
    else:
        return True


def _lock_holder_alive(data: dict) -> int | None:
    """若锁对应的 sidecar 还在，返回其 server_pid（或宿主 pid）；否则 None。"""
    for key in ("server_pid", "pid"):
        try:
            pid = int(data.get(key) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid and pid_alive(pid):
            return pid
    return None


def acquire_lock(path: Path, port: int, token_fp: str, server_pid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            data = {}
        alive = _lock_holder_alive(data if isinstance(data, dict) else {})
        if alive:
            raise SystemExit(
                f"桌面壳已在运行（pid={alive}，锁文件 {path}）。先关掉已有窗口，或删掉过期锁文件。"
            )
    payload = {
        "schema": "mas.desktop.lock/1",
        "pid": os.getpid(),
        "server_pid": server_pid,
        "port": port,
        "token_fp": token_fp,
        "started_at": int(time.time()),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def release_lock(path: Path, server_pid: int | None = None) -> None:
    try:
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        lock_server = int(data.get("server_pid") or 0)
        lock_pid = int(data.get("pid") or 0)
        mine = lock_pid in (0, os.getpid()) or (server_pid and lock_server == server_pid)
        if mine:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    except (ValueError, json.JSONDecodeError, TypeError):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _request(method: str, url: str, token: str, timeout: float = 2.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        method=method,
        headers={"X-MAS-Desktop-Token": token, "Content-Type": "application/json"},
        data=b"{}" if method == "POST" else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def wait_ready(port: int, token: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/"
    last = ""
    while time.time() < deadline:
        status, body = _request("GET", url, token, timeout=1.0)
        if status == 200 and ("发送" in body or "<!DOCTYPE" in body or "<html" in body.lower()):
            return
        last = f"{status} {body[:120]}"
        time.sleep(0.1)
    raise SystemExit(f"sidecar 在 {timeout:.0f}s 内没有就绪：{last}")


class Sidecar:
    def __init__(
        self,
        *,
        data_dir: str | None = None,
        port: int | None = None,
        token: str | None = None,
        lock_path: Path | None = None,
        python: str | None = None,
    ):
        self.port = int(port) if port else free_port()
        self.token = token or new_token()
        self.data_dir = data_dir
        self.lock_path = Path(lock_path) if lock_path else DEFAULT_LOCK
        self.python = python or os.environ.get("MAS_PYTHON") or sys.executable
        self.proc: subprocess.Popen | None = None
        self._locked = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?desktop_token={self.token}"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> dict:
        if not WEB_SERVER.is_file():
            raise SystemExit(f"找不到 Web 服务：{WEB_SERVER}")
        # 先拒绝「别人的活锁」，真正写锁等到 sidecar 起来之后（带上 server_pid）。
        if self.lock_path.is_file():
            try:
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                data = {}
            alive = _lock_holder_alive(data if isinstance(data, dict) else {})
            if alive:
                raise SystemExit(
                    f"桌面壳已在运行（pid={alive}，锁文件 {self.lock_path}）。先关掉已有窗口，或删掉过期锁文件。"
                )
        ready_file = tempfile.NamedTemporaryFile(prefix="mas-ready-", suffix=".json", delete=False)
        ready_path = ready_file.name
        ready_file.close()
        try:
            os.unlink(ready_path)
        except OSError:
            pass
        cmd = [
            self.python,
            str(WEB_SERVER),
            "--port",
            str(self.port),
            "--desktop-token",
            self.token,
            "--ready-file",
            ready_path,
        ]
        if self.data_dir:
            cmd.extend(["--data-dir", self.data_dir])
        env = os.environ.copy()
        env["MAS_DESKTOP_TOKEN"] = self.token
        # Windows：用 CREATE_NEW_PROCESS_GROUP 方便后来发 CTRL_BREAK；Unix 默认进程组即可。
        kwargs: dict = {
            "cwd": str(ROOT),
            "env": env,
            # 避免 server 的 MAS_READY 混进宿主 stdout（Tauri/启动器只认宿主这一行）
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        self.proc = subprocess.Popen(cmd, **kwargs)
        try:
            wait_ready(self.port, self.token)
        except SystemExit:
            self.stop()
            raise
        acquire_lock(self.lock_path, self.port, self.token[:8], server_pid=self.proc.pid)
        self._locked = True
        info = {
            "schema": "mas.desktop.ready/1",
            "host": "127.0.0.1",
            "port": self.port,
            "url": self.url,
            "base": self.base,
            "token_required": True,
            "pid": os.getpid(),
            "server_pid": self.proc.pid,
            "lock": str(self.lock_path),
        }
        return info

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                _request("POST", f"{self.base}/api/desktop/stop", self.token, timeout=2.0)
            except Exception:
                pass
            pid = self.proc.pid
            if sys.platform == "win32":
                # /T 连同子进程一起结束，避免关窗后残留 server.py
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    self.proc.terminate()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
        self.proc = None
        if self._locked:
            release_lock(self.lock_path, server_pid=None)
            self._locked = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAS 桌面 sidecar 宿主")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--port", type=int, default=0, help="0 = 挑空闲端口")
    parser.add_argument("--token", default=None, help="默认随机生成")
    parser.add_argument("--lock-file", default=None)
    parser.add_argument("--ready-file", default=None, help="就绪后写入 JSON，给 Tauri 读")
    parser.add_argument("--print-url", action="store_true", help="只打印可打开的 URL（含令牌）")
    args = parser.parse_args(argv)

    side = Sidecar(
        data_dir=args.data_dir,
        port=args.port or None,
        token=args.token,
        lock_path=Path(args.lock_file) if args.lock_file else None,
    )
    info = side.start()
    line = json.dumps(info, ensure_ascii=False)
    print(f"MAS_READY {line}", flush=True)
    if args.ready_file:
        Path(args.ready_file).write_text(line, encoding="utf-8")
    if args.print_url:
        print(info["url"], flush=True)

    stop_once = threading.Event()

    def _shutdown(*_args) -> None:
        if stop_once.is_set():
            return
        stop_once.set()
        side.stop()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not stop_once.is_set():
            if side.proc is not None and side.proc.poll() is not None:
                break
            time.sleep(0.3)
    finally:
        _shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
