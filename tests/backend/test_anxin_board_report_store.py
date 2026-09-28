from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path

import pytest

import app.anxin_board_report_store as report_store
from app.anxin_board_report import (
    AnxinBoardReportError,
    MODULES,
    build_anxin_board_report,
    validate_anxin_board_report,
)
from app.anxin_board_report_store import (
    AnxinBoardReportHistoryInputError,
    AnxinBoardReportProjectMismatchError,
    AnxinBoardReportProjectNotFoundError,
    AnxinBoardReportStoredInvalidError,
    load_anxin_board_report_history,
    load_latest_anxin_board_report,
    persist_anxin_board_report,
)
from app.client_stage_summary import build_client_stage_summary
from app.db import get_connection, init_db
from app.plain_language_change_summary import build_plain_language_change_summary


SQLITE_SIGNED_INTEGER_MAX = 2**63 - 1


class _StrictInt(int):
    pass


def _hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _evidence() -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "development_change_evidence_v1",
        "evidence_state": "complete",
        "has_change": True,
        "commit_count": 2,
        "changed_file_count": 3,
        "added_lines": 40,
        "deleted_lines": 10,
        "line_change_total": 50,
        "line_change_metric_role": "supporting_evidence_only",
        "production_change_file_count": 1,
        "test_change_file_count": 1,
        "ci_change_file_count": 1,
        "documentation_change_file_count": 0,
        "operations_change_file_count": 0,
        "other_change_file_count": 0,
        "affected_areas": [
            {"area": "backend", "file_count": 1},
            {"area": "backend_tests", "file_count": 1},
            {"area": "ci", "file_count": 1},
        ],
    }
    result["development_change_evidence_hash"] = _hash(result)
    return result


def _report(
    project_name: str,
    *,
    report_date: str = "2026-08-22",
    supplement: str = "同意大模型的日报。",
) -> dict[str, object]:
    modules = [
        build_client_stage_summary(module_name=name, stage="开发中")
        for _, name in MODULES
    ]
    daily_change = build_plain_language_change_summary(evidence=_evidence())
    return build_anxin_board_report(
        project_name=project_name,
        report_date=report_date,
        module_summaries=modules,
        daily_change=daily_change,
        manager_supplement=supplement,
    )


@pytest.fixture()
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    init_db()
    yield


def _create_project(name: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO projects (name, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, "draft", now, now),
        )
        return int(cursor.lastrowid)


def _report_rows(project_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM anxin_board_reports WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _seed_legacy_report(*, project_id: int, report: dict[str, object]) -> dict[str, object]:
    validated = validate_anxin_board_report(report)
    with get_connection() as conn:
        project = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise AnxinBoardReportProjectNotFoundError()
        if validated["project_name"] != project["name"]:
            raise AnxinBoardReportProjectMismatchError()
        row = conn.execute(
            "SELECT id, project_id, schema_version, report_date, report_hash, report_json, created_at "
            "FROM anxin_board_reports WHERE project_id = ? AND report_hash = ?",
            (project_id, validated["anxin_board_report_hash"]),
        ).fetchone()
        if row is None:
            created_at = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                "INSERT INTO anxin_board_reports "
                "(project_id, schema_version, report_date, report_hash, report_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    validated["schema_version"],
                    validated["report_date"],
                    validated["anxin_board_report_hash"],
                    json.dumps(validated, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT id, project_id, schema_version, report_date, report_hash, report_json, created_at "
                "FROM anxin_board_reports WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        assert row is not None
        stored = validate_anxin_board_report(json.loads(row["report_json"]))
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "schema_version": row["schema_version"],
            "report_date": row["report_date"],
            "report_hash": row["report_hash"],
            "created_at": row["created_at"],
            "report": stored,
        }


# Test-local fixture seam only. Production code remains report_store.persist_anxin_board_report.
persist_anxin_board_report = _seed_legacy_report


def _bomb_if_db_opened():
    raise AssertionError("history input guard must fail before database access")


def test_db_init_is_idempotent_and_creates_exact_report_table_and_latest_index(database):
    init_db()
    init_db()
    with get_connection() as conn:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(anxin_board_reports)")]
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(anxin_board_reports)")
        }
    assert columns == [
        "id",
        "project_id",
        "schema_version",
        "report_date",
        "report_hash",
        "report_json",
        "created_at",
    ]
    assert "ix_anxin_board_reports_project_latest" in indexes


