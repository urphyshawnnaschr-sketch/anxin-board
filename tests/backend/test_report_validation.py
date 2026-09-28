import sqlite3

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import report_validation
from app.report_review_api import CreateReportValidationRequest


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
GIT_SNAPSHOT_ID = 29


@pytest.fixture()
def validation_env(tmp_path, monkeypatch):
    db_path = tmp_path / "validation.sqlite3"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                git_url TEXT,
                branch TEXT,
                version INTEGER
            );
            CREATE TABLE analysis_lineages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                sequence_no INTEGER,
                branch TEXT,
                status TEXT
            );
            CREATE TABLE prd_versions (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                version_no INTEGER,
                status TEXT,
                source_hash TEXT,
                parsed_hash TEXT,
                structured_hash TEXT,
                document_fingerprint TEXT
            );
            CREATE TABLE evidence_snapshots (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                git_snapshot_id INTEGER,
                analysis_lineage_id INTEGER,
                branch TEXT,
                project_repository_url TEXT,
                project_config_hash TEXT,
                git_facts_hash TEXT,
                prd_id INTEGER,
                prd_source_hash TEXT,
                prd_parsed_hash TEXT,
                prd_structured_hash TEXT,
                prd_document_fingerprint TEXT,
                profile_id INTEGER,
                profile_content_hash TEXT,
                snapshot_hash TEXT
            );
            CREATE TABLE report_versions (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                lifecycle TEXT,
                state_version INTEGER
            );
            CREATE TABLE report_supplement_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_version_id INTEGER,
                version_no INTEGER,
                content_hash TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?)",
            (1, "https://github.com/example/project.git", "main", 1),
        )
        conn.execute(
            "INSERT INTO analysis_lineages VALUES (?, ?, ?, ?, ?)",
            (5, 1, 1, "main", "active"),
        )
        conn.execute(
            "INSERT INTO prd_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (7, 1, 1, "parse_confirmed", H1, H2, H3, H4),
        )
        conn.execute(
            "INSERT INTO evidence_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                11,
                1,
                GIT_SNAPSHOT_ID,
                5,
                "main",
                "https://github.com/example/project.git",
                H5,
                HD,
                7,
                H1,
                H2,
                H3,
                H4,
                13,
                H6,
                H7,
            ),
        )
        conn.execute(
            "INSERT INTO report_versions VALUES (?, ?, ?, ?)",
            (17, 1, "pending_review", 1),
        )
        conn.commit()

    bundle = {
        "schema_version": "report_review_bundle_v1",
        "project_id": 1,
        "report_version": {
            "report_version_id": 17,
            "schema_version": "report_version_v1",
            "project_id": 1,
            "version_no": 1,
            "parent_report_version_id": None,
            "model_execution_result_id": 19,
            "execution_result_hash": H8,
            "formal_response_hash": H9,
            "validated_result_hash": HA,
            "model_call_id": 23,
            "call_identity_hash": HB,
            "evidence_snapshot_id": 11,
            "evidence_snapshot_hash": H7,
            "report_content_hash": HA,
            "lifecycle": "pending_review",
            "state_version": 1,
            "created_at": "2026-09-01T00:00:00+00:00",
        },
        "ai_raw": {
            "model_execution_result_id": 19,
            "execution_result_hash": H8,
            "formal_response_hash": H9,
            "validated_result_hash": HA,
            "model_call_id": 23,
            "call_identity_hash": HB,
            "snapshot_id": 11,
            "local_task_id": "task-1",
            "task_type": "daily_report_generate",
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "model_version": "DeepSeek-V4.1-Flash",
            "actual_model": "deepseek-flash",
            "provider_runtime_fingerprint": "runtime-1",
            "content": {
                "plain_summary": "summary",
                "feature_progress": [
                    {
                        "feature": "F1",
                        "stage": "开发中",
                        "source_type": "ai_analysis",
                        "implementation_scope": "后端",
                        "evidence_ids": ["git:file:1:001"],
                    }
                ],
                "code_change_summary": [],
                "test_evidence": [],
                "risks": [],
                "unknown_items": [],
                "source_warnings": [],
            },
        },
        "evidence_snapshot": {"snapshot_id": 11, "snapshot_hash": H7},
        "git_facts": {
            "git_snapshot_id": GIT_SNAPSHOT_ID,
            "git_facts_hash": HD,
        },
        "evidence_refs": [],
        "current_supplement": None,
    }
    model_call = {
        "model_call_id": 23,
        "project_id": 1,
        "snapshot_id": 11,
        "snapshot_hash": H7,
        "candidate_set_hash": HE,
        "call_identity_hash": HB,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "model_version": "DeepSeek-V4.1-Flash",
        "rule_version": "report-rule-v1",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "bench-v1",
        "qualification_status": "qualified",
        "qualification_hash": HC,
        "authorization_authorized": 1,
        "authorization_valid": 1,
        "authorization_hash": H5,
    }
    candidate = {
        "schema_version": "context_candidate_set_v1",
        "snapshot_id": 11,
        "snapshot_hash": H7,
        "candidate_set_hash": HE,
        "project_id": 1,
        "range": {
            "git_snapshot_id": GIT_SNAPSHOT_ID,
            "git_facts_hash": HD,
        },
    }
    model_result = {
        "model_result_id": 19,
        "project_id": 1,
        "snapshot_id": 11,
        "execution_result_hash": H8,
        "model_call_id": 23,
        "call_identity_hash": HB,
        "validated_result_hash": HA,
    }

    monkeypatch.setattr(report_validation, "get_review_bundle", lambda **_: bundle)
    monkeypatch.setattr(report_validation, "get_model_call", lambda _: model_call)
    monkeypatch.setattr(
        report_validation, "build_context_candidate_set", lambda _: candidate
    )
    monkeypatch.setattr(
        report_validation, "get_model_execution_result", lambda _: model_result
    )
    monkeypatch.setattr(
        report_validation,
        "read_current_confirmed_project_profile",
        lambda project_id, conn=None: {
            "id": 13,
            "project_id": project_id,
            "version_no": 1,
            "source_prd_id": 7,
            "status": "confirmed",
            "content_hash": H6,
            "content": {},
        },
    )
    return db_path, bundle, model_call


