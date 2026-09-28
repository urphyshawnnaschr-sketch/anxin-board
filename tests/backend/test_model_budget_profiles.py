"""Model Budget Profile V1: deterministic budget assertion, fail-closed validation, and side-effect tests."""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import smtplib
import socket
import sqlite3
import subprocess
import sys

import httpx
import pytest
from fastapi import HTTPException

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from app import model_budget_profiles, model_call_ledger  # noqa: E402
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_model_call_ledger as ledger_tests  # noqa: E402


STRING_FIELDS = (
    "provider",
    "model_id",
    "model_version",
    "budget_policy_version",
    "tokenizer_family",
    "tokenizer_version",
    "counting_policy_version",
)
INT_FIELDS = (
    "context_window_tokens",
    "max_output_tokens",
    "reserved_output_tokens",
    "safety_margin_tokens",
)


@pytest.fixture()
def budget_state(tmp_path, monkeypatch):
    return candidate_tests._make_state(tmp_path, monkeypatch)


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _assert_code(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected
    return caught


def _budget_record(**overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "budget_policy_version": "budget/1.0",
        "context_window_tokens": 16_384,
        "max_output_tokens": 4_096,
        "reserved_output_tokens": 3_072,
        "safety_margin_tokens": 513,
        "tokenizer_family": "tokenizer-a",
        "tokenizer_version": "2026-08",
        "counting_policy_version": "count/1.0",
    }
    value.update(overrides)
    return value


def _prepare(state: dict, *, call_prepare_key: str = "budget-prepare-1", **overrides):
    return ledger_tests._prepare(
        state,
        call_prepare_key=call_prepare_key,
        **overrides,
    )


def _db_signature(state: dict) -> tuple[str, list[tuple]]:
    with sqlite3.connect(state["db_path"]) as conn:
        dump = "\n".join(conn.iterdump())
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') ORDER BY type, name"
        ).fetchall()
    return dump, schema


def _canonical_hash(value: dict[str, object]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_t01_real_happy_path_uses_persisted_ledger_and_builds_deterministic_profile(budget_state):
    prepared = _prepare(budget_state)
    before = _db_signature(budget_state)
    record = _budget_record()

    result = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"],
        budget_record=record,
    )

    assert result == {
        "schema_version": "model_budget_profile_v1",
        "model_call_id": prepared["model_call_id"],
        "call_identity_hash": prepared["call_identity_hash"],
        "task_type": prepared["task_type"],
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "budget_policy_version": "budget/1.0",
        "context_window_tokens": 16_384,
        "max_output_tokens": 4_096,
        "reserved_output_tokens": 3_072,
        "safety_margin_tokens": 513,
        "max_input_tokens": 12_799,
        "tokenizer_family": "tokenizer-a",
        "tokenizer_version": "2026-08",
        "counting_policy_version": "count/1.0",
        "budget_authority_state": "assertion_only",
        "token_count_state": "not_counted",
        "model_send_state": "not_admitted",
        "budget_profile_hash": result["budget_profile_hash"],
    }
    payload = {field: result[field] for field in model_budget_profiles._HASH_FIELDS}
    assert result["budget_profile_hash"] == _canonical_hash(payload)
    assert len(result["budget_profile_hash"]) == 64
    int(result["budget_profile_hash"], 16)
    assert _db_signature(budget_state) == before


def test_t02_exact_repeat_is_deep_equal_same_hash_and_db_unchanged(budget_state):
    prepared = _prepare(budget_state)
    record = _budget_record()
    before = _db_signature(budget_state)

    first = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    )
    second = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    )

    assert second == first
    assert second["budget_profile_hash"] == first["budget_profile_hash"]
    assert _db_signature(budget_state) == before


