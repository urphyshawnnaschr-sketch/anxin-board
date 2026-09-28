"""Pure fail-closed mail admission candidate composition.

This module does not decide that pending authorities are independently accepted and does
not persist or execute a send. It only proves that already-supplied immutable facts are
mutually identical enough to prepare the future R4 admission record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re

from app.mail_gateway import MailContractError, MailMessage
from app.mail_report_renderer import (
    MailReportRender,
    MailReportRenderError,
    validate_mail_report_render,
)
from app.mail_transport_profile import MailTransportProfile
from app.recipient_config import RecipientConfigError, canonicalize_to_recipients
from app.secret_store import InvalidSecretReferenceError, validate_secret_ref


SCHEMA_VERSION = "mail_admission_candidate_v1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_MAX = 2**63 - 1
_MAX_IDENTITY_CHARS = 320


class MailAdmissionCandidateError(RuntimeError):
    code = "MAIL_ADMISSION_CANDIDATE_INVALID"


@dataclass(frozen=True, slots=True)
class MailAdmissionCandidate:
    schema_version: str
    project_id: int
    report_version_id: int
    report_content_hash: str
    approval_snapshot_id: int
    approval_snapshot_hash: str
    approval_recipient_binding_hash: str
    recipient_config_version_id: int
    recipient_config_version_no: int
    recipients_hash: str
    to_recipients: tuple[str, ...]
    transport_profile_id: int
    transport_profile_version_no: int
    transport_profile_hash: str
    secret_ref: str
    from_identity: str
    formal_report_hash: str
    render_identity: str
    render_hash: str
    html_sha256: str
    subject: str
    message_id: str
    admission_hash: str


def _fail() -> MailAdmissionCandidateError:
    return MailAdmissionCandidateError()


def _positive(value: object) -> int:
    if type(value) is not int or not 0 < value <= _SQLITE_MAX:
        raise _fail()
    return value


def _hash(value: object) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise _fail()
    return value


def _identity(value: object) -> str:
    if type(value) is not str or not value or len(value) > _MAX_IDENTITY_CHARS:
        raise _fail()
    if value != value.strip():
        raise _fail()
    for char in value:
        point = ord(char)
        if point < 32 or 127 <= point <= 159 or point in (0x2028, 0x2029):
            raise _fail()
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fail() from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _r1_recipients_hash(value: object) -> tuple[tuple[str, ...], str]:
    try:
        recipients = canonicalize_to_recipients(value)
    except RecipientConfigError as exc:
        raise _fail() from exc
    canonical_json = json.dumps(list(recipients), ensure_ascii=True, separators=(",", ":"))
    return recipients, _sha256(canonical_json)


def _message_id(formal_report_hash: str, render_identity: str, render_hash: str) -> str:
    # A confirmed module explanation changes the delivered document while preserving
    # the original report approval. Keep frozen IDs, but give each new document its
    # own ID so a corrected mail cannot be deduplicated as the earlier document.
    if render_identity.startswith("mail_report_render_v11:"):
        return f"<anxin-r11-{render_hash[:32]}@anxinboard.local>"
    if render_identity.startswith("mail_report_render_v10:"):
        return f"<anxin-r10-{render_hash[:32]}@anxinboard.local>"
    if render_identity.startswith("mail_report_render_v9:"):
        return f"<anxin-r9-{render_hash[:32]}@anxinboard.local>"
    return f"<anxin-{formal_report_hash[:32]}@anxinboard.local>"


def _candidate_payload(candidate: MailAdmissionCandidate) -> dict[str, object]:
    if not isinstance(candidate, MailAdmissionCandidate) or candidate.schema_version != SCHEMA_VERSION:
        raise _fail()

    project_id = _positive(candidate.project_id)
    report_version_id = _positive(candidate.report_version_id)
    approval_snapshot_id = _positive(candidate.approval_snapshot_id)
    recipient_config_version_id = _positive(candidate.recipient_config_version_id)
    recipient_config_version_no = _positive(candidate.recipient_config_version_no)
    transport_profile_id = _positive(candidate.transport_profile_id)
    transport_profile_version_no = _positive(candidate.transport_profile_version_no)

    report_content_hash = _hash(candidate.report_content_hash)
    approval_snapshot_hash = _hash(candidate.approval_snapshot_hash)
    binding_hash = _hash(candidate.approval_recipient_binding_hash)
    recipients_hash = _hash(candidate.recipients_hash)
    transport_profile_hash = _hash(candidate.transport_profile_hash)
    formal_report_hash = _hash(candidate.formal_report_hash)
    render_hash = _hash(candidate.render_hash)
    html_sha256 = _hash(candidate.html_sha256)
    admission_hash = _hash(candidate.admission_hash)
    render_identity = _identity(candidate.render_identity)

    if type(candidate.to_recipients) is not tuple or not candidate.to_recipients:
        raise _fail()
    canonical_recipients, expected_recipients_hash = _r1_recipients_hash(candidate.to_recipients)
    if canonical_recipients != candidate.to_recipients or expected_recipients_hash != recipients_hash:
        raise _fail()
    try:
        secret_ref = validate_secret_ref(candidate.secret_ref)
        message = MailMessage(
            message_id=candidate.message_id,
            from_identity=candidate.from_identity,
            to_recipients=candidate.to_recipients,
            subject=candidate.subject,
            html_body="<p>mail-admission-candidate-validation</p>",
        )
    except (InvalidSecretReferenceError, MailContractError) as exc:
        raise _fail() from exc
    if message.to_recipients != candidate.to_recipients:
        raise _fail()
    expected_message_id = _message_id(formal_report_hash, render_identity, render_hash)
    if message.message_id != expected_message_id:
        raise _fail()

    payload = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "report_version_id": report_version_id,
        "report_content_hash": report_content_hash,
        "approval_snapshot_id": approval_snapshot_id,
        "approval_snapshot_hash": approval_snapshot_hash,
        "approval_recipient_binding_hash": binding_hash,
        "recipient_config_version_id": recipient_config_version_id,
        "recipient_config_version_no": recipient_config_version_no,
        "recipients_hash": recipients_hash,
        "to_recipients": list(message.to_recipients),
        "transport_profile_id": transport_profile_id,
        "transport_profile_version_no": transport_profile_version_no,
        "transport_profile_hash": transport_profile_hash,
        "secret_ref": secret_ref,
        "from_identity": message.from_identity,
        "formal_report_hash": formal_report_hash,
        "render_identity": render_identity,
        "render_hash": render_hash,
        "html_sha256": html_sha256,
        "subject": message.subject,
        "message_id": message.message_id,
    }
    if admission_hash != _sha256(_canonical_json(payload)):
        raise _fail()
    return payload


def validate_mail_admission_candidate(candidate: MailAdmissionCandidate) -> MailAdmissionCandidate:
    """Re-close a frozen candidate before any durable R4 consumer trusts it."""
    _candidate_payload(candidate)
    return candidate


def build_mail_admission_candidate(
    *,
    approval_recipient_binding: Mapping[str, object],
    approval_snapshot: Mapping[str, object],
    recipient_config: Mapping[str, object],
    formal_report: Mapping[str, object],
    render: MailReportRender,
    transport_profile: MailTransportProfile,
) -> MailAdmissionCandidate:
    """Cross-close immutable facts without credential access, persistence or send execution."""
    if not isinstance(transport_profile, MailTransportProfile):
        raise _fail()
    try:
        render = validate_mail_report_render(render)
    except MailReportRenderError as exc:
        raise _fail() from exc

    project_id = _positive(approval_recipient_binding.get("project_id"))
    report_version_id = _positive(approval_recipient_binding.get("report_version_id"))
    report_content_hash = _hash(approval_recipient_binding.get("report_content_hash"))
    approval_snapshot_id = _positive(approval_recipient_binding.get("approval_snapshot_id"))
    approval_snapshot_hash = _hash(approval_recipient_binding.get("approval_snapshot_hash"))
    binding_hash = _hash(approval_recipient_binding.get("binding_hash"))
    config_id = _positive(approval_recipient_binding.get("recipient_config_version_id"))
    config_no = _positive(approval_recipient_binding.get("recipient_config_version_no"))
    recipients_hash = _hash(approval_recipient_binding.get("recipients_hash"))

    if (
        approval_snapshot.get("approval_snapshot_id") != approval_snapshot_id
        or approval_snapshot.get("project_id") != project_id
        or approval_snapshot.get("report_version_id") != report_version_id
        or approval_snapshot.get("report_content_hash") != report_content_hash
        or approval_snapshot.get("approval_snapshot_hash") != approval_snapshot_hash
    ):
        raise _fail()

    if (
        recipient_config.get("id") != config_id
        or recipient_config.get("version_no") != config_no
        or recipient_config.get("project_id") != project_id
        or recipient_config.get("recipients_hash") != recipients_hash
    ):
        raise _fail()
    raw_recipients = recipient_config.get("to_recipients")
    bound_recipients = approval_recipient_binding.get("to_recipients")
    if type(raw_recipients) is not list or type(bound_recipients) is not list:
        raise _fail()
    try:
        to_recipients, expected_recipients_hash = _r1_recipients_hash(raw_recipients)
    except MailAdmissionCandidateError:
        raise
    if (
        not to_recipients
        or tuple(bound_recipients) != to_recipients
        or tuple(raw_recipients) != to_recipients
        or expected_recipients_hash != recipients_hash
    ):
        raise _fail()

    formal_report_hash = _hash(formal_report.get("anxin_board_report_hash"))
    if (
        formal_report.get("schema_version") != "anxin_board_report_v3"
        or formal_report.get("approval_snapshot_id") != approval_snapshot_id
        or formal_report.get("approval_snapshot_hash") != approval_snapshot_hash
        or formal_report.get("report_version_id") != report_version_id
        or formal_report.get("report_content_hash") != report_content_hash
        or render.report_hash != formal_report_hash
    ):
        raise _fail()

    profile_id = _positive(transport_profile.id)
    profile_version = _positive(transport_profile.version_no)
    profile_hash = _hash(transport_profile.profile_hash)
    if transport_profile.schema_version != "mail_transport_profile_v1":
        raise _fail()

    message_id = _message_id(formal_report_hash, render.render_identity, render.render_hash)
    try:
        message = MailMessage(
            message_id=message_id,
            from_identity=transport_profile.from_identity,
            to_recipients=to_recipients,
            subject=render.subject,
            html_body=render.html_body,
        )
    except MailContractError as exc:
        raise _fail() from exc
    if message.body_identity_sha256 != render.html_sha256:
        raise _fail()

    payload = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "report_version_id": report_version_id,
        "report_content_hash": report_content_hash,
        "approval_snapshot_id": approval_snapshot_id,
        "approval_snapshot_hash": approval_snapshot_hash,
        "approval_recipient_binding_hash": binding_hash,
        "recipient_config_version_id": config_id,
        "recipient_config_version_no": config_no,
        "recipients_hash": recipients_hash,
        "to_recipients": list(message.to_recipients),
        "transport_profile_id": profile_id,
        "transport_profile_version_no": profile_version,
        "transport_profile_hash": profile_hash,
        "secret_ref": transport_profile.secret_ref,
        "from_identity": message.from_identity,
        "formal_report_hash": formal_report_hash,
        "render_identity": render.render_identity,
        "render_hash": render.render_hash,
        "html_sha256": render.html_sha256,
        "subject": message.subject,
        "message_id": message.message_id,
    }
    admission_hash = _sha256(_canonical_json(payload))
    candidate = MailAdmissionCandidate(
        schema_version=SCHEMA_VERSION,
        project_id=project_id,
        report_version_id=report_version_id,
        report_content_hash=report_content_hash,
        approval_snapshot_id=approval_snapshot_id,
        approval_snapshot_hash=approval_snapshot_hash,
        approval_recipient_binding_hash=binding_hash,
        recipient_config_version_id=config_id,
        recipient_config_version_no=config_no,
        recipients_hash=recipients_hash,
        to_recipients=message.to_recipients,
        transport_profile_id=profile_id,
        transport_profile_version_no=profile_version,
        transport_profile_hash=profile_hash,
        secret_ref=transport_profile.secret_ref,
        from_identity=message.from_identity,
        formal_report_hash=formal_report_hash,
        render_identity=render.render_identity,
        render_hash=render.render_hash,
        html_sha256=render.html_sha256,
        subject=message.subject,
        message_id=message.message_id,
        admission_hash=admission_hash,
    )
    return validate_mail_admission_candidate(candidate)
