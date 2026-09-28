"""Report preparation risk-diagnostics fast-path regressions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import report_generation_preparation as subject  # noqa: E402


BUDGET = {"provider": "deepseek", "model_id": "m", "max_input_tokens": 100}
MANIFEST = {
    "model_call_id": 7,
    "manifest_core_hash": "a" * 64,
    "admitted_targets": [{"target": "git:1"}],
    "denied_targets": [],
}


def test_clean_admitted_target_skips_diagnostics_body_rescan(monkeypatch):
    runtime_calls: list[str] = []

    def redaction(**kwargs):
        runtime_calls.append(kwargs["target"])
        return {"redaction_state": "completed", "redaction_match_count": 0}

    monkeypatch.setattr(
        subject.context_redaction_runtime,
        "build_context_redaction_result",
        redaction,
    )
    monkeypatch.setattr(
        subject.context_redaction,
        "build_context_redaction_diagnostics",
        lambda **_kwargs: pytest.fail("clean target must not be rescanned for line diagnostics"),
    )

    assert subject._safe_context_findings(MANIFEST, BUDGET) == []
    assert runtime_calls == ["git:1"]


def test_matching_admitted_target_computes_safe_line_diagnostics(monkeypatch):
    monkeypatch.setattr(
        subject.context_redaction_runtime,
        "build_context_redaction_result",
        lambda **_kwargs: {"redaction_state": "completed", "redaction_match_count": 1},
    )
    diagnostics = [{
        "target": "git:1",
        "path": "src/style.scss",
        "line_start": 4,
        "line_end": 4,
        "rule_id": "R3",
        "risk_level": "warning",
        "action": "isolated",
        "safe_snippet": "[REDACTED:CREDENTIAL]",
    }]
    seen: list[str] = []

    def build(**kwargs):
        seen.append(kwargs["target"])
        return diagnostics

    monkeypatch.setattr(subject.context_redaction, "build_context_redaction_diagnostics", build)

    assert subject._safe_context_findings(MANIFEST, BUDGET) == diagnostics
    assert seen == ["git:1"]


def test_quarantined_target_still_gets_visible_diagnostics(monkeypatch):
    monkeypatch.setattr(
        subject.context_redaction_runtime,
        "build_context_redaction_result",
        lambda **_kwargs: {"redaction_state": "quarantined", "redaction_match_count": 0},
    )
    seen: list[str] = []

    def build(**kwargs):
        seen.append(kwargs["target"])
        return [{"target": kwargs["target"], "safe_snippet": "[REDACTED:TARGET_QUARANTINED]"}]

    monkeypatch.setattr(subject.context_redaction, "build_context_redaction_diagnostics", build)

    findings = subject._safe_context_findings(MANIFEST, BUDGET)
    assert findings[0]["safe_snippet"] == "[REDACTED:TARGET_QUARANTINED]"
    assert seen == ["git:1"]


def test_token_framing_uses_request_local_redaction_runtime():
    from app import context_redaction_runtime, context_token_framing

    assert (
        context_token_framing.build_context_redaction_result
        is context_redaction_runtime.build_context_redaction_result
    )
