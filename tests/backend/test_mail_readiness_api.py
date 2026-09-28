from fastapi import HTTPException

import app.mail_readiness_api as api
from app.mail_readiness import MailReadinessError


def test_readiness_api_returns_projection_without_write_guard(monkeypatch):
    expected = {
        "schema_version": "mail_readiness_v1",
        "project_id": 7,
        "state": "blocked",
        "candidate_ready": False,
        "send_action_available": False,
        "checks": [],
        "candidate": None,
    }
    monkeypatch.setattr(api, "get_mail_readiness", lambda project_id: expected)
    assert api.read_mail_readiness(7) == expected


def test_readiness_api_maps_invalid_authority_to_non_secret_conflict(monkeypatch):
    def fail(project_id):
        raise MailReadinessError("MAIL_READINESS_BINDING_INVALID")

    monkeypatch.setattr(api, "get_mail_readiness", fail)
    try:
        api.read_mail_readiness(7)
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["code"] == "MAIL_READINESS_BINDING_INVALID"
        assert "密码" not in exc.detail["message"]
    else:
        raise AssertionError("expected HTTPException")
