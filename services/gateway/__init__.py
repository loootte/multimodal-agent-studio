"""供应商适配器。按模型标识选择。统一请求走 stream。

12.C（#59）统一出口中间件：stream 在交给任何适配器之前，先对统一请求跑一遍本地正文检查（规范化、分片、短正文指纹、
编码窗），命中就记 egress.blocked 并抛错，适配器根本拿不到这次请求；真联网的适配器（audits_egress）还要在产出第一条事件前
通过 egress.guard 开过 ticket，否则按 egress_policy_denied 拦下。不联网的测试替身声明 offline。
"""

import time

from packages.contracts.events import EVENT_MESSAGE_COMPLETED, EVENT_RUN_STARTED
from packages.contracts.request import normalize
from services.gateway.openai_chat import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    GatewayError,
    OpenAIAdapter,
    reject_unsupported,
    stream_chat,
)
from packages.contracts.events import HUMAN
from services import telemetry
from services.gateway import egress
from services.gateway.registry import Registry

registry = Registry()


def install_defaults() -> None:
    registry.reset()
    registry.register("openai", OpenAIAdapter())
    registry.set_default("openai")


install_defaults()


def describe(model: str) -> dict:
    return registry.resolve(model).capabilities(model)


def stream(
    request,
    *,
    api_key: str,
    timeout_sec: float,
    cancel,
    artifacts=None,
    local_bodies=None,
    base_url=None,
):
    try:
        req = normalize(request)
    except ValueError as exc:
        raise GatewayError("provider_error", str(exc) or "请求不合法。", retryable=False) from exc
    adapter = registry.resolve(req["model"])
    caps = adapter.capabilities(req["model"])
    reject_unsupported(req, caps)
    yield {
        "type": EVENT_RUN_STARTED,
        "model": req["model"],
        "ignored_params": list(req.get("ignored_params") or []),
    }
    completed = False
    started = time.monotonic()
    telemetry.take_usage()
    outcome = {"ok": False, "error_code": "aborted"}
    tool = None
    chars = 0
    audited = getattr(adapter, "offline", False) is not True
    upstream = base_url or getattr(adapter, "destination", None) or DEFAULT_BASE_URL
    try:
        reason = egress.inspect(req, local_bodies or [], exempt=("tools",)) if local_bodies else None
        if reason:
            egress.blocked("chat_completion", upstream, reason, local_derived=True)
            egress.security_event(reason, "chat_completion")
            raise GatewayError(reason, HUMAN[reason])
        tickets = egress.ticket_count()
        checked = False
        for event in adapter.stream(
            req,
            api_key=api_key,
            timeout_sec=timeout_sec,
            cancel=cancel,
            artifacts=artifacts or {},
            local_bodies=local_bodies or [],
            base_url=base_url,
        ):
            if event.get("type") == EVENT_RUN_STARTED:
                continue
            if audited and not checked:
                checked = True
                if egress.ticket_count() == tickets:  # 适配器没走出口中间件：不把它的输出交出去
                    egress.blocked("chat_completion", upstream, "egress_policy_denied")
                    egress.security_event("egress_policy_denied", "chat_completion")
                    raise GatewayError("egress_policy_denied", HUMAN["egress_policy_denied"])
            if event.get("type") == EVENT_MESSAGE_COMPLETED:
                completed = True
            if event.get("type") == "message.delta":
                chars += len(event.get("text") or "")
            if event.get("type") == "tool.requested":
                tool = event.get("name")
            yield event
        outcome = {"ok": True}
    except GatewayError as exc:
        outcome = {"ok": False, "error_code": exc.code}
        raise
    except GeneratorExit:
        outcome = {"ok": True, "closed_early": True}
        raise
    finally:
        telemetry.record(
            "provider",
            model=req["model"],
            ms=int((time.monotonic() - started) * 1000),
            usage=telemetry.take_usage(),
            tokens_estimated=max(1, chars // 2) if chars else None,
            tool_requested=tool,
            **outcome,
        )
    if not completed:
        yield {"type": EVENT_MESSAGE_COMPLETED}
