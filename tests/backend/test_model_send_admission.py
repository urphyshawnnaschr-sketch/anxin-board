"""Model-Send Admission V1 targeted acceptance T01-T20."""

from __future__ import annotations

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

from app import context_sensitive_path_policy, model_send_admission  # noqa: E402
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_context_redaction as redaction_tests  # noqa: E402

EXPECTED_RULES = [
    ("SP01", "exact_basename", ".env"),
    ("SP02", "exact_basename", ".env.local"),
    ("SP03", "exact_basename", ".env.development.local"),
    ("SP04", "exact_basename", ".env.test.local"),
    ("SP05", "exact_basename", ".env.production.local"),
    ("SP06", "exact_basename", ".git-credentials"),
    ("SP07", "exact_basename", ".netrc"),
    ("SP08", "exact_basename", "_netrc"),
    ("SP09", "exact_basename", ".npmrc"),
    ("SP10", "exact_basename", ".pypirc"),
    ("SP11", "exact_basename", "id_rsa"),
    ("SP12", "exact_basename", "id_dsa"),
    ("SP13", "exact_basename", "id_ecdsa"),
    ("SP14", "exact_basename", "id_ed25519"),
    ("SP15", "exact_basename", "credentials.json"),
    ("SP16", "exact_basename", "secrets.json"),
    ("SP17", "exact_basename", "secrets.yaml"),
    ("SP18", "exact_basename", "secrets.yml"),
    ("SP19", "exact_basename", "secrets.toml"),
    ("SP20", "basename_suffix", ".key"),
    ("SP21", "basename_suffix", ".p12"),
    ("SP22", "basename_suffix", ".pfx"),
    ("SP23", "basename_suffix", ".jks"),
    ("SP24", "basename_suffix", ".keystore"),
    ("SP25", "basename_suffix", ".tfstate"),
    ("SP26", "basename_suffix", ".tfstate.backup"),
]
EXPECTED_RESULT_KEYS = (
    "schema_version",
    "sensitive_path_policy_identity_hash",
    "redaction_result_hash",
    "model_call_id",
    "call_identity_hash",
    "snapshot_id",
    "snapshot_hash",
    "project_id",
    "candidate_set_hash",
    "target",
    "target_type",
    "source_identity",
    "path_subject_state",
    "path_identity_hash",
    "sensitive_path_policy_id",
    "sensitive_path_policy_hash",
    "policy_evaluation_state",
    "sensitive_path_decision",
    "matched_rule_id",
    "model_send_state",
    "admission_reason",
    "unsupported_context_sources",
    "model_send_admission_hash",
)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    return candidate_tests._make_state(tmp_path, monkeypatch)


def _prepare(state: dict, *, key: str = "model-send-admission-prepare-1"):
    return redaction_tests._prepare(state, call_prepare_key=key)


def _budget_record(**overrides) -> dict[str, object]:
    return redaction_tests._budget_record(**overrides)


def _item(state: dict, *, item_type: str, binary: bool | None = None) -> dict:
    return redaction_tests._item(state, item_type=item_type, binary=binary)


def _resolve(prepared: dict, target: str):
    return model_send_admission.build_model_send_admission(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record(),
        target=target,
    )


def _identity(prepared: dict, target: str):
    return context_sensitive_path_policy.build_sensitive_path_policy_identity(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record(),
        target=target,
    )


