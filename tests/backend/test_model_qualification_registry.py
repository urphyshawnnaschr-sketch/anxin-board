"""Page07 3B3A read-only model qualification registry core tests."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import model_qualification_registry as registry  # noqa: E402
from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION  # noqa: E402


REGENERATE_IDENTITY = {
    "task_type": "daily_report_regenerate",
    "output_schema_version": "daily-report-regenerate/1.0",
    "prompt_version": "page07-regenerate-prompt/1.0",
    "rule_version": "page07-regenerate-rules/2.0",
    "sample_pack_version": "page07-regenerate-qualification-pack/2.0",
}
CONTRADICTION_IDENTITY = {
    "task_type": "report_contradiction_check",
    "output_schema_version": "report-contradiction-check/1.0",
    "prompt_version": "page07-contradiction-prompt/1.0",
    "rule_version": "page07-contradiction-rules/1.0",
    "sample_pack_version": "page07-contradiction-qualification-pack/1.0",
}


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _lookup(task_identity: dict[str, str] | None = None, **overrides) -> dict[str, object]:
    identity = REGENERATE_IDENTITY if task_identity is None else task_identity
    value: dict[str, object] = {
        "provider": "TEST-ONLY-provider",
        "model_id": "TEST-ONLY-model",
        "model_version": "TEST-ONLY-model-version",
        "task_type": identity["task_type"],
        "ai_contract_schema_version": AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": identity["output_schema_version"],
        "prompt_version": identity["prompt_version"],
        "prompt_contract_hash": _sha("TEST-ONLY-prompt-contract"),
        "rule_version": identity["rule_version"],
        "sample_pack_version": identity["sample_pack_version"],
        "sampling_parameters_hash": _sha("TEST-ONLY-sampling-parameters"),
    }
    value.update(overrides)
    return value


def _synthetic_record(task_identity: dict[str, str] | None = None, **overrides) -> dict[str, object]:
    value = _lookup(task_identity)
    value.update(
        {
            "sample_manifest_hash": _sha("TEST-ONLY-sample-manifest"),
            "qualification_harness_commit": hashlib.sha1(b"TEST-ONLY-harness-commit").hexdigest(),
            "evidence_manifest_hash": _sha("TEST-ONLY-evidence-manifest"),
            "review_ref": "TEST-ONLY-INDEPENDENT-EVIDENCE-REVIEW-PASS",
            "qualification_status": "qualified",
        }
    )
    value.update(overrides)
    payload = {field: value[field] for field in registry._HASH_FIELDS}
    value["record_hash"] = registry._stable_hash(payload)
    return value


def _assert_code(expected: str, callable_):
    with pytest.raises(registry.QualificationRegistryError) as caught:
        callable_()
    assert caught.value.code == expected
    return caught


def test_t01_shipping_registry_is_empty_and_both_real_task_identities_fail_not_admitted():
    assert registry._SHIPPING_ADMITTED_RECORDS == ()
    assert tuple(registry.APPROVED_TASK_IDENTITIES) == (
        "daily_report_regenerate",
        "report_contradiction_check",
    )
    assert dict(registry.APPROVED_TASK_IDENTITIES["daily_report_regenerate"]) == REGENERATE_IDENTITY
    assert dict(registry.APPROVED_TASK_IDENTITIES["report_contradiction_check"]) == CONTRADICTION_IDENTITY

    _assert_code("QUALIFICATION_NOT_ADMITTED", lambda: registry.lookup_qualified_record(_lookup()))
    _assert_code(
        "QUALIFICATION_NOT_ADMITTED",
        lambda: registry.lookup_qualified_record(_lookup(CONTRADICTION_IDENTITY)),
    )


def test_t02_synthetic_test_only_record_exactly_recloses_and_matches_once():
    record = _synthetic_record()
    result = registry._lookup_exact_record_from_registry(_lookup(), (record,))

    assert dict(result) == record
    assert result["qualification_status"] == "qualified"
    assert result["record_hash"] == registry._stable_hash(
        {field: result[field] for field in registry._HASH_FIELDS}
    )
    with pytest.raises(TypeError):
        result["qualification_status"] = "not_qualified"


def test_t03_zero_multiple_and_non_matching_records_fail_closed_without_fallback():
    query = _lookup()
    record = _synthetic_record()
    _assert_code(
        "QUALIFICATION_NOT_ADMITTED",
        lambda: registry._lookup_exact_record_from_registry(query, ()),
    )
    _assert_code(
        "QUALIFICATION_AMBIGUOUS",
        lambda: registry._lookup_exact_record_from_registry(query, (record, dict(record))),
    )

    contradiction_record = _synthetic_record(CONTRADICTION_IDENTITY)
    _assert_code(
        "QUALIFICATION_NOT_ADMITTED",
        lambda: registry._lookup_exact_record_from_registry(query, (contradiction_record,)),
    )

    drifted = dict(record)
    drifted["model_version"] = "TEST-ONLY-newer-model-version"
    drifted["record_hash"] = registry._stable_hash(
        {field: drifted[field] for field in registry._HASH_FIELDS}
    )
    _assert_code(
        "QUALIFICATION_NOT_ADMITTED",
        lambda: registry._lookup_exact_record_from_registry(query, (drifted,)),
    )


def test_t04_lookup_is_closed_world_and_caller_cannot_inject_qualified_or_fuzzy_latest():
    for invalid in (None, [], "x", 7):
        _assert_code("QUALIFICATION_LOOKUP_INVALID", lambda invalid=invalid: registry.lookup_qualified_record(invalid))

    missing = _lookup()
    missing.pop("prompt_version")
    _assert_code("QUALIFICATION_LOOKUP_INVALID", lambda: registry.lookup_qualified_record(missing))

    for field, injected in (
        ("qualified", True),
        ("qualification_status", "qualified"),
        ("latest", True),
        ("fallback", "daily_report_generate"),
    ):
        extra = _lookup()
        extra[field] = injected
        _assert_code("QUALIFICATION_LOOKUP_INVALID", lambda extra=extra: registry.lookup_qualified_record(extra))

    for field, drift in (
        ("task_type", "daily_report_generate"),
        ("output_schema_version", "daily-report-regenerate/latest"),
        ("prompt_version", "page07-regenerate-prompt/1.1"),
        ("rule_version", "page07-regenerate-rules/1.1"),
        ("sample_pack_version", "page07-regenerate-qualification-pack/1.1"),
        ("ai_contract_schema_version", "ai-agent-contract/latest"),
    ):
        query = _lookup(**{field: drift})
        _assert_code("QUALIFICATION_IDENTITY_MISMATCH", lambda query=query: registry.lookup_qualified_record(query))

    upper_provider = _lookup(provider="TEST-ONLY-PROVIDER")
    lower_record = _synthetic_record()
    _assert_code(
        "QUALIFICATION_NOT_ADMITTED",
        lambda: registry._lookup_exact_record_from_registry(upper_provider, (lower_record,)),
    )


def test_t05_malformed_or_mismatched_registry_record_invalidates_registry_instead_of_being_skipped():
    query = _lookup()
    good = _synthetic_record()

    malformed = dict(good)
    malformed["unexpected"] = "TEST-ONLY"
    _assert_code(
        "QUALIFICATION_REGISTRY_INVALID",
        lambda: registry._lookup_exact_record_from_registry(query, (good, malformed)),
    )

    bad_hash = dict(good)
    bad_hash["record_hash"] = "0" * 64
    _assert_code(
        "QUALIFICATION_REGISTRY_INVALID",
        lambda: registry._lookup_exact_record_from_registry(query, (bad_hash,)),
    )

    bad_status = _synthetic_record(qualification_status="not_qualified")
    _assert_code(
        "QUALIFICATION_REGISTRY_INVALID",
        lambda: registry._lookup_exact_record_from_registry(query, (bad_status,)),
    )

    mismatched_identity = dict(good)
    mismatched_identity["prompt_version"] = "page07-regenerate-prompt/9.9"
    mismatched_identity["record_hash"] = registry._stable_hash(
        {field: mismatched_identity[field] for field in registry._HASH_FIELDS}
    )
    _assert_code(
        "QUALIFICATION_IDENTITY_MISMATCH",
        lambda: registry._lookup_exact_record_from_registry(query, (mismatched_identity,)),
    )


def test_t06_string_and_hash_validation_fail_closed():
    for field in registry._LOOKUP_FIELDS:
        bad = _lookup()
        bad[field] = ""
        _assert_code("QUALIFICATION_LOOKUP_INVALID", lambda bad=bad: registry.lookup_qualified_record(bad))

    for field in ("prompt_contract_hash", "sampling_parameters_hash"):
        for invalid_hash in ("abcd", "A" * 64, "g" * 64):
            bad = _lookup(**{field: invalid_hash})
            _assert_code("QUALIFICATION_LOOKUP_INVALID", lambda bad=bad: registry.lookup_qualified_record(bad))

    bad_utf8 = _lookup(provider="\ud800")
    _assert_code("QUALIFICATION_LOOKUP_INVALID", lambda: registry.lookup_qualified_record(bad_utf8))


def test_t07_record_hash_covers_every_admission_field_and_is_canonical():
    record = _synthetic_record()
    payload = {field: record[field] for field in registry._HASH_FIELDS}
    baseline = registry._stable_hash(payload)
    reversed_payload = dict(reversed(list(payload.items())))
    assert registry._stable_hash(reversed_payload) == baseline == record["record_hash"]

    for field in registry._HASH_FIELDS:
        changed = dict(payload)
        changed[field] = f"{changed[field]}-changed"
        assert registry._stable_hash(changed) != baseline, field


def test_t08_module_has_no_runtime_writer_or_io_provider_dependency():
    module_path = Path(registry.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    forbidden_import_prefixes = (
        "sqlite3",
        "socket",
        "subprocess",
        "requests",
        "httpx",
        "urllib",
        "smtplib",
        "app.db",
        "app.storage",
        "app.secret_store",
        "app.windows_credential_store",
        "app.deepseek_transport",
        "app.model_gateway",
        "app.deepseek_current_authority",
    )
    assert not any(
        imported == prefix or imported.startswith(f"{prefix}.")
        for imported in imports
        for prefix in forbidden_import_prefixes
    )

    public_names = set(registry.__all__)
    assert public_names == {
        "APPROVED_TASK_IDENTITIES",
        "QualificationRegistryError",
        "lookup_qualified_record",
    }
    assert not any(
        token in name.lower()
        for name in public_names
        for token in ("add", "admit", "create", "delete", "register", "set", "update", "write")
    )


def test_t09_test_fixtures_are_unmistakably_synthetic_and_never_shipping_records():
    record = _synthetic_record(CONTRADICTION_IDENTITY)
    assert record["provider"].startswith("TEST-ONLY-")
    assert record["model_id"].startswith("TEST-ONLY-")
    assert record["model_version"].startswith("TEST-ONLY-")
    assert record["qualification_harness_commit"] == hashlib.sha1(b"TEST-ONLY-harness-commit").hexdigest()
    assert record["review_ref"].startswith("TEST-ONLY-")
    assert registry._SHIPPING_ADMITTED_RECORDS == ()
