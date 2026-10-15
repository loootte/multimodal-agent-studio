"""供应商适配器。按模型标识选择。统一请求走 stream。"""

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
        if event.get("type") == EVENT_MESSAGE_COMPLETED:
            completed = True
        yield event
    if not completed:
        yield {"type": EVENT_MESSAGE_COMPLETED}
