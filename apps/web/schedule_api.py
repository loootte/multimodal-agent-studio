"""14.E（#82）定时任务的网页宿主：调度器接线、到点运行（专用会话）、面板 API、四个 schedule_* 模型工具。

不变量（见 docs/scheduler.md、ADR 0011）：
- 总开关默认关；关着时不触发、不给模型 schedule_* 工具，请求和以前逐字节一致。
- 每次到点都进「待开始」队列 + 桌面通知；用户点「开始本次」才跑。超时 confirm_timeout。
- 运行里的确认照常弹。唯一例外是用户在面板里经宿主确认卡打开的「常驻授权」：只免掉内置 generate_image
  发往本机（loopback）ComfyUI 的确认，绑定任务编号 + 当前句柄 + 后端主机，最多 images 次；远端 / 高危、
  其它 confirm:always 工具一律照常问。模型不能设置常驻授权。
- 出图任务的提示词只以本会话句柄（content_handle）给模型；任务自己的 scheduled 句柄只在宿主里。
- 模型给的本地内容只收句柄；模型写的名字 / 文字指令要过本地正文检查。
"""

from __future__ import annotations

import hashlib
import secrets
import sys
import threading
import time
import uuid

import memory_api
from services import telemetry
from services.gateway import egress
from services.gateway.egress import LocalBodies
from services.runtime import memory as memory_rules
from services.runtime import scheduler as rules
from services.scheduler import jobs as job_store
from services.scheduler.jobs import JobError
from services.scheduler.runner import Scheduler

STANDING_TTL_SEC = 300
HUMAN = {
    "scheduler_disabled": "定时任务没有打开（设置 → 定时任务）。",
    "local_content": "文字里有本机内容正文，没有收下。本地内容只能用句柄（content_handle）给定时任务。",
    "standing_by_model": "常驻授权只能由你在定时任务面板里打开，模型不能设置。",
    "invalid_parameter": "参数不对。",
}


class PanelError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------- 接线 ----------

def values(app) -> dict:
    return app.schedule_settings.current()


def enabled(app) -> bool:
    try:
        return bool(values(app).get("enabled"))
    except Exception:
        return False


def tool_include(app) -> tuple:
    """调度打开时放进单 Agent 文字轮请求的工具；关着时是空的。"""
    return rules.TOOLS if enabled(app) else ()


def _record(kind: str, **fields) -> None:
    try:
        telemetry.record(kind, trace_id="sch_" + uuid.uuid4().hex, **fields)
    except Exception:
        pass


def install(app, data_dir: str) -> None:
    import os

    import store

    app.schedules = job_store.JobStore(data_dir, app.storage)
    app.schedule_settings = store.ContextSettingsStore(os.path.join(data_dir, "scheduler_settings.json"),
                                                       module="scheduler")
    app._standing_offers = {}

    def notify(kind):
        hub = getattr(app, "notify", None)
        if hub is not None and hasattr(hub, "schedule"):
            hub.schedule(kind)

    app.scheduler = Scheduler(app.schedules, lambda: values(app), notify_fn=notify, record_fn=_record)


def comfy_destination() -> tuple[str | None, str]:
    import image_run

    base = image_run.comfy_base()
    if not base:
        return None, "third_party"
    host, scope = egress.destination(base)
    return (host or None), scope


def egress_bodies(app, session_id: str):
    """运行时出口检查用：记忆那一套 + 调度打开时所有出图任务的提示词（本地正文）。"""
    local = memory_api.egress_bodies(app, session_id)
    try:
        extra = app.schedules.bodies() if enabled(app) else []
    except Exception:
        extra = []
    extra = [text for text in extra if text not in local]
    if not extra:
        return local
    prints = list(getattr(local, "fingerprints", ()) or ())
    prints.extend(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in extra)
    return LocalBodies(list(local) + extra, prints)


def _known(app, session_id: str | None) -> LocalBodies:
    base = memory_api.known_bodies(app, session_id)
    extra = [text for text in app.schedules.bodies() if text not in base]
    prints = list(getattr(base, "fingerprints", ()) or ())
    prints.extend(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in extra)
    return LocalBodies(list(base) + extra, prints)


def _check(app, session_id, texts) -> None:
    reason = memory_rules.check_text(texts, _known(app, session_id))
    if reason:
        egress.security_event(reason, "schedule")
        raise JobError(reason, HUMAN["local_content"])


