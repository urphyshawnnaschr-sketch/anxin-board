"""Sensitive-Path Policy Identity V1: formal-chain, identity and safety tests."""

from __future__ import annotations

import ast
import builtins
from copy import deepcopy
import glob
import hashlib
import inspect
import json
import os
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
    context_redaction,
    context_sensitive_path_policy,
)
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_context_redaction as redaction_tests  # noqa: E402

EXPECTED_RULES = [
    {"rule_id": "SP01", "rule_type": "exact_basename", "value": ".env"},
    {"rule_id": "SP02", "rule_type": "exact_basename", "value": ".env.local"},
    {"rule_id": "SP03", "rule_type": "exact_basename", "value": ".env.development.local"},
    {"rule_id": "SP04", "rule_type": "exact_basename", "value": ".env.test.local"},
    {"rule_id": "SP05", "rule_type": "exact_basename", "value": ".env.production.local"},
    {"rule_id": "SP06", "rule_type": "exact_basename", "value": ".git-credentials"},
    {"rule_id": "SP07", "rule_type": "exact_basename", "value": ".netrc"},
    {"rule_id": "SP08", "rule_type": "exact_basename", "value": "_netrc"},
    {"rule_id": "SP09", "rule_type": "exact_basename", "value": ".npmrc"},
    {"rule_id": "SP10", "rule_type": "exact_basename", "value": ".pypirc"},
    {"rule_id": "SP11", "rule_type": "exact_basename", "value": "id_rsa"},
    {"rule_id": "SP12", "rule_type": "exact_basename", "value": "id_dsa"},
    {"rule_id": "SP13", "rule_type": "exact_basename", "value": "id_ecdsa"},
    {"rule_id": "SP14", "rule_type": "exact_basename", "value": "id_ed25519"},
    {"rule_id": "SP15", "rule_type": "exact_basename", "value": "credentials.json"},
    {"rule_id": "SP16", "rule_type": "exact_basename", "value": "secrets.json"},
    {"rule_id": "SP17", "rule_type": "exact_basename", "value": "secrets.yaml"},
    {"rule_id": "SP18", "rule_type": "exact_basename", "value": "secrets.yml"},
    {"rule_id": "SP19", "rule_type": "exact_basename", "value": "secrets.toml"},
    {"rule_id": "SP20", "rule_type": "basename_suffix", "value": ".key"},
    {"rule_id": "SP21", "rule_type": "basename_suffix", "value": ".p12"},
    {"rule_id": "SP22", "rule_type": "basename_suffix", "value": ".pfx"},
    {"rule_id": "SP23", "rule_type": "basename_suffix", "value": ".jks"},
    {"rule_id": "SP24", "rule_type": "basename_suffix", "value": ".keystore"},
    {"rule_id": "SP25", "rule_type": "basename_suffix", "value": ".tfstate"},
    {"rule_id": "SP26", "rule_type": "basename_suffix", "value": ".tfstate.backup"},
]
EXPECTED_RULE_ORDER = [f"SP{index:02d}" for index in range(1, 27)]
EXPECTED_POLICY_DESCRIPTOR = {
    "policy_schema_version": "sensitive_path_policy_v1",
    "policy_id": "builtin_sensitive_path_v1",
    "policy_version": "1.0",
    "policy_origin": "governance_frozen_builtin",
    "path_namespace": "historical_git_repository_relative_path",
    "path_subject_identity_version": "historical-git-path-identity-1.0",
    "match_semantics_id": "ascii-ci-git-path-exact-basename-suffix-v1",
    "rule_schema_version": "sensitive-path-rule-v1",
    "rules": EXPECTED_RULES,
    "rule_order": EXPECTED_RULE_ORDER,
}
EXPECTED_RESULT_KEYS = (
    "schema_version",
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
    "model_send_state",
    "unsupported_context_sources",
    "sensitive_path_policy_identity_hash",
)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    return candidate_tests._make_state(tmp_path, monkeypatch)


def _prepare(state: dict, *, call_prepare_key: str = "sensitive-path-identity-prepare-1"):
    return redaction_tests._prepare(state, call_prepare_key=call_prepare_key)


def _budget_record(**overrides) -> dict[str, object]:
    return redaction_tests._budget_record(**overrides)


def _candidate(state: dict) -> dict:
    return redaction_tests._candidate(state)