def _validate():
    return report_validation.create_validation_result(
        project_id=1,
        report_version_id=17,
        expected_report_state_version=1,
    )


def test_exact_candidate_replays_one_passed_result(validation_env):
    first = _validate()
    second = _validate()

    assert first["created"] is True
    result = first["validation_result"]
    assert result["state"] == "passed"
    assert result["blockers"] == []
    assert result["git_snapshot_id"] == GIT_SNAPSHOT_ID
    assert result["git_facts_hash"] == HD
    assert second["created"] is False
    assert second["validation_result"]["validation_result_id"] == result["validation_result_id"]
    assert second["validation_result"]["result_hash"] == result["result_hash"]


def test_rebuilt_candidate_set_hash_must_match_frozen_model_input(
    validation_env, monkeypatch
):
    db_path, _, _ = validation_env
    passed = _validate()["validation_result"]
    assert passed["state"] == "passed"

    monkeypatch.setattr(
        report_validation,
        "build_context_candidate_set",
        lambda _: {
            "schema_version": "context_candidate_set_v1",
            "snapshot_id": 11,
            "snapshot_hash": H7,
            "candidate_set_hash": HF,
            "project_id": 1,
            "range": {
                "git_snapshot_id": GIT_SNAPSHOT_ID,
                "git_facts_hash": HD,
            },
        },
    )

    with pytest.raises(HTTPException) as exc:
        _validate()
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "REPORT_VALIDATION_STORED_INVALID"

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, state FROM report_validation_results ORDER BY id"
        ).fetchall()
    assert rows == [(passed["validation_result_id"], "passed")]


def test_current_project_git_drift_appends_blocked_result(validation_env):
    db_path, _, _ = validation_env
    passed = _validate()["validation_result"]

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE projects SET branch = 'release', version = 2 WHERE id = 1")
        conn.commit()

    blocked = _validate()["validation_result"]
    assert blocked["state"] == "blocked"
    assert blocked["validation_result_id"] != passed["validation_result_id"]
    assert {item["code"] for item in blocked["blockers"]} >= {
        "PROJECT_GIT_AUTHORITY_DRIFT"
    }