# ---------- 到点运行 ----------

def _job_line(job: dict, handle: str) -> str:
    """出图任务发给模型的那一句：宿主模板，只有任务名、句柄和张数，没有提示词正文。"""
    images = int(job.get("images") or 1)
    return (f"定时任务「{job.get('name')}」到点了（用户已点「开始本次」）。请用本机提示词 content_handle={handle} 出图，"
            f"共 {images} 张：先用 plan_images 报张数，再逐张调用 generate_image（每次一张，用这个 content_handle）。"
            "提示词正文只在本机，你看不到也不需要它。")


def confirm_timeout_sec(app) -> float:
    """待开始队列和运行里工具确认共用的等待上限（设置里的分钟数，默认 30）。"""
    return float(int(values(app).get("confirm_timeout_min") or 30) * 60)


def _ensure_session(app, job: dict) -> str:
    session_id = job.get("session_id")
    if session_id:
        try:
            app.sessions.get(session_id)
            return session_id
        except Exception:
            pass
    user = app.identity.current()
    return app.sessions.create(user["user_id"])["session_id"]


def launch(app, job: dict, entry_id: str) -> tuple[str, str]:
    """用户点了「开始本次」：在任务的专用会话里开一轮。返回 (run_id, session_id)。"""
    import chat_run

    session_id = _ensure_session(app, job)
    budget = dict(rules.BUDGET_DEFAULTS, **(job.get("budget") or {}))
    context = {
        "job_id": job["job_id"], "entry_id": entry_id, "kind": job["kind"], "images": int(job.get("images") or 1),
        "budget": budget, "confirm_timeout_sec": confirm_timeout_sec(app),
        "content_handle": None, "standing_used": 0,
    }
    if job["kind"] == "image":
        record = app.sessions.add_content(session_id, job["body"], "image_prompt")
        handle = record["handle"]
        context["content_handle"] = handle

        def remember(entry):
            items = [item for item in entry.get("session_handles") or [] if item.get("handle") != handle]
            entry["session_handles"] = items + [{"handle": handle, "session_id": session_id}]
        app.schedules.mutate(job["job_id"], remember)
        text = _job_line(job, handle)
    else:
        text = job["body"]
    try:
        run = chat_run.start(app, session_id, text, [], job=context)
    except chat_run.RequestError as exc:
        raise JobError(exc.code, exc.message, 409) from exc
    threading.Thread(target=_watch, args=(app, job["job_id"], entry_id, run, session_id), daemon=True).start()
    return run.run_id, session_id


def _watch(app, job_id: str, entry_id: str, run, session_id: str) -> None:
    run.done.wait()
    status, code, reason = "failed", None, None
    try:
        for message in (app.sessions.get(session_id) or {}).get("messages") or []:
            if message.get("run_id") == run.run_id or message.get("message_id") == run.message_id:
                status = str(message.get("status") or "failed")
                error = message.get("error") if isinstance(message.get("error"), dict) else {}
                code = error.get("code")
                reason = error.get("message")
    except Exception:
        pass
    stop = getattr(run, "_stop_code", None)
    if stop:
        status, code = "stopped", stop
        reason = "等待确认超时，已经停下。" if stop == "confirm_timeout" else reason
    elif code == "budget_exceeded":
        status = "stopped"
    elif status == "completed":
        code = None
    app.scheduler.finish(job_id, entry_id, run.run_id, status, code or (None if status == "completed" else status),
                         reason)


def standing_allows(app, run, name: str, arguments) -> bool:
    """运行时 mutating 里问：这一次 generate_image 能不能用常驻授权免确认。只在全部条件都满足时才是 True。"""
    job = getattr(run, "job", None)
    if not isinstance(job, dict) or job.get("kind") != "image" or name != "generate_image":
        return False
    if not isinstance(arguments, dict):
        return False
    if arguments.get("backend", "local") != "local":  # 远端出图是高危组合，永远要确认
        return False
    handle = job.get("content_handle")
    if not handle or arguments.get("content_handle") != handle:
        return False
    if int(job.get("standing_used") or 0) >= int(job.get("images") or 1):
        return False
    try:
        from services.tools import packs

        pack = packs.find_tool("generate_image")
        if pack is None or not pack.builtin:
            return False
        host, scope = comfy_destination()
        if scope != "local" or not app.schedules.standing_ok(job["job_id"], host):
            return False
        record, code = app.sessions.resolve_content(run.session_id, handle, tool="generate_image",
                                                    run_id=run.run_id, consume=False)
        body, state = app.schedules.resolve(job["job_id"], (app.schedules.raw(job["job_id"]) or {}).get("handle"))
        if code != "ok" or state != "ok" or not record or record.get("text") != body:
            return False
    except Exception:
        return False
    job["standing_used"] = int(job.get("standing_used") or 0) + 1
    _record("schedule.standing_used", job_id=job["job_id"], host=host)
    return True


