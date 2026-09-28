"""Regression: ordinary source grammar must not veto an otherwise usable target."""

import json
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))
from app import context_redaction as redaction


@pytest.mark.parametrize("selector", [".password:focus {", ".api-key:hover {", ".token:active {"])
def test_css_selector_does_not_become_a_secret_assignment(selector):
    source = selector + "\n  color: red;\n}\n"
    assert redaction._redact_text(source, include_assignments=True) == (source, {})


def test_empty_form_labels_and_prd_credential_words_are_not_blockers():
    source = "password:\nAPI Key / Token 是配置字段，密码不应显示。\npassword: string;\n.normal { color: red; }\n"
    safe, _ = redaction._redact_text(source, include_assignments=True)
    assert "API Key / Token 是配置字段" in safe
    assert ".normal { color: red; }" in safe


@pytest.mark.parametrize("assignment", [
    "password=SYNTHETIC_SECRET,a",
    'api_key="SYNTHETIC_SECRET" trailing',
    "token=[SYNTHETIC_SECRET]",
    "password=[REDACTED:CREDENTIAL]",
])
def test_ambiguous_single_line_is_quarantined_with_location_and_safe_warning(assignment):
    source = "ordinary before\n" + assignment + "\nordinary after\n"
    result = redaction.redact_with_diagnostics(source)
    assert "SYNTHETIC_SECRET" not in json.dumps(result)
    assert "ordinary before" in result["redacted_text"]
    assert "ordinary after" in result["redacted_text"]
    finding = result["diagnostics"][0]
    assert finding == {
        "rule_id": "R3", "risk_level": "warning", "action": "isolated",
        "line_start": 2, "line_end": 2,
        "safe_snippet": "[REDACTED:CREDENTIAL]",
    }


def test_unterminated_multiline_scalar_is_never_released_as_a_warning_raw_body():
    source = 'ordinary\npassword="SYNTHETIC_FIRST\nSYNTHETIC_SECOND\n'
    result = redaction.redact_with_diagnostics(source)
    assert "SYNTHETIC" not in json.dumps(result)
    assert result["diagnostics"][0]["line_start"] == 2
    assert result["diagnostics"][0]["line_end"] == 3
    assert result["redacted_text"].splitlines()[0] == "ordinary"
    assert len(result["redacted_text"].splitlines()) == 3


def test_yaml_multiline_secret_is_isolated_until_dedent():
    source = "password: |\n  SYNTHETIC_FIRST\n  SYNTHETIC_SECOND\nordinary: keep\n"
    result = redaction.redact_with_diagnostics(source)
    assert "SYNTHETIC" not in json.dumps(result)
    assert result["redacted_text"].endswith("ordinary: keep\n")
    assert result["diagnostics"][0]["line_end"] == 3


def test_explicit_api_key_is_redacted_and_diagnostic_cannot_echo_it():
    result = redaction.redact_with_diagnostics("api_key=SYNTHETIC_VALUE_123\n")
    assert result["redacted_text"] == "api_key=[REDACTED:CREDENTIAL]\n"
    assert "SYNTHETIC" not in json.dumps(result)
    assert result["diagnostics"][0]["risk_level"] == "high"
    assert result["diagnostics"][0]["action"] == "redacted"


def test_ambiguous_url_is_locally_isolated_without_releasing_userinfo():
    result = redaction.redact_with_diagnostics("before https://SYNTHETIC@a@host.invalid/x after\n")
    assert "SYNTHETIC" not in json.dumps(result)
    assert "before " in result["redacted_text"] and " after" in result["redacted_text"]
    assert result["diagnostics"][0]["risk_level"] == "warning"


def test_unclosed_private_key_still_cannot_escape_as_raw_text():
    with pytest.raises(HTTPException) as caught:
        redaction.redact_with_diagnostics("-----BEGIN PRIVATE KEY-----\nSYNTHETIC_MATERIAL\n")
    assert caught.value.detail["code"] == "CONTEXT_REDACTION_UNSAFE_AMBIGUITY"
    assert "SYNTHETIC" not in json.dumps(caught.value.detail)


def test_git_diff_prefixes_and_line_count_survive_local_isolation():
    source = "diff --git a/style.scss b/style.scss\n@@ -1,2 +1,3 @@\n .password:focus {\n+password=[SYNTHETIC_VALUE]\n+ color: red;\n }\n"
    safe, stats = redaction._redact_git_diff(source)
    assert "SYNTHETIC" not in safe
    assert " .password:focus {\n" in safe and "+ color: red;\n" in safe
    assert len(safe.splitlines()) == len(source.splitlines())
    assert stats["R3"] > 0


