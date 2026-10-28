"""按模型标识选择已接入的适配器。"""

from __future__ import annotations


class Registry:
    def __init__(self) -> None:
        self._adapters: dict[str, object] = {}
        self._models: dict[str, str] = {}
        self._default: str | None = None

    def register(self, name: str, adapter) -> None:
        """12.C（#59）：适配器必须声明出口方式。真联网的 audits_egress=True（走 services.gateway.egress.guard，
        网关运行时还会核对它确实开了 ticket）；不联网的测试替身 offline=True。两个都没有的不让注册。"""
        if not (getattr(adapter, "audits_egress", False) is True or getattr(adapter, "offline", False) is True):
            raise ValueError(f"适配器 {name} 没有挂出站中间件（audits_egress）也没有声明 offline，不能注册")
        self._adapters[name] = adapter

    def bind(self, model: str, adapter_name: str) -> None:
        self._models[model] = adapter_name

    def set_default(self, name: str) -> None:
        self._default = name

    def adapters(self) -> dict:
        return dict(self._adapters)

    def resolve(self, model: str):
        name = self._models.get(model) or self._default
        adapter = self._adapters.get(name) if name else None
        if adapter is None:
            from services.gateway.openai_chat import GatewayError

            raise GatewayError("provider_error", "没有接入这个模型。", retryable=False)
        return adapter

    def reset(self) -> None:
        self._adapters.clear()
        self._models.clear()
        self._default = None