def _item(state: dict, *, item_type: str, binary: bool | None = None) -> dict:
    return redaction_tests._item(state, item_type=item_type, binary=binary)


def _resolve(prepared: dict, target: str, record: dict[str, object] | None = None):
    return context_sensitive_path_policy.build_sensitive_path_policy_identity(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record() if record is None else record,
        target=target,
    )


def _redaction(prepared: dict, target: str, record: dict[str, object] | None = None):
    return context_redaction.build_context_redaction_result(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record() if record is None else record,
        target=target,
    )


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _assert_code(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected
    return caught


def _canonical_hash(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _redaction_result_hash(value: dict[str, object]) -> str:
    payload = {
        key: deepcopy(item)
        for key, item in value.items()
        if key not in {"redacted_body", "redaction_result_hash"}
    }
    return _canonical_hash(payload)


def _identity_hash_payload(result: dict[str, object]) -> dict[str, object]:
    return {
        key: deepcopy(value)
        for key, value in result.items()
        if key != "sensitive_path_policy_identity_hash"
    }


def _ascii_ci(value: str) -> str:
    return "".join(chr(ord(char) + 32) if "A" <= char <= "Z" else char for char in value)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _matches_rule(path: str, rule: dict[str, str]) -> bool:
    rule_type = rule["rule_type"]
    expected = _ascii_ci(rule["value"])
    if rule_type == "exact_relative_path":
        return _ascii_ci(path) == expected
    basename = _ascii_ci(_basename(path))
    if rule_type == "exact_basename":
        return basename == expected
    if rule_type == "basename_suffix":
        return basename.endswith(expected)
    raise AssertionError(f"unexpected rule type: {rule_type}")


def _first_expected_match(path: str) -> str | None:
    by_id = {rule["rule_id"]: rule for rule in EXPECTED_RULES}
    for rule_id in EXPECTED_RULE_ORDER:
        if _matches_rule(path, by_id[rule_id]):
            return rule_id
    return None


def test_t01_real_formal_chain_calls_redaction_transform_exactly_once(state, monkeypatch):
    prepared = _prepare(state)
    original = context_sensitive_path_policy.build_context_redaction_result
    calls = 0

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(context_sensitive_path_policy, "build_context_redaction_result", counted)
    result = _resolve(prepared, "profile")
    assert calls == 1
    assert result["target_type"] == "profile"
    source = inspect.getsource(context_sensitive_path_policy)
    for forbidden in (
        "build_context_manifest_core",
        "build_context_candidate_set",
        "resolve_prd_block",
        "resolve_git_file_fact",
        "resolve_context_redaction_input",
    ):
        assert forbidden not in source


def test_t02_signature_blocks_path_policy_override_and_send_injection(state):
    prepared = _prepare(state)
    signature = inspect.signature(context_sensitive_path_policy.build_sensitive_path_policy_identity)
    assert list(signature.parameters) == ["model_call_id", "budget_record", "target"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    for name, value in {
        "redaction_result": {},
        "path": ".env",
        "local_path": "C:/secret",
        "rules": [{"rule_id": "caller"}],
        "glob": "**/.env",
        "regex": ".*",
        "policy_descriptor": {},
        "policy_hash": "0" * 64,
        "override": True,
        "send_permission": True,
        "provider": "caller-provider",
    }.items():
        with pytest.raises(TypeError):
            context_sensitive_path_policy.build_sensitive_path_policy_identity(
                model_call_id=prepared["model_call_id"],
                budget_record=_budget_record(),
                target="profile",
                **{name: value},
            )


def test_t03_git_historical_path_identity_uses_exact_formal_string(state):
    prepared = _prepare(state)
    target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    upstream = _redaction(prepared, target)
    path = upstream["source_identity"]["path"]
    result = _resolve(prepared, target)
    assert result["target_type"] == "git_file_fact"
    assert result["source_identity"] == upstream["source_identity"]
    assert result["source_identity"] is not upstream["source_identity"]
    assert result["path_subject_state"] == "available"
    assert result["path_identity_hash"] == _canonical_hash(
        {
            "path_namespace": "historical_git_repository_relative_path",
            "path": path,
        }
    )
    ascii_lowered = "".join(
        chr(ord(char) + 32) if "A" <= char <= "Z" else char for char in path
    )
    if ascii_lowered != path:
        assert result["path_identity_hash"] != _canonical_hash(
            {
                "path_namespace": "historical_git_repository_relative_path",
                "path": ascii_lowered,
            }
        )


def test_t04_profile_path_subject_is_not_applicable_without_fallback(state):
    prepared = _prepare(state)
    upstream = _redaction(prepared, "profile")
    assert "path" not in upstream["source_identity"]
    result = _resolve(prepared, "profile")
    assert result["target_type"] == "profile"
    assert result["path_subject_state"] == "not_applicable"
    assert result["path_identity_hash"] is None


def test_t05_prd_path_subject_is_not_applicable_without_upload_path(state):
    prepared = _prepare(state)
    target = _item(state, item_type="prd_block")["evidence_id"]
    upstream = _redaction(prepared, target)
    assert "path" not in upstream["source_identity"]
    result = _resolve(prepared, target)
    assert result["target_type"] == "prd_block"
    assert result["path_subject_state"] == "not_applicable"
    assert result["path_identity_hash"] is None


def test_t06_upstream_identity_hash_state_stats_and_body_drift_fail_closed(state, monkeypatch):
    prepared = _prepare(state)
    original = context_sensitive_path_policy.build_context_redaction_result

    for field, replacement in (
        ("snapshot_hash", "f" * 64),
        ("model_send_state", "admitted"),
        ("redaction_policy_id", "caller-policy"),
    ):
        def drifted(field=field, replacement=replacement, **kwargs):
            value = deepcopy(original(**kwargs))
            value[field] = replacement
            return value

        with monkeypatch.context() as isolated:
            isolated.setattr(context_sensitive_path_policy, "build_context_redaction_result", drifted)
            _assert_code(
                "SENSITIVE_PATH_POLICY_UPSTREAM_INCONSISTENT",
                lambda: _resolve(prepared, "profile"),
            )

    def invalid_rule_stats(**kwargs):
        value = deepcopy(original(**kwargs))
        value["redaction_stats"] = {"R7": 1}
        value["redaction_match_count"] = 1
        value["redaction_result_hash"] = _redaction_result_hash(value)
        return value

    with monkeypatch.context() as isolated:
        isolated.setattr(
            context_sensitive_path_policy,
            "build_context_redaction_result",
            invalid_rule_stats,
        )
        _assert_code(
            "SENSITIVE_PATH_POLICY_UPSTREAM_INCONSISTENT",
            lambda: _resolve(prepared, "profile"),
        )

    prd_target = _item(state, item_type="prd_block")["evidence_id"]
    text_target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    body_cases = (
        (
            "profile",
            lambda body: {**dict(body), "_identity_review_drift": "profile"},
        ),
        (
            prd_target,
            lambda body: {**dict(body), "_identity_review_drift": "prd"},
        ),
        (
            text_target,
            lambda body: body + "\n# identity-review-drift",
        ),
    )
    for target, mutate_body in body_cases:
        def drifted_body(expected_target=target, mutate_body=mutate_body, **kwargs):
            value = deepcopy(original(**kwargs))
            assert value["target"] == expected_target
            original_body_hash = value["redacted_body_hash"]
            original_result_hash = value["redaction_result_hash"]
            value["redacted_body"] = mutate_body(value["redacted_body"])
            assert value["redacted_body_hash"] == original_body_hash
            assert value["redaction_result_hash"] == original_result_hash
            return value

        with monkeypatch.context() as isolated:
            isolated.setattr(
                context_sensitive_path_policy,
                "build_context_redaction_result",
                drifted_body,
            )
            _assert_code(
                "SENSITIVE_PATH_POLICY_UPSTREAM_INCONSISTENT",
                lambda target=target: _resolve(prepared, target),
            )


def test_t07_policy_descriptor_is_independently_exact_closed_and_rejected_on_shape_drift(monkeypatch):
    production = context_sensitive_path_policy.POLICY_DESCRIPTOR
    assert tuple(production) == tuple(EXPECTED_POLICY_DESCRIPTOR)
    assert production == EXPECTED_POLICY_DESCRIPTOR
    assert len(production) == 10
    assert len(production["rules"]) == 26
    assert all(tuple(rule) == ("rule_id", "rule_type", "value") for rule in production["rules"])
    assert context_sensitive_path_policy.SENSITIVE_PATH_POLICY_HASH == _canonical_hash(
        EXPECTED_POLICY_DESCRIPTOR
    )

    variants = []
    extended = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    extended["unknown_extension"] = True
    variants.append(extended)
    missing = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    missing.pop("rule_order")
    variants.append(missing)
    rule_extended = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    rule_extended["rules"][0]["extension"] = "no"
    variants.append(rule_extended)
    rule_missing = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    rule_missing["rules"][0].pop("value")
    variants.append(rule_missing)

    for variant in variants:
        with monkeypatch.context() as isolated:
            isolated.setattr(context_sensitive_path_policy, "POLICY_DESCRIPTOR", variant)
            _assert_code(
                "SENSITIVE_PATH_POLICY_INTERNAL_POLICY_INCONSISTENT",
                context_sensitive_path_policy._validate_internal_policy,
            )


def test_t08_policy_hash_binds_rule_type_value_order_and_all_identity_versions(monkeypatch):
    variants = []
    for key in (
        "policy_id",
        "policy_version",
        "match_semantics_id",
        "rule_schema_version",
    ):
        variant = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
        variant[key] = str(variant[key]) + "-changed"
        variants.append(variant)

    rule_value = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    rule_value["rules"][0]["value"] = ".env.changed"
    variants.append(rule_value)
    rule_type = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    rule_type["rules"][0]["rule_type"] = "exact_relative_path"
    variants.append(rule_type)
    rule_order = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    rule_order["rule_order"] = list(reversed(rule_order["rule_order"]))
    variants.append(rule_order)
    physical_order = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    physical_order["rules"] = list(reversed(physical_order["rules"]))
    variants.append(physical_order)

    expected_hash = _canonical_hash(EXPECTED_POLICY_DESCRIPTOR)
    for variant in variants:
        variant_hash = _canonical_hash(variant)
        assert variant_hash != expected_hash
        with monkeypatch.context() as isolated:
            isolated.setattr(context_sensitive_path_policy, "POLICY_DESCRIPTOR", variant)
            isolated.setattr(context_sensitive_path_policy, "SENSITIVE_PATH_POLICY_HASH", variant_hash)
            _assert_code(
                "SENSITIVE_PATH_POLICY_INTERNAL_POLICY_INCONSISTENT",
                context_sensitive_path_policy._validate_internal_policy,
            )


def test_t09_fixed_catalog_and_normative_ascii_ci_matching_matrix_are_closed():
    assert EXPECTED_POLICY_DESCRIPTOR["rules"] == EXPECTED_RULES
    assert EXPECTED_POLICY_DESCRIPTOR["rule_order"] == EXPECTED_RULE_ORDER
    assert context_sensitive_path_policy.POLICY_DESCRIPTOR == EXPECTED_POLICY_DESCRIPTOR

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
        assert _first_expected_match(path) == expected_rule

    for path in (
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
    ):
        assert _first_expected_match(path) is None

    assert _basename("dir/secrets.json") == "secrets.json"
    assert _basename(r"dir\secrets.json") == r"dir\secrets.json"
    assert _ascii_ci("ABC-Ä-İ") == "abc-Ä-İ"
    assert not _matches_rule(
        "safe/../secret.txt",
        {"rule_id": "X", "rule_type": "exact_relative_path", "value": "secret.txt"},
    )
    assert _matches_rule(
        "Config/.ENV",
        {"rule_id": "X", "rule_type": "exact_relative_path", "value": "config/.env"},
    )
    assert "exact_relative_path" in context_sensitive_path_policy._ALLOWED_RULE_TYPES
    assert not any(rule["rule_type"] == "exact_relative_path" for rule in EXPECTED_RULES)


def test_t10_identity_layer_adds_no_filesystem_resolution_after_formal_upstream(state, monkeypatch):
    prepared = _prepare(state)
    frozen = _redaction(prepared, "profile")

    def bomb(name):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"identity layer attempted filesystem operation: {name}")
        return fail

    with monkeypatch.context() as isolated:
        isolated.setattr(
            context_sensitive_path_policy,
            "build_context_redaction_result",
            lambda **_kwargs: deepcopy(frozen),
        )
        isolated.setattr(builtins, "open", bomb("open"))
        isolated.setattr(os, "stat", bomb("os.stat"))
        isolated.setattr(os, "listdir", bomb("os.listdir"))
        isolated.setattr(os, "scandir", bomb("os.scandir"))
        isolated.setattr(os, "readlink", bomb("os.readlink"))
        isolated.setattr(os.path, "realpath", bomb("os.path.realpath"))
        isolated.setattr(glob, "glob", bomb("glob.glob"))
        isolated.setattr(glob, "iglob", bomb("glob.iglob"))
        isolated.setattr(Path, "open", bomb("Path.open"))
        isolated.setattr(Path, "read_text", bomb("Path.read_text"))
        isolated.setattr(Path, "read_bytes", bomb("Path.read_bytes"))
        isolated.setattr(Path, "stat", bomb("Path.stat"))
        isolated.setattr(Path, "lstat", bomb("Path.lstat"))
        isolated.setattr(Path, "iterdir", bomb("Path.iterdir"))
        isolated.setattr(Path, "glob", bomb("Path.glob"))
        isolated.setattr(Path, "rglob", bomb("Path.rglob"))
        isolated.setattr(Path, "resolve", bomb("Path.resolve"))
        result = _resolve(prepared, "profile")

    assert result["path_subject_state"] == "not_applicable"
    tree = ast.parse(inspect.getsource(context_sensitive_path_policy))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    )
    assert not (imported_modules & {"os", "pathlib", "glob", "shutil"})


def test_t11_policy_identity_does_not_read_config_environment_gitignore_or_override(state, monkeypatch):
    prepared = _prepare(state)
    frozen = _redaction(prepared, "profile")
    monkeypatch.setattr(
        context_sensitive_path_policy,
        "build_context_redaction_result",
        lambda **_kwargs: deepcopy(frozen),
    )
    monkeypatch.setenv("SENSITIVE_PATHS", "caller/.env")
    first = _resolve(prepared, "profile")
    monkeypatch.setenv("SENSITIVE_PATHS", "caller/public.pem")
    second = _resolve(prepared, "profile")
    assert first == second
    assert first["sensitive_path_policy_hash"] == _canonical_hash(EXPECTED_POLICY_DESCRIPTOR)
    source = inspect.getsource(context_sensitive_path_policy)
    for forbidden in (
        "os.environ",
        "os.getenv",
        "gitignore",
        "dotenv",
        "project_config",
        "sensitive_paths",
        "allowlist",
    ):
        assert forbidden not in source


def test_t12_identity_result_never_preempts_evaluation_admission_or_provider(state):
    prepared = _prepare(state)
    result = _resolve(prepared, "profile")
    assert result["policy_evaluation_state"] == "not_evaluated"
    assert result["model_send_state"] == "not_admitted"
    for forbidden_key in (
        "matched",
        "not_matched",
        "matched_rule_id",
        "safe",
        "sensitive",
        "allow",
        "deny",
        "authorized",
        "qualified",
        "admitted",
        "provider_decision",
    ):
        assert forbidden_key not in result

    tree = ast.parse(inspect.getsource(context_sensitive_path_policy))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    assert not any(
        token in name.casefold()
        for name in imported
        for token in ("provider", "smtp", "admission", "tokenizer", "final_manifest")
    )


def test_t13_exact_result_schema_is_independent_closed_and_hashable(state):
    prepared = _prepare(state)
    result = _resolve(prepared, "profile")
    assert tuple(result) == EXPECTED_RESULT_KEYS
    assert context_sensitive_path_policy._RESULT_KEYS == EXPECTED_RESULT_KEYS
    assert set(result) == set(EXPECTED_RESULT_KEYS)
    assert result["schema_version"] == "sensitive_path_policy_identity_result_v1"
    assert result["sensitive_path_policy_id"] == "builtin_sensitive_path_v1"
    assert result["sensitive_path_policy_identity_hash"] == _canonical_hash(
        _identity_hash_payload(result)
    )
    extended = deepcopy(result)
    extended["extension"] = "not-allowed"
    assert set(extended) != set(EXPECTED_RESULT_KEYS)


def test_t14_result_hash_binds_path_policy_upstream_target_and_states(state):
    prepared = _prepare(state)
    target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    result = _resolve(prepared, target)
    baseline_hash = result["sensitive_path_policy_identity_hash"]
    baseline_payload = _identity_hash_payload(result)
    assert baseline_hash == _canonical_hash(baseline_payload)

    for key, replacement in (
        ("redaction_result_hash", "f" * 64),
        ("target", str(result["target"]) + "-changed"),
        ("path_identity_hash", "e" * 64),
        ("sensitive_path_policy_hash", "d" * 64),
        ("policy_evaluation_state", "changed"),
        ("model_send_state", "changed"),
    ):
        variant = deepcopy(baseline_payload)
        variant[key] = replacement
        assert _canonical_hash(variant) != baseline_hash


def test_t15_success_and_failure_have_no_db_file_log_network_or_smtp_side_effects(
    state, monkeypatch, capsys
):
    prepared = _prepare(state)
    db_before = candidate_tests._db_dump(state["db_path"])
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
    assert result["model_send_state"] == "not_admitted"
    assert denied == [] and calls == []
    assert candidate_tests._db_dump(state["db_path"]) == db_before
    assert state["profile_content"]["project_summary"] not in captured.out
    assert state["profile_content"]["project_summary"] not in captured.err


def test_t16_real_chain_and_frozen_upstream_object_have_zero_mutation(state, monkeypatch):
    prepared = _prepare(state)
    record = _budget_record()
    target = _item(state, item_type="git_file_fact", binary=False)["evidence_id"]
    state_before = candidate_tests._state_signature(state)

    real_result = context_sensitive_path_policy.build_sensitive_path_policy_identity(
        model_call_id=prepared["model_call_id"],
        budget_record=deepcopy(record),
        target=target,
    )
    assert real_result["model_send_state"] == "not_admitted"
    assert candidate_tests._state_signature(state) == state_before

    upstream = context_redaction.build_context_redaction_result(
        model_call_id=prepared["model_call_id"],
        budget_record=deepcopy(record),
        target=target,
    )
    upstream_before = deepcopy(upstream)
    monkeypatch.setattr(
        context_sensitive_path_policy,
        "build_context_redaction_result",
        lambda **_kwargs: upstream,
    )
    object_result = context_sensitive_path_policy.build_sensitive_path_policy_identity(
        model_call_id=prepared["model_call_id"],
        budget_record=deepcopy(record),
        target=target,
    )
    assert object_result["redaction_result_hash"] == upstream["redaction_result_hash"]
    assert upstream == upstream_before
    assert candidate_tests._state_signature(state) == state_before


def test_t17_fixed_identity_does_not_turn_future_hard_deny_into_allow_or_override(state):
    assert _first_expected_match("config/.ENV") == "SP01"
    assert _first_expected_match("public.pem") is None
    assert context_sensitive_path_policy.POLICY_DESCRIPTOR == EXPECTED_POLICY_DESCRIPTOR
    prepared = _prepare(state)
    result = _resolve(prepared, "profile")
    assert result["policy_evaluation_state"] == "not_evaluated"
    assert result["model_send_state"] == "not_admitted"
    assert not any(key in result for key in ("allow", "deny", "matched_rule_id", "override"))
    function_names = {
        node.name
        for node in ast.walk(ast.parse(inspect.getsource(context_sensitive_path_policy)))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not any("match" in name or "admission" in name or "evaluate" in name for name in function_names)


def test_t18_profile_prd_text_and_binary_regressions_preserve_formal_redaction_identity(state):
    prepared = _prepare(state)
    targets = [
        "profile",
        _item(state, item_type="prd_block")["evidence_id"],
        _item(state, item_type="git_file_fact", binary=False)["evidence_id"],
        _item(state, item_type="git_file_fact", binary=True)["evidence_id"],
    ]
    for target in targets:
        upstream = _redaction(prepared, target)
        result = _resolve(prepared, target)
        assert result["redaction_result_hash"] == upstream["redaction_result_hash"]
        assert result["call_identity_hash"] == upstream["call_identity_hash"]
        assert result["snapshot_hash"] == upstream["snapshot_hash"]
        assert result["candidate_set_hash"] == upstream["candidate_set_hash"]
        assert result["source_identity"] == upstream["source_identity"]
        assert result["unsupported_context_sources"] == upstream["unsupported_context_sources"]
        assert result["policy_evaluation_state"] == "not_evaluated"
        assert result["model_send_state"] == "not_admitted"
        if upstream["target_type"] == "git_file_fact":
            assert result["path_subject_state"] == "available"
            assert isinstance(result["path_identity_hash"], str)
        else:
            assert result["path_subject_state"] == "not_applicable"
            assert result["path_identity_hash"] is None