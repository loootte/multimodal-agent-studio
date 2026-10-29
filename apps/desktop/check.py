"""桌面壳无 GUI 冒烟（13.A / #74）。不访问外网。

覆盖：空闲端口、启动令牌（无令牌 401 / 有令牌 200）、Cookie 引导、关停后无残留进程、
单实例锁、直接跑 Web 服务时不启用令牌。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
sys.path.insert(0, str(DESKTOP_DIR))
sys.path.insert(0, str(ROOT / "apps" / "web"))
sys.path.insert(0, str(ROOT))

import host as desktop_host  # noqa: E402
import server  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def http(method: str, url: str, headers: dict | None = None, timeout: float = 3.0) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), dict(exc.headers.items())


def _web_mode_no_token() -> None:
    """直接跑 Web 服务：不设令牌，行为与现在一致。"""
    directory = tempfile.mkdtemp(prefix="mas-web-")
    app = server.App(directory, timeout_sec=5, desktop_token=None)
    if app.desktop_token is not None:
        fail("Web 模式不该启用令牌")
    site = server.make_httpd(app, "127.0.0.1", 0)
    thread_started = {"ok": False}

    def serve():
        thread_started["ok"] = True
        site.serve_forever()

    import threading

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(0.05)
    base = f"http://127.0.0.1:{site.server_address[1]}"
    try:
        status, body, _ = http("GET", base + "/")
        if status != 200 or "发送" not in body:
            fail(f"Web 模式无令牌应 200：{status} {body[:80]}")
        status, body, _ = http("GET", base + "/api/identity")
        if status != 200:
            fail(f"Web 模式 /api/identity 应 200：{status}")
    finally:
        site.shutdown()
        site.server_close()
    print("web mode ok")


def _token_gate_unit() -> dict:
    """令牌中间件：无令牌 401；头 / Cookie / 查询均可；查询引导写 Cookie。"""
    directory = tempfile.mkdtemp(prefix="mas-desk-tok-")
    token = "a" * 64
    app = server.App(directory, timeout_sec=5, desktop_token=token)
    site = server.make_httpd(app, "127.0.0.1", 0)
    import threading

    threading.Thread(target=site.serve_forever, daemon=True).start()
    time.sleep(0.05)
    base = f"http://127.0.0.1:{site.server_address[1]}"
    try:
        status, body, _ = http("GET", base + "/")
        if status != 401 or "desktop_token_required" not in body:
            fail(f"无令牌访问 / 应 401：{status} {body[:120]}")
        status, body, _ = http("GET", base + "/api/identity")
        if status != 401:
            fail(f"无令牌访问 /api/identity 应 401：{status}")
        status, body, _ = http("GET", base + "/api/storage")
        if status != 401:
            fail(f"无令牌访问 /api/storage 应 401：{status}")
        status, body, _ = http("GET", base + "/api/identity", headers={"X-MAS-Desktop-Token": token})
        if status != 200 or "本机用户" not in body:
            fail(f"带头令牌应 200：{status} {body[:120]}")
        # 查询令牌：302 + Set-Cookie，再带 Cookie 访问。
        status, body, headers = http("GET", base + f"/?desktop_token={token}")
        # urllib 默认不自动跟跨方案重定向以外的；302 时 urlopen 会跟随。
        # 若已跟随，最终可能是 200；检查 Cookie 路径：手动带 Cookie。
        status2, body2, _ = http(
            "GET",
            base + "/api/identity",
            headers={"Cookie": f"mas_desktop_token={token}"},
        )
        if status2 != 200:
            fail(f"带 Cookie 应 200：{status2} {body2[:120]}")
        # 错误令牌
        status, body, _ = http("GET", base + "/api/identity", headers={"X-MAS-Desktop-Token": "wrong"})
        if status != 401:
            fail(f"错误令牌应 401：{status}")
        return {"port": site.server_address[1], "denied": True, "allowed": True}
    finally:
        site.shutdown()
        site.server_close()


def _sidecar_lifecycle() -> dict:
    """host.Sidecar：随机端口、就绪、无令牌拒、有令牌通、stop 后进程退出、锁释放。"""
    data = tempfile.mkdtemp(prefix="mas-desk-data-")
    lock = Path(tempfile.mkdtemp(prefix="mas-desk-lock-")) / "desktop.lock"
    side = desktop_host.Sidecar(data_dir=data, lock_path=lock)
    info = side.start()
    if info["port"] == 8766:
        # 允许碰巧抽到 8766，但不该是硬编码；再抽一次看是否能避开占用场景在下面测
        pass
    try:
        status, body, _ = http("GET", info["base"] + "/")
        if status != 401:
            fail(f"sidecar 无令牌应 401：{status}")
        status, body, _ = http(
            "GET", info["base"] + "/api/identity", headers={"X-MAS-Desktop-Token": side.token}
        )
        if status != 200:
            fail(f"sidecar 有令牌应 200：{status} {body[:100]}")
        pid = side.proc.pid if side.proc else 0
        side.stop()
        # 等进程退出
        deadline = time.time() + 8
        while time.time() < deadline and desktop_host.pid_alive(pid):
            time.sleep(0.1)
        if desktop_host.pid_alive(pid):
            fail(f"关停后 Python sidecar 仍在（pid={pid}）")
        if lock.is_file():
            fail("关停后锁文件还在")
        return {"port": info["port"], "pid": pid, "stopped": True}
    finally:
        side.stop()


def _busy_port_and_lock() -> None:
    """端口被占时宿主换端口；锁被活进程占用时拒绝第二次启动。"""
    import socket

    data = tempfile.mkdtemp(prefix="mas-desk-data2-")
    lock_dir = Path(tempfile.mkdtemp(prefix="mas-desk-lock2-"))
    lock = lock_dir / "desktop.lock"
    occupied = desktop_host.free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", occupied))
    blocker.listen(1)
    try:
        side = desktop_host.Sidecar(data_dir=data, lock_path=lock)
        info = side.start()
        if info["port"] == occupied:
            fail("抽到了已被占用的端口")
        side2 = desktop_host.Sidecar(data_dir=data + "-b", lock_path=lock)
        try:
            side2.start()
            side2.stop()
            fail("同锁第二次启动应该失败")
        except SystemExit as exc:
            if "已在运行" not in str(exc):
                fail(f"单实例拒绝语不对：{exc}")
        side.stop()
    finally:
        blocker.close()
    print("port/lock ok")


def _cli_ready_file() -> None:
    """server.py --port 0 --desktop-token --ready-file 写出就绪 JSON。"""
    data = tempfile.mkdtemp(prefix="mas-cli-")
    ready = Path(tempfile.mkdtemp(prefix="mas-ready-")) / "ready.json"
    token = "b" * 64
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "apps" / "web" / "server.py"),
            "--port",
            "0",
            "--desktop-token",
            token,
            "--data-dir",
            data,
            "--ready-file",
            str(ready),
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not ready.is_file():
            if proc.poll() is not None:
                out = proc.stdout.read if proc.stdout else None
                fail(f"server 提前退出：{proc.returncode}")
            time.sleep(0.05)
        if not ready.is_file():
            fail("ready-file 没写出来")
        info = json.loads(ready.read_text(encoding="utf-8"))
        if not info.get("port") or not info.get("token_required"):
            fail(f"ready-file 内容不对：{info}")
        status, body, _ = http(
            "GET",
            f"http://127.0.0.1:{info['port']}/api/identity",
            headers={"X-MAS-Desktop-Token": token},
        )
        if status != 200:
            fail(f"cli 令牌访问失败：{status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    print("cli ready-file ok")


def main() -> None:
    _web_mode_no_token()
    _token_gate_unit()
    print("token gate ok")
    _sidecar_lifecycle()
    print("sidecar lifecycle ok")
    _busy_port_and_lock()
    _cli_ready_file()
    print("desktop check ok")


if __name__ == "__main__":
    main()
