from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

import app.mail_durable_admission as admission_module
import app.report_approval_recipient_binding as binding_module
from app.db import get_connection, init_db
from app.mail_admission_candidate import MailAdmissionCandidate
from app.mail_durable_admission import (
    MailDurableAdmissionError,
    get_durable_mail_admission,
    prepare_durable_mail_admission,
)
from app.mail_send_attempts import claim_prepared_attempt
from app.mail_transport_profile import save_mail_transport_profile
from app.recipient_config import save_recipient_config
from app.report_approval_recipient_binding import create_approval_recipient_binding


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _candidate(*, project_id: int, approval: dict[str, object], binding: dict[str, object], config, transport) -> MailAdmissionCandidate:
    formal_hash = _h("formal-r4")
    message_id = f"<anxin-{formal_hash[:32]}@anxinboard.local>"
    payload = {
        "schema_version": "mail_admission_candidate_v1",
        "project_id": project_id,
        "report_version_id": 801,
        "report_content_hash": approval["report_content_hash"],
        "approval_snapshot_id": approval["approval_snapshot_id"],
        "approval_snapshot_hash": approval["approval_snapshot_hash"],
        "approval_recipient_binding_hash": binding["binding_hash"],
        "recipient_config_version_id": config["id"],
        "recipient_config_version_no": config["version_no"],
        "recipients_hash": config["recipients_hash"],
        "to_recipients": list(config["to_recipients"]),
        "transport_profile_id": transport.id,
        "transport_profile_version_no": transport.version_no,
        "transport_profile_hash": transport.profile_hash,
        "secret_ref": transport.secret_ref,
        "from_identity": transport.from_identity,
        "formal_report_hash": formal_hash,
        "render_identity": f"mail_report_render_v1:{formal_hash}",
        "render_hash": _h("render-r4"),
        "html_sha256": _h("html-r4"),
        "subject": "Anxin Board R4",
        "message_id": message_id,
    }
    return MailAdmissionCandidate(
        schema_version="mail_admission_candidate_v1",
        project_id=project_id,
        report_version_id=801,
        report_content_hash=approval["report_content_hash"],
        approval_snapshot_id=approval["approval_snapshot_id"],
        approval_snapshot_hash=approval["approval_snapshot_hash"],
        approval_recipient_binding_hash=binding["binding_hash"],
        recipient_config_version_id=config["id"],
        recipient_config_version_no=config["version_no"],
        recipients_hash=config["recipients_hash"],
        to_recipients=tuple(config["to_recipients"]),
        transport_profile_id=transport.id,
        transport_profile_version_no=transport.version_no,
        transport_profile_hash=transport.profile_hash,
        secret_ref=transport.secret_ref,
        from_identity=transport.from_identity,
        formal_report_hash=formal_hash,
        render_identity=f"mail_report_render_v1:{formal_hash}",
        render_hash=_h("render-r4"),
        html_sha256=_h("html-r4"),
        subject="Anxin Board R4",
        message_id=message_id,
        admission_hash=_h(_canonical(payload)),
    )


