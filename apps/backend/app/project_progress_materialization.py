"""Close cumulative progress against the exact approved evidence transaction."""
from __future__ import annotations

import json
from app import project_progress as ledger
from app.project_profile_v2 import profile_planned_modules
from app.project_progress_context import progress_scope, read_progress_context

_STAGES = ('暂时无法确认', '开发中', '等待联调', '等待测试', '测试中', '已完成')
_BASE = {'implemented': '已完成', 'partial': '开发中'}


class ProgressMaterializationError(ValueError):
    def __init__(self, code='PROJECT_PROGRESS_MATERIALIZATION_INVALID'):
        self.code = code
        messages = {
            'PROJECT_PROGRESS_STALE': '已有另一份报告更新了累计进度，请重新核对本次范围后再确认。',
            'PROJECT_PROGRESS_LINEAGE_INVALID': '本次代码范围没有接续已确认进度，请重新检查当前分支与分析起点。',
            'PROJECT_PROGRESS_CORRECTION_EVIDENCE_INVALID': '功能状态更正必须引用本次分析中可核对的代码依据。',
            'PROJECT_PROGRESS_APPROVAL_MISMATCH': '本次确认与报告冻结的项目或代码范围不一致，请刷新后核对。',
            'PROJECT_PROGRESS_EVIDENCE_INVALID': '本次分析的已保存依据不完整，无法更新累计进度。',
            'PROJECT_PROGRESS_GIT_INVALID': '已保存的代码范围无法核对，请重新读取当前代码范围。',
        }
        super().__init__(messages.get(code, '本次模块结果无法与已确认功能计划核对。') + f'（{code}）')


def _require(condition, code='PROJECT_PROGRESS_MATERIALIZATION_INVALID'):
    if not condition:
        raise ProgressMaterializationError(code)


def _valid_refs(refs, allowed, git_refs):
    return (type(refs) is list and 0 < len(refs) <= 100 and
            all(type(x) is str and x in allowed for x in refs) and len(set(refs)) == len(refs) and bool(set(refs) & git_refs))


def compute_updates(*, profile, ai_raw, previous, allowed_git_refs, allowed_evidence_refs=None, corrections=None):
    """Project uniquely named observations; explicit Human corrections may regress.

    Only selected Git citations from this frozen range qualify here. Activity in a
    narrower test scope is not whole-module development completion. Lower activity
    stages do not imply regression of previously established development progress.
    """
    allowed = allowed_git_refs if allowed_evidence_refs is None else allowed_evidence_refs
    modules = profile_planned_modules(profile['content'])
    names = [m['name'] for m in modules]
    _require(len(names) == len(set(names)))
    by_name = {m['name']: m['client_id'] for m in modules}
    prior = {m['module_id']:m['stage'] for m in previous['modules']} if previous else {
        m['planned_module_id']:_BASE.get(m['status'], '暂时无法确认')
        for m in profile['content'].get('implementation_mappings', [])}
    content = ai_raw.get('content', {})
    if ai_raw.get('task_type') == 'daily_report_regenerate':
        content = content.get('new_report', {})
    _require(type(content) is dict and type(content.get('feature_progress')) is list)
    matches = {}
    for item in content['feature_progress']:
        if type(item) is dict and type(item.get('feature')) is str:
            matches.setdefault(item['feature'], []).append(item)
    result = {}
    for name, module_id in by_name.items():
        rows = matches.get(name, [])
        if len(rows) != 1:
            continue
        item = rows[0]
        stage = item.get('stage')
        if stage not in _STAGES or stage == '暂时无法确认' or item.get('implementation_scope') == '测试':
            continue
        if not _valid_refs(item.get('evidence_ids'), allowed, allowed_git_refs):
            continue
        if _STAGES.index(stage) < _STAGES.index(prior.get(module_id, '暂时无法确认')):
            continue
        result[module_id] = {'stage':stage, 'evidence_ids':list(item['evidence_ids'])}
    if corrections is not None:
        _require(type(corrections) is dict)
        for module_id, value in corrections.items():
            _require(module_id in by_name.values() and type(value) is dict and set(value) == {'stage','regression_reason','evidence_ids'})
            reason = value['regression_reason']
            _require(value['stage'] in _STAGES)
            _require(type(reason) is str and 0 < len(reason.strip()) <= 2000 and reason == reason.strip())
            _require(_valid_refs(value['evidence_ids'], allowed, allowed_git_refs), 'PROJECT_PROGRESS_CORRECTION_EVIDENCE_INVALID')
            result[module_id] = {'stage':value['stage'], 'evidence_ids':list(value['evidence_ids']), 'regression_reason':reason}
    return result


