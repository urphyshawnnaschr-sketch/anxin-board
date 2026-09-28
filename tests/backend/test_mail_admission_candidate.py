from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.mail_admission_candidate import (  # noqa: E402
    MailAdmissionCandidateError,
    build_mail_admission_candidate,
    validate_mail_admission_candidate,
)
from app.mail_report_renderer import MailReportRender  # noqa: E402
from app.mail_transport_profile import MailTransportProfile  # noqa: E402
from app.smtp_mail_gateway import SmtpSecurity  # noqa: E402


def _recipient_hash(value: list[str]) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


H = {
    "approval": "a" * 64,
    "report": "b" * 64,
    "binding": "c" * 64,
    "recipients": _recipient_hash(["a@example.test", "b@example.test"]),
    "formal": "e" * 64,
    "profile": "2" * 64,
}


def _render_hash(report_hash: str, render_identity: str, html_sha256: str, subject: str) -> str:
    payload = json.dumps(
        {
            "schema_version": "mail_report_render_v1",
            "report_hash": report_hash,
            "render_identity": render_identity,
            "html_sha256": html_sha256,
            "subject": subject,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _facts():
    binding = {
        "project_id": 7,
        "report_version_id": 19,
        "report_content_hash": H["report"],
        "approval_snapshot_id": 11,
        "approval_snapshot_hash": H["approval"],
        "binding_hash": H["binding"],
        "recipient_config_version_id": 31,
        "recipient_config_version_no": 4,
        "recipients_hash": H["recipients"],
        "to_recipients": ["a@example.test", "b@example.test"],
    }
    approval = {
        "approval_snapshot_id": 11,
        "project_id": 7,
        "report_version_id": 19,
        "report_content_hash": H["report"],
        "approval_snapshot_hash": H["approval"],
    }
    recipients = {
        "id": 31,
        "project_id": 7,
        "version_no": 4,
        "recipients_hash": H["recipients"],
        "to_recipients": ["a@example.test", "b@example.test"],
    }
    formal = {
        "schema_version": "anxin_board_report_v3",
        "anxin_board_report_hash": H["formal"],
        "approval_snapshot_id": 11,
        "approval_snapshot_hash": H["approval"],
        "report_version_id": 19,
        "report_content_hash": H["report"],
    }
    html = "<html><body>approved report</body></html>"
    html_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    subject = "测试项目｜2026-09-06 安心看板"
    render_identity = f"mail_report_render_v1:{H['formal']}"
    render = MailReportRender(
        schema_version="mail_report_render_v1",
        report_hash=H["formal"],
        render_identity=render_identity,
        render_hash=_render_hash(H["formal"], render_identity, html_hash, subject),
        html_body=html,
        html_sha256=html_hash,
        subject=subject,
    )
    profile = MailTransportProfile(
        id=41,
        schema_version="mail_transport_profile_v1",
        version_no=3,
        host="smtp.example.test",
        port=587,
        security=SmtpSecurity.STARTTLS,
        username="mailer@example.test",
        from_identity="reports@example.test",
        secret_ref="smtp-password-test-3",
        timeout_seconds=30.0,
        configured_by="local-settings-api",
        created_at="2026-09-06T09:00:00+00:00",
        predecessor_version_id=40,
        profile_hash=H["profile"],
    )
    return binding, approval, recipients, formal, render, profile


def _build():
    binding, approval, recipients, formal, render, profile = _facts()
    return build_mail_admission_candidate(
        approval_recipient_binding=binding,
        approval_snapshot=approval,
        recipient_config=recipients,
        formal_report=formal,
        render=render,
        transport_profile=profile,
    )


def test_a01_candidate_is_deterministic_complete_and_frozen():
    first = _build()
    second = _build()

    assert first == second
    assert first.project_id == 7
    assert first.report_version_id == 19
    assert first.to_recipients == ("a@example.test", "b@example.test")
    assert first.secret_ref == "smtp-password-test-3"
    assert first.message_id == f"<anxin-{H['formal'][:32]}@anxinboard.local>"
    assert len(first.admission_hash) == 64
    with pytest.raises(FrozenInstanceError):
        first.project_id = 8  # type: ignore[misc]


def test_a02_approval_identity_mismatch_fails_closed():
    binding, approval, recipients, formal, render, profile = _facts()
    approval["approval_snapshot_hash"] = "9" * 64
    with pytest.raises(MailAdmissionCandidateError):
        build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval,
            recipient_config=recipients,
            formal_report=formal,
            render=render,
            transport_profile=profile,
        )


def test_a03_recipient_version_or_order_mismatch_fails_closed():
    binding, approval, recipients, formal, render, profile = _facts()
    recipients["to_recipients"] = ["b@example.test", "a@example.test"]
    with pytest.raises(MailAdmissionCandidateError):
        build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval,
            recipient_config=recipients,
            formal_report=formal,
            render=render,
            transport_profile=profile,
        )


def test_a04_formal_report_must_close_to_same_approval_and_render():
    binding, approval, recipients, formal, render, profile = _facts()
    formal["approval_snapshot_id"] = 12
    with pytest.raises(MailAdmissionCandidateError):
        build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval,
            recipient_config=recipients,
            formal_report=formal,
            render=render,
            transport_profile=profile,
        )

    binding, approval, recipients, formal, render, profile = _facts()
    forged_identity = f"mail_report_render_v1:{'9' * 64}"
    render = replace(
        render,
        report_hash="9" * 64,
        render_identity=forged_identity,
        render_hash=_render_hash("9" * 64, forged_identity, render.html_sha256, render.subject),
    )
    with pytest.raises(MailAdmissionCandidateError):
        build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval,
            recipient_config=recipients,
            formal_report=formal,
            render=render,
            transport_profile=profile,
        )


