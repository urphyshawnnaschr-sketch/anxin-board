"""Page 07 Report Review Slice 1/2 core invariants."""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from app import (  # noqa: E402
    context_resolver,
    db,
    model_execution_results,
    report_generation_tasks,
    report_review,
)
import test_context_candidate_set as candidate_tests  # noqa: E402
import test_model_execution_results as result_tests  # noqa: E402


@pytest.fixture()
def review_state(tmp_path, monkeypatch):
    state = candidate_tests._make_state(tmp_path, monkeypatch)
    db.init_db()
    report_review.init_report_review_schema()
    candidate = context_resolver.build_context_candidate_set(state["snapshot_id"])
    state["evidence_id"] = candidate["items"][0]["evidence_id"]
    state["prd_evidence_id"] = next(
        item["evidence_id"] for item in candidate["items"] if item["type"] == "prd_block"
    )
    call = result_tests._prepare(
        state,
        task_type="daily_report_generate",
        local_task_id="page07-slice1-report",
        call_prepare_key="page07-slice1-prepare",
    )
    result = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"],
        receipt=result_tests._receipt(state, call),
    )
    state["call"] = call
    state["model_result"] = result
    return state


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _model_row(state: dict) -> tuple:
    with sqlite3.connect(state["db_path"]) as conn:
        row = conn.execute(
            """
            SELECT formal_response_json, formal_response_hash,
                   validated_result_json, validated_result_hash,
                   execution_result_hash
            FROM model_execution_results
            WHERE id = ?
            """,
            (state["model_result"]["model_result_id"],),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _report_identity_row(state: dict, report_id: int) -> tuple:
    with sqlite3.connect(state["db_path"]) as conn:
        row = conn.execute(
            """
            SELECT schema_version, project_id, version_no, parent_report_version_id,
                   model_execution_result_id, execution_result_hash,
                   formal_response_hash, validated_result_hash, model_call_id,
                   call_identity_hash, evidence_snapshot_id, evidence_snapshot_hash,
                   report_content_hash, created_at
            FROM report_versions
            WHERE id = ?
            """,
            (report_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _report_state_row(state: dict, report_id: int) -> tuple:
    with sqlite3.connect(state["db_path"]) as conn:
        row = conn.execute(
            "SELECT lifecycle, state_version FROM report_versions WHERE id = ?",
            (report_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _report_count(state: dict) -> int:
    with sqlite3.connect(state["db_path"]) as conn:
        return conn.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0]


def _supplement_count(state: dict) -> int:
    with sqlite3.connect(state["db_path"]) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM report_supplement_versions"
        ).fetchone()[0]


def _reanalysis_counts(state: dict) -> tuple[int, int, int]:
    with sqlite3.connect(state["db_path"]) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM report_reanalysis_requests").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM report_generation_tasks").fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM report_generation_task_attempts"
            ).fetchone()[0],
        )


def _materialize(state: dict) -> dict:
    return report_review.materialize_report_version(
        project_id=state["project_id"],
        model_execution_result_id=state["model_result"]["model_result_id"],
    )


def _reanalyze(state: dict, report_id: int, **overrides) -> dict:
    payload = {
        "project_id": state["project_id"],
        "report_version_id": report_id,
        "error_location": "模块 backend / 测试状态",
        "corrected_truth": "测试结论尚未形成，不能写成已完成。",
        "correction_basis": "PM 核对现有客观证据后确认。",
        "correction_source": "来源：项目经理核对冻结证据。",
        "requested_by": "pm-user",
        "requested_timezone": "Asia/Shanghai",
        "idempotency_key": "reanalyze-1",
        "expected_report_state_version": 1,
    }
    payload.update(overrides)
    return report_review.create_reanalysis_request(**payload)


