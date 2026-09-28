import sqlite3

import pytest
from fastapi import HTTPException

from app import report_approval


H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
H6 = "6" * 64
H7 = "7" * 64
H8 = "8" * 64
H9 = "9" * 64
HA = "a" * 64
HB = "b" * 64
HC = "c" * 64
HD = "d" * 64
HE = "e" * 64
HF = "f" * 64


@pytest.fixture()
def approval_env(tmp_path, monkeypatch):
    db_path = tmp_path / "approval.sqlite3"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE report_versions (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                lifecycle TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                report_content_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO report_versions VALUES (3, 1, 1, 'pending_review', 1, ?)",
            (H1,),
        )
        conn.commit()

    bundle = {
        "project_id": 1,
        "report_version": {
            "report_version_id": 3,
            "project_id": 1,
            "version_no": 1,
            "state_version": 1,
            "lifecycle": "pending_review",
            "report_content_hash": H1,
            "evidence_snapshot_id": 11,
            "evidence_snapshot_hash": H2,
            "model_execution_result_id": 17,
            "execution_result_hash": H3,
            "model_call_id": 19,
            "call_identity_hash": H4,
        },
        "ai_raw": {
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "model_version": "DeepSeek-V4.1-Flash",
            "actual_model": "deepseek-flash",
            "provider_runtime_fingerprint": "runtime-deepseek-flash-0731",
        },
        "current_supplement": None,
    }
    snapshot = {
        "project_repository_url": "https://github.com/example/project.git",
        "prd_id": 7,
        "prd_source_hash": H5,
        "prd_parsed_hash": H6,
        "prd_structured_hash": H7,
        "prd_document_fingerprint": H8,
        "profile_id": 13,
        "profile_content_hash": H9,
    }
    model_call = {
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "benchmark-pack/1.0",
        "qualification_hash": HA,
        "authorization_hash": HB,
    }
    closure = {
        "state": "passed",
        "candidate_hash": HC,
        "current_authority_hash": HD,
        "git_snapshot_id": 29,
        "git_facts_hash": HE,
        "git_branch": "main",
        "git_from_commit": "a" * 40,
        "git_to_commit": "b" * 40,
        "snapshot": snapshot,
        "model_call": model_call,
        "current_authority": {
            "current_profile": {
                "id": 13,
                "version_no": 4,
                "source_prd_id": 7,
                "content_hash": H9,
            }
        },
        "checks": [],
        "blockers": [],
    }
    validation = {
        "validation_result_id": 23,
        "project_id": 1,
        "report_version_id": 3,
        "report_state_version": 1,
        "report_content_hash": H1,
        "supplement_version_id": None,
        "supplement_content_hash": None,
        "evidence_snapshot_id": 11,
        "evidence_snapshot_hash": H2,
        "git_snapshot_id": 29,
        "git_facts_hash": HE,
        "model_execution_result_id": 17,
        "execution_result_hash": H3,
        "model_call_id": 19,
        "call_identity_hash": H4,
        "candidate_hash": HC,
        "current_authority_hash": HD,
        "state": "passed",
        "checks": [],
        "blockers": [],
        "result_hash": HF,
    }
    monkeypatch.setattr(report_approval, "get_review_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        report_approval.report_validation_runtime,
        "create_validation_result",
        lambda **_kwargs: {"validation_result": validation, "created": False},
    )
    monkeypatch.setattr(
        report_approval,
        "_reclose_under_lock",
        lambda **_kwargs: closure,
    )
    monkeypatch.setattr(
        report_approval,
        "build_plain_language_change_summary_from_snapshot",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        report_approval,
        "materialize_approved_anxin_board_v3_in_transaction",
        lambda **_kwargs: {"id": 501, "project_id": 1},
    )
    return db_path, bundle, closure, validation


def approve():
    return report_approval.create_report_approval_snapshot(
        project_id=1,
        report_version_id=3,
        expected_report_state_version=1,
        confirmed_by="张经理",
        confirmed_timezone="Asia/Shanghai",
        confirmed_utc_offset_minutes=480,
        human_confirmed=True,
        idempotency_key="approval-3-1",
    )