def test_validator_returns_detached_copy_and_fails_closed_on_hash_or_nested_tamper():
    source = _report("验证项目")
    validated = validate_anxin_board_report(source)
    assert validated == source
    assert validated is not source
    assert validated["modules"] is not source["modules"]

    source["manager_supplement"] = "调用方事后篡改"
    assert validated["manager_supplement"] == "同意大模型的日报。"

    bad_final = deepcopy(validated)
    bad_final["anxin_board_report_hash"] = "0" * 64
    with pytest.raises(AnxinBoardReportError):
        validate_anxin_board_report(bad_final)

    bad_nested = deepcopy(validated)
    bad_nested["modules"][0]["client_stage_summary_hash"] = "1" * 64
    payload = {k: deepcopy(v) for k, v in bad_nested.items() if k != "anxin_board_report_hash"}
    bad_nested["anxin_board_report_hash"] = _hash(payload)
    with pytest.raises(AnxinBoardReportError):
        validate_anxin_board_report(bad_nested)


def test_valid_report_persists_and_latest_returns_exact_formal_payload(database):
    project_id = _create_project("安心看板项目")
    report = _report("安心看板项目")

    record = persist_anxin_board_report(project_id=project_id, report=report)
    latest = load_latest_anxin_board_report(project_id=project_id)

    assert record["project_id"] == project_id
    assert record["report_hash"] == report["anxin_board_report_hash"]
    assert record["report"] == report
    assert latest == report


def test_invalid_report_does_not_write(database):
    project_id = _create_project("无效报告项目")
    report = _report("无效报告项目")
    report["anxin_board_report_hash"] = "0" * 64

    with pytest.raises(AnxinBoardReportError):
        persist_anxin_board_report(project_id=project_id, report=report)
    assert _report_rows(project_id) == []


def test_missing_project_does_not_write(database):
    with pytest.raises(AnxinBoardReportProjectNotFoundError):
        persist_anxin_board_report(project_id=99999, report=_report("不存在项目"))
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM anxin_board_reports").fetchone()[0]
    assert count == 0


def test_project_name_mismatch_does_not_write(database):
    project_id = _create_project("项目 A")
    with pytest.raises(AnxinBoardReportProjectMismatchError):
        persist_anxin_board_report(project_id=project_id, report=_report("项目 B"))
    assert _report_rows(project_id) == []


def test_new_legacy_write_requires_approval_and_does_not_write(database):
    project_id = _create_project("旧写入口项目")
    report = _report("旧写入口项目")

    with pytest.raises(report_store.AnxinBoardReportApprovalRequiredError):
        report_store.persist_anxin_board_report(project_id=project_id, report=report)
    assert _report_rows(project_id) == []



def test_same_project_same_hash_is_idempotent_without_timestamp_refresh(database):
    project_id = _create_project("幂等项目")
    report = _report("幂等项目")

    first = persist_anxin_board_report(project_id=project_id, report=report)
    second = persist_anxin_board_report(project_id=project_id, report=deepcopy(report))

    assert second == first
    assert len(_report_rows(project_id)) == 1


def test_same_day_different_hash_keeps_both_versions_and_latest_is_newest(database):
    project_id = _create_project("纠正版项目")
    first = _report("纠正版项目", supplement="今天按原计划推进。")
    second = _report("纠正版项目", supplement="今天复核后按新结论推进。")

    first_record = persist_anxin_board_report(project_id=project_id, report=first)
    second_record = persist_anxin_board_report(project_id=project_id, report=second)
    rows = _report_rows(project_id)

    assert len(rows) == 2
    assert first_record["id"] < second_record["id"]
    assert rows[0]["report_hash"] == first["anxin_board_report_hash"]
    assert rows[1]["report_hash"] == second["anxin_board_report_hash"]
    assert load_latest_anxin_board_report(project_id=project_id) == second


def test_different_projects_are_strictly_isolated(database):
    first_id = _create_project("项目一")
    second_id = _create_project("项目二")
    first = _report("项目一")
    second = _report("项目二")

    persist_anxin_board_report(project_id=first_id, report=first)
    persist_anxin_board_report(project_id=second_id, report=second)

    assert load_latest_anxin_board_report(project_id=first_id) == first
    assert load_latest_anxin_board_report(project_id=second_id) == second
    assert len(_report_rows(first_id)) == 1
    assert len(_report_rows(second_id)) == 1