def test_current_prd_or_profile_drift_blocks(validation_env, monkeypatch):
    db_path, _, _ = validation_env
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE prd_versions SET status = 'superseded' WHERE id = 7")
        conn.commit()
    monkeypatch.setattr(
        report_validation,
        "read_current_confirmed_project_profile",
        lambda project_id, conn=None: {
            "id": 99,
            "project_id": project_id,
            "version_no": 2,
            "source_prd_id": 99,
            "status": "confirmed",
            "content_hash": "f" * 64,
            "content": {},
        },
    )

    result = _validate()["validation_result"]
    codes = {item["code"] for item in result["blockers"]}
    assert result["state"] == "blocked"
    assert "CURRENT_PRD_AUTHORITY_DRIFT" in codes
    assert "CURRENT_PROFILE_AUTHORITY_DRIFT" in codes


def test_supplement_requires_exact_bound_contradiction_evidence(validation_env):
    _, bundle, _ = validation_env
    bundle["current_supplement"] = {
        "supplement_version_id": 31,
        "version_no": 1,
        "content_hash": "f" * 64,
        "source_type": "pm_external_fact",
        "provided_by": "pm",
        "provided_at": "2026-09-01T00:01:00+00:00",
        "provided_timezone": "Asia/Shanghai",
    }
    db_path, _, _ = validation_env
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO report_supplement_versions (id, report_version_id, version_no, content_hash) VALUES (?, ?, ?, ?)",
            (31, 17, 1, "f" * 64),
        )
        conn.commit()

    result = _validate()["validation_result"]
    assert result["state"] == "blocked"
    assert "REPORT_CONTRADICTION_CHECK_REQUIRED" in {
        item["code"] for item in result["blockers"]
    }


def test_new_supplement_after_bundle_read_is_stale(validation_env):
    db_path, _, _ = validation_env
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO report_supplement_versions (report_version_id, version_no, content_hash) VALUES (?, ?, ?)",
            (17, 1, "f" * 64),
        )
        conn.commit()

    with pytest.raises(HTTPException) as exc:
        _validate()
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "REPORT_VALIDATION_STALE_CANDIDATE"


def test_superseded_bundle_lifecycle_is_not_validatable(validation_env):
    db_path, bundle, _ = validation_env
    bundle["report_version"]["lifecycle"] = "superseded"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE report_versions SET lifecycle = 'superseded' WHERE id = 17")
        conn.commit()

    with pytest.raises(HTTPException) as exc:
        _validate()
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "REPORT_VALIDATION_STALE_CANDIDATE"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_validation_results").fetchone()[0] == 0


def test_approved_bundle_lifecycle_is_not_validatable(validation_env):
    db_path, bundle, _ = validation_env
    bundle["report_version"]["lifecycle"] = "approved"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE report_versions SET lifecycle = 'approved' WHERE id = 17")
        conn.commit()

    with pytest.raises(HTTPException) as exc:
        _validate()
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "REPORT_VALIDATION_STALE_CANDIDATE"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_validation_results").fetchone()[0] == 0


def test_lifecycle_change_after_bundle_read_is_rechecked_under_write_lock(validation_env):
    db_path, bundle, _ = validation_env
    assert bundle["report_version"]["lifecycle"] == "pending_review"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE report_versions SET lifecycle = 'superseded' WHERE id = 17")
        conn.commit()

    with pytest.raises(HTTPException) as exc:
        _validate()
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "REPORT_VALIDATION_STALE_CANDIDATE"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_validation_results").fetchone()[0] == 0


def test_stale_report_state_version_fails_before_write(validation_env):
    with pytest.raises(HTTPException) as exc:
        report_validation.create_validation_result(
            project_id=1,
            report_version_id=17,
            expected_report_state_version=2,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "REPORT_VALIDATION_STALE_CANDIDATE"


def test_validation_rows_are_append_only(validation_env):
    db_path, _, _ = validation_env
    result_id = _validate()["validation_result"]["validation_result_id"]
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE report_validation_results SET state = 'blocked' WHERE id = ?",
                (result_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM report_validation_results WHERE id = ?", (result_id,))


def test_latest_validation_get_is_pure_read_before_first_write(validation_env):
    db_path, _, _ = validation_env
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_validation_results'"
        ).fetchone() is None

    assert report_validation.get_latest_validation_result(
        project_id=1, report_version_id=17
    ) is None

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_validation_results'"
        ).fetchone() is None


def test_validation_http_payload_has_no_client_pass_override():
    payload = CreateReportValidationRequest(expected_report_state_version=1)
    assert payload.expected_report_state_version == 1
    with pytest.raises(ValidationError):
        CreateReportValidationRequest(
            expected_report_state_version=1,
            validation_passed=True,
        )
