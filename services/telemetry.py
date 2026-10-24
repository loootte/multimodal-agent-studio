"""统一遥测：一次运行一个 trace_id，网页请求、运行时、模型调用、工具调用、ComfyUI 都记在同一条线上。

记录写到本机 jsonl（服务启动时由 configure 指定路径）。没有配置路径时什么都不写。
trace_id 绑定在当前线程上：网页为每次运行开一个线程，运行时、模型和工具都在这个线程里执行。
"""

from __future__ import annotations

import json
import os
import threading
import time

_local = threading.local()
_lock = threading.Lock()
_path: str | None = None

FAILURE_CLASSES = {
    "cancelled": "cancelled",
    "timeout": "timeout",
    "budget_exceeded": "budget",
    "missing_key": "config",
    "unauthorized": "auth",
    "invalid_key": "auth",
    "rejected": "policy",
    "busy": "policy",
    "unsupported_modality": "validation",
    "payload_too_large": "validation",
    "provider_error": "provider",
    "rate_limited": "provider",
}


def configure(path: str | None) -> None:
    global _path
    _path = path
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def path() -> str | None:
    return _path


def bind(trace_id: str | None, owner: str = "web") -> None:
    _local.trace_id = trace_id
    _local.owner = owner if trace_id else None
    _local.usage = None


def owner() -> str | None:
    return getattr(_local, "owner", None)


def current() -> str | None:
    return getattr(_local, "trace_id", None)


def classify(code) -> str:
    code = str(code or "")
    if not code:
        return ""
    if code in FAILURE_CLASSES:
        return FAILURE_CLASSES[code]
    if code.startswith("invalid") or code.endswith("_not_found"):
        return "validation"
    if code.startswith("comfy") or code in {"workflow_failed", "backend_error"}:
        return "backend"
    return "tool"


def note_usage(usage) -> None:
    if isinstance(usage, dict):
        _local.usage = usage


def take_usage() -> dict | None:
    usage = getattr(_local, "usage", None)
    _local.usage = None
    if not isinstance(usage, dict):
        return None
    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(usage.get(key), int):
            out[key] = usage[key]
    return out or None


def record(kind: str, trace_id: str | None = None, **fields) -> None:
    trace_id = trace_id or current()
    if not _path or not trace_id:
        return
    entry = {"ts": round(time.time(), 3), "trace_id": trace_id, "kind": kind}
    for key, value in fields.items():
        if value is not None:
            entry[key] = value
    if entry.get("error_code") and "failure_class" not in entry:
        entry["failure_class"] = classify(entry["error_code"])
    line = json.dumps(entry, ensure_ascii=False)
    with _lock:
        with open(_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read(trace_id: str | None = None, limit: int = 500) -> list[dict]:
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
                if trace_id and entry.get("trace_id") != trace_id:
                    continue
                items.append(entry)
    return items[-limit:] if limit else items


def summary(items: list[dict]) -> dict:
    """按种类汇总：次数、失败率、平均和最大延迟、token 合计。"""
    kinds: dict[str, dict] = {}
    for entry in items:
        bucket = kinds.setdefault(entry.get("kind") or "", {"count": 0, "failed": 0, "ms": [], "tokens": 0, "failures": {}})
        bucket["count"] += 1
        if entry.get("ok") is False:
            bucket["failed"] += 1
            klass = entry.get("failure_class") or "other"
            bucket["failures"][klass] = bucket["failures"].get(klass, 0) + 1
        if isinstance(entry.get("ms"), (int, float)):
            bucket["ms"].append(entry["ms"])
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        bucket["tokens"] += int(usage.get("total_tokens") or entry.get("tokens_estimated") or 0)
    out = {}
    for kind, bucket in kinds.items():
        ms = bucket.pop("ms")
        out[kind] = {
            **bucket,
            "failure_rate": round(bucket["failed"] / bucket["count"], 3) if bucket["count"] else 0.0,
            "avg_ms": int(sum(ms) / len(ms)) if ms else 0,
            "max_ms": int(max(ms)) if ms else 0,
        }
    return {"traces": len({entry.get("trace_id") for entry in items}), "kinds": out}


class span:
    """with telemetry.span("tool", name=...) as s: s["ok"] = ...；结束时记一条，带耗时。异常记为失败。"""

    def __init__(self, kind: str, **fields):
        self.kind = kind
        self.fields = dict(fields)

    def __enter__(self):
        self.started = time.monotonic()
        return self.fields

    def __exit__(self, exc_type, exc, _tb):
        self.fields["ms"] = int((time.monotonic() - self.started) * 1000)
        if exc is not None:
            self.fields["ok"] = False
            code = getattr(exc, "code", None)
            self.fields.setdefault("error_code", code if isinstance(code, str) else type(exc).__name__)
        self.fields.setdefault("ok", True)
        record(self.kind, **self.fields)
        return False