@pytest.fixture()
def r4_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "anxinboard.db"))
    init_db()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES (?, ?, ?)",
            ("mail-r4", "active", "2026-09-06T00:00:00+00:00"),
        )
        project_id = int(cursor.lastrowid)
        conn.commit()

    approval = {
        "approval_snapshot_id": 701,
        "project_id": project_id,
        "report_version_id": 801,
        "approval_snapshot_hash": _h("approval-r4"),
        "report_content_hash": _h("report-r4"),
    }
    monkeypatch.setattr(
        binding_module,
        "get_report_approval_snapshot",
        lambda *, project_id, report_version_id: (
            dict(approval)
            if project_id == approval["project_id"] and report_version_id == approval["report_version_id"]
            else None
        ),
    )

    config = save_recipient_config(
        project_id=project_id,
        to_recipients=("alpha@example.test", "beta@example.test"),
        created_by="R4 Test",
        expected_version_no=0,
    )
    binding = create_approval_recipient_binding(
        project_id=project_id,
        report_version_id=801,
        expected_recipient_config_version_id=int(config["id"]),
        expected_recipient_config_version_no=int(config["version_no"]),
        confirmed_by="Project Manager",
        confirmed_timezone="Asia/Shanghai",
        confirmed_utc_offset_minutes=480,
        human_confirmed=True,
        idempotency_key="r4-binding",
    )
    transport = save_mail_transport_profile(
        host="smtp.example.test",
        port=587,
        security="starttls",
        username="mailer@example.test",
        from_identity="mailer@example.test",
        secret_ref="smtp.r4.v1",
        timeout_seconds=30.0,
        configured_by="R4 Test",
        expected_version_no=0,
    )
    candidate = _candidate(
        project_id=project_id,
        approval=approval,
        binding=binding,
        config=config,
        transport=transport,
    )

    monkeypatch.setattr(
        admission_module,
        "_compose_current_candidate",
        lambda actual: candidate if actual == project_id else None,
    )
    monkeypatch.setattr(
        admission_module,
        "_compose_current_candidate_in_transaction",
        lambda conn, actual: candidate if actual == project_id else None,
    )
    return {
        "project_id": project_id,
        "approval": approval,
        "config": config,
        "binding": binding,
        "transport": transport,
        "candidate": candidate,
    }


def _admission_count() -> int:
    with get_connection() as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_durable_admissions'"
        ).fetchone()
        if table is None:
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM mail_durable_admissions").fetchone()[0])


def test_r4_01_persists_admission_only_after_prepared_owner_attempt_and_replays_prepared(r4_db):
    candidate = r4_db["candidate"]
    admission, attempt = prepare_durable_mail_admission(
        project_id=int(r4_db["project_id"]),
        expected_admission_hash=candidate.admission_hash,
    )

    assert attempt.state == "prepared"
    assert admission.send_attempt_id == attempt.send_attempt_id
    assert admission.candidate == candidate
    assert get_durable_mail_admission(admission.send_attempt_id) == admission

    replay, replay_attempt = prepare_durable_mail_admission(
        project_id=int(r4_db["project_id"]),
        expected_admission_hash=candidate.admission_hash,
    )
    assert replay.id == admission.id
    assert replay_attempt.id == attempt.id
    assert replay_attempt.state == "prepared"


def test_r4_02_invalid_predecessor_never_leaves_durable_admission(r4_db):
    candidate = r4_db["candidate"]

    with pytest.raises(MailDurableAdmissionError) as caught:
        prepare_durable_mail_admission(
            project_id=int(r4_db["project_id"]),
            expected_admission_hash=candidate.admission_hash,
            predecessor_send_attempt_id="missing-terminal-predecessor",
        )

    assert caught.value.code == "MAIL_ADMISSION_SEND_ATTEMPT_REJECTED"
    assert _admission_count() == 0


def test_r4_03_replay_rejects_matching_attempt_after_it_leaves_prepared(r4_db):
    candidate = r4_db["candidate"]
    admission, attempt = prepare_durable_mail_admission(
        project_id=int(r4_db["project_id"]),
        expected_admission_hash=candidate.admission_hash,
    )
    claimed = claim_prepared_attempt(attempt.send_attempt_id)
    assert claimed.state == "sending"

    with pytest.raises(MailDurableAdmissionError) as caught:
        prepare_durable_mail_admission(
            project_id=int(r4_db["project_id"]),
            expected_admission_hash=candidate.admission_hash,
        )

    assert caught.value.code == "MAIL_ADMISSION_SEND_ATTEMPT_NOT_PREPARED"
    assert get_durable_mail_admission(admission.send_attempt_id).id == admission.id


