from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path
import socket
import subprocess

import pytest

import app.anxin_board_report_pipeline as pipeline
from app.anxin_board_report import (
    AnxinBoardReportError,
    DEFAULT_MANAGER_SUPPLEMENT,
    MODULES,
    build_anxin_board_report,
)
from app.anxin_board_report_store import (
    AnxinBoardReportApprovalRequiredError,
    AnxinBoardReportProjectMismatchError,
    AnxinBoardReportProjectNotFoundError,
    AnxinBoardReportStoredInvalidError,
)
from app.client_stage_summary import build_client_stage_summary
from app.db import get_connection, init_db
from app.development_change_evidence import build_development_change_evidence
from app.git_analysis import FileEvidenceCandidate, RangeCandidate
from app.plain_language_change_summary import build_plain_language_change_summary


def _module_summaries() -> list[dict[str, object]]:
    return [
        build_client_stage_summary(module_name=name, stage="开发中")
        for _, name in MODULES
    ]


def _daily_change() -> dict[str, object]:
    candidate = RangeCandidate(
        baseline_commit="a" * 40,
        remote_head="b" * 40,
        commits=["c" * 40],
        commit_count=1,
        changed_file_count=1,
        added_lines=3,
        deleted_lines=1,
        diff_bytes=120,
        files=(
            FileEvidenceCandidate(
                path="apps/backend/app/example.py",
                added_lines=3,
                deleted_lines=1,
                is_binary=False,
            ),
        ),
        continuity="continuous",
        capacity="within",
    )
    evidence = build_development_change_evidence(candidate=candidate)
    return build_plain_language_change_summary(evidence=evidence)


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