def test_t03_valid_non_blank_model_identity_drift_is_stable_mismatch_with_zero_write(budget_state):
    prepared = _prepare(budget_state)
    before = _db_signature(budget_state)
    drift_values = {
        "provider": "provider-b",
        "model_id": "model-b",
        "model_version": "2026-09",
    }

    for field, drift in drift_values.items():
        record = _budget_record(**{field: drift})
        _assert_code(
            "MODEL_BUDGET_MODEL_MISMATCH",
            lambda record=record: model_budget_profiles.build_model_budget_profile(
                model_call_id=prepared["model_call_id"], budget_record=record
            ),
        )
        assert _db_signature(budget_state) == before


def test_t04_closed_world_input_adversarial_and_error_precedence(budget_state):
    prepared = _prepare(budget_state)
    model_call_id = prepared["model_call_id"]

    for invalid_id in (0, -1, True, 1.0, "1"):
        _assert_code(
            "MODEL_CALL_INPUT_INVALID",
            lambda invalid_id=invalid_id: model_budget_profiles.build_model_budget_profile(
                model_call_id=invalid_id, budget_record=_budget_record()
            ),
        )

    for non_mapping in (None, [], "not-a-mapping", 7):
        _assert_code(
            "MODEL_BUDGET_INPUT_INVALID",
            lambda non_mapping=non_mapping: model_budget_profiles.build_model_budget_profile(
                model_call_id=model_call_id, budget_record=non_mapping
            ),
        )

    for field in model_budget_profiles._BUDGET_FIELDS:
        missing = _budget_record()
        missing.pop(field)
        _assert_code(
            "MODEL_BUDGET_INPUT_INVALID",
            lambda missing=missing: model_budget_profiles.build_model_budget_profile(
                model_call_id=model_call_id, budget_record=missing
            ),
        )

    extra = _budget_record()
    extra["unexpected"] = "x"
    _assert_code(
        "MODEL_BUDGET_INPUT_INVALID",
        lambda: model_budget_profiles.build_model_budget_profile(
            model_call_id=model_call_id, budget_record=extra
        ),
    )

    for field in STRING_FIELDS:
        wrong_type = _budget_record(**{field: 123})
        _assert_code(
            "MODEL_BUDGET_INPUT_INVALID",
            lambda wrong_type=wrong_type: model_budget_profiles.build_model_budget_profile(
                model_call_id=model_call_id, budget_record=wrong_type
            ),
        )
        for blank in ("", "   ", "\t", "\n"):
            invalid = _budget_record(**{field: blank})
            _assert_code(
                "MODEL_BUDGET_INPUT_INVALID",
                lambda invalid=invalid: model_budget_profiles.build_model_budget_profile(
                    model_call_id=model_call_id, budget_record=invalid
                ),
            )

    invalid_utf8 = _budget_record(tokenizer_version="\ud800")
    _assert_code(
        "MODEL_BUDGET_INPUT_INVALID",
        lambda: model_budget_profiles.build_model_budget_profile(
            model_call_id=model_call_id, budget_record=invalid_utf8
        ),
    )

    for field in INT_FIELDS:
        wrong_type = _budget_record(**{field: "1"})
        _assert_code(
            "MODEL_BUDGET_INPUT_INVALID",
            lambda wrong_type=wrong_type: model_budget_profiles.build_model_budget_profile(
                model_call_id=model_call_id, budget_record=wrong_type
            ),
        )
        bool_value = _budget_record(**{field: True})
        _assert_code(
            "MODEL_BUDGET_INPUT_INVALID",
            lambda bool_value=bool_value: model_budget_profiles.build_model_budget_profile(
                model_call_id=model_call_id, budget_record=bool_value
            ),
        )


