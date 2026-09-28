"""Request-local reuse of already-redacted Context results.

Only redacted results are cached. Raw historical bodies remain owned by context_redaction and are
never stored in this runtime cache. Outside an explicit preparation scope this wrapper delegates
straight to the authoritative transform with no memoization.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json

from app.context_candidate_runtime import request_cache_namespace
from app.context_redaction import build_context_redaction_result as _build_context_redaction_result


def _budget_identity(budget_record: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(budget_record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_context_redaction_result(
    *, model_call_id: int, budget_record: Mapping[str, object], target: str
) -> dict[str, object]:
    """Reuse one deterministic redacted result within the active preparation request only."""
    cache = request_cache_namespace("redaction")
    if cache is None:
        return _build_context_redaction_result(
            model_call_id=model_call_id,
            budget_record=budget_record,
            target=target,
        )

    key = (model_call_id, target, _budget_identity(budget_record))
    cached = cache.get(key)
    if cached is None:
        cached = deepcopy(
            _build_context_redaction_result(
                model_call_id=model_call_id,
                budget_record=budget_record,
                target=target,
            )
        )
        cache[key] = cached
    return deepcopy(cached)
