"""Context Redaction Input Closure V1: real historical closure and raw-body safety tests."""

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
    context_redaction_inputs,
    context_resolver,
    model_budget_profiles,
    model_call_ledger,
    project_profiles,
)
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_model_call_ledger as ledger_tests  # noqa: E402


@pytest.fixture()
def redaction_state(tmp_path, monkeypatch):
    return candidate_tests._make_state(tmp_path, monkeypatch)


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _assert_code(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected
    return caught


def _prepare(state: dict, *, call_prepare_key: str = "redaction-input-prepare-1"):
    return ledger_tests._prepare(state, call_prepare_key=call_prepare_key)


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


def _resolve(prepared: dict, target: str, record: dict[str, object] | None = None):
    return context_redaction_inputs.resolve_context_redaction_input(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record() if record is None else record,
        target=target,
    )


def _candidate(state: dict) -> dict:
    return context_resolver.build_context_candidate_set(state["snapshot_id"])


def _item(state: dict, *, item_type: str, binary: bool | None = None) -> dict:
    for item in _candidate(state)["items"]:
        if item["type"] != item_type:
            continue
        if binary is not None and item.get("is_binary") is not binary:
            continue
        return item
    raise AssertionError(f"missing item type={item_type} binary={binary}")


def _canonical_hash(value: dict[str, object]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _redaction_hash(result: dict[str, object]) -> str:
    payload = {
        key: value
        for key, value in result.items()
        if key not in {"raw_body", "redaction_input_hash"}
    }
    return _canonical_hash(payload)


def test_t01_profile_real_closure_returns_exact_historical_body_and_safe_identity(redaction_state):
    prepared = _prepare(redaction_state)
    candidate = _candidate(redaction_state)

    result = _resolve(prepared, "profile")

    assert set(result) == {
        "schema_version",
        "manifest_core_hash",
        "model_call_id",
        "call_identity_hash",
        "snapshot_id",
        "snapshot_hash",
        "project_id",
        "candidate_set_hash",
        "target",
        "target_type",
        "source_identity",
        "raw_body_kind",
        "raw_body",
        "raw_body_state",
        "resolved_content_redaction_state",
        "model_send_state",
        "unsupported_context_sources",
        "redaction_input_hash",
    }
    assert result["schema_version"] == "context_redaction_input_v1"
    assert result["target"] == "profile"
    assert result["target_type"] == "profile"
    assert result["raw_body_kind"] == "profile_json"
    frozen = candidate["profile"]["progress_context"]
    assert result["raw_body"] == {**redaction_state["profile_content"],
        "approved_project_progress": {key: value for key, value in frozen.items() if key != "context_hash"}}
    assert result["source_identity"] == {
        "profile_id": candidate["profile"]["profile_id"],
        "profile_content_hash": candidate["profile"]["profile_content_hash"],
        "source_prd_id": candidate["profile"]["source_prd_id"],
        "progress_context_hash": frozen["context_hash"],
    }
    assert result["raw_body_state"] == "local_unredacted_ephemeral"
    assert result["resolved_content_redaction_state"] == "pending"
    assert result["model_send_state"] == "not_admitted"
    assert result["redaction_input_hash"] == _redaction_hash(result)


def test_t02_prd_paragraph_and_table_use_formal_structured_historical_blocks(redaction_state):
    prepared = _prepare(redaction_state)
    prd_items = [
        item for item in _candidate(redaction_state)["items"] if item["type"] == "prd_block"
    ]
    assert len(prd_items) == 2

    for item in prd_items:
        result = _resolve(prepared, item["evidence_id"])
        block = redaction_state["blocks"][item["ordinal"] - 1]
        assert result["target_type"] == "prd_block"
        assert result["raw_body_kind"] == "prd_structured_block"
        assert result["raw_body"] == {
            "kind": block["kind"],
            "text": block["text"],
            "table_rows": block["table_rows"],
            "heading_level": block["heading_level"],
            "page_no": block["page_no"],
        }
        assert result["source_identity"]["content_hash"] == item["content_hash"]
        assert result["source_identity"]["prd_structured_hash"] == item["prd_structured_hash"]
        assert result["source_identity"]["prd_document_fingerprint"] == item[
            "prd_document_fingerprint"
        ]
        assert result["raw_body_state"] == "local_unredacted_ephemeral"
        assert result["resolved_content_redaction_state"] == "pending"
        assert result["model_send_state"] == "not_admitted"


def test_t03_git_text_uses_exact_formal_historical_diff_and_hash(redaction_state):
    prepared = _prepare(redaction_state)
    item = _item(redaction_state, item_type="git_file_fact", binary=False)
    expected = context_resolver.resolve_git_file_fact(
        redaction_state["snapshot_id"], item["evidence_id"]
    )

    result = _resolve(prepared, item["evidence_id"])

    assert result["target_type"] == "git_file_fact"
    assert result["raw_body_kind"] == "text_unified_diff"
    assert result["raw_body"] == expected["diff_text"]
    assert result["source_identity"]["resolved_content_hash"] == expected[
        "resolved_content_hash"
    ]
    assert result["source_identity"]["resolved_diff_bytes"] == expected[
        "resolved_diff_bytes"
    ]
    raw = result["raw_body"].encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == result["source_identity"]["resolved_content_hash"]
    assert len(raw) == result["source_identity"]["resolved_diff_bytes"]
    assert result["raw_body_state"] == "local_unredacted_ephemeral"
    assert result["model_send_state"] == "not_admitted"


def test_t04_binary_is_metadata_only_and_target_resolver_never_reads_binary_diff(
    redaction_state, monkeypatch
):
    prepared = _prepare(redaction_state)
    item = _item(redaction_state, item_type="git_file_fact", binary=True)
    original_core = context_redaction_inputs.build_context_manifest_core
    calls: list[str] = []

    def core_then_bomb(*, model_call_id, budget_record):
        core = original_core(model_call_id=model_call_id, budget_record=budget_record)

        def bomb(*_args, **_kwargs):
            calls.append("_read_exact_diff")
            raise AssertionError("binary target attempted exact diff materialization")

        monkeypatch.setattr(context_resolver, "_read_exact_diff", bomb)
        return core

    monkeypatch.setattr(context_redaction_inputs, "build_context_manifest_core", core_then_bomb)
    result = _resolve(prepared, item["evidence_id"])

    assert calls == []
    assert result["raw_body"] is None
    assert result["raw_body_kind"] == "binary_metadata_only"
    assert result["raw_body_state"] == "not_applicable"
    assert result["resolved_content_redaction_state"] == "not_applicable"
    assert result["model_send_state"] == "not_admitted"


def test_t05_signature_rejects_snapshot_path_hash_body_policy_and_send_injection(redaction_state):
    prepared = _prepare(redaction_state)
    signature = inspect.signature(context_redaction_inputs.resolve_context_redaction_input)
    assert list(signature.parameters) == ["model_call_id", "budget_record", "target"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    injections = {
        "snapshot_id": redaction_state["snapshot_id"],
        "project_id": redaction_state["project_id"],
        "path": "src/a.txt",
        "content_hash": "f" * 64,
        "raw_body": "secret",
        "sensitive_paths": ["secrets/**"],
        "redaction_result": {"safe": True},
        "send_permission": True,
        "provider_request": {"messages": []},
    }
    for name, value in injections.items():
        with pytest.raises(TypeError):
            context_redaction_inputs.resolve_context_redaction_input(
                model_call_id=prepared["model_call_id"],
                budget_record=_budget_record(),
                target="profile",
                **{name: value},
            )


def test_t06_target_admission_rejects_invalid_missing_and_duplicate_membership(
    redaction_state, monkeypatch
):
    prepared = _prepare(redaction_state)
    _assert_code("CONTEXT_REDACTION_INPUT_TARGET_INVALID", lambda: _resolve(prepared, ""))
    _assert_code(
        "CONTEXT_REDACTION_INPUT_TARGET_INVALID",
        lambda: _resolve(prepared, "prd:block:not-present:999"),
    )

    target = _item(redaction_state, item_type="prd_block")["evidence_id"]
    original_core = context_redaction_inputs.build_context_manifest_core

    def duplicate_member(*, model_call_id, budget_record):
        core = original_core(model_call_id=model_call_id, budget_record=budget_record)
        duplicate = deepcopy(
            next(item for item in core["items"] if item["evidence_id"] == target)
        )
        core["items"].append(duplicate)
        payload = {key: value for key, value in core.items() if key != "manifest_core_hash"}
        core["manifest_core_hash"] = context_redaction_inputs._stable_hash(payload)
        return core

    monkeypatch.setattr(context_redaction_inputs, "build_context_manifest_core", duplicate_member)
    _assert_code("CONTEXT_REDACTION_INPUT_TARGET_INVALID", lambda: _resolve(prepared, target))


def test_t07_real_prd_resolver_identity_drift_fails_closed_as_body_drift(
    redaction_state, monkeypatch
):
    prepared = _prepare(redaction_state)
    target = _item(redaction_state, item_type="prd_block")["evidence_id"]
    original = context_redaction_inputs.resolve_prd_block

    def real_then_drift(snapshot_id, evidence_id):
        result = original(snapshot_id, evidence_id)
        drifted = dict(result)
        drifted["content_hash"] = (
            "f" * 64 if result["content_hash"] != "f" * 64 else "e" * 64
        )
        return drifted

    monkeypatch.setattr(context_redaction_inputs, "resolve_prd_block", real_then_drift)
    _assert_code("CONTEXT_REDACTION_INPUT_BODY_DRIFT", lambda: _resolve(prepared, target))


def test_t08_profile_second_candidate_closure_detects_identity_drift(redaction_state, monkeypatch):
    prepared = _prepare(redaction_state)
    original = context_redaction_inputs.build_context_candidate_set

    def real_then_profile_drift(snapshot_id):
        candidate = original(snapshot_id)
        drifted = deepcopy(candidate)
        current_hash = drifted["profile"]["profile_content_hash"]
        drifted["profile"]["profile_content_hash"] = (
            "f" * 64 if current_hash != "f" * 64 else "e" * 64
        )
        return drifted

    monkeypatch.setattr(context_redaction_inputs, "build_context_candidate_set", real_then_profile_drift)
    _assert_code("CONTEXT_REDACTION_INPUT_BODY_DRIFT", lambda: _resolve(prepared, "profile"))


def test_t09_upstream_errors_propagate_and_target_resolver_error_is_not_remapped(
    redaction_state, monkeypatch
):
    prepared = _prepare(redaction_state)
    _assert_code(
        "MODEL_BUDGET_INPUT_INVALID",
        lambda: _resolve(prepared, "profile", {}),
    )
    _assert_code(
        "MODEL_CALL_NOT_FOUND",
        lambda: context_redaction_inputs.resolve_context_redaction_input(
            model_call_id=999_999,
            budget_record=_budget_record(),
            target="profile",
        ),
    )

    target = _item(redaction_state, item_type="prd_block")["evidence_id"]
    original_core = context_redaction_inputs.build_context_manifest_core

    def core_then_remove_artifact(*, model_call_id, budget_record):
        core = original_core(model_call_id=model_call_id, budget_record=budget_record)
        redaction_state["prd_target"].unlink()
        return core

    monkeypatch.setattr(
        context_redaction_inputs,
        "build_context_manifest_core",
        core_then_remove_artifact,
    )
    _assert_code("CONTEXT_PRD_ARTIFACT_REQUIRED", lambda: _resolve(prepared, target))


def test_t10_redaction_input_hash_is_deterministic_safe_identity_hash_and_excludes_raw_body(
    redaction_state,
):
    prepared = _prepare(redaction_state)
    first = _resolve(prepared, "profile")
    second = _resolve(prepared, "profile")

    assert second == first
    assert first["redaction_input_hash"] == _redaction_hash(first)

    raw_mutated = deepcopy(first)
    raw_mutated["raw_body"] = {"changed": "secret body only"}
    assert _redaction_hash(raw_mutated) == first["redaction_input_hash"]

    identity_mutated = deepcopy(first)
    identity_mutated["source_identity"]["profile_content_hash"] = "f" * 64
    assert _redaction_hash(identity_mutated) != first["redaction_input_hash"]


def test_t11_unsupported_sources_remain_fail_visible_without_current_rules_backfill(redaction_state):
    prepared = _prepare(redaction_state)
    result = _resolve(prepared, "profile")

    assert "analysis_rules:not_frozen_in_snapshot_v2" in result["unsupported_context_sources"]
    source = inspect.getsource(context_redaction_inputs)
    assert "current_rules" not in source
    assert "rules_version" not in source
    assert result["model_send_state"] == "not_admitted"


def test_t12_raw_body_has_no_db_write_file_output_log_network_provider_or_smtp_side_effect(
    redaction_state, monkeypatch, capsys
):
    prepared = _prepare(redaction_state)
    db_before = candidate_tests._db_dump(redaction_state["db_path"])
    original_connect = sqlite3.connect
    denied: list[int] = []
    calls: list[str] = []
    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_ALTER_TABLE,
    }

    def guarded_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)

        def authorizer(action, _arg1, _arg2, _db_name, _trigger_name):
            if action in write_actions:
                denied.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    def bomb(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"forbidden side effect: {name}")

        return fail

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(socket, "socket", bomb("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", bomb("socket.create_connection"))
    monkeypatch.setattr(httpx, "request", bomb("httpx.request"))
    monkeypatch.setattr(httpx.Client, "request", bomb("httpx.Client.request"))
    monkeypatch.setattr(httpx.AsyncClient, "request", bomb("httpx.AsyncClient.request"))
    monkeypatch.setattr(smtplib, "SMTP", bomb("smtplib.SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", bomb("smtplib.SMTP_SSL"))
    monkeypatch.setattr(Path, "write_text", bomb("Path.write_text"))
    monkeypatch.setattr(Path, "write_bytes", bomb("Path.write_bytes"))

    result = _resolve(prepared, "profile")
    _assert_code(
        "CONTEXT_REDACTION_INPUT_TARGET_INVALID",
        lambda: _resolve(prepared, "missing-target-after-core"),
    )
    captured = capsys.readouterr()

    assert {key: value for key, value in result["raw_body"].items() if key != "approved_project_progress"} == redaction_state["profile_content"]
    assert result["raw_body"]["approved_project_progress"]["previous"] is None
    assert denied == []
    assert calls == []
    assert candidate_tests._db_dump(redaction_state["db_path"]) == db_before
    secret = redaction_state["profile_content"]["project_summary"]
    assert secret not in captured.out
    assert secret not in captured.err


def test_t13_no_tokenizer_redactor_sensitive_path_send_admission_or_provider_runtime(
    redaction_state, monkeypatch
):
    prepared = _prepare(redaction_state)
    original_import = builtins.__import__
    forbidden_imports: list[str] = []

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        lowered = name.casefold()
        if lowered.startswith(("tiktoken", "transformers")) or "redactor" in lowered:
            forbidden_imports.append(name)
            raise AssertionError(f"forbidden downstream import: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = _resolve(prepared, "profile")

    assert forbidden_imports == []
    assert result["resolved_content_redaction_state"] == "pending"
    assert result["model_send_state"] == "not_admitted"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "admitted" not in serialized.replace("not_admitted", "")
    assert "exact_tokens" not in result
    assert "manifest_hash" not in result

    tree = ast.parse(inspect.getsource(context_redaction_inputs))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    assert not any(
        token in name.casefold()
        for name in imported
        for token in ("tiktoken", "transformers", "redactor", "provider_sdk")
    )


def test_t14_current_mutable_profile_is_ignored_and_dirty_worktree_fails_closed(
    redaction_state, monkeypatch
):
    prepared = _prepare(redaction_state)
    historical = _resolve(prepared, "profile")

    alternate = project_profiles.ProjectProfileContent(
        schema_version=project_profiles.SCHEMA_VERSION,
        project_summary="CURRENT MUTABLE PROFILE MUST NOT REPLACE FROZEN PROFILE",
        modules=[],
        domain_glossary=[],
        exclude_patterns=[],
        notes="current-only",
    )
    alt_json, alt_hash = project_profiles._canonicalize(alternate)
    with sqlite3.connect(redaction_state["db_path"]) as conn:
        conn.execute(
            "UPDATE projects SET name = 'Current Mutable Name' WHERE id = ?",
            (redaction_state["project_id"],),
        )
        # Preserve the frozen row's id/content/hash but make it historical, so the DB's
        # one-confirmed-profile invariant permits a distinct newer current profile.
        conn.execute(
            "UPDATE project_profiles SET status = 'superseded' WHERE id = ?",
            (redaction_state["profile_id"],),
        )
        conn.execute(
            """
            INSERT INTO project_profiles (
                project_id, version_no, source_prd_id, status, content_json,
                content_hash, edit_version, created_at, updated_at, confirmed_by, confirmed_at
            ) VALUES (?, 99, ?, 'confirmed', ?, ?, 1,
                      '2026-08-19T00:00:00+00:00', '2026-08-19T00:00:00+00:00', 'pm', '2026-08-19T00:00:00+00:00')
            """,
            (
                redaction_state["project_id"],
                redaction_state["prd_id"],
                alt_json,
                alt_hash,
            ),
        )

    assert _resolve(prepared, "profile") == historical

    git_target = _item(redaction_state, item_type="git_file_fact", binary=False)["evidence_id"]
    original_core = context_redaction_inputs.build_context_manifest_core

    def core_then_dirty_workspace(*, model_call_id, budget_record):
        core = original_core(model_call_id=model_call_id, budget_record=budget_record)
        (redaction_state["repo"] / "current-only.txt").write_text("dirty", encoding="utf-8")
        return core

    monkeypatch.setattr(
        context_redaction_inputs,
        "build_context_manifest_core",
        core_then_dirty_workspace,
    )
    _assert_code("GIT_WORKSPACE_DIRTY", lambda: _resolve(prepared, git_target))


def test_t15_upstream_outputs_db_prd_and_git_state_are_unchanged_after_real_text_closure(
    redaction_state,
):
    prepared = _prepare(redaction_state)
    record = _budget_record()
    target = _item(redaction_state, item_type="git_file_fact", binary=False)["evidence_id"]

    ledger_before = model_call_ledger.get_model_call(prepared["model_call_id"])
    budget_before = model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    )
    candidate_before = _candidate(redaction_state)
    core_before = context_manifest.build_context_manifest_core(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    )
    state_before = candidate_tests._state_signature(redaction_state)

    result = _resolve(prepared, target, deepcopy(record))

    assert result["model_send_state"] == "not_admitted"
    assert model_call_ledger.get_model_call(prepared["model_call_id"]) == ledger_before
    assert model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    ) == budget_before
    assert _candidate(redaction_state) == candidate_before
    assert context_manifest.build_context_manifest_core(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    ) == core_before
    assert candidate_tests._state_signature(redaction_state) == state_before