def test_r4_04_source_drift_after_attempt_materialization_cannot_persist_admission(r4_db, monkeypatch):
    candidate = r4_db["candidate"]
    drifted = replace(candidate, admission_hash="f" * 64)
    monkeypatch.setattr(
        admission_module,
        "_compose_current_candidate_in_transaction",
        lambda conn, actual: drifted,
    )

    with pytest.raises(MailDurableAdmissionError) as caught:
        prepare_durable_mail_admission(
            project_id=int(r4_db["project_id"]),
            expected_admission_hash=candidate.admission_hash,
        )

    assert caught.value.code == "MAIL_ADMISSION_SOURCE_DRIFT"
    assert _admission_count() == 0
    with get_connection() as conn:
        attempts = conn.execute("SELECT send_attempt_id, state FROM mail_send_attempts").fetchall()
    assert len(attempts) == 1
    assert attempts[0]["state"] == "prepared"


def test_r4_05_expected_candidate_hash_remains_optimistic_stale_guard(r4_db):
    with pytest.raises(MailDurableAdmissionError) as caught:
        prepare_durable_mail_admission(
            project_id=int(r4_db["project_id"]),
            expected_admission_hash="0" * 64,
        )
    assert caught.value.code == "MAIL_ADMISSION_CANDIDATE_STALE"
    assert _admission_count() == 0


def test_r4_06_stored_admission_tamper_fails_closed(r4_db):
    candidate = r4_db["candidate"]
    admission, _ = prepare_durable_mail_admission(
        project_id=int(r4_db["project_id"]),
        expected_admission_hash=candidate.admission_hash,
    )
    with get_connection() as conn:
        conn.execute("DROP TRIGGER trg_mail_durable_admissions_no_update")
        conn.execute(
            "UPDATE mail_durable_admissions SET record_hash = ? WHERE id = ?",
            (_h("tampered-r4-record"), admission.id),
        )
        conn.commit()

    with pytest.raises(MailDurableAdmissionError) as caught:
        get_durable_mail_admission(admission.send_attempt_id)
    assert caught.value.code == "MAIL_ADMISSION_STORED_INVALID"


