"""14.E（#82）定时任务存储：mas.schedule/1。

- 任务正文（出图提示词 / 文字指令）只在本机，落盘跟随 12.F：encrypted 用 mas/schedule/store 派生的独立密钥加密，
  memory 只在内存，plaintext 时出图任务（本地正文）默认拒绝（schedule_needs_encryption），文字指令照常存。
- 每个任务有一枚 purpose=scheduled 的句柄（sh_…），绑定任务编号，有效期 MAS_SCHEDULED_HANDLE_TTL_SEC（默认 7 天）。
  这枚句柄只给宿主用，从不进模型请求；跨任务解析一律 handle_forbidden。
- 改任务：旧句柄立即作废，任务停下（handle_expired），常驻授权一并作废；用户点「重新授权」签发新句柄。
  过期一样处理。删任务：正文、句柄、常驻授权、历史全清。
- 常驻授权（选项 C）：只给出图任务 + 内置 generate_image + 本机 ComfyUI；只能由用户在面板经宿主确认卡打开；
  绑定任务编号 + 当前句柄 + 后端主机，任何一项变了就无效。

本模块只管存取和句柄状态，不判断能不能运行、能不能给模型看。
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import secrets
import threading
import time

from packages.artifacts import crypto as storage_crypto
from services.runtime import scheduler as rules
from services.scheduler import spec as specs

SCHEMA = "mas.schedule/1"
PURPOSE = "scheduled"
KINDS = ("image", "text")
STATES = ("active", "needs_reauth", "done")
ID_RE = re.compile(r"j_[0-9a-f]{32}")
HANDLE_RE = re.compile(r"sh_[0-9a-f]{32}")
NAME_CHARS = 40
BODY_CHARS = 8000
HISTORY = 30
FILE_NAME = "schedules.json"
SEAL_ID = "store"
DEFAULT_TTL = 7 * 24 * 3600


def handle_ttl() -> int:
    raw = os.environ.get("MAS_SCHEDULED_HANDLE_TTL_SEC")
    try:
        value = int(raw) if raw not in (None, "") else DEFAULT_TTL
    except ValueError:
        value = DEFAULT_TTL
    return max(1, min(value, 90 * 24 * 3600))


class JobError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def local_iso(moment: _dt.datetime | None) -> str | None:
    return moment.replace(microsecond=0).isoformat() if moment else None


def parse_iso(value) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def clean_name(value) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:NAME_CHARS] or "定时任务"


class JobStore:
    def __init__(self, data_dir: str, storage: "storage_crypto.Storage | None" = None, clock=time.time):
        self.dir = os.path.join(data_dir, "schedules")
        self.path = os.path.join(self.dir, FILE_NAME)
        self.storage = storage if storage is not None else storage_crypto.Storage("plaintext")
        self.mode = self.storage.mode
        self.cipher = self.storage.cipher if self.mode == "encrypted" else None
        self.clock = clock
        self._lock = threading.RLock()
        self._volatile: dict | None = None
        self.corrupt = False

    # ---------- 底层 ----------

    @property
    def encrypted(self) -> bool:
        return self.mode in ("encrypted", "memory")

    def _empty(self) -> dict:
        return {"schema": SCHEMA, "jobs": [], "handles": []}

    def _load(self) -> dict:
        if self.mode == "memory":
            if self._volatile is None:
                self._volatile = self._empty()
            return copy.deepcopy(self._volatile)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            self.corrupt = False
            return self._empty()
        except (OSError, ValueError):
            self.corrupt = True
            return self._empty()
        try:
            if storage_crypto.is_envelope(raw):
                if self.cipher is None:
                    raise storage_crypto.StorageError("storage_key_missing")
                raw = self.cipher.open_json(SEAL_ID, raw, scope="schedule")
            elif self.cipher is not None:
                raise storage_crypto.StorageError("storage_decrypt_failed", "not_envelope")
        except storage_crypto.StorageError:
            self.corrupt = True
            return self._empty()
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            self.corrupt = True
            return self._empty()
        self.corrupt = False
        jobs = [j for j in raw.get("jobs") or [] if isinstance(j, dict) and valid_id(j.get("job_id"))
                and j.get("kind") in KINDS]
        handles = [h for h in raw.get("handles") or [] if isinstance(h, dict)
                   and isinstance(h.get("handle"), str) and HANDLE_RE.fullmatch(h["handle"])]
        return {"schema": SCHEMA, "jobs": jobs, "handles": handles}

    def _save(self, doc: dict) -> None:
        if self.corrupt:
            raise JobError("schedule_corrupt", "定时任务文件读不出来（坏了或密钥不对），没有覆盖它。", 409)
        if self.mode == "memory":
            self._volatile = copy.deepcopy(doc)
            return
        payload = self.cipher.seal_json(SEAL_ID, doc, scope="schedule") if self.cipher is not None else doc
        os.makedirs(self.dir, exist_ok=True)
        temp = self.path + ".part"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, self.path)

    @staticmethod
    def _find(doc: dict, job_id: str) -> dict | None:
        if not valid_id(job_id):
            return None
        return next((j for j in doc["jobs"] if j.get("job_id") == job_id), None)

    def _now(self) -> float:
        return float(self.clock())

    def _local_now(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self._now()).replace(microsecond=0)

    # ---------- 句柄 ----------

    def _issue(self, doc: dict, job: dict) -> str:
        now = self._now()
        # 作废 / 过期超过 30 天的旧句柄记录不再留（只留够判断 handle_expired 的近期记录）
        doc["handles"] = [h for h in doc["handles"]
                          if now - float(h.get("revoked_at") or h.get("expires_at") or now) < 30 * 86400]
        record = {"handle": "sh_" + secrets.token_hex(16), "job_id": job["job_id"], "purpose": PURPOSE,
                  "issued_at": now, "expires_at": now + handle_ttl(), "revoked_at": None, "reason": None}
        doc["handles"].append(record)
        job["handle"] = record["handle"]
        job["state"] = "active"
        job["stop_reason"] = None
        return record["handle"]

    def _revoke(self, doc: dict, job: dict, reason: str) -> None:
        now = self._now()
        for record in doc["handles"]:
            if record.get("job_id") == job["job_id"] and not record.get("revoked_at"):
                record["revoked_at"] = now
                record["reason"] = reason
        job["handle"] = None
        job["standing"] = None  # 常驻授权绑定句柄：句柄换了就无效

    def _handle_code(self, doc: dict, job_id: str, handle) -> str:
        if not isinstance(handle, str) or not HANDLE_RE.fullmatch(handle):
            return "handle_not_found"
        record = next((h for h in doc["handles"] if h.get("handle") == handle), None)
        if record is None:
            return "handle_not_found"
        if record.get("job_id") != job_id or record.get("purpose") != PURPOSE:
            return "handle_forbidden"
        if record.get("revoked_at") or self._now() >= float(record.get("expires_at") or 0):
            return "handle_expired"
        return "ok"

    def resolve(self, job_id: str, handle) -> tuple[str | None, str]:
        """宿主取任务正文的唯一入口：(正文, "ok") 或 (None, 错误码)。跨任务的句柄是 handle_forbidden。"""
        with self._lock:
            doc = self._load()
            code = self._handle_code(doc, job_id, handle)
            if code != "ok":
                return None, code
            job = self._find(doc, job_id)
            if job is None:
                return None, "handle_not_found"
            if job.get("handle") != handle:
                return None, "handle_expired"
            return job.get("body"), "ok"

    def handle_state(self, job_id: str) -> str:
        """当前句柄的状态：ok / handle_expired / handle_not_found。过期时顺手把任务标成待重新授权。"""
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None:
                return "handle_not_found"
            if not job.get("handle"):
                return "handle_expired"
            code = self._handle_code(doc, job_id, job["handle"])
            if code == "handle_expired" and job.get("state") == "active":
                self._revoke(doc, job, "expired")
                job["state"] = "needs_reauth"
                job["stop_reason"] = "handle_expired"
                self._save(doc)
            return code

    # ---------- 视图 ----------

    def _public(self, doc: dict, job: dict, audience: str = "panel") -> dict:
        out = {
            "job_id": job["job_id"],
            "name": job.get("name"),
            "kind": job.get("kind"),
            "schedule": dict(job.get("spec") or {}),
            "schedule_text": specs.describe(job.get("spec") or {}),
            "enabled": bool(job.get("enabled")),
            "state": job.get("state"),
            "stop_reason": job.get("stop_reason"),
            "next_at": job.get("next_at"),
            "catch_up": bool(job.get("catch_up")),
            "images": int(job.get("images") or 1) if job.get("kind") == "image" else None,
            "budget": dict(job.get("budget") or {}),
            "source": job.get("source"),
        }
        if audience == "model":
            return out  # 模型只看到编号、名字、时间表和状态；没有正文、没有句柄
        handle = job.get("handle")
        record = next((h for h in doc["handles"] if h.get("handle") == handle), None) if handle else None
        standing = job.get("standing") if isinstance(job.get("standing"), dict) else None
        out.update({
            "handle": handle,
            "handle_expires_at": local_iso(_dt.datetime.fromtimestamp(record["expires_at"])) if record else None,
            "standing": {"backend_host": standing.get("backend_host"), "granted_at": standing.get("granted_at")}
            if standing else None,
            "session_id": job.get("session_id"),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "last_fire_at": job.get("last_fire_at"),
            "body_chars": len(job.get("body") or ""),
            "history": list(job.get("history") or [])[-HISTORY:],
            "daily": dict(job.get("daily") or {}),
        })
        return out

    def list(self, audience: str = "panel") -> list[dict]:
        with self._lock:
            doc = self._load()
            return [self._public(doc, job, audience) for job in doc["jobs"]]

    def get(self, job_id: str, audience: str = "panel") -> dict | None:
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            return self._public(doc, job, audience) if job else None

    def raw(self, job_id: str) -> dict | None:
        """宿主内部用（调度器）：任务记录副本，含正文。不能返回给浏览器或模型。"""
        with self._lock:
            job = self._find(self._load(), job_id)
            return copy.deepcopy(job) if job else None

    def ids(self) -> list[str]:
        with self._lock:
            return [job["job_id"] for job in self._load()["jobs"]]

    def bodies(self) -> list[str]:
        """出图任务的本地正文（出口检查用）。文字指令本来就要发给模型，不算。"""
        with self._lock:
            return [job["body"] for job in self._load()["jobs"] if job.get("kind") == "image" and job.get("body")]

    # ---------- 写 ----------

    def _fields(self, fields: dict, base: dict | None, allow_plaintext: bool = False) -> dict:
        out: dict = {}
        if base is None or "kind" in fields:
            kind = fields.get("kind")
            if kind not in KINDS:
                raise JobError("invalid_parameter", "任务类型只能是 image（出图）或 text（文字）。")
            if base is not None and kind != base.get("kind"):
                raise JobError("invalid_parameter", "任务类型不能改；删掉重建。")
            out["kind"] = kind
        kind = out.get("kind") or base.get("kind")
        if base is None or "name" in fields:
            out["name"] = clean_name(fields.get("name"))
        if base is None or "schedule" in fields:
            try:
                out["spec"] = specs.validate(fields.get("schedule"))
            except specs.SpecError as exc:
                raise JobError(exc.code, exc.message) from exc
        if base is None or "body" in fields:
            body = fields.get("body")
            if not isinstance(body, str) or not body.strip():
                raise JobError("empty", "任务内容不能为空。")
            if len(body) > BODY_CHARS:
                raise JobError("payload_too_large", f"任务内容最多 {BODY_CHARS} 字。")
            if kind == "image" and not (self.encrypted or allow_plaintext):
                raise JobError("schedule_needs_encryption",
                               "当前是明文存储：出图任务的提示词是本地内容，只加密落盘。打开存储加密，"
                               "或在调度设置里明确允许明文保存。")
            out["body"] = body.strip()
        if base is None or "images" in fields:
            images = fields.get("images", 1)
            if kind == "text":
                images = 1
            if isinstance(images, bool) or not isinstance(images, int) or not 1 <= images <= rules.MAX_IMAGES:
                raise JobError("invalid_parameter", f"每次张数要在 1–{rules.MAX_IMAGES}。")
            out["images"] = images
        if base is None or "budget" in fields:
            values, bad = rules.budget(fields.get("budget"), (base or {}).get("budget"))
            if bad:
                raise JobError("invalid_parameter", "预算超出范围：" + "、".join(bad))
            out["budget"] = values
        for key in ("catch_up", "enabled"):
            if base is None or key in fields:
                value = fields.get(key, key == "enabled")
                if not isinstance(value, bool):
                    raise JobError("invalid_parameter", f"{key} 要是布尔值。")
                out[key] = value
        return out

    def _schedule_next(self, job: dict, after: _dt.datetime) -> None:
        anchor = parse_iso(job.get("anchor")) or after
        moment = specs.next_fire(job["spec"], after, anchor)
        job["next_at"] = local_iso(moment)
        if moment is None and job["spec"].get("kind") == "once" and job.get("last_fire_at"):
            job["state"] = "done" if job.get("state") == "active" else job.get("state")

    def create(self, fields: dict, source: str = "user", max_jobs: int = 20, allow_plaintext: bool = False) -> dict:
        with self._lock:
            doc = self._load()
            if len(doc["jobs"]) >= max_jobs:
                raise JobError("too_many_jobs", f"最多 {max_jobs} 个定时任务。", 409)
            values = self._fields(fields, None, allow_plaintext)
            now = self._local_now()
            job = {"job_id": "j_" + secrets.token_hex(16), **values, "source": source if source in ("user", "model")
                   else "user", "state": "active", "stop_reason": None, "handle": None, "standing": None,
                   "session_id": None, "created_at": local_iso(now), "updated_at": local_iso(now),
                   "anchor": local_iso(now), "next_at": None, "last_fire_at": None, "history": [],
                   "daily": {}, "session_handles": []}
            self._issue(doc, job)
            self._schedule_next(job, now)
            doc["jobs"].append(job)
            self._save(doc)
            return self._public(doc, job)

    def update(self, job_id: str, fields: dict, allow_plaintext: bool = False) -> tuple[dict, list[dict]]:
        """改任务：旧句柄立即作废、常驻授权作废，任务停在 needs_reauth（handle_expired），等用户「重新授权」。
        只改开关（enabled）和「补跑」不算改内容，句柄不动。返回 (新视图, 要作废的会话句柄)。"""
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None:
                raise JobError("not_found", "找不到这个定时任务。", 404)
            values = self._fields(fields, job, allow_plaintext)
            changed = any(job.get(key) != value for key, value in values.items() if key not in ("enabled", "catch_up"))
            job.update(values)
            now = self._local_now()
            job["updated_at"] = local_iso(now)
            stale: list[dict] = []
            if changed:
                self._revoke(doc, job, "changed")
                job["state"] = "needs_reauth"
                job["stop_reason"] = "handle_expired"
                job["anchor"] = local_iso(now)
                stale = [dict({"session_id": job.get("session_id")}, **item) for item in job.get("session_handles") or []]
                job["session_handles"] = []
            self._schedule_next(job, now)
            self._save(doc)
            return self._public(doc, job), stale

    def reauthorize(self, job_id: str) -> dict:
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None:
                raise JobError("not_found", "找不到这个定时任务。", 404)
            self._revoke(doc, job, "reissued")
            self._issue(doc, job)
            now = self._local_now()
            if job["spec"].get("kind") == "once" and job.get("last_fire_at"):
                job["last_fire_at"] = None  # 一次性任务重新授权后再按原时间（如果还没过）跑
            self._schedule_next(job, now)
            self._save(doc)
            return self._public(doc, job)

    def delete(self, job_id: str) -> tuple[bool, list[dict]]:
        """删任务：正文、句柄记录、常驻授权、历史全部清掉。返回 (删了没有, 要清掉的会话句柄)。"""
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None:
                return False, []
            stale = [dict({"session_id": job.get("session_id")}, **item) for item in job.get("session_handles") or []]
            doc["jobs"] = [j for j in doc["jobs"] if j is not job]
            doc["handles"] = [h for h in doc["handles"] if h.get("job_id") != job_id]
            self._save(doc)
            return True, stale

    def grant_standing(self, job_id: str, backend_host: str) -> dict:
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None:
                raise JobError("not_found", "找不到这个定时任务。", 404)
            if job.get("kind") != "image":
                raise JobError("standing_not_allowed", "常驻授权只给出图任务（内置 generate_image + 本机 ComfyUI）。")
            if not job.get("handle") or self._handle_code(doc, job_id, job["handle"]) != "ok":
                raise JobError("handle_expired", "任务的句柄已失效，先「重新授权」。", 409)
            if not isinstance(backend_host, str) or not backend_host:
                raise JobError("standing_not_allowed", "没有可用的本机 ComfyUI。")
            job["standing"] = {"handle": job["handle"], "backend_host": backend_host,
                               "granted_at": local_iso(self._local_now())}
            self._save(doc)
            return self._public(doc, job)

    def revoke_standing(self, job_id: str) -> dict:
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None:
                raise JobError("not_found", "找不到这个定时任务。", 404)
            job["standing"] = None
            self._save(doc)
            return self._public(doc, job)

    def standing_ok(self, job_id: str, backend_host: str | None) -> bool:
        """常驻授权现在还算不算数：在、绑定的是当前有效句柄、后端主机没变、任务是出图任务。"""
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None or job.get("kind") != "image" or job.get("state") != "active":
                return False
            standing = job.get("standing")
            if not isinstance(standing, dict) or not backend_host:
                return False
            if standing.get("handle") != job.get("handle") or standing.get("backend_host") != backend_host:
                return False
            return self._handle_code(doc, job_id, job.get("handle")) == "ok"

    def mutate(self, job_id: str, fn) -> dict | None:
        """调度器内部：在锁里改一条任务记录（历史、下一次时间、每日计数、会话）。"""
        with self._lock:
            doc = self._load()
            job = self._find(doc, job_id)
            if job is None:
                return None
            fn(job)
            job["history"] = list(job.get("history") or [])[-HISTORY:]
            self._save(doc)
            return self._public(doc, job)