def read_progress_inputs(*, conn, project_id, profile, evidence_snapshot, replay_report_id=None):
    """Read-only authority closure usable before approval and during its transaction."""
    from app.evidence_snapshots import _validate_existing_snapshot_integrity, _git_facts_hash
    snapshot = dict(evidence_snapshot)
    _require(snapshot['project_id'] == project_id and snapshot['profile_id'] == profile['id'] and
             snapshot['profile_content_hash'] == profile['content_hash'])
    try:
        _validate_existing_snapshot_integrity(conn, snapshot)
    except Exception as exc:
        raise ProgressMaterializationError('PROJECT_PROGRESS_EVIDENCE_INVALID') from exc
    git_row = conn.execute('SELECT * FROM git_snapshots WHERE id=?', (snapshot['git_snapshot_id'],)).fetchone()
    _require(git_row is not None, 'PROJECT_PROGRESS_GIT_INVALID')
    git = dict(git_row)
    _require(all(git[key] == snapshot[key] for key in ('project_id','analysis_lineage_id','branch','from_commit','to_commit')), 'PROJECT_PROGRESS_GIT_INVALID')
    try:
        _require(_git_facts_hash(git) == snapshot['git_facts_hash'], 'PROJECT_PROGRESS_GIT_INVALID')
        commits = json.loads(git['commits_json'])
    except (ValueError, TypeError) as exc:
        raise ProgressMaterializationError('PROJECT_PROGRESS_GIT_INVALID') from exc
    scope = progress_scope(snapshot)
    previous = ledger.read_latest(conn, scope=scope)
    frozen = read_progress_context(conn, snapshot=snapshot)
    expected = frozen['previous'] if frozen is not None else None
    if previous and previous['report']['report_version_id'] == replay_report_id:
        _require(previous['previous_snapshot_hash'] == (expected or {}).get('snapshot_hash'), 'PROJECT_PROGRESS_STALE')
        previous = expected
    _require((expected or {}).get('snapshot_hash') == (previous or {}).get('snapshot_hash'), 'PROJECT_PROGRESS_STALE')
    heads = {m.get('exact_head') for m in profile['content'].get('implementation_mappings', []) if m.get('exact_head')}
    _require(len(heads) <= 1)
    parent = previous['report']['head_sha'] if previous else next(iter(heads), snapshot['from_commit'])
    _require(parent in {snapshot['from_commit'], *commits}, 'PROJECT_PROGRESS_LINEAGE_INVALID')
    # Empty, unchanged ranges are valid; they cannot move the cumulative HEAD.
    if not commits:
        _require(snapshot['to_commit'] == snapshot['from_commit'] == parent, 'PROJECT_PROGRESS_LINEAGE_INVALID')
    else:
        # GitClient rev-list --reverse ends at to_commit.
        _require(commits[-1] == snapshot['to_commit'] and snapshot['to_commit'] != snapshot['from_commit'], 'PROJECT_PROGRESS_LINEAGE_INVALID')
    refs = conn.execute("SELECT evidence_id FROM evidence_items WHERE snapshot_id=? AND type='git_file_fact' AND selected=1 AND redaction_state='not_applicable'", (snapshot['id'],)).fetchall()
    all_refs = conn.execute("SELECT evidence_id FROM evidence_items WHERE snapshot_id=? AND selected=1", (snapshot['id'],)).fetchall()
    return {'allowed_evidence_refs':{r['evidence_id'] for r in all_refs}, 'scope':scope, 'previous':previous, 'allowed_git_refs':{r['evidence_id'] for r in refs},
            'head_sha':snapshot['to_commit'], 'parent_head_sha':parent}


def materialize_progress(*, conn, project_id, profile, ai_raw, approval_snapshot, corrections=None):
    _require(conn.in_transaction)
    approval = approval_snapshot
    row = conn.execute('SELECT * FROM evidence_snapshots WHERE id=?', (approval['evidence_snapshot_id'],)).fetchone()
    _require(row is not None, 'PROJECT_PROGRESS_EVIDENCE_INVALID')
    snapshot = dict(row)
    pairs = {'project_id':'project_id', 'snapshot_hash':'evidence_snapshot_hash', 'git_snapshot_id':'git_snapshot_id',
             'git_facts_hash':'git_facts_hash', 'branch':'git_branch', 'from_commit':'git_from_commit',
             'to_commit':'git_to_commit', 'project_repository_url':'project_repository_url',
             'profile_id':'profile_id', 'profile_content_hash':'profile_content_hash'}
    _require(all(snapshot[k] == approval.get(v) for k,v in pairs.items()), 'PROJECT_PROGRESS_APPROVAL_MISMATCH')
    values = read_progress_inputs(conn=conn, project_id=project_id, profile=profile, evidence_snapshot=snapshot, replay_report_id=approval['report_version_id'])
    updates = compute_updates(profile=profile, ai_raw=ai_raw, previous=values['previous'], allowed_git_refs=values['allowed_git_refs'], allowed_evidence_refs=values['allowed_evidence_refs'], corrections=corrections)
    report = {key:approval[key] for key in ('report_version_id','report_content_hash','approval_snapshot_id','approval_snapshot_hash')}
    report.update(head_sha=values['head_sha'], parent_head_sha=values['parent_head_sha'])
    ledger.ensure_schema(conn)
    return ledger.append_snapshot(conn, scope=values['scope'], profile=profile, report=report, updates=updates)
