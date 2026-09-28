"""Read-only product readiness projection for formal report mail.

The projection composes already-owned authorities and reports whether the explicit
Page08 send action can be offered. It never reads SecretStore, creates a SendAttempt,
invokes MailGateway/SMTP, mutates report approval/recipient binding, or advances
checkpoints. Actual send permission still requires the protected local POST action and
an explicit Human confirmation.
"""

from __future__ import annotations

from dataclasses import asdict
import sqlite3

from fastapi import HTTPException

from app.anxin_board_report_store import (
    AnxinBoardReportProjectNotFoundError,
    AnxinBoardReportStoredInvalidError,
    load_latest_anxin_board_report,
)
from app.db import get_connection
from app.mail_admission_candidate import (
    MailAdmissionCandidateError,
    build_mail_admission_candidate,
)
from app.mail_report_renderer import MailReportRenderError, render_approved_report_for_mail
from app.approved_report_narrative import ApprovedReportNarrativeError, load_approved_report_narrative
from app.approved_module_narrative import ApprovedModuleNarrativeError, get_approved_module_narrative
from app.approved_report_git_metrics import ApprovedReportGitMetricsError, load_approved_report_git_metrics
from app.mail_transport_profile import (
    MailTransportProfileError,
    get_current_mail_transport_profile,
)
from app.project_profiles import (
    ProjectProfileAuthorityError,
    read_bound_project_profile_for_report,
)
from app.recipient_config import RecipientConfigError, get_current_recipient_config
from app.report_approval import get_report_approval_snapshot
from app.report_approval_recipient_binding import (
    ApprovalRecipientBindingError,
    get_approval_recipient_binding,
)


SCHEMA_VERSION = "mail_readiness_v1"
_BINDING_TABLE = "report_approval_recipient_bindings"


class MailReadinessError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> MailReadinessError:
    return MailReadinessError(code)


def _project_exists(project_id: int) -> bool:
    if type(project_id) is not int or project_id <= 0:
        raise _fail("MAIL_READINESS_INPUT_INVALID")
    try:
        with get_connection() as conn:
            return conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None
    except sqlite3.Error as exc:
        raise _fail("MAIL_READINESS_STORAGE_INVALID") from exc


def _table_exists(table_name: str) -> bool:
    """Pure schema presence probe; importantly does not initialize R3 on GET."""
    try:
        with get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone() is not None
    except sqlite3.Error as exc:
        raise _fail("MAIL_READINESS_STORAGE_INVALID") from exc


def _check(code: str, ready: bool, summary: str) -> dict[str, object]:
    return {"code": code, "ready": ready, "summary": summary}


def _blocked(
    project_id: int,
    checks: list[dict[str, object]],
    *,
    send_action_available: bool = False,
    state: str = "blocked",
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "state": state,
        "candidate_ready": False,
        "send_action_available": send_action_available,
        "checks": checks,
        "candidate": None,
    }


def _candidate_projection(candidate) -> dict[str, object]:
    # Deliberately exclude secret_ref and full recipient addresses from the generic
    # readiness projection. Recipient settings own Human-visible addresses.
    data = asdict(candidate)
    return {
        "admission_hash": data["admission_hash"],
        "message_id": data["message_id"],
        "report_version_id": data["report_version_id"],
        "approval_snapshot_id": data["approval_snapshot_id"],
        "recipient_config_version_no": data["recipient_config_version_no"],
        "recipient_count": len(data["to_recipients"]),
        "transport_profile_version_no": data["transport_profile_version_no"],
        "formal_report_hash": data["formal_report_hash"],
        "render_hash": data["render_hash"],
        "html_sha256": data["html_sha256"],
        "subject": data["subject"],
    }


