"""把会话文件和 telemetry.jsonl 导出成 mas.trajectory/1 轨迹，写到数据目录的 trajectories/，带增量索引（ADR 0006）。

    python -m services.meta.export [--data-dir DIR] [--keep-days 90]
    python -m services.meta.export query [--tool T] [--agent A] [--skill S] [--outcome O] [--since D] [--until D] [--limit N]

只读会话和遥测，不改它们。本地内容、文件字节、密钥和绝对路径不进轨迹。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apps.web import store as web_store  # noqa: E402  复用 _cover_local、_scrub_value、_file_secrets
from packages.artifacts import ArtifactStore  # noqa: E402
from services.meta.trajectory import SCHEMA, validate  # noqa: E402
from services.telemetry import classify  # noqa: E402

INDEX_SCHEMA = "mas.trajectory-index/1"
SUMMARY_CHARS = 120
KEEP_DAYS = 90
_lock = threading.Lock()

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{12,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{12,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S{6,}"),
]
_PATHS = [
    re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|]*"),
    re.compile(r"\\\\[^\s\\\"']+\\[^\s\"'<>|]*"),
    re.compile(r"(?<![\w.])/(?:home|Users|root|tmp|workspace|mnt|var|opt|private|data|srv|etc|media)(?:/[^\s\"'<>|]*)?"),
]


def _iso(ts) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _norm_time(value: str | None, end: bool = False) -> str | None:
    """接受 2026-10-01、2026-10-01T08:00 或完整 ISO；统一成 UTC ISO 字符串，便于按字符串比较。"""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value + ("T23:59:59Z" if end else "T00:00:00Z")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        return value + ":00Z"
    return value if value.endswith("Z") else value + "Z"


class Scrubber:
    """一个会话一份：本地正文和文件字节作为密钥集合，外加 API key、令牌形态和绝对路径。"""

    def __init__(self, bodies: list[tuple[str, str]], secrets: list[str], extra: list[str]):
        self.bodies = sorted(bodies, key=lambda pair: len(pair[0]), reverse=True)
        self.secrets = [s for s in secrets if isinstance(s, str) and len(s) >= 8]
        self.extra = [s for s in extra if isinstance(s, str) and len(s) >= 6]

    def text(self, value: str) -> str:
        value = web_store._cover_local(value or "", self.bodies)
        value = web_store._scrub_text(value, self.secrets)
        return self.redact(value)

    def redact(self, value: str) -> str:
        for item in self.extra:
            if item in value:
                value = value.replace(item, "[secret]" if not os.path.isabs(item) else "[path]")
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub("[secret]", value)
        for pattern in _PATHS:
            value = pattern.sub("[path]", value)
        return value

    def summary(self, value) -> tuple[str, int]:
        value = value if isinstance(value, str) else ""
        covered = self.text(value)
        flat = re.sub(r"\s+", " ", covered).strip()
        return flat[:SUMMARY_CHARS], len(value)

    def record(self, record: dict):
        record = web_store._scrub_value(record, self.secrets)
        record = _map_strings(record, self.redact)
        blob = json.dumps(record, ensure_ascii=False)
        for item in self.secrets + self.extra:
            if item in blob:
                return None
        for body, _handle in self.bodies:
            if len(body) >= 8 and body in blob:
                return None
        return record


def _map_strings(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {key: _map_strings(item, fn) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, fn) for item in value]
    return value


class Exporter:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.sessions_dir = os.path.join(self.data_dir, "sessions")
        self.out_dir = os.path.join(self.data_dir, "trajectories")
        self.jsonl = os.path.join(self.out_dir, "trajectories.jsonl")
        self.index_path = os.path.join(self.out_dir, "index.json")
        self._artifacts = None

    # ---------- 读取 ----------

    def _load_json(self, path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _sessions(self) -> dict[str, dict]:
        found = {}
        if not os.path.isdir(self.sessions_dir):
            return found
        for name in sorted(os.listdir(self.sessions_dir)):
            if not re.fullmatch(r"s_[0-9a-f]{8,64}\.json", name):
                continue
            data = self._load_json(os.path.join(self.sessions_dir, name))
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                found[name[:-5]] = data
        return found

    def _telemetry(self, traces: set[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        path = os.path.join(self.data_dir, "telemetry.jsonl")
        if not traces or not os.path.isfile(path):
            return out
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("trace_id") in traces:
                    out.setdefault(entry["trace_id"], []).append(entry)
        return out

    def _blob(self, session_id: str, item: dict) -> bytes | None:
        checksum = item.get("checksum")
        if isinstance(checksum, str) and checksum:
            if self._artifacts is None:
                self._artifacts = ArtifactStore(os.path.join(self.data_dir, "artifacts"))
            try:
                data = self._artifacts.get(checksum)
            except Exception:
                data = None
            if data is not None:
                return data
        handle = item.get("handle")
        if isinstance(handle, str) and re.fullmatch(r"[a-z]+_[0-9a-f]{8,64}", handle):
            path = os.path.join(self.sessions_dir, session_id, "files", handle)
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        return None

    def _extra_secrets(self) -> list[str]:
        extra = [self.data_dir, ROOT]
        provider = self._load_json(os.path.join(self.data_dir, "provider.json"))
        if isinstance(provider, dict):
            for key in ("api_key", "token", "secret"):
                if isinstance(provider.get(key), str):
                    extra.append(provider[key])
        for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "CHAT_API_KEY"):
            if os.environ.get(name):
                extra.append(os.environ[name])
        return extra

    def _scrubber(self, session_id: str, session: dict) -> Scrubber:
        bodies, secrets = [], []
        for item in session.get("contents") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            handle = item.get("handle") or "c_?"
            if isinstance(text, str) and text:
                bodies.append((text, handle))
                secrets.append(text)
            else:
                data = self._blob(session_id, item)
                if data:
                    secrets.extend(web_store._file_secrets(data))
        for item in session.get("attachments") or []:
            if isinstance(item, dict):
                data = self._blob(session_id, item)
                if data:
                    secrets.extend(web_store._file_secrets(data))
        for message in session.get("messages") or []:
            for item in message.get("attachments") or []:
                if isinstance(item, dict) and item.get("checksum"):
                    data = self._blob(session_id, item)
                    if data:
                        secrets.extend(web_store._file_secrets(data))
        return Scrubber(bodies, secrets, self._extra_secrets())

    # ---------- 构建 ----------

    def build_session(self, session_id: str, session: dict, telemetry: dict[str, list[dict]] | None = None) -> tuple[list[dict], int]:
        """返回 (记录, 被丢弃的条数)。只导出已经结束的回合。"""
        scrub = self._scrubber(session_id, session)
        local_by_message = {}
        for item in session.get("contents") or []:
            if isinstance(item, dict) and item.get("message_id") and isinstance(item.get("handle"), str):
                local_by_message[item["message_id"]] = item["handle"]
        messages = [m for m in session.get("messages") or [] if isinstance(m, dict)]
        turns = []
        last_user = None
        for message in messages:
            if message.get("role") == "user":
                last_user = message
            elif message.get("role") == "assistant" and message.get("run_id"):
                turns.append((last_user, message))
        if telemetry is None:
            telemetry = self._telemetry({m.get("run_id") for _, m in turns})
        drafts = []
        for index, (user, assistant) in enumerate(turns):
            if assistant.get("status") == "running":
                continue
            following = turns[index + 1] if index + 1 < len(turns) else None
            drafts.extend(self._turn(session_id, user, assistant, following, telemetry.get(assistant["run_id"]) or [],
                                     local_by_message, scrub))
        records, dropped = [], 0
        for draft in drafts:
            clean = scrub.record(draft)
            if clean is None or validate(clean):
                dropped += 1
                continue
            records.append(clean)
        return records, dropped

    def _steps(self, entries: list[dict]) -> list[dict]:
        steps = []
        for entry in entries:
            kind = entry.get("kind")
            if kind not in ("provider", "tool"):
                continue
            step = {"kind": "model" if kind == "provider" else "tool",
                    "name": str(entry.get("model") if kind == "provider" else entry.get("name") or ""),
                    "ok": entry.get("ok") if isinstance(entry.get("ok"), bool) else None,
                    "ms": int(entry["ms"]) if isinstance(entry.get("ms"), (int, float)) else None,
                    "at": _iso(entry.get("ts"))}
            if entry.get("error_code"):
                step["error_code"] = str(entry["error_code"])
                step["failure_class"] = entry.get("failure_class") or classify(entry["error_code"])
            if kind == "provider":
                tokens = dict(entry.get("usage") or {}) if isinstance(entry.get("usage"), dict) else {}
                if isinstance(entry.get("tokens_estimated"), int):
                    tokens["estimated"] = entry["tokens_estimated"]
                if tokens:
                    step["tokens"] = tokens
                if entry.get("tool_requested"):
                    step["tool_requested"] = str(entry["tool_requested"])
            elif isinstance(entry.get("args"), dict):
                step["args"] = entry["args"]
            if entry.get("via"):
                step["via"] = str(entry["via"])
            steps.append(step)
        return steps

    def _runtime_steps(self, message: dict) -> list[dict]:
        """没有遥测时（旧数据或没配置遥测），用会话里持久化的 runtime 步骤。"""
        steps = []
        for event in message.get("runtime") or []:
            if not isinstance(event, dict) or event.get("kind") not in ("model", "tool"):
                continue
            steps.append({"kind": event["kind"], "name": str(event.get("name") or ""),
                          "ok": event.get("ok") if isinstance(event.get("ok"), bool) else None,
                          "ms": int(event["ms"]) if isinstance(event.get("ms"), (int, float)) else None})
        return steps

    def _rollup(self, record: dict, steps: list[dict]) -> None:
        record["steps"] = steps
        record["tools"] = [s["name"] for s in steps if s["kind"] == "tool"]
        skills = []
        for step in steps:
            if step["kind"] == "tool" and step["name"] == "read_skill":
                name = (step.get("args") or {}).get("name")
                if isinstance(name, str) and name not in skills:
                    skills.append(name)
        record["skills"] = skills
        tokens = {}
        for step in steps:
            for key, value in (step.get("tokens") or {}).items():
                if isinstance(value, int):
                    tokens[key] = tokens.get(key, 0) + value
        record["budget"] = {"steps": sum(1 for s in steps if s["kind"] == "model"), "tokens": tokens}

    def _turn(self, session_id, user, assistant, following, entries, local_by_message, scrub) -> list[dict]:
        top_id = assistant["run_id"]
        by_run: dict[str, list[dict]] = {}
        child_meta: dict[str, dict] = {}
        policy = False
        started = ended = None
        top_ms = None
        model = ""
        for entry in entries:
            kind = entry.get("kind")
            if kind == "web.request" and started is None:
                started = entry.get("ts")
            if kind == "run":
                ended = entry.get("ts")
                top_ms = entry.get("ms") if isinstance(entry.get("ms"), int) else top_ms
                model = entry.get("model") or model
            if kind == "orchestrator.policy":
                policy = True
            if kind == "agent.run" and entry.get("run_id"):
                child_meta[entry["run_id"]] = entry
                continue
            by_run.setdefault(entry.get("run_id") or top_id, []).append(entry)
        agent_events = {}
        for event in assistant.get("events") or []:
            if isinstance(event, dict) and event.get("type") == "agent" and event.get("run_id") and event.get("status") != "running":
                agent_events[event["run_id"]] = event
        multi = policy or bool(child_meta) or bool(agent_events)
        mode = "multi" if multi else "single"

        status = assistant.get("status")
        error = assistant.get("error") if isinstance(assistant.get("error"), dict) else {}
        code = error.get("code") if isinstance(error.get("code"), str) else None
        if status not in ("completed", "failed", "cancelled"):
            status = "unknown"
        if code == "cancelled":
            status = "cancelled"

        user_text = (user or {}).get("text") or ""
        local_handle = local_by_message.get((user or {}).get("message_id"))
        if local_handle:
            summary, chars = "", len(user_text)
        else:
            summary, chars = scrub.summary(user_text)
        attachments = []
        for item in (user or {}).get("attachments") or []:
            if isinstance(item, dict) and isinstance(item.get("handle"), str):
                attachments.append({k: item.get(k) for k in ("handle", "kind", "media_type", "size") if item.get(k) is not None})
        artifacts = []
        for event in assistant.get("events") or []:
            if isinstance(event, dict) and event.get("type") == "artifact" and isinstance(event.get("artifact_id"), str):
                artifacts.append(event["artifact_id"])
        out_summary, out_chars = scrub.summary(assistant.get("text") or "")

        top_steps = self._steps(by_run.get(top_id, [])) or self._runtime_steps(assistant)
        created = assistant.get("created_at") if isinstance(assistant.get("created_at"), str) else None
        top = {
            "schema": SCHEMA,
            "run_id": top_id,
            "parent_run_id": None,
            "trace_id": top_id,
            "session_id": session_id,
            "turn_id": assistant.get("message_id") or "",
            "agent": "orchestrator" if multi else "assistant",
            "role": "orchestrator" if multi else "assistant",
            "mode": mode,
            "depth": 0,
            "model": str(model or ""),
            "started_at": _iso(started) or created or "1970-01-01T00:00:00Z",
            "ended_at": _iso(ended),
            "ms": top_ms,
            "input": {"summary": summary, "chars": chars, "local_handle": local_handle, "attachments": attachments},
            "output": {"summary": out_summary, "chars": out_chars, "artifacts": artifacts},
            "outcome": {"status": status, "error_code": code,
                        "failure_class": classify(code) if code and status != "completed" else None},
        }
        self._rollup(top, top_steps)
        top["feedback"] = self._feedback(user, top, following)
        records = [top]
        child_ids = list(dict.fromkeys(list(agent_events) + list(child_meta)))
        delegations: dict[str, list[dict]] = {}
        for run_id in child_ids:
            meta = child_meta.get(run_id) or {}
            event = agent_events.get(run_id) or {}
            parent = meta.get("parent_run_id") or event.get("parent_run_id") or top_id
            child_status = meta.get("status") or event.get("status") or "unknown"
            if child_status not in ("completed", "failed", "cancelled"):
                child_status = "unknown"
            child_code = meta.get("error_code")
            text_summary, text_chars = scrub.summary(event.get("summary") or "")
            ms = meta.get("ms") if isinstance(meta.get("ms"), int) else (event.get("ms") if isinstance(event.get("ms"), int) else None)
            child = {
                "schema": SCHEMA,
                "run_id": run_id,
                "parent_run_id": parent,
                "trace_id": top_id,
                "session_id": session_id,
                "turn_id": top["turn_id"],
                "agent": str(meta.get("agent") or event.get("agent") or ""),
                "role": str(meta.get("role") or event.get("role") or ""),
                "mode": "multi",
                "depth": int(meta.get("depth") or event.get("depth") or 1),
                "model": str(meta.get("model") or event.get("model") or ""),
                "started_at": _iso(meta["ts"] - (ms or 0) / 1000) if isinstance(meta.get("ts"), (int, float)) else top["started_at"],
                "ended_at": _iso(meta.get("ts")),
                "ms": ms,
                "input": None,
                "output": {"summary": text_summary, "chars": text_chars, "artifacts": []},
                "outcome": {"status": child_status, "error_code": child_code,
                            "failure_class": classify(child_code) if child_code else None},
                "feedback": {"retried": False, "reseeded": False, "stopped": child_status == "cancelled"},
            }
            self._rollup(child, self._steps(by_run.get(run_id, [])))
            if child["budget"]["steps"] == 0 and isinstance(event.get("steps"), int):
                child["budget"]["steps"] = event["steps"]
            delegations.setdefault(parent, []).append({"run_id": run_id, "agent": child["agent"], "status": child_status})
            records.append(child)
        for record in records:
            record["delegations"] = delegations.get(record["run_id"], [])
            record["exported_at"] = _iso(time.time())
        return records

    def _feedback(self, user, top, following) -> dict:
        stopped = top["outcome"]["status"] == "cancelled"
        retried = reseeded = False
        if following:
            next_user, next_assistant = following
            same = (user or {}).get("text") and (next_user or {}).get("text") == (user or {}).get("text")
            retried = bool(same) or ((next_user or {}).get("text") or "").strip() in ("重试", "再来一次", "retry", "重新生成")
            mine = {(s.get("args") or {}).get("content_handle") for s in top["steps"] if s["name"] == "generate_image"} - {None}
            if mine:
                for event in next_assistant.get("events") or []:
                    args = event.get("arguments") if isinstance(event, dict) else None
                    if event.get("tool") == "generate_image" and isinstance(args, dict) and args.get("content_handle") in mine:
                        reseeded = True
        return {"retried": retried, "reseeded": reseeded, "stopped": stopped}

    # ---------- 写出和索引 ----------

    def load_index(self) -> dict:
        data = self._load_json(self.index_path)
        if not isinstance(data, dict) or data.get("schema") != INDEX_SCHEMA:
            return {"schema": INDEX_SCHEMA, "runs": {}, "sessions": {}}
        data.setdefault("runs", {})
        data.setdefault("sessions", {})
        return data

    def _save_index(self, index: dict) -> None:
        index["updated_at"] = _iso(time.time())
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False)
        os.replace(tmp, self.index_path)

    def _read_records(self) -> list[dict]:
        out = []
        if not os.path.isfile(self.jsonl):
            return out
        with open(self.jsonl, encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("run_id"):
                    out.append(item)
        return out

    def _rewrite(self, keep) -> None:
        records = [r for r in self._read_records() if keep(r)]
        tmp = self.jsonl + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp, self.jsonl)

    @staticmethod
    def _entry(record: dict) -> dict:
        return {"session_id": record["session_id"], "turn_id": record["turn_id"], "trace_id": record["trace_id"],
                "parent_run_id": record["parent_run_id"], "agent": record["agent"], "role": record["role"],
                "mode": record["mode"], "started_at": record["started_at"], "outcome": record["outcome"]["status"],
                "tools": sorted(set(record["tools"])), "skills": record["skills"]}

    def export(self, keep_days: int = KEEP_DAYS, since: str | None = None) -> dict:
        """增量导出，幂等。返回统计。"""
        with _lock:
            os.makedirs(self.out_dir, exist_ok=True)
            index = self.load_index()
            sessions = self._sessions()
            purge: set[str] = set()
            for session_id in list(index["sessions"]):
                if session_id not in sessions:
                    purge.add(session_id)
            for session_id, session in sessions.items():
                state = index["sessions"].get(session_id)
                handles = sorted(i.get("handle") for i in session.get("contents") or []
                                 if isinstance(i, dict) and isinstance(i.get("handle"), str))
                if state and not set(state.get("contents") or []) <= set(handles):
                    purge.add(session_id)   # 有本地内容被忘掉：重新生成这个会话的轨迹
            cutoff = _iso(time.time() - keep_days * 86400) if keep_days and keep_days > 0 else None
            removed = 0
            if purge or cutoff:
                stale = {rid for rid, e in index["runs"].items()
                         if e["session_id"] in purge or (cutoff and e["started_at"] < cutoff)}
                if stale:
                    self._rewrite(lambda r: r["run_id"] not in stale)
                    for rid in stale:
                        index["runs"].pop(rid, None)
                    removed = len(stale)
                for session_id in purge:
                    index["sessions"].pop(session_id, None)
            since_iso = _norm_time(since)
            written = dropped = skipped = 0
            todo = {}
            for session_id, session in sessions.items():
                state = index["sessions"].get(session_id) or {}
                if state.get("updated_at") == session.get("updated_at") and not any(
                        m.get("status") == "running" for m in session["messages"] if isinstance(m, dict)):
                    skipped += 1
                    continue
                todo[session_id] = session
            traces = {m.get("run_id") for s in todo.values() for m in s["messages"]
                      if isinstance(m, dict) and m.get("role") == "assistant" and m.get("run_id")}
            telemetry = self._telemetry(traces)
            with open(self.jsonl, "a", encoding="utf-8") as handle:
                for session_id, session in todo.items():
                    records, lost = self._build_safe(session_id, session, telemetry)
                    dropped += lost
                    for record in records:
                        if record["run_id"] in index["runs"]:
                            continue
                        if since_iso and record["started_at"] < since_iso:
                            continue
                        if cutoff and record["started_at"] < cutoff:
                            continue
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        index["runs"][record["run_id"]] = self._entry(record)
                        written += 1
                    running = any(m.get("status") == "running" for m in session["messages"] if isinstance(m, dict))
                    index["sessions"][session_id] = {
                        "updated_at": None if running else session.get("updated_at"),
                        "contents": sorted(i.get("handle") for i in session.get("contents") or []
                                           if isinstance(i, dict) and isinstance(i.get("handle"), str)),
                    }
            self._save_index(index)
            return {"written": written, "removed": removed, "dropped": dropped, "sessions_skipped": skipped,
                    "total": len(index["runs"]), "path": os.path.relpath(self.jsonl, self.data_dir)}

    def _build_safe(self, session_id, session, telemetry):
        try:
            return self.build_session(session_id, session, telemetry)
        except Exception:  # 一个坏会话不拖垮整个导出
            return [], 1

    # ---------- 查询 ----------

    def query(self, tool=None, agent=None, skill=None, outcome=None, since=None, until=None,
              session=None, mode=None, limit: int = 200, full: bool = True) -> list[dict]:
        index = self.load_index()
        since_iso, until_iso = _norm_time(since), _norm_time(until, end=True)
        hits = []
        for run_id, entry in index["runs"].items():
            if tool and tool not in entry["tools"]:
                continue
            if agent and agent not in (entry["agent"], entry["role"]):
                continue
            if skill and skill not in entry["skills"]:
                continue
            if outcome and entry["outcome"] != outcome:
                continue
            if mode and entry["mode"] != mode:
                continue
            if session and entry["session_id"] != session:
                continue
            if since_iso and entry["started_at"] < since_iso:
                continue
            if until_iso and entry["started_at"] > until_iso:
                continue
            hits.append(run_id)
        hits.sort(key=lambda rid: index["runs"][rid]["started_at"], reverse=True)
        hits = hits[:max(1, int(limit))] if limit else hits
        if not full:
            return [dict(index["runs"][rid], run_id=rid) for rid in hits]
        wanted = set(hits)
        found = {r["run_id"]: r for r in self._read_records() if r["run_id"] in wanted}
        return [found[rid] for rid in hits if rid in found]


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    default_dir = os.environ.get("CHAT_DATA_DIR") or os.path.join(ROOT, "apps", "web", "data")
    parser = argparse.ArgumentParser(prog="python -m services.meta.export", description="导出和查询去敏的轨迹（mas.trajectory/1）")
    parser.add_argument("command", nargs="?", default="export", choices=("export", "query"))
    parser.add_argument("--data-dir", default=default_dir)
    parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--tool")
    parser.add_argument("--agent")
    parser.add_argument("--skill")
    parser.add_argument("--outcome", choices=("completed", "failed", "cancelled", "unknown"))
    parser.add_argument("--mode", choices=("single", "multi"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--brief", action="store_true", help="查询只输出索引摘要")
    args = parser.parse_args(argv)
    exporter = Exporter(args.data_dir)
    if args.command == "export":
        print(json.dumps(exporter.export(keep_days=args.keep_days, since=args.since), ensure_ascii=False))
        return 0
    exporter.export(keep_days=args.keep_days)
    for item in exporter.query(tool=args.tool, agent=args.agent, skill=args.skill, outcome=args.outcome,
                               since=args.since, until=args.until, mode=args.mode, limit=args.limit, full=not args.brief):
        print(json.dumps(item, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
