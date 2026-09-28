import sqlite3
from contextlib import contextmanager

from app import report_progress_preview as preview


def test_preview_shows_full_baseline_plus_today_without_writing(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE evidence_snapshots(id INTEGER, snapshot_hash TEXT, profile_id INTEGER)")
    conn.execute("INSERT INTO evidence_snapshots VALUES(1, 'hash', 2)")
    conn.execute("CREATE TABLE evidence_items(snapshot_id INTEGER,evidence_id TEXT,type TEXT,source_ref TEXT)")
    conn.execute("INSERT INTO evidence_items VALUES(1,'git1','git_file_fact','frozen:1')")
    conn.commit()
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(preview, "get_connection", connection)
    profile = {'content': {'schema_version': 'project_profile_v2', 'planned_modules': [
        {'client_id': 'a', 'name': '登录'}, {'client_id': 'b', 'name': '消息'}],
        'implementation_mappings': [{'planned_module_id': 'a', 'status': 'implemented'}]}}
    monkeypatch.setattr(preview, "read_bound_project_profile_for_report", lambda *_a, **_k: profile)
    monkeypatch.setattr(preview, "read_progress_inputs", lambda **_k: {
        'previous': None, 'allowed_git_refs': {'git1'}, 'allowed_evidence_refs': {'git1'}})
    bundle = {'project_id': 1, 'report_version': {'report_version_id': 4, 'lifecycle': 'pending_review',
        'evidence_snapshot_id': 1, 'evidence_snapshot_hash': 'hash'}, 'ai_raw': {
        'content': {'feature_progress': [{'feature':'消息','stage':'开发中','implementation_scope':'后端','evidence_ids':['git1']}]}}}
    before = '\n'.join(conn.iterdump())
    result = preview.build_preview(bundle)
    assert result['state'] == 'ready'
    assert [(m['previous_stage'],m['stage']) for m in result['modules']] == [('已完成','已完成'),('暂时无法确认','开发中')]
    assert result['evidence_refs'][0]['type'] == 'git_file_fact'
    assert '\n'.join(conn.iterdump()) == before


def test_history_never_reconstructed_as_new_progress():
    assert preview.build_preview({'report_version':{'lifecycle':'approved'}}) is None
