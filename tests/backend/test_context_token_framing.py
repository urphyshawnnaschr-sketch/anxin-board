"""Token + Framing Accounting V1 acceptance tests."""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import context_token_framing  # noqa: E402


H = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _budget(*, max_input: int = 100_000, counting_policy: str = "utf8_byte_upper_bound_v1"):
    value = {
        "schema_version": "model_budget_profile_v1",
        "model_call_id": 7,
        "call_identity_hash": H("call"),
        "task_type": "daily_report",
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "budget_policy_version": "budget/1.0",
        "context_window_tokens": 200_000,
        "max_output_tokens": 8_000,
        "reserved_output_tokens": 4_000,
        "safety_margin_tokens": 1_000,
        "max_input_tokens": max_input,
        "tokenizer_family": "asserted-tokenizer",
        "tokenizer_version": "asserted-v1",
        "counting_policy_version": counting_policy,
        "budget_authority_state": "assertion_only",
        "token_count_state": "not_counted",
        "model_send_state": "not_admitted",
    }
    value["budget_profile_hash"] = _stable_hash(value)
    return value


def _core(
    targets: tuple[str, ...],
    *,
    max_input: int = 100_000,
    counting_policy: str = "utf8_byte_upper_bound_v1",
    unsupported: tuple[str, ...] = (),
):
    value = {
        "schema_version": "context_manifest_core_v1",
        "manifest_stage": "pre_redaction_selection_core",
        "model_call_id": 7,
        "call_identity_hash": H("call"),
        "task_type": "daily_report",
        "snapshot_id": 11,
        "snapshot_hash": H("snapshot"),
        "project_id": 3,
        "candidate_set_hash": H("candidate"),
        "budget_profile": _budget(max_input=max_input, counting_policy=counting_policy),
        "selection_policy_version": "context_manifest_core_preserve_all_v1",
        "profile": {"profile_id": 5},
        "range": {"git_snapshot_id": 9},
        "items": [{"evidence_id": target} for target in targets],
        "excluded": [],
        "compression": [],
        "unsupported_context_sources": list(unsupported),
        "estimated_tokens": None,
        "exact_tokens": None,
        "redaction_state": "pending",
        "token_count_state": "not_counted",
        "budget_fit_state": "not_evaluated",
        "framing_state": "not_defined",
        "model_send_state": "not_admitted",
        "final_manifest_state": "not_final",
    }
    value["manifest_core_hash"] = _stable_hash(value)
    return value


def _source_identity(target: str, target_type: str, *, binary: bool = False):
    if target_type == "profile":
        return {"profile_id": 5, "profile_content_hash": H("profile")}
    if target_type == "prd_block":
        return {"evidence_id": target, "content_hash": H(target)}
    return {"evidence_id": target, "path": f"src/{target}.py", "is_binary": binary}


def _redaction(core: dict, target: str, spec: dict):
    target_type = spec["target_type"]
    body = deepcopy(spec["body"])
    source_identity = deepcopy(spec["source_identity"])
    kind = {
        "profile": "profile_json",
        "prd_block": "prd_structured_block",
        "git_file_fact": "text_unified_diff",
    }[target_type]
    if kind == "text_unified_diff":
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    else:
        body_hash = _stable_hash(body)
    result = {
        "schema_version": "context_redaction_result_v1",
        "redaction_input_hash": H(f"input:{target}"),
        "manifest_core_hash": core["manifest_core_hash"],
        "model_call_id": core["model_call_id"],
        "call_identity_hash": core["call_identity_hash"],
        "snapshot_id": core["snapshot_id"],
        "snapshot_hash": core["snapshot_hash"],
        "project_id": core["project_id"],
        "candidate_set_hash": core["candidate_set_hash"],
        "target": target,
        "target_type": target_type,
        "source_identity": source_identity,
        "raw_body_kind": kind,
        "redaction_policy_id": "credential_redaction_v1",
        "redaction_policy_hash": H("redaction-policy"),
        "redaction_state": "completed",
        "redacted_body_state": "local_redacted_ephemeral",
        "redacted_body": body,
        "redacted_body_hash": body_hash,
        "redaction_stats": {},
        "redaction_match_count": 0,
        "model_send_state": "not_admitted",
        "unsupported_context_sources": list(core["unsupported_context_sources"]),
    }
    hash_payload = {
        key: deepcopy(result[key])
        for key in result
        if key != "redacted_body"
    }
    result["redaction_result_hash"] = _stable_hash(hash_payload)
    return result


