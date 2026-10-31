"""#93 工具安全策略（mas.toolpolicy/1）：MCP 出站控制和工具安全检查的开关，统一在一份配置里。

结构：{"schema": "mas.toolpolicy/1", "global": {...}, "packs": {包名: {...}}, "tools": {工具名: {...}}}
优先级：tools > packs > global > 默认值（逐字段覆盖，更具体的一层说了算，可以收紧也可以放宽）。
没有配置文件 / 文件坏了 / 某项不合法 = 该项用默认值，默认值就是 #93 之前最严格的行为（局域网地址比以前更严）。
非法项不生效，返回 warnings（字段路径，不含取值）。

可配置（* 是放宽项：生效时要在安装 / 启用时进入权限审查，记 security.policy_relaxed，只记字段名）：
- stdio_commands*   stdio MCP server 可以用的命令（裸命令名，如 npx）。默认 []：只许包里的 .py 在 12.D 沙箱里跑。global / packs。
- http_loopback*    MCP 可连回环地址（127.0.0.0/8、::1、localhost）。默认 false。本程序自己的端口永远不行。global / packs。
- http_link_local*  MCP 可连链路本地地址（169.254/16、fe80::/10，含云元数据地址）。默认 false。global / packs。
- http_private*     MCP 可连局域网地址（10/8、172.16/12、192.168/16、100.64/10、fc00::/7、fec0::/10）。默认 false。global / packs。
- auth*             HTTP MCP 的认证头：{"header", "scheme", "source": "env"|"keychain", "env"}。只存引用不存值。仅 packs。
- params_allow*     从参数名黑名单里放行的名字。global / packs / tools（只对 MCP 远端工具的参数生效）。
- params_block      额外加进黑名单的参数名。global / packs / tools。
- confirm           manifest（按清单）| always（每次都确认）。只能收紧。global / packs / tools。
- result_max_chars  MCP 结果回给模型的最大字数（500–16000，默认 16000）。只能收紧。global / packs / tools。
- untrusted_results*            false = 第三方 / MCP 工具结果不再标 untrusted、不带「不是指令」提示（仍是 user 角色的数据）。默认 true。
- schema_reauth*                false = MCP 工具说明 / 参数变了不用重新启用（新 schema 仍要过黑名单和结构检查）。默认 true。
- mcp_confirm_every_call*       false = MCP 远端工具一轮里确认一次后复用（仍至少确认一次）。默认 true。
- sensitive_confirm_every_call* false = 联网的第三方工具一轮里确认一次后复用；读本地内容的包不受影响。默认 true。
（这四项 #93 后续由用户决定开放；默认值仍是最严。三层都可写。）
- adult_remote_hosts*  成人分级（rating=adult 或露骨）内容允许发往的第三方出图服务主机名。默认 []：只走本机 ComfyUI。
                       global / packs（分级工具包）/ tools（分级工具名，或 generate_image 管露骨提示词）。
                       放宽后每次实际发送都要确认，卡上写目的主机；本地提示词还要 remote_local_prompt=confirm。
                       这是路由（出站去向），和内容审核无关；本地提示词出站另归本地正文那一套（send_local_to_model 锁定）。
- content_guard*       内容审核级别：strict（默认，全部检查）| standard（不查版权角色）| off（不审核）。
                       只管审不审，不管发到哪里（关掉审核不会让任何内容出本机或发第三方）。global / packs / tools。
                       未成年 / 真人的性化内容拦截是底线（LOCKED content_safety_baseline），任何级别都生效。

锁定项（LOCKED）：只显示，不能改；写了别的值记 warning 并按锁定值处理。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import threading

SCHEMA = "mas.toolpolicy/1"
LEVELS = ("global", "packs", "tools")
BASE_BLOCKED_PARAMS = frozenset({"prompt", "text", "body", "content", "bytes", "path", "filename"})
CONFIRM_LEVELS = ("manifest", "always")
MAX_RESULT = 16000
MIN_RESULT = 500
ADDRESS_FIELDS = ("http_loopback", "http_link_local", "http_private")
ADDRESS_CLASS = {"http_loopback": "loopback", "http_link_local": "link_local", "http_private": "private"}
# 默认 true（最严），写 false 才是放宽的开关。
STRICT_SWITCHES = ("untrusted_results", "schema_reauth", "mcp_confirm_every_call", "sensitive_confirm_every_call")
RELAXING = ("stdio_commands", "http_loopback", "http_link_local", "http_private", "auth", "params_allow") + STRICT_SWITCHES \
    + ("adult_remote_hosts", "content_guard")
MODERATION_LEVELS = ("strict", "standard", "off")
DEFAULTS = {
    "stdio_commands": [],
    "http_loopback": False,
    "http_link_local": False,
    "http_private": False,
    "auth": None,
    "params_allow": [],
    "params_block": [],
    "confirm": "manifest",
    "result_max_chars": MAX_RESULT,
    "untrusted_results": True,
    "schema_reauth": True,
    "mcp_confirm_every_call": True,
    "sensitive_confirm_every_call": True,
    "adult_remote_hosts": [],
    "content_guard": "strict",
}
FIELD_LEVELS = {
    "stdio_commands": ("global", "packs"),
    "http_loopback": ("global", "packs"),
    "http_link_local": ("global", "packs"),
    "http_private": ("global", "packs"),
    "auth": ("packs",),
    "params_allow": ("global", "packs", "tools"),
    "params_block": ("global", "packs", "tools"),
    "confirm": ("global", "packs", "tools"),
    "result_max_chars": ("global", "packs", "tools"),
    **{key: ("global", "packs", "tools") for key in STRICT_SWITCHES},
    "adult_remote_hosts": ("global", "packs", "tools"),
    "content_guard": ("global", "packs", "tools"),
}
# 底线：不开放配置（显示在设置页上，只读）。要不要开放其中哪一项由用户决定，见 docs/tool-policy.md。
LOCKED = {
    "local_body_check": ("strict", "出站前检查本地正文（原文、截段、编码变体、本地指纹），命中就在连接前拦下。本地正文不出本机是底线。"),
    "local_bodies_unavailable": ("refuse", "取不到本会话的本地内容登记时，按最严处理，不连接。"),
    "send_local_to_model": ("forbidden", "本地内容出站的总闸：本地正文 / 本地提示词要不要发给第三方模型或服务都归它。"
                                         "第三方工具包不能把本地内容交给模型；本地提示词发第三方出图只能走 generate_image 的 "
                                         "remote_local_prompt（默认拒绝，显式 confirm 后每轮确认）。内容审核和成人内容路由都改不了它。"),
    "remote_local_prompt_default": ("refuse", "generate_image 把本地提示词发第三方默认拒绝；放宽只能在该工具设置里显式确认。"),
    "content_safety_baseline": ("on", "内容安全底线：性化内容（分级工具或露骨文字）里不能有未成年 / 像未成年的角色、不能有真人。"
                                      "不属于内容审核开关，content_guard=off 也照样拦。"),
    "loopback_self_port": ("deny", "MCP 永远不能连本程序自己的端口。"),
    "unspecified_unresolved": ("deny", "0.0.0.0 / :: 和解析不了的主机永远拒绝。"),
}
COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PARAM = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NAME = re.compile(r"^[a-z][a-z0-9_]{1,95}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
ENV_NAME = re.compile(r"^MAS_MCP_[A-Z0-9_]{1,60}$")
HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
FORBIDDEN_HEADERS = {"host", "content-type", "content-length", "accept", "cookie", "connection", "transfer-encoding",
                     "mcp-session-id", "mcp-protocol-version", "origin", "referer", "user-agent", "proxy-authorization"}
AUTH_SOURCES = ("env", "keychain")
AUTH_SCHEMES = ("bearer", "none")
MAX_SECRET = 2048
MAX_LIST = 32

# 局域网（http_private 管的范围）：RFC 1918、运营商级 NAT、IPv6 唯一本地 / 站点本地。
LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7", "fec0::/10"))

# 由网页宿主设置：策略文件路径、本程序自己监听的端口（回环放行时也不许连）。
PATH: str | None = None
HOST_PORTS: set = set()

_lock = threading.RLock()
_cache: dict = {"key": None, "value": None}


class PolicyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------- 校验 ----------

def _names(value, pattern, path, warnings, limit=MAX_LIST):
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, str) and pattern.match(v) for v in value):
        warnings.append(path)
        return None
    return sorted(set(value))


def _auth(value, path, warnings):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"header", "scheme", "source", "env"}:
        warnings.append(path)
        return None
    header = value.get("header", "Authorization")
    scheme = str(value.get("scheme", "bearer")).lower() if isinstance(value.get("scheme", "bearer"), str) else None
    source = value.get("source")
    if not isinstance(header, str) or not HEADER.match(header) or header.lower() in FORBIDDEN_HEADERS:
        warnings.append(path + ".header")
        return None
    if scheme not in AUTH_SCHEMES:
        warnings.append(path + ".scheme")
        return None
    if source not in AUTH_SOURCES:
        warnings.append(path + ".source")
        return None
    out = {"header": header, "scheme": scheme, "source": source}
    if source == "env":
        env = value.get("env")
        if not isinstance(env, str) or not ENV_NAME.match(env):
            warnings.append(path + ".env")
            return None
        out["env"] = env
    elif "env" in value:
        warnings.append(path + ".env")
    return out


def _field(key, value, path, warnings):
    """返回 (是否接受, 规范化后的值)。"""
    if key == "stdio_commands":
        names = _names(value, COMMAND, path, warnings)
        return names is not None, names
    if key in ADDRESS_FIELDS or key in STRICT_SWITCHES:
        if isinstance(value, bool):
            return True, value
        warnings.append(path)
        return False, None
    if key == "auth":
        before = len(warnings)
        out = _auth(value, path, warnings)
        return len(warnings) == before, out
    if key == "adult_remote_hosts":
        if isinstance(value, list):
            value = [v.strip().lower().rstrip(".") if isinstance(v, str) else v for v in value]
        names = _names(value, HOSTNAME, path, warnings)
        return names is not None, names
    if key == "content_guard":
        if value in MODERATION_LEVELS:
            return True, value
        warnings.append(path)
        return False, None
    if key in ("params_allow", "params_block"):
        names = _names(value, PARAM, path, warnings, limit=64)
        return names is not None, names
    if key == "confirm":
        if value in CONFIRM_LEVELS:
            return True, value
        warnings.append(path)
        return False, None
    if key == "result_max_chars":
        if isinstance(value, int) and not isinstance(value, bool) and MIN_RESULT <= value <= MAX_RESULT:
            return True, value
        warnings.append(path)
        return False, None
    warnings.append(path)
    return False, None


def _level(raw, level, path, warnings) -> dict:
    out = {}
    if not isinstance(raw, dict):
        warnings.append(path)
        return out
    for key, value in raw.items():
        sub = f"{path}.{key}"
        if key in LOCKED:
            if value != LOCKED[key][0]:
                warnings.append(sub)
            continue
        if key not in FIELD_LEVELS or level not in FIELD_LEVELS[key]:
            warnings.append(sub)
            continue
        if value is None:
            continue
        ok, normalized = _field(key, value, sub, warnings)
        if ok:
            out[key] = normalized
    return out


def normalize(raw) -> tuple[dict, list[str]]:
    """原始配置 -> (规范化策略, warnings)。不合法的项丢掉（= 默认值），warnings 只有字段路径。"""
    warnings: list[str] = []
    out = {"schema": SCHEMA, "global": {}, "packs": {}, "tools": {}}
    if raw is None:
        return out, warnings
    if not isinstance(raw, dict):
        return out, ["*"]
    if raw.get("schema") not in (None, SCHEMA):
        warnings.append("schema")
    for key in raw:
        if key not in ("schema",) + LEVELS:
            warnings.append(str(key)[:48])
    out["global"] = _level(raw.get("global") or {}, "global", "global", warnings)
    for level in ("packs", "tools"):
        section = raw.get(level) or {}
        if not isinstance(section, dict):
            warnings.append(level)
            continue
        for name, value in list(section.items())[:200]:
            if not isinstance(name, str) or not NAME.match(name):
                warnings.append(f"{level}.?")
                continue
            body = _level(value, level, f"{level}.{name}", warnings)
            if body:
                out[level][name] = body
    return out, warnings


# ---------- 读取 ----------

def path() -> str | None:
    if PATH:
        return PATH
    base = os.environ.get("MAS_DATA_DIR")
    return os.path.join(base, "tool_policy.json") if base else None


def _read_raw():
    target = path()
    if not target:
        return None, None
    try:
        stat = os.stat(target)
    except OSError:
        return None, None
    key = (target, stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _cache["key"] == key:
            return _cache["value"], key
        try:
            with open(target, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            raw = "corrupt"
        _cache.update(key=key, value=raw)
        return raw, key


def load() -> tuple[dict, list[str]]:
    raw, _key = _read_raw()
    if raw == "corrupt":
        policy, _ = normalize(None)
        return policy, ["*"]
    return normalize(raw)


def current() -> dict:
    return load()[0]


def _refuse_in_tool(target: str) -> None:
    from services.runtime import injection

    injection.refuse_config_write(target)


def save(raw) -> tuple[dict, list[str]]:
    """只存校验通过的项。工具执行期间不能写（12.G）。"""
    _refuse_in_tool("tool_policy.json")
    policy, warnings = normalize(raw)
    target = path()
    if not target:
        raise PolicyError("policy_unavailable", "没有数据目录，不能保存工具安全策略。")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temp = target + ".tmp"
    with _lock:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(policy, handle, ensure_ascii=False, indent=2)
        os.replace(temp, target)
        _cache.update(key=None, value=None)
    return policy, warnings


def reset() -> None:
    _refuse_in_tool("tool_policy.json")
    target = path()
    with _lock:
        if target and os.path.exists(target):
            os.remove(target)
        _cache.update(key=None, value=None)


# ---------- 解析生效值 ----------

def resolve(pack: str | None, tool: str | None = None, policy: dict | None = None) -> dict:
    """某个包 / 工具的生效策略（tools > packs > global > 默认）。另给 blocked_params（最终参数名黑名单）。"""
    policy = policy if policy is not None else current()
    out = json.loads(json.dumps(DEFAULTS))
    layers = [policy.get("global") or {}]
    if pack:
        layers.append((policy.get("packs") or {}).get(pack) or {})
    if tool:
        layers.append((policy.get("tools") or {}).get(tool) or {})
    blocked = set(BASE_BLOCKED_PARAMS)
    for layer in layers:
        for key, value in layer.items():
            out[key] = value
        if "params_block" in layer:
            blocked |= set(layer["params_block"])
        if "params_allow" in layer:
            blocked -= set(layer["params_allow"])
    # 黑名单逐层叠：同一层先加再减，更具体的层后算，所以能放行上层加的名字，也能把上层放行的名字加回去
    out["blocked_params"] = sorted(blocked)
    out["params_allowed"] = sorted(BASE_BLOCKED_PARAMS - blocked)
    return out


def relaxations(effective: dict, transport: str | None = None, command: str | None = None, *,
                mcp: bool = True, sensitive_relaxable: bool = False, rated: bool = False,
                moderated: bool = False) -> list[str]:
    """生效策略里对这个包 / 工具真正起作用的放宽项（字段名）。
    mcp=False 是普通第三方包：只看 untrusted_results 和（联网、不读本地内容的包）sensitive_confirm_every_call。"""
    out = []
    if not mcp:
        if effective.get("untrusted_results") is False:
            out.append("untrusted_results")
        if sensitive_relaxable and effective.get("sensitive_confirm_every_call") is False:
            out.append("sensitive_confirm_every_call")
        if rated and effective.get("adult_remote_hosts"):
            out.append("adult_remote_hosts")
        if moderated and effective.get("content_guard", "strict") != "strict":
            out.append("content_guard")
        return out
    if command and command in (effective.get("stdio_commands") or []):
        out.append("stdio_commands")
    if transport in ("http", "stdio"):
        for key in ADDRESS_FIELDS:
            if effective.get(key):
                out.append(key)
    if transport == "http" and effective.get("auth"):
        out.append("auth")
    if effective.get("params_allowed"):
        out.append("params_allow")
    for key in ("untrusted_results", "schema_reauth", "mcp_confirm_every_call"):
        if effective.get(key) is False:
            out.append(key)
    return out


def adult_remote_allowed(effective: dict, host: str) -> bool:
    """成人分级内容能不能发往这个第三方出图主机（只比主机名，大小写和末尾点不计）。默认 [] = 只走本机。"""
    host = (host or "").strip("[]").lower().rstrip(".")
    return bool(host) and host in (effective.get("adult_remote_hosts") or [])


def digest(effective: dict) -> str:
    """生效策略的指纹（只含引用，不含密钥值）。绑进 MCP 启用记录：策略变了，已启用的工具要重新确认。"""
    canonical = json.dumps(effective, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


DEFAULT_DIGEST = digest(resolve(None, None, {"global": {}, "packs": {}, "tools": {}}))


# ---------- 地址 ----------

def classify(host: str) -> set:
    """主机 -> 地址类别集合：loopback / link_local / private / unspecified / unresolved / public。"""
    text = (host or "").strip("[]").split("%")[0].lower().rstrip(".")
    if text in ("", "unix"):
        return {"unresolved"}
    if text == "localhost" or text.endswith(".localhost"):
        return {"loopback"}
    try:
        addresses = [ipaddress.ip_address(text)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in socket.getaddrinfo(text, None)]
        except (OSError, UnicodeError, ValueError):
            return {"unresolved"}
    if not addresses:
        return {"unresolved"}
    found = set()
    for address in addresses:
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if address.is_unspecified:
            found.add("unspecified")
        elif address.is_loopback:
            found.add("loopback")
        elif address.is_link_local:
            found.add("link_local")
        elif any(address in net for net in LAN_NETWORKS if net.version == address.version):
            found.add("private")
        elif address.is_multicast or address.is_reserved:
            found.add("unspecified")  # 组播 / 保留 / 广播：永远拒绝
        else:
            found.add("public")
    return found


def address_decision(effective: dict, host: str, port) -> tuple[bool, str | None]:
    """按生效策略裁决一个目的地址。返回 (放行, 原因码)。"""
    classes = classify(host)
    if classes & {"unspecified", "unresolved"}:
        return False, "egress_policy_denied"
    allowed = {ADDRESS_CLASS[k] for k in ADDRESS_FIELDS if effective.get(k)} | {"public"}
    if classes - allowed:
        return False, "egress_policy_denied"
    if "loopback" in classes:
        try:
            number = int(port) if port is not None else None
        except (TypeError, ValueError):
            number = None
        if number is None or number in HOST_PORTS:
            return False, "egress_policy_denied"
    return True, None


# ---------- 认证头 ----------

def auth_status(auth: dict | None, pack: str) -> dict | None:
    if not auth:
        return None
    out = {"header": auth["header"], "scheme": auth["scheme"], "source": auth["source"]}
    if auth["source"] == "env":
        out["env"] = auth["env"]
        out["available"] = bool(os.environ.get(auth["env"]))
    else:
        out["entry"] = keychain_entry(pack)
        out["available"] = _keychain_has(pack)
    return out


def keychain_entry(pack: str) -> str:
    return f"mcp.{pack}.auth"


def _keychain_has(pack: str) -> bool:
    try:
        from packages import keychain

        return bool(keychain.active() and keychain.get(keychain_entry(pack)))
    except Exception:  # noqa: BLE001 钥匙串不可用 = 没有
        return False


def auth_header(auth: dict | None, pack: str) -> tuple[str, str] | None:
    """发请求那一刻取值。值不缓存、不落盘、不进日志和审计；错误信息里也不带值。"""
    if not auth:
        return None
    if auth["source"] == "env":
        value = os.environ.get(auth["env"]) or ""
    else:
        try:
            from packages import keychain

            value = (keychain.get(keychain_entry(pack)) if keychain.active() else None) or ""
        except Exception:  # noqa: BLE001
            value = ""
    if not value:
        raise PolicyError("auth_unavailable", "配置了认证头，但取不到密钥（环境变量没设，或钥匙串里没有）。")
    if len(value) > MAX_SECRET or any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise PolicyError("auth_invalid", "认证密钥格式不对（太长或含空白 / 控制字符）。")
    return auth["header"], ("Bearer " + value if auth["scheme"] == "bearer" else value)


def set_secret(pack: str, value: str | None) -> None:
    """本机钥匙串里存 / 删某个 MCP 包的认证密钥。只写不读：没有任何接口把值读回去。"""
    from packages import keychain

    _refuse_in_tool("tool_policy.secret")

    if not isinstance(pack, str) or not NAME.match(pack):
        raise PolicyError("invalid_pack", "包名不合法。")
    if not keychain.active():
        raise PolicyError("keychain_unavailable", "系统钥匙串不可用（只有桌面壳启用），请改用环境变量 MAS_MCP_*。")
    if value is None:
        keychain.delete(keychain_entry(pack))
        return
    if not isinstance(value, str) or not value or len(value) > MAX_SECRET or \
            any(ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise PolicyError("auth_invalid", "密钥格式不对（不能为空、不能有空白和控制字符，最多 2048 字）。")
    keychain.set(keychain_entry(pack), value)


def scrub(value, secrets: list[str]):
    """把密钥（含 Bearer 前缀形式）从将要交给模型 / 日志的内容里抹掉。"""
    secrets = [s for s in secrets if s]
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[已隐去]")
        return value
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    return value


# ---------- 给设置页 ----------

def public() -> dict:
    policy, warnings = load()
    packs = {}
    for name, body in (policy.get("packs") or {}).items():
        item = dict(body)
        if item.get("auth"):
            item["auth"] = auth_status(item["auth"], name)
        packs[name] = item
    return {"schema": SCHEMA, "policy": {"global": policy["global"], "packs": packs, "tools": policy["tools"]},
            "defaults": json.loads(json.dumps(DEFAULTS)), "base_blocked_params": sorted(BASE_BLOCKED_PARAMS),
            "relaxing": list(RELAXING), "field_levels": {k: list(v) for k, v in FIELD_LEVELS.items()},
            "locked": [{"key": k, "value": v[0], "text": v[1]} for k, v in LOCKED.items()],
            "warnings": warnings}


def is_relaxed(key: str, value) -> bool:
    if key in STRICT_SWITCHES:
        return value is False
    if key == "content_guard":
        return value in MODERATION_LEVELS and value != "strict"
    return key in RELAXING and value not in (None, [], False)


def relaxed_fields(policy: dict) -> list[str]:
    """整份策略里出现的放宽字段名（审计用）。"""
    found = set()
    for level in LEVELS:
        sections = [policy.get(level) or {}] if level == "global" else list((policy.get(level) or {}).values())
        for body in sections:
            for key in RELAXING:
                if is_relaxed(key, body.get(key)):
                    found.add(key)
    return sorted(found)


def newly_relaxed(before: dict, after: dict) -> list[str]:
    """after 比 before 新放宽（或换了放宽取值）的字段路径（level.key，不含包名 / 工具名）。
    #93 决定 7：写入这些要服务端确认（acknowledge 逐项列出）。收紧和恢复默认不用确认。"""
    out = set()
    for level in LEVELS:
        a = before.get(level) or {}
        b = after.get(level) or {}
        pairs = [(a, b)] if level == "global" else [(a.get(n) or {}, b.get(n) or {}) for n in b]
        for x, y in pairs:
            for key in RELAXING:
                if is_relaxed(key, y.get(key)) and y.get(key) != x.get(key):
                    out.add(f"{level}.{key}")
    return sorted(out)


def changed_fields(before: dict, after: dict) -> list[str]:
    """两份策略之间变了的字段路径（审计只记这些名字）。"""
    out = set()
    for level in LEVELS:
        a = before.get(level) or {}
        b = after.get(level) or {}
        if level == "global":
            for key in set(a) | set(b):
                if a.get(key) != b.get(key):
                    out.add(f"global.{key}")
            continue
        for name in set(a) | set(b):
            x, y = a.get(name) or {}, b.get(name) or {}
            for key in set(x) | set(y):
                if x.get(key) != y.get(key):
                    out.add(f"{level}.{key}")
    return sorted(out)
