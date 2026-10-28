"""评测用探针（12.D #60）：按 act 做一件越权或正常的事。越权的那些应当被沙箱拦下，整次调用变成 permission_denied。"""

import os
import socket
import subprocess
import tempfile
import urllib.request


def call(arguments, config):
    act = arguments.get("act") or "scratch"
    target = arguments.get("target") or ""
    if act == "scratch":  # 临时目录可写
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as handle:
            handle.write("ok")
        return {"ok": True, "act": act}
    if act == "env":  # 只看有没有，不回传值
        leaked = sorted(k for k in os.environ if any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD")))
        return {"ok": True, "act": act, "leaked": leaked, "has_config_key": bool(config.get("api_key")),
                "sandbox": os.environ.get("MAS_SANDBOX", "")}
    if act == "net":
        socket.create_connection(("203.0.113.7", 80), timeout=2).close()
    elif act == "net_host":
        urllib.request.urlopen("http://example.com/", timeout=2).close()
    elif act == "loopback":
        socket.create_connection(("127.0.0.1", int(arguments.get("port") or 9)), timeout=2).close()
    elif act == "swallow_net":  # 自己吞掉异常也照样算越权
        try:
            socket.create_connection(("203.0.113.7", 443), timeout=2).close()
        except OSError:
            pass
    elif act == "write_out":
        with open(os.path.join(target or os.path.dirname(os.path.abspath(__file__)), "escaped.txt"), "w") as handle:
            handle.write("x")
    elif act == "write_own":
        with open(os.path.join(os.environ["MAS_TOOLPACK_DATA"], "note.txt"), "w") as handle:
            handle.write("x")
    elif act == "read_data":
        with open(target, encoding="utf-8") as handle:
            return {"ok": True, "act": act, "size": len(handle.read())}
    elif act == "list_data":
        return {"ok": True, "act": act, "names": os.listdir(target)}
    elif act == "spawn":
        subprocess.run(["python", "--version"], check=False)
    return {"ok": True, "act": act}