def test_no_report_returns_none(database):
    project_id = _create_project("空项目")
    assert load_latest_anxin_board_report(project_id=project_id) is None


def test_stored_json_or_row_hash_tamper_fails_closed(database):
    project_id = _create_project("篡改项目")
    report = _report("篡改项目")
    persist_anxin_board_report(project_id=project_id, report=report)

    with get_connection() as conn:
        conn.execute(
            "UPDATE anxin_board_reports SET report_json = ? WHERE project_id = ?",
            ("{not-json", project_id),
        )
    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_latest_anxin_board_report(project_id=project_id)

    with get_connection() as conn:
        conn.execute(
            "UPDATE anxin_board_reports SET report_json = ?, report_hash = ? WHERE project_id = ?",
            (json.dumps(report, ensure_ascii=False, separators=(",", ":")), "f" * 64, project_id),
        )
    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_latest_anxin_board_report(project_id=project_id)


def test_project_binding_drift_fails_closed(database):
    project_id = _create_project("原项目名")
    persist_anxin_board_report(project_id=project_id, report=_report("原项目名"))

    with get_connection() as conn:
        conn.execute("UPDATE projects SET name = ? WHERE id = ?", ("新项目名", project_id))

    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_latest_anxin_board_report(project_id=project_id)


def test_corrupt_latest_never_falls_back_to_older_version(database):
    project_id = _create_project("不回退项目")
    older = _report("不回退项目", supplement="第一版结论。")
    newest = _report("不回退项目", supplement="第二版结论。")
    persist_anxin_board_report(project_id=project_id, report=older)
    newest_record = persist_anxin_board_report(project_id=project_id, report=newest)

    with get_connection() as conn:
        conn.execute(
            "UPDATE anxin_board_reports SET report_json = ? WHERE id = ?",
            ("{}", newest_record["id"]),
        )

    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_latest_anxin_board_report(project_id=project_id)