# ---------- 面板 API ----------

def _audit(action: str, fields) -> None:
    try:
        telemetry.record("security.config_changed", trace_id="cfg_" + uuid.uuid4().hex, target="schedule",
                         fields=sorted(str(item)[:48] for item in fields)[:20], via="schedule_panel", action=action)
    except Exception as exc:
        print(f"警告：调度审计失败 {type(exc).__name__}", file=sys.stderr)


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except JobError as exc:
        raise PanelError(exc.status, exc.code, exc.message) from exc


def view(app) -> dict:
    settings = app.schedule_settings.public()
    host, scope = comfy_destination()
    return {
        "settings": settings,
        "storage_mode": app.schedules.mode,
        "corrupt": app.schedules.corrupt,
        "comfy": {"host": host, "scope": scope, "standing_possible": scope == "local"},
        "jobs": app.schedules.list(),
        "pending": app.scheduler.pending_list(),
        "running": dict(app.scheduler.running),
        "limits": {"budget": {k: list(v) for k, v in rules.BUDGET_LIMITS.items()}, "max_images": rules.MAX_IMAGES},
    }


_PANEL_FIELDS = ("name", "kind", "schedule", "body", "images", "budget", "catch_up", "enabled")


def _panel_fields(body: dict) -> dict:
    if any(key in body for key in ("standing", "preauthorize", "handle")):
        raise PanelError(400, "invalid_parameter", "常驻授权要单独经确认卡打开。")
    return {key: body[key] for key in _PANEL_FIELDS if key in body}


def create(app, body: dict) -> dict:
    fields = _panel_fields(body)
    _wrap(_check, app, None, [fields.get("name") or ""])
    current = values(app)
    job = _wrap(app.schedules.create, fields, source="user", max_jobs=int(current.get("max_jobs") or 20),
                allow_plaintext=bool(current.get("allow_plaintext_image")))
    _audit("create", [job["kind"]])
    return job


def _drop_session_handles(app, stale, forget: bool) -> None:
    for item in stale or []:
        session_id, handle = item.get("session_id"), item.get("handle")
        if not session_id or not handle:
            continue
        try:
            if forget:
                app.sessions.forget_content(session_id, handle)
            else:
                app.sessions.revoke_content(session_id, handle)
        except Exception:
            pass


def _stop_running(app, job_id: str) -> None:
    run_id = app.scheduler.running.get(job_id)
    run = app.runs.get(run_id) if run_id else None
    if run is not None:
        run._stop_code = getattr(run, "_stop_code", None) or "handle_expired"
        run.stop()


def update(app, job_id: str, body: dict, source: str = "user", session_id: str | None = None) -> dict:
    fields = _panel_fields(body) if source == "user" else body
    if "name" in fields:
        _wrap(_check, app, session_id, [fields.get("name") or ""])
    current = values(app)
    job, stale = _wrap(app.schedules.update, job_id, fields, allow_plaintext=bool(current.get("allow_plaintext_image")))
    if job.get("state") == "needs_reauth":
        app.scheduler.forget(job_id)
        _stop_running(app, job_id)
        _drop_session_handles(app, stale, forget=False)
        _record("schedule.handle_revoked", job_id=job_id, reason="changed")
    _audit("update", sorted(fields))
    return job


def delete(app, job_id: str) -> dict:
    _stop_running(app, job_id)
    removed, stale = app.schedules.delete(job_id)
    if not removed:
        raise PanelError(404, "not_found", "找不到这个定时任务。")
    app.scheduler.forget(job_id)
    _drop_session_handles(app, stale, forget=True)
    for offer_id, offer in list(app._standing_offers.items()):
        if offer.get("job_id") == job_id:
            app._standing_offers.pop(offer_id, None)
    _audit("delete", ["job"])
    return {"ok": True, "job_id": job_id}