def test_t01_report_version_is_reference_only_immutable_and_idempotent(review_state):
    before_model = _model_row(review_state)
    first = _materialize(review_state)
    second = _materialize(review_state)

    assert first["created"] is True
    assert second["created"] is False
    assert first["report_version"] == second["report_version"]
    report = first["report_version"]
    assert report["version_no"] == 1
    assert report["lifecycle"] == "pending_review"
    assert report["state_version"] == 1
    assert report["model_execution_result_id"] == review_state["model_result"]["model_result_id"]
    assert report["execution_result_hash"] == review_state["model_result"]["execution_result_hash"]
    assert report["formal_response_hash"] == review_state["model_result"]["formal_response_hash"]
    assert report["validated_result_hash"] == review_state["model_result"]["validated_result_hash"]
    assert report["report_content_hash"] == review_state["model_result"]["validated_result_hash"]
    assert report["evidence_snapshot_id"] == review_state["snapshot_id"]
    assert _report_count(review_state) == 1
    assert _model_row(review_state) == before_model

    with sqlite3.connect(review_state["db_path"]) as conn:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(report_versions)").fetchall()
        ]
    assert "formal_response_json" not in columns
    assert "validated_result_json" not in columns
    assert "ai_raw" not in columns


def test_t02_non_daily_model_result_cannot_materialize(review_state):
    state = review_state
    call = result_tests._prepare(
        state,
        task_type="project_profile_build",
        local_task_id="page07-not-report",
        call_prepare_key="page07-not-report-prepare",
    )
    other = model_execution_results.record_model_execution_result(
        model_call_id=call["model_call_id"],
        receipt=result_tests._receipt(state, call),
    )
    before = _report_count(state)
    with pytest.raises(HTTPException) as caught:
        report_review.materialize_report_version(
            project_id=state["project_id"],
            model_execution_result_id=other["model_result_id"],
        )
    assert _code(caught) == "REPORT_VERSION_SOURCE_NOT_REVIEWABLE"
    assert _report_count(state) == before


def test_t03_review_bundle_is_read_only_and_separates_ai_git_evidence(review_state):
    report = _materialize(review_state)["report_version"]
    before_model = _model_row(review_state)
    before_counts = (_report_count(review_state), _supplement_count(review_state))

    bundle = report_review.get_review_bundle(
        project_id=review_state["project_id"],
        report_version_id=report["report_version_id"],
    )
    assert bundle["schema_version"] == "report_review_bundle_v1"
    assert bundle["report_version"] == report
    assert bundle["ai_raw"]["content"] == review_state["model_result"]["validated_result"]
    assert (
        bundle["ai_raw"]["model_execution_result_id"]
        == review_state["model_result"]["model_result_id"]
    )
    assert bundle["ai_raw"]["validated_result_hash"] == report["validated_result_hash"]
    assert bundle["evidence_snapshot"]["snapshot_id"] == review_state["snapshot_id"]
    assert bundle["git_facts"]["branch"] == "main"
    assert bundle["git_facts"]["from_commit"] == review_state["from_commit"]
    assert bundle["git_facts"]["to_commit"] == review_state["to_commit"]
    assert bundle["current_supplement"] is None
    assert review_state["evidence_id"] in {
        item["evidence_id"] for item in bundle["evidence_refs"]
    }

    assert _model_row(review_state) == before_model
    assert (_report_count(review_state), _supplement_count(review_state)) == before_counts


