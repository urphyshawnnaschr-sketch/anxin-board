from types import SimpleNamespace

import app.mail_readiness as readiness


def _recipient(version_id=11, version_no=2, recipients_hash="a" * 64):
    return {
        "id": version_id,
        "project_id": 7,
        "version_no": version_no,
        "recipients_hash": recipients_hash,
        "to_recipients": ["owner@example.com"],
    }


def _report():
    return {
        "schema_version": "anxin_board_report_v3",
        "report_version_id": 31,
        "profile_id": 5,
        "anxin_board_report_hash": "b" * 64,
    }


def test_readiness_does_not_touch_r3_owner_when_binding_table_never_existed(monkeypatch):
    monkeypatch.setattr(readiness, "_project_exists", lambda project_id: True)
    monkeypatch.setattr(
        readiness,
        "_table_exists",
        lambda table: table == "mail_transport_profile_versions",
    )
    monkeypatch.setattr(readiness, "get_current_mail_transport_profile", lambda: SimpleNamespace())
    monkeypatch.setattr(readiness, "get_current_recipient_config", lambda project_id: _recipient())
    monkeypatch.setattr(readiness, "load_latest_anxin_board_report", lambda **kwargs: _report())

    def forbidden(**kwargs):
        raise AssertionError("R3 getter must not run when its table is absent")

    monkeypatch.setattr(readiness, "get_approval_recipient_binding", forbidden)

    result = readiness.get_mail_readiness(7)

    assert result["state"] == "send_confirmation_required"
    assert result["candidate_ready"] is False
    assert result["send_action_available"] is True
    binding = next(item for item in result["checks"] if item["code"] == "approval_recipient_binding")
    assert binding["ready"] is False
    assert "发送时" in binding["summary"]


def test_real_sqlite_readiness_probe_does_not_create_r3_table(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "readiness.db"))
    with readiness.get_connection() as conn:
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO projects (id, name) VALUES (7, 'Project A')")
        conn.execute("CREATE TABLE mail_transport_profile_versions (id INTEGER PRIMARY KEY)")
        conn.commit()

    monkeypatch.setattr(readiness, "get_current_mail_transport_profile", lambda: SimpleNamespace())
    monkeypatch.setattr(readiness, "get_current_recipient_config", lambda project_id: _recipient())
    monkeypatch.setattr(readiness, "load_latest_anxin_board_report", lambda **kwargs: _report())

    def forbidden(**kwargs):
        raise AssertionError("absent R3 table must not call the R3 owner")

    monkeypatch.setattr(readiness, "get_approval_recipient_binding", forbidden)

    result = readiness.get_mail_readiness(7)
    assert result["state"] == "send_confirmation_required"
    assert result["send_action_available"] is True

    with readiness.get_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_approval_recipient_bindings'"
        ).fetchone() is None


def test_changed_recipient_version_invalidates_existing_binding(monkeypatch):
    monkeypatch.setattr(readiness, "_project_exists", lambda project_id: True)
    monkeypatch.setattr(readiness, "_table_exists", lambda table: True)
    monkeypatch.setattr(readiness, "get_current_mail_transport_profile", lambda: SimpleNamespace())
    monkeypatch.setattr(readiness, "get_current_recipient_config", lambda project_id: _recipient(version_id=12, version_no=3))
    monkeypatch.setattr(readiness, "load_latest_anxin_board_report", lambda **kwargs: _report())
    monkeypatch.setattr(
        readiness,
        "get_approval_recipient_binding",
        lambda **kwargs: {
            "recipient_config_version_id": 11,
            "recipient_config_version_no": 2,
            "recipients_hash": "a" * 64,
        },
    )
    monkeypatch.setattr(
        readiness,
        "build_mail_admission_candidate",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("stale binding must not reach candidate")),
    )

    result = readiness.get_mail_readiness(7)

    assert result["state"] == "blocked"
    assert result["send_action_available"] is False
    binding = next(item for item in result["checks"] if item["code"] == "approval_recipient_binding")
    assert binding["ready"] is False
    assert "变化" in binding["summary"]


def test_exact_facts_reach_candidate_ready_and_offer_human_send_action(monkeypatch):
    recipient = _recipient()
    report = _report()
    transport = SimpleNamespace()
    binding = {
        "recipient_config_version_id": 11,
        "recipient_config_version_no": 2,
        "recipients_hash": "a" * 64,
    }
    candidate = SimpleNamespace()

    monkeypatch.setattr(readiness, "_project_exists", lambda project_id: True)
    monkeypatch.setattr(readiness, "_table_exists", lambda table: True)
    monkeypatch.setattr(readiness, "get_current_mail_transport_profile", lambda: transport)
    monkeypatch.setattr(readiness, "get_current_recipient_config", lambda project_id: recipient)
    monkeypatch.setattr(readiness, "load_latest_anxin_board_report", lambda **kwargs: report)
    monkeypatch.setattr(readiness, "get_approval_recipient_binding", lambda **kwargs: binding)
    monkeypatch.setattr(readiness, "get_report_approval_snapshot", lambda **kwargs: {"id": 41})
    monkeypatch.setattr(readiness, "read_bound_project_profile_for_report", lambda profile_id: {"id": profile_id})
    narrative = {"approved-content": "exact-report"}
    monkeypatch.setattr(readiness, "load_approved_report_narrative", lambda **kwargs: narrative)
    module_narrative = {'synthetic-confirmed-module-note': 'exact-bound-source'}
    monkeypatch.setattr(readiness, 'get_approved_module_narrative', lambda **kwargs: module_narrative)
    git_metrics = {'synthetic-git-metrics': 'exact-approved-snapshot'}
    monkeypatch.setattr(readiness, 'load_approved_report_git_metrics', lambda **kwargs: git_metrics)
    def render(*args, **kwargs):
        assert kwargs["approved_narrative"] is narrative
        assert kwargs['approved_module_narrative'] is module_narrative
        assert kwargs['approved_git_metrics'] is git_metrics
        return object()
    monkeypatch.setattr(readiness, "render_approved_report_for_mail", render)
    monkeypatch.setattr(readiness, "build_mail_admission_candidate", lambda **kwargs: candidate)
    monkeypatch.setattr(readiness, "_candidate_projection", lambda value: {"admission_hash": "c" * 64})

    result = readiness.get_mail_readiness(7)

    assert result["state"] == "candidate_ready"
    assert result["candidate_ready"] is True
    assert result["send_action_available"] is True
    assert result["candidate"] == {"admission_hash": "c" * 64}
    assert all(item["ready"] is True for item in result["checks"])