def _admission(core: dict, target: str, spec: dict):
    admitted = spec.get("state", "admitted") == "admitted"
    redaction_hash = (
        _redaction(core, target, spec)["redaction_result_hash"]
        if admitted
        else H(f"denied-redaction:{target}")
    )
    reason = spec.get(
        "reason",
        "content_policy_passed" if admitted else "sensitive_path_hard_deny",
    )
    result = {
        "schema_version": "model_send_admission_v1",
        "sensitive_path_policy_identity_hash": H(f"path-policy:{target}"),
        "redaction_result_hash": redaction_hash,
        "model_call_id": core["model_call_id"],
        "call_identity_hash": core["call_identity_hash"],
        "snapshot_id": core["snapshot_id"],
        "snapshot_hash": core["snapshot_hash"],
        "project_id": core["project_id"],
        "candidate_set_hash": core["candidate_set_hash"],
        "target": target,
        "target_type": spec["target_type"],
        "source_identity": deepcopy(spec["source_identity"]),
        "path_subject_state": "available" if spec["target_type"] == "git_file_fact" else "not_applicable",
        "path_identity_hash": H(f"path:{target}") if spec["target_type"] == "git_file_fact" else None,
        "sensitive_path_policy_id": "builtin_sensitive_path_v1",
        "sensitive_path_policy_hash": H("sensitive-policy"),
        "policy_evaluation_state": "evaluated" if spec["target_type"] == "git_file_fact" else "not_applicable",
        "sensitive_path_decision": spec.get(
            "sensitive_path_decision",
            "not_blocked" if spec["target_type"] == "git_file_fact" and admitted else (
                "hard_deny" if spec["target_type"] == "git_file_fact" and reason == "sensitive_path_hard_deny" else "not_applicable"
            ),
        ),
        "matched_rule_id": spec.get("matched_rule_id"),
        "model_send_state": "admitted" if admitted else "denied",
        "admission_reason": reason,
        "unsupported_context_sources": list(core["unsupported_context_sources"]),
    }
    result["model_send_admission_hash"] = _stable_hash(result)
    return result


def _specs(*, unicode_body: bool = False):
    return {
        "profile": {
            "target_type": "profile",
            "source_identity": _source_identity("profile", "profile"),
            "body": {"name": "安心看板" if unicode_body else "Anxin"},
        },
        "prd:1": {
            "target_type": "prd_block",
            "source_identity": _source_identity("prd:1", "prd_block"),
            "body": {"kind": "paragraph", "text": "今日进展" if unicode_body else "progress"},
        },
        "git:1": {
            "target_type": "git_file_fact",
            "source_identity": _source_identity("git:1", "git_file_fact"),
            "body": "+新增一行\n" if unicode_body else "+added line\n",
        },
    }


def _install(
    monkeypatch,
    specs: dict,
    *,
    targets: tuple[str, ...] = ("prd:1", "git:1"),
    max_input: int = 100_000,
    counting_policy: str = "utf8_byte_upper_bound_v1",
    unsupported: tuple[str, ...] = (),
):
    core = _core(
        targets,
        max_input=max_input,
        counting_policy=counting_policy,
        unsupported=unsupported,
    )
    calls = {"core": 0, "admission": [], "redaction": []}

    def build_core(*, model_call_id, budget_record):
        calls["core"] += 1
        assert model_call_id == 7
        return deepcopy(core)

    def build_admission(*, model_call_id, budget_record, target):
        calls["admission"].append(target)
        return _admission(core, target, specs[target])

    def build_redaction(*, model_call_id, budget_record, target):
        calls["redaction"].append(target)
        assert specs[target].get("state", "admitted") == "admitted"
        return _redaction(core, target, specs[target])

    monkeypatch.setattr(context_token_framing, "_batch_selection", lambda _: None)
    monkeypatch.setattr(context_token_framing, "build_context_manifest_core", build_core)
    monkeypatch.setattr(context_token_framing, "build_model_send_admission", build_admission)
    monkeypatch.setattr(context_token_framing, "build_context_redaction_result", build_redaction)
    return core, calls


def _run():
    return context_token_framing.build_context_token_framing_accounting(
        model_call_id=7,
        budget_record={"caller": "opaque-to-this-layer"},
    )


def _frame_bytes(core: dict, target: str, spec: dict, ordinal: int):
    redaction = _redaction(core, target, spec)
    frame = {
        "frame_schema_version": "context_payload_frame_v1",
        "ordinal": ordinal,
        "target": target,
        "target_type": spec["target_type"],
        "redaction_result_hash": redaction["redaction_result_hash"],
        "body": deepcopy(spec["body"]),
    }
    return _canonical_bytes(frame)


def _code(caught: pytest.ExceptionInfo[HTTPException]) -> str:
    return caught.value.detail["code"]


def test_t01_entry_has_exact_two_keyword_only_parameters():
    sig = inspect.signature(context_token_framing.build_context_token_framing_accounting)
    assert list(sig.parameters) == ["model_call_id", "budget_record"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())