def test_t04_supplement_is_append_only_versioned_and_preserves_provenance(review_state):
    report = _materialize(review_state)["report_version"]
    report_id = report["report_version_id"]
    before_model = _model_row(review_state)

    first = report_review.append_supplement_version(
        project_id=review_state["project_id"],
        report_version_id=report_id,
        content="甲方补充：接口联调仍待确认。",
        source_type="pm_external_fact",
        provided_by="pm-user",
        provided_timezone="Asia/Shanghai",
        idempotency_key="supplement-1",
        expected_latest_supplement_version=0,
    )
    second = report_review.append_supplement_version(
        project_id=review_state["project_id"],
        report_version_id=report_id,
        content="项目经理更正：测试结论尚未形成。",
        source_type="pm_correction",
        provided_by="pm-user",
        provided_timezone="Asia/Shanghai",
        idempotency_key="supplement-2",
        expected_latest_supplement_version=1,
    )

    assert first["created"] is True
    assert second["created"] is True
    assert first["supplement_version"]["version_no"] == 1
    assert second["supplement_version"]["version_no"] == 2
    assert first["supplement_version"]["provided_by"] == "pm-user"
    assert first["supplement_version"]["provided_at"]
    assert first["supplement_version"]["provided_timezone"] == "Asia/Shanghai"
    assert first["supplement_version"]["content_hash"] == hashlib.sha256(
        "甲方补充：接口联调仍待确认。".encode("utf-8")
    ).hexdigest()

    history = report_review.list_supplement_versions(
        project_id=review_state["project_id"],
        report_version_id=report_id,
    )
    assert [item["version_no"] for item in history] == [1, 2]
    assert history[0]["content"] == "甲方补充：接口联调仍待确认。"
    assert history[1]["content"] == "项目经理更正：测试结论尚未形成。"
    assert _model_row(review_state) == before_model

    bundle = report_review.get_review_bundle(
        project_id=review_state["project_id"],
        report_version_id=report_id,
    )
    assert bundle["current_supplement"] == history[1]
    assert bundle["ai_raw"]["content"] == review_state["model_result"]["validated_result"]


