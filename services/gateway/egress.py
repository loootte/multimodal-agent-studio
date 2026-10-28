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
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid

from packages.contracts.events import (
    EGRESS_CHANNELS,
    EGRESS_FIELDS,
    EGRESS_SCHEMA,
    EVENT_EGRESS_BLOCKED,
    EVENT_EGRESS_FAILED,
    EVENT_EGRESS_PLANNED,
    EVENT_EGRESS_SENT,
)
from services import telemetry

FINGERPRINT_CHARS = 16
HANDLE_PREFIX_CHARS = 6
MIN_LOCAL_BODY = 8

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
    """本地正文（长度不少于 8）是否出现在载荷里。exempt 里的键（工具契约）不参与检查。"""
    scanned = {k: v for k, v in payload.items() if k not in exempt} if isinstance(payload, dict) else payload
    for secret in bodies or []:
        if isinstance(secret, str) and len(secret) >= MIN_LOCAL_BODY and _has(scanned, secret):
            return True
    return False


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
        hint = _note()
        if local_derived is None:
            local_derived = hint.get("local_derived")
        handle = handle or hint.get("handle")
        host, scope = destination(url)
        self.egress_id = "eg_" + uuid.uuid4().hex[:16]
        self.hit = False
        self.done = False
        self.base = {
            "egress_id": self.egress_id,
            "channel": channel,
            "destination_host": host,
            "destination_scope": scope,
            "contains_local_derived": bool(local_derived),
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


def guard(channel: str, url, payload, *, local_bodies=None, body: bytes | None = None, exempt=("tools",),
          local_derived: bool | None = None, handle: str | None = None) -> Ticket:
    """网关唯一出口：度量、查本地正文。混进本地正文时记 blocked 并把 ticket.hit 置位；否则记 planned。"""
    stats = measure(payload, body=body, exempt=exempt)
    hit = contains_local(payload, local_bodies, exempt) if local_bodies else False
    ticket = Ticket(channel, url, stats=stats, local_derived=True if hit else local_derived, handle=handle)
    if hit:
        ticket.hit = True
        return ticket.blocked("local_content")
    return ticket.planned()


def blocked(channel: str, url, reason_code: str, *, local_derived: bool | None = None, handle: str | None = None) -> Ticket:
    """宿主在组请求之前就拦下（例如本地提示词不许发第三方）：没有载荷可度量，只记目的地和原因。"""
    return Ticket(channel, url, local_derived=local_derived, handle=handle).blocked(reason_code)


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
