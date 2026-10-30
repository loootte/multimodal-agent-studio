"""第三方工具包降权执行（路线图 12.D #60，ADR 0009）。

内置工具（entry.kind=host 或仓库里的 python 模块）仍在宿主进程里跑，由 services.runtime.tools.authorize 按清单核对权限。
安装的第三方包一律在子进程里跑（services/tools/sandbox_runner.py）：

- 环境变量只有白名单（ENV_KEEP），用户 shell 里的 API key、令牌一概不继承；工具配置（含密钥）由宿主显式注入。
- 没授权 network：所有 socket 连接和域名解析被拦（另设 HTTP(S)_PROXY 指向拒绝地址兜底）；
  授权了 network：每次连接先问宿主，宿主按出站策略决定，并记 12.A 出站审计（渠道 toolpack）。
  回环地址（包括本机 MAS 接口和 ComfyUI）一律拒绝，第三方包不能绕过宿主去读本机接口。
- 写文件只许落在本次调用的临时目录；授权了 write_files 再加上包自己的数据目录 <data_dir>/toolpack_data/<name>。
- 数据目录（会话、句柄、配置、审计）不许读写，本地内容只能经宿主的句柄 API。
- 起子进程、加载原生库一律拒绝；超时杀进程。

任何越权尝试都让这次调用返回 permission_denied（带 violations 种类，不带路径或正文），并记遥测 security.sandbox_denied。
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "sandbox_runner.py")
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SCHEMA = "mas.sandbox/1"
MODE = "subprocess"
# 子进程只继承这些环境变量（Windows 上 Winsock 和随机数需要 SYSTEMROOT）。其余一律不带，包括 *_API_KEY、*_TOKEN。
ENV_KEEP = ("SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
DENY_PROXY = "http://127.0.0.1:9"
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
MAX_OUTPUT = 1024 * 1024
MAX_AUDIT = 20
VIOLATIONS = ("network", "write_files", "read_local_content", "process", "native")
VIOLATION_TEXT = {
    "network": "没有联网权限却试图联网（或连本机回环地址）",
    "write_files": "试图写临时目录和自己数据目录以外的文件",
    "read_local_content": "试图直接读取数据目录（本地内容只能经句柄 API）",
    "process": "试图启动子进程",
    "native": "试图加载原生库（ctypes）",
}

# 由网页宿主设置（apps/web/server.py）：数据目录。没设置时用仓库下的 data/ 兜底，一样不许读。
DATA_DIR: str | None = None


def data_dirs() -> list[str]:
    found = []
    # 12.F（#62）：放主密钥的目录也禁读（和 packages/artifacts/crypto.key_dirs 同一套规则）。
    key_file = os.environ.get("MAS_MASTER_KEY_FILE")
    key_dirs = [os.path.dirname(os.path.abspath(os.path.expanduser(key_file)))] if key_file else []
    key_dirs.append(os.path.join(os.path.expanduser("~"), ".mas"))
    for path in (DATA_DIR, os.environ.get("MAS_DATA_DIR"), os.path.join(ROOT, "data"), *key_dirs):
        if path:
            full = os.path.realpath(os.path.abspath(path))
            if full not in found:
                found.append(full)
    return found


def pack_data_dir(name: str) -> str | None:
    base = DATA_DIR or os.environ.get("MAS_DATA_DIR")
    if not base:
        return None
    return os.path.join(os.path.realpath(base), "toolpack_data", name)


def _site_paths() -> list[str]:
    """-I 会关掉用户级 site-packages；包要的第三方库（requires.python）可能装在那里，显式补上。"""
    try:
        import site

        user = site.getusersitepackages()
    except (AttributeError, ImportError):
        return []
    return [user] if isinstance(user, str) and os.path.isdir(user) else []


def timeout() -> float:
    try:
        value = float(os.environ.get("MAS_TOOLPACK_TIMEOUT_SEC") or DEFAULT_TIMEOUT)
    except ValueError:
        value = DEFAULT_TIMEOUT
    return max(1.0, min(MAX_TIMEOUT, value))


def child_env(scratch: str, network: bool) -> dict:
    env = {key: os.environ[key] for key in ENV_KEEP if os.environ.get(key)}
    env.update({
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": scratch, "TEMP": scratch, "TMP": scratch, "HOME": scratch, "USERPROFILE": scratch,
        "MAS_SANDBOX": SCHEMA,
    })
    if not network:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = DENY_PROXY
        env["NO_PROXY"] = env["no_proxy"] = ""
    return env


def _is_loopback(host: str) -> bool:
    text = (host or "").strip("[]").split("%")[0].lower()
    if text in ("", "localhost", "unix") or text.endswith(".localhost"):
        return True
    try:
        addresses = [ipaddress.ip_address(text)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in socket.getaddrinfo(text, None)]
        except (OSError, UnicodeError, ValueError):
            return True  # 解析不了就当可疑，不放行
    return any(a.is_loopback or a.is_unspecified or a.is_link_local or
               (getattr(a, "ipv4_mapped", None) is not None and a.ipv4_mapped.is_loopback) for a in addresses)


def decide(tool: str, granted: set, host: str, port) -> tuple[bool, str | None]:
    """宿主对子进程一次出网的裁决。返回 (放行, 原因码)。"""
    if "network" not in granted:
        return False, "permission_denied"
    if _is_loopback(host):
        return False, "egress_policy_denied"
    return True, None


def _audit(tool: str, host: str, port, allowed: bool, reason: str | None) -> None:
    try:
        from services.gateway import egress
    except ImportError:  # poc 单独跑（没有 services 包）时不记
        return
    egress.toolpack_connect(tool, host, allowed=allowed, reason_code=reason)


def _security(tool: str, kinds: list[str]) -> None:
    try:
        from services import telemetry
    except ImportError:
        return
    for kind in kinds:
        try:
            telemetry.record("security.sandbox_denied", tool=tool, error_code="permission_denied", violation=kind)
        except Exception as exc:  # 遥测失败不改变拦截结果
            print(f"警告：沙箱安全事件写入失败 {type(exc).__name__}", file=sys.stderr)


def _denied(tool: str, kinds: list[str]) -> dict:
    kinds = [k for k in VIOLATIONS if k in kinds]
    text = "；".join(VIOLATION_TEXT[k] for k in kinds)
    # 越权前包可能已经跑了一部分，所以不写 executed=False。
    return {"ok": False, "tool": tool,
            "error": {"code": "permission_denied", "message": f"工具越权，已拦下：{text}。", "violations": kinds}}


def _error(tool: str, code: str, message: str) -> dict:
    return {"ok": False, "tool": tool, "error": {"code": code, "message": message}}


FUNCTIONS = ("call", "catalog", "preview", "run", "mcp")  # mcp：14.A stdio MCP server，一次喂一批 JSON-RPC 消息


def run(pack, arguments: dict, config: dict, granted: set, function: str = "call") -> dict:
    """在子进程里调用第三方包的 call(arguments, config)。granted 是宿主核对过的生效权限（清单 ∩ 授权）。
    #86：entry.kind=module 的表单工具按工具代码契约调用 catalog(ctx) / preview(fields, ctx) / run(args, ctx)，
    这时 config 位置传的是宿主给的 ctx（纯 JSON）。"""
    if function not in FUNCTIONS:
        return _error(pack.name, "tool_error", f"不能调用 {function}。")
    name = pack.name
    scratch = tempfile.mkdtemp(prefix="mas-sbx-")
    write_roots = [scratch]
    own = pack_data_dir(name)
    if "write_files" in granted and own:
        os.makedirs(own, exist_ok=True)
        write_roots.append(own)
    spec = {
        "module": pack.manifest["entry"]["module"], "pack_dir": pack.folder,
        "arguments": arguments, "config": config, "permissions": sorted(granted),
        "write_roots": write_roots, "deny_roots": data_dirs(), "scratch": scratch,
        "extra_paths": _site_paths(), "function": function,
    }
    env = child_env(scratch, "network" in granted)
    if len(write_roots) > 1:
        env["MAS_TOOLPACK_DATA"] = write_roots[1]  # 包自己的数据目录（授权了 write_files 才有）
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", RUNNER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=scratch, env=env,
            text=True, encoding="utf-8", errors="replace", creationflags=flags,
        )
    except OSError as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        return _error(name, "sandbox_error", f"沙箱启动失败（{type(exc).__name__}）。")
    lines: queue.Queue = queue.Queue()

    def pump():
        try:
            size = 0
            for line in proc.stdout:
                size += len(line)
                if size > MAX_OUTPUT:
                    lines.put({"t": "error", "error": "OutputTooLarge"})
                    break
                try:
                    lines.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            lines.put(None)

    def drain():  # 包自己的输出只丢弃，不进日志（可能含任何东西）
        for _ in proc.stderr:
            pass

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=drain, daemon=True).start()
    deadline = threading.Event()
    timer = threading.Timer(timeout(), lambda: (deadline.set(), proc.kill()))
    timer.daemon = True
    timer.start()
    final = None
    audited = 0
    kinds: list[str] = []
    try:
        proc.stdin.write(json.dumps(spec, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        while True:
            message = lines.get()
            if message is None:
                break
            if message.get("t") == "egress":
                host = str(message.get("host") or "")
                allowed, reason = decide(name, granted, host, message.get("port"))
                if audited < MAX_AUDIT:
                    _audit(name, host, message.get("port"), allowed, reason)
                    audited += 1
                if not allowed and "network" not in kinds:
                    kinds.append("network")
                try:
                    proc.stdin.write(json.dumps({"allow": allowed}) + "\n")
                    proc.stdin.flush()
                except OSError:
                    break
                continue
            if message.get("t") in ("done", "error"):
                final = message
                break
    except OSError:
        pass
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(scratch, ignore_errors=True)
    if final:
        for kind in final.get("violations") or []:
            if kind in VIOLATIONS and kind not in kinds:
                kinds.append(kind)
    if kinds:
        _security(name, kinds)
        return _denied(name, kinds)
    if deadline.is_set():
        return _error(name, "tool_timeout", f"工具运行超时（{int(timeout())} 秒），已停止。")
    if final is None:
        return _error(name, "tool_error", "工具进程异常退出。")
    if final.get("t") == "error":
        error = final.get("error") or "Exception"
        if error == "NoCall":
            if function != "call":
                return _error(name, "tool_error", f"工具包的入口没有 {function}()（见 docs/tool-code-contract.md）。")
            return _error(name, "tool_error", "工具包的入口没有 call(arguments, config)。")
        return _error(name, "tool_error", f"工具运行出错（{error}）。")
    result = final.get("result")
    if not isinstance(result, dict):
        return _error(name, "tool_error", "工具没有返回对象。")
    return result