def test_t05_supplement_exact_idempotent_replay_and_conflict(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    kwargs = dict(
        project_id=review_state["project_id"],
        report_version_id=report_id,
        content="补充一",
        source_type="pm_external_fact",
        provided_by="pm",
        provided_timezone="UTC",
        idempotency_key="same-key",
        expected_latest_supplement_version=0,
    )
    first = report_review.append_supplement_version(**kwargs)
    replay = report_review.append_supplement_version(**kwargs)
    assert first["created"] is True
    assert replay["created"] is False
    assert replay["supplement_version"] == first["supplement_version"]
    assert _supplement_count(review_state) == 1

    conflict = dict(kwargs)
    conflict["content"] = "不同内容"
    with pytest.raises(HTTPException) as caught:
        report_review.append_supplement_version(**conflict)
    assert _code(caught) == "REPORT_SUPPLEMENT_IDEMPOTENCY_CONFLICT"
    assert _supplement_count(review_state) == 1


def test_t06_stale_supplement_expected_version_is_zero_write(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    report_review.append_supplement_version(
        project_id=review_state["project_id"],
        report_version_id=report_id,
        content="v1",
        source_type="pm_external_fact",
        provided_by="pm",
        provided_timezone="UTC",
        idempotency_key="v1",
        expected_latest_supplement_version=0,
    )
    before_model = _model_row(review_state)
    with pytest.raises(HTTPException) as caught:
        report_review.append_supplement_version(
            project_id=review_state["project_id"],
            report_version_id=report_id,
            content="stale",
            source_type="pm_external_fact",
            provided_by="pm",
            provided_timezone="UTC",
            idempotency_key="stale",
            expected_latest_supplement_version=0,
        )
    assert _code(caught) == "REPORT_SUPPLEMENT_STALE"
    assert _supplement_count(review_state) == 1
    assert _model_row(review_state) == before_model


def test_t07_database_guards_preserve_report_identity_and_supplement_history(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    created = report_review.append_supplement_version(
        project_id=review_state["project_id"],
        report_version_id=report_id,
        content="immutable",
        source_type="pm_external_fact",
        provided_by="pm",
        provided_timezone="UTC",
        idempotency_key="immutable",
        expected_latest_supplement_version=0,
    )["supplement_version"]
    with sqlite3.connect(review_state["db_path"]) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE report_versions SET validated_result_hash = ? WHERE id = ?",
                ("0" * 64, report_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE report_supplement_versions SET content = 'mutated' WHERE id = ?",
                (created["supplement_version_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM report_supplement_versions WHERE id = ?",
                (created["supplement_version_id"],),
            )


def test_t08_tampered_model_execution_bytes_fail_closed_on_review(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    with sqlite3.connect(review_state["db_path"]) as conn:
        conn.execute(
            "UPDATE model_execution_results SET validated_result_json = '{}' WHERE id = ?",
            (review_state["model_result"]["model_result_id"],),
        )
    before = _supplement_count(review_state)
    with pytest.raises(HTTPException) as caught:
        report_review.get_review_bundle(
            project_id=review_state["project_id"],
            report_version_id=report_id,
        )
    assert _code(caught) == "REPORT_REVIEW_STORED_INVALID"
    assert _supplement_count(review_state) == before


def test_t09_cross_project_access_is_not_a_write_or_data_alias(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    before_model = _model_row(review_state)
    with pytest.raises(HTTPException) as caught:
        report_review.get_review_bundle(
            project_id=review_state["project_id"] + 999,
            report_version_id=report_id,
        )
    assert _code(caught) == "REPORT_VERSION_NOT_FOUND"
    assert _model_row(review_state) == before_model


@pytest.mark.parametrize("read_path", ["current", "exact"])
def test_t10_read_paths_do_not_install_report_review_schema(
    tmp_path, monkeypatch, read_path
):
    state = candidate_tests._make_state(tmp_path, monkeypatch)
    db.init_db()

    schema_names = {
        "report_versions",
        "report_supplement_versions",
        "report_reanalysis_requests",
        "ix_report_versions_project_current",
        "ix_report_supplement_versions_latest",
        "tr_report_versions_no_delete",
        "tr_report_versions_immutable_identity",
        "tr_report_supplement_versions_no_update",
        "tr_report_supplement_versions_no_delete",
        "tr_report_reanalysis_requests_no_update",
        "tr_report_reanalysis_requests_no_delete",
    }

    def installed_review_schema_names() -> set[str]:
        placeholders = ",".join("?" for _ in schema_names)
        with sqlite3.connect(state["db_path"]) as conn:
            rows = conn.execute(
                f"SELECT name FROM sqlite_master WHERE name IN ({placeholders})",
                tuple(sorted(schema_names)),
            ).fetchall()
        return {row[0] for row in rows}

    assert installed_review_schema_names() == set()

    with pytest.raises(HTTPException) as caught:
        if read_path == "current":
            report_review.get_current_review_bundle(project_id=state["project_id"])
        else:
            report_review.get_review_bundle(
                project_id=state["project_id"],
                report_version_id=1,
            )

    assert _code(caught) == "REPORT_REVIEW_STORED_INVALID"
    assert installed_review_schema_names() == set()


def test_t11_authoritative_prd_item_tamper_blocks_review_bundle(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    with sqlite3.connect(review_state["db_path"]) as conn:
        conn.execute(
            """
            UPDATE evidence_items
            SET redaction_state = 'not_applicable'
            WHERE snapshot_id = ? AND evidence_id = ?
            """,
            (review_state["snapshot_id"], review_state["prd_evidence_id"]),
        )

    with pytest.raises(HTTPException) as caught:
        report_review.get_review_bundle(
            project_id=review_state["project_id"],
            report_version_id=report_id,
        )
    assert _code(caught) == "REPORT_REVIEW_STORED_INVALID"


def test_t12_authoritative_prd_item_tamper_blocks_materialization(review_state):
    with sqlite3.connect(review_state["db_path"]) as conn:
        conn.execute(
            """
            UPDATE evidence_items
            SET redaction_state = 'not_applicable'
            WHERE snapshot_id = ? AND evidence_id = ?
            """,
            (review_state["snapshot_id"], review_state["prd_evidence_id"]),
        )

    with pytest.raises(HTTPException) as caught:
        _materialize(review_state)
    assert _code(caught) == "REPORT_REVIEW_STORED_INVALID"
    assert _report_count(review_state) == 0


def test_t13_materialization_holds_db_and_git_guard_through_authoritative_read(
    review_state, monkeypatch
):
    real_build = report_review.build_context_candidate_set
    observed = {"git_competitor_blocked": False, "db_competitor_blocked": False}

    def probed_build(snapshot_id: int) -> dict:
        def compete_for_git_lock() -> None:
            try:
                with context_resolver.project_workspace_lock(review_state["project_id"]):
                    observed["git_competitor_blocked"] = False
            except context_resolver.GitOperationInProgress:
                observed["git_competitor_blocked"] = True

        competitor = threading.Thread(target=compete_for_git_lock)
        competitor.start()
        competitor.join(timeout=2)
        assert not competitor.is_alive()

        try:
            with sqlite3.connect(review_state["db_path"], timeout=0) as other:
                other.execute(
                    """
                    UPDATE evidence_snapshots
                    SET frozen_at = frozen_at || ''
                    WHERE id = ?
                    """,
                    (review_state["snapshot_id"],),
                )
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
            observed["db_competitor_blocked"] = True

        return real_build(snapshot_id)

    monkeypatch.setattr(report_review, "build_context_candidate_set", probed_build)
    created = _materialize(review_state)

    assert created["created"] is True
    assert observed == {
        "git_competitor_blocked": True,
        "db_competitor_blocked": True,
    }
    assert _report_count(review_state) == 1


def test_t14_reanalysis_creates_durable_request_and_new_same_snapshot_regenerate_task(
    review_state,
):
    report = _materialize(review_state)["report_version"]
    report_id = report["report_version_id"]
    before_model = _model_row(review_state)
    before_report_identity = _report_identity_row(review_state, report_id)

    result = _reanalyze(review_state, report_id)

    assert result["created"] is True
    request = result["reanalysis_request"]
    task = result["replacement_task"]
    superseded = result["source_report_version"]

    assert request["report_version_id"] == report_id
    assert request["evidence_snapshot_id"] == report["evidence_snapshot_id"]
    assert request["evidence_snapshot_hash"] == report["evidence_snapshot_hash"]
    assert request["source_report_state_version"] == 1
    assert request["source_report_lifecycle"] == "pending_review"
    assert request["error_location"] == "模块 backend / 测试状态"
    assert request["corrected_truth"] == "测试结论尚未形成，不能写成已完成。"
    assert request["correction_basis"] == "PM 核对现有客观证据后确认。"
    assert request["correction_source"] == "来源：项目经理核对冻结证据。"
    assert request["correction_source_hash"] == hashlib.sha256(
        "来源：项目经理核对冻结证据。".encode("utf-8")
    ).hexdigest()
    assert request["replacement_task_id"] == task["id"]
    assert request["replacement_local_task_id"] == task["local_task_id"]
    assert request["replacement_task_identity_hash"] == task["identity_hash"]
    assert task["task_type"] == report_generation_tasks.REGENERATE_TASK_TYPE
    assert task["evidence_snapshot_id"] == report["evidence_snapshot_id"]
    assert task["state"] == "queued"
    assert task["current_attempt"]["sequence_no"] == 1
    assert task["current_attempt"]["state"] == "queued"
    assert superseded["lifecycle"] == "superseded"
    assert superseded["state_version"] == 2

    assert _reanalysis_counts(review_state) == (1, 1, 1)
    assert _report_identity_row(review_state, report_id) == before_report_identity
    assert _model_row(review_state) == before_model

    old_bundle = report_review.get_review_bundle(
        project_id=review_state["project_id"],
        report_version_id=report_id,
    )
    assert old_bundle["report_version"]["lifecycle"] == "superseded"
    assert old_bundle["ai_raw"]["content"] == review_state["model_result"]["validated_result"]
    assert old_bundle["ai_raw"]["validated_result_hash"] == report["validated_result_hash"]


def test_t15_reanalysis_exact_replay_is_idempotent_and_does_not_duplicate_task(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    first = _reanalyze(review_state, report_id)
    replay = _reanalyze(review_state, report_id)

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["reanalysis_request"] == first["reanalysis_request"]
    assert replay["replacement_task"]["id"] == first["replacement_task"]["id"]
    assert replay["replacement_task"]["current_attempt_id"] == first["replacement_task"]["current_attempt_id"]
    assert _reanalysis_counts(review_state) == (1, 1, 1)
    assert _report_state_row(review_state, report_id) == ("superseded", 2)


def test_t16_reanalysis_same_key_or_same_report_drift_is_conflict_without_second_chain(
    review_state,
):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    _reanalyze(review_state, report_id)

    with pytest.raises(HTTPException) as caught:
        _reanalyze(
            review_state,
            report_id,
            corrected_truth="不同更正内容。",
        )
    assert _code(caught) == "REPORT_REANALYSIS_IDEMPOTENCY_CONFLICT"

    with pytest.raises(HTTPException) as source_caught:
        _reanalyze(
            review_state,
            report_id,
            correction_source="不同来源说明。",
        )
    assert _code(source_caught) == "REPORT_REANALYSIS_IDEMPOTENCY_CONFLICT"
    assert _reanalysis_counts(review_state) == (1, 1, 1)
    assert _report_state_row(review_state, report_id) == ("superseded", 2)


def test_t17_stale_reanalysis_is_zero_write_and_leaves_report_reviewable(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]

    with pytest.raises(HTTPException) as caught:
        _reanalyze(
            review_state,
            report_id,
            expected_report_state_version=2,
        )
    assert _code(caught) == "REPORT_REANALYSIS_STALE"
    assert _reanalysis_counts(review_state) == (0, 0, 0)
    assert _report_state_row(review_state, report_id) == ("pending_review", 1)


def test_t18_reanalysis_transaction_fault_rolls_back_task_request_and_supersede(
    review_state, monkeypatch
):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    before_model = _model_row(review_state)
    before_report_identity = _report_identity_row(review_state, report_id)
    real_create = report_generation_tasks.create_report_regeneration_task_in_transaction

    def fail_after_task(**kwargs):
        real_create(**kwargs)
        raise report_review._stored_invalid("fault after regenerate task admission")

    monkeypatch.setattr(
        report_generation_tasks,
        "create_report_regeneration_task_in_transaction",
        fail_after_task,
    )

    with pytest.raises(HTTPException) as caught:
        _reanalyze(review_state, report_id)
    assert _code(caught) == "REPORT_REVIEW_STORED_INVALID"
    assert _reanalysis_counts(review_state) == (0, 0, 0)
    assert _report_state_row(review_state, report_id) == ("pending_review", 1)
    assert _report_identity_row(review_state, report_id) == before_report_identity
    assert _model_row(review_state) == before_model


def test_t19_reanalysis_request_is_append_only_and_old_report_hashes_stay_guarded(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    result = _reanalyze(review_state, report_id)
    request_id = result["reanalysis_request"]["reanalysis_request_id"]

    with sqlite3.connect(review_state["db_path"]) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE report_reanalysis_requests SET corrected_truth = 'mutated' WHERE id = ?",
                (request_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM report_reanalysis_requests WHERE id = ?",
                (request_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE report_versions SET report_content_hash = ? WHERE id = ?",
                ("0" * 64, report_id),
            )


def test_t20_reanalysis_durable_read_closes_request_task_and_old_history(review_state):
    report_id = _materialize(review_state)["report_version"]["report_version_id"]
    created = _reanalyze(review_state, report_id)

    reread = report_review.get_reanalysis_request(
        project_id=review_state["project_id"],
        report_version_id=report_id,
    )
    assert reread["reanalysis_request"] == created["reanalysis_request"]
    assert reread["replacement_task"]["id"] == created["replacement_task"]["id"]
    assert reread["replacement_task"]["task_type"] == "daily_report_regenerate"
    assert reread["source_report_version"]["lifecycle"] == "superseded"
    assert reread["source_report_version"]["evidence_snapshot_id"] == review_state["snapshot_id"]


def test_t21_slice2_sources_have_no_provider_transport_or_execution_call_surface():
    forbidden_imports = {"requests", "httpx", "socket", "smtplib", "subprocess"}
    for module in (report_review, report_generation_tasks):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported.isdisjoint(forbidden_imports)
        assert "deepseek_transport" not in source
        assert "model_execution_orchestr" not in source
        assert "provider_send" not in source
