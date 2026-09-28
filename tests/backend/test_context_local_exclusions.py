"""Local target exclusions must not discard unrelated, trustworthy source context."""

from copy import deepcopy
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import context_resolver, model_send_admission
from app import final_context_manifest, model_gateway, model_provider_gateway
import test_context_candidate_set as candidates
import test_context_redaction_inputs as inputs
import test_context_token_framing as framing
import test_final_context_manifest as manifest_tests
import test_model_gateway as gateway_tests
import test_model_provider_gateway as provider_tests


def _source_target():
    item = manifest_tests._admitted_target(ordinal=1, target="git:safe")
    item["target_type"] = "git_file_fact"
    return item


def test_mixed_source_and_denied_target_can_continue_with_visible_partial_coverage(monkeypatch):
    accounting = manifest_tests._accounting(
        admitted=[_source_target()], denied=[manifest_tests._denied_target()]
    )
    accounting["coverage_state"] = "partial_fail_visible"
    accounting["token_framing_accounting_hash"] = manifest_tests._stable_hash(
        {key: accounting[key] for key in manifest_tests.ACCOUNTING_KEYS[:-1]}
    )
    result, _ = manifest_tests._build(monkeypatch, accounting=accounting)
    assert result["local_request_readiness_state"] == "ready_for_gateway_evaluation"
    assert result["denied_target_count"] == 1
    assert result["coverage_state"] == "partial_fail_visible"


@pytest.mark.parametrize("source", [False, True])
def test_exclusion_never_bypasses_source_sufficiency_or_budget(monkeypatch, source):
    accounting = manifest_tests._accounting(
        admitted=[_source_target() if source else manifest_tests._admitted_target()],
        denied=[manifest_tests._denied_target()], fit=False,
    )
    accounting["coverage_state"] = "partial_fail_visible"
    accounting["token_framing_accounting_hash"] = manifest_tests._stable_hash(
        {key: accounting[key] for key in manifest_tests.ACCOUNTING_KEYS[:-1]}
    )
    result, _ = manifest_tests._build(monkeypatch, accounting=accounting)
    assert result["local_request_readiness_state"] == (
        "blocked_context_budget" if source else "blocked_context_denied"
    )


@pytest.mark.parametrize("gateway", [model_gateway, model_provider_gateway])
def test_both_gateway_closures_accept_locally_isolated_targets(gateway):
    value = gateway_tests._manifest() if gateway is model_gateway else provider_tests._manifest()
    source = _source_target()
    value.update(admitted_targets=[source], denied_targets=[manifest_tests._denied_target()],
                 denied_target_count=1, context_admission_state="contains_denied_targets",
                 coverage_state="partial_fail_visible")
    value["final_context_manifest_hash"] = gateway._stable_hash(
        {field: value[field] for field in gateway._FINAL_MANIFEST_HASH_KEYS}
    )
    assert gateway._validate_manifest(value, model_call_id=value["model_call_id"])["local_request_readiness_state"] == "ready_for_gateway_evaluation"


def test_framing_exclusion_is_accounted_and_body_never_materialized(monkeypatch):
    specs = framing._specs()
    specs["git:safe"] = deepcopy(specs["git:1"])
    specs["git:safe"]["source_identity"] = framing._source_identity("git:safe", "git_file_fact")
    specs["git:1"].update(state="denied", body="secret-canary-never-framed")
    _, calls = framing._install(monkeypatch, specs, targets=("git:safe", "git:1"))
    result = framing._run()
    assert result["coverage_state"] == "partial_fail_visible"
    assert calls["redaction"] == ["profile", "git:safe"]


