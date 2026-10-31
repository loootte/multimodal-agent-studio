"""14.E（#82）调度器：到点 → 进「待开始」队列 + 桌面通知 → 用户点「开始本次」才真正跑。

- 总开关关着时 tick 什么都不做。
- 到点时上一轮还没结束（或上一条还在待开始）就跳过这次（skipped_overlap），不叠跑。
- 错过的不补跑（missed）；任务勾了「补跑」时，服务启动后最多补一次。
- 待开始超过 confirm_timeout_min 没人点就作废（confirm_timeout）。
- 每天次数到上限就跳过（daily_runs）。
- 句柄过期 / 被改动作废时任务停下（handle_expired），等用户「重新授权」。

时间一律是本机本地时间；tick(now) 可以注入时间，评测直接驱动。
"""

from __future__ import annotations

import datetime as _dt
import secrets
import threading

from services.scheduler import jobs as job_store

TICK_SEC = 15
GRACE_SEC = 120  # 到点后这么久之内算准点；更晚就算错过


def _local_now() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


class Scheduler:
    def __init__(self, store: job_store.JobStore, settings_fn, notify_fn=None, record_fn=None):
        self.store = store
        self.settings_fn = settings_fn
        self.notify_fn = notify_fn
        self.record_fn = record_fn
        self.pending: dict[str, dict] = {}
        self.running: dict[str, str] = {}  # job_id -> run_id
        self._caught_up: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 后台线程 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="mas-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SEC):
            try:
                self.tick()
            except Exception as exc:  # 调度器自己不能死；只记类型
                self._record("schedule.tick_failed", error=type(exc).__name__)

    def _record(self, kind: str, **fields) -> None:
        if self.record_fn is not None:
            try:
                self.record_fn(kind, **fields)
            except Exception:
                pass

    def enabled(self) -> bool:
        try:
            return bool(self.settings_fn().get("enabled"))
        except Exception:
            return False

    def _timeout_sec(self) -> int:
        try:
            return int(self.settings_fn().get("confirm_timeout_min") or 30) * 60
        except Exception:
            return 30 * 60

    # ---------- 历史 ----------

    def _history(self, job_id: str, entry_id: str, **fields) -> None:
        def apply(job):
            items = job.setdefault("history", [])
            found = next((item for item in items if item.get("id") == entry_id), None)
            if found is None:
                items.append({"id": entry_id, **fields})
            else:
                found.update(fields)
        self.store.mutate(job_id, apply)

    # ---------- tick ----------

    def tick(self, now: _dt.datetime | None = None) -> list[dict]:
        """检查一遍所有任务。返回这次做了什么（给评测和日志），不含任何正文。"""
        if not self.enabled():
            return []
        now = (now or _local_now()).replace(microsecond=0)
        actions: list[dict] = []
        with self._lock:
            for pending_id, item in list(self.pending.items()):
                if now.timestamp() >= item["expires_at"]:
                    self.pending.pop(pending_id, None)
                    self._history(item["job_id"], pending_id, status="confirm_timeout", code="confirm_timeout",
                                  at=job_store.local_iso(now))
                    self._record("schedule.timeout", job_id=item["job_id"])
                    actions.append({"job_id": item["job_id"], "action": "confirm_timeout"})
            for job_id in self.store.ids():
                action = self._tick_job(job_id, now)
                if action:
                    actions.append(action)
        return actions

    def _tick_job(self, job_id: str, now: _dt.datetime) -> dict | None:
        job = self.store.raw(job_id)
        if job is None or not job.get("enabled") or job.get("state") != "active":
            return None
        due = job_store.parse_iso(job.get("next_at"))
        if due is None or now < due:
            return None
        late = (now - due).total_seconds()
        caught_up = False
        if late > GRACE_SEC:
            if job.get("catch_up") and job_id not in self._caught_up:
                caught_up = True
                self._caught_up.add(job_id)
            else:
                self._advance(job_id, now, fired=False)
                entry = "h_" + secrets.token_hex(8)
                self._history(job_id, entry, status="missed", code="missed", due_at=job.get("next_at"),
                              at=job_store.local_iso(now))
                return {"job_id": job_id, "action": "missed"}
        self._advance(job_id, now, fired=True)
        return self._fire(job, now, job.get("next_at"), caught_up)

    def _advance(self, job_id: str, now: _dt.datetime, fired: bool) -> None:
        def apply(job):
            if fired:
                job["last_fire_at"] = job_store.local_iso(now)
            if job["spec"].get("kind") == "once":
                job["next_at"] = None
                job["state"] = "done" if job.get("state") == "active" else job.get("state")
                return
            anchor = job_store.parse_iso(job.get("anchor")) or now
            from services.scheduler import spec as specs

            job["next_at"] = job_store.local_iso(specs.next_fire(job["spec"], now, anchor))
        self.store.mutate(job_id, apply)

    def _daily_left(self, job: dict, now: _dt.datetime) -> bool:
        daily = job.get("daily") if isinstance(job.get("daily"), dict) else {}
        used = int(daily.get("count") or 0) if daily.get("date") == now.date().isoformat() else 0
        return used < int((job.get("budget") or {}).get("daily_runs") or 1)

    def _fire(self, job: dict, now: _dt.datetime, due_at, caught_up: bool) -> dict:
        job_id = job["job_id"]
        entry = "p_" + secrets.token_hex(16)
        base = {"due_at": due_at, "at": job_store.local_iso(now), "caught_up": caught_up}
        if self.store.handle_state(job_id) != "ok":
            self._history(job_id, entry, status="stopped", code="handle_expired", **base)
            self._record("schedule.stopped", job_id=job_id, code="handle_expired")
            return {"job_id": job_id, "action": "handle_expired"}
        if job_id in self.running or any(item["job_id"] == job_id for item in self.pending.values()):
            self._history(job_id, entry, status="skipped", code="skipped_overlap", **base)
            return {"job_id": job_id, "action": "skipped_overlap"}
        if not self._daily_left(job, now):
            self._history(job_id, entry, status="skipped", code="daily_runs", **base)
            return {"job_id": job_id, "action": "daily_runs"}
        self.pending[entry] = {"pending_id": entry, "job_id": job_id, "due_at": due_at,
                               "created_at": now.timestamp(), "expires_at": now.timestamp() + self._timeout_sec(),
                               "caught_up": caught_up}
        self._history(job_id, entry, status="waiting_start", code=None, **base)
        self._record("schedule.due", job_id=job_id, caught_up=caught_up)
        if self.notify_fn is not None:
            try:
                self.notify_fn("schedule")
            except Exception:
                pass
        return {"job_id": job_id, "action": "waiting_start", "pending_id": entry}

    # ---------- 用户操作 ----------

    def pending_list(self) -> list[dict]:
        with self._lock:
            out = []
            for item in self.pending.values():
                job = self.store.get(item["job_id"], audience="model") or {}
                out.append({"pending_id": item["pending_id"], "job_id": item["job_id"], "name": job.get("name"),
                            "kind": job.get("kind"), "due_at": item["due_at"], "caught_up": item["caught_up"],
                            "expires_at": job_store.local_iso(_dt.datetime.fromtimestamp(item["expires_at"]))})
            return out

    def start_pending(self, pending_id: str, launcher, now: _dt.datetime | None = None) -> dict:
        """用户点了「开始本次」。launcher(job 原始记录) -> (run_id, session_id)；出错抛 JobError。"""
        now = (now or _local_now()).replace(microsecond=0)
        with self._lock:
            item = self.pending.get(pending_id)
            if item is None:
                raise job_store.JobError("not_waiting", "这一次已经开始、过期或被取消了。", 409)
            job_id = item["job_id"]
            if now.timestamp() >= item["expires_at"]:
                self.pending.pop(pending_id, None)
                self._history(job_id, pending_id, status="confirm_timeout", code="confirm_timeout")
                raise job_store.JobError("confirm_timeout", "等待开始超时，这一次已作废。", 409)
            job = self.store.raw(job_id)
            if job is None:
                self.pending.pop(pending_id, None)
                raise job_store.JobError("not_found", "找不到这个定时任务。", 404)
            if self.store.handle_state(job_id) != "ok":
                self.pending.pop(pending_id, None)
                self._history(job_id, pending_id, status="stopped", code="handle_expired")
                raise job_store.JobError("handle_expired", "任务的句柄已失效（过期或任务被改过），先「重新授权」。", 409)
            if job_id in self.running:
                raise job_store.JobError("busy", "上一轮还没结束。", 409)
            if not self._daily_left(job, now):
                self.pending.pop(pending_id, None)
                self._history(job_id, pending_id, status="skipped", code="daily_runs")
                raise job_store.JobError("daily_runs", "今天的运行次数已经到上限。", 409)
            self.pending.pop(pending_id, None)
            run_id, session_id = launcher(job, pending_id)
            self.running[job_id] = run_id
            today = now.date().isoformat()

            def count(record):
                daily = record.get("daily") if isinstance(record.get("daily"), dict) else {}
                used = int(daily.get("count") or 0) if daily.get("date") == today else 0
                record["daily"] = {"date": today, "count": used + 1}
                record["session_id"] = session_id
            self.store.mutate(job_id, count)
            self._history(job_id, pending_id, status="running", code=None, run_id=run_id,
                          started_at=job_store.local_iso(now))
            self._record("schedule.started", job_id=job_id)
            return {"pending_id": pending_id, "job_id": job_id, "run_id": run_id, "session_id": session_id}

    def dismiss(self, pending_id: str) -> dict:
        with self._lock:
            item = self.pending.pop(pending_id, None)
            if item is None:
                raise job_store.JobError("not_waiting", "这一次已经开始、过期或被取消了。", 409)
            self._history(item["job_id"], pending_id, status="skipped", code="dismissed")
            return {"ok": True}

    def finish(self, job_id: str, entry_id: str, run_id: str, status: str, code=None, reason=None) -> None:
        with self._lock:
            if self.running.get(job_id) == run_id:
                self.running.pop(job_id, None)
            fields = {"status": status, "code": code, "finished_at": job_store.local_iso(_local_now())}
            if reason:
                fields["reason"] = str(reason)[:120]
            self._history(job_id, entry_id, **fields)
            self._record("schedule.finished", job_id=job_id, status=status, code=code)

    def forget(self, job_id: str) -> None:
        """任务被删 / 改了：丢掉它的待开始项。正在跑的那一轮由调用方停掉。"""
        with self._lock:
            for pending_id, item in list(self.pending.items()):
                if item["job_id"] == job_id:
                    self.pending.pop(pending_id, None)
            self._caught_up.discard(job_id)
