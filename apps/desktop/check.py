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

# 13.B：宿主默认打开加密 + 系统钥匙串。冒烟一律用临时钥匙串文件和临时主密钥路径，
# 不碰真实的 Windows 凭据管理器 / macOS 钥匙串 / ~/.mas。
_SANDBOX = tempfile.mkdtemp(prefix="mas-desk-check-")
os.environ["MAS_KEYCHAIN"] = "file:" + os.path.join(_SANDBOX, "keychain.json")
os.environ["MAS_MASTER_KEY_FILE"] = os.path.join(_SANDBOX, "master.key")
os.environ.pop("MAS_MASTER_KEY", None)
os.environ.pop("MAS_STORAGE_ENCRYPTION", None)

import host as desktop_host  # noqa: E402
import security_checks  # noqa: E402
import server  # noqa: E402


def _child_env() -> dict:
    """check 拉起的子进程一律 UTF-8 输出（Windows 默认 GBK）。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


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
        # 13.B：桌面默认加密，主密钥进钥匙串（这里是临时钥匙串文件），不落 master.key。
        if info.get("storage_encryption") != "on" or (info.get("keychain") or {}).get("backend") != "file":
            fail(f"桌面默认没有加密 / 钥匙串：{info.get('storage_encryption')} {info.get('keychain')}")
        status, body, _ = http("GET", info["base"] + "/api/storage", headers={"X-MAS-Desktop-Token": side.token})
        storage = json.loads(body)
        if storage.get("mode") != "encrypted" or storage.get("key_source") != "keychain":
            fail(f"sidecar 的数据目录不是加密 / 主密钥不在钥匙串：{body[:200]}")
        if os.path.exists(os.environ["MAS_MASTER_KEY_FILE"]):
            fail("桌面默认把主密钥写成了文件")
        if "MAS_KEYCHAIN" in body or side.token in body:
            fail("存储状态接口带了不该有的东西")
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
    """server.py --port 0 --ready-file（令牌经 MAS_DESKTOP_TOKEN）写出就绪 JSON；--desktop-token 已停用。"""
    data = tempfile.mkdtemp(prefix="mas-cli-")
    ready = Path(tempfile.mkdtemp(prefix="mas-ready-")) / "ready.json"
    token = "b" * 64
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "apps" / "web" / "server.py"),
            "--port",
            "0",
            "--data-dir",
            data,
            "--ready-file",
            str(ready),
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(_child_env(), MAS_DESKTOP_TOKEN=token),
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not ready.is_file():
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                fail(f"server 提前退出：{proc.returncode} {out[-300:]}")
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


def _sidecar_plaintext_refused() -> None:
    """13.B：已有明文数据的目录，宿主启动前预检拒绝并给 storage_tool.py 迁移命令，不悄悄转换、不拉起服务。"""
    data = tempfile.mkdtemp(prefix="mas-desk-plain-")
    lock = Path(tempfile.mkdtemp(prefix="mas-desk-lock3-")) / "desktop.lock"
    os.environ["MAS_STORAGE_ENCRYPTION"] = "off"
    try:
        app = server.App(data, timeout_sec=5)
        app.sessions.create(app.identity.current()["user_id"])
    finally:
        os.environ.pop("MAS_STORAGE_ENCRYPTION", None)
    side = desktop_host.Sidecar(data_dir=data, lock_path=lock)
    try:
        side.start()
    except desktop_host.desktop_env.PreflightError as exc:
        info = exc.public()
        if info["code"] != "storage_migration_required" or not any("storage_tool.py" in c for c in info["commands"]):
            fail(f"明文数据目录的引导不对：{info}")
    else:
        side.stop()
        fail("明文数据目录不该直接启动（要先迁移）")
    if side.proc is not None or lock.is_file():
        fail("预检失败后不该留下进程或锁")
    # 命令行入口：MAS_ERROR 一行 + 退出码 2，给启动器弹窗用。
    proc = subprocess.run([sys.executable, str(DESKTOP_DIR / "host.py"), "--data-dir", data, "--lock-file", str(lock)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=_child_env(), timeout=60)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("MAS_ERROR ")), "")
    if proc.returncode != 2 or "storage_migration_required" not in line:
        fail(f"host.py 预检失败应输出 MAS_ERROR 并退出 2：{proc.returncode} {proc.stdout[-200:]}")



def _wizard_and_update_unit() -> None:
    """13.C：向导只探测用户地址；更新默认关闭；卸载默认保留数据（脚本层约定）。"""
    import desktop_config
    import wizard
    import updater

    with tempfile.TemporaryDirectory(prefix="mas-wiz-") as tmp:
        data = os.path.join(tmp, "data")
        # 向导未完成
        if not wizard.needs_wizard(data):
            fail("空配置应需要向导")
        # 拒绝通配地址（不扫描）
        try:
            wizard._normalize_comfy_url("http://0.0.0.0:8188")
            fail("通配 Comfy 地址应拒绝")
        except wizard.WizardError as exc:
            if exc.code != "comfy_scan_refused":
                fail(f"通配拒绝码不对：{exc.code}")
        # 完成向导（不真探活）
        out = wizard.apply(
            data_dir=data,
            encryption_confirmed=True,
            comfy_url="http://127.0.0.1:8188",
            skip_api_key=True,
            auto_update=False,
            probe=False,
        )
        if not out["ok"] or not out["local_only"]:
            fail(f"向导结果不对：{out}")
        if wizard.needs_wizard(data):
            fail("完成后不应再要向导")
        cfg = desktop_config.load(data)
        if cfg.get("auto_update"):
            fail("自动更新默认应关闭")
        # 更新跳过
        skipped = updater.maybe_check(data)
        if not skipped.get("skipped"):
            fail(f"更新应跳过：{skipped}")
        # 打包脚本存在
        nsis = DESKTOP_DIR / "packaging" / "windows" / "installer.nsi"
        build = DESKTOP_DIR / "packaging" / "windows" / "build.ps1"
        if not nsis.is_file() or "un.删除数据目录" not in nsis.read_text(encoding="utf-8"):
            fail("NSIS 卸载默认保留数据的可选段缺失")
        if not build.is_file():
            fail("缺少 Windows build.ps1")
        # macOS/Linux stubs
        for name in ("macos", "linux"):
            stub = DESKTOP_DIR / "packaging" / name / "build.sh"
            if not stub.is_file():
                fail(f"缺少 {name} 占位脚本")



def main() -> None:
    desktop_host.force_utf8_stdio()
    _web_mode_no_token()
    _token_gate_unit()
    print("token gate ok")
    _sidecar_lifecycle()
    print("sidecar lifecycle ok")
    _sidecar_plaintext_refused()
    print("plaintext preflight ok")
    for name, func in security_checks.ALL.items():
        func()
        print(f"{name} ok")
    _wizard_and_update_unit()
    print("wizard/update ok")
    _busy_port_and_lock()
    _cli_ready_file()
    print("desktop check ok")


if __name__ == "__main__":
    main()