def test_t05_limit_boundary_table_and_valid_edges(budget_state):
    prepared = _prepare(budget_state)
    model_call_id = prepared["model_call_id"]
    invalid_records = (
        _budget_record(context_window_tokens=0),
        _budget_record(context_window_tokens=100_000_001),
        _budget_record(max_output_tokens=0),
        _budget_record(context_window_tokens=10, max_output_tokens=11, reserved_output_tokens=1, safety_margin_tokens=0),
        _budget_record(reserved_output_tokens=0),
        _budget_record(max_output_tokens=10, reserved_output_tokens=11),
        _budget_record(safety_margin_tokens=-1),
        _budget_record(context_window_tokens=10, max_output_tokens=8, reserved_output_tokens=1, safety_margin_tokens=10),
        _budget_record(context_window_tokens=10, max_output_tokens=8, reserved_output_tokens=8, safety_margin_tokens=2),
        _budget_record(context_window_tokens=10, max_output_tokens=8, reserved_output_tokens=8, safety_margin_tokens=3),
    )
    for record in invalid_records:
        _assert_code(
            "MODEL_BUDGET_LIMIT_INVALID",
            lambda record=record: model_budget_profiles.build_model_budget_profile(
                model_call_id=model_call_id, budget_record=record
            ),
        )

    valid_edges = (
        (_budget_record(context_window_tokens=2, max_output_tokens=1, reserved_output_tokens=1, safety_margin_tokens=0), 1),
        (_budget_record(context_window_tokens=100_000_000, max_output_tokens=100_000_000, reserved_output_tokens=1, safety_margin_tokens=0), 99_999_999),
        (_budget_record(context_window_tokens=100, max_output_tokens=100, reserved_output_tokens=99, safety_margin_tokens=0), 1),
    )
    for record, expected_input in valid_edges:
        result = model_budget_profiles.build_model_budget_profile(
            model_call_id=model_call_id, budget_record=record
        )
        assert result["max_input_tokens"] == expected_input


def test_t06_fixed_formula_uses_one_time_safety_margin_without_hidden_rounding(budget_state):
    prepared = _prepare(budget_state)
    model_call_id = prepared["model_call_id"]
    cases = (
        (12_347, 2_003, 1_997, 431),
        (65_537, 8_191, 7_777, 1_013),
        (9_973, 3_001, 2_333, 127),
    )
    for context_window, max_output, reserved, margin in cases:
        record = _budget_record(
            context_window_tokens=context_window,
            max_output_tokens=max_output,
            reserved_output_tokens=reserved,
            safety_margin_tokens=margin,
        )
        result = model_budget_profiles.build_model_budget_profile(
            model_call_id=model_call_id, budget_record=record
        )
        assert result["max_input_tokens"] == context_window - reserved - margin

    base = model_budget_profiles.build_model_budget_profile(
        model_call_id=model_call_id,
        budget_record=_budget_record(safety_margin_tokens=503),
    )
    plus_seven = model_budget_profiles.build_model_budget_profile(
        model_call_id=model_call_id,
        budget_record=_budget_record(safety_margin_tokens=510),
    )
    assert base["max_input_tokens"] - plus_seven["max_input_tokens"] == 7
    assert plus_seven["max_input_tokens"] != (
        plus_seven["context_window_tokens"]
        - plus_seven["reserved_output_tokens"]
        - 2 * plus_seven["safety_margin_tokens"]
    )