def _report_rows(project_id: int) -> list[dict[str, object]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM anxin_board_reports WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _pipeline_call(
    *,
    project_id: int,
    project_name: str,
    supplement: str = DEFAULT_MANAGER_SUPPLEMENT,
) -> dict[str, object]:
    return pipeline.assemble_and_persist_anxin_board_report(
        project_id=project_id,
        project_name=project_name,
        report_date="2026-08-22",
        module_summaries=_module_summaries(),
        daily_change=_daily_change(),
        manager_supplement=supplement,
    )


def test_signature_is_keyword_only_and_matches_frozen_entrypoint():
    signature = inspect.signature(pipeline.assemble_and_persist_anxin_board_report)
    assert list(signature.parameters) == [
        "project_id",
        "project_name",
        "report_date",
        "module_summaries",
        "daily_change",
        "manager_supplement",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert signature.parameters["manager_supplement"].default == DEFAULT_MANAGER_SUPPLEMENT


def test_valid_inputs_build_legacy_report_but_new_persistence_requires_approval(database):
    project_name = "流水线项目"
    project_id = _create_project(project_name)
    modules = _module_summaries()
    daily_change = _daily_change()
    expected_report = build_anxin_board_report(
        project_name=project_name,
        report_date="2026-08-22",
        module_summaries=modules,
        daily_change=daily_change,
    )

    with pytest.raises(AnxinBoardReportApprovalRequiredError) as caught:
        pipeline.assemble_and_persist_anxin_board_report(
            project_id=project_id,
            project_name=project_name,
            report_date="2026-08-22",
            module_summaries=modules,
            daily_change=daily_change,
        )

    assert caught.value.code == "ANXIN_BOARD_REPORT_APPROVAL_REQUIRED"
    assert expected_report["schema_version"] == "anxin_board_report_v1"
    assert _report_rows(project_id) == []


def test_assembler_and_persistence_are_called_exactly_once_with_exact_report(monkeypatch):
    calls: list[tuple[str, object]] = []
    formal_report = {"formal": "report"}
    persistence_record = {"stored": "record"}

    def fake_build(**kwargs):
        calls.append(("build", kwargs))
        return formal_report

    def fake_persist(*, project_id, report):
        calls.append(("persist", (project_id, report)))
        assert report is formal_report
        return persistence_record

    monkeypatch.setattr(pipeline, "build_anxin_board_report", fake_build)
    monkeypatch.setattr(pipeline, "persist_anxin_board_report", fake_persist)

    result = pipeline.assemble_and_persist_anxin_board_report(
        project_id=1,
        project_name="项目",
        report_date="2026-08-22",
        module_summaries=[],
        daily_change={},
        manager_supplement="补充",
    )

    assert result is persistence_record
    assert [name for name, _ in calls] == ["build", "persist"]
    assert calls[1][1] == (1, formal_report)


def test_assembler_error_propagates_and_persistence_is_not_called(monkeypatch):
    def fail_build(**_kwargs):
        raise AnxinBoardReportError()

    def forbidden_persist(**_kwargs):
        raise AssertionError("persistence must not run after assembler failure")

    monkeypatch.setattr(pipeline, "build_anxin_board_report", fail_build)
    monkeypatch.setattr(pipeline, "persist_anxin_board_report", forbidden_persist)

    with pytest.raises(AnxinBoardReportError) as caught:
        pipeline.assemble_and_persist_anxin_board_report(
            project_id=1,
            project_name="项目",
            report_date="2026-08-22",
            module_summaries=[],
            daily_change={},
        )
    assert caught.value.code == "ANXIN_BOARD_REPORT_INVALID"


@pytest.mark.parametrize("project_id", [2**63, True, "1", 1.0, 0, -1])
def test_invalid_project_id_domain_fails_before_assembler_or_persistence(
    project_id, monkeypatch
):
    def forbidden_build(**_kwargs):
        raise AssertionError("assembler must not run for invalid project_id")

    def forbidden_persist(**_kwargs):
        raise AssertionError("persistence must not run for invalid project_id")

    monkeypatch.setattr(pipeline, "build_anxin_board_report", forbidden_build)
    monkeypatch.setattr(pipeline, "persist_anxin_board_report", forbidden_persist)

    with pytest.raises(AnxinBoardReportProjectNotFoundError) as caught:
        pipeline.assemble_and_persist_anxin_board_report(
            project_id=project_id,
            project_name="项目",
            report_date="2026-08-22",
            module_summaries=[],
            daily_change={},
        )
    assert caught.value.code == "PROJECT_NOT_FOUND"


def test_bindable_missing_project_propagates_project_not_found(database):
    with pytest.raises(AnxinBoardReportProjectNotFoundError) as caught:
        _pipeline_call(project_id=99999, project_name="不存在项目")
    assert caught.value.code == "PROJECT_NOT_FOUND"
    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM anxin_board_reports").fetchone()[0] == 0


def test_project_name_mismatch_propagates_and_writes_nothing(database):
    project_id = _create_project("项目 A")
    with pytest.raises(AnxinBoardReportProjectMismatchError) as caught:
        _pipeline_call(project_id=project_id, project_name="项目 B")
    assert caught.value.code == "ANXIN_BOARD_REPORT_PROJECT_MISMATCH"
    assert _report_rows(project_id) == []


def test_same_inputs_remain_rejected_without_creating_legacy_rows(database):
    project_name = "幂等流水线"
    project_id = _create_project(project_name)

    for _ in range(2):
        with pytest.raises(AnxinBoardReportApprovalRequiredError) as caught:
            _pipeline_call(project_id=project_id, project_name=project_name)
        assert caught.value.code == "ANXIN_BOARD_REPORT_APPROVAL_REQUIRED"

    assert _report_rows(project_id) == []


def test_same_day_changed_manager_supplement_still_cannot_create_legacy_revision(database):
    project_name = "修订流水线"
    project_id = _create_project(project_name)

    for supplement in ("今天按原计划推进。", "今天复核后按新结论推进。"):
        with pytest.raises(AnxinBoardReportApprovalRequiredError) as caught:
            _pipeline_call(
                project_id=project_id,
                project_name=project_name,
                supplement=supplement,
            )
        assert caught.value.code == "ANXIN_BOARD_REPORT_APPROVAL_REQUIRED"

    assert _report_rows(project_id) == []


def test_stored_invalid_error_is_propagated_without_wrapping(monkeypatch):
    formal_report = {"formal": "report"}
    error = AnxinBoardReportStoredInvalidError()

    monkeypatch.setattr(pipeline, "build_anxin_board_report", lambda **_kwargs: formal_report)

    def fail_persist(**_kwargs):
        raise error

    monkeypatch.setattr(pipeline, "persist_anxin_board_report", fail_persist)

    with pytest.raises(AnxinBoardReportStoredInvalidError) as caught:
        pipeline.assemble_and_persist_anxin_board_report(
            project_id=1,
            project_name="项目",
            report_date="2026-08-22",
            module_summaries=[],
            daily_change={},
        )
    assert caught.value is error
    assert caught.value.code == "ANXIN_BOARD_REPORT_STORED_INVALID"


def test_rejected_legacy_pipeline_does_not_use_git_workspace_or_network(database, monkeypatch):
    project_name = "无副作用流水线"
    project_id = _create_project(project_name)
    modules = _module_summaries()
    daily_change = _daily_change()

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess/Git must not run")
        ),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network must not run")
        ),
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("workspace file reads must not run")
        ),
    )

    with pytest.raises(AnxinBoardReportApprovalRequiredError) as caught:
        pipeline.assemble_and_persist_anxin_board_report(
            project_id=project_id,
            project_name=project_name,
            report_date="2026-08-22",
            module_summaries=modules,
            daily_change=daily_change,
        )
    assert caught.value.code == "ANXIN_BOARD_REPORT_APPROVAL_REQUIRED"
    assert _report_rows(project_id) == []


def test_pipeline_source_has_no_db_sql_router_network_or_duplicated_report_algorithms():
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    upper = source.upper()

    assert "get_connection" not in source
    assert "validate_anxin_board_report" not in source
    assert "APIRouter" not in source
    assert "subprocess" not in source
    assert "socket" not in source
    assert "git_client" not in source
    assert "hashlib" not in source
    assert "anxin_board_report_v1" not in source
    for statement in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert statement not in upper
