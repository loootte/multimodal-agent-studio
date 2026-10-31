"""#93 工具安全策略。

没有配置文件时等于 DEFAULT，也就是现在最严的行为，不会因为没写配置而放宽。
文件是 <数据目录>/tool_policy.json，或环境变量 MAS_TOOL_POLICY 指到的路径。

三层：global、packs.<包名>、tools.<工具名>。工具覆盖包，包覆盖全局，全局覆盖默认。
只认下面这些字段。策略文件里不写密钥；认证只记「从哪个环境变量或钥匙串条目读」。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
from contextlib import contextmanager

SCHEMA = "mas.toolpolicy/1"
FILE_NAME = "tool_policy.json"
ACCEPT_PREFIX = "policy:"
GUARDS = ("strict", "off")
# 跟 services.tools.registry.BLOCKED_EXTERNAL 同一份名字。这里再写一遍，避免和注册表互相导入。
DEFAULT_BLOCK = ("prompt", "text", "body", "content", "bytes", "path", "filename")
SWITCHES = ("confirm_every_call", "local_body_check", "untrusted_results", "schema_reauth")
FLAGS = ("allow_loopback", "allow_link_local", "allow_lan")
_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_ARG_BAD = set("\n\r\x00&|;<>`")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
_HEADER = re.compile(r"[A-Za-z0-9-]{1,40}")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,60}")
_ENTRY = re.compile(r"[A-Za-z0-9_.-]{1,120}")
_SECRET_KEYS = {"token", "secret", "password", "value", "api_key", "apikey", "authorization"}
# 局域网只算 RFC1918 和 IPv6 ULA。Python 的 is_private 还包含文档地址（203.0.113.0/24），那些不是局域网。
_LAN_V4 = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16"))
_LAN_V6 = ipaddress.ip_network("fc00::/7")
_UNSET = object()
_local = threading.local()

# 放宽项给人看的说明。审计只记字段名，不记这些句子里以外的取值。
RELAX_TEXT = {
    "stdio_commands": ("允许白名单外的命令", "stdio 可以跑 npx 或指定命令，不在包内 Python 沙箱里。"),
    "allow_loopback": ("允许连接回环地址", "HTTP MCP 可以连 127.0.0.1 / localhost。"),
    "allow_link_local": ("允许连接链路本地地址", "HTTP MCP 可以连 169.254.x / fe80::。"),
    "allow_lan": ("允许连接局域网地址", "HTTP MCP 可以连内网地址。"),
    "auth": ("注入认证头", "从环境变量或钥匙串读取密钥，加到 MCP 请求上。密钥不进策略文件。"),
    "params_block": ("放宽参数名黑名单", "原本禁止的参数名可以出现在给模型的工具上。"),
    "confirm_every_call": ("不再每次确认", "调用前不再强制弹确认卡。"),
    "local_body_check": ("不检查本地正文外发", "发给 MCP 的参数不再查本地正文。"),
    "untrusted_results": ("结果不再标不可信", "工具结果不再带不可信标记。"),
    "schema_reauth": ("说明变了不再重新启用", "远端工具的说明或参数变了也继续调用。"),
    "content_guard": ("关闭内容审核", "不再做露骨、未成年、真人和版权检查。"),
}


class PolicyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def defaults() -> dict:
    return {
        "stdio_commands": [],
        "allow_loopback": False,
        "allow_link_local": False,
        "allow_lan": False,
        "auth": None,
        "params_block": list(DEFAULT_BLOCK),
        "params_allow": [],
        "confirm_every_call": True,
        "local_body_check": True,
        "untrusted_results": True,
        "schema_reauth": True,
        "content_guard": "strict",
    }


def file_path() -> str | None:
    override = os.environ.get("MAS_TOOL_POLICY")
    if override:
        return override
    base = os.environ.get("MAS_DATA_DIR")
    if not base:
        try:
            from services.tools import sandbox

            base = sandbox.DATA_DIR
        except Exception:  # noqa: BLE001
            base = None
    if not base:
        return None
    return os.path.join(base, FILE_NAME)


def load() -> dict:
    """读策略。坏文件或读不到都当成没有配置（最严），不因此放宽。"""
    forced = getattr(_local, "doc", _UNSET)
    if forced is not _UNSET:
        return forced if isinstance(forced, dict) else {}
    path = file_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(doc: dict) -> dict:
    """写入。带密钥字段、不认识的字段或类型不对都拒绝，不落盘。"""
    cleaned = _require(doc)
    path = file_path()
    if not path:
        raise PolicyError("policy_path", "没有数据目录。设置 MAS_DATA_DIR 或 MAS_TOOL_POLICY 后再保存。")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    text = json.dumps(cleaned, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return cleaned


def public_view() -> dict:
    """设置页要的视图。密钥字段去掉后再给页面；坏文件按空配置，不把原文送回去。"""
    raw = load()
    warning = None
    try:
        document = _require(_strip_secrets(raw) if raw else {"schema": SCHEMA})
    except PolicyError as exc:
        document = {"schema": SCHEMA, "global": {}, "packs": {}, "tools": {}}
        warning = exc.message
    if warning:
        resolved = defaults()
        relaxed: list = []
    else:
        resolved = resolve()
        relaxed = relaxations()
    return {
        "schema": SCHEMA,
        "document": document,
        "defaults": defaults(),
        "resolved": resolved,
        "relaxations": relaxed,
        "labels": {key: {"label": label, "effect": effect} for key, (label, effect) in RELAX_TEXT.items()},
        "warning": warning,
    }


def _strip_secrets(obj):
    """复制一份并去掉密钥字段名，不改调用方手里的那份。"""
    if isinstance(obj, dict):
        return {
            key: _strip_secrets(value)
            for key, value in obj.items()
            if str(key).lower() not in _SECRET_KEYS
        }
    if isinstance(obj, list):
        return [_strip_secrets(item) for item in obj]
    return obj


@contextmanager
def using(doc: dict):
    """检查里临时换一份策略，不写文件。"""
    previous = getattr(_local, "doc", _UNSET)
    _local.doc = _require(doc)
    try:
        yield
    finally:
        if previous is _UNSET:
            delattr(_local, "doc")
        else:
            _local.doc = previous


def resolve(pack: str | None = None, tool: str | None = None) -> dict:
    """生效策略。tool 名是 <包名>__<远端名> 时，没传 pack 也会带上那一层。"""
    if tool and not pack and isinstance(tool, str) and "__" in tool:
        pack = tool.split("__", 1)[0]
    doc = load()
    out = defaults()
    _apply(out, doc.get("global"))
    packs = doc.get("packs") if isinstance(doc.get("packs"), dict) else {}
    tools = doc.get("tools") if isinstance(doc.get("tools"), dict) else {}
    if pack:
        _apply(out, packs.get(pack))
    if tool:
        _apply(out, tools.get(tool))
    return out


def blocked_params(pack: str | None = None, tool: str | None = None) -> set:
    cur = resolve(pack, tool)
    allow = set(cur["params_allow"])
    return {name for name in cur["params_block"] if name not in allow}


def relaxations(pack: str | None = None, tool: str | None = None) -> list[dict]:
    """相对默认值放宽了的项。收紧（多拦参数、保持审核）不算。"""
    cur = resolve(pack, tool)
    found = []
    if cur["stdio_commands"]:
        found.append("stdio_commands")
    for flag in FLAGS:
        if cur[flag]:
            found.append(flag)
    if cur["auth"]:
        found.append("auth")
    if not set(DEFAULT_BLOCK) <= blocked_params(pack, tool):
        found.append("params_block")
    for key in SWITCHES:
        if cur[key] is False:
            found.append(key)
    if cur["content_guard"] != "strict":
        found.append("content_guard")
    out = []
    for field in found:
        label, effect = RELAX_TEXT[field]
        out.append({"field": field, "permission": ACCEPT_PREFIX + field, "label": label, "effect": effect})
    return out


def fingerprint(pack: str | None = None, tool: str | None = None) -> str:
    """启用记录绑这个摘要。变了就要重新确认。摘要里没有密钥。"""
    body = json.dumps(resolve(pack, tool), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def grant_ok(pack: str, stored) -> bool:
    """旧的授权记录没有摘要：策略仍是最严时照旧可用；一旦有放宽，就要重装。"""
    current = fingerprint(pack)
    if stored == current:
        return True
    return stored is None and not relaxations(pack)


def manifest_problems(data: dict) -> list[str]:
    """命令模式的 stdio：命令名必须在白名单里。没配置时白名单是空的，只能用包内 Python。"""
    entry = data.get("entry") if isinstance(data, dict) else None
    if not isinstance(entry, dict) or entry.get("kind") != "mcp" or entry.get("transport") != "stdio":
        return []
    command = entry.get("command")
    if not command:
        return []
    allowed = resolve(data.get("name") if isinstance(data.get("name"), str) else None)["stdio_commands"]
    if command not in allowed:
        return [f"stdio 命令 {command} 不在白名单里（默认只许包内 Python）。"]
    return []


def http_allowed(host: str, resolved: dict | None = None) -> tuple[bool, str | None]:
    """HTTP MCP 的地址类别。解析不了、0.0.0.0 / :: 永远拒绝。"""
    cur = resolved if resolved is not None else resolve()
    kind = host_class(host)
    if kind == "denied":
        return False, "egress_policy_denied"
    flag = {"loopback": "allow_loopback", "link_local": "allow_link_local", "lan": "allow_lan"}.get(kind)
    if flag and not cur.get(flag):
        return False, "egress_policy_denied"
    return True, None


def host_class(host: str) -> str:
    """loopback / link_local / lan / public / denied。一个名字解析出多类地址时取最严的那类。"""
    text = (host or "").strip("[]").split("%")[0].lower()
    if text in ("", "localhost", "unix") or text.endswith(".localhost"):
        return "loopback"
    try:
        return _kind(ipaddress.ip_address(text))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(text, None)
    except (OSError, UnicodeError):
        return "denied"
    kinds = []
    for info in infos:
        try:
            kinds.append(_kind(ipaddress.ip_address(str(info[4][0]).split("%")[0])))
        except ValueError:
            return "denied"
    if not kinds:
        return "denied"
    order = ("denied", "loopback", "link_local", "lan", "public")
    return min(kinds, key=order.index)


def auth_headers(pack: str | None, tool: str | None = None) -> dict:
    """HTTP 要加的头。值只存在返回的字典里，调用方不得写进日志或回给模型。"""
    auth = resolve(pack, tool)["auth"]
    if not auth:
        return {}
    value = _read_secret(auth)
    if not value:
        where = auth.get("env") or auth.get("keychain") or ""
        raise PolicyError("auth_unavailable", f"认证密钥读不到（{where}），没有连接。")
    return {auth["header"]: value}


def auth_env(pack: str | None, tool: str | None = None) -> dict:
    """stdio 只透传策略点名的那一个环境变量，不继承用户 shell 里的其他密钥。"""
    auth = resolve(pack, tool)["auth"]
    if not auth or "env" not in auth:
        return {}
    value = os.environ.get(auth["env"])
    if not value:
        raise PolicyError("auth_unavailable", f"认证密钥读不到（{auth['env']}），没有启动。")
    return {auth["env"]: value}


def peek_secret(pack: str | None, tool: str | None = None) -> str | None:
    """用来从工具结果里抹掉密钥。读不到就当没有，不报错。"""
    auth = resolve(pack, tool)["auth"]
    if not auth:
        return None
    try:
        return _read_secret(auth) or None
    except PolicyError:
        return None


def redact(text: str, secret: str | None) -> str:
    if secret and secret in text:
        return text.replace(secret, "…")
    return text


def audit(pack: str | None, tool: str | None = None) -> None:
    """放宽生效时记一条。只有字段名。"""
    fields = [item["field"] for item in relaxations(pack, tool)]
    if not fields:
        return
    try:
        from services import telemetry

        telemetry.record("security.policy_relaxed", tool=(tool or pack or "")[:48], fields=",".join(fields)[:200])
    except Exception:  # noqa: BLE001
        return


def args_ok(args) -> bool:
    if not isinstance(args, list) or len(args) > 16:
        return False
    for arg in args:
        if not isinstance(arg, str) or not arg or len(arg) > 400 or any(ch in _ARG_BAD for ch in arg):
            return False
    return True


def _kind(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return _kind(mapped)
    if addr.is_unspecified:
        return "denied"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link_local"
    if isinstance(addr, ipaddress.IPv6Address):
        return "lan" if addr in _LAN_V6 else "public"
    return "lan" if any(addr in net for net in _LAN_V4) else "public"


def _read_secret(auth: dict) -> str:
    if "env" in auth:
        return os.environ.get(auth["env"]) or ""
    from packages import keychain

    return keychain.get(auth["keychain"]) or ""


def _apply(out: dict, layer) -> None:
    if not isinstance(layer, dict):
        return
    for key in out:
        if key not in layer or not _ok_value(key, layer[key]):
            continue
        value = layer[key]
        out[key] = list(value) if isinstance(value, list) else (dict(value) if isinstance(value, dict) else value)


def _ok_value(key: str, value) -> bool:
    if key == "stdio_commands":
        return isinstance(value, list) and len(value) <= 8 and all(isinstance(v, str) and _COMMAND.fullmatch(v) for v in value)
    if key in FLAGS or key in SWITCHES:
        return isinstance(value, bool)
    if key == "content_guard":
        return value in GUARDS
    if key in ("params_block", "params_allow"):
        return isinstance(value, list) and len(value) <= 32 and all(isinstance(v, str) and _NAME.fullmatch(v) for v in value)
    if key == "auth":
        return value is None or _ok_auth(value)
    return False


def _ok_auth(value) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value) - {"header", "env", "keychain"} or "header" not in value:
        return False
    if ("env" in value) == ("keychain" in value):
        return False
    if not isinstance(value["header"], str) or not _HEADER.fullmatch(value["header"]):
        return False
    if "env" in value:
        return isinstance(value["env"], str) and _ENV.fullmatch(value["env"])
    return isinstance(value["keychain"], str) and _ENTRY.fullmatch(value["keychain"])


def _secret_keys(obj, found: list) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in _SECRET_KEYS:
                found.append(str(key))
            _secret_keys(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _secret_keys(item, found)


def _require(doc: dict) -> dict:
    """保存和 using() 用：不合法就拒绝，而不是悄悄忽略。"""
    if not isinstance(doc, dict):
        raise PolicyError("invalid_policy", "策略必须是 JSON 对象。")
    bad = []
    _secret_keys(doc, bad)
    if bad:
        raise PolicyError("policy_secret", "策略里不能写密钥，只写环境变量名或钥匙串条目名。")
    unknown = set(doc) - {"schema", "global", "packs", "tools"}
    if unknown or doc.get("schema") not in (None, SCHEMA):
        raise PolicyError("invalid_policy", "策略有不认识的字段。")
    cleaned = {"schema": SCHEMA, "global": {}, "packs": {}, "tools": {}}
    for scope in ("global",):
        cleaned["global"] = _require_layer(doc.get("global") or {})
    for scope in ("packs", "tools"):
        raw = doc.get(scope) or {}
        if not isinstance(raw, dict):
            raise PolicyError("invalid_policy", f"{scope} 必须是对象。")
        cleaned[scope] = {str(name): _require_layer(layer) for name, layer in raw.items()}
    return cleaned


def _require_layer(layer: dict) -> dict:
    if not isinstance(layer, dict):
        raise PolicyError("invalid_policy", "每一层必须是对象。")
    base = defaults()
    unknown = set(layer) - set(base)
    if unknown:
        raise PolicyError("invalid_policy", "策略有不认识的字段：" + "、".join(sorted(unknown)))
    out = {}
    for key, value in layer.items():
        if not _ok_value(key, value):
            raise PolicyError("invalid_policy", f"{key} 的值不合法。")
        out[key] = list(value) if isinstance(value, list) else (dict(value) if isinstance(value, dict) else value)
    return out


def check_case(case: str) -> None:
    """离线检查。evals 和 python -m services.tools.check 都走这里。"""
    if case == "default_block":
        _case_default()
    elif case == "relax":
        _case_relax()
    elif case == "priority":
        _case_priority()
    elif case == "secret":
        _case_secret()
    else:
        raise PolicyError("unknown_case", case)


def self_check() -> None:
    for case in ("default_block", "relax", "priority", "secret"):
        check_case(case)


def _case_default() -> None:
    from services.runtime import content_guard

    for host in ("127.0.0.1", "localhost", "169.254.1.1", "192.168.1.5", "10.1.1.1", "0.0.0.0", "::", "no-such-host.invalid"):
        allowed, _reason = http_allowed(host, defaults())
        if allowed:
            raise PolicyError("default_block", f"默认放行了 {host}")
    allowed, _reason = http_allowed("203.0.113.10", defaults())
    if not allowed:
        raise PolicyError("default_block", "公网文档地址也被默认拦住了")
    if "prompt" not in blocked_params():
        raise PolicyError("default_block", "默认参数黑名单丢了 prompt")
    problem = manifest_problems({"name": "mcp_x", "entry": {"kind": "mcp", "transport": "stdio", "command": "npx", "args": ["-y", "some-server"]}})
    if not problem:
        raise PolicyError("default_block", "没配置也允许了 npx")
    if relaxations():
        raise PolicyError("default_block", "默认策略被当成放宽")
    manifest = {"name": "rated_demo", "content": {"rating": "adult", "min_age": 18, "label": "成人模式"}}
    try:
        content_guard.check_inputs(manifest, {"note": "少女"})
    except content_guard.GuardError:
        pass
    else:
        raise PolicyError("default_block", "默认内容审核没有拦住未成年描述")


def _case_relax() -> None:
    import shutil
    import sys
    import tempfile

    from services.gateway import egress
    from services.runtime import content_guard
    from services.tools import mcp_client, packs

    command = os.path.basename(sys.executable)
    doc = {"schema": SCHEMA, "global": {
        "allow_lan": True, "stdio_commands": [command], "local_body_check": False,
        "schema_reauth": False, "content_guard": "off",
    }}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    saved_dir = os.environ.get("MAS_TOOLPACKS_DIR")
    temp = tempfile.mkdtemp(prefix="mas-policy-relax-")
    os.environ["MAS_TOOLPACKS_DIR"] = temp
    try:
        with using(doc):
            if not http_allowed("192.168.1.5")[0] or http_allowed("127.0.0.1")[0]:
                raise PolicyError("relax", "显式放宽局域网后，回环或局域网的判定不对")
            manifest = {"name": "rated_demo", "content": {"rating": "adult", "min_age": 18, "label": "成人模式"}}
            content_guard.check_inputs(manifest, {"note": "少女"})
            source = tempfile.mkdtemp(prefix="mas-policy-cmd-")
            folder = os.path.join(source, "mcp_cmd")
            shutil.copytree(os.path.join(root, "toolpacks", "examples", "mcp_echo"), folder)
            with open(os.path.join(folder, "tool.json"), encoding="utf-8") as handle:
                tool = json.load(handle)
            tool["name"] = "mcp_cmd"
            tool["entry"] = {"kind": "mcp", "transport": "stdio", "command": command, "args": ["server.py"]}
            with open(os.path.join(folder, "tool.json"), "w", encoding="utf-8") as handle:
                json.dump(tool, handle)
            summary = packs.inspect(folder)
            if summary["problems"]:
                raise PolicyError("relax", "白名单里的命令仍被拒绝：" + "；".join(summary["problems"]))
            wanted = summary["accept"]
            if "policy:stdio_commands" not in wanted or "policy:allow_lan" not in wanted or "code" not in wanted:
                raise PolicyError("relax", f"放宽没有进安装审阅：{wanted}")
            packs.install(folder, wanted)
            pack = packs.find("mcp_cmd")
            if pack is None or pack.grant_state() != "ok":
                raise PolicyError("relax", "放宽确认后没有装上")
            rows = {row["remote"]: row for row in mcp_client.refresh(pack)["tools"]}
            try:
                mcp_client.enable(pack, "echo", rows["echo"]["digest"])
            except mcp_client.McpError as exc:
                if exc.code != "policy_not_accepted":
                    raise
            else:
                raise PolicyError("relax", "启用时没要求确认放宽项")
            fields = sorted(item["field"] for item in relaxations("mcp_cmd", "mcp_cmd__echo"))
            mcp_client.enable(pack, "echo", rows["echo"]["digest"], acknowledge=fields)
            tool = packs.find_tool("mcp_cmd__echo")
            said = mcp_client.call(tool, {"message": "hi"})
            if not said.get("ok") or "hi" not in json.dumps(said, ensure_ascii=False):
                raise PolicyError("relax", "放行的命令没有跑起来")
            # 只改这个工具的策略：包的安装授权不变，已启用的工具要重新启用。
            changed = {"schema": SCHEMA, "global": doc["global"],
                       "tools": {"mcp_cmd__echo": {"confirm_every_call": False}}}
            with using(changed):
                again = mcp_client.call(tool, {"message": "hi"})
                if (again.get("error") or {}).get("code") != "reauth_required":
                    raise PolicyError("relax", "策略变了没有要求重新启用")
    finally:
        if saved_dir is None:
            os.environ.pop("MAS_TOOLPACKS_DIR", None)
        else:
            os.environ["MAS_TOOLPACKS_DIR"] = saved_dir
        shutil.rmtree(temp, ignore_errors=True)

    # 本地正文检查关掉以后，参数里的正文不再挡连接。用假 HTTP，不联网。
    _relax_local_body()


def _relax_local_body() -> None:
    import importlib.util
    import shutil
    import tempfile

    from services.gateway import egress
    from services.tools import mcp_client, packs

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("mas_mcp_fake_policy", os.path.join(root, "evals", "fixtures", "mcp_fake.py"))
    fake_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake_mod)
    saved_dir = os.environ.get("MAS_TOOLPACKS_DIR")
    saved_post = mcp_client.HTTP_POST
    temp = tempfile.mkdtemp(prefix="mas-policy-http-")
    source = tempfile.mkdtemp(prefix="mas-policy-http-src-")
    os.environ["MAS_TOOLPACKS_DIR"] = temp
    fake = fake_mod.FakeMcpHttp()
    mcp_client.HTTP_POST = fake
    marker = "LOCAL-POLICY-RELAX-93"
    try:
        with using({"schema": SCHEMA, "global": {"local_body_check": False, "schema_reauth": False}}):
            folder = fake_mod.write_pack(source, name="mcp_relax")
            summary = packs.inspect(folder)
            packs.install(folder, summary["accept"])
            pack = packs.find("mcp_relax")
            view = mcp_client.refresh(pack)
            fields = sorted(item["field"] for item in relaxations("mcp_relax", "mcp_relax__lookup"))
            mcp_client.enable(pack, "lookup", view["tools"][0]["digest"], acknowledge=fields)
            from services import telemetry

            trace = "t_policy_relax"
            previous = telemetry.current()
            egress.attach(trace, session_id="s_policy", local_bodies=lambda: egress.LocalBodies([marker]))
            telemetry.bind(trace)
            try:
                result = mcp_client.call(packs.find_tool("mcp_relax__lookup"), {"query": marker})
            finally:
                telemetry.bind(previous)
                egress.detach(trace)
            if not result.get("ok") or "tools/call" not in fake.calls:
                raise PolicyError("relax", "关掉本地正文检查后仍然没有连接")
            fake.tools[0] = dict(fake.tools[0], description="变了")
            before = fake.calls.count("tools/call")
            changed = mcp_client.call(packs.find_tool("mcp_relax__lookup"), {"query": "abc"})
            if not changed.get("ok") or fake.calls.count("tools/call") == before:
                raise PolicyError("relax", "schema_reauth=false 时说明变了仍被拦住")
    finally:
        mcp_client.HTTP_POST = saved_post
        if saved_dir is None:
            os.environ.pop("MAS_TOOLPACKS_DIR", None)
        else:
            os.environ["MAS_TOOLPACKS_DIR"] = saved_dir
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)


def _case_priority() -> None:
    doc = {
        "schema": SCHEMA,
        "global": {"allow_lan": True, "allow_loopback": True, "params_block": list(DEFAULT_BLOCK)},
        "packs": {"mcp_fake": {"allow_lan": False, "allow_loopback": True}},
        "tools": {"mcp_fake__lookup": {"allow_loopback": False, "params_allow": ["prompt"]}},
    }
    with using(doc):
        if not resolve()["allow_lan"] or not resolve()["allow_loopback"]:
            raise PolicyError("priority", "全局层没有生效")
        pack = resolve("mcp_fake")
        if pack["allow_lan"] or not pack["allow_loopback"]:
            raise PolicyError("priority", "包层没有盖过全局")
        tool = resolve("mcp_fake", "mcp_fake__lookup")
        if tool["allow_loopback"] or tool["allow_lan"]:
            raise PolicyError("priority", "工具层没有盖过包层")
        if "prompt" in blocked_params("mcp_fake", "mcp_fake__lookup"):
            raise PolicyError("priority", "工具层放宽参数名没有生效")
        if "prompt" not in blocked_params("mcp_fake", "mcp_fake__other"):
            raise PolicyError("priority", "别的工具被带着放宽了")
        if http_allowed("127.0.0.1", tool)[0]:
            raise PolicyError("priority", "工具层关闭回环后仍然放行")
        if not http_allowed("203.0.113.10", tool)[0]:
            raise PolicyError("priority", "公网地址被包层误伤")


def _case_secret() -> None:
    import importlib.util
    import shutil
    import tempfile

    from services.tools import mcp_client, packs

    secret = "mas-policy-secret-value-93"
    path = os.path.join(tempfile.mkdtemp(prefix="mas-policy-file-"), FILE_NAME)
    saved_policy = os.environ.get("MAS_TOOL_POLICY")
    os.environ["MAS_TOOL_POLICY"] = path
    try:
        try:
            save({"schema": SCHEMA, "global": {"auth": {"header": "Authorization", "env": "MAS_MCP_TOKEN", "value": secret}}})
        except PolicyError as exc:
            if exc.code != "policy_secret":
                raise
        else:
            raise PolicyError("secret", "策略文件接受了密钥字段")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                if secret in handle.read():
                    raise PolicyError("secret", "拒绝保存后密钥仍写进了文件")
        save({"schema": SCHEMA, "global": {"auth": {"header": "Authorization", "env": "MAS_MCP_TOKEN"}}})
        with open(path, encoding="utf-8") as handle:
            stored = handle.read()
        if secret in stored or "value" in stored:
            raise PolicyError("secret", "策略文件里出现了密钥")
        _secret_call(secret, path)
    finally:
        if saved_policy is None:
            os.environ.pop("MAS_TOOL_POLICY", None)
        else:
            os.environ["MAS_TOOL_POLICY"] = saved_policy
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def _secret_call(secret: str, policy_path: str) -> None:
    import importlib.util
    import shutil
    import tempfile

    from services import telemetry
    from services.tools import mcp_client, packs

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("mas_mcp_fake_secret", os.path.join(root, "evals", "fixtures", "mcp_fake.py"))
    fake_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake_mod)
    saved_dir = os.environ.get("MAS_TOOLPACKS_DIR")
    saved_post = mcp_client.HTTP_POST
    saved_telemetry = telemetry.path()
    saved_token = os.environ.get("MAS_MCP_TOKEN")
    temp = tempfile.mkdtemp(prefix="mas-policy-secret-")
    source = tempfile.mkdtemp(prefix="mas-policy-secret-src-")
    log = os.path.join(temp, "telemetry.jsonl")
    telemetry.configure(log)
    os.environ["MAS_TOOLPACKS_DIR"] = temp
    os.environ["MAS_MCP_TOKEN"] = secret
    seen = {}
    fake = fake_mod.FakeMcpHttp(result_text=f"echo {secret}")

    def post(url, body, headers, timeout):
        seen["headers"] = dict(headers)
        return fake(url, body, headers, timeout)

    mcp_client.HTTP_POST = post
    previous = telemetry.current()
    telemetry.bind("t_policy_secret")
    try:
        folder = fake_mod.write_pack(source, name="mcp_auth")
        summary = packs.inspect(folder)
        if "policy:auth" not in summary["accept"]:
            raise PolicyError("secret", "注入认证头没有进安装审阅")
        packs.install(folder, summary["accept"])
        pack = packs.find("mcp_auth")
        view = mcp_client.refresh(pack)
        fields = sorted(item["field"] for item in relaxations("mcp_auth", "mcp_auth__lookup"))
        mcp_client.enable(pack, "lookup", view["tools"][0]["digest"], acknowledge=fields)
        result = mcp_client.call(packs.find_tool("mcp_auth__lookup"), {"query": "abc"})
        if seen.get("headers", {}).get("Authorization") != secret:
            raise PolicyError("secret", "请求没有带上环境变量里的认证头")
        dumped = json.dumps(result, ensure_ascii=False)
        if secret in dumped:
            raise PolicyError("secret", "密钥回到了工具结果里")
        if "…" not in dumped:
            raise PolicyError("secret", "结果里的密钥没有被抹掉")
        with open(log, encoding="utf-8") as handle:
            logged = handle.read()
        if secret in logged:
            raise PolicyError("secret", "密钥进了审计")
        with open(policy_path, encoding="utf-8") as handle:
            if secret in handle.read():
                raise PolicyError("secret", "密钥进了策略文件")
    finally:
        telemetry.bind(previous)
        telemetry.configure(saved_telemetry)
        mcp_client.HTTP_POST = saved_post
        if saved_token is None:
            os.environ.pop("MAS_MCP_TOKEN", None)
        else:
            os.environ["MAS_MCP_TOKEN"] = saved_token
        if saved_dir is None:
            os.environ.pop("MAS_TOOLPACKS_DIR", None)
        else:
            os.environ["MAS_TOOLPACKS_DIR"] = saved_dir
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)
