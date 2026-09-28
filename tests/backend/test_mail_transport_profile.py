from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app.db import get_connection, init_db  # noqa: E402
from app.mail_transport_profile import (  # noqa: E402
    MailTransportProfileError,
    get_current_mail_transport_profile,
    get_mail_transport_profile_history,
    save_mail_transport_profile,
)
from app.smtp_mail_gateway import SmtpSecurity  # noqa: E402


@pytest.fixture()
def profile_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "anxinboard.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(path))
    init_db()
    return path


def _save(*, expected: int = 0, **overrides):
    values = {
        "host": "SMTP.Example.TEST",
        "port": 587,
        "security": "starttls",
        "username": "mailer@example.test",
        "from_identity": "Reports@Example.TEST",
        "secret_ref": "smtp-password-v1",
        "timeout_seconds": 30,
        "configured_by": "PM Alice",
        "expected_version_no": expected,
    }
    values.update(overrides)
    return save_mail_transport_profile(**values)


def test_p01_first_profile_is_canonical_immutable_and_materializes_adapter_config(profile_db: Path):
    assert get_current_mail_transport_profile() is None

    profile = _save()

    assert profile.version_no == 1
    assert profile.predecessor_version_id is None
    assert profile.host == "smtp.example.test"
    assert profile.security is SmtpSecurity.STARTTLS
    assert profile.from_identity == "Reports@example.test"
    assert profile.secret_ref == "smtp-password-v1"
    assert len(profile.profile_hash) == 64
    assert get_current_mail_transport_profile() == profile

    config = profile.gateway_config()
    assert config.host == profile.host
    assert config.port == profile.port
    assert config.security is profile.security
    assert config.username == profile.username
    assert config.secret_ref == profile.secret_ref
    assert config.timeout_seconds == profile.timeout_seconds

    with pytest.raises(FrozenInstanceError):
        profile.host = "changed.example.test"  # type: ignore[misc]


def test_p02_exact_config_noop_does_not_create_version_for_different_operator(profile_db: Path):
    first = _save()
    replay = _save(expected=1, configured_by="PM Bob")

    assert replay == first
    assert len(get_mail_transport_profile_history()) == 1


def test_p03_changed_nonsecret_config_appends_predecessor_and_history_is_append_only(profile_db: Path):
    first = _save()
    second = _save(
        expected=1,
        secret_ref="smtp-password-v2",
        configured_by="PM Bob",
    )

    assert second.version_no == 2
    assert second.predecessor_version_id == first.id
    assert second.secret_ref == "smtp-password-v2"
    assert [item.id for item in get_mail_transport_profile_history()] == [first.id, second.id]

    with get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE mail_transport_profile_versions SET host = 'evil.example.test' WHERE id = ?",
                (first.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM mail_transport_profile_versions WHERE id = ?",
                (first.id,),
            )


def test_p04_optimistic_version_conflict_fails_closed(profile_db: Path):
    _save()
    with pytest.raises(MailTransportProfileError) as caught:
        _save(expected=0, secret_ref="smtp-password-v2")
    assert caught.value.code == "MAIL_TRANSPORT_PROFILE_VERSION_CONFLICT"
    assert len(get_mail_transport_profile_history()) == 1


def test_p05_invalid_transport_and_provenance_inputs_are_rejected_before_persistence(profile_db: Path):
    invalid_cases = (
        {"host": "bad host"},
        {"port": 0},
        {"security": "plaintext"},
        {"username": "user\nname"},
        {"from_identity": "Reports Team <reports@example.test>"},
        {"from_identity": "reports@localhost"},
        {"secret_ref": "SMTP-PASSWORD"},
        {"timeout_seconds": 0},
        {"configured_by": "PM\u2028Alice"},
    )
    for overrides in invalid_cases:
        with pytest.raises(MailTransportProfileError):
            _save(**overrides)
    assert get_mail_transport_profile_history() == []


def test_p06_stored_noncanonical_or_tampered_row_fails_closed(profile_db: Path):
    profile = _save()
    with get_connection() as conn:
        conn.execute("DROP TRIGGER trg_mail_transport_profile_versions_no_update")
        conn.execute(
            "UPDATE mail_transport_profile_versions SET host = ? WHERE id = ?",
            ("SMTP.EXAMPLE.TEST", profile.id),
        )
        conn.commit()

    with pytest.raises(MailTransportProfileError) as caught:
        get_current_mail_transport_profile()
    assert caught.value.code == "MAIL_TRANSPORT_PROFILE_STORED_INVALID"


def test_p07_profile_persists_only_opaque_secret_reference_not_secret_bytes(profile_db: Path):
    marker = "DO-NOT-PERSIST-SMTP-SECRET-BYTES"
    profile = _save(secret_ref="smtp-secret-ref-only")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM mail_transport_profile_versions WHERE id = ?",
            (profile.id,),
        ).fetchone()
        serialized = repr(dict(row))

    assert profile.secret_ref == "smtp-secret-ref-only"
    assert marker not in serialized
    assert "password" not in set(dict(row).keys())
    assert "secret" not in set(dict(row).keys())
    assert "secret_value" not in set(dict(row).keys())


def test_p08_reader_detects_broken_predecessor_lineage(profile_db: Path):
    first = _save()
    second = _save(expected=1, secret_ref="smtp-password-v2")

    with get_connection() as conn:
        conn.execute("DROP TRIGGER trg_mail_transport_profile_versions_no_update")
        conn.execute(
            "UPDATE mail_transport_profile_versions SET predecessor_version_id = NULL WHERE id = ?",
            (second.id,),
        )
        conn.commit()

    with pytest.raises(MailTransportProfileError) as caught:
        get_mail_transport_profile_history()
    assert caught.value.code == "MAIL_TRANSPORT_PROFILE_STORED_INVALID"
    assert first.id != second.id
