"""Exact closed-world registry for reviewed model provider adapters."""

from __future__ import annotations

from collections.abc import Iterable

from app.model_provider_contract import ModelProviderAdapter


class ModelProviderRegistryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModelProviderRegistry:
    """Resolve one exact reviewed adapter; never fallback to another provider."""

    def __init__(self, adapters: Iterable[ModelProviderAdapter] = ()) -> None:
        self._adapters: dict[str, ModelProviderAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ModelProviderAdapter) -> None:
        provider = getattr(adapter, "provider_id", None)
        if type(provider) is not str or not provider or provider != provider.strip():
            raise ModelProviderRegistryError(
                "MODEL_PROVIDER_REGISTRY_INVALID_PROVIDER",
                "Provider adapter 必须声明 exact non-empty provider_id。",
            )
        for method_name in (
            "get_capability",
            "estimate_request_utf8_bytes",
            "resolve_current_authority",
            "execute",
        ):
            if not callable(getattr(adapter, method_name, None)):
                raise ModelProviderRegistryError(
                    "MODEL_PROVIDER_REGISTRY_INVALID_ADAPTER",
                    "Provider adapter 未实现完整 narrow contract。",
                )
        if provider in self._adapters:
            raise ModelProviderRegistryError(
                "MODEL_PROVIDER_REGISTRY_DUPLICATE_PROVIDER",
                "同一 exact provider 只能注册一个 reviewed adapter。",
            )
        self._adapters[provider] = adapter

    def resolve(self, provider: object) -> ModelProviderAdapter:
        if type(provider) is not str or not provider or provider != provider.strip():
            raise ModelProviderRegistryError(
                "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER",
                "Provider identity 不存在 reviewed adapter。",
            )
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ModelProviderRegistryError(
                "MODEL_PROVIDER_REGISTRY_UNKNOWN_PROVIDER",
                "Provider identity 不存在 reviewed adapter。",
            )
        return adapter

    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
