"""出站审计：所有离开本进程的请求走同一个钩子（ADR 0009，路线图 12.A #57）。

用法（适配器和出图代码只调钩子，不自己拼审计记录）：

    ticket = egress.guard("chat_completion", base_url, payload, local_bodies=bodies, exempt=("tools",))
    if ticket.hit:            # 请求里混进了本地正文：已记 egress.blocked，调用方抛错，不打开连接
        raise ...
    ...发出请求...
    ticket.sent(http_status=200)   # 或 ticket.failed("http_error", http_status=...)

记录只有 packages.contracts.events.EGRESS_FIELDS 里的字段：主机名、渠道、模态、字数字节数、消息条数、
工具契约字节数、是否含本地派生内容、sha256 指纹前缀、运行标识、原因码。不记正文、提示词、密钥和本地路径。

记录写到本机 jsonl（服务启动时 configure 指定路径），同时交给当前运行登记的监听者（网页把它推到「数据出站」时间线）。
审计写不进去不影响主流程，但会在 stderr 打出告警并计数（/api/egress 里能看到 write_failures）。

12.C（#59）出口硬化，同在 guard 里按顺序做：
1. 结构化策略（packages.contracts.events.EGRESS_POLICIES）：渠道没登记或策略为空、出现策略外的顶层字段 / 消息字段、
   一次要多张图，记 blocked/egress_policy_denied。
2. 本地正文检查（inspect）：明文子串；规范化（NFKC、大小写、去掉空白 / 标点 / 零宽等非字母数字字符）后的子串，
   包括把各个正文片段按顺序拼起来再查（防分片）；短于 8 的已登记正文按「规范化后指纹 + 整段相等」比对；
   以上命中记 local_content。再把载荷里的 base64 / 百分号编码窗解码（最多两层）重查，命中记 local_content_encoded。
   工具契约（策略里的 exempt）仍豁免，但字节数记进审计 exempt_bytes。
短正文只以指纹参与比对，正文本身不进任何记录或日志。
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import functools
import hashlib
import ipaddress
import json
import os
import sys
import threading
import re
import time
import unicodedata
import urllib.parse
import uuid

from packages.contracts.events import (
    EGRESS_CHANNELS,
    EGRESS_FIELDS,
    EGRESS_POLICIES,
    EGRESS_POLICY_SCHEMA,
    EGRESS_SCHEMA,
    EGRESS_STRUCTURAL_KEYS,
    EVENT_EGRESS_BLOCKED,
    EVENT_EGRESS_FAILED,
    EVENT_EGRESS_PLANNED,
    EVENT_EGRESS_SENT,
)
from services import telemetry

FINGERPRINT_CHARS = 16
HANDLE_PREFIX_CHARS = 6
MIN_LOCAL_BODY = 8
MIN_SHORT_BODY = 4          # 规范化后至少 4 个字母数字才做短正文全文匹配，避免「好的」「是」这类常用词误伤
MAX_NORMALIZE = 256 * 1024  # 超过这个长度的片段（多半是图片 data URL）只做明文比对，不做规范化和解码
MIN_ENCODED_WINDOW = 16     # base64 窗至少 16 个字符（约 12 字节）
MAX_DECODE_DEPTH = 2
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{%d,}={0,2}" % MIN_ENCODED_WINDOW)

_lock = threading.Lock()
_path: str | None = None
_contexts: dict[str, dict] = {}
_contexts_lock = threading.Lock()
_local = threading.local()
_failures = {"write": 0, "listener": 0}


# ---------- 配置与上下文 ----------

def configure(path: str | None) -> None:
    global _path
    _path = path
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def path() -> str | None:
    return _path


def failures() -> dict:
    return dict(_failures)


def attach(trace_id: str, *, session_id: str | None = None, listener=None) -> None:
    """网页一次运行开始时登记：这条 trace 上的出站记录归到哪个会话、推给谁。子 Agent 线程共用 trace_id，也能归进来。"""
    if not trace_id:
        return
    with _contexts_lock:
        _contexts[trace_id] = {"session_id": session_id, "listener": listener}


def detach(trace_id: str) -> None:
    with _contexts_lock:
        _contexts.pop(trace_id, None)


@contextlib.contextmanager
def note(*, local_derived: bool | None = None, handle: str | None = None):
    """宿主在发出之前标明：这次请求是否带本地派生内容、用的是哪个句柄。只在当前线程有效。"""
    previous = getattr(_local, "note", None)
    _local.note = {"local_derived": local_derived, "handle": handle}
    try:
        yield
    finally:
        _local.note = previous


def _note() -> dict:
    return getattr(_local, "note", None) or {}


# ---------- 度量 ----------

def destination(url) -> tuple[str, str]:
    """只取主机名，不要路径、查询串、用户名和密码。返回 (host, scope)。"""
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    return host, scope_of(host)


def scope_of(host: str) -> str:
    host = (host or "").strip("[]").lower()
    if not host:
        return "third_party"
    if host == "localhost" or host.endswith(".localhost"):
        return "local"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "third_party"
    if address.is_loopback:
        return "local"
    if address.is_private or address.is_link_local:
        return "lan"
    return "third_party"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _has(value, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_has(key, secret) or _has(item, secret) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has(item, secret) for item in value)
    return False


def contains_local(payload, bodies, exempt=("tools",)) -> bool:
    """本地正文是否出现在载荷里（明文、规范化、分片、短正文指纹、编码窗）。exempt 里的键（工具契约）不参与检查。"""
    return inspect(payload, bodies, exempt=exempt) is not None


# ---------- 12.C：规范化与本地正文检查 ----------

@functools.lru_cache(maxsize=1024)
def normalize(text: str) -> str:
    """NFKC + casefold，只留字母、数字和组合符号（去掉空白、标点、零宽和控制字符）。"""
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] in "LNM")


def short_fingerprint(text: str) -> str:
    """短正文登记用的指纹：规范化后 sha256 的前 32 个十六进制字符。只在内存里比对，不写进任何记录。"""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:32]


class LocalIndex:
    """一次检查用的本地正文索引。长正文留明文和规范化形；短正文只留指纹。"""

    def __init__(self, bodies=None, fingerprints=None):
        self.raw: list[str] = []
        self.folded: list[str] = []
        self.short: set[str] = set(f for f in (fingerprints or []) if isinstance(f, str))
        for body in bodies or []:
            if not isinstance(body, str) or not body:
                continue
            if len(body) >= MIN_LOCAL_BODY:
                self.raw.append(body)
            if len(body) > MAX_NORMALIZE:
                continue
            folded = normalize(body)
            if len(folded) >= MIN_LOCAL_BODY:
                self.folded.append(folded)
            elif len(folded) >= MIN_SHORT_BODY:
                self.short.add(hashlib.sha256(folded.encode("utf-8")).hexdigest()[:32])

    def __bool__(self) -> bool:
        return bool(self.raw or self.folded or self.short)


def _leaves(value, *, key=None, depth=0):
    """载荷里的正文片段（按出现顺序）：字典键和结构标记的值不算；看起来像 JSON 的字符串（工具参数）展开再找。"""
    if isinstance(value, str):
        if key in EGRESS_STRUCTURAL_KEYS:
            return
        stripped = value.strip()
        if depth < 3 and stripped[:1] in ("{", "[") and len(stripped) <= MAX_NORMALIZE:
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                yield value
                yield from _leaves(parsed, depth=depth + 1)
                return
        yield value
    elif isinstance(value, dict):
        for item_key, item in value.items():
            yield from _leaves(item, key=item_key, depth=depth)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _leaves(item, key=key, depth=depth)


def _text_hit(text: str, index: LocalIndex) -> bool:
    if any(secret in text for secret in index.raw):
        return True
    if len(text) > MAX_NORMALIZE:
        return False
    folded = normalize(text)
    if any(secret in folded for secret in index.folded):
        return True
    if index.short and MIN_SHORT_BODY <= len(folded) < MIN_LOCAL_BODY:
        if hashlib.sha256(folded.encode("utf-8")).hexdigest()[:32] in index.short:
            return True
    return False


def _decoded(text: str):
    """片段里常见的编码窗：base64（标准 / URL 安全，补齐 padding）和百分号编码。data URL 是附件字节，交给明文比对。"""
    if len(text) > MAX_NORMALIZE or text.startswith("data:"):
        return
    if "%" in text:
        unquoted = urllib.parse.unquote(text)
        if unquoted != text:
            yield unquoted
    for match in _B64_RUN.finditer(text):
        window = match.group(0).rstrip("=")
        window += "=" * (-len(window) % 4)
        for altchars in (None, b"-_"):
            try:
                raw = base64.b64decode(window, altchars=altchars, validate=False)
            except (binascii.Error, ValueError):
                continue
            decoded = raw.decode("utf-8", "ignore")
            if decoded and sum(ch.isprintable() for ch in decoded) >= len(decoded) * 0.8:
                yield decoded
                break


def _encoded_hit(text: str, index: LocalIndex, depth: int = 1) -> bool:
    for decoded in _decoded(text):
        if _text_hit(decoded, index):
            return True
        if depth < MAX_DECODE_DEPTH and _encoded_hit(decoded, index, depth + 1):
            return True
    return False


def inspect(payload, bodies=None, *, exempt=("tools",), fingerprints=None) -> str | None:
    """查载荷里有没有本地正文。返回原因码 local_content / local_content_encoded，没有返回 None。不记录、不抛错。"""
    index = bodies if isinstance(bodies, LocalIndex) else LocalIndex(bodies, fingerprints)
    if not index or payload is None:
        return None
    scanned = {k: v for k, v in payload.items() if k not in exempt} if isinstance(payload, dict) else payload
    if index.raw and any(_has(scanned, secret) for secret in index.raw):
        return "local_content"
    leaves = list(_leaves(scanned))
    if any(_text_hit(leaf, index) for leaf in leaves):
        return "local_content"
    if index.folded:  # 分片：把正文片段按顺序拼起来再找
        joined = "".join(normalize(leaf) for leaf in leaves if len(leaf) <= MAX_NORMALIZE)
        if any(secret in joined for secret in index.folded):
            return "local_content"
    if any(_encoded_hit(leaf, index) for leaf in leaves):
        return "local_content_encoded"
    return None


def policy_of(channel: str):
    return EGRESS_POLICIES.get(channel) if channel in EGRESS_CHANNELS else None


def check_policy(channel: str, payload, *, raw_upload: bool = False) -> str | None:
    """结构化出站策略。通过返回 None，否则返回 egress_policy_denied。"""
    policy = policy_of(channel)
    if not policy:
        return "egress_policy_denied"
    if raw_upload or payload is None:
        return None if policy.get("raw_upload") else "egress_policy_denied"
    if not isinstance(payload, dict):
        return "egress_policy_denied"
    if set(payload) - set(policy.get("fields") or ()):
        return "egress_policy_denied"
    message_fields = policy.get("message_fields")
    if message_fields is not None:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return "egress_policy_denied"
        for message in messages:
            if not isinstance(message, dict) or set(message) - set(message_fields):
                return "egress_policy_denied"
    limit = policy.get("max_images")
    if limit is not None and "n" in payload:
        count = payload.get("n")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1 or count > limit:
            return "egress_policy_denied"
    return None


def _modalities(payload) -> list[str]:
    found = set()
    for text in _strings(payload):
        if text in ("image_url", "input_image") or text.startswith("data:image/"):
            found.add("image")
        elif text in ("input_audio",) or text.startswith("data:audio/"):
            found.add("audio")
        elif text.startswith("data:video/"):
            found.add("video")
    found.add("text")
    return sorted(found)


def measure(payload, *, body: bytes | None = None, exempt=("tools",)) -> dict:
    """载荷的统计量：字节数、字数、消息条数、工具契约字节数、模态、指纹。不含任何正文。"""
    if body is None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    stats = {
        "byte_count": len(body),
        "content_fingerprint": hashlib.sha256(body).hexdigest()[:FINGERPRINT_CHARS] if body else None,
    }
    if isinstance(payload, dict):
        scanned = {k: v for k, v in payload.items() if k not in exempt}
        contract = {k: v for k, v in payload.items() if k in exempt}
        stats["char_count"] = sum(len(text) for text in _strings(scanned))
        stats["tool_contract_bytes"] = len(json.dumps(contract, ensure_ascii=False).encode("utf-8")) if contract else 0
        stats["exempt_bytes"] = stats["tool_contract_bytes"]
        messages = payload.get("messages")
        stats["message_count"] = len(messages) if isinstance(messages, list) else None
        stats["modality"] = _modalities(scanned)
        model = payload.get("model")
        if isinstance(model, str) and len(model) <= 120:
            stats["model"] = model
    return stats


# ---------- 记录 ----------

class Ticket:
    """一次出站请求。planned / blocked / sent / failed 共用 egress_id。"""

    def __init__(self, channel: str, url, *, stats: dict | None = None, local_derived: bool | None = None,
                 handle: str | None = None):
        if channel not in EGRESS_CHANNELS:
            raise ValueError(f"未知出站渠道：{channel}")
        _local.tickets = getattr(_local, "tickets", 0) + 1
        hint = _note()
        if local_derived is None:
            local_derived = hint.get("local_derived")
        handle = handle or hint.get("handle")
        host, scope = destination(url)
        self.egress_id = "eg_" + uuid.uuid4().hex[:16]
        self.hit = False
        self.done = False
        self.reason_code: str | None = None
        self.base = {
            "egress_id": self.egress_id,
            "channel": channel,
            "destination_host": host,
            "destination_scope": scope,
            "contains_local_derived": bool(local_derived),
            "policy": EGRESS_POLICY_SCHEMA,
        }
        if isinstance(handle, str) and handle:
            self.base["handle_id_prefix"] = handle[:HANDLE_PREFIX_CHARS]
        for key, value in (stats or {}).items():
            if key in EGRESS_FIELDS:
                self.base[key] = value

    def planned(self) -> "Ticket":
        emit(EVENT_EGRESS_PLANNED, self.base)
        return self

    def blocked(self, reason_code: str) -> "Ticket":
        self.done = True
        emit(EVENT_EGRESS_BLOCKED, self.base, reason_code=reason_code)
        return self

    def sent(self, http_status: int | None = None) -> "Ticket":
        if not self.done:
            self.done = True
            emit(EVENT_EGRESS_SENT, self.base, http_status=http_status)
        return self

    def failed(self, reason_code: str, http_status: int | None = None) -> "Ticket":
        if not self.done:
            self.done = True
            emit(EVENT_EGRESS_FAILED, self.base, reason_code=reason_code, http_status=http_status)
        return self


def ticket_count() -> int:
    """当前线程开过几张出站 ticket。网关用它确认适配器真的走了出口中间件（12.C）。"""
    return getattr(_local, "tickets", 0)


def guard(channel: str, url, payload, *, local_bodies=None, body: bytes | None = None, exempt=None,
          local_derived: bool | None = None, handle: str | None = None, local_fingerprints=None,
          raw_upload: bool = False, modality=None) -> Ticket:
    """网关唯一出口（中间件）：策略、度量、查本地正文。

    被拦时记 blocked、ticket.hit 置位、ticket.reason_code 给出原因码（egress_policy_denied / local_content /
    local_content_encoded），调用方据此抛错、不打开连接；通过则记 planned。
    exempt 不传时用渠道策略里的豁免字段。
    """
    known = channel in EGRESS_CHANNELS
    policy = policy_of(channel) or {}
    if exempt is None:
        exempt = tuple(policy.get("exempt") or ())
    stats = measure(payload, body=body, exempt=exempt)
    if modality:
        stats["modality"] = list(modality)
    reason = check_policy(channel if known else "other", payload, raw_upload=raw_upload)
    if reason is None and (local_bodies or local_fingerprints):
        reason = inspect(payload, local_bodies, exempt=exempt, fingerprints=local_fingerprints)
    hit_local = reason in ("local_content", "local_content_encoded")
    ticket = Ticket(channel if known else "other", url, stats=stats,
                    local_derived=True if hit_local else local_derived, handle=handle)
    ticket.reason_code = reason
    if reason:
        ticket.hit = True
        ticket.blocked(reason)
        security_event(reason, ticket.base.get("channel"))
        return ticket
    return ticket.planned()


def security_event(reason: str, channel: str | None) -> None:
    """拦截同时记一条遥测安全事件（只有原因码和渠道）。"""
    try:
        telemetry.record("security.egress_denied", error_code=reason, channel=channel)
    except Exception as exc:  # 遥测失败不改变拦截结果
        print(f"警告：出站安全事件写入失败 {type(exc).__name__}", file=sys.stderr)


def blocked(channel: str, url, reason_code: str, *, local_derived: bool | None = None, handle: str | None = None) -> Ticket:
    """宿主在组请求之前就拦下（例如本地提示词不许发第三方）：没有载荷可度量，只记目的地和原因。"""
    return Ticket(channel, url, local_derived=local_derived, handle=handle).blocked(reason_code)


def toolpack_connect(tool: str, host: str, *, allowed: bool, reason_code: str | None = None) -> Ticket:
    """12.D：沙箱里的第三方工具包要连某个主机。宿主看不到载荷，只记工具名、主机名和裁决。

    放行记 planned + sent（sent 表示「连接已放行」，不代表对方收到了什么）；拒绝记 blocked 并记安全事件。
    host 只取主机名或 IP，不带端口以外的任何东西。"""
    host = str(host or "").strip()[:253]
    ticket = Ticket("toolpack", "//" + (f"[{host}]" if ":" in host else host) if host else "", local_derived=False)
    if isinstance(tool, str) and tool:
        ticket.base["tool"] = tool[:48]
    if not allowed:
        ticket.reason_code = reason_code or "permission_denied"
        ticket.hit = True
        ticket.blocked(ticket.reason_code)
        security_event(ticket.reason_code, "toolpack")
        return ticket
    ticket.planned()
    return ticket.sent()


def emit(kind: str, base: dict, **extra) -> dict:
    trace_id = telemetry.current()
    run_id = telemetry.current_run() or trace_id
    with _contexts_lock:
        context = dict(_contexts.get(trace_id) or {}) if trace_id else {}
    entry = {"schema": EGRESS_SCHEMA, "type": kind, "ts": round(time.time(), 3)}
    entry.update(base)
    entry.update(extra)
    entry["trace_id"] = trace_id
    entry["run_id"] = run_id
    if run_id and trace_id and run_id != trace_id:
        entry["agent_run_id"] = run_id
    entry["session_id"] = context.get("session_id")
    entry = {key: value for key, value in entry.items() if key in EGRESS_FIELDS and value is not None}
    _write(entry)
    listener = context.get("listener")
    if callable(listener):
        try:
            listener(dict(entry))
        except Exception as exc:  # 推送失败不影响请求本身
            _failures["listener"] += 1
            print(f"警告：出站审计推送失败 {type(exc).__name__}", file=sys.stderr)
    return entry


def _write(entry: dict) -> None:
    if not _path:
        return
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            with open(_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception as exc:
        _failures["write"] += 1
        print(f"警告：出站审计写入失败 {type(exc).__name__}，请求照常进行，但这条记录丢了", file=sys.stderr)


def read(session_id: str | None = None, limit: int = 2000) -> list[dict]:
    if not _path or not os.path.isfile(_path):
        return []
    items = []
    with _lock:
        with open(_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if session_id and entry.get("session_id") != session_id:
                    continue
                items.append(entry)
    return items[-limit:] if limit else items


def summary(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    hosts: dict[str, int] = {}
    for item in items:
        counts[item.get("type") or ""] = counts.get(item.get("type") or "", 0) + 1
        if item.get("type") == EVENT_EGRESS_SENT:
            host = item.get("destination_host") or ""
            hosts[host] = hosts.get(host, 0) + 1
    return {"counts": counts, "sent_hosts": hosts}
