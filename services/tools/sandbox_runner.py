"""第三方工具包的子进程入口（12.D #60）。只由 services/tools/sandbox.py 启动，不要直接 import。

协议（一行一个 JSON，UTF-8）：
- 父进程 → 子进程第一行：{"module", "pack_dir", "arguments", "config", "permissions", "write_roots", "deny_roots", "scratch", "function"}。
  function 默认 call(arguments, config)；#86 表单工具（entry.kind=module）是 catalog(ctx) / preview(fields, ctx) / run(args, ctx)。
  config 里的密钥是父进程显式注入的；子进程的环境变量只有白名单，拿不到用户 shell 里的密钥。
- 子进程 → 父进程：
  {"t": "egress", "host": ..., "port": ...}   要出网（或查域名）之前问一次，父进程回 {"allow": bool}；
  {"t": "done", "result": {...}, "violations": [...]} 或 {"t": "error", "error": "类名", "violations": [...]}。
  violations 只有种类（network / write_files / read_local_content / process / native），不带路径和正文。

降权靠 sys.addaudithook：钩子装上后不能卸掉。没授权 network 就拦所有 socket 连接和域名解析；
写文件只许落在 write_roots（本次调用的临时目录，授权了 write_files 再加上包自己的数据目录）；
deny_roots（数据目录：会话、句柄、配置、审计）一律不许读写，本地内容只能经宿主的句柄 API；
起子进程、fork、加载原生库（ctypes）一律拒绝。这是一期的进程内降权，不是内核级沙箱，见 docs/tool-sandbox.md。
"""

import importlib.util
import json
import os
import sys
import threading

_proto_out = sys.stdout
_proto_in = sys.stdin


def _send(message: dict) -> None:
    _proto_out.write(json.dumps(message, ensure_ascii=False) + "\n")
    _proto_out.flush()


def main() -> None:
    spec = json.loads(_proto_in.readline())
    granted = set(spec.get("permissions") or [])
    scratch = os.path.realpath(spec["scratch"])
    # Windows 路径不分大小写：比较前一律 normcase，免得 D:\Data 和 d:\data 被当成两处。
    write_roots = [os.path.normcase(os.path.realpath(p)) for p in spec.get("write_roots") or []]
    deny_roots = [os.path.normcase(os.path.realpath(p)) for p in spec.get("deny_roots") or []]
    pack_dir = os.path.realpath(spec["pack_dir"])
    violations: list[str] = []
    state = threading.local()  # 每个线程各自防重入，别的线程的事件不会因为这里在忙而漏检

    # 包自己 print 的东西不能混进协议流；挪到 stderr（父进程只保留尾巴，不记录）。
    sys.stdout = sys.stderr
    sys.stdin = open(os.devnull, encoding="utf-8")
    os.chdir(scratch)
    runner_dir = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = [pack_dir] + [p for p in sys.path if p and os.path.realpath(p) != runner_dir]
    sys.path.extend(p for p in spec.get("extra_paths") or [] if isinstance(p, str) and p not in sys.path)

    def deny(kind: str, what: str = ""):
        if kind not in violations:
            violations.append(kind)
        raise PermissionError(f"沙箱拒绝：{kind}{('（' + what + '）') if what else ''}")

    def resolve(path):
        if isinstance(path, int):
            return None
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str):
            path = os.fspath(path)
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    def under(path: str, roots) -> bool:
        for root in roots:
            if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False

    def check_read(path):
        full = resolve(path)
        if full and under(full, deny_roots) and not under(full, write_roots):
            deny("read_local_content", "数据目录只能经句柄 API 读取")

    def check_write(path):
        full = resolve(path)
        if full is None:
            return
        if not under(full, write_roots):
            deny("write_files")

    ask_lock = threading.Lock()

    def ask_network(host, port):
        with ask_lock:
            _ask(host, port)

    def _ask(host, port):
        reply = {}
        _send({"t": "egress", "host": str(host)[:253], "port": port if isinstance(port, int) else None})
        line = _proto_in.readline()
        try:
            reply = json.loads(line) if line else {}
        except ValueError:
            reply = {}
        if not reply.get("allow"):
            deny("network", host if isinstance(host, str) else "")

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def hook(event, args):
        if getattr(state, "busy", False):
            return
        state.busy = True
        try:
            if event == "open":
                path, mode, flags = (list(args) + [None, None, None])[:3]
                writing = False
                if isinstance(mode, str):
                    writing = any(ch in mode for ch in "wax+")
                elif isinstance(flags, int):
                    writing = bool(flags & write_flags)
                check_write(path) if writing else check_read(path)
            elif event in ("os.listdir", "os.scandir", "glob.glob", "os.chdir"):
                if args and args[0] is not None:
                    check_read(args[0])
            elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime",
                           "os.chflags", "os.lchflags", "os.setxattr", "os.removexattr", "shutil.rmtree"):
                if args:
                    check_write(args[0])
            elif event in ("os.rename", "os.link", "os.symlink", "shutil.copyfile", "shutil.copymode",
                           "shutil.copystat", "shutil.move"):
                if len(args) >= 2:
                    check_read(args[0])
                    check_write(args[1])
            elif event in ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
                           "os.forkpty", "os.startfile", "pty.spawn", "_winapi.CreateProcess", "os.kill", "os.killpg",
                           "signal.pthread_kill"):
                deny("process")
            elif event.startswith("ctypes.") or event in ("cpython.PyInterpreterState_New", "_xxsubinterpreters.create"):
                deny("native")
            elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                           "socket.gethostbyaddr", "socket.getnameinfo"):
                if "network" not in granted:
                    host = args[0] if args else ""
                    if isinstance(host, tuple):
                        host = host[0]
                    if isinstance(host, bytes):
                        host = host.decode("ascii", "replace")
                    ask_network(host, args[1] if len(args) > 1 else None)
            elif event in ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind"):
                address = args[1] if len(args) > 1 else None
                if isinstance(address, tuple) and address:
                    host, port = address[0], (address[1] if len(address) > 1 else None)
                elif event == "socket.bind":
                    host, port = "", None
                else:
                    host, port = str(address or "unix"), None
                if event == "socket.bind":
                    if "network" not in granted:
                        deny("network")
                    return
                ask_network(host, port)
        finally:
            state.busy = False

    entry = os.path.realpath(os.path.join(pack_dir, spec["module"]))
    if not entry.startswith(pack_dir + os.sep):
        _send({"t": "error", "error": "BadEntry", "violations": []})
        return
    module_spec = importlib.util.spec_from_file_location("toolpack_entry", entry)
    sys.addaudithook(hook)
    try:
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        function = spec.get("function") or "call"
        call = getattr(module, function, None) if function in ("call", "catalog", "preview", "run") else None
        if not callable(call):
            _send({"t": "error", "error": "NoCall", "violations": violations})
            return
        if function == "catalog":
            result = call(dict(spec.get("config") or {}))
        else:
            result = call(dict(spec.get("arguments") or {}), dict(spec.get("config") or {}))
    except BaseException as exc:  # noqa: BLE001  包里的任何异常都折成类名交回
        _send({"t": "error", "error": type(exc).__name__, "violations": violations})
        return
    try:
        _send({"t": "done", "result": result, "violations": violations})
    except (TypeError, ValueError):
        _send({"t": "error", "error": "NotJSON", "violations": violations})


if __name__ == "__main__":
    main()