def _canonical_hash(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rehash_identity(value: dict[str, object]) -> dict[str, object]:
    value["sensitive_path_policy_identity_hash"] = _canonical_hash(
        {
            key: deepcopy(item)
            for key, item in value.items()
            if key != "sensitive_path_policy_identity_hash"
        }
    )
    return value


def _with_path(value: dict[str, object], path: str) -> dict[str, object]:
    changed = deepcopy(value)
    changed["source_identity"]["path"] = path
    changed["path_identity_hash"] = _canonical_hash(
        {
            "path_namespace": "historical_git_repository_relative_path",
            "path": path,
        }
    )
    return _rehash_identity(changed)


def _synthetic_resolve(monkeypatch, prepared: dict, upstream: dict[str, object]):
    frozen = deepcopy(upstream)

    def supply(**kwargs):
        assert kwargs["model_call_id"] == prepared["model_call_id"]
        assert kwargs["target"] == frozen["target"]
        return frozen

    monkeypatch.setattr(model_send_admission, "build_sensitive_path_policy_identity", supply)
    return _resolve(prepared, frozen["target"]), frozen


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _assert_code(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected


def test_t01_t02_signature_and_formal_identity_exactly_once(state, monkeypatch):
    prepared = _prepare(state)
    signature = inspect.signature(model_send_admission.build_model_send_admission)
    assert list(signature.parameters) == ["model_call_id", "budget_record", "target"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())

    for name, value in {
        "upstream": {},
        "path": ".env",
        "rules": [],
        "override": True,
        "provider_authorization": True,
        "qualification": {},
        "token_count": 1,
        "final_manifest": {},
    }.items():
        with pytest.raises(TypeError):
            model_send_admission.build_model_send_admission(
                model_call_id=prepared["model_call_id"],
                budget_record=_budget_record(),
                target="profile",
                **{name: value},
            )

    original = model_send_admission.build_sensitive_path_policy_identity
    calls = 0

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(model_send_admission, "build_sensitive_path_policy_identity", counted)
    result = _resolve(prepared, "profile")
    assert calls == 1
    assert result["model_send_state"] == "admitted"


def test_t03_upstream_shape_hash_state_and_path_drift_fail_closed(state, monkeypatch):
    prepared = _prepare(state)
    original = _identity(prepared, "profile")
    cases = []

    changed = deepcopy(original)
    changed["model_send_state"] = "admitted"
    cases.append(changed)

    changed = deepcopy(original)
    changed["snapshot_hash"] = "f" * 64
    cases.append(changed)

    changed = deepcopy(original)
    changed["extra"] = "not-closed"
    cases.append(changed)

    for value in cases:
        with monkeypatch.context() as isolated:
            isolated.setattr(
                model_send_admission,
                "build_sensitive_path_policy_identity",
                lambda **_kwargs: deepcopy(value),
            )
            _assert_code(
                "MODEL_SEND_ADMISSION_UPSTREAM_INCONSISTENT",
                lambda: _resolve(prepared, "profile"),
            )

    text_target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    git_upstream = _identity(prepared, text_target)
    git_upstream["path_identity_hash"] = "0" * 64
    git_upstream = _rehash_identity(git_upstream)
    monkeypatch.setattr(
        model_send_admission,
        "build_sensitive_path_policy_identity",
        lambda **_kwargs: deepcopy(git_upstream),
    )
    _assert_code(
        "MODEL_SEND_ADMISSION_UPSTREAM_INCONSISTENT",
        lambda: _resolve(prepared, text_target),
    )


def test_t04_fixed_policy_descriptor_hash_and_order_drift_fail_closed(state, monkeypatch):
    prepared = _prepare(state)
    upstream = _identity(prepared, "profile")
    monkeypatch.setattr(
        model_send_admission,
        "build_sensitive_path_policy_identity",
        lambda **_kwargs: deepcopy(upstream),
    )

    drifted = deepcopy(model_send_admission.POLICY_DESCRIPTOR)
    drifted["rules"][0]["value"] = ".env-drift"
    monkeypatch.setattr(model_send_admission, "POLICY_DESCRIPTOR", drifted)
    _assert_code(
        "MODEL_SEND_ADMISSION_INTERNAL_POLICY_INCONSISTENT",
        lambda: _resolve(prepared, "profile"),
    )


def test_t05_t06_t07_t08_independent_lexical_matrix_and_determinism(state, monkeypatch):
    prepared = _prepare(state)
    target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    base = _identity(prepared, target)

    positives = {
        ".env": "SP01",
        "config/.ENV": "SP01",
        "foo/.env.production.local": "SP05",
        "ID_RSA": "SP11",
        "keys/server.KEY": "SP20",
        "certs/client.P12": "SP21",
        "terraform/state.tfstate": "SP25",
        "terraform/state.tfstate.backup": "SP26",
        "secrets.JSON": "SP16",
    }
    for path, expected_rule in positives.items():
        with monkeypatch.context() as isolated:
            result, _ = _synthetic_resolve(isolated, prepared, _with_path(base, path))
            assert result["policy_evaluation_state"] == "evaluated"
            assert result["sensitive_path_decision"] == "hard_deny"
            assert result["matched_rule_id"] == expected_rule
            assert result["model_send_state"] == "denied"
            assert result["admission_reason"] == "sensitive_path_hard_deny"
            repeated, _ = _synthetic_resolve(isolated, prepared, _with_path(base, path))
            assert repeated["matched_rule_id"] == expected_rule

    negatives = (
        ".env.example",
        "public.pem",
        "certificate.PEM",
        "secrets-prod.json",
        "credentials-prod.json",
        "config.yaml",
        "monkey",
        "mykey",
        r"dir\secrets.json",
        "İD_RSA",
        "a/../.env.example",
    )
    for path in negatives:
        with monkeypatch.context() as isolated:
            result, _ = _synthetic_resolve(isolated, prepared, _with_path(base, path))
            assert result["sensitive_path_decision"] == "not_blocked"
            assert result["matched_rule_id"] is None
            assert result["model_send_state"] == "admitted"

    assert [row[0] for row in EXPECTED_RULES] == [f"SP{i:02d}" for i in range(1, 27)]


def test_t09_t10_profile_and_prd_are_path_na_and_admitted(state):
    prepared = _prepare(state)
    profile = _resolve(prepared, "profile")
    assert profile["target_type"] == "profile"
    assert profile["path_subject_state"] == "not_applicable"
    assert profile["path_identity_hash"] is None
    assert profile["policy_evaluation_state"] == "not_applicable"
    assert profile["sensitive_path_decision"] == "not_applicable"
    assert profile["matched_rule_id"] is None
    assert profile["model_send_state"] == "admitted"

    prd_target = _item(state, item_type="prd_block")["evidence_id"]
    prd = _resolve(prepared, prd_target)
    assert prd["target_type"] == "prd_block"
    assert prd["path_subject_state"] == "not_applicable"
    assert prd["model_send_state"] == "admitted"
    assert prd["admission_reason"] == "content_policy_passed"


def test_t11_t12_non_sensitive_git_text_admitted_sensitive_text_denied(state, monkeypatch):
    prepared = _prepare(state)
    target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    base = _identity(prepared, target)

    admitted, _ = _synthetic_resolve(monkeypatch, prepared, _with_path(base, "src/service.py"))
    assert admitted["sensitive_path_decision"] == "not_blocked"
    assert admitted["model_send_state"] == "admitted"
    assert admitted["admission_reason"] == "content_policy_passed"

    with monkeypatch.context() as isolated:
        denied, _ = _synthetic_resolve(isolated, prepared, _with_path(base, "config/.env"))
        assert denied["sensitive_path_decision"] == "hard_deny"
        assert denied["matched_rule_id"] == "SP01"
        assert denied["model_send_state"] == "denied"


def test_t13_t14_git_binary_denied_and_sensitive_path_has_priority(state, monkeypatch):
    prepared = _prepare(state)
    target = _item(state, item_type="git_file_fact", binary=True)["evidence_id"]
    base = _identity(prepared, target)

    ordinary, _ = _synthetic_resolve(monkeypatch, prepared, _with_path(base, "assets/logo.bin"))
    assert ordinary["sensitive_path_decision"] == "not_blocked"
    assert ordinary["matched_rule_id"] is None
    assert ordinary["model_send_state"] == "denied"
    assert ordinary["admission_reason"] == "binary_metadata_only_no_sendable_body"

    with monkeypatch.context() as isolated:
        sensitive, _ = _synthetic_resolve(isolated, prepared, _with_path(base, "keys/device.p12"))
        assert sensitive["sensitive_path_decision"] == "hard_deny"
        assert sensitive["matched_rule_id"] == "SP21"
        assert sensitive["admission_reason"] == "sensitive_path_hard_deny"


def test_t15_unsupported_context_sources_remain_fail_visible(state):
    prepared = _prepare(state)
    upstream = _identity(prepared, "profile")
    result = _resolve(prepared, "profile")
    assert result["unsupported_context_sources"] == upstream["unsupported_context_sources"]
    assert result["unsupported_context_sources"] is not upstream["unsupported_context_sources"]


def test_t16_no_filesystem_db_network_provider_or_transport_side_effect(state, monkeypatch):
    prepared = _prepare(state)
    upstream = _identity(prepared, "profile")
    monkeypatch.setattr(
        model_send_admission,
        "build_sensitive_path_policy_identity",
        lambda **_kwargs: deepcopy(upstream),
    )

    redaction = model_send_admission.build_context_redaction_result(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record(), target="profile",
    )
    monkeypatch.setattr(model_send_admission, "build_context_redaction_result", lambda **_kwargs: deepcopy(redaction))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden side effect")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(httpx, "request", forbidden)

    result = _resolve(prepared, "profile")
    assert result["model_send_state"] == "admitted"
    source = inspect.getsource(model_send_admission)
    for forbidden_name in (
        "requests.",
        "httpx.",
        "sqlite3.",
        "smtplib.",
        "socket.",
        "Path(",
        "tokenizer",
        "provider_request_id",
    ):
        assert forbidden_name not in source


def test_t17_exact_result_schema_and_independent_hash_recompute(state):
    prepared = _prepare(state)
    result = _resolve(prepared, "profile")
    assert tuple(result) == EXPECTED_RESULT_KEYS
    payload = {key: deepcopy(value) for key, value in result.items() if key != "model_send_admission_hash"}
    assert result["model_send_admission_hash"] == _canonical_hash(payload)


def test_t18_admit_and_deny_do_not_return_body_secret_or_provider_fields(state, monkeypatch):
    prepared = _prepare(state)
    admitted = _resolve(prepared, "profile")
    target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    denied, _ = _synthetic_resolve(monkeypatch, prepared, _with_path(_identity(prepared, target), ".env"))

    forbidden_keys = {
        "raw_body",
        "redacted_body",
        "credential",
        "secret",
        "path_excerpt",
        "token_count",
        "provider_authorization",
        "qualification",
        "provider_request_id",
        "network_result",
    }
    for result in (admitted, denied):
        assert not forbidden_keys.intersection(result)


def test_t19_upstream_result_is_not_mutated(state, monkeypatch):
    prepared = _prepare(state)
    upstream = _identity(prepared, "profile")
    before = deepcopy(upstream)
    result, supplied = _synthetic_resolve(monkeypatch, prepared, upstream)
    assert supplied == before
    assert upstream == before
    assert result["source_identity"] == upstream["source_identity"]
    assert result["source_identity"] is not upstream["source_identity"]


def test_t20_formal_chain_regression_profile_prd_git_text_and_binary(state):
    prepared = _prepare(state)
    profile = _resolve(prepared, "profile")
    prd = _resolve(prepared, _item(state, item_type="prd_block")["evidence_id"])
    git_text = _resolve(prepared, _item(state, item_type="git_file_fact", binary=False)["evidence_id"])
    git_binary = _resolve(prepared, _item(state, item_type="git_file_fact", binary=True)["evidence_id"])

    assert profile["model_send_state"] == "admitted"
    assert prd["model_send_state"] == "admitted"
    assert git_text["model_send_state"] in {"admitted", "denied"}
    if git_text["sensitive_path_decision"] == "not_blocked":
        assert git_text["model_send_state"] == "admitted"
    else:
        assert git_text["admission_reason"] == "sensitive_path_hard_deny"
    assert git_binary["model_send_state"] == "denied"