def test_t07_hash_payload_covers_every_frozen_field_and_is_canonical(budget_state):
    prepared = _prepare(budget_state)
    result = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record()
    )
    payload = model_budget_profiles._hash_payload(result)
    baseline = model_budget_profiles._stable_hash(payload)
    assert baseline == result["budget_profile_hash"]

    reversed_payload = dict(reversed(list(payload.items())))
    assert model_budget_profiles._stable_hash(reversed_payload) == baseline

    # Helper-level proof remains useful for fixed fields that cannot legally drift in production.
    for field in model_budget_profiles._HASH_FIELDS:
        changed = deepcopy(payload)
        current = changed[field]
        if type(current) is int:
            changed[field] = current + 1
        else:
            changed[field] = f"{current}-changed"
        assert model_budget_profiles._stable_hash(changed) != baseline, field

    # Caller-supplied hash-bound fields are also varied through the real production builder.
    budget_variations = {
        "budget_policy_version": "budget/2.0",
        "context_window_tokens": 16_385,
        "max_output_tokens": 4_097,
        "reserved_output_tokens": 3_073,
        "safety_margin_tokens": 514,
        "tokenizer_family": "tokenizer-b",
        "tokenizer_version": "2026-09",
        "counting_policy_version": "count/2.0",
    }
    for field, changed_value in budget_variations.items():
        changed_result = model_budget_profiles.build_model_budget_profile(
            model_call_id=prepared["model_call_id"],
            budget_record=_budget_record(**{field: changed_value}),
        )
        assert changed_result[field] == changed_value
        assert changed_result["budget_profile_hash"] != baseline, field

    # A second legitimate preparation proves Ledger preparation identity reaches the production hash.
    second_identity = _prepare(budget_state, call_prepare_key="budget-hash-identity-2")
    second_identity_result = model_budget_profiles.build_model_budget_profile(
        model_call_id=second_identity["model_call_id"], budget_record=_budget_record()
    )
    assert second_identity_result["model_call_id"] != result["model_call_id"]
    assert second_identity_result["call_identity_hash"] != result["call_identity_hash"]
    assert second_identity_result["task_type"] == result["task_type"]
    assert second_identity_result["provider"] == result["provider"]
    assert second_identity_result["model_id"] == result["model_id"]
    assert second_identity_result["model_version"] == result["model_version"]
    assert second_identity_result["budget_profile_hash"] != baseline

    # Ledger-bound semantic fields vary only via formal Ledger production/readback, never private payload edits.
    ledger_variations = (
        (
            "task_type",
            "daily_report_regenerate",
            {"task_type": "daily_report_regenerate"},
            {},
        ),
        (
            "provider",
            "provider-b",
            {"provider": "provider-b"},
            {"provider": "provider-b"},
        ),
        (
            "model_id",
            "model-b",
            {"model_id": "model-b"},
            {"model_id": "model-b"},
        ),
        (
            "model_version",
            "2026-09",
            {"model_version": "2026-09"},
            {"model_version": "2026-09"},
        ),
    )
    for field, expected, prepare_overrides, budget_overrides in ledger_variations:
        changed_prepared = _prepare(
            budget_state,
            call_prepare_key=f"budget-hash-{field}",
            **prepare_overrides,
        )
        changed_result = model_budget_profiles.build_model_budget_profile(
            model_call_id=changed_prepared["model_call_id"],
            budget_record=_budget_record(**budget_overrides),
        )
        assert changed_result[field] == expected
        assert changed_result["budget_profile_hash"] != baseline, field


def test_t08_ledger_missing_and_persisted_tamper_propagate_unchanged(budget_state):
    prepared = _prepare(budget_state)
    missing_id = prepared["model_call_id"] + 1_000_000
    _assert_code(
        "MODEL_CALL_NOT_FOUND",
        lambda: model_budget_profiles.build_model_budget_profile(
            model_call_id=missing_id, budget_record=_budget_record()
        ),
    )

    with sqlite3.connect(budget_state["db_path"]) as conn:
        conn.execute(
            "UPDATE model_calls SET model_version = ? WHERE id = ?",
            ("tampered-version", prepared["model_call_id"]),
        )
        conn.commit()
    _assert_code(
        "MODEL_CALL_LEDGER_INVALID",
        lambda: model_budget_profiles.build_model_budget_profile(
            model_call_id=prepared["model_call_id"], budget_record=_budget_record()
        ),
    )


def test_t09_no_candidate_raw_context_tokenizer_or_git_work_is_performed(budget_state, monkeypatch):
    prepared = _prepare(budget_state)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden upstream/raw/tokenizer/Git work was attempted")

    monkeypatch.setattr(model_call_ledger, "build_context_candidate_set", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    result = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record()
    )
    assert result["token_count_state"] == "not_counted"

    source = inspect.getsource(model_budget_profiles)
    for forbidden_name in (
        "context_resolver",
        "project_profiles",
        "GitClient",
        "tiktoken",
        "subprocess",
        "Path(",
    ):
        assert forbidden_name not in source