def test_r4_07_in_transaction_source_composition_uses_owner_closures(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "anxinboard.db"))
    init_db()
    with get_connection() as conn:
        project_id = int(
            conn.execute(
                "INSERT INTO projects (name, status, created_at) VALUES ('owner-reclose','active','2026-09-06T00:00:00+00:00')"
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO anxin_board_reports (project_id, schema_version, report_date, report_hash, report_json, created_at) "
            "VALUES (?, 'legacy-test', '2026-09-06', ?, '{}', '2026-09-06T00:00:00+00:00')",
            (project_id, _h("owner-row")),
        )
        conn.execute("CREATE TABLE fake_r3 (project_id INTEGER NOT NULL, report_version_id INTEGER NOT NULL)")
        conn.execute("INSERT INTO fake_r3 VALUES (?, 801)", (project_id,))
        conn.commit()

    approval = {
        "approval_snapshot_id": 701,
        "project_id": project_id,
        "report_version_id": 801,
        "approval_snapshot_hash": _h("approval-owner"),
        "report_content_hash": _h("report-owner"),
    }
    config = {
        "id": 11,
        "project_id": project_id,
        "version_no": 1,
        "to_recipients": ["owner@example.test"],
        "recipients_hash": _h(json.dumps(["owner@example.test"], ensure_ascii=True, separators=(",", ":"))),
    }
    binding = {
        "project_id": project_id,
        "report_version_id": 801,
        "report_content_hash": approval["report_content_hash"],
        "approval_snapshot_id": 701,
        "approval_snapshot_hash": approval["approval_snapshot_hash"],
        "binding_hash": _h("binding-owner"),
        "recipient_config_version_id": 11,
        "recipient_config_version_no": 1,
        "recipients_hash": config["recipients_hash"],
        "to_recipients": ["owner@example.test"],
    }
    transport = save_mail_transport_profile(
        host="smtp.owner.test",
        port=587,
        security="starttls",
        username="owner@example.test",
        from_identity="owner@example.test",
        secret_ref="smtp.owner.v1",
        configured_by="Owner Test",
        expected_version_no=0,
    )
    formal = {
        "schema_version": "anxin_board_report_v3",
        "report_version_id": 801,
        "profile_id": 99,
        "report_content_hash": approval["report_content_hash"],
        "approval_snapshot_id": 701,
        "approval_snapshot_hash": approval["approval_snapshot_hash"],
        "anxin_board_report_hash": _h("formal-owner"),
    }

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(admission_module.report_store, "_read_columns", lambda conn: "id, project_id")
    monkeypatch.setattr(
        admission_module.report_store,
        "_validate_stored_row",
        lambda row, *, conn, expected_project_id, expected_project_name: (calls.append(("report", id(conn))) or ({}, formal)),
    )
    monkeypatch.setattr(
        admission_module.recipient_owner,
        "_read_history_in_tx",
        lambda conn, pid: (calls.append(("recipient", id(conn))) or [config]),
    )
    monkeypatch.setattr(
        admission_module.transport_owner,
        "_read_history_in_tx",
        lambda conn: (calls.append(("transport", id(conn))) or [transport]),
    )
    monkeypatch.setattr(admission_module.binding_owner, "_TABLE", "fake_r3")
    monkeypatch.setattr(admission_module.binding_owner, "_COLUMNS", "project_id, report_version_id")
    monkeypatch.setattr(
        admission_module.binding_owner,
        "_close_row",
        lambda row: (calls.append(("binding", -1)) or binding),
    )
    monkeypatch.setattr(
        admission_module.approval_owner,
        "_existing_by_report",
        lambda conn, pid, rid: (calls.append(("approval-read", id(conn))) or {"ok": True}),
    )
    monkeypatch.setattr(
        admission_module.approval_owner,
        "_close_existing_with_report",
        lambda conn, row: (calls.append(("approval-close", id(conn))) or approval),
    )
    monkeypatch.setattr(
        admission_module,
        "read_bound_project_profile_for_report",
        lambda profile_id, *, conn: (calls.append(("profile", id(conn))) or {"id": profile_id}),
    )
    narrative = {"approved-content": "exact-report"}
    monkeypatch.setattr(admission_module, "load_approved_report_narrative",
        lambda *, conn, **kwargs: (calls.append(("narrative", id(conn))) or narrative))
    module_narrative = {'synthetic-confirmed-module-note': 'exact-bound-source'}
    monkeypatch.setattr(admission_module, 'get_approved_module_narrative',
        lambda *, conn, **kwargs: (calls.append(('module-narrative', id(conn))) or module_narrative))
    git_metrics = {'synthetic-git-metrics': 'exact-approved-snapshot'}
    monkeypatch.setattr(admission_module, 'load_approved_report_git_metrics',
        lambda *, conn, **kwargs: (calls.append(('git-metrics', id(conn))) or git_metrics))
    def render(*args, **kwargs):
        assert kwargs["approved_narrative"] is narrative
        assert kwargs['approved_module_narrative'] is module_narrative
        assert kwargs['approved_git_metrics'] is git_metrics
        return object()
    monkeypatch.setattr(admission_module, "render_approved_report_for_mail", render)
    expected = _candidate(
        project_id=project_id,
        approval=approval,
        binding=binding,
        config=config,
        transport=transport,
    )
    monkeypatch.setattr(admission_module, "build_mail_admission_candidate", lambda **kwargs: expected)
    monkeypatch.setattr(admission_module, "validate_mail_admission_candidate", lambda value: value)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actual = admission_module._compose_current_candidate_in_transaction(conn, project_id)
        transaction_conn_id = id(conn)
        conn.rollback()

    assert actual == expected
    same_tx_names = {name for name, conn_id in calls if conn_id == transaction_conn_id}
    assert {'module-narrative', 'git-metrics'}.issubset(same_tx_names)
    assert {"report", "recipient", "transport", "approval-read", "approval-close", "profile"}.issubset(same_tx_names)
    assert ("binding", -1) in calls
