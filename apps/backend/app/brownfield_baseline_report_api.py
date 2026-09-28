"""Session-protected, zero-model HTML views of persisted Atlas baseline evidence."""
from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path
import re
import stat
import sys

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import brownfield_baseline_store as store
from app.brownfield_baseline_report import BaselineReportError, CSP, render_baseline_report, valid_brand_png
from app.db import get_connection
from app.local_session_api import require_local_read_request
from app.project_profiles import read_current_confirmed_project_profile, ProjectProfileAuthorityError


router = APIRouter(prefix='/api/projects/{project_id}/project-state-baseline/atlas/tasks')
MESSAGES = {
    'BROWNFIELD_REPORT_NOT_FOUND': '没有找到本项目可展示的代码盘点结果。',
    'BROWNFIELD_REPORT_IDENTITY_INVALID': '代码盘点结果与档案内容无法对应，请重新核对。',
    'BROWNFIELD_REPORT_CONFIRMATION_REQUIRED': '请先在产品中审阅并确认当前档案，再下载代码盘点。',
    'BROWNFIELD_REPORT_SCOPE_CHANGED': '当前档案或代码范围已变化，可预览历史结果；请核对范围后再下载。',
    'BROWNFIELD_REPORT_SENSITIVE_TEXT': '结果包含需要安全核对的内容，暂不能展示或下载。',
    'BROWNFIELD_REPORT_READ_FAILED': '暂时无法读取代码盘点结果，请稍后重试。',
}


def _error(code, status=409):
    return HTTPException(status_code=status, detail={'code': code, 'message': MESSAGES[code]})


def _plain_asset_path(path):
    info = path.lstat()
    return not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _read_brand_png(assets):
    """One immutable bundled PNG only; no recursion, arbitrary path or network fallback."""
    try:
        if not assets.is_dir() or not _plain_asset_path(assets.parent) or not _plain_asset_path(assets):
            return None
        matches = [p for p in assets.iterdir() if re.fullmatch(r'anxin-board-calligraphy-[A-Za-z0-9_-]{8,32}\.png', p.name)]
        if len(matches) != 1:
            return None
        path = matches[0]
        if not _plain_asset_path(path) or not path.is_file() or path.resolve().parent != assets.resolve() or path.stat().st_size > 65536:
            return None
        with path.open('rb') as source:
            value = source.read(65537)
        return value if valid_brand_png(value) else None
    except OSError:
        return None


def _brand_png():
    assets = (Path(sys.executable).resolve().parent / 'ui' / 'assets' if getattr(sys, 'frozen', False)
              else Path(__file__).resolve().parents[2] / 'frontend' / 'dist' / 'assets')
    return _read_brand_png(assets)


def _require_current_scope(conn, project, profile, task):
    try:
        current = read_current_confirmed_project_profile(project['id'], conn=conn)
    except ProjectProfileAuthorityError:
        raise _error('BROWNFIELD_REPORT_SCOPE_CHANGED') from None
    identity = task['identity']
    git = conn.execute('SELECT * FROM project_git_connections WHERE project_id=?', (project['id'],)).fetchone()
    prd = conn.execute("SELECT id,source_hash FROM prd_versions WHERE id=? AND project_id=? AND status='parse_confirmed'",
                       (identity['prd_id'], project['id'])).fetchone()
    valid = (current['id'] == profile['id'] and current['content_hash'] == profile['content_hash']
             and current['source_prd_id'] == identity['prd_id']
             and project['git_url'] == identity['git_url'] and project['branch'] == identity['git_branch']
             and git is not None and git['status'] == 'connected'
             and git['checked_url_hash'] == hashlib.sha256(project['git_url'].encode('utf-8')).hexdigest()
             and git['checked_branch'] == project['branch']
             and git['local_head'] == git['remote_head'] == identity['exact_head']
             and prd is not None and prd['source_hash'] == identity['prd_source_hash'])
    if not valid:
        raise _error('BROWNFIELD_REPORT_SCOPE_CHANGED')


def _html(project_id, task_id, request, *, preview):
    # Guard before opening the database. No writes, worker admission, provider or Git access.
    require_local_read_request(request)
    try:
        with closing(get_connection()) as conn:
            conn.execute('PRAGMA query_only = ON')
            conn.execute('BEGIN')
            task = store.get_task(conn, task_id)
            project_row = conn.execute('SELECT id,name,git_url,branch FROM projects WHERE id=?', (project_id,)).fetchone()
            if task is None or task['project_id'] != project_id or project_row is None:
                raise _error('BROWNFIELD_REPORT_NOT_FOUND', 404)
            profile_row = conn.execute('SELECT id,project_id,status,content_json,content_hash,confirmed_by,confirmed_at FROM project_profiles WHERE id=? AND project_id=?',
                                       (task['profile_id'], project_id)).fetchone()
            saved = store.get_output(conn, task_id)
            if profile_row is None or saved is None:
                raise _error('BROWNFIELD_REPORT_NOT_FOUND', 404)
            project, profile = dict(project_row), dict(profile_row)
            profile['content'] = json.loads(profile.pop('content_json'))
            # Re-close the output ledger checksum as well as the profile content checksum.
            encoded = json.dumps(saved['output'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if hashlib.sha256(encoded.encode('utf-8')).hexdigest() != saved['output_hash']:
                raise _error('BROWNFIELD_REPORT_IDENTITY_INVALID')
            if not preview:
                if profile['status'] != 'confirmed':
                    raise _error('BROWNFIELD_REPORT_CONFIRMATION_REQUIRED')
                _require_current_scope(conn, project, profile, task)
            html = render_baseline_report(project=project, profile=profile, task=task, output=saved['output'], preview=preview, brand_png=_brand_png())
        disposition = 'inline' if preview else f'attachment; filename="anxin-board-baseline-{project_id}-{profile["id"]}.html"'
        return HTMLResponse(html, headers={'Content-Disposition': disposition, 'Cache-Control': 'no-store',
                            'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff',
                            'Referrer-Policy': 'no-referrer'})
    except HTTPException:
        raise
    except BaselineReportError as exc:
        code = str(exc)
        raise _error(code if code in MESSAGES else 'BROWNFIELD_REPORT_IDENTITY_INVALID') from None
    except sqlite3.Error:
        raise _error('BROWNFIELD_REPORT_READ_FAILED', 503) from None
    except Exception:
        raise _error('BROWNFIELD_REPORT_IDENTITY_INVALID') from None


@router.get('/{task_id}/review-html', response_class=HTMLResponse)
def preview_baseline_html(project_id: int, task_id: str, request: Request):
    return _html(project_id, task_id, request, preview=True)


@router.get('/{task_id}/export-html', response_class=HTMLResponse)
def export_baseline_html(project_id: int, task_id: str, request: Request):
    return _html(project_id, task_id, request, preview=False)
