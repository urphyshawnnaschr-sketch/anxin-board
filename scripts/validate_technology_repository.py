"""Read-only external-fixture harness. No product DB, provider or remote Git.

The harness supplies local Git object inventory to the unchanged Core scan loop;
only the Product-managed workspace admission is replaced for an isolated clone.
Raw file bodies never appear in the summary. Do not use as a Product reader.
"""
import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps' / 'backend'))
from app import profile_repository_map as core
from app.repository_index import index_repository_map
from app.profile_reconciliation_batches import plan_reconciliation_batches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(args.repository).resolve()
    calls = {'provider_calls': 0, 'network_model_calls': 0}

    def forbidden(*a, **kw):
        raise AssertionError('NETWORK_FORBIDDEN')

    def git(*arguments):
        result = subprocess.run(['git', '-c', f'safe.directory={root.as_posix()}',
            '--no-replace-objects', '--no-lazy-fetch', *arguments], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return result.stdout

    def head():
        actual = git('rev-parse', 'HEAD').decode().strip()
        if actual != args.head:
            raise ValueError('EXTERNAL_HEAD_CHANGED')
        return actual

    head()
    entries = []
    for line in git('ls-tree', '-r', '-l', '-z', args.head).split(b'\0'):
        if not line:
            continue
        metadata, path = line.split(b'\t', 1)
        mode, kind, sha, size = metadata.split()
        entries.append(dict(path=path.decode('utf-8'), mode=mode.decode(),
            object_type=kind.decode(), object_sha=sha.decode(),
            size=int(size) if size != b'-' else None))
    access = SimpleNamespace(path=root, expected_url='isolated-fixture', close=lambda: None)

    class LocalObjects:
        def inspect_workspace(self, a): head()
        def assert_clean(self, a):
            if git('status', '--porcelain', '--untracked-files=no').strip():
                raise ValueError('EXTERNAL_TRACKED_TREE_DIRTY')
        def get_local_head(self, a): return head()
        def _exec(self, command, **kw):
            if command[-3:-1] != ['cat-file', 'blob']:
                raise ValueError('ONLY_LOCAL_BLOB_READ_ALLOWED')
            return 0, git('cat-file', 'blob', command[-1]), b''

    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket, 'create_connection', forbidden), \
         patch.object(core, 'GitClient', LocalObjects), \
         patch.object(core, 'project_workspace_lock', lambda *a: nullcontext()), \
         patch.object(core, '_assert_workspace_access', lambda *a: None), \
         patch.object(core, '_locked_safe_local_config', lambda *a: nullcontext()), \
         patch.object(core.legacy, 'resolve_workspace_paths', lambda *a, **kw: None), \
         patch.object(core.legacy, 'validate_workspace_paths', lambda *a: None), \
         patch.object(core.legacy, 'open_workspace_access', lambda *a: access), \
         patch.object(core.legacy, '_list_exact_head_tree', lambda *a: entries):
        mapped = core.build_repository_map(1, {'git_url': 'isolated-fixture'}, {'remote_head': head()})
        generic = index_repository_map(mapped, adapters=(), technology_adapters=())
        indexed = index_repository_map(mapped)
        plan = {'schema_version': 'project_profile_v2', 'planned_modules': [
            {'client_id': 'account', 'name': 'account login', 'requirements': ['login account service']},
            {'client_id': 'routing', 'name': 'router component', 'requirements': ['navigation request']} ]}
        budget = dict(context_window_tokens=64000, max_output_tokens=8000, reserved_output_tokens=8000,
                      safety_margin_tokens=4000, counting_policy_version='utf8_byte_upper_bound_v1')
        batches = plan_reconciliation_batches(plan, indexed['evidence'], exact_head=args.head,
                    plan_profile_id=1, budget_record=budget, repository_coverage=indexed['coverage'])
        head()
    records = {r['evidence_id']: r for e in indexed['evidence'] for r in e['technology_evidence']}
    examples = {}
    sampled = set()
    for record in records.values():
        samples = examples.setdefault(record['adapter_id'], [])
        key = (record['adapter_id'], record['evidence_type'], record['relation'])
        if key not in sampled and len(samples) < 16:
            samples.append(record)
            sampled.add(key)
    summary = dict(exact_head=args.head, tracked_files=mapped['tracked_files'],
        safe_text_bytes=mapped['safe_text_bytes'], coverage=mapped['coverage'],
        generic_structured_relations=sum(len(e['technology_evidence']) for e in generic['evidence']),
        technology_detection=indexed['technology_detection'],
        adapter_relations=len(records), examples=examples, planned_module_count=len(plan['planned_modules']),
        batch_count=len(batches), batch_input_tokens=[len(json.dumps(b, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()) for b in batches], **calls)
    Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ('exact_head', 'tracked_files', 'safe_text_bytes',
        'coverage', 'adapter_relations', 'batch_count', 'provider_calls', 'network_model_calls')}))


if __name__ == '__main__':
    main()