def test_sensitive_path_is_never_read_and_keeps_nonbinary_identity(tmp_path, monkeypatch):
    original_write = candidates._write
    def write(repo, path, data):
        return original_write(repo, ".env" if path == "src/a.txt" else path, data)
    monkeypatch.setattr(candidates, "_write", write)
    state = candidates._make_state(tmp_path, monkeypatch)
    original_read = context_resolver._read_exact_diff
    reads = []
    def read(client, access, snapshot, path):
        assert path != ".env", "sensitive target body must not be read"
        reads.append(path)
        return original_read(client, access, snapshot, path)
    monkeypatch.setattr(context_resolver, "_read_exact_diff", read)
    candidate = context_resolver.build_context_candidate_set(state["snapshot_id"])
    excluded = next(item for item in candidate["items"] if item.get("path") == ".env")
    assert excluded["is_binary"] is False
    assert excluded["content_kind"] == "sensitive_path_metadata_only"
    assert excluded["resolved_content_hash"] is None
    assert "src/b.txt" in reads
    prepared = inputs._prepare(state)
    resolved = inputs._resolve(prepared, excluded["evidence_id"])
    assert resolved["raw_body"] is None
    assert resolved["raw_body_kind"] == "sensitive_path_metadata_only"
    admission = model_send_admission.build_model_send_admission(
        model_call_id=prepared["model_call_id"], budget_record=inputs._budget_record(),
        target=excluded["evidence_id"],
    )
    assert admission["model_send_state"] == "denied"
    assert admission["matched_rule_id"] == "SP01"


def test_unclosed_private_key_is_quarantined_without_discarding_safe_source(tmp_path, monkeypatch):
    from app import context_token_framing, context_redaction
    original_write = candidates._write
    def write(repo, path, data):
        if path == "src/b.txt":
            data += b"\n-----BEGIN PRIVATE KEY-----\nLOCAL-SECRET-CANARY\n"
        return original_write(repo, path, data)
    monkeypatch.setattr(candidates, "_write", write)
    state = candidates._make_state(tmp_path, monkeypatch)
    prepared = inputs._prepare(state)
    candidate = context_resolver.build_context_candidate_set(state["snapshot_id"])
    target = next(item["evidence_id"] for item in candidate["items"] if item.get("path") == "src/b.txt")
    kwargs = dict(model_call_id=prepared["model_call_id"], budget_record=inputs._budget_record(counting_policy_version="utf8_byte_upper_bound_v1"))
    result = context_redaction.build_context_redaction_result(**kwargs, target=target)
    assert result["redaction_state"] == "quarantined"
    assert "LOCAL-SECRET-CANARY" not in str(result)
    admission = model_send_admission.build_model_send_admission(**kwargs, target=target)
    assert admission["model_send_state"] == "denied"
    assert admission["admission_reason"] == "credential_boundary_quarantined"
    manifest = final_context_manifest.build_final_context_manifest(**kwargs)
    assert manifest["local_request_readiness_state"] == "ready_for_gateway_evaluation"
    assert manifest["coverage_state"] == "partial_fail_visible"
    assert any(item["target"] == target for item in manifest["denied_targets"])
    payload = context_token_framing._materialize_context_payload_transient(**kwargs)["payload"]
    assert b"LOCAL-SECRET-CANARY" not in payload




def test_only_profile_and_exclusions_remain_blocked_even_with_available_budget(monkeypatch):
    accounting = manifest_tests._accounting(
        admitted=[manifest_tests._admitted_target()],
        denied=[manifest_tests._denied_target()], fit=True,
    )
    result, _ = manifest_tests._build(monkeypatch, accounting=accounting)
    assert result["local_request_readiness_state"] == "blocked_context_denied"


@pytest.mark.parametrize('body, expected', [
    ('@@ -0,0 +1 @@\n+-----BEGIN PRIVATE KEY-----\n+[REDACTED:PRIVATE_KEY]\n+-----END PRIVATE KEY-----\n', False),
    ('diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+[REDACTED:PRIVATE_KEY]\n', False),
    ('@@ -0,0 +1 @@\n+api_key = "[REDACTED:SECRET_ASSIGNMENT]"\n', False),
    ('@@ -0,0 +1 @@\n+{\n+}\n', False),
    ('@@ -0,0 +1 @@\n+api_key = "[REDACTED:SECRET_ASSIGNMENT]"\n+def render():\n+    return "hello"\n', True),
    ('@@ -0,0 +1 @@\n+.password:focus {\n+  color: red;\n+}\n', True),
])
def test_diff_framing_and_placeholder_only_content_are_not_source(body, expected):
    assert model_send_admission._has_analyzable_diff_body(body) is expected