def get_mail_readiness(project_id: int) -> dict[str, object]:
    """Return current immutable-mail facts without performing any send side effect."""
    if not _project_exists(project_id):
        raise _fail("MAIL_READINESS_PROJECT_NOT_FOUND")

    checks: list[dict[str, object]] = []

    try:
        transport = get_current_mail_transport_profile() if _table_exists("mail_transport_profile_versions") else None
    except MailTransportProfileError as exc:
        raise _fail("MAIL_READINESS_TRANSPORT_INVALID") from exc
    checks.append(
        _check(
            "transport_profile",
            transport is not None,
            "SMTP 配置已保存。" if transport is not None else "还没有保存 SMTP 配置。",
        )
    )

    try:
        recipient_config = get_current_recipient_config(project_id)
    except RecipientConfigError as exc:
        raise _fail("MAIL_READINESS_RECIPIENT_CONFIG_INVALID") from exc
    checks.append(
        _check(
            "recipient_config",
            recipient_config is not None,
            (
                f"当前收件人配置为 V{recipient_config['version_no']}，共 {len(recipient_config['to_recipients'])} 个地址。"
                if recipient_config is not None
                else "还没有保存项目收件人。"
            ),
        )
    )

    try:
        report = load_latest_anxin_board_report(project_id=project_id)
    except AnxinBoardReportProjectNotFoundError as exc:
        raise _fail("MAIL_READINESS_PROJECT_NOT_FOUND") from exc
    except AnxinBoardReportStoredInvalidError as exc:
        raise _fail("MAIL_READINESS_REPORT_INVALID") from exc

    formal_ready = isinstance(report, dict) and report.get("schema_version") == "anxin_board_report_v3"
    checks.append(
        _check(
            "formal_report",
            formal_ready,
            "已有正式确认版安心看板。" if formal_ready else "还没有可用于邮件的正式确认版安心看板。",
        )
    )

    if not formal_ready or transport is None or recipient_config is None:
        checks.append(_check("approval_recipient_binding", False, "前置配置未闭合，暂不能确认报告与收件人的正式绑定。"))
        checks.append(_check("mail_render", False, "前置配置未闭合，暂不生成正式邮件正文 identity。"))
        checks.append(_check("admission_candidate", False, "邮件身份链尚未闭合。"))
        return _blocked(project_id, checks)

    report_version_id = report.get("report_version_id")
    if type(report_version_id) is not int or report_version_id <= 0:
        raise _fail("MAIL_READINESS_REPORT_INVALID")

    binding = None
    if _table_exists(_BINDING_TABLE):
        try:
            binding = get_approval_recipient_binding(
                project_id=project_id,
                report_version_id=report_version_id,
            )
        except ApprovalRecipientBindingError as exc:
            raise _fail("MAIL_READINESS_BINDING_INVALID") from exc

    binding_current = bool(
        binding is not None
        and binding.get("recipient_config_version_id") == recipient_config.get("id")
        and binding.get("recipient_config_version_no") == recipient_config.get("version_no")
        and binding.get("recipients_hash") == recipient_config.get("recipients_hash")
    )
    checks.append(
        _check(
            "approval_recipient_binding",
            binding_current,
            (
                "正式报告已绑定当前收件人版本。"
                if binding_current
                else (
                    "收件人配置已变化；这份已确认报告不能静默切换到新的收件人。"
                    if binding is not None
                    else "发送时将把这份正式报告绑定到当前收件人版本。"
                )
            ),
        )
    )
    if not binding_current:
        checks.append(_check("mail_render", False, "完成发送确认后再冻结本次邮件正文 identity。"))
        checks.append(_check("admission_candidate", False, "完成发送确认后再闭合本次邮件身份链。"))
        if binding is None:
            return _blocked(
                project_id,
                checks,
                send_action_available=True,
                state="send_confirmation_required",
            )
        return _blocked(project_id, checks)

    try:
        approval_snapshot = get_report_approval_snapshot(
            project_id=project_id,
            report_version_id=report_version_id,
        )
    except HTTPException as exc:
        raise _fail("MAIL_READINESS_APPROVAL_INVALID") from exc
    if not isinstance(approval_snapshot, dict):
        raise _fail("MAIL_READINESS_APPROVAL_INVALID")

    profile_id = report.get("profile_id")
    if type(profile_id) is not int or profile_id <= 0:
        raise _fail("MAIL_READINESS_REPORT_INVALID")
    try:
        profile = read_bound_project_profile_for_report(profile_id)
    except ProjectProfileAuthorityError as exc:
        raise _fail("MAIL_READINESS_PROFILE_INVALID") from exc

    try:
        narrative = load_approved_report_narrative(
            project_id=project_id, report=report, approval_snapshot=approval_snapshot,
        )
        module_narrative = get_approved_module_narrative(
            project_id=project_id, report=report, approval_snapshot=approval_snapshot, profile=profile,
        )
        git_metrics = load_approved_report_git_metrics(project_id=project_id, report=report, approval_snapshot=approval_snapshot)
        render = render_approved_report_for_mail(
            report,
            profile=profile,
            approval_snapshot=approval_snapshot,
            approved_narrative=narrative,
            approved_module_narrative=module_narrative,
            approved_git_metrics=git_metrics,
        )
    except (ApprovedReportNarrativeError, ApprovedModuleNarrativeError) as exc:
        raise _fail("MAIL_READINESS_NARRATIVE_INVALID") from exc
    except (MailReportRenderError, ApprovedReportGitMetricsError) as exc:
        raise _fail("MAIL_READINESS_RENDER_INVALID") from exc
    checks.append(_check("mail_render", True, "正式邮件正文 identity 已从确认版安心看板确定生成。"))

    try:
        candidate = build_mail_admission_candidate(
            approval_recipient_binding=binding,
            approval_snapshot=approval_snapshot,
            recipient_config=recipient_config,
            formal_report=report,
            render=render,
            transport_profile=transport,
        )
    except MailAdmissionCandidateError as exc:
        raise _fail("MAIL_READINESS_CANDIDATE_INVALID") from exc

    checks.append(_check("admission_candidate", True, "报告、收件人、SMTP 配置与邮件正文 identity 已互相闭合。"))
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "state": "candidate_ready",
        "candidate_ready": True,
        "send_action_available": True,
        "checks": checks,
        "candidate": _candidate_projection(candidate),
    }