def test_v3_materialization_failure_rolls_back_approval_and_report_transition(approval_env, monkeypatch):
    db_path, _bundle, _closure, _validation = approval_env

    def fail_materialization(**_kwargs):
        raise report_approval.AnxinBoardV3MaterializationError("blocked")

    monkeypatch.setattr(
        report_approval,
        "materialize_approved_anxin_board_v3_in_transaction",
        fail_materialization,
    )
    with pytest.raises(HTTPException) as caught:
        approve()
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "ANXIN_BOARD_V3_MATERIALIZATION_BLOCKED"

    with sqlite3.connect(db_path) as conn:
        report = conn.execute(
            "SELECT lifecycle, state_version FROM report_versions WHERE id = 3"
        ).fetchone()
        approval_count = conn.execute(
            "SELECT COUNT(*) FROM report_approval_snapshots"
        ).fetchone()[0]
    assert report == ("pending_review", 1)
    assert approval_count == 0

def test_valid_approval_is_append_only_and_atomically_marks_report_approved(approval_env):
    db_path, _bundle, _closure, _validation = approval_env
    result = approve()
    snapshot = result["approval_snapshot"]
    assert result["created"] is True
    assert snapshot["confirmed_by"] == "张经理"
    assert snapshot["report_state_version_before"] == 1
    assert snapshot["report_state_version_after"] == 2
    assert snapshot["report_version_no"] == 1
    assert snapshot["git_branch"] == "main"
    assert snapshot["git_from_commit"] == "a" * 40
    assert snapshot["git_to_commit"] == "b" * 40
    assert snapshot["profile_version_no"] == 4
    assert snapshot["provider_runtime_fingerprint"] == "runtime-deepseek-flash-0731"
    assert snapshot["benchmark_sample_pack_version"] == "benchmark-pack/1.0"
    assert snapshot["human_acknowledged"] is True
    assert snapshot["supplement_provided_by"] is None
    assert snapshot["supplement_provided_at"] is None
    assert len(snapshot["approval_snapshot_hash"]) == 64
    for forbidden in (
        "template_version",
        "template_source_filename",
        "template_sha256",
        "disclaimer_version",
    ):
        assert forbidden not in snapshot

    with sqlite3.connect(db_path) as conn:
        report = conn.execute(
            "SELECT lifecycle, state_version FROM report_versions WHERE id = 3"
        ).fetchone()
        assert report == ("approved", 2)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(report_approval_snapshots)").fetchall()
        }
        assert "template_version" not in columns
        assert "template_source_filename" not in columns
        assert "template_sha256" not in columns
        assert "disclaimer_version" not in columns
        with pytest.raises(sqlite3.IntegrityError, match="APPROVAL_SNAPSHOT_APPEND_ONLY"):
            conn.execute("UPDATE report_approval_snapshots SET confirmed_by = 'other' WHERE report_version_id = 3")
        with pytest.raises(sqlite3.IntegrityError, match="APPROVAL_SNAPSHOT_APPEND_ONLY"):
            conn.execute("DELETE FROM report_approval_snapshots WHERE report_version_id = 3")


def test_same_idempotency_key_replays_exact_snapshot_without_second_transition(approval_env):
    first = approve()
    second = approve()
    assert second["created"] is False
    assert second["approval_snapshot"]["approval_snapshot_hash"] == first["approval_snapshot"]["approval_snapshot_hash"]
    assert second["approval_snapshot"]["approval_snapshot_id"] == first["approval_snapshot"]["approval_snapshot_id"]


def test_stored_query_columns_must_match_canonical_snapshot_json(approval_env):
    db_path, _bundle, _closure, _validation = approval_env
    approve()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_report_approval_snapshots_no_update")
        conn.execute(
            "UPDATE report_approval_snapshots SET project_id = 2 WHERE report_version_id = 3"
        )
        conn.commit()

    with pytest.raises(HTTPException) as error:
        report_approval.get_report_approval_snapshot(project_id=2, report_version_id=3)
    assert error.value.detail["code"] == "REPORT_APPROVAL_STORED_INVALID"