def test_history_signature_is_keyword_only_with_frozen_defaults():
    signature = inspect.signature(load_anxin_board_report_history)
    assert list(signature.parameters) == ["project_id", "before_id", "limit"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert signature.parameters["before_id"].default is None
    assert signature.parameters["limit"].default == 20


def test_history_returns_metadata_records_in_descending_id_order(database):
    project_id = _create_project("历史项目")
    reports = [
        _report("历史项目", report_date="2026-08-20", supplement="第一版。"),
        _report("历史项目", report_date="2026-08-21", supplement="第二版。"),
        _report("历史项目", report_date="2026-08-22", supplement="第三版。"),
    ]
    records = [persist_anxin_board_report(project_id=project_id, report=item) for item in reports]

    page = load_anxin_board_report_history(project_id=project_id)

    assert [item["id"] for item in page] == [item["id"] for item in reversed(records)]
    assert [item["report"] for item in page] == list(reversed(reports))
    assert all(item["project_id"] == project_id for item in page)
    assert all(item["report_hash"] == item["report"]["anxin_board_report_hash"] for item in page)
    assert all(item["schema_version"] == item["report"]["schema_version"] for item in page)
    assert all(item["report_date"] == item["report"]["report_date"] for item in page)
    assert all(type(item["created_at"]) is str and item["created_at"] for item in page)


def test_history_cursor_is_exclusive_without_duplicates(database):
    project_id = _create_project("游标项目")
    records = [
        persist_anxin_board_report(
            project_id=project_id,
            report=_report("游标项目", supplement=f"第 {index} 版。"),
        )
        for index in range(1, 5)
    ]

    first_page = load_anxin_board_report_history(project_id=project_id, limit=2)
    second_page = load_anxin_board_report_history(
        project_id=project_id,
        before_id=first_page[-1]["id"],
        limit=2,
    )

    expected_ids = [record["id"] for record in reversed(records)]
    assert [item["id"] for item in first_page] == expected_ids[:2]
    assert [item["id"] for item in second_page] == expected_ids[2:]
    assert {item["id"] for item in first_page}.isdisjoint(
        {item["id"] for item in second_page}
    )


def test_history_empty_and_cursor_before_oldest_return_empty_page(database):
    project_id = _create_project("空历史项目")
    assert load_anxin_board_report_history(project_id=project_id) == []

    oldest = persist_anxin_board_report(
        project_id=project_id,
        report=_report("空历史项目", supplement="唯一一版。"),
    )
    assert load_anxin_board_report_history(
        project_id=project_id,
        before_id=oldest["id"],
    ) == []


@pytest.mark.parametrize(
    "value",
    [0, 51, True, 1.0, 1.5, "1", _StrictInt(1)],
)
def test_history_invalid_limit_fails_before_database_access(monkeypatch, value):
    monkeypatch.setattr(report_store, "get_connection", _bomb_if_db_opened)
    with pytest.raises(AnxinBoardReportHistoryInputError) as caught:
        load_anxin_board_report_history(project_id=1, limit=value)
    assert caught.value.code == "ANXIN_BOARD_REPORT_HISTORY_INPUT_INVALID"


@pytest.mark.parametrize(
    "value",
    [0, -1, True, 1.0, "1", SQLITE_SIGNED_INTEGER_MAX + 1, _StrictInt(1)],
)
def test_history_invalid_before_id_fails_before_database_access(monkeypatch, value):
    monkeypatch.setattr(report_store, "get_connection", _bomb_if_db_opened)
    with pytest.raises(AnxinBoardReportHistoryInputError) as caught:
        load_anxin_board_report_history(project_id=1, before_id=value)
    assert caught.value.code == "ANXIN_BOARD_REPORT_HISTORY_INPUT_INVALID"


@pytest.mark.parametrize(
    "value",
    [0, -1, True, 1.0, "1", SQLITE_SIGNED_INTEGER_MAX + 1, _StrictInt(1)],
)
def test_history_invalid_project_id_uses_existing_not_found_before_database_access(
    monkeypatch,
    value,
):
    monkeypatch.setattr(report_store, "get_connection", _bomb_if_db_opened)
    with pytest.raises(AnxinBoardReportProjectNotFoundError) as caught:
        load_anxin_board_report_history(project_id=value)
    assert caught.value.code == "PROJECT_NOT_FOUND"


def test_history_structurally_valid_missing_project_uses_existing_not_found(database):
    with pytest.raises(AnxinBoardReportProjectNotFoundError) as caught:
        load_anxin_board_report_history(project_id=SQLITE_SIGNED_INTEGER_MAX)
    assert caught.value.code == "PROJECT_NOT_FOUND"


def test_history_query_is_bounded_cursor_sql_without_offset():
    source = inspect.getsource(report_store.load_anxin_board_report_history)
    upper = source.upper()
    assert "WHERE PROJECT_ID = ?" in upper
    assert "AND ID < ?" in upper
    assert upper.count("ORDER BY ID DESC") == 2
    assert upper.count("LIMIT ?") == 2
    assert "OFFSET" not in upper
    assert "SELECT NAME FROM PROJECTS WHERE ID = ?" in " ".join(upper.split())


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("report_hash", "0" * 64),
        ("report_date", "1900-01-01"),
        ("schema_version", "unexpected_schema"),
        ("report_json", "{not-json"),
    ],
)
def test_history_any_selected_row_storage_tamper_fails_whole_page(
    database,
    column,
    value,
):
    project_id = _create_project("整页失败项目")
    first = persist_anxin_board_report(
        project_id=project_id,
        report=_report("整页失败项目", supplement="第一版。"),
    )
    persist_anxin_board_report(
        project_id=project_id,
        report=_report("整页失败项目", supplement="第二版。"),
    )
    with get_connection() as conn:
        conn.execute(
            f"UPDATE anxin_board_reports SET {column} = ? WHERE id = ?",
            (value, first["id"]),
        )

    with pytest.raises(AnxinBoardReportStoredInvalidError) as caught:
        load_anxin_board_report_history(project_id=project_id, limit=2)
    assert caught.value.code == "ANXIN_BOARD_REPORT_STORED_INVALID"


