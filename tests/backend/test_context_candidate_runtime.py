"""Request-local Context Candidate cache acceptance tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import context_candidate_runtime as subject  # noqa: E402


def _candidate(snapshot_id: int) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "project_id": 1,
        "snapshot_hash": "a" * 64,
        "items": [{"evidence_id": f"git:{snapshot_id}"}],
    }


def test_outside_scope_keeps_authoritative_builder_semantics(monkeypatch):
    calls: list[int] = []

    def build(snapshot_id: int):
        calls.append(snapshot_id)
        return _candidate(snapshot_id)

    monkeypatch.setattr(subject, "_build_context_candidate_set", build)
    subject.build_context_candidate_set(11)
    subject.build_context_candidate_set(11)
    assert calls == [11, 11]


def test_one_scope_materializes_each_snapshot_once_and_returns_detached_values(monkeypatch):
    calls: list[int] = []

    def build(snapshot_id: int):
        calls.append(snapshot_id)
        return _candidate(snapshot_id)

    monkeypatch.setattr(subject, "_build_context_candidate_set", build)
    with subject.candidate_materialization_scope():
        first = subject.build_context_candidate_set(11)
        first["items"][0]["evidence_id"] = "mutated"
        second = subject.build_context_candidate_set(11)
        third = subject.build_context_candidate_set(12)
        assert second["items"][0]["evidence_id"] == "git:11"
        assert third["snapshot_id"] == 12

    assert calls == [11, 12]


def test_nested_scope_reuses_outer_cache_and_scope_exit_drops_it(monkeypatch):
    calls: list[int] = []

    def build(snapshot_id: int):
        calls.append(snapshot_id)
        return _candidate(snapshot_id)

    monkeypatch.setattr(subject, "_build_context_candidate_set", build)
    with subject.candidate_materialization_scope():
        subject.build_context_candidate_set(11)
        with subject.candidate_materialization_scope():
            subject.build_context_candidate_set(11)
    subject.build_context_candidate_set(11)

    assert calls == [11, 11]


def test_scope_is_cleared_after_exception(monkeypatch):
    calls: list[int] = []

    def build(snapshot_id: int):
        calls.append(snapshot_id)
        return _candidate(snapshot_id)

    monkeypatch.setattr(subject, "_build_context_candidate_set", build)
    with pytest.raises(RuntimeError):
        with subject.candidate_materialization_scope():
            subject.build_context_candidate_set(11)
            raise RuntimeError("boom")

    subject.build_context_candidate_set(11)
    assert calls == [11, 11]