def test_a05_forged_render_hash_or_html_hash_fails_before_candidate_is_created():
    binding, approval, recipients, formal, render, profile = _facts()
    forged = replace(render, render_hash="0" * 64)
    with pytest.raises(MailAdmissionCandidateError):
        build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval,
            recipient_config=recipients,
            formal_report=formal,
            render=forged,
            transport_profile=profile,
        )

    forged = replace(render, html_sha256="0" * 64)
    with pytest.raises(MailAdmissionCandidateError):
        build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval,
            recipient_config=recipients,
            formal_report=formal,
            render=forged,
            transport_profile=profile,
        )


def test_a06_candidate_contains_only_opaque_secret_reference_not_secret_value():
    candidate = _build()
    serialized = repr(candidate)

    assert "smtp-password-test-3" in serialized
    assert "test-only-password" not in serialized
    assert not hasattr(candidate, "password")
    assert not hasattr(candidate, "secret_value")


def test_a07_transport_profile_identity_changes_admission_hash_but_not_report_message_id():
    binding, approval, recipients, formal, render, profile = _facts()
    first = build_mail_admission_candidate(
        approval_recipient_binding=binding,
        approval_snapshot=approval,
        recipient_config=recipients,
        formal_report=formal,
        render=render,
        transport_profile=profile,
    )
    rotated = replace(
        profile,
        id=42,
        version_no=4,
        predecessor_version_id=41,
        profile_hash="3" * 64,
        secret_ref="smtp-password-test-4",
    )
    second = build_mail_admission_candidate(
        approval_recipient_binding=binding,
        approval_snapshot=approval,
        recipient_config=recipients,
        formal_report=formal,
        render=render,
        transport_profile=rotated,
    )

    assert second.admission_hash != first.admission_hash
    assert second.message_id == first.message_id


def test_a08_validator_rejects_forged_candidate_hash_recipient_hash_message_id_or_secret_ref():
    candidate = _build()

    for forged in (
        replace(candidate, admission_hash="0" * 64),
        replace(candidate, recipients_hash="0" * 64),
        replace(candidate, message_id="<forged@anxinboard.local>"),
        replace(candidate, secret_ref="INVALID SECRET REF"),
    ):
        with pytest.raises(MailAdmissionCandidateError):
            validate_mail_admission_candidate(forged)


def test_a09_recipient_hash_must_be_r1_canonical_hash_even_when_admission_hash_is_recomputed():
    candidate = _build()
    forged_recipients_hash = "0" * 64
    forged_payload = {
        "schema_version": candidate.schema_version,
        "project_id": candidate.project_id,
        "report_version_id": candidate.report_version_id,
        "report_content_hash": candidate.report_content_hash,
        "approval_snapshot_id": candidate.approval_snapshot_id,
        "approval_snapshot_hash": candidate.approval_snapshot_hash,
        "approval_recipient_binding_hash": candidate.approval_recipient_binding_hash,
        "recipient_config_version_id": candidate.recipient_config_version_id,
        "recipient_config_version_no": candidate.recipient_config_version_no,
        "recipients_hash": forged_recipients_hash,
        "to_recipients": list(candidate.to_recipients),
        "transport_profile_id": candidate.transport_profile_id,
        "transport_profile_version_no": candidate.transport_profile_version_no,
        "transport_profile_hash": candidate.transport_profile_hash,
        "secret_ref": candidate.secret_ref,
        "from_identity": candidate.from_identity,
        "formal_report_hash": candidate.formal_report_hash,
        "render_identity": candidate.render_identity,
        "render_hash": candidate.render_hash,
        "html_sha256": candidate.html_sha256,
        "subject": candidate.subject,
        "message_id": candidate.message_id,
    }
    forged_admission_hash = hashlib.sha256(
        json.dumps(
            forged_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    forged = replace(
        candidate,
        recipients_hash=forged_recipients_hash,
        admission_hash=forged_admission_hash,
    )
    with pytest.raises(MailAdmissionCandidateError):
        validate_mail_admission_candidate(forged)


def test_approval_owner_legacy_id_only_fails_closed():
    binding, approval, recipients, formal, render, profile = _facts()
    legacy_id = approval.pop("approval_snapshot_id")
    approval["id"] = legacy_id
    with pytest.raises(MailAdmissionCandidateError):
        build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval,
            recipient_config=recipients,
            formal_report=formal,
            render=render,
            transport_profile=profile,
        )


def _rehash_candidate(candidate):
    payload = asdict(candidate)
    payload.pop("admission_hash")
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return replace(candidate, admission_hash=digest)


@pytest.mark.parametrize('version', [9, 10, 11])
def test_confirmed_module_revision_has_distinct_message_id_without_changing_old_mail(version):
    old = _build()
    revised = _rehash_candidate(replace(
        old,
        render_identity=f"mail_report_render_v{version}:{old.formal_report_hash}:{'7' * 64}",
        render_hash="8" * 64,
        html_sha256="9" * 64,
        message_id=f"<anxin-r{version}-{'8' * 32}@anxinboard.local>",
    ))
    assert validate_mail_admission_candidate(revised) == revised
    assert validate_mail_admission_candidate(old) == old
    assert revised.message_id != old.message_id
    assert revised.report_version_id == old.report_version_id
    assert revised.approval_snapshot_hash == old.approval_snapshot_hash
    with pytest.raises(MailAdmissionCandidateError):
        validate_mail_admission_candidate(_rehash_candidate(replace(revised, message_id=old.message_id)))
    with pytest.raises(MailAdmissionCandidateError):
        validate_mail_admission_candidate(_rehash_candidate(replace(revised, render_hash="6" * 64)))
