"""Model Execution Result Ledger V1：verified-success、幂等、并发与篡改 Fail Closed。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi import HTTPException

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from app import context_resolver, db, model_call_ledger, model_execution_results  # noqa: E402
from app.ai_contract_validation import validate_ai_response  # noqa: E402
import test_context_candidate_set as candidate_tests  # noqa: E402


EXPECTED_COLUMNS = [
    "id",
    "schema_version",
    "model_call_id",
    "project_id",
    "snapshot_id",
    "local_task_id",
    "task_type",
    "call_identity_hash",
    "provider",
    "model_id",
    "model_version",
    "provider_response_id",
    "actual_model",
    "provider_runtime_fingerprint",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "formal_response_json",
    "formal_response_hash",
    "validated_result_json",
    "validated_result_hash",
    "execution_result_hash",
    "created_at",
]
OUTPUT_SCHEMA_BY_TASK = {
    "project_profile_build": "project-profile/1.0",
    "daily_report_generate": "daily-report/1.0",
    "daily_report_regenerate": "daily-report/1.0",
    "report_contradiction_check": "report-contradiction/1.0",
}


@pytest.fixture()
def ledger_state(tmp_path, monkeypatch):
    state = candidate_tests._make_state(tmp_path, monkeypatch)
    # _make_state initializes the DB before this slice's table may be inspected.
    db.init_db()
    candidate = context_resolver.build_context_candidate_set(state["snapshot_id"])
    assert candidate["items"]
    state["evidence_id"] = candidate["items"][0]["evidence_id"]
    return state


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _row_count(state: dict) -> int:
    with sqlite3.connect(state["db_path"]) as conn:
        return conn.execute("SELECT COUNT(*) FROM model_execution_results").fetchone()[0]


def _row_for_call(state: dict, model_call_id: int) -> dict | None:
    with sqlite3.connect(state["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM model_execution_results WHERE model_call_id = ?",
            (model_call_id,),
        ).fetchone()
        return None if row is None else dict(row)


def _prepare(
    state: dict,
    *,
    task_type: str = "daily_report_generate",
    local_task_id: str = "task-1",
    call_prepare_key: str = "prepare-1",
) -> dict:
    output_schema_version = OUTPUT_SCHEMA_BY_TASK[task_type]
    qualification = {
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "rule_version": "rules/1.0",
        "output_schema_version": output_schema_version,
        "benchmark_sample_pack_version": "samples/1.0",
        "qualification_status": "qualified",
    }
    authorization = {"provider": "provider-a", "authorized": True, "valid": True}
    return model_call_ledger.prepare_model_call(
        local_task_id=local_task_id,
        call_prepare_key=call_prepare_key,
        snapshot_id=state["snapshot_id"],
        task_type=task_type,
        provider="provider-a",
        model_id="model-a",
        model_version="2026-08",
        rule_version="rules/1.0",
        output_schema_version=output_schema_version,
        benchmark_sample_pack_version="samples/1.0",
        qualification_record=qualification,
        data_sending_authorization=authorization,
    )


def _daily_result(evidence_id: str) -> dict:
    return {
        "plain_summary": "本次按冻结证据生成日报。",
        "feature_progress": [
            {
                "feature": "后端结果记录",
                "stage": "开发中",
                "source_type": "git_fact",
                "implementation_scope": "后端",
                "evidence_ids": [evidence_id],
            }
        ],
        "code_change_summary": [],
        "test_evidence": [],
        "risks": [],
        "unknown_items": [],
        "source_warnings": [],
    }


def _result_for_task(task_type: str, evidence_id: str) -> dict:
    if task_type == "project_profile_build":
        return {
            "project_summary": "候选项目档案摘要",
            "features": [],
            "module_path_candidates": [],
            "terminology": [],
            "exclusion_rule_candidates": [],
            "analysis_rule_candidates": [],
            "unmapped_areas": [],
            "evidence_refs": [evidence_id],
        }
    if task_type == "daily_report_generate":
        return _daily_result(evidence_id)
    if task_type == "daily_report_regenerate":
        return {
            "new_report": _daily_result(evidence_id),
            "correction_trace": [
                {
                    "reason": "按冻结证据重新分析",
                    "handled": True,
                    "evidence_ids": [evidence_id],
                }
            ],
        }
    if task_type == "report_contradiction_check":
        return {
            "has_conflict": False,
            "checked_items": [
                {
                    "content": "已核验冻结证据",
                    "source_type": "git_fact",
                    "evidence_ids": [evidence_id],
                }
            ],
            "conflicts": [],
            "unresolved_items": [],
            "recommended_action": "继续按冻结证据推进。",
        }
    raise AssertionError(task_type)


def _receipt(
    state: dict,
    call: dict,
    *,
    result: dict | None = None,
    provider_response_id: str | None = None,
) -> dict:
    return {
        "provider": call["provider"],
        "provider_response_id": provider_response_id or f"resp-{call['model_call_id']}",
        "actual_model": call["model_id"],
        "provider_runtime_fingerprint": "runtime-fp-001",
        "finish_reason": "stop",
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "result": result if result is not None else _result_for_task(call["task_type"], state["evidence_id"]),
    }


def _assert_error(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected
    return caught


def test_t01_happy_path_exact_envelope_and_db_shape(ledger_state):
    call = _prepare(ledger_state)
    record = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
    )
    assert record["schema_version"] == "model_execution_result_v1"
    assert record["model_call_id"] == call["model_call_id"]
    assert record["project_id"] == call["project_id"]
    assert record["snapshot_id"] == call["snapshot_id"]
    assert record["actual_model"] == "model-a"
    assert record["model_version"] == "2026-08"
    assert record["provider_runtime_fingerprint"] == "runtime-fp-001"
    assert record["formal_response"] == {
        "local_task_id": call["local_task_id"],
        "provider_request_id": f"resp-{call['model_call_id']}",
        "task_type": "daily_report_generate",
        "status": "succeeded",
        "schema_version": "ai-agent-contract/1.0",
        "output_schema_version": "daily-report/1.0",
        "actual_model": {
            "provider": "provider-a",
            "id": "model-a",
            "version": "2026-08",
        },
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "cost": "unknown",
        "billing_status": "unknown",
        "warnings": [],
        "result": _daily_result(ledger_state["evidence_id"]),
    }
    assert record["validated_result"] == record["formal_response"]["result"]
    for field in ("formal_response_hash", "validated_result_hash", "execution_result_hash"):
        assert len(record[field]) == 64
        int(record[field], 16)
    assert model_execution_results.get_model_execution_result(record["model_result_id"]) == record
    assert model_execution_results.get_model_execution_result_for_call(call["model_call_id"]) == record

    with sqlite3.connect(ledger_state["db_path"]) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(model_execution_results)")]
        indexes = conn.execute("PRAGMA index_list(model_execution_results)").fetchall()
    assert columns == EXPECTED_COLUMNS
    assert any(index[2] == 1 for index in indexes)  # UNIQUE(model_call_id)


def test_t02_record_reads_formal_call_candidate_and_validator_exactly_once(ledger_state, monkeypatch):
    call = _prepare(ledger_state, call_prepare_key="exactly-once")
    original_call = model_execution_results.get_model_call
    original_candidate = model_execution_results.build_context_candidate_set
    original_validate = model_execution_results.validate_ai_response
    counts = {"call": 0, "candidate": 0, "validate": 0}

    def counted_call(model_call_id):
        counts["call"] += 1
        return original_call(model_call_id)

    def counted_candidate(snapshot_id):
        counts["candidate"] += 1
        return original_candidate(snapshot_id)

    def counted_validate(response, evidence):
        counts["validate"] += 1
        return original_validate(response, evidence)

    monkeypatch.setattr(model_execution_results, "get_model_call", counted_call)
    monkeypatch.setattr(model_execution_results, "build_context_candidate_set", counted_candidate)
    monkeypatch.setattr(model_execution_results, "validate_ai_response", counted_validate)
    model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
    )
    assert counts == {"call": 1, "candidate": 1, "validate": 1}


def test_t03_caller_cannot_inject_authority_and_receipt_is_closed_world(ledger_state):
    call = _prepare(ledger_state, call_prepare_key="inject")
    receipt = _receipt(ledger_state, call)
    with pytest.raises(TypeError):
        model_execution_results.record_model_execution_result(  # type: ignore[call-arg]
            model_call_id=call["model_call_id"],
            receipt=receipt,
            allowed_evidence_ids={ledger_state["evidence_id"]},
        )
    before = _row_count(ledger_state)
    receipt["model_version"] = "caller-version"
    _assert_error(
        "MODEL_EXECUTION_RESULT_RECEIPT_INVALID",
        lambda: model_execution_results.record_model_execution_result(
            model_call_id=call["model_call_id"], receipt=receipt
        ),
    )
    assert _row_count(ledger_state) == before


@pytest.mark.parametrize(
    "task_type",
    [
        "project_profile_build",
        "daily_report_generate",
        "daily_report_regenerate",
        "report_contradiction_check",
    ],
)
def test_t04_every_registered_task_result_contract_can_be_verified(ledger_state, task_type):
    call = _prepare(
        ledger_state,
        task_type=task_type,
        local_task_id=f"task-{task_type}",
        call_prepare_key=f"prepare-{task_type}",
    )
    receipt = _receipt(
        ledger_state,
        call,
        result=_result_for_task(task_type, ledger_state["evidence_id"]),
    )
    record = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=receipt
    )
    assert record["task_type"] == task_type
    assert record["formal_response"]["result"] == receipt["result"]


def test_t05_unknown_evidence_is_formal_output_error_and_zero_write(ledger_state):
    call = _prepare(ledger_state, call_prepare_key="unknown-evidence")
    result = _daily_result("ev-not-in-frozen-candidate")
    before = _row_count(ledger_state)
    _assert_error(
        "MODEL_EXECUTION_RESULT_OUTPUT_INVALID",
        lambda: model_execution_results.record_model_execution_result(
            model_call_id=call["model_call_id"],
            receipt=_receipt(ledger_state, call, result=result),
        ),
    )
    assert _row_count(ledger_state) == before


def test_t06_candidate_duplicate_or_blank_evidence_id_fails_before_validator(ledger_state, monkeypatch):
    call = _prepare(ledger_state, call_prepare_key="candidate-invalid")
    candidate = context_resolver.build_context_candidate_set(call["snapshot_id"])
    broken = deepcopy(candidate)
    broken["items"].append(deepcopy(broken["items"][0]))
    monkeypatch.setattr(model_execution_results, "build_context_candidate_set", lambda _snapshot: broken)
    called = False

    def forbidden_validate(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("validator must not run after evidence closure failure")

    monkeypatch.setattr(model_execution_results, "validate_ai_response", forbidden_validate)
    _assert_error(
        "MODEL_EXECUTION_RESULT_EVIDENCE_INVALID",
        lambda: model_execution_results.record_model_execution_result(
            model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
        ),
    )
    assert called is False
    assert _row_count(ledger_state) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "provider-b"),
        ("actual_model", "model-b"),
        ("provider_response_id", ""),
        ("provider_runtime_fingerprint", " "),
        ("finish_reason", "length"),
        ("prompt_tokens", True),
        ("prompt_tokens", -1),
        ("completion_tokens", 2**63),
        ("total_tokens", 99),
    ],
)
def test_t07_receipt_identity_usage_and_finish_boundaries_fail_closed(ledger_state, field, value):
    call = _prepare(ledger_state, call_prepare_key=f"receipt-{field}-{value}")
    receipt = _receipt(ledger_state, call)
    receipt[field] = value
    before = _row_count(ledger_state)
    _assert_error(
        "MODEL_EXECUTION_RESULT_RECEIPT_INVALID",
        lambda: model_execution_results.record_model_execution_result(
            model_call_id=call["model_call_id"], receipt=receipt
        ),
    )
    assert _row_count(ledger_state) == before


def test_t08_semantic_model_version_and_runtime_fingerprint_never_alias(ledger_state):
    call = _prepare(ledger_state, call_prepare_key="identity-separation")
    receipt = _receipt(ledger_state, call)
    receipt["provider_runtime_fingerprint"] = "backend-fingerprint-xyz"
    record = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=receipt
    )
    assert record["model_version"] == "2026-08"
    assert record["provider_runtime_fingerprint"] == "backend-fingerprint-xyz"
    assert record["formal_response"]["actual_model"]["version"] == "2026-08"
    assert record["formal_response"]["actual_model"]["version"] != record["provider_runtime_fingerprint"]


def test_t09_exact_replay_same_row_and_sequential_drift_conflict(ledger_state):
    call = _prepare(ledger_state, call_prepare_key="replay")
    receipt = _receipt(ledger_state, call)
    first = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=receipt
    )
    before = _row_for_call(ledger_state, call["model_call_id"])
    second = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=deepcopy(receipt)
    )
    assert second == first
    assert _row_for_call(ledger_state, call["model_call_id"]) == before
    assert _row_count(ledger_state) == 1

    drift = deepcopy(receipt)
    drift["provider_response_id"] = "different-provider-response"
    _assert_error(
        "MODEL_EXECUTION_RESULT_CONFLICT",
        lambda: model_execution_results.record_model_execution_result(
            model_call_id=call["model_call_id"], receipt=drift
        ),
    )
    assert _row_count(ledger_state) == 1


def test_t10_real_unique_collision_exact_winner_is_recovered(ledger_state, monkeypatch):
    call = _prepare(ledger_state, call_prepare_key="race-exact")
    original_insert = model_execution_results._insert_result_row
    injected_id = None
    collision_seen = False

    def collide(conn, row):
        nonlocal injected_id, collision_seen
        with sqlite3.connect(ledger_state["db_path"]) as winner_conn:
            winner_conn.row_factory = sqlite3.Row
            injected_id = original_insert(winner_conn, dict(row))
        try:
            return original_insert(conn, row)
        except sqlite3.IntegrityError:
            collision_seen = True
            raise

    monkeypatch.setattr(model_execution_results, "_insert_result_row", collide)
    recovered = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
    )
    assert collision_seen is True
    assert injected_id is not None
    assert recovered["model_result_id"] == injected_id
    assert _row_count(ledger_state) == 1


def test_t11_real_unique_collision_drift_winner_is_conflict(ledger_state, monkeypatch):
    call = _prepare(ledger_state, call_prepare_key="race-drift")
    original_insert = model_execution_results._insert_result_row
    collision_seen = False

    def collide(conn, row):
        nonlocal collision_seen
        winner = dict(row)
        winner["provider_response_id"] = "winner-different-response"
        formal = json.loads(winner["formal_response_json"])
        formal["provider_request_id"] = winner["provider_response_id"]
        winner["formal_response_json"] = model_execution_results._canonical_json(formal)
        winner["formal_response_hash"] = hashlib.sha256(
            winner["formal_response_json"].encode("utf-8")
        ).hexdigest()
        winner["execution_result_hash"] = model_execution_results._stable_hash(
            model_execution_results._execution_identity_payload(winner)
        )
        with sqlite3.connect(ledger_state["db_path"]) as winner_conn:
            winner_conn.row_factory = sqlite3.Row
            original_insert(winner_conn, winner)
        try:
            return original_insert(conn, row)
        except sqlite3.IntegrityError:
            collision_seen = True
            raise

    monkeypatch.setattr(model_execution_results, "_insert_result_row", collide)
    _assert_error(
        "MODEL_EXECUTION_RESULT_CONFLICT",
        lambda: model_execution_results.record_model_execution_result(
            model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
        ),
    )
    assert collision_seen is True
    assert _row_count(ledger_state) == 1


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("formal_response_json", '{ "tampered": true }'),
        ("formal_response_hash", "f" * 64),
        ("validated_result_json", '{ "tampered": true }'),
        ("validated_result_hash", "e" * 64),
        ("execution_result_hash", "d" * 64),
    ],
)
def test_t12_persisted_json_and_hash_tamper_fail_closed(ledger_state, field, replacement):
    call = _prepare(ledger_state, call_prepare_key=f"tamper-{field}")
    record = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
    )
    with sqlite3.connect(ledger_state["db_path"]) as conn:
        conn.execute(
            f"UPDATE model_execution_results SET {field} = ? WHERE id = ?",
            (replacement, record["model_result_id"]),
        )
    _assert_error(
        "MODEL_EXECUTION_RESULT_STORED_INVALID",
        lambda: model_execution_results.get_model_execution_result(record["model_result_id"]),
    )


def test_t13_noncanonical_json_bytes_are_tamper_even_if_semantics_match(ledger_state):
    call = _prepare(ledger_state, call_prepare_key="noncanonical")
    record = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
    )
    with sqlite3.connect(ledger_state["db_path"]) as conn:
        raw = conn.execute(
            "SELECT formal_response_json FROM model_execution_results WHERE id = ?",
            (record["model_result_id"],),
        ).fetchone()[0]
        semantic = json.loads(raw)
        noncanonical = json.dumps(semantic, ensure_ascii=False, indent=2)
        assert noncanonical != raw
        conn.execute(
            "UPDATE model_execution_results SET formal_response_json = ? WHERE id = ?",
            (noncanonical, record["model_result_id"]),
        )
    _assert_error(
        "MODEL_EXECUTION_RESULT_STORED_INVALID",
        lambda: model_execution_results.get_model_execution_result(record["model_result_id"]),
    )


def test_t14_durable_model_call_drift_or_tamper_breaks_result_read(ledger_state):
    call = _prepare(ledger_state, call_prepare_key="call-drift")
    record = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
    )
    with sqlite3.connect(ledger_state["db_path"]) as conn:
        conn.execute(
            "UPDATE model_calls SET model_version = 'tampered-version' WHERE id = ?",
            (call["model_call_id"],),
        )
    _assert_error(
        "MODEL_EXECUTION_RESULT_STORED_INVALID",
        lambda: model_execution_results.get_model_execution_result(record["model_result_id"]),
    )


def test_t15_validator_error_is_zero_write_and_warning_only_remains_verified(ledger_state, monkeypatch):
    call = _prepare(ledger_state, call_prepare_key="validator-error")
    bad_result = _daily_result(ledger_state["evidence_id"])
    bad_result["plain_summary"] = ""
    before = _row_count(ledger_state)
    _assert_error(
        "MODEL_EXECUTION_RESULT_OUTPUT_INVALID",
        lambda: model_execution_results.record_model_execution_result(
            model_call_id=call["model_call_id"],
            receipt=_receipt(ledger_state, call, result=bad_result),
        ),
    )
    assert _row_count(ledger_state) == before

    call2 = _prepare(
        ledger_state,
        local_task_id="task-warning",
        call_prepare_key="validator-warning",
    )
    original = validate_ai_response

    def warning_only(response, allowed_evidence_ids):
        validated = original(response, allowed_evidence_ids)
        assert validated.is_valid is True
        from app.ai_contracts import ValidationIssue, ValidationResult

        return ValidationResult(
            validated.issues
            + (ValidationIssue("TEST_WARNING", "$.result", "warning", severity="warning"),)
        )

    monkeypatch.setattr(model_execution_results, "validate_ai_response", warning_only)
    record = model_execution_results.record_model_execution_result(
        model_call_id=call2["model_call_id"], receipt=_receipt(ledger_state, call2)
    )
    assert record["model_call_id"] == call2["model_call_id"]


def test_t16_not_found_and_signed_integer_guards_are_stable(ledger_state):
    for value in (True, 0, -1, 2**63):
        _assert_error(
            "MODEL_EXECUTION_RESULT_INPUT_INVALID",
            lambda value=value: model_execution_results.get_model_execution_result(value),
        )
        _assert_error(
            "MODEL_EXECUTION_RESULT_INPUT_INVALID",
            lambda value=value: model_execution_results.get_model_execution_result_for_call(value),
        )
    _assert_error(
        "MODEL_EXECUTION_RESULT_NOT_FOUND",
        lambda: model_execution_results.get_model_execution_result(999999),
    )
    _assert_error(
        "MODEL_EXECUTION_RESULT_NOT_FOUND",
        lambda: model_execution_results.get_model_execution_result_for_call(999999),
    )


def test_t17_persistence_and_source_boundary_excludes_raw_transport_secret_and_sender(ledger_state):
    call = _prepare(ledger_state, call_prepare_key="persistence-boundary")
    record = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"], receipt=_receipt(ledger_state, call)
    )
    with sqlite3.connect(ledger_state["db_path"]) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(model_execution_results)")]
        stored = dict(
            zip(
                columns,
                conn.execute(
                    "SELECT * FROM model_execution_results WHERE id = ?",
                    (record["model_result_id"],),
                ).fetchone(),
            )
        )
    forbidden_columns = {
        "api_key",
        "authorization",
        "authorization_header",
        "raw_body",
        "raw_headers",
        "raw_request",
        "raw_context",
        "reasoning",
        "chain_of_thought",
    }
    assert forbidden_columns.isdisjoint(columns)
    persisted_text = json.dumps(stored, ensure_ascii=False, sort_keys=True)
    assert "Bearer " not in persisted_text
    assert "api_key" not in persisted_text.casefold()

    source = inspect.getsource(model_execution_results)
    for forbidden_dependency in ("import httpx", "import requests", "import socket", "import subprocess"):
        assert forbidden_dependency not in source
    for forbidden_entrypoint in ("send_deepseek", "chat/completions", "Authorization"):
        assert forbidden_entrypoint not in source