def test_t02_core_is_called_exactly_once(monkeypatch):
    specs = _specs()
    _, calls = _install(monkeypatch, specs)
    _run()
    assert calls["core"] == 1


def test_t03_target_order_is_profile_first_then_candidate_item_order(monkeypatch):
    specs = _specs()
    _, calls = _install(monkeypatch, specs, targets=("git:1", "prd:1"))
    _run()
    assert calls["admission"] == ["profile", "git:1", "prd:1"]


def test_t04_admission_is_exactly_once_per_target(monkeypatch):
    specs = _specs()
    _, calls = _install(monkeypatch, specs)
    _run()
    assert calls["admission"] == ["profile", "prd:1", "git:1"]
    assert len(calls["admission"]) == len(set(calls["admission"]))


def test_t05_admitted_target_redaction_exactly_once_and_hash_bound(monkeypatch):
    specs = _specs()
    core, calls = _install(monkeypatch, specs)
    result = _run()
    assert calls["redaction"] == ["profile", "prd:1", "git:1"]
    expected = {
        target: _redaction(core, target, specs[target])["redaction_result_hash"]
        for target in ("profile", "prd:1", "git:1")
    }
    assert {item["target"]: item["redaction_result_hash"] for item in result["admitted_targets"]} == expected


def test_t06_denied_target_never_gets_second_redaction_body_read(monkeypatch):
    specs = _specs()
    specs["git:1"].update(state="denied", reason="sensitive_path_hard_deny", matched_rule_id="SP01")
    _, calls = _install(monkeypatch, specs)
    result = _run()
    assert "git:1" not in calls["redaction"]
    assert result["denied_targets"][0]["target"] == "git:1"


def test_t07_canonical_single_frame_hash_and_byte_count_are_independently_recomputed(monkeypatch):
    specs = _specs()
    core, _ = _install(monkeypatch, specs, targets=())
    result = _run()
    encoded = _frame_bytes(core, "profile", specs["profile"], 1)
    summary = result["admitted_targets"][0]
    assert summary["frame_hash"] == hashlib.sha256(encoded).hexdigest()
    assert summary["frame_utf8_bytes"] == len(encoded)


def test_t08_aggregate_payload_uses_exactly_one_lf_between_frames(monkeypatch):
    specs = _specs()
    core, _ = _install(monkeypatch, specs, targets=("prd:1",))
    result = _run()
    payload = b"\n".join(
        [
            _frame_bytes(core, "profile", specs["profile"], 1),
            _frame_bytes(core, "prd:1", specs["prd:1"], 2),
        ]
    )
    assert result["framed_payload_hash"] == hashlib.sha256(payload).hexdigest()
    assert result["framed_payload_utf8_bytes"] == len(payload)


def test_t09_utf8_multibyte_characters_are_counted_as_bytes_not_characters(monkeypatch):
    specs = _specs(unicode_body=True)
    core, _ = _install(monkeypatch, specs, targets=())
    result = _run()
    encoded = _frame_bytes(core, "profile", specs["profile"], 1)
    assert len(encoded) > len(encoded.decode("utf-8"))
    assert result["framed_payload_utf8_bytes"] == len(encoded)


def test_t10_conservative_upper_bound_is_exactly_payload_utf8_bytes(monkeypatch):
    _install(monkeypatch, _specs())
    result = _run()
    assert result["conservative_input_token_upper_bound"] == result["framed_payload_utf8_bytes"]
    assert result["exact_tokens"] is None


def test_t11_budget_fit_boundary_uses_less_than_or_equal(monkeypatch):
    specs = _specs()
    provisional = _core((), max_input=100_000)
    boundary = len(_frame_bytes(provisional, "profile", specs["profile"], 1))
    _install(monkeypatch, specs, targets=(), max_input=boundary)
    result = _run()
    assert result["framed_payload_utf8_bytes"] == boundary
    assert result["budget_fit_state"] == "fit_by_conservative_upper_bound"


def test_t12_over_budget_is_fail_visible_not_fit(monkeypatch):
    specs = _specs()
    provisional = _core((), max_input=100_000)
    payload_len = len(_frame_bytes(provisional, "profile", specs["profile"], 1))
    _install(monkeypatch, specs, targets=(), max_input=payload_len - 1)
    result = _run()
    assert result["budget_fit_state"] == "not_fit_by_conservative_upper_bound"
    assert result["final_request_fit_state"] == "not_evaluated"


def test_t13_unsupported_counting_policy_fails_closed_before_target_work(monkeypatch):
    _, calls = _install(monkeypatch, _specs(), counting_policy="provider-exact-v999")
    with pytest.raises(HTTPException) as caught:
        _run()
    assert _code(caught) == "TOKEN_FRAMING_COUNTING_POLICY_UNSUPPORTED"
    assert calls["admission"] == []
    assert calls["redaction"] == []


