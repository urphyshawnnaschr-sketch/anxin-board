"""SQLite authority for immutable previews and at-most-once native-image delivery."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from uuid import uuid4
import re

from app.db import get_connection
from app.approved_report_delivery import DeliveryError, authority_stamp

_SAFE_CONFIG_REJECTIONS = frozenset({
    'GATEWAY_AUTH_REJECTED','GATEWAY_POLICY_REJECTED','GATEWAY_UPLOADS_DISABLED',
    'GATEWAY_TOOL_UNAVAILABLE','GATEWAY_INPUT_INVALID',
})


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_account(value):
    """Match OpenClaw 4735f663 normalization-core for explicit account IDs.

    Empty values stay empty for the local disconnected sentinel; callers never
    fall back to an implicit default account when accepting configuration.
    """
    if type(value) is not str:return value
    lowered=value.strip().lower()
    if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',lowered):return lowered
    return re.sub(r'[^a-z0-9_-]+','-',lowered).strip('-')[:64]


@contextmanager
def transaction():
    with closing(get_connection()) as conn:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn


def ensure_wechat_delivery_schema():
    with closing(get_connection()) as conn, conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS wechat_configs (
          project_id INTEGER NOT NULL, version_no INTEGER NOT NULL,
          gateway_url TEXT NOT NULL, account_id TEXT NOT NULL, target TEXT NOT NULL,
          recipient_label TEXT NOT NULL, session_key TEXT NOT NULL, secret_ref TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(project_id,version_no));
        CREATE TABLE IF NOT EXISTS wechat_previews (
          preview_id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, report_version_id INTEGER NOT NULL,
          report_hash TEXT NOT NULL, module_narrative_hash TEXT, report_label TEXT NOT NULL,
          document_hash TEXT NOT NULL, document_target_hash TEXT NOT NULL, authority_hash TEXT NOT NULL,
          config_version_no INTEGER NOT NULL, recipient_label TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS wechat_preview_images (
          preview_id TEXT NOT NULL, page_index INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
          sha256 TEXT NOT NULL, png BLOB NOT NULL, PRIMARY KEY(preview_id,page_index));
        CREATE TABLE IF NOT EXISTS wechat_attempts (
          attempt_id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, preview_id TEXT NOT NULL UNIQUE,
          document_target_hash TEXT NOT NULL, report_version_id INTEGER NOT NULL,
          recipient_label TEXT NOT NULL, created_at TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('sending','accepted','partial','failed','unknown')),
          config_version_no INTEGER NOT NULL CHECK(config_version_no>0),
          sequence_no INTEGER NOT NULL CHECK(sequence_no>0), UNIQUE(document_target_hash,sequence_no));
        CREATE TABLE IF NOT EXISTS wechat_request_keys (
          project_id INTEGER NOT NULL, idempotency_key TEXT NOT NULL, document_target_hash TEXT NOT NULL,
          attempt_id TEXT NOT NULL, PRIMARY KEY(project_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS wechat_preview_attempt_bindings (
          preview_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS wechat_attempt_pages (
          attempt_id TEXT NOT NULL, page_index INTEGER NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('not_sent','sending','accepted','rejected','unknown')),
          code TEXT NOT NULL, PRIMARY KEY(attempt_id,page_index));
        CREATE INDEX IF NOT EXISTS idx_wechat_history ON wechat_attempts(project_id,created_at DESC);
        ''')
        if 'transport' not in {r[1] for r in conn.execute('PRAGMA table_info(wechat_configs)')}:
            conn.execute("ALTER TABLE wechat_configs ADD COLUMN transport TEXT NOT NULL DEFAULT 'openclaw'")
        for table in ('wechat_configs', 'wechat_previews', 'wechat_preview_images', 'wechat_request_keys',
                      'wechat_preview_attempt_bindings'):
            for action in ('UPDATE', 'DELETE'):
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                    BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable WeChat record'); END''')
        conn.executescript('''
        CREATE TRIGGER IF NOT EXISTS wechat_attempts_identity
        BEFORE UPDATE OF attempt_id,project_id,preview_id,document_target_hash,report_version_id,recipient_label,created_at,config_version_no,sequence_no
        ON wechat_attempts BEGIN SELECT RAISE(ABORT,'immutable WeChat attempt'); END;
        CREATE TRIGGER IF NOT EXISTS wechat_attempts_no_delete BEFORE DELETE ON wechat_attempts
        BEGIN SELECT RAISE(ABORT,'immutable WeChat attempt'); END;
        CREATE TRIGGER IF NOT EXISTS wechat_attempts_transition BEFORE UPDATE OF state ON wechat_attempts
        WHEN OLD.state <> 'sending' OR NEW.state = 'sending'
        BEGIN SELECT RAISE(ABORT,'terminal WeChat attempt'); END;
        CREATE TRIGGER IF NOT EXISTS wechat_pages_identity BEFORE UPDATE OF attempt_id,page_index ON wechat_attempt_pages
        BEGIN SELECT RAISE(ABORT,'immutable WeChat page'); END;
        CREATE TRIGGER IF NOT EXISTS wechat_pages_no_delete BEFORE DELETE ON wechat_attempt_pages
        BEGIN SELECT RAISE(ABORT,'immutable WeChat page'); END;
        CREATE TRIGGER IF NOT EXISTS wechat_pages_transition BEFORE UPDATE OF state,code ON wechat_attempt_pages
        WHEN NOT ((OLD.state='not_sent' AND NEW.state='sending') OR
                  (OLD.state='sending' AND NEW.state IN ('accepted','rejected','unknown')))
        BEGIN SELECT RAISE(ABORT,'terminal WeChat page'); END;
        ''')


def project_exists(conn, project_id):
    if not conn.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
        raise DeliveryError('WECHAT_PROJECT_NOT_FOUND')


def current_config(conn, project_id):
    project_exists(conn, project_id)
    row = conn.execute('SELECT * FROM wechat_configs WHERE project_id=? ORDER BY version_no DESC LIMIT 1',
                       (project_id,)).fetchone()
    return dict(row) if row else None


def get_config(project_id):
    with closing(get_connection()) as conn:
        return current_config(conn, project_id)


def save_config(project_id, value, expected_version):
    with transaction() as conn:
        return insert_config(conn,project_id,value,expected_version)


def insert_config(conn, project_id, value, expected_version):
    current = current_config(conn, project_id)
    if (current['version_no'] if current else 0) != expected_version:
        raise DeliveryError('WECHAT_CONFIG_STALE')
    result = dict(value, project_id=project_id, version_no=expected_version+1, created_at=now())
    result.setdefault('transport','openclaw')
    result['account_id']=canonical_account(result['account_id'])
    columns = ','.join(result)
    conn.execute(f'INSERT INTO wechat_configs ({columns}) VALUES ({",".join("?" for _ in result)})', tuple(result.values()))
    return result


def assert_current(conn, preview):
    config = current_config(conn, preview['project_id'])
    if config is None or config['version_no'] != preview['config_version_no']:
        raise DeliveryError('WECHAT_CONFIG_STALE')
    if authority_stamp(conn, preview['project_id'], preview['report_version_id']) != preview['authority_hash']:
        raise DeliveryError('WECHAT_REPORT_STALE')


def insert_preview(value, images):
    with transaction() as conn:
        assert_current(conn, value)
        conn.execute(f'INSERT INTO wechat_previews ({",".join(value)}) VALUES ({",".join("?" for _ in value)})', tuple(value.values()))
        conn.executemany('INSERT INTO wechat_preview_images VALUES (?,?,?,?,?,?)', images)


def get_preview(project_id, preview_id):
    with closing(get_connection()) as conn:
        row = conn.execute('SELECT * FROM wechat_previews WHERE project_id=? AND preview_id=?',
                           (project_id, preview_id)).fetchone()
        if row is None:
            raise DeliveryError('WECHAT_PREVIEW_NOT_FOUND')
        images = [dict(r) for r in conn.execute('SELECT * FROM wechat_preview_images WHERE preview_id=? ORDER BY page_index', (preview_id,))]
        return dict(row), images


def attempt_response(conn, attempt_id):
    row = conn.execute('SELECT * FROM wechat_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
    return {**{k: row[k] for k in ('attempt_id','state','report_version_id','recipient_label','created_at')},
            'retry_requires_config_change': _safe_unsent_failure(conn,row),
            'pages': [{'index': r['page_index'], 'state': r['state'], 'code': r['code']}
                      for r in conn.execute('SELECT * FROM wechat_attempt_pages WHERE attempt_id=? ORDER BY page_index', (attempt_id,))]}


def _safe_unsent_failure(conn, attempt):
    if attempt['state'] != 'failed':
        return False
    pages = conn.execute('SELECT state,code FROM wechat_attempt_pages WHERE attempt_id=?',
                         (attempt['attempt_id'],)).fetchall()
    # A terminal attempt whose pages never started is also demonstrably unsent
    # (for example, configuration changed after claim and before page one).
    return bool(pages) and all(page['state']=='not_sent' or
                              (page['state']=='rejected' and page['code'] in _SAFE_CONFIG_REJECTIONS)
                              for page in pages)


def _equivalent_attempts(conn,preview):
    """Include shipped gateway records that used the bot's raw @im.bot spelling.

    Historical hashes and keys remain immutable. Canonical routing equivalence is
    checked from their frozen config and document, rather than rewriting history.
    """
    config=conn.execute('SELECT account_id,target FROM wechat_configs WHERE project_id=? AND version_no=?',
                        (preview['project_id'],preview['config_version_no'])).fetchone()
    if config is None:raise DeliveryError('WECHAT_CONFIG_STALE')
    rows=conn.execute('''SELECT a.*,c.account_id AS bound_account,c.target AS bound_target
        FROM wechat_attempts a JOIN wechat_previews p ON p.preview_id=a.preview_id
        JOIN wechat_configs c ON c.project_id=p.project_id AND c.version_no=p.config_version_no
        WHERE a.project_id=? AND p.document_hash=? ORDER BY a.rowid DESC''',
        (preview['project_id'],preview['document_hash'])).fetchall()
    return [row for row in rows if canonical_account(row['bound_account'])==canonical_account(config['account_id'])
            and row['bound_target']==config['target']]


def _existing_attempt(conn, preview, key):
    """Replays keep their original result, even when a newer retry now exists."""
    keyed = conn.execute('SELECT * FROM wechat_request_keys WHERE project_id=? AND idempotency_key=?',
                         (preview['project_id'],key)).fetchone()
    exact = conn.execute('''SELECT a.* FROM wechat_preview_attempt_bindings b
                            JOIN wechat_attempts a ON a.attempt_id=b.attempt_id WHERE b.preview_id=?''',
                         (preview['preview_id'],)).fetchone()
    equivalent=_equivalent_attempts(conn,preview)
    if keyed:
        # Both identities are immutable. Neither may override the other when
        # an explicitly confirmed configuration retry created a newer attempt.
        if ((keyed['document_target_hash'] != preview['document_target_hash'] and
             keyed['attempt_id'] not in {row['attempt_id'] for row in equivalent}) or
                (exact and keyed['attempt_id'] != exact['attempt_id'])):
            raise DeliveryError('WECHAT_IDEMPOTENCY_CONFLICT')
        return conn.execute('SELECT * FROM wechat_attempts WHERE attempt_id=?', (keyed['attempt_id'],)).fetchone()
    if exact:
        return exact
    latest = equivalent[0] if equivalent else None
    if latest and (preview['config_version_no'] <= latest['config_version_no'] or not _safe_unsent_failure(conn,latest)):
        return latest
    return None


def _bind_key(conn, preview, key, attempt_id):
    conn.execute('INSERT OR IGNORE INTO wechat_request_keys VALUES (?,?,?,?)',
                 (preview['project_id'],key,preview['document_target_hash'],attempt_id))
    # A deduplicated preview is consumed too. A new key cannot change its answer
    # after the operator updates configuration and qualifies for a narrow retry.
    conn.execute('INSERT OR IGNORE INTO wechat_preview_attempt_bindings VALUES (?,?)',
                 (preview['preview_id'],attempt_id))


def lookup_attempt(project_id, preview, key):
    with transaction() as conn:
        row = _existing_attempt(conn,preview,key)
        if row:
            _bind_key(conn,preview,key,row['attempt_id'])
            return attempt_response(conn,row['attempt_id'])
        return None


def claim_attempt(preview, key, page_count):
    with transaction() as conn:
        row = _existing_attempt(conn,preview,key)
        claimed = row is None
        if claimed:
            assert_current(conn, preview)
            attempt_id = uuid4().hex
            sequence = conn.execute('SELECT COALESCE(MAX(sequence_no),0)+1 FROM wechat_attempts WHERE document_target_hash=?',
                                    (preview['document_target_hash'],)).fetchone()[0]
            conn.execute('INSERT INTO wechat_attempts VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (attempt_id,preview['project_id'],preview['preview_id'],preview['document_target_hash'],
                          preview['report_version_id'],preview['recipient_label'],now(),'sending',preview['config_version_no'],sequence))
            conn.executemany('INSERT INTO wechat_attempt_pages VALUES (?,?,?,?)',
                             [(attempt_id,i,'not_sent','WECHAT_NOT_SENT') for i in range(1,page_count+1)])
        else:
            attempt_id = row['attempt_id']
        _bind_key(conn,preview,key,attempt_id)
        return attempt_response(conn, attempt_id), claimed


def start_page(attempt_id, index, preview):
    with transaction() as conn:
        assert_current(conn, preview)
        changed = conn.execute("UPDATE wechat_attempt_pages SET state='sending',code='WECHAT_SENDING' WHERE attempt_id=? AND page_index=? AND state='not_sent'", (attempt_id,index)).rowcount
        if changed != 1:
            raise DeliveryError('WECHAT_ATTEMPT_ALREADY_CLAIMED')


def finish_page(attempt_id, index, state, code):
    with transaction() as conn:
        conn.execute("UPDATE wechat_attempt_pages SET state=?,code=? WHERE attempt_id=? AND page_index=? AND state='sending'", (state,code,attempt_id,index))


def finish_attempt(attempt_id):
    with transaction() as conn:
        states = [r[0] for r in conn.execute('SELECT state FROM wechat_attempt_pages WHERE attempt_id=?', (attempt_id,))]
        state = ('unknown' if any(s in ('unknown','sending') for s in states) else
                 'accepted' if states and all(s == 'accepted' for s in states) else
                 'partial' if 'accepted' in states else 'failed')
        conn.execute("UPDATE wechat_attempts SET state=? WHERE attempt_id=? AND state='sending'", (state,attempt_id))
        return attempt_response(conn, attempt_id)


def recover_interrupted_sends():
    """Call only at process startup, never on ordinary requests or concurrent sends."""
    with transaction() as conn:
        conn.execute("UPDATE wechat_attempt_pages SET state='unknown',code='WECHAT_INTERRUPTED_UNKNOWN' WHERE state='sending'")
        conn.execute("UPDATE wechat_attempts SET state='unknown' WHERE state='sending'")


def history(project_id):
    with closing(get_connection()) as conn:
        project_exists(conn, project_id)
        ids = conn.execute('SELECT attempt_id FROM wechat_attempts WHERE project_id=? ORDER BY created_at DESC LIMIT 20', (project_id,)).fetchall()
        return [attempt_response(conn, row[0]) for row in ids]
