"""Append-only direct bindings. Only opaque credential references enter SQLite."""
from contextlib import closing
from app.db import get_connection
from app import wechat_delivery_store as delivery
from app.approved_report_delivery import DeliveryError


def ensure_wechat_binding_schema():
    with closing(get_connection()) as conn,conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS wechat_bindings (
          project_id INTEGER NOT NULL, config_version_no INTEGER NOT NULL,
          binding_state TEXT NOT NULL CHECK(binding_state IN ('awaiting_message','ready','disconnected')),
          context_ref TEXT NOT NULL, cursor_ref TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(project_id,config_version_no));
        CREATE TRIGGER IF NOT EXISTS wechat_bindings_no_update BEFORE UPDATE ON wechat_bindings
        BEGIN SELECT RAISE(ABORT,'immutable WeChat binding'); END;
        CREATE TRIGGER IF NOT EXISTS wechat_bindings_no_delete BEFORE DELETE ON wechat_bindings
        BEGIN SELECT RAISE(ABORT,'immutable WeChat binding'); END;
        ''')


def get_binding(project_id,config_version_no):
    with closing(get_connection()) as conn:
        row=conn.execute('SELECT * FROM wechat_bindings WHERE project_id=? AND config_version_no=?',
                         (project_id,config_version_no)).fetchone()
        return dict(row) if row else None


def save_binding(project_id,config,expected_version,*,state,context_ref='',cursor_ref=''):
    with delivery.transaction() as conn:
        saved=delivery.insert_config(conn,project_id,dict(config,transport='direct'),expected_version)
        conn.execute('INSERT INTO wechat_bindings VALUES (?,?,?,?,?,?)',
                     (project_id,saved['version_no'],state,context_ref,cursor_ref,delivery.now()))
        return saved


def assert_version(project_id,expected_version):
    with delivery.transaction() as conn:
        current=delivery.current_config(conn,project_id)
        if (current['version_no'] if current else 0)!=expected_version:
            raise DeliveryError('WECHAT_CONFIG_STALE')


def direct_secret_refs(project_id):
    with closing(get_connection()) as conn:
        refs=set()
        for row in conn.execute('''SELECT c.secret_ref,b.context_ref,b.cursor_ref FROM wechat_configs c
            JOIN wechat_bindings b ON b.project_id=c.project_id AND b.config_version_no=c.version_no
            WHERE c.project_id=? AND c.transport='direct' ''',(project_id,)):
            refs.update(value for value in row if value)
        return refs