def test_t10_zero_network_http_or_downstream_side_effects_on_success_and_failure(budget_state, monkeypatch):
    prepared = _prepare(budget_state)
    calls: list[str] = []

    def forbidden(name):
        def _raise(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"forbidden side effect: {name}")

        return _raise

    # Runtime instrumentation covers the concrete outbound/output mechanisms present in the current codebase.
    monkeypatch.setattr(socket, "socket", forbidden("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))
    monkeypatch.setattr(httpx, "request", forbidden("httpx.request"))
    monkeypatch.setattr(httpx.Client, "request", forbidden("httpx.Client.request"))
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden("httpx.AsyncClient.request"))
    monkeypatch.setattr(smtplib, "SMTP", forbidden("smtplib.SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden("smtplib.SMTP_SSL"))
    monkeypatch.setattr(Path, "write_text", forbidden("Path.write_text"))
    monkeypatch.setattr(Path, "write_bytes", forbidden("Path.write_bytes"))

    ok = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record()
    )
    assert ok["model_send_state"] == "not_admitted"

    _assert_code(
        "MODEL_BUDGET_MODEL_MISMATCH",
        lambda: model_budget_profiles.build_model_budget_profile(
            model_call_id=prepared["model_call_id"],
            budget_record=_budget_record(model_id="other-model"),
        ),
    )
    assert calls == []


def test_t11_success_and_failure_never_write_db_or_schema(budget_state):
    prepared = _prepare(budget_state)
    before = _db_signature(budget_state)

    model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record()
    )
    assert _db_signature(budget_state) == before

    _assert_code(
        "MODEL_BUDGET_LIMIT_INVALID",
        lambda: model_budget_profiles.build_model_budget_profile(
            model_call_id=prepared["model_call_id"],
            budget_record=_budget_record(reserved_output_tokens=0),
        ),
    )
    assert _db_signature(budget_state) == before


def test_t12_formal_ledger_compatibility_and_minimal_dependency_surface(budget_state):
    prepared = _prepare(budget_state)
    ledger = model_call_ledger.get_model_call(prepared["model_call_id"])
    result = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record()
    )

    assert model_budget_profiles.get_model_call is model_call_ledger.get_model_call
    for field in (
        "model_call_id",
        "call_identity_hash",
        "task_type",
        "provider",
        "model_id",
        "model_version",
    ):
        assert result[field] == ledger[field]
    assert result["budget_authority_state"] == "assertion_only"
    assert result["token_count_state"] == "not_counted"
    assert result["model_send_state"] == "not_admitted"

    # Machine-check the complete production import surface instead of inferring it from the PR title.
    tree = ast.parse(inspect.getsource(model_budget_profiles))
    imports: set[tuple[str, str, str | None]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(("import", alias.name, alias.asname))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports.add((f"from:{node.module}", alias.name, alias.asname))

    assert imports == {
        ("from:__future__", "annotations", None),
        ("from:collections.abc", "Mapping", None),
        ("import", "hashlib", None),
        ("import", "json", None),
        ("from:fastapi", "HTTPException", None),
        ("from:app.model_call_ledger", "get_model_call", None),
    }

    dynamic_import_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"__import__", "import_module"}
    }
    assert dynamic_import_calls == set()

    # The exact import surface proves this slice adds no provider SDK, tokenizer, redaction,
    # send-admission, Context Manifest, report/SMTP, filesystem, subprocess, or new DB dependency.
    # Full regression proof is supplied by exact-head GitHub CI, which runs the entire backend suite
    # (including Ledger, Candidate Set, and AI contract validation) plus frontend build/E2E.