def test_history_middle_bad_row_never_returns_partial_page(database):
    project_id = _create_project("中间坏行项目")
    oldest = persist_anxin_board_report(
        project_id=project_id,
        report=_report("中间坏行项目", supplement="第一版。"),
    )
    middle = persist_anxin_board_report(
        project_id=project_id,
        report=_report("中间坏行项目", supplement="第二版。"),
    )
    newest = persist_anxin_board_report(
        project_id=project_id,
        report=_report("中间坏行项目", supplement="第三版。"),
    )
    with get_connection() as conn:
        conn.execute(
            "UPDATE anxin_board_reports SET report_json = ? WHERE id = ?",
            ("{}", middle["id"]),
        )

    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_anxin_board_report_history(project_id=project_id, limit=3)

    assert oldest["id"] < middle["id"] < newest["id"]


def test_history_nested_tamper_fails_even_when_outer_hashes_are_self_consistent(database):
    project_id = _create_project("嵌套篡改项目")
    report = _report("嵌套篡改项目")
    record = persist_anxin_board_report(project_id=project_id, report=report)

    tampered = deepcopy(report)
    tampered["modules"][0]["client_stage_summary_hash"] = "1" * 64
    payload = {
        key: deepcopy(value)
        for key, value in tampered.items()
        if key != "anxin_board_report_hash"
    }
    tampered["anxin_board_report_hash"] = _hash(payload)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE anxin_board_reports
            SET report_json = ?, report_hash = ?
            WHERE id = ?
            """,
            (
                json.dumps(tampered, ensure_ascii=False, separators=(",", ":")),
                tampered["anxin_board_report_hash"],
                record["id"],
            ),
        )

    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_anxin_board_report_history(project_id=project_id)


def test_history_bad_row_outside_current_page_does_not_poison_page(database):
    project_id = _create_project("页外坏行项目")
    oldest = persist_anxin_board_report(
        project_id=project_id,
        report=_report("页外坏行项目", supplement="第一版。"),
    )
    middle = persist_anxin_board_report(
        project_id=project_id,
        report=_report("页外坏行项目", supplement="第二版。"),
    )
    newest = persist_anxin_board_report(
        project_id=project_id,
        report=_report("页外坏行项目", supplement="第三版。"),
    )
    with get_connection() as conn:
        conn.execute(
            "UPDATE anxin_board_reports SET report_json = ? WHERE id = ?",
            ("{}", oldest["id"]),
        )

    page = load_anxin_board_report_history(project_id=project_id, limit=2)
    assert [item["id"] for item in page] == [newest["id"], middle["id"]]

    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_anxin_board_report_history(project_id=project_id, limit=3)


def test_history_project_rename_fails_closed(database):
    project_id = _create_project("历史原项目名")
    persist_anxin_board_report(
        project_id=project_id,
        report=_report("历史原项目名"),
    )
    with get_connection() as conn:
        conn.execute(
            "UPDATE projects SET name = ?, version = version + 1 WHERE id = ?",
            ("历史新项目名", project_id),
        )

    with pytest.raises(AnxinBoardReportStoredInvalidError):
        load_anxin_board_report_history(project_id=project_id)


def test_history_same_input_is_deterministic(database):
    project_id = _create_project("确定性项目")
    for index in range(3):
        persist_anxin_board_report(
            project_id=project_id,
            report=_report("确定性项目", supplement=f"第 {index + 1} 版。"),
        )

    first = load_anxin_board_report_history(project_id=project_id, limit=2)
    second = load_anxin_board_report_history(project_id=project_id, limit=2)
    assert second == first


def test_history_does_not_change_latest_or_persist_contract(database):
    project_id = _create_project("兼容项目")
    first_report = _report("兼容项目", supplement="第一版。")
    second_report = _report("兼容项目", supplement="第二版。")
    first_record = persist_anxin_board_report(project_id=project_id, report=first_report)
    second_record = persist_anxin_board_report(project_id=project_id, report=second_report)

    history = load_anxin_board_report_history(project_id=project_id)

    assert [item["id"] for item in history] == [second_record["id"], first_record["id"]]
    assert load_latest_anxin_board_report(project_id=project_id) == second_report
    replay = persist_anxin_board_report(project_id=project_id, report=deepcopy(first_report))
    assert replay == first_record
    assert len(_report_rows(project_id)) == 2


def test_persistence_store_has_no_network_provider_ai_or_frontend_dependency():
    source = Path("apps/backend/app/anxin_board_report_store.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "import requests",
        "import httpx",
        "import socket",
        "model_gateway",
        "openai",
        "anthropic",
        "smtp",
        "frontend",
    )
    assert all(term not in source for term in forbidden)


@pytest.mark.parametrize(
    ("limit", "expected_count"),
    [(1, 1), (20, 3), (50, 3)],
)
def test_history_accepts_valid_limit_boundaries(database, limit, expected_count):
    project_id = _create_project("limit 边界项目")
    records = [
        persist_anxin_board_report(
            project_id=project_id,
            report=_report("limit 边界项目", supplement=f"第 {index} 版。"),
        )
        for index in range(1, 4)
    ]

    page = load_anxin_board_report_history(project_id=project_id, limit=limit)

    assert len(page) == expected_count
    assert [item["id"] for item in page] == [
        record["id"] for record in reversed(records)
    ][:expected_count]


def test_history_negative_limit_fails_before_database_access(monkeypatch):
    monkeypatch.setattr(report_store, "get_connection", _bomb_if_db_opened)
    with pytest.raises(AnxinBoardReportHistoryInputError) as caught:
        load_anxin_board_report_history(project_id=1, limit=-1)
    assert caught.value.code == "ANXIN_BOARD_REPORT_HISTORY_INPUT_INVALID"


def test_history_two_projects_are_strictly_isolated(database):
    first_id = _create_project("历史隔离项目一")
    second_id = _create_project("历史隔离项目二")
    first_records = [
        persist_anxin_board_report(
            project_id=first_id,
            report=_report("历史隔离项目一", supplement=f"项目一第 {index} 版。"),
        )
        for index in range(1, 3)
    ]
    second_records = [
        persist_anxin_board_report(
            project_id=second_id,
            report=_report("历史隔离项目二", supplement=f"项目二第 {index} 版。"),
        )
        for index in range(1, 3)
    ]

    first_page = load_anxin_board_report_history(project_id=first_id, limit=50)
    second_page = load_anxin_board_report_history(project_id=second_id, limit=50)

    assert [item["id"] for item in first_page] == [
        record["id"] for record in reversed(first_records)
    ]
    assert [item["id"] for item in second_page] == [
        record["id"] for record in reversed(second_records)
    ]
    assert all(item["project_id"] == first_id for item in first_page)
    assert all(item["project_id"] == second_id for item in second_page)
    assert all(item["report"]["project_name"] == "历史隔离项目一" for item in first_page)
    assert all(item["report"]["project_name"] == "历史隔离项目二" for item in second_page)


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("corruption", ["json", "hash", "project_binding"])
def test_history_selected_page_tamper_position_matrix_fails_whole_page(
    database,
    position,
    corruption,
):
    project_name = "位置矩阵项目"
    project_id = _create_project(project_name)
    records = [
        persist_anxin_board_report(
            project_id=project_id,
            report=_report(project_name, supplement=f"第 {index} 版。"),
        )
        for index in range(1, 4)
    ]
    selected_ids = [record["id"] for record in reversed(records)]
    target_id = selected_ids[position]

    with get_connection() as conn:
        if corruption == "json":
            conn.execute(
                "UPDATE anxin_board_reports SET report_json = ? WHERE id = ?",
                ("{not-json", target_id),
            )
        elif corruption == "hash":
            conn.execute(
                "UPDATE anxin_board_reports SET report_hash = ? WHERE id = ?",
                ("0" * 64, target_id),
            )
        else:
            wrong_report = _report("其他项目", supplement=f"binding-{position}")
            conn.execute(
                """
                UPDATE anxin_board_reports
                SET report_json = ?, report_hash = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        wrong_report,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    wrong_report["anxin_board_report_hash"],
                    target_id,
                ),
            )

    with pytest.raises(AnxinBoardReportStoredInvalidError) as caught:
        load_anxin_board_report_history(project_id=project_id, limit=3)
    assert caught.value.code == "ANXIN_BOARD_REPORT_STORED_INVALID"


def test_history_read_does_not_modify_persisted_rows(database):
    project_id = _create_project("只读证明项目")
    for index in range(1, 4):
        persist_anxin_board_report(
            project_id=project_id,
            report=_report("只读证明项目", supplement=f"第 {index} 版。"),
        )
    before_rows = _report_rows(project_id)

    page = load_anxin_board_report_history(project_id=project_id, limit=2)

    after_rows = _report_rows(project_id)
    assert len(page) == 2
    assert after_rows == before_rows