def reauthorize(app, job_id: str) -> dict:
    job = _wrap(app.schedules.reauthorize, job_id)
    _audit("reauthorize", ["handle"])
    return job


def standing_offer(app, job_id: str) -> dict:
    """第一步：宿主生成确认卡（固定文案 + 任务名 + 后端主机），给一次性的 offer_id。不改任何东西。"""
    job = app.schedules.get(job_id)
    if job is None:
        raise PanelError(404, "not_found", "找不到这个定时任务。")
    if job["kind"] != "image":
        raise PanelError(400, "standing_not_allowed", "常驻授权只给出图任务。")
    if job["state"] != "active" or app.schedules.handle_state(job_id) != "ok":
        raise PanelError(409, "handle_expired", "任务的句柄已失效，先「重新授权」。")
    host, scope = comfy_destination()
    if scope != "local" or not host:
        raise PanelError(409, "standing_not_allowed", "常驻授权只给本机（127.0.0.1 / localhost）上的 ComfyUI。")
    offer_id = "so_" + secrets.token_hex(16)
    app._standing_offers[offer_id] = {"job_id": job_id, "handle": job["handle"], "host": host,
                                      "expires": time.time() + STANDING_TTL_SEC}
    card = {
        "title": "打开常驻授权",
        "lines": [
            f"任务：{job['name']}",
            f"只免确认：内置 generate_image → 本机 ComfyUI（{host}），每次最多 {job['images']} 张。",
            "每次到点仍要你点「开始本次」；远端出图、其它要确认的工具照常每次问。",
            "改任务、句柄过期、重新授权、删任务或换 ComfyUI 地址，授权立即作废。",
        ],
        "requires_ack": "standing",
    }
    return {"offer_id": offer_id, "card": card}


def standing_confirm(app, job_id: str, body: dict) -> dict:
    offer = app._standing_offers.pop(body.get("offer_id") if isinstance(body.get("offer_id"), str) else "", None)
    if offer is None or offer["job_id"] != job_id or time.time() > offer["expires"]:
        raise PanelError(409, "not_waiting", "确认卡已过期，重新打开。")
    if body.get("acknowledge") != "standing":
        raise PanelError(409, "acknowledge_required", "要在确认卡上明确确认（acknowledge=standing）。")
    job = app.schedules.get(job_id)
    host, scope = comfy_destination()
    if job is None or job.get("handle") != offer["handle"] or host != offer["host"] or scope != "local":
        raise PanelError(409, "standing_not_allowed", "任务或 ComfyUI 地址在确认期间变了，没有打开。")
    out = _wrap(app.schedules.grant_standing, job_id, host)
    _audit("standing_grant", ["standing"])
    _record("schedule.standing_granted", job_id=job_id, host=host)
    return out


def standing_revoke(app, job_id: str) -> dict:
    out = _wrap(app.schedules.revoke_standing, job_id)
    _audit("standing_revoke", ["standing"])
    return out


def start_pending(app, pending_id: str) -> dict:
    if not enabled(app):
        raise PanelError(409, "scheduler_disabled", HUMAN["scheduler_disabled"])
    return _wrap(app.scheduler.start_pending, pending_id, lambda job, entry: launch(app, job, entry))


def dismiss_pending(app, pending_id: str) -> dict:
    return _wrap(app.scheduler.dismiss, pending_id)


def cancel(app, job_id: str) -> dict:
    run_id = app.scheduler.running.get(job_id)
    run = app.runs.get(run_id) if run_id else None
    if run is None:
        raise PanelError(409, "not_running", "这个任务现在没有在跑。")
    run._stop_code = "cancelled"
    run.stop()
    return {"ok": True, "run_id": run_id}


def preview(app, job_id: str) -> dict:
    job = app.schedules.raw(job_id)
    if job is None:
        raise PanelError(404, "not_found", "找不到这个定时任务。")
    return {"job_id": job_id, "body": job.get("body") or ""}


def run_now(app, job_id: str) -> dict:
    """面板上的「现在跑一次」：等同于到点，进待开始队列（仍要点「开始本次」）。"""
    if not enabled(app):
        raise PanelError(409, "scheduler_disabled", HUMAN["scheduler_disabled"])
    job = app.schedules.raw(job_id)
    if job is None:
        raise PanelError(404, "not_found", "找不到这个定时任务。")
    import datetime as _dt

    with app.scheduler._lock:
        return app.scheduler._fire(job, _dt.datetime.now().replace(microsecond=0), None, False)


