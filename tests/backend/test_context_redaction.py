"""Redaction Policy / Transform V1: formal-chain, deterministic grammar and safety tests."""

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
    context_redaction,
    context_redaction_inputs,
    context_resolver,
    model_budget_profiles,
    model_call_ledger,
)
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_context_redaction_inputs as input_tests  # noqa: E402

SYNTHETIC = "SYNTHETIC_CREDENTIAL_123456"
INLINE = "[REDACTED:CREDENTIAL]"
PRIVATE = "[REDACTED:PRIVATE_KEY_MATERIAL]"

EXPECTED_POLICY_DESCRIPTOR = {
    "policy_schema_version": "context_redaction_policy_v1",
    "policy_id": "credential_redaction_v1",
    "policy_algorithm_version": "credential-redaction-algorithm-1.1",
    "inline_placeholder": INLINE,
    "private_key_placeholder": PRIVATE,
    "key_normalization_id": "ascii-secret-key-normalization-v1",
    "header_grammar_id": "single-line-sensitive-header-v1",
    "text_assignment_grammar_id": "single-line-secret-assignment-v1",
    "url_grammar_id": "explicit-scheme-url-token-v1",
    "url_query_grammar_id": "raw-query-no-percent-decode-v1",
    "auth_token_grammar_id": "bearer-basic-token-boundary-v1",
    "private_key_grammar_id": "exact-line-private-key-block-v1",
    "secret_key_exact": [
        "password", "passwd", "pwd", "passphrase", "secret", "client_secret", "api_key",
        "apikey", "access_token", "refresh_token", "id_token", "auth_token", "token",
        "authorization", "proxy_authorization", "cookie", "set_cookie", "private_key",
        "private_key_password", "secret_access_key", "smtp_password",
    ],
    "secret_key_suffixes": [
        "_password", "_passwd", "_passphrase", "_secret", "_client_secret", "_api_key",
        "_access_token", "_refresh_token", "_auth_token", "_private_key",
        "_private_key_password", "_secret_access_key",
    ],
    "header_names": ["Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie"],
    "auth_schemes": ["Bearer", "Basic"],
    "private_key_block_labels": [
        "PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY",
        "DSA PRIVATE KEY", "PGP PRIVATE KEY BLOCK",
    ],
    "rule_order": [
        "R1 private_key_block", "R2 sensitive_header_value", "R3 secret_key_scalar",
        "R4 url_userinfo", "R5 url_secret_query", "R6 auth_scheme_token",
    ],
}

EXPECTED_RESULT_KEYS = (
    "schema_version", "redaction_input_hash", "manifest_core_hash", "model_call_id",
    "call_identity_hash", "snapshot_id", "snapshot_hash", "project_id",
    "candidate_set_hash", "target", "target_type", "source_identity", "raw_body_kind",
    "redaction_policy_id", "redaction_policy_hash", "redaction_state",
    "redacted_body_state", "redacted_body", "redacted_body_hash", "redaction_stats",
    "redaction_match_count", "model_send_state", "unsupported_context_sources",
    "redaction_result_hash",
)


@pytest.fixture()
def redaction_state(tmp_path, monkeypatch):
    return candidate_tests._make_state(tmp_path, monkeypatch)


