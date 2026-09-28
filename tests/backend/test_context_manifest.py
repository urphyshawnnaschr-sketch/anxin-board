"""Context Manifest Core V1: formal upstream closure, preserve-all, safety, and replay tests."""

from __future__ import annotations

import ast
import builtins
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import smtplib
import socket
import sqlite3
import sys

import httpx
import pytest
from fastapi import HTTPException

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from app import (  # noqa: E402
    context_manifest,
    context_resolver,
    model_budget_profiles,
    model_call_ledger,
)
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_model_call_ledger as ledger_tests  # noqa: E402


@pytest.fixture()
def manifest_state(tmp_path, monkeypatch):
    return candidate_tests._make_state(tmp_path, monkeypatch)


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _assert_code(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected
    return caught


def _prepare(state: dict, *, call_prepare_key: str = "manifest-prepare-1", **overrides):
    return ledger_tests._prepare(
        state,
        call_prepare_key=call_prepare_key,
        **overrides,
    )


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


def _manifest_hash(result: dict[str, object]) -> str:
    payload = {key: value for key, value in result.items() if key != "manifest_core_hash"}
    return _canonical_hash(payload)


def _build(state: dict, prepared: dict | None = None, record: dict | None = None):
    if prepared is None:
        prepared = _prepare(state)
    return context_manifest.build_context_manifest_core(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record() if record is None else record,
    )


def test_t01_real_happy_path_closes_formal_upstreams_and_hashes_exact_core(manifest_state):
    prepared = _prepare(manifest_state)
    candidate = context_resolver.build_context_candidate_set(manifest_state["snapshot_id"])
    budget = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record()
    )
    before = _db_signature(manifest_state)

    result = _build(manifest_state, prepared)

    assert set(result) == {
        "schema_version",
        "manifest_stage",
        "model_call_id",
        "call_identity_hash",
        "task_type",
        "snapshot_id",
        "snapshot_hash",
        "project_id",
        "candidate_set_hash",
        "budget_profile",
        "selection_policy_version",
        "profile",
        "range",
        "items",
        "excluded",
        "compression",
        "unsupported_context_sources",
        "estimated_tokens",
        "exact_tokens",
        "redaction_state",
        "token_count_state",
        "budget_fit_state",
        "framing_state",
        "model_send_state",
        "final_manifest_state",
        "manifest_core_hash",
    }
    assert result["schema_version"] == "context_manifest_core_v1"
    assert result["manifest_stage"] == "pre_redaction_selection_core"
    assert result["model_call_id"] == prepared["model_call_id"]
    assert result["call_identity_hash"] == prepared["call_identity_hash"]
    assert result["snapshot_id"] == candidate["snapshot_id"]
    assert result["snapshot_hash"] == candidate["snapshot_hash"]
    assert result["project_id"] == candidate["project_id"]
    assert result["candidate_set_hash"] == candidate["candidate_set_hash"]
    assert result["budget_profile"] == budget
    assert result["selection_policy_version"] == "context_manifest_core_preserve_all_v1"
    assert result["redaction_state"] == "pending"
    assert result["token_count_state"] == "not_counted"
    assert result["budget_fit_state"] == "not_evaluated"
    assert result["framing_state"] == "not_defined"
    assert result["model_send_state"] == "not_admitted"
    assert result["final_manifest_state"] == "not_final"
    assert result["estimated_tokens"] is None
    assert result["exact_tokens"] is None
    assert "manifest_hash" not in result
    assert result["manifest_core_hash"] == _manifest_hash(result)
    assert len(result["manifest_core_hash"]) == 64
    int(result["manifest_core_hash"], 16)
    assert _db_signature(manifest_state) == before


def test_t02_same_frozen_chain_and_budget_are_deep_equal_and_same_hash(manifest_state):
    prepared = _prepare(manifest_state)
    record = _budget_record()
    before = _db_signature(manifest_state)

    first = _build(manifest_state, prepared, deepcopy(record))
    second = _build(manifest_state, prepared, deepcopy(record))

    assert second == first
    assert second["manifest_core_hash"] == first["manifest_core_hash"]
    assert _db_signature(manifest_state) == before


