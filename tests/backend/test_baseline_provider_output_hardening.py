import hashlib

import pytest

from app.context_token_framing import COUNTING_POLICY_VERSION
from app.profile_reconciliation_batches import BatchError, plan_reconciliation_batches
from app.profile_reconciliation_provider import _normalize_provider_mappings


HEAD = "a" * 40
PLAN = {
    "schema_version": "project_profile_v2",
    "planned_modules": [
        {
            "client_id": "login",
            "name": "登录",
            "description": "",
            "requirements": ["用户登录"],
            "prd_refs": ["prd-a"],
            "exclusions": [],
        }
    ],
}
BUDGET = {
    "context_window_tokens": 40000,
    "max_output_tokens": 2000,
    "reserved_output_tokens": 2000,
    "safety_margin_tokens": 1000,
    "counting_policy_version": COUNTING_POLICY_VERSION,
}


def _batch():
    text = "def authenticate_user(): return True"
    evidence = {
        "evidence_id": "repo-code-login",
        "path": "app/auth.py",
        "content": text,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "exact_head": HEAD,
    }
    return plan_reconciliation_batches(
        PLAN,
        [evidence],
        exact_head=HEAD,
        plan_profile_id=42,
        budget_record=BUDGET,
    )[0]


def test_cosmetic_provider_variance_is_canonicalized_without_changing_evidence_binding():
    batch = _batch()
    result = {
        "mappings": [
            {
                "planned_module_id": " login ",
                "status": " Implemented ",
                "evidence_ids": [" repo-code-login "],
                "rationale": " matched authenticate_user behavior " + ("x" * 2100),
                "confidence": 0.92,
            }
        ],
        "summary": "ignored cosmetic provider metadata",
    }

    normalized = _normalize_provider_mappings(batch, result)

    assert normalized[0]["planned_module_id"] == "login"
    assert normalized[0]["status"] == "implemented"
    assert normalized[0]["evidence_ids"] == ["repo-code-login"]
    assert 1 <= len(normalized[0]["rationale"]) <= 2000
    assert set(normalized[0]) == {
        "planned_module_id",
        "status",
        "evidence_ids",
        "rationale",
    }


def test_hardening_still_rejects_invented_or_cross_batch_evidence():
    batch = _batch()

    with pytest.raises(BatchError, match="CROSS_BATCH_EVIDENCE"):
        _normalize_provider_mappings(
            batch,
            {
                "mappings": [
                    {
                        "planned_module_id": "login",
                        "status": "partial",
                        "evidence_ids": ["repo-code-invented"],
                        "rationale": "claims support from evidence not present in this request",
                    }
                ]
            },
        )


def test_hardening_still_rejects_missing_module_assessment():
    batch = _batch()

    with pytest.raises(BatchError, match="INVALID_BATCH_RESULT"):
        _normalize_provider_mappings(batch, {"mappings": []})


def test_hardening_still_rejects_positive_status_without_evidence():
    batch = _batch()

    with pytest.raises(BatchError, match="IMPLEMENTATION_WITHOUT_EVIDENCE"):
        _normalize_provider_mappings(
            batch,
            {
                "mappings": [
                    {
                        "planned_module_id": "login",
                        "status": "implemented",
                        "evidence_ids": [],
                        "rationale": "unsupported completion claim",
                    }
                ]
            },
        )
