import sqlite3

import pytest

from app import anxin_board_report_store as report_store
from app import report_approval
from app.db import get_connection


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


def _approval_snapshot() -> dict[str, object]:
    return {
        "schema_version": "approval_snapshot_v1",
        "project_id": 1,
        "report_version_id": 3,
        "report_version_no": 1,
        "report_state_version_before": 1,
        "report_state_version_after": 2,
        "validation_result_id": 23,
        "validation_result_hash": HF,
        "candidate_hash": HC,
        "current_authority_hash": HD,
        "report_content_hash": H1,
        "supplement_version_id": None,
        "supplement_content_hash": None,
        "supplement_provided_by": None,
        "supplement_provided_at": None,
        "supplement_provided_timezone": None,
        "supplement_source_type": None,
        "evidence_snapshot_id": 11,
        "evidence_snapshot_hash": H2,
        "git_snapshot_id": 29,
        "git_facts_hash": HE,
        "git_branch": "main",
        "git_from_commit": "a" * 40,
        "git_to_commit": "b" * 40,
        "project_repository_url": "https://github.com/example/project.git",
        "prd_id": 7,
        "prd_source_hash": H5,
        "prd_parsed_hash": H6,
        "prd_structured_hash": H7,
        "prd_document_fingerprint": H8,
        "profile_id": 13,
        "profile_version_no": 4,
        "profile_content_hash": H9,
        "model_execution_result_id": 17,
        "execution_result_hash": H3,
        "model_call_id": 19,
        "call_identity_hash": H4,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "actual_model": "deepseek-flash",
        "provider_runtime_fingerprint": "runtime-deepseek-flash-0731",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "benchmark-pack/1.0",
        "qualification_hash": HA,
        "authorization_hash": HB,
        "confirmed_by": "张经理",
        "confirmed_at": "2026-09-04T05:00:00+00:00",
        "confirmed_timezone": "Asia/Shanghai",
        "confirmed_utc_offset_minutes": 480,
        "human_acknowledged": True,
    }


def test_page08_v3_reader_recloses_all_approval_snapshot_query_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "approval-row-integrity.sqlite3"
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
            "INSERT INTO report_versions VALUES (3, 1, 1, 'approved', 2, ?)",
            (H1,),
        )
        conn.commit()

    report_approval._ensure_schema()
    snapshot = _approval_snapshot()
    snapshot_json = report_approval._canonical_json(snapshot)
    snapshot_hash = report_approval._stable_hash(snapshot)
    stored = {
        **snapshot,
        "idempotency_key": "approval-row-integrity",
        "snapshot_json": snapshot_json,
        "approval_snapshot_hash": snapshot_hash,
        "created_at": "2026-09-04T05:00:00+00:00",
    }

    with get_connection() as conn:
        columns = ", ".join(stored)
        placeholders = ", ".join("?" for _ in stored)
        cursor = conn.execute(
            f"INSERT INTO report_approval_snapshots ({columns}) VALUES ({placeholders})",
            tuple(stored.values()),
        )
        approval_snapshot_id = int(cursor.lastrowid)
        conn.commit()

    with get_connection() as conn:
        valid = report_store._read_approval_snapshot_for_v3(
            conn,
            project_id=1,
            approval_snapshot_id=approval_snapshot_id,
            approval_snapshot_hash=snapshot_hash,
        )
    assert valid["provider"] == "deepseek"

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_report_approval_snapshots_no_update")
        conn.execute(
            "UPDATE report_approval_snapshots SET provider = 'tampered-provider' WHERE id = ?",
            (approval_snapshot_id,),
        )
        conn.commit()

    with get_connection() as conn:
        with pytest.raises(report_store.AnxinBoardReportStoredInvalidError):
            report_store._read_approval_snapshot_for_v3(
                conn,
                project_id=1,
                approval_snapshot_id=approval_snapshot_id,
                approval_snapshot_hash=snapshot_hash,
            )