def test_t03_signature_rejects_caller_supplied_snapshot_candidate_and_selection_identity(manifest_state):
    prepared = _prepare(manifest_state)
    signature = inspect.signature(context_manifest.build_context_manifest_core)
    assert list(signature.parameters) == ["model_call_id", "budget_record"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    injections = {
        "snapshot_id": manifest_state["snapshot_id"],
        "candidate_set_hash": "f" * 64,
        "items": [],
        "excluded": [],
        "compression": [],
        "call_identity_hash": "e" * 64,
        "budget_profile_hash": "d" * 64,
    }
    for name, value in injections.items():
        with pytest.raises(TypeError):
            context_manifest.build_context_manifest_core(
                model_call_id=prepared["model_call_id"],
                budget_record=_budget_record(),
                **{name: value},
            )


def test_t04_direct_persisted_ledger_tamper_propagates_ledger_invalid(manifest_state):
    prepared = _prepare(manifest_state)
    with sqlite3.connect(manifest_state["db_path"]) as conn:
        conn.execute(
            "UPDATE model_calls SET provider = 'tampered-provider' WHERE id = ?",
            (prepared["model_call_id"],),
        )

    _assert_code(
        "MODEL_CALL_LEDGER_INVALID",
        lambda: _build(manifest_state, prepared),
    )


def test_t05_self_closed_ledger_candidate_hash_drift_is_detected_by_real_second_closure(manifest_state):
    prepared = _prepare(manifest_state)
    drift_hash = "f" * 64
    assert drift_hash != prepared["candidate_set_hash"]

    with sqlite3.connect(manifest_state["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(
            conn.execute(
                "SELECT * FROM model_calls WHERE id = ?", (prepared["model_call_id"],)
            ).fetchone()
        )
        row["candidate_set_hash"] = drift_hash
        row["call_identity_hash"] = model_call_ledger._stable_hash(
            model_call_ledger._call_identity_payload(row)
        )
        conn.execute(
            "UPDATE model_calls SET candidate_set_hash = ?, call_identity_hash = ? WHERE id = ?",
            (row["candidate_set_hash"], row["call_identity_hash"], prepared["model_call_id"]),
        )

    closed = model_call_ledger.get_model_call(prepared["model_call_id"])
    assert closed["candidate_set_hash"] == drift_hash
    actual_candidate = context_resolver.build_context_candidate_set(manifest_state["snapshot_id"])
    assert actual_candidate["candidate_set_hash"] != closed["candidate_set_hash"]

    _assert_code(
        "CONTEXT_MANIFEST_CANDIDATE_DRIFT",
        lambda: _build(manifest_state, closed),
    )


def test_t06_formal_budget_and_ledger_errors_propagate_without_manifest_remap(manifest_state):
    prepared = _prepare(manifest_state)

    _assert_code(
        "MODEL_BUDGET_INPUT_INVALID",
        lambda: _build(manifest_state, prepared, {}),
    )
    _assert_code(
        "MODEL_BUDGET_MODEL_MISMATCH",
        lambda: _build(manifest_state, prepared, _budget_record(provider="provider-b")),
    )
    _assert_code(
        "MODEL_BUDGET_LIMIT_INVALID",
        lambda: _build(
            manifest_state,
            prepared,
            _budget_record(reserved_output_tokens=5_000),
        ),
    )
    _assert_code(
        "MODEL_CALL_NOT_FOUND",
        lambda: context_manifest.build_context_manifest_core(
            model_call_id=999_999, budget_record=_budget_record()
        ),
    )

    with sqlite3.connect(manifest_state["db_path"]) as conn:
        conn.execute(
            "UPDATE model_calls SET snapshot_hash = ? WHERE id = ?",
            ("0" * 64, prepared["model_call_id"]),
        )
    _assert_code(
        "MODEL_CALL_LEDGER_INVALID",
        lambda: _build(manifest_state, prepared),
    )


def test_t06a_real_double_read_identity_drift_reaches_core_upstream_inconsistent(
    manifest_state, monkeypatch
):
    prepared = _prepare(manifest_state)
    original_outer_get = context_manifest.get_model_call
    mutation_count = 0

    def outer_then_mutate(model_call_id):
        nonlocal mutation_count
        outer = original_outer_get(model_call_id)
        mutation_count += 1
        with sqlite3.connect(manifest_state["db_path"]) as conn:
            conn.row_factory = sqlite3.Row
            row = dict(conn.execute("SELECT * FROM model_calls WHERE id = ?", (model_call_id,)).fetchone())
            row["local_task_id"] = "task-drift-after-outer-read"
            row["call_identity_hash"] = model_call_ledger._stable_hash(
                model_call_ledger._call_identity_payload(row)
            )
            conn.execute(
                "UPDATE model_calls SET local_task_id = ?, call_identity_hash = ? WHERE id = ?",
                (row["local_task_id"], row["call_identity_hash"], model_call_id),
            )
        return outer

    monkeypatch.setattr(context_manifest, "get_model_call", outer_then_mutate)

    _assert_code(
        "CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT",
        lambda: _build(manifest_state, prepared),
    )
    assert mutation_count == 1
    # Budget's independent formal readback sees a self-closed, genuinely different identity.
    reread = model_call_ledger.get_model_call(prepared["model_call_id"])
    assert reread["call_identity_hash"] != prepared["call_identity_hash"]


def test_t06b_real_budget_result_with_missing_core_identity_fails_closed(manifest_state, monkeypatch):
    prepared = _prepare(manifest_state)
    original_budget = context_manifest.build_model_budget_profile
    calls = 0

    def real_budget_then_drop_field(*, model_call_id, budget_record):
        nonlocal calls
        calls += 1
        result = original_budget(model_call_id=model_call_id, budget_record=budget_record)
        malformed = dict(result)
        malformed.pop("call_identity_hash")
        return malformed

    monkeypatch.setattr(context_manifest, "build_model_budget_profile", real_budget_then_drop_field)
    _assert_code(
        "CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT",
        lambda: _build(manifest_state, prepared),
    )
    assert calls == 1


def test_t07_preserve_all_membership_order_and_empty_planning_arrays(manifest_state):
    prepared = _prepare(manifest_state)
    candidate = context_resolver.build_context_candidate_set(manifest_state["snapshot_id"])
    core = _build(manifest_state, prepared)

    expected_items = []
    for item in candidate["items"]:
        projected = dict(item)
        projected["selection_state"] = "preserved_pending_processing"
        expected_items.append(projected)
    assert core["items"] == expected_items
    assert len(core["items"]) == len(candidate["items"])
    assert [item["evidence_id"] for item in core["items"]] == [
        item["evidence_id"] for item in candidate["items"]
    ]
    assert core["excluded"] == []
    assert core["compression"] == []


def test_t08_raw_profile_prd_git_bodies_and_final_manifest_hash_do_not_leak(manifest_state):
    prepared = _prepare(manifest_state)
    core = _build(manifest_state, prepared)

    assert "content" not in core["profile"]
    for item in core["items"]:
        assert {"content", "diff_text", "text", "table_rows"}.isdisjoint(item)
    assert "manifest_hash" not in core
    payload = {key: value for key, value in core.items() if key != "manifest_core_hash"}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert "provider_request" not in serialized
    assert "credentials" not in serialized
    assert core["manifest_core_hash"] == _canonical_hash(payload)


def test_t09_complete_frozen_git_range_is_preserved_and_hash_bound(manifest_state):
    prepared = _prepare(manifest_state)
    candidate = context_resolver.build_context_candidate_set(manifest_state["snapshot_id"])
    core = _build(manifest_state, prepared)

    expected_range = dict(candidate["range"])
    expected_range["selection_state"] = "preserved"
    assert core["range"] == expected_range
    assert core["range"]["commits"] == manifest_state["commits"]
    assert core["range"]["commit_count"] == len(manifest_state["commits"])

    mutated = deepcopy(core)
    mutated.pop("manifest_core_hash")
    mutated["range"] = deepcopy(mutated["range"])
    commits = mutated["range"]["commits"]
    assert isinstance(commits, list) and commits
    original_first = commits[0]
    commits[0] = "0" * 40 if original_first != "0" * 40 else "1" * 40
    assert _canonical_hash(mutated) != core["manifest_core_hash"]


def test_t10_unsupported_context_sources_are_preserved_without_current_rules_backfill(manifest_state):
    prepared = _prepare(manifest_state)
    candidate = context_resolver.build_context_candidate_set(manifest_state["snapshot_id"])
    core = _build(manifest_state, prepared)

    assert core["unsupported_context_sources"] == candidate["unsupported_context_sources"]
    assert "analysis_rules:not_frozen_in_snapshot_v2" in core["unsupported_context_sources"]
    source = inspect.getsource(context_manifest)
    assert "analysis_rules" not in source
    assert "current_rules" not in source


def test_t11_tokenizer_execution_and_token_lies_are_absent(manifest_state, monkeypatch):
    prepared = _prepare(manifest_state)
    original_import = builtins.__import__
    forbidden_imports: list[str] = []

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        lowered = name.casefold()
        if "tokenizer" in lowered or lowered.startswith(("tiktoken", "transformers")):
            forbidden_imports.append(name)
            raise AssertionError(f"tokenizer import attempted: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    core = _build(manifest_state, prepared)

    assert forbidden_imports == []
    assert core["estimated_tokens"] is None
    assert core["exact_tokens"] is None
    assert core["token_count_state"] == "not_counted"
    assert core["budget_fit_state"] == "not_evaluated"
    assert core["budget_profile"]["token_count_state"] == "not_counted"


def test_t12_no_redaction_send_provider_report_smtp_or_external_output(manifest_state, monkeypatch):
    prepared = _prepare(manifest_state)
    calls: list[str] = []

    def bomb(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"forbidden side effect: {name}")

        return fail

    monkeypatch.setattr(socket, "socket", bomb("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", bomb("socket.create_connection"))
    monkeypatch.setattr(httpx, "request", bomb("httpx.request"))
    monkeypatch.setattr(httpx.Client, "request", bomb("httpx.Client.request"))
    monkeypatch.setattr(httpx.AsyncClient, "request", bomb("httpx.AsyncClient.request"))
    monkeypatch.setattr(smtplib, "SMTP", bomb("smtplib.SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", bomb("smtplib.SMTP_SSL"))
    monkeypatch.setattr(Path, "write_text", bomb("Path.write_text"))
    monkeypatch.setattr(Path, "write_bytes", bomb("Path.write_bytes"))

    core = _build(manifest_state, prepared)
    assert calls == []
    assert core["redaction_state"] == "pending"
    assert core["model_send_state"] == "not_admitted"
    assert core["final_manifest_state"] == "not_final"

    tree = ast.parse(inspect.getsource(context_manifest))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.update(f"{module}.{alias.name}" for alias in node.names)
    assert imports == {
        "__future__.annotations",
        "collections.abc.Mapping",
        "fnmatch.fnmatchcase",
        "hashlib",
        "json",
        "fastapi.HTTPException",
        "app.context_candidate_runtime.build_context_candidate_set",
        "app.model_budget_profiles.build_model_budget_profile",
        "app.model_call_ledger.get_model_call",
        "app.project_profiles._path_pattern_error",
        "app.project_profile_v2.profile_planned_modules",
    }


def test_t13_sqlite_authorizer_proves_core_and_upstream_reclosures_are_read_only(
    manifest_state, monkeypatch
):
    prepared = _prepare(manifest_state)
    original_connect = sqlite3.connect
    denied: list[int] = []
    write_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}

    def guarded_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)

        def authorizer(action, _arg1, _arg2, _db_name, _trigger_name):
            if action in write_actions:
                denied.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    core = _build(manifest_state, prepared)

    assert core["schema_version"] == "context_manifest_core_v1"
    assert denied == []


def test_t14_single_core_build_invokes_formal_candidate_builder_exactly_once(manifest_state, monkeypatch):
    prepared = _prepare(manifest_state)
    original_candidate = context_manifest.build_context_candidate_set
    calls: list[int] = []

    def counted(snapshot_id):
        calls.append(snapshot_id)
        return original_candidate(snapshot_id)

    monkeypatch.setattr(context_manifest, "build_context_candidate_set", counted)
    core = _build(manifest_state, prepared)

    assert core["candidate_set_hash"] == prepared["candidate_set_hash"]
    assert calls == [manifest_state["snapshot_id"]]


def test_t15_upstream_outputs_db_schema_config_and_workspace_are_unchanged(manifest_state):
    prepared = _prepare(manifest_state)
    record = _budget_record()
    ledger_before = model_call_ledger.get_model_call(prepared["model_call_id"])
    budget_before = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    )
    candidate_before = context_resolver.build_context_candidate_set(manifest_state["snapshot_id"])
    db_before = _db_signature(manifest_state)
    workspace_before = candidate_tests._git(manifest_state["repo"], "status", "--porcelain")
    with sqlite3.connect(manifest_state["db_path"]) as conn:
        project_before = conn.execute(
            "SELECT git_url, branch FROM projects WHERE id = ?", (manifest_state["project_id"],)
        ).fetchone()

    _build(manifest_state, prepared, deepcopy(record))

    assert model_call_ledger.get_model_call(prepared["model_call_id"]) == ledger_before
    assert model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    ) == budget_before
    assert context_resolver.build_context_candidate_set(manifest_state["snapshot_id"]) == candidate_before
    assert _db_signature(manifest_state) == db_before
    assert candidate_tests._git(manifest_state["repo"], "status", "--porcelain") == workspace_before
    with sqlite3.connect(manifest_state["db_path"]) as conn:
        project_after = conn.execute(
            "SELECT git_url, branch FROM projects WHERE id = ?", (manifest_state["project_id"],)
        ).fetchone()
    assert project_after == project_before
