"""Request-local redacted-result cache acceptance tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import context_candidate_runtime as candidate_runtime  # noqa: E402
from app import context_redaction_runtime as subject  # noqa: E402


BUDGET = {"provider": "deepseek", "model_id": "m", "max_input_tokens": 100}


def _result(target: str) -> dict[str, object]:
    return {
        "target": target,
        "redaction_state": "completed",
        "redacted_body": "safe",
        "redaction_match_count": 0,
        "redaction_result_hash": "a" * 64,
    }


def test_outside_scope_does_not_cache(monkeypatch):
    calls: list[str] = []

    def build(**kwargs):
        calls.append(kwargs["target"])
        return _result(kwargs["target"])

    monkeypatch.setattr(subject, "_build_context_redaction_result", build)
    subject.build_context_redaction_result(model_call_id=1, budget_record=BUDGET, target="git:1")
    subject.build_context_redaction_result(model_call_id=1, budget_record=BUDGET, target="git:1")
    assert calls == ["git:1", "git:1"]


def test_scope_reuses_redacted_result_and_returns_detached_copy(monkeypatch):
    calls: list[str] = []

    def build(**kwargs):
        calls.append(kwargs["target"])
        return _result(kwargs["target"])

    monkeypatch.setattr(subject, "_build_context_redaction_result", build)
    with candidate_runtime.candidate_materialization_scope():
        first = subject.build_context_redaction_result(
            model_call_id=1, budget_record=BUDGET, target="git:1"
        )
        first["redacted_body"] = "mutated"
        second = subject.build_context_redaction_result(
            model_call_id=1, budget_record=BUDGET, target="git:1"
        )
        assert second["redacted_body"] == "safe"

    assert calls == ["git:1"]


def test_budget_identity_and_target_are_part_of_cache_key(monkeypatch):
    calls: list[tuple[str, int]] = []

    def build(**kwargs):
        calls.append((kwargs["target"], kwargs["budget_record"]["max_input_tokens"]))
        return _result(kwargs["target"])

    monkeypatch.setattr(subject, "_build_context_redaction_result", build)
    with candidate_runtime.candidate_materialization_scope():
        subject.build_context_redaction_result(
            model_call_id=1, budget_record=BUDGET, target="git:1"
        )
        subject.build_context_redaction_result(
            model_call_id=1,
            budget_record={**BUDGET, "max_input_tokens": 101},
            target="git:1",
        )
        subject.build_context_redaction_result(
            model_call_id=1, budget_record=BUDGET, target="git:2"
        )

    assert calls == [("git:1", 100), ("git:1", 101), ("git:2", 100)]
