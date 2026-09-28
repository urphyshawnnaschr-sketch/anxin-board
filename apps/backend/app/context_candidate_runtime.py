"""Request-local Context preparation reuse for one bounded preparation call.

The authoritative builders remain unchanged. This module only memoizes already-validated,
non-provider results while an explicit runtime scope is active, so repeated consumers inside the
same preparation request do not replay the same frozen Git workspace over and over. No cache
survives the scope, crosses requests, or persists credential/provider data.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

from app.context_resolver import build_context_candidate_set as _build_context_candidate_set


_RUNTIME_CACHE: ContextVar[dict[str, dict[object, Any]] | None] = ContextVar(
    "context_preparation_runtime_cache",
    default=None,
)


@contextmanager
def candidate_materialization_scope() -> Iterator[None]:
    """Open one request-local preparation cache; nested scopes reuse the outer cache."""
    current = _RUNTIME_CACHE.get()
    if current is not None:
        yield
        return

    token = _RUNTIME_CACHE.set({})
    try:
        yield
    finally:
        _RUNTIME_CACHE.reset(token)


def request_cache_namespace(name: str) -> dict[object, Any] | None:
    """Return one request-local cache namespace, or None when no explicit scope is active."""
    cache = _RUNTIME_CACHE.get()
    if cache is None:
        return None
    return cache.setdefault(name, {})


def build_context_candidate_set(snapshot_id: int) -> dict[str, object]:
    """Delegate normally, or reuse one validated candidate inside an explicit local scope."""
    cache = request_cache_namespace("candidate")
    if cache is None:
        return _build_context_candidate_set(snapshot_id)

    cached = cache.get(snapshot_id)
    if cached is None:
        cached = deepcopy(_build_context_candidate_set(snapshot_id))
        cache[snapshot_id] = cached
    return deepcopy(cached)