@pytest.mark.parametrize("kind", ["profile_json", "prd_structured_block", "text_unified_diff"])
def test_formal_ambiguity_quarantines_target_without_raw_body(monkeypatch, kind):
    body = "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_MATERIAL\n"
    raw = body if kind == "text_unified_diff" else ({"notes": body} if kind == "profile_json" else
        {"kind": "paragraph", "text": body, "table_rows": None, "heading_level": None, "page_no": None})
    upstream = {"raw_body_kind": kind, "raw_body": raw}
    monkeypatch.setattr(redaction, "resolve_context_redaction_input", lambda **kwargs: upstream)
    monkeypatch.setattr(redaction, "_validate_upstream", lambda value, **kwargs: value)
    monkeypatch.setattr(redaction, "_build_result", lambda upstream, **kwargs: kwargs)
    if kind == "text_unified_diff":
        upstream["raw_body"] = "diff --git a/a b/a\n@@ -0,0 +1,2 @@\n+-----BEGIN PRIVATE KEY-----\n+SYNTHETIC_MATERIAL\n"
    result = redaction.build_context_redaction_result(model_call_id=1, budget_record={}, target="x")
    assert result["redaction_state"] == "quarantined"
    assert redaction.is_quarantined_body(kind, result["redacted_body"])
    assert "SYNTHETIC" not in json.dumps(result)


def test_formal_diagnostics_include_target_and_diff_location(monkeypatch):
    upstream = {"raw_body_kind": "text_unified_diff", "source_identity": {"path": "a.txt", "source_ref": "git:1"},
        "raw_body": "diff --git a/a b/a\n@@ -0,0 +1 @@\n+api_key=SYNTHETIC_123\n"}
    monkeypatch.setattr(redaction, "resolve_context_redaction_input", lambda **kwargs: upstream)
    monkeypatch.setattr(redaction, "_validate_upstream", lambda value, **kwargs: value)
    result = redaction.build_context_redaction_diagnostics(model_call_id=1, budget_record={}, target="git:1")
    assert result[0]["target"] == "git:1"
    assert result[0]["path"] == "a.txt"
    assert result[0]["line_start"] == 3
    assert result[0]["range_kind"] == "diff_lines"
    assert "SYNTHETIC" not in json.dumps(result)


@pytest.mark.parametrize("text", ["Authorization: Bearer SYNTHETIC_123", "Cookie: SYNTHETIC_123",
    "https://user:SYNTHETIC_123@host.invalid/", "https://host.invalid/?api_key=SYNTHETIC_123",
    "Bearer SYNTHETIC_123", "password=SYNTHETIC_123 # SYNTHETIC_COMMENT"])
def test_all_existing_secret_rules_keep_values_out_of_diagnostics(text):
    result = redaction.redact_with_diagnostics(text)
    assert "SYNTHETIC" not in json.dumps(result)
    assert result["diagnostics"]


def test_input_placeholder_cannot_hide_later_url_credential():
    result = redaction.redact_with_diagnostics(
        "https://host.invalid/?token=[REDACTED:CREDENTIAL]&api_key=SYNTHETIC_123")
    assert "SYNTHETIC" not in json.dumps(result)
    assert result["redaction_stats"]["R5"] == 2


@pytest.mark.parametrize("source", [
    'password = {\n "value": "SYNTHETIC_NESTED"\n}\n',
    'password = [\n "SYNTHETIC_NESTED"\n]\n',
    'password = "SYNTHETIC_PART1" + ' + chr(92) + '\n "SYNTHETIC_PART2"\n',
])
def test_multiline_container_and_continuation_withhold_all_credential_material(source):
    result = redaction.redact_with_diagnostics(source)
    assert "SYNTHETIC" not in json.dumps(result)
    assert result["diagnostics"][0]["line_end"] == len(source.splitlines())
    assert len(result["redacted_text"].splitlines()) == len(source.splitlines())


def test_brace_inside_string_is_not_a_multiline_container_boundary():
    result = redaction.redact_with_diagnostics('password = { "marker": "}"\n "SYNTHETIC_NESTED"\n}\n')
    assert "SYNTHETIC" not in json.dumps(result)
    assert result["diagnostics"][0]["line_end"] == 3
