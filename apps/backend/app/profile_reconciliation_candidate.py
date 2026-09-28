"""Deterministic candidate aggregation. No provider and no automatic confirmation."""
from copy import deepcopy
import json
from app.profile_reconciliation_batches import aggregate_batches, BatchError, _hash
from app.project_profile_v2 import ProjectProfileV2Content


def aggregate_candidate(conn, batches, plan, *, current_head):
    if not batches or any(b['plan_hash'] != _hash(plan) for b in batches):
        raise BatchError('PLAN_CHANGED')
    mappings = aggregate_batches(conn, batches, current_head=current_head)
    evidence = {e['evidence_id']: e for b in batches for e in b['repo_evidence']}
    if evidence and not any(mapping["status"] == "partial" and mapping["evidence_ids"] for mapping in mappings):
        raise BatchError("ANALYSIS_EMPTY_NO_CODE_EVIDENCE")
    result = deepcopy(plan)
    result['implementation_mappings'] = []
    # Preserve fixed PRD plan byte-for-byte; only replace assessments for this task.
    result['unplanned_code_features'] = []
    for mapping in mappings:
        paths = sorted({evidence[e]['path'] for e in mapping['evidence_ids']})
        # V2 schema has explicit evidence/path capacity; no silent truncation.
        if len(mapping['evidence_ids']) > 100 or len(paths) > 100:
            raise BatchError('CANDIDATE_EVIDENCE_CAPACITY_EXCEEDED')
        model_rationale = str(mapping.get("rationale") or "").strip()
        if not model_rationale:
            raise BatchError("CANDIDATE_RATIONALE_MISSING")
        if mapping['status'] == 'partial':
            rationale = ('模型证据判断：' + model_rationale + '；首次基线仅保守标记为部分实现，代码存在本身不等于功能已经完成。')[:2000]
        else:
            rationale = ('模型未找到可绑定代码证据：' + model_rationale)[:2000]
        result['implementation_mappings'].append({
            **mapping,
            'paths': [{'type': 'other', 'pattern': path, 'required': True, 'note': ''} for path in paths],
            'rationale': rationale,
        })
    result = ProjectProfileV2Content.model_validate(result).model_dump()
    if result['planned_modules'] != plan['planned_modules']:
        raise BatchError('PLAN_CHANGED')
    if current_head() != batches[0]['exact_head']:
        raise BatchError('HEAD_CHANGED')
    # Isolated task-store candidate, never confirmed and never promoted implicitly
    # into the Product project_profiles table or report authority.
    conn.execute('''CREATE TABLE IF NOT EXISTS profile_reconciliation_candidates(
        batch_set_hash TEXT PRIMARY KEY, exact_head TEXT NOT NULL, plan_profile_id INTEGER NOT NULL,
        plan_hash TEXT NOT NULL, content_hash TEXT NOT NULL, content_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status='candidate'))''')
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    conn.execute('INSERT OR IGNORE INTO profile_reconciliation_candidates VALUES(?,?,?,?,?,?,?)',
      (batches[0]['batch_set_hash'], batches[0]['exact_head'], batches[0]['plan_profile_id'], batches[0]['plan_hash'], _hash(result), raw, 'candidate'))
    row = conn.execute('SELECT content_hash FROM profile_reconciliation_candidates WHERE batch_set_hash=?', (batches[0]['batch_set_hash'],)).fetchone()
    if row[0] != _hash(result):
        conn.rollback(); raise BatchError('CANDIDATE_IDENTITY_CONFLICT')
    conn.commit()
    return {'status': 'candidate', 'content': result, 'content_hash': _hash(result), 'batch_set_hash': batches[0]['batch_set_hash']}