@pytest.fixture()
def credential_state(tmp_path, monkeypatch):
    """Build real frozen Profile/PRD/Git sources containing only synthetic credentials."""
    original_profile_class = candidate_tests.project_profiles.ProjectProfileContent
    original_profile_canonicalize = candidate_tests.project_profiles._canonicalize
    original_canonical_json_bytes = candidate_tests.prd._canonical_json_bytes
    original_write = candidate_tests._write

    def credential_profile_canonicalize(value):
        if isinstance(value, original_profile_class):
            value = value.model_copy(update={"notes": f"Authorization: Bearer {SYNTHETIC}"})
        return original_profile_canonicalize(value)

    def credential_prd_bytes(value):
        if (
            isinstance(value, dict)
            and value.get("schema_version") == candidate_tests.PRD_SCHEMA
            and isinstance(value.get("blocks"), list)
        ):
            value = deepcopy(value)
            for block in value["blocks"]:
                if block.get("ordinal") == 1:
                    block["text"] = f"password={SYNTHETIC}"
                elif block.get("ordinal") == 2:
                    block["table_rows"] = [
                        ["Header", "Value"],
                        ["auth", f"Authorization: Bearer {SYNTHETIC}"],
                    ]
        return original_canonical_json_bytes(value)

    def credential_git_write(repo: Path, path: str, data: bytes) -> None:
        if path == "src/a.txt" and data == b"alpha\na2\n":
            data = f"alpha\npassword={SYNTHETIC}\n".encode("utf-8")
        original_write(repo, path, data)

    monkeypatch.setattr(
        candidate_tests.project_profiles, "_canonicalize", credential_profile_canonicalize
    )
    monkeypatch.setattr(candidate_tests.prd, "_canonical_json_bytes", credential_prd_bytes)
    monkeypatch.setattr(candidate_tests, "_write", credential_git_write)
    try:
        state = candidate_tests._make_state(tmp_path, monkeypatch)
    finally:
        monkeypatch.setattr(
            candidate_tests.project_profiles, "_canonicalize", original_profile_canonicalize
        )
        monkeypatch.setattr(
            candidate_tests.prd, "_canonical_json_bytes", original_canonical_json_bytes
        )
        monkeypatch.setattr(candidate_tests, "_write", original_write)

    state["profile_content"] = deepcopy(state["profile_content"])
    state["profile_content"]["notes"] = f"Authorization: Bearer {SYNTHETIC}"
    structured = json.loads(state["prd_target"].read_text(encoding="utf-8"))
    state["blocks"] = structured["blocks"]
    state["prd_raw"] = state["prd_target"].read_bytes()
    return state


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _assert_code(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected
    return caught


def _prepare(state: dict, *, call_prepare_key: str = "redaction-transform-prepare-1"):
    return input_tests._prepare(state, call_prepare_key=call_prepare_key)


def _budget_record(**overrides) -> dict[str, object]:
    return input_tests._budget_record(**overrides)


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


def _resolve(prepared: dict, target: str, record: dict[str, object] | None = None):
    return context_redaction.build_context_redaction_result(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record() if record is None else record,
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


def _result_hash_payload(result: dict[str, object]) -> dict[str, object]:
    return {
        key: deepcopy(value)
        for key, value in result.items()
        if key not in {"redacted_body", "redaction_result_hash"}
    }


def test_t01_profile_formal_chain_redacts_credential_and_preserves_identity(credential_state):
    prepared = _prepare(credential_state)
    upstream = context_redaction_inputs.resolve_context_redaction_input(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record(),
        target="profile",
    )
    result = _resolve(prepared, "profile")
    assert upstream["raw_body"]["notes"] == f"Authorization: Bearer {SYNTHETIC}"
    assert "raw_body" not in result
    assert result["target"] == "profile"
    assert result["target_type"] == "profile"
    assert result["source_identity"] == upstream["source_identity"]
    assert result["redacted_body"]["notes"] == f"Authorization: {INLINE}"
    assert result["redaction_stats"] == {"R2": 1}
    assert result["redaction_state"] == "completed"
    assert result["redacted_body_state"] == "local_redacted_ephemeral"
    assert result["model_send_state"] == "not_admitted"


def test_t02_prd_real_paragraph_and_table_preserve_structure(credential_state):
    prepared = _prepare(credential_state)
    items = [item for item in _candidate(credential_state)["items"] if item["type"] == "prd_block"]
    assert len(items) == 2
    paragraph = _resolve(prepared, items[0]["evidence_id"])
    table = _resolve(prepared, items[1]["evidence_id"])
    assert paragraph["redacted_body"]["text"] == f"password={INLINE}"
    assert paragraph["redacted_body"]["kind"] == "paragraph"
    assert paragraph["redacted_body"]["heading_level"] is None
    assert paragraph["redacted_body"]["page_no"] == 1
    assert paragraph["redaction_stats"] == {"R3": 1}
    assert table["redacted_body"]["kind"] == "table"
    assert table["redacted_body"]["text"] is None
    assert table["redacted_body"]["table_rows"] == [
        ["Header", "Value"],
        ["auth", f"Authorization: {INLINE}"],
    ]
    assert table["redaction_stats"] == {"R2": 1}


def test_t03_git_real_historical_diff_preserves_metadata_prefixes_and_newlines(credential_state):
    prepared = _prepare(credential_state)
    item = _item(credential_state, item_type="git_file_fact", binary=False)
    upstream = context_redaction_inputs.resolve_context_redaction_input(
        model_call_id=prepared["model_call_id"],
        budget_record=_budget_record(),
        target=item["evidence_id"],
    )
    raw = upstream["raw_body"]
    assert isinstance(raw, str) and f"password={SYNTHETIC}" in raw
    result = _resolve(prepared, item["evidence_id"])
    redacted = result["redacted_body"]
    assert isinstance(redacted, str)
    assert f"password={INLINE}" in redacted
    assert SYNTHETIC not in redacted
    assert raw.count("\n") == redacted.count("\n")
    raw_lines = raw.splitlines(keepends=True)
    redacted_lines = redacted.splitlines(keepends=True)
    assert len(raw_lines) == len(redacted_lines)
    for before, after in zip(raw_lines, redacted_lines):
        if before.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@")):
            assert after == before
        elif before[:1] in {"+", "-", " "}:
            assert after[:1] == before[:1]
    assert result["source_identity"] == upstream["source_identity"]


def test_t04_binary_is_not_materialized_and_uses_exact_result_schema(redaction_state):
    prepared = _prepare(redaction_state)
    item = _item(redaction_state, item_type="git_file_fact", binary=True)
    result = _resolve(prepared, item["evidence_id"])
    assert tuple(result) == EXPECTED_RESULT_KEYS
    assert result["raw_body_kind"] == "binary_metadata_only"
    assert result["redacted_body"] is None
    assert result["redacted_body_hash"] is None
    assert result["redaction_state"] == "not_applicable"
    assert result["redacted_body_state"] == "not_applicable"
    assert result["redaction_match_count"] == 0
    assert result["model_send_state"] == "not_admitted"


def test_t05_signature_rejects_body_policy_sensitive_path_send_and_provider_injection(redaction_state):
    prepared = _prepare(redaction_state)
    signature = inspect.signature(context_redaction.build_context_redaction_result)
    assert list(signature.parameters) == ["model_call_id", "budget_record", "target"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    for name, value in {
        "raw_body": "secret",
        "policy": {"id": "caller"},
        "regex": ".*",
        "sensitive_paths": ["**/.env"],
        "send_permission": True,
        "provider": "caller-provider",
        "token_count": 1,
    }.items():
        with pytest.raises(TypeError):
            context_redaction.build_context_redaction_result(
                model_call_id=prepared["model_call_id"],
                budget_record=_budget_record(),
                target="profile",
                **{name: value},
            )


def test_t06_formal_redaction_input_closure_is_called_exactly_once(redaction_state, monkeypatch):
    prepared = _prepare(redaction_state)
    original = context_redaction.resolve_context_redaction_input
    calls = 0

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(context_redaction, "resolve_context_redaction_input", counted)
    result = _resolve(prepared, "profile")
    assert calls == 1
    assert result["target"] == "profile"
    source = inspect.getsource(context_redaction)
    assert "build_context_manifest_core" not in source
    assert "build_context_candidate_set" not in source
    assert "resolve_prd_block" not in source
    assert "resolve_git_file_fact" not in source


def test_t07_upstream_safe_identity_or_hash_drift_fails_closed(redaction_state, monkeypatch):
    prepared = _prepare(redaction_state)
    original = context_redaction.resolve_context_redaction_input

    def drifted(**kwargs):
        value = deepcopy(original(**kwargs))
        value["snapshot_hash"] = "f" * 64 if value["snapshot_hash"] != "f" * 64 else "e" * 64
        return value

    monkeypatch.setattr(context_redaction, "resolve_context_redaction_input", drifted)
    _assert_code(
        "CONTEXT_REDACTION_UPSTREAM_INCONSISTENT",
        lambda: _resolve(prepared, "profile"),
    )


def test_t08_policy_descriptor_is_exact_closed_and_hash_binds_all_identity_dimensions():
    exact_keys = tuple(EXPECTED_POLICY_DESCRIPTOR)
    assert len(exact_keys) == 18
    assert context_redaction.POLICY_DESCRIPTOR == EXPECTED_POLICY_DESCRIPTOR
    assert tuple(context_redaction.POLICY_DESCRIPTOR) == exact_keys
    assert context_redaction.REDACTION_POLICY_HASH == _canonical_hash(EXPECTED_POLICY_DESCRIPTOR)

    variants = []
    for key in (
        "policy_algorithm_version", "inline_placeholder", "header_grammar_id",
        "text_assignment_grammar_id", "url_grammar_id", "url_query_grammar_id",
        "auth_token_grammar_id", "private_key_grammar_id",
    ):
        variant = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
        variant[key] = str(variant[key]) + "-changed"
        variants.append(variant)
    for array_key in (
        "secret_key_exact", "secret_key_suffixes", "header_names", "auth_schemes",
        "private_key_block_labels", "rule_order",
    ):
        reordered = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
        reordered[array_key] = list(reversed(reordered[array_key]))
        variants.append(reordered)
    extended = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    extended["unknown_extension"] = True
    assert set(extended) != set(exact_keys)
    variants.append(extended)
    missing = deepcopy(EXPECTED_POLICY_DESCRIPTOR)
    missing.pop("rule_order")
    assert set(missing) != set(exact_keys)
    variants.append(missing)
    for variant in variants:
        assert _canonical_hash(variant) != context_redaction.REDACTION_POLICY_HASH


def test_t09_zero_match_keeps_content_but_never_marks_safe_or_admitted(redaction_state):
    prepared = _prepare(redaction_state)
    upstream = context_redaction_inputs.resolve_context_redaction_input(
        model_call_id=prepared["model_call_id"], budget_record=_budget_record(), target="profile"
    )
    result = _resolve(prepared, "profile")
    assert result["redacted_body"] == upstream["raw_body"]
    assert result["redaction_stats"] == {}
    assert result["redaction_match_count"] == 0
    assert result["redaction_state"] == "completed"
    assert result["model_send_state"] == "not_admitted"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "sendable" not in serialized
    # Approved source facts are data, not send authority.
    assert {"approved", "safe", "sendable"}.isdisjoint(result)


def test_t10_secret_key_classifier_and_assignment_boundaries_are_closed():
    for value in (
        "password", "API-KEY", "client.secret", "db_password", "openai_api_key",
        "prod_access_token",
    ):
        assert context_redaction._is_secret_key(value)
    for value in ("token_count", "max_tokens", "token_budget", "secretary", "mytoken"):
        assert not context_redaction._is_secret_key(value)
    positives = {
        f"password={SYNTHETIC}\n": f"password={INLINE}\n",
        f" API-KEY : \"{SYNTHETIC}\" ,\r\n": f" API-KEY : \"{INLINE}\" ,\r\n",
        f"'client.secret' = '{SYNTHETIC}'\n": f"'client.secret' = '{INLINE}'\n",
        f"db_password = {SYNTHETIC}   # keep\n": f"db_password = {INLINE}\n",
    }
    for source, expected in positives.items():
        actual, count = context_redaction._apply_r3_assignments(source)
        assert actual == expected and count == 1
    unchanged, count = context_redaction._apply_r3_assignments("token_count=12\n")
    assert unchanged == "token_count=12\n" and count == 0
    for source in (
        'password="unterminated\n', "password=|\n", "password=>\n",
        "password=a,b\n", 'password="ok" trailing\n',
        "password=\n", "password=   \n", "password= #comment\n",
        f"password={INLINE}\n",
    ):
        safe, _ = context_redaction._apply_r3_assignments(source)
        if not source.split("=", 1)[1].strip():
            assert safe == source
        else:
            assert INLINE in safe
        assert "unterminated" not in safe and "a,b" not in safe and "trailing" not in safe
    structured, stats = context_redaction._redact_profile_value(
        {"password": SYNTHETIC, "token_count": 10, "nested": {"api_key": None}}
    )
    assert structured == {"password": INLINE, "token_count": 10, "nested": {"api_key": None}}
    assert stats == {"R3": 1}

    integrated, stats = context_redaction._redact_text(
        f"Authorization: Bearer {SYNTHETIC}\n", include_assignments=True
    )
    assert integrated == f"Authorization: {INLINE}\n"
    assert stats == {"R2": 1}
    assert context_redaction._redact_text(
        f"password={INLINE}\n", include_assignments=True
    )[0] == INLINE + "\n"


def test_t11_sensitive_headers_are_line_anchored_and_value_only():
    source = (
        f"  Authorization :  Bearer {SYNTHETIC}\r\n"
        f"proxy-authorization:\tBasic {SYNTHETIC}\n"
        "Cookie: session=synthetic\n"
        "Set-Cookie: session=synthetic\n"
        f"text Authorization: Bearer {SYNTHETIC}\n"
        "Authorization:   \n"
        f"Authorization Bearer {SYNTHETIC}\n"
        "X-Header: keep\n"
    )
    actual, count = context_redaction._apply_r2_headers(source)
    assert count == 4
    assert f"text Authorization: Bearer {SYNTHETIC}" in actual
    assert "Authorization:   \n" in actual
    assert f"Authorization Bearer {SYNTHETIC}\n" in actual
    assert "X-Header: keep\n" in actual
    assert actual.startswith(f"  Authorization :  {INLINE}\r\n")
    assert f"proxy-authorization:\t{INLINE}\n" in actual


def test_t12_url_query_and_auth_token_boundaries_are_deterministic():
    userinfo, count = context_redaction._apply_r4_url_userinfo(
        "go https://user:pass@example.invalid/p?q=1 end"
    )
    assert userinfo == f"go https://{INLINE}@example.invalid/p?q=1 end" and count == 1
    assert context_redaction._apply_r4_url_userinfo("user:pass@example.invalid")[1] == 0
    assert context_redaction._apply_r4_url_userinfo(
        "https://user%40domain@example.invalid"
    )[0] == f"https://{INLINE}@example.invalid"
    assert context_redaction._apply_r4_url_userinfo("https://a@b@host.invalid/x") == (INLINE, 1)
    query, count = context_redaction._apply_r5_url_secret_query(
        "https://example.invalid/p?token=abc&x=1&api_key=a=b#frag"
    )
    assert query == f"https://example.invalid/p?token={INLINE}&x=1&api_key={INLINE}#frag"
    assert count == 2
    assert context_redaction._apply_r5_url_secret_query(
        "https://example.invalid/?api%5Fkey=x&token="
    )[1] == 0
    auth, count = context_redaction._apply_r6_auth_tokens(
        "Bearer ABCDEFGH, Basic 12345678; noBearer ABCDEFGH Bearer short"
    )
    assert auth == f"Bearer {INLINE}, Basic {INLINE}; noBearer ABCDEFGH Bearer short"
    assert count == 2

    integrated, stats = context_redaction._redact_text(
        "https://user:pass@example.invalid/p?token=abc&x=1",
        include_assignments=True,
    )
    assert integrated == f"https://{INLINE}@example.invalid/p?token={INLINE}&x=1"
    assert stats == {"R4": 1, "R5": 1}

    raw_placeholder = f"https://example.invalid/?token={INLINE}&api_key=abc"
    unchanged, stats = context_redaction._redact_text(
        raw_placeholder, include_assignments=True
    )
    assert unchanged == f"https://example.invalid/?token={INLINE}&api_key={INLINE}"
    assert stats == {"R5": 2}


def test_t13_private_key_state_machine_preserves_line_endings_and_fails_closed():
    for label in context_redaction.PRIVATE_KEY_BLOCK_LABELS:
        source = (
            f" \t-----BEGIN {label}----- \t\r\nAAA\r\nBBB\n\t-----END {label}-----  \r\n"
        )
        actual, count = context_redaction._apply_r1_private_key_blocks(source)
        assert actual == (
            f" \t-----BEGIN {label}----- \t\r\n{PRIVATE}\r\n{PRIVATE}\n"
            f"\t-----END {label}-----  \r\n"
        )
        assert count == 2
    invalid = (
        "-----END PRIVATE KEY-----\n",
        "-----BEGIN PRIVATE KEY-----\nAAA\n",
        "-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
        "-----BEGIN PRIVATE KEY-----\nAAA\n-----END RSA PRIVATE KEY-----\n",
        "-----BEGIN PRIVATE KEY-----\n-----BEGIN RSA PRIVATE KEY-----\nAAA\n"
        "-----END RSA PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
    )
    for source in invalid:
        _assert_code(
            "CONTEXT_REDACTION_UNSAFE_AMBIGUITY",
            lambda source=source: context_redaction._apply_r1_private_key_blocks(source),
        )
    unchanged = "-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n"
    assert context_redaction._apply_r1_private_key_blocks(unchanged) == (unchanged, 0)


def test_t14_unsupported_structured_shapes_fail_without_partial_success():
    _assert_code(
        "CONTEXT_REDACTION_UNSUPPORTED_SHAPE",
        lambda: context_redaction._redact_profile_value({"password": [SYNTHETIC]}),
    )
    _assert_code(
        "CONTEXT_REDACTION_UNSUPPORTED_SHAPE",
        lambda: context_redaction._redact_prd_body(
            {
                "kind": "table", "text": None, "table_rows": [["ok", 123]],
                "heading_level": None, "page_no": 1,
            }
        ),
    )


def test_t15_exact_result_schema_body_hash_and_result_hash_are_fully_bound(credential_state):
    prepared = _prepare(credential_state)
    result = _resolve(prepared, "profile")
    assert tuple(result) == EXPECTED_RESULT_KEYS
    assert context_redaction._RESULT_KEYS == EXPECTED_RESULT_KEYS
    assert set(result) == set(EXPECTED_RESULT_KEYS)
    assert "raw_body" not in result
    assert result["redacted_body_hash"] == _canonical_hash(result["redacted_body"])
    assert result["redaction_result_hash"] == _canonical_hash(_result_hash_payload(result))
    body_changed = deepcopy(result)
    body_changed["redacted_body"]["notes"] = "changed redacted content"
    assert _canonical_hash(body_changed["redacted_body"]) != result["redacted_body_hash"]
    metadata_changed = _result_hash_payload(result)
    metadata_changed["redaction_match_count"] = result["redaction_match_count"] + 1
    assert _canonical_hash(metadata_changed) != result["redaction_result_hash"]
    extended = deepcopy(result)
    extended["extension"] = "not-allowed"
    assert set(extended) != set(EXPECTED_RESULT_KEYS)
    expected_hash_keys = tuple(
        key for key in EXPECTED_RESULT_KEYS if key not in {"redacted_body", "redaction_result_hash"}
    )
    assert context_redaction._RESULT_HASH_KEYS == expected_hash_keys
    assert set(expected_hash_keys) == set(result) - {"redacted_body", "redaction_result_hash"}


def test_t16_success_and_failure_have_no_persistence_log_network_or_file_side_effects(
    redaction_state, monkeypatch, capsys
):
    prepared = _prepare(redaction_state)
    db_before = candidate_tests._db_dump(redaction_state["db_path"])
    original_connect = sqlite3.connect
    denied: list[int] = []
    calls: list[str] = []
    write_actions = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
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
    assert candidate_tests._db_dump(redaction_state["db_path"]) == db_before
    assert redaction_state["profile_content"]["project_summary"] not in captured.out
    assert redaction_state["profile_content"]["project_summary"] not in captured.err


def test_t17_no_downstream_preemption_and_unsupported_marker_stays_visible(redaction_state):
    prepared = _prepare(redaction_state)
    result = _resolve(prepared, "profile")
    assert "analysis_rules:not_frozen_in_snapshot_v2" in result["unsupported_context_sources"]
    assert result["model_send_state"] == "not_admitted"
    tree = ast.parse(inspect.getsource(context_redaction))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    assert imported & {
        "app.context_manifest.build_context_manifest_core",
        "app.context_resolver.build_context_candidate_set",
        "app.context_resolver.resolve_prd_block",
        "app.context_resolver.resolve_git_file_fact",
    } == set()
    assert not any(
        token in name.casefold()
        for name in imported
        for token in (
            "tiktoken", "transformers", "provider", "smtp", "sensitive_path",
            "admission", "final_manifest",
        )
    )
    serialized = json.dumps(result, ensure_ascii=False)
    assert "admitted" not in serialized.replace("not_admitted", "")


def test_t18_real_chain_and_upstream_object_are_not_mutated(redaction_state, monkeypatch):
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

    real_result = context_redaction.build_context_redaction_result(
        model_call_id=prepared["model_call_id"],
        budget_record=deepcopy(record),
        target=target,
    )
    assert real_result["model_send_state"] == "not_admitted"
    assert model_call_ledger.get_model_call(prepared["model_call_id"]) == ledger_before
    assert model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    ) == budget_before
    assert _candidate(redaction_state) == candidate_before
    assert context_manifest.build_context_manifest_core(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    ) == core_before
    assert candidate_tests._state_signature(redaction_state) == state_before

    upstream = context_redaction_inputs.resolve_context_redaction_input(
        model_call_id=prepared["model_call_id"],
        budget_record=deepcopy(record),
        target=target,
    )
    upstream_before = deepcopy(upstream)
    monkeypatch.setattr(
        context_redaction, "resolve_context_redaction_input", lambda **_kwargs: upstream
    )
    object_result = context_redaction.build_context_redaction_result(
        model_call_id=prepared["model_call_id"],
        budget_record=deepcopy(record),
        target=target,
    )
    assert object_result["model_send_state"] == "not_admitted"
    assert upstream == upstream_before
    assert model_call_ledger.get_model_call(prepared["model_call_id"]) == ledger_before
    assert model_budget_profiles.build_model_budget_profile(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    ) == budget_before
    assert _candidate(redaction_state) == candidate_before
    assert context_manifest.build_context_manifest_core(
        model_call_id=prepared["model_call_id"], budget_record=deepcopy(record)
    ) == core_before
    assert candidate_tests._state_signature(redaction_state) == state_before
