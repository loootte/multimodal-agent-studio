"""15.C 文字可控披露（打开盲盒）：本机校验 / 抽取 / 短摘要，不引入本地小模型栈。

确认与审计由 chat_run 负责；这里只做句柄消费后的本机处理。结果绝不能带回完整正文：
短于预览上限的正文只回统计，避免整段正文进入下一轮 chat_completion。
邮箱 / 电话做廉价占位符替换；完整可逆回复映射列为缺口，见 docs/local-resources.md。
"""

from __future__ import annotations

import re
import sys
import uuid

MODES = ("validate", "extract", "summarize")
PREVIEW_MAX = 200
# 正文必须严格长于预览上限，才允许把截断预览放进工具结果（否则整段正文会出门）。
MIN_BODY_FOR_PREVIEW = PREVIEW_MAX + 1
TOOL = "disclose_local_text"
PURPOSE = "disclose"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\- ]{7,14}\d)(?!\d)")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


def _audit(kind: str, **fields) -> None:
    try:
        from services import telemetry

        payload = {"trace_id": fields.pop("trace_id", None) or ("sec_" + uuid.uuid4().hex)}
        for key, value in fields.items():
            if value is None:
                continue
            payload[key] = value
        telemetry.record(kind, **payload)
    except Exception as exc:  # noqa: BLE001 审计失败不改变主结果
        print(f"警告：disclose 审计失败 {type(exc).__name__}", file=sys.stderr)


def pseudonymize(text: str) -> tuple[str, dict[str, str]]:
    """廉价邮箱 / 电话占位符。映射只留在本机（可选写进 disclose 句柄元数据），不进工具结果。"""
    mapping: dict[str, str] = {}

    def email(match: re.Match) -> str:
        key = f"EMAIL_{len(mapping) + 1}"
        mapping[key] = match.group(0)
        return f"[{key}]"

    def phone(match: re.Match) -> str:
        key = f"PHONE_{len(mapping) + 1}"
        mapping[key] = match.group(0)
        return f"[{key}]"

    out = _EMAIL.sub(email, text)
    out = _PHONE.sub(phone, out)
    return out, mapping


def _stats(text: str) -> dict:
    lines = text.splitlines() or ([""] if text == "" else [])
    words = [w for w in re.split(r"\s+", text.strip()) if w] if text.strip() else []
    return {
        "char_count": len(text),
        "line_count": len(lines),
        "word_count": len(words),
        "empty": not bool(text.strip()),
        "has_cjk": bool(_CJK.search(text)),
        "has_latin": bool(_LATIN.search(text)),
    }


def _tags(text: str, mapping: dict[str, str]) -> list[str]:
    tags = []
    if any(k.startswith("EMAIL_") for k in mapping):
        tags.append("has_email")
    if any(k.startswith("PHONE_") for k in mapping):
        tags.append("has_phone")
    if _CJK.search(text):
        tags.append("zh")
    if _LATIN.search(text):
        tags.append("latin")
    if len(text) >= 2000:
        tags.append("long")
    return tags


def _public_placeholders(mapping: dict[str, str]) -> list[str]:
    return [f"[{key}]" for key in mapping]


def run(app, session_id: str, run_id: str | None, arguments: dict) -> dict:
    """执行打开盲盒。调用方须已通过用户确认。"""
    raw = arguments if isinstance(arguments, dict) else {}
    handle = raw.get("content_handle") if isinstance(raw.get("content_handle"), str) else ""
    mode = raw.get("mode") if isinstance(raw.get("mode"), str) else ""
    if mode not in MODES:
        return {
            "ok": False,
            "tool": TOOL,
            "error": {"code": "invalid_parameter", "message": "mode 只能是 validate、extract 或 summarize。"},
        }
    if not handle:
        return {
            "ok": False,
            "tool": TOOL,
            "error": {"code": "handle_not_found", "message": "请提供本会话的文字句柄。"},
        }
    record, code = app.sessions.resolve_content(
        session_id, handle, tool=TOOL, run_id=run_id, consume=False,
    )
    if record is None:
        _audit(
            "security.disclose_denied",
            session_id=session_id,
            run_id=run_id,
            tool=TOOL,
            mode=mode,
            handle_id_prefix=handle[:6],
            error_code=code,
            reason="resolve",
        )
        return {
            "ok": False,
            "tool": TOOL,
            "error": {"code": code, "message": {
                "handle_not_found": "句柄不存在或不属于本会话。",
                "handle_expired": "句柄已过期或已撤销。",
                "handle_forbidden": "这个句柄的用途不允许打开盲盒。",
            }.get(code, "句柄不可用。")},
            "content_handle": handle,
            "mode": mode,
        }
    if record.get("type") == "file" or record.get("kind") == "file":
        return {
            "ok": False,
            "tool": TOOL,
            "error": {"code": "invalid_parameter", "message": "文件句柄不能打开文字盲盒；请用文字句柄。"},
            "content_handle": handle,
            "mode": mode,
        }
    text = record.get("text")
    if not isinstance(text, str):
        return {
            "ok": False,
            "tool": TOOL,
            "error": {"code": "handle_not_found", "message": "句柄没有可披露的文字。"},
            "content_handle": handle,
            "mode": mode,
        }
    purpose = (record.get("scope") or {}).get("purpose") or record.get("purpose") or "keep"
    stats = _stats(text)
    redacted, mapping = pseudonymize(text)
    base = {
        "ok": True,
        "tool": TOOL,
        "mode": mode,
        "content_handle": handle,
        "source_purpose": purpose,
        "kind": "text",
        **{k: stats[k] for k in ("char_count", "line_count", "word_count", "empty")},
    }
    if mode == "validate":
        base.update({"has_cjk": stats["has_cjk"], "has_latin": stats["has_latin"]})
        _audit(
            "security.disclose_opened",
            session_id=session_id,
            run_id=run_id,
            tool=TOOL,
            mode=mode,
            handle_id_prefix=handle[:6],
            char_count=stats["char_count"],
        )
        return base

    if mode == "extract":
        base["tags"] = _tags(text, mapping)
        base["placeholders"] = _public_placeholders(mapping)
        base["email_count"] = sum(1 for k in mapping if k.startswith("EMAIL_"))
        base["phone_count"] = sum(1 for k in mapping if k.startswith("PHONE_"))
        _audit(
            "security.disclose_opened",
            session_id=session_id,
            run_id=run_id,
            tool=TOOL,
            mode=mode,
            handle_id_prefix=handle[:6],
            char_count=stats["char_count"],
            email_count=base["email_count"],
            phone_count=base["phone_count"],
        )
        return base

    # summarize
    if len(text) < MIN_BODY_FOR_PREVIEW:
        base["preview"] = None
        base["preview_omitted"] = "body_too_short_for_safe_preview"
        base["note"] = "正文过短：若输出预览会等于整段正文，已省略预览，只回统计。"
        _audit(
            "security.disclose_opened",
            session_id=session_id,
            run_id=run_id,
            tool=TOOL,
            mode=mode,
            handle_id_prefix=handle[:6],
            char_count=stats["char_count"],
            preview=False,
        )
        return base

    preview = redacted.strip().replace("\r\n", "\n")
    if len(preview) > PREVIEW_MAX:
        preview = preview[:PREVIEW_MAX] + "…"
    # 防御：截断后仍不得等于或包含完整原文。
    if preview == text or text in preview:
        base["preview"] = None
        base["preview_omitted"] = "preview_would_leak_body"
        base["note"] = "无法生成不泄露全文的预览，已省略。"
        _audit(
            "security.disclose_opened",
            session_id=session_id,
            run_id=run_id,
            tool=TOOL,
            mode=mode,
            handle_id_prefix=handle[:6],
            char_count=stats["char_count"],
            preview=False,
        )
        return base

    issued = app.sessions.add_content(session_id, preview, purpose=PURPOSE)
    base["preview"] = preview
    base["disclose_handle"] = issued["handle"]
    base["preview_chars"] = len(preview)
    _audit(
        "security.disclose_opened",
        session_id=session_id,
        run_id=run_id,
        tool=TOOL,
        mode=mode,
        handle_id_prefix=handle[:6],
        disclose_handle_prefix=issued["handle"][:6],
        char_count=stats["char_count"],
        preview_chars=len(preview),
        preview=True,
    )
    return base


def audit_user_decision(session_id: str, run_id: str | None, arguments: dict, *, allowed: bool) -> None:
    """确认卡同意 / 拒绝。拒绝时正文未读；同意只记 confirmed，真正打开记 disclose_opened。"""
    raw = arguments if isinstance(arguments, dict) else {}
    handle = raw.get("content_handle") if isinstance(raw.get("content_handle"), str) else ""
    mode = raw.get("mode") if isinstance(raw.get("mode"), str) else ""
    _audit(
        "security.disclose_confirmed" if allowed else "security.disclose_denied",
        session_id=session_id,
        run_id=run_id,
        tool=TOOL,
        mode=mode if mode in MODES else None,
        handle_id_prefix=(handle[:6] if handle else None),
        allowed=allowed,
        reason="confirm",
    )
