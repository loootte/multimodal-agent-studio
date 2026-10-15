"""按模型标识选择已接入的适配器。"""

from __future__ import annotations


class Registry:
    def __init__(self) -> None:
        self._adapters: dict[str, object] = {}
        self._models: dict[str, str] = {}
        self._default: str | None = None

    def register(self, name: str, adapter) -> None:
        self._adapters[name] = adapter

    def bind(self, model: str, adapter_name: str) -> None:
        self._models[model] = adapter_name

    def set_default(self, name: str) -> None:
        self._default = name

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
