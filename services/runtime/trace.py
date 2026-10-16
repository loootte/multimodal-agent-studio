"""按一次运行串起来的追踪。"""

from __future__ import annotations

_STORE: dict[str, list[dict]] = {}


def start(run_id: str) -> None:
    _STORE[run_id] = []


def add(run_id: str, entry: dict) -> None:
    _STORE.setdefault(run_id, []).append(dict(entry))


def for_run(run_id: str) -> list[dict]:
    return [dict(item) for item in _STORE.get(run_id) or []]