# ---------- 模型工具 ----------

def _tool_error(name: str, code: str, message: str) -> dict:
    return {"ok": False, "tool": name, "error": {"code": code, "message": message}}


def _model_content(app, session_id: str, run_id, raw: dict, name: str):
    """模型给的本地内容只收本会话句柄。返回正文；出错返回错误结果。"""
    handle = raw.get("content_handle")
    record, code = app.sessions.resolve_content(session_id, handle, run_id=run_id, consume=False)
    if record is None:
        return None, _tool_error(name, code, "句柄无效（过期、不是本会话的，或不存在）。")
    if record.get("type") == "file" or record.get("kind") == "file":
        return None, _tool_error(name, "schedule_file_unsupported", "定时出图只收文字提示词，文件不收。")
    text = record.get("text")
    if not isinstance(text, str) or not text:
        return None, _tool_error(name, "handle_not_found", "句柄没有正文。")
    return text, None


def _model_fields(app, session_id: str, run_id, raw: dict, name: str, base: dict | None):
    if any(key in raw for key in ("standing", "preauthorize", "budget_override")):
        return None, _tool_error(name, "standing_by_model", HUMAN["standing_by_model"])
    fields = {key: raw[key] for key in ("name", "kind", "schedule", "images", "catch_up", "enabled", "budget")
              if key in raw}
    kind = fields.get("kind") or (base or {}).get("kind")
    texts = [fields.get("name") or ""]
    if kind == "image" and "content_handle" in raw:
        body, problem = _model_content(app, session_id, run_id, raw, name)
        if problem:
            return None, problem
        fields["body"] = body
    elif kind == "image" and base is None:
        return None, _tool_error(name, "invalid_parameter", "出图任务要给 content_handle（本会话的提示词句柄）。")
    if kind == "text" and "instruction" in raw:
        fields["body"] = raw.get("instruction")
        texts.append(raw.get("instruction") if isinstance(raw.get("instruction"), str) else "")
    try:
        _check(app, session_id, texts)
    except JobError as exc:
        return None, _tool_error(name, exc.code, exc.message)
    return fields, None


def call_tool(app, session_id: str, run_id, name: str, raw: dict) -> dict:
    if not enabled(app):
        return _tool_error(name, "scheduler_disabled", HUMAN["scheduler_disabled"])
    raw = raw if isinstance(raw, dict) else {}
    try:
        if name == "schedule_list":
            return {"ok": True, "tool": name, "items": app.schedules.list(audience="model")}
        if name == "schedule_create":
            fields, problem = _model_fields(app, session_id, run_id, raw, name, None)
            if problem:
                return problem
            current = values(app)
            job = app.schedules.create(fields, source="model", max_jobs=int(current.get("max_jobs") or 20),
                                       allow_plaintext=bool(current.get("allow_plaintext_image")))
            _audit("create", [job["kind"], "model"])
            return {"ok": True, "tool": name, "job": app.schedules.get(job["job_id"], audience="model"),
                    "note": "每次到点都要用户点「开始本次」；免确认出图只能由用户在面板里打开。"}
        job_id = raw.get("job_id")
        base = app.schedules.raw(job_id) if isinstance(job_id, str) else None
        if base is None:
            return _tool_error(name, "not_found", "找不到这个定时任务。")
        if name == "schedule_update":
            fields, problem = _model_fields(app, session_id, run_id, raw, name, base)
            if problem:
                return problem
            fields.pop("kind", None) if fields.get("kind") == base.get("kind") else None
            job = update(app, job_id, fields, source="model", session_id=session_id)
            return {"ok": True, "tool": name, "job": app.schedules.get(job["job_id"], audience="model"),
                    "note": "改了内容或时间表的任务要用户在面板里「重新授权」后才会再跑。"
                    if job.get("state") == "needs_reauth" else ""}
        if name == "schedule_delete":
            delete(app, job_id)
            return {"ok": True, "tool": name, "job_id": job_id, "status": "deleted"}
    except JobError as exc:
        return _tool_error(name, exc.code, exc.message)
    except PanelError as exc:
        return _tool_error(name, exc.code, exc.message)
    return _tool_error(name, "invalid_tool_call", "不认识的工具。")
