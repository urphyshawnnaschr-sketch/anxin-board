"""FR-13: confirmed global Project Profile exclusions must govern Context Manifest selection."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import context_manifest  # noqa: E402
from app.context_token_framing import _targets  # noqa: E402

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _item(evidence_id: str, path: str) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "type": "git_file_fact",
        "source_ref": f"git_file_evidence:1:{evidence_id[-1]}",
        "content_hash": HASH_A,
        "git_snapshot_id": 1,
        "ordinal": int(evidence_id[-1]),
        "path": path,
        "added_lines": 1,
        "deleted_lines": 0,
        "is_binary": False,
        "from_commit": "1" * 40,
        "to_commit": "2" * 40,
        "git_facts_hash": HASH_B,
        "file_manifest_hash": HASH_C,
        "resolved_content_hash": HASH_D,
        "resolved_diff_bytes": 10,
        "resolved_content_redaction_state": "pending",
        "content_kind": "text_unified_diff",
        "model_send_state": "not_admitted",
    }


def _prd_item() -> dict[str, object]:
    return {
        "evidence_id": "prd:block:fingerprint:1",
        "type": "prd_block",
        "source_ref": "prd_structured_block:1:1",
        "content_hash": HASH_A,
        "prd_id": 1,
        "ordinal": 1,
        "kind": "paragraph",
        "heading_level": None,
        "page_no": 1,
        "prd_structured_hash": HASH_B,
        "prd_document_fingerprint": HASH_C,
        "resolved_content_redaction_state": "pending",
        "model_send_state": "not_admitted",
    }


def _candidate(*, global_exclusions=None, module_exclusions=None) -> dict[str, object]:
    global_exclusions = [] if global_exclusions is None else global_exclusions
    module_exclusions = [] if module_exclusions is None else module_exclusions
    return {
        "schema_version": "context_candidate_set_v1",
        "snapshot_id": 1,
        "snapshot_hash": HASH_A,
        "project_id": 1,
        "profile": {
            "profile_id": 1,
            "profile_content_hash": HASH_B,
            "source_prd_id": 1,
            "content": {
                "schema_version": "project_profile_manual_v1",
                "project_summary": "demo",
                "modules": [
                    {
                        "client_id": "core",
                        "name": "Core",
                        "description": "",
                        "prd_refs": [],
                        "requirements": ["demo"],
                        "paths": [
                            {
                                "type": "backend",
                                "pattern": "src/**",
                                "required": True,
                                "note": "",
                            }
                        ],
                        "exclusions": list(module_exclusions),
                    }
                ],
                "domain_glossary": [],
                "exclude_patterns": list(global_exclusions),
                "notes": "",
            },
            "resolved_content_redaction_state": "pending",
            "model_send_state": "not_admitted",
        },
        "range": {
            "git_snapshot_id": 1,
            "branch": "main",
            "from_commit": "1" * 40,
            "to_commit": "2" * 40,
            "commits": ["2" * 40],
            "commit_count": 1,
            "git_facts_hash": HASH_B,
        },
        "items": [
            _item("git:file:1:001", "src/public/a.py"),
            _item("git:file:1:002", "src/private/secret.py"),
            _item("git:file:1:003", "assets/nested/data.bin"),
            _prd_item(),
        ],
        "unsupported_context_sources": [],
        "candidate_set_hash": HASH_C,
    }


def _ledger() -> dict[str, object]:
    return {
        "model_call_id": 7,
        "project_id": 1,
        "snapshot_id": 1,
        "snapshot_hash": HASH_A,
        "candidate_set_hash": HASH_C,
        "task_type": "daily_report_generate",
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "call_identity_hash": HASH_D,
    }


def _budget() -> dict[str, object]:
    return {
        "schema_version": "model_budget_profile_v1",
        "model_call_id": 7,
        "call_identity_hash": HASH_D,
        "task_type": "daily_report_generate",
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "budget_policy_version": "budget/1",
        "context_window_tokens": 10000,
        "max_output_tokens": 2000,
        "reserved_output_tokens": 1500,
        "safety_margin_tokens": 500,
        "max_input_tokens": 8000,
        "tokenizer_family": "test",
        "tokenizer_version": "1",
        "counting_policy_version": "utf8_byte_upper_bound_v1",
        "budget_authority_state": "assertion_only",
        "token_count_state": "not_counted",
        "model_send_state": "not_admitted",
        "budget_profile_hash": HASH_A,
    }


def test_empty_global_profile_exclusions_preserve_existing_v1_behavior():
    included, excluded, policy = context_manifest._project_items(_candidate())
    assert [item["evidence_id"] for item in included] == [
        "git:file:1:001",
        "git:file:1:002",
        "git:file:1:003",
        "prd:block:fingerprint:1",
    ]
    assert excluded == []
    assert policy == "context_manifest_core_preserve_all_v1"


def test_global_directory_exclusion_is_recursive_and_auditable():
    included, excluded, policy = context_manifest._project_items(
        _candidate(global_exclusions=["assets/**"])
    )
    assert "git:file:1:003" not in {item["evidence_id"] for item in included}
    assert [item["evidence_id"] for item in excluded] == ["git:file:1:003"]
    assert excluded[0]["selection_state"] == "excluded_by_project_profile"
    assert excluded[0]["matched_exclusion_rules"] == [
        {"scope": "profile", "pattern": "assets/**"}
    ]
    assert policy == "context_manifest_profile_exclusions_v1"


def test_global_exclusion_controls_git_context_but_not_prd_blocks():
    included, excluded, policy = context_manifest._project_items(
        _candidate(global_exclusions=["src/private/**"])
    )
    included_ids = {item["evidence_id"] for item in included}
    assert "git:file:1:002" not in included_ids
    assert "prd:block:fingerprint:1" in included_ids
    assert [item["evidence_id"] for item in excluded] == ["git:file:1:002"]
    assert policy == "context_manifest_profile_exclusions_v1"


def test_plain_directory_pattern_excludes_descendants():
    included, excluded, _policy = context_manifest._project_items(
        _candidate(global_exclusions=["src/private"])
    )
    assert "git:file:1:002" not in {item["evidence_id"] for item in included}
    assert [item["path"] for item in excluded] == ["src/private/secret.py"]


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("src/*/private/**", "src/a/private"),
        ("src/*/private/**", "src/a/private/secret.py"),
        ("**/secrets/**", "secrets/root.txt"),
        ("**/secrets/**", "services/api/secrets/key.txt"),
        ("src/**/private/**", "src/private/root.txt"),
        ("src/**/private/**", "src/a/b/c/private/secret.py"),
        ("src/[ab]/private/**", "src/a/private/secret.py"),
        ("src/?/private/**", "src/z/private/secret.py"),
    ],
)
def test_segment_glob_recursive_rules_exclude_directory_and_descendants(pattern, path):
    assert context_manifest._path_matches_exclusion(path, pattern) is True


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("src/*/private/**", "src/a/public/secret.py"),
        ("src/**/private/**", "src/a/b/public/secret.py"),
        ("src/[ab]/private/**", "src/c/private/secret.py"),
        ("src/?/private/**", "src/ab/private/secret.py"),
    ],
)
def test_segment_glob_rules_do_not_overexclude_nonmatching_paths(pattern, path):
    assert context_manifest._path_matches_exclusion(path, pattern) is False


def test_wildcard_prefix_recursive_rule_is_enforced_in_manifest_not_only_helper():
    candidate = _candidate(global_exclusions=["src/*/private/**"])
    candidate["items"][1]["path"] = "src/a/private/secret.py"

    included, excluded, policy = context_manifest._project_items(candidate)

    assert "git:file:1:002" not in {item["evidence_id"] for item in included}
    assert [item["path"] for item in excluded] == ["src/a/private/secret.py"]
    assert excluded[0]["matched_exclusion_rules"] == [
        {"scope": "profile", "pattern": "src/*/private/**"}
    ]
    assert policy == "context_manifest_profile_exclusions_v1"


def test_recursive_double_star_can_span_multiple_directory_levels_in_manifest():
    candidate = _candidate(global_exclusions=["src/**/private/**"])
    candidate["items"][1]["path"] = "src/a/b/c/private/secret.py"

    included, excluded, _policy = context_manifest._project_items(candidate)

    assert "git:file:1:002" not in {item["evidence_id"] for item in included}
    assert [item["path"] for item in excluded] == ["src/a/b/c/private/secret.py"]


def test_module_exclusions_are_not_promoted_to_project_wide_deny_before_module_projection():
    included, excluded, policy = context_manifest._project_items(
        _candidate(module_exclusions=["src/private/**"])
    )
    assert "git:file:1:002" in {item["evidence_id"] for item in included}
    assert excluded == []
    assert policy == "context_manifest_core_preserve_all_v1"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ../secret",
        "../secret",
        "/absolute",
        "C:/secret",
        "a\\b",
        "src//private/**",
        "src/[/**",
        "src/[abc/**",
        "**/secrets/[",
    ],
)
def test_invalid_frozen_global_exclusion_rule_fails_closed(bad):
    with pytest.raises(HTTPException) as caught:
        context_manifest._project_items(_candidate(global_exclusions=[bad]))
    assert caught.value.detail["code"] == "CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT"


@pytest.mark.parametrize("bad", ["src/[/**", "src/[abc/**", "**/secrets/[", "src//private/**"])
def test_damaged_glob_fails_closed_even_when_match_helper_is_called_directly(bad):
    with pytest.raises(HTTPException) as caught:
        context_manifest._path_matches_exclusion("src/a/private/secret.py", bad)
    assert caught.value.detail["code"] == "CONTEXT_MANIFEST_UPSTREAM_INCONSISTENT"


def test_manifest_hash_binds_global_exclusion_and_framing_targets_cannot_reinclude(
    monkeypatch,
):
    candidate = _candidate(global_exclusions=["assets/**", "src/private/**"])
    monkeypatch.setattr(
        context_manifest, "get_model_call", lambda _model_call_id: deepcopy(_ledger())
    )
    monkeypatch.setattr(
        context_manifest,
        "build_model_budget_profile",
        lambda **_kwargs: deepcopy(_budget()),
    )
    monkeypatch.setattr(
        context_manifest,
        "build_context_candidate_set",
        lambda _snapshot_id: deepcopy(candidate),
    )

    first = context_manifest.build_context_manifest_core(
        model_call_id=7,
        budget_record={"ignored": True},
    )
    second = context_manifest.build_context_manifest_core(
        model_call_id=7,
        budget_record={"ignored": True},
    )

    assert first == second
    assert first["selection_policy_version"] == "context_manifest_profile_exclusions_v1"
    assert [item["evidence_id"] for item in first["excluded"]] == [
        "git:file:1:002",
        "git:file:1:003",
    ]
    assert [item["evidence_id"] for item in first["items"]] == [
        "git:file:1:001",
        "prd:block:fingerprint:1",
    ]
    targets = _targets(first)
    assert "git:file:1:001" in targets
    assert "prd:block:fingerprint:1" in targets
    assert "git:file:1:002" not in targets
    assert "git:file:1:003" not in targets
    assert len(first["manifest_core_hash"]) == 64


def test_global_profile_exclusion_change_changes_manifest_hash(monkeypatch):
    current = {"candidate": _candidate(global_exclusions=["assets/**"])}
    monkeypatch.setattr(
        context_manifest, "get_model_call", lambda _model_call_id: deepcopy(_ledger())
    )
    monkeypatch.setattr(
        context_manifest,
        "build_model_budget_profile",
        lambda **_kwargs: deepcopy(_budget()),
    )
    monkeypatch.setattr(
        context_manifest,
        "build_context_candidate_set",
        lambda _snapshot_id: deepcopy(current["candidate"]),
    )
    first = context_manifest.build_context_manifest_core(model_call_id=7, budget_record={})

    current["candidate"] = _candidate(global_exclusions=["src/private/**"])
    second = context_manifest.build_context_manifest_core(model_call_id=7, budget_record={})

    assert first["manifest_core_hash"] != second["manifest_core_hash"]