def test_invalid_or_meaningless_confirmer_is_blocked_before_state_change(approval_env):
    db_path, _bundle, _closure, _validation = approval_env
    for bad in ("", "   ", "!!!", "\n\t"):
        with pytest.raises(HTTPException) as error:
            report_approval.create_report_approval_snapshot(
                project_id=1,
                report_version_id=3,
                expected_report_state_version=1,
                confirmed_by=bad,
                confirmed_timezone="Asia/Shanghai",
                confirmed_utc_offset_minutes=480,
                human_confirmed=True,
                idempotency_key="approval-bad",
            )
        assert error.value.detail["code"] == "REPORT_APPROVAL_INPUT_INVALID"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT lifecycle, state_version FROM report_versions WHERE id = 3").fetchone() == ("pending_review", 1)


def test_reclosed_blocker_prevents_snapshot_and_report_transition(approval_env, monkeypatch):
    db_path, _bundle, closure, _validation = approval_env
    blocked = dict(closure)
    blocked["state"] = "blocked"
    blocked["blockers"] = [{"code": "DRIFT", "message": "changed"}]
    monkeypatch.setattr(report_approval, "_reclose_under_lock", lambda **_kwargs: blocked)
    with pytest.raises(HTTPException) as error:
        approve()
    assert error.value.detail["code"] == "REPORT_APPROVAL_BLOCKED"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT lifecycle, state_version FROM report_versions WHERE id = 3").fetchone() == ("pending_review", 1)
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_approval_snapshots'").fetchone()
        assert table == (1,)
        assert conn.execute("SELECT COUNT(*) FROM report_approval_snapshots").fetchone()[0] == 0


def test_missing_actual_model_version_blocks_formal_approval(approval_env):
    _db_path, bundle, _closure, _validation = approval_env
    bundle["ai_raw"]["model_version"] = ""
    with pytest.raises(HTTPException) as error:
        approve()
    assert error.value.detail["code"] == "REPORT_APPROVAL_BLOCKED"


def test_selected_supplement_provenance_is_bound_into_snapshot(approval_env):
    _db_path, bundle, _closure, validation = approval_env
    bundle["current_supplement"] = {
        "supplement_version_id": 41,
        "version_no": 1,
        "content_hash": HE,
        "provided_by": "李经理",
        "provided_at": "2026-09-03T12:00:00Z",
        "provided_timezone": "Asia/Shanghai",
        "source_type": "pm_external_fact",
    }
    validation["supplement_version_id"] = 41
    validation["supplement_content_hash"] = HE
    snapshot = approve()["approval_snapshot"]
    assert snapshot["supplement_version_id"] == 41
    assert snapshot["supplement_content_hash"] == HE
    assert snapshot["supplement_provided_by"] == "李经理"
    assert snapshot["supplement_provided_at"] == "2026-09-03T12:00:00Z"
    assert snapshot["supplement_provided_timezone"] == "Asia/Shanghai"
    assert snapshot["supplement_source_type"] == "pm_external_fact"


def test_get_approval_fails_closed_when_approved_report_mirror_is_inconsistent(approval_env):
    db_path, _bundle, _closure, _validation = approval_env
    approve()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE report_versions SET lifecycle = 'pending_review' WHERE id = 3")
        conn.commit()
    with pytest.raises(HTTPException) as error:
        report_approval.get_report_approval_snapshot(project_id=1, report_version_id=3)
    assert error.value.detail["code"] == "REPORT_APPROVAL_STORED_INVALID"


def test_get_approval_is_pure_read_before_first_write(tmp_path, monkeypatch):
    db_path = tmp_path / "pure-read.sqlite3"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE report_versions (id INTEGER PRIMARY KEY)")
        conn.commit()
    assert report_approval.get_report_approval_snapshot(project_id=1, report_version_id=3) is None
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_approval_snapshots'").fetchone() is None
