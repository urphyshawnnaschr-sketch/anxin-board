"""Read-only closure of the current formal customer document, without SMTP admission."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import re

from app.db import get_connection
from app import mail_send_service
from app.mail_brand_asset import standalone_customer_html


class DeliveryError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def authority_stamp(conn, project_id, report_version_id):
    """Short SQLite-only check used again inside each claim transaction.

    Rendering happens outside writer transactions. Capture mutable lifecycle and
    latest-report selection, plus the append-only annotation presence, to close
    the gap between the full renderer validation and reserving a send.
    """
    version = conn.execute('SELECT * FROM report_versions WHERE id=? AND project_id=?',
                           (report_version_id, project_id)).fetchone()
    if version is None or version['lifecycle'] != 'approved':
        raise DeliveryError('WECHAT_FORMAL_REPORT_REQUIRED')
    facts = [dict(version)]
    queries = (
        ('SELECT * FROM projects WHERE id=?', (project_id,)),
        ('SELECT * FROM anxin_board_reports WHERE project_id=? ORDER BY id DESC LIMIT 1', (project_id,)),
        ('SELECT * FROM report_approval_snapshots WHERE project_id=? AND report_version_id=?',
         (project_id, report_version_id)),
    )
    for sql, args in queries:
        facts.append([dict(row) for row in conn.execute(sql, args)])
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approved_module_narratives'").fetchone():
        facts.append([dict(row) for row in conn.execute(
            'SELECT * FROM approved_module_narratives WHERE project_id=? AND report_version_id=?',
            (project_id, report_version_id))])
    else:
        facts.append([])
    return digest(facts)


@dataclass(frozen=True)
class ApprovedDocument:
    report_version_id: int
    report_hash: str
    module_narrative_hash: str | None
    report_label: str
    html: str
    document_hash: str
    authority_hash: str


def load_approved_document(project_id, expected_report_version_id, expected_report_hash,
                           expected_module_narrative_hash):
    try:
        with closing(get_connection()) as conn:
            before = authority_stamp(conn, project_id, expected_report_version_id)
        report = mail_send_service._latest_v3(project_id)
        if (report.get('report_version_id') != expected_report_version_id
                or report.get('anxin_board_report_hash') != expected_report_hash):
            raise DeliveryError('WECHAT_REPORT_STALE')
        _, rendered = mail_send_service._render_exact_current(
            project_id=project_id, expected_report_version_id=expected_report_version_id,
            expected_report_content_hash=report['report_content_hash'])
        match = re.fullmatch(r'mail_report_render_v11:[0-9a-f]{64}:([0-9a-f]{64}|none)', rendered.render_identity)
        if match is None:
            raise DeliveryError('WECHAT_REPORT_RENDER_INVALID')
        module_hash = None if match[1] == 'none' else match[1]
        if module_hash != expected_module_narrative_hash:
            raise DeliveryError('WECHAT_MODULE_NARRATIVE_STALE')
        html = standalone_customer_html(rendered.html_body)
        if not html:
            raise DeliveryError('WECHAT_REPORT_RENDER_INVALID')
        with closing(get_connection()) as conn:
            if authority_stamp(conn, project_id, expected_report_version_id) != before:
                raise DeliveryError('WECHAT_REPORT_STALE')
        return ApprovedDocument(expected_report_version_id, expected_report_hash, module_hash,
                                rendered.subject, html, hashlib.sha256(html.encode()).hexdigest(), before)
    except DeliveryError:
        raise
    except Exception:
        # Stored report/provider text is never part of HTTP errors or logs.
        raise DeliveryError('WECHAT_REPORT_INVALID') from None
