"""One explicitly confirmed module annotation may revise a delivered document.

This is not a transport retry: the prior mail must be sent, the report/approval/
recipients remain frozen, and the new document has its own Message-ID.
"""
import re


def revision_sql():
    """Correlated trigger predicate; source owners are reclosed by Python as well."""
    return """EXISTS (
        SELECT 1 FROM approved_module_narratives n
        JOIN mail_send_attempts predecessor
          ON predecessor.send_attempt_id=NEW.predecessor_send_attempt_id
        WHERE n.project_id=NEW.project_id AND n.report_version_id=NEW.report_version_id
          AND n.schema_version='approved_module_narrative_v1'
          AND json_extract(n.narrative_json,'$.approval_snapshot_id')=NEW.approval_snapshot_id
          AND json_extract(n.narrative_json,'$.approval_snapshot_hash')=NEW.approval_snapshot_hash
          AND json_extract(n.narrative_json,'$.report_content_hash')=NEW.report_content_hash
          AND json_extract(n.narrative_json,'$.module_narrative_hash')=n.narrative_hash
          AND NEW.render_identity='mail_report_render_v11:' || json_extract(n.narrative_json,'$.anxin_board_report_hash') || ':' || n.narrative_hash
          AND NEW.message_id='<anxin-r11-' || substr(NEW.render_hash,1,32) || '@anxinboard.local>'
          AND predecessor.project_id=NEW.project_id AND predecessor.state='sent'
          AND predecessor.report_version_id=NEW.report_version_id
          AND predecessor.report_content_hash=NEW.report_content_hash
          AND predecessor.approval_snapshot_id=NEW.approval_snapshot_id
          AND predecessor.approval_snapshot_hash=NEW.approval_snapshot_hash
          AND predecessor.recipients_json=NEW.recipients_json
          AND predecessor.recipients_hash=NEW.recipients_hash
          AND predecessor.render_identity<>NEW.render_identity
          AND prior.report_version_id=NEW.report_version_id
          AND prior.report_content_hash=NEW.report_content_hash
          AND prior.approval_snapshot_id=NEW.approval_snapshot_id
          AND prior.approval_snapshot_hash=NEW.approval_snapshot_hash
          AND prior.recipients_json=NEW.recipients_json
          AND prior.recipients_hash=NEW.recipients_hash
    )"""


def validated_module_revision(conn, prior, values):
    """Admit only current v11 revisions with an intact confirmed annotation."""
    match = re.fullmatch(r'mail_report_render_v11:([0-9a-f]{64}):([0-9a-f]{64})', values['render_identity'])
    if not match or prior.state != 'sent' or prior.render_identity == values['render_identity']:
        return False
    pairs = (
        (prior.project_id, values['project_id']),
        (prior.report_version_id, values['report_version_id']),
        (prior.report_content_hash, values['report_content_hash']),
        (prior.approval_snapshot_id, values['approval_snapshot_id']),
        (prior.approval_snapshot_hash, values['approval_snapshot_hash']),
        (prior.recipients_hash, values['recipients_hash']),
        (prior.send_attempt_id, values['predecessor_send_attempt_id']),
    )
    if any(left != right for left, right in pairs):
        return False
    if values['message_id'] != f"<anxin-r11-{values['render_hash'][:32]}@anxinboard.local>":
        return False
    from app.approved_module_narrative import load_module_narrative_target, get_approved_module_narrative
    try:
        report, approval, profile = load_module_narrative_target(
            conn, values['project_id'], values['report_version_id'], match[1],
        )
        annotation = get_approved_module_narrative(values['project_id'], report, approval, profile, conn=conn)
        return bool(annotation and annotation['module_narrative_hash'] == match[2]
                    and approval['approval_snapshot_id'] == values['approval_snapshot_id']
                    and approval['approval_snapshot_hash'] == values['approval_snapshot_hash'])
    except Exception:
        return False


def current_document_predecessor(project_id, report_version_id, render):
    """Retain exact retries; link a confirmed revision to the last delivered mail."""
    from app.mail_send_attempts import list_send_attempt_history, MailSendAttemptError
    previous = [a for a in list_send_attempt_history(project_id) if a.report_version_id == report_version_id]
    if not previous:
        return None
    latest = previous[-1]
    if latest.render_identity == render.render_identity and latest.render_hash == render.render_hash:
        return latest.predecessor_send_attempt_id
    if latest.state != 'sent' or not re.fullmatch(r'mail_report_render_v11:[0-9a-f]{64}:[0-9a-f]{64}', render.render_identity):
        raise MailSendAttemptError('MAIL_SEND_DOCUMENT_REVISION_NOT_CONFIRMED')
    # create_send_attempt rechecks the full annotation and latest lineage at insertion.
    return latest.send_attempt_id