def test_t14_sensitive_git_deny_remains_fail_visible(monkeypatch):
    specs = _specs()
    specs["git:1"].update(
        state="denied",
        reason="sensitive_path_hard_deny",
        matched_rule_id="SP20",
        sensitive_path_decision="hard_deny",
    )
    _install(monkeypatch, specs)
    result = _run()
    denied = result["denied_targets"][0]
    assert denied["target"] == "git:1"
    assert denied["admission_reason"] == "sensitive_path_hard_deny"
    assert denied["matched_rule_id"] == "SP20"
    assert result["context_admission_state"] == "contains_denied_targets"


def test_t15_git_binary_deny_remains_fail_visible(monkeypatch):
    specs = _specs()
    specs["git:1"].update(
        state="denied",
        reason="binary_metadata_only_no_sendable_body",
        matched_rule_id=None,
        sensitive_path_decision="not_blocked",
        source_identity=_source_identity("git:1", "git_file_fact", binary=True),
    )
    _install(monkeypatch, specs)
    result = _run()
    denied = result["denied_targets"][0]
    assert denied["admission_reason"] == "binary_metadata_only_no_sendable_body"
    assert denied["matched_rule_id"] is None


def test_t16_mixed_admitted_and_denied_targets_are_not_silently_dropped(monkeypatch):
    specs = _specs()
    specs["git:1"].update(state="denied", reason="sensitive_path_hard_deny", matched_rule_id="SP01")
    _install(monkeypatch, specs)
    result = _run()
    assert [item["target"] for item in result["admitted_targets"]] == ["profile", "prd:1"]
    assert [item["target"] for item in result["denied_targets"]] == ["git:1"]
    assert result["context_admission_state"] == "contains_denied_targets"


def test_t17_unsupported_context_sources_are_preserved_exactly(monkeypatch):
    marker = ("analysis_rules:not_frozen_in_snapshot_v2",)
    _install(monkeypatch, _specs(), unsupported=marker)
    result = _run()
    assert result["unsupported_context_sources"] == list(marker)
    assert result["coverage_state"] == "partial_fail_visible"


def test_t18_result_never_returns_raw_or_redacted_body(monkeypatch):
    specs = _specs()
    specs["profile"]["body"] = {"secret_note": "DO_NOT_RETURN_THIS_BODY"}
    _install(monkeypatch, specs)
    result = _run()
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert "DO_NOT_RETURN_THIS_BODY" not in rendered
    assert "redacted_body" not in rendered
    assert '"body"' not in rendered


def test_t19_repeat_is_deterministic_hash_recomputes_and_inputs_are_not_mutated(monkeypatch):
    specs = _specs()
    specs_before = deepcopy(specs)
    _install(monkeypatch, specs)
    budget_record = {"caller": ["must", "not", "mutate"]}
    budget_before = deepcopy(budget_record)
    first = context_token_framing.build_context_token_framing_accounting(
        model_call_id=7, budget_record=budget_record
    )
    second = context_token_framing.build_context_token_framing_accounting(
        model_call_id=7, budget_record=budget_record
    )
    assert second == first
    payload = {
        key: deepcopy(first[key])
        for key in context_token_framing._RESULT_HASH_KEYS
    }
    assert first["token_framing_accounting_hash"] == _stable_hash(payload)
    assert specs == specs_before
    assert budget_record == budget_before


def test_t20_module_has_no_provider_network_online_tokenizer_dependency_or_final_manifest_side_effect():
    source = Path(context_token_framing.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"httpx", "requests", "socket", "smtplib", "tiktoken", "transformers", "openai", "anthropic"})
    assert "Final Context Manifest" not in source
    assert "provider request" not in source.lower()
    assert context_token_framing.FRAMING_SCOPE == "context_payload_only"


def test_t21_transient_materialization_helper_reuses_exact_canonical_frames(monkeypatch):
    specs = _specs(unicode_body=True)
    core, calls = _install(monkeypatch, specs, targets=("prd:1",))
    materialized = context_token_framing._materialize_context_payload_transient(
        model_call_id=7,
        budget_record={"caller": "opaque-to-this-layer"},
    )
    expected = b"\n".join(
        [
            _frame_bytes(core, "profile", specs["profile"], 1),
            _frame_bytes(core, "prd:1", specs["prd:1"], 2),
        ]
    )
    assert materialized["payload"] == expected
    assert materialized["framed_payload_hash"] == hashlib.sha256(expected).hexdigest()
    assert materialized["framed_payload_utf8_bytes"] == len(expected)
    assert calls["core"] == 1
    assert calls["admission"] == ["profile", "prd:1"]
    assert calls["redaction"] == ["profile", "prd:1"]
