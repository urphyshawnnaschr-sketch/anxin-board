"""No-provider repository inventory tests with in-memory Git objects."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import profile_repository_map as repo_map

HEAD = "a" * 40


@pytest.fixture
def fake_git(monkeypatch):
    records, blobs, opened = [], {}, []
    state = {"heads": [HEAD, HEAD], "closed": False, "listed": 0, "batches": []}
    access = SimpleNamespace(path=Path("fake-controlled-repository"), expected_url="https://github.com/example/project.git")
    access.close = lambda: state.update(closed=True)

    def add(path, raw, mode="100644", object_type="blob"):
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        records.append(dict(path=path, size=len(raw), mode=mode, object_type=object_type, object_sha=sha))
        blobs[sha] = raw
        return sha

    class Client:
        def inspect_workspace(self, _access): pass
        def assert_clean(self, _access): pass
        def get_local_head(self, _access): return state["heads"].pop(0)
        def _exec(self, args, *, cwd, input_data=None):
            assert "--no-replace-objects" in args and "--no-lazy-fetch" in args
            assert args[-2:] == ["cat-file", "--batch"]
            assert type(input_data) is bytes and len(input_data) <= 65536
            ids = input_data.decode('ascii').splitlines()
            state['batches'].append(ids)
            opened.extend(ids)
            output = b''.join(oid.encode() + b' blob ' + str(len(blobs[oid])).encode() + b'\n' + blobs[oid] + b'\n' for oid in ids)
            if state.get('mutate'): output = state['mutate'](output)
            return 0, output, b""


    def tree(client, actual_access, head):
        assert actual_access is access and head == HEAD
        state["listed"] += 1
        return records

    monkeypatch.setattr(repo_map, "GitClient", Client)
    monkeypatch.setattr(repo_map, "project_workspace_lock", lambda _, **__: nullcontext())
    monkeypatch.setattr(repo_map, "_assert_workspace_access", lambda _: None)
    monkeypatch.setattr(repo_map, "_locked_safe_local_config", lambda *a: nullcontext())
    monkeypatch.setattr(repo_map.legacy, "resolve_workspace_paths", lambda *a, **kw: object())
    monkeypatch.setattr(repo_map.legacy, "validate_workspace_paths", lambda _: None)
    monkeypatch.setattr(repo_map.legacy, "open_workspace_access", lambda *a: access)
    monkeypatch.setattr(repo_map.legacy, "_list_exact_head_tree", tree)
    return SimpleNamespace(add=add, blobs=blobs, opened=opened, state=state)


def build():
    return repo_map.build_repository_map(2, {"git_url": "https://github.com/example/project.git"}, {"remote_head": HEAD})


def test_full_text_above_old_two_megabyte_limit_is_accounted(fake_git):
    text = "# ordinary source line\n" * 26000
    for ordinal in range(4):
        fake_git.add(f"src/module{ordinal}.py", text.encode())
    result = build()
    assert result["safe_text_bytes"] > 2 * 1024 * 1024
    assert result["tracked_files"] == 4
    assert result["coverage"] == {"safe_text": 4}
    assert result["complete_inventory"] and result["complete_safe_analysis"]
    for ordinal in range(4):
        chunks = [item for item in result["evidence"] if item["path"] == f"src/module{ordinal}.py"]
        assert "".join(item["content"] for item in chunks) == text
        assert all(item["exact_head"] == HEAD for item in chunks)
    assert fake_git.state["closed"]


def test_sensitive_path_never_opens_blob_or_exports_path(fake_git):
    sensitive_sha = fake_git.add(".env", b"synthetic-not-a-real-credential")
    fake_git.add("src/app.py", b"def launch():\n    pass\n")
    result = build()
    assert sensitive_sha not in fake_git.opened
    assert result["coverage"] == {"sensitive_excluded": 1, "safe_text": 1}
    assert ".env" not in json.dumps(result)
    assert "synthetic-not-a-real-credential" not in json.dumps(result)
    assert "path_hash" in result["files"][0]


def test_redaction_failure_is_explicit_without_content_or_error_leak(fake_git, monkeypatch):
    fake_git.add("src/blocked.py", b"SYNTHETIC_PRIVATE_INPUT")
    def blocked(*a, **kw):
        raise HTTPException(409, {"code": "CONTEXT_REDACTION_UNSAFE_AMBIGUITY", "message": "SYNTHETIC_PRIVATE_EXCEPTION"})
    monkeypatch.setattr(repo_map, "_redact_text", blocked)
    result = build()
    assert result["coverage"] == {"redaction_blocked": 1}
    assert result["complete_inventory"] and not result["complete_safe_analysis"]
    assert not result["evidence"]
    assert "index" not in result["files"][0]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)


def test_unsupported_encoding_stays_visible(fake_git):
    fake_git.add("src/legacy.py", b"\xff\xfeinvalid")
    result = build()
    assert result["coverage"] == {"unsupported_encoding": 1}
    assert result["complete_inventory"] and not result["complete_safe_analysis"]
    assert result["evidence"] == []


@pytest.mark.parametrize("heads", [["b" * 40], [HEAD, "b" * 40]])
def test_head_change_before_or_after_read_stops(fake_git, heads):
    fake_git.add("src/app.py", b"def run(): pass\n")
    fake_git.state["heads"] = heads
    with pytest.raises(HTTPException) as caught: build()
    assert caught.value.detail["code"] == "PROFILE_RECONCILIATION_HEAD_CHANGED"
    assert fake_git.state["closed"]
    if len(heads) == 0 and fake_git.state["listed"] == 0:
        assert not fake_git.opened


def test_corrupt_blob_is_rejected(fake_git):
    sha = fake_git.add("src/app.py", b"original")
    fake_git.blobs[sha] = b"modified"
    with pytest.raises(HTTPException) as caught: build()
    assert caught.value.detail["code"] == "PROFILE_GENERATION_REPO_BLOB_INVALID"
    assert fake_git.state["closed"]


def test_chunks_roundtrip_and_indexes_are_deterministic():
    text = "class Account:\r\n    pass\r\n@router.get('/accounts')\ndef list_accounts():\n    pass\nCREATE TABLE accounts (id INTEGER);\ndef test_accounts():\n    pass\nfetch('/api/accounts')\n# 中文无末尾换行"
    chunks = repo_map.chunk_text(text, head=HEAD, path="src/app.py", blob_hash="b" * 40, max_bytes=40)
    assert "".join(item["content"] for item in chunks) == text
    assert chunks == repo_map.chunk_text(text, head=HEAD, path="src/app.py", blob_hash="b" * 40, max_bytes=40)
    assert len({item["evidence_id"] for item in chunks}) == len(chunks)
    assert chunks[0]["line_start"] == 1
    assert all(left["line_end"] + 1 == right["line_start"] for left, right in zip(chunks, chunks[1:]))
    assert chunks[-1]['char_end']==len(text)
    assert all(text[c['char_start']:c['char_end']]==c['content'] for c in chunks)
    assert not hasattr(repo_map,'map_text')


def test_repository_scan_waits_briefly_for_inflight_git_operation(fake_git, monkeypatch):
    fake_git.add("src/app.py", b"def run(): pass\n")
    calls = []

    @contextmanager
    def lock(project_id, *, acquire_timeout=0.0):
        calls.append((project_id, acquire_timeout))
        yield

    monkeypatch.setattr(repo_map, "project_workspace_lock", lock)
    result = build()
    assert result["tracked_files"] == 1
    assert calls == [(2, 3.0)]


def test_batch_count_and_byte_boundaries_preserve_order(fake_git, monkeypatch):
    monkeypatch.setattr(repo_map, 'MAX_BATCH_BLOB_BYTES', 8)
    monkeypatch.setattr(repo_map, 'MAX_BATCH_OBJECTS', 2)
    for i, raw in enumerate([b'1234', b'5678', b'9', b'abcdefghij', b'x', b'y', b'z']):
        fake_git.add(f'src/{i}.txt', raw)
    result = build()
    assert [len(group) for group in fake_git.state['batches']] == [2, 1, 1, 2, 1]
    assert [f['path'] for f in result['files']] == [f'src/{i}.txt' for i in range(7)]


@pytest.mark.parametrize('mutation', [
    lambda data: data[:-1],
    lambda data: data + b'extra',
    lambda data: data.replace(b' blob ', b' tree ', 1),
    lambda data: b'f'*40 + data[40:],
    lambda data: data.replace(b' blob 3\n', b' blob 4\n', 1),
    lambda data: data.replace(b' blob 3\n', b' blob 03\n', 1),
    lambda data: data.replace(b'abc\n', b'abcX', 1),
    lambda data: b'',
])
def test_batch_protocol_malformed_is_rejected(fake_git, mutation):
    fake_git.add('src/a.txt', b'abc')
    fake_git.state['mutate'] = mutation
    with pytest.raises(HTTPException) as caught: build()
    assert caught.value.detail['code'] == 'PROFILE_GENERATION_REPO_BLOB_INVALID'
    assert fake_git.state['closed']


def test_batch_serialization_parity_across_grouping(fake_git, monkeypatch):
    for path, raw, mode in [('src/a.py', b'print(1)\r\n', '100644'), ('.env', b'private', '100644'), ('image.png', b'\0binary', '100644'), ('src/b.txt', b'\xffbad', '100644'), ('link', b'target', '120000'), ('src/c.txt', b'last', '100644')]:
        fake_git.add(path, raw, mode)
    result = build()
    fake_git.state['heads'] = [HEAD, HEAD]
    monkeypatch.setattr(repo_map, 'MAX_BATCH_OBJECTS', 1)
    assert json.dumps(build(), sort_keys=True) == json.dumps(result, sort_keys=True)
    assert result['coverage'] == {'safe_text': 2, 'sensitive_excluded': 1, 'binary_hashed': 1, 'unsupported_encoding': 1, 'unsupported_object': 1}


def test_batch_rejects_reordering_missing_or_additional_objects(fake_git):
    first = fake_git.add('src/a.txt', b'one')
    second = fake_git.add('src/b.txt', b'two')
    first_frame = first.encode() + b' blob 3\none\n'
    second_frame = second.encode() + b' blob 3\ntwo\n'
    for output in [second_frame + first_frame, first_frame, first_frame + second_frame + first_frame]:
        fake_git.state['heads'] = [HEAD, HEAD]
        fake_git.state['mutate'] = lambda _, output=output: output
        with pytest.raises(HTTPException) as caught: build()
        assert caught.value.detail['code'] == 'PROFILE_GENERATION_REPO_BLOB_INVALID'


def test_batch_keeps_workspace_and_config_guards_per_group(fake_git, monkeypatch):
    monkeypatch.setattr(repo_map, 'MAX_BATCH_OBJECTS', 1)
    for i in range(3): fake_git.add(f'src/{i}.txt', str(i).encode())
    events = []
    monkeypatch.setattr(repo_map, '_assert_workspace_access', lambda _: events.append('workspace'))
    @contextmanager
    def config(*_):
        events.append('config-enter')
        yield
        events.append('config-exit')
    monkeypatch.setattr(repo_map, '_locked_safe_local_config', config)
    build()
    assert events == ['workspace', 'config-enter', 'config-exit', 'workspace'] * 3


def test_batch_planning_resource_and_input_bounds():
    def record(size, **kwargs):
        return dict(path='src/a.txt', object_type='blob', mode='100644', object_sha='b'*40, size=size, **kwargs)
    cap = repo_map.MAX_BATCH_BLOB_BYTES
    assert [len(g) for g in repo_map._blob_groups([record(cap), record(1), record(cap + 1), record(0)])] == [1, 1, 1, 1]
    groups = list(repo_map._blob_groups([record(0)] * 129))
    assert list(map(len, groups)) == [128, 1]
    assert max(len(g) * 41 for g in groups) < 65536
    assert len(list(repo_map._blob_groups([record(repo_map.MAX_LOCAL_BLOB_BYTES)]))) == 1
    for size in [-1, True, repo_map.MAX_LOCAL_BLOB_BYTES + 1]:
        with pytest.raises(HTTPException) as caught: list(repo_map._blob_groups([record(size)]))
        assert caught.value.detail['code'] == 'PROFILE_RECONCILIATION_LOCAL_RESOURCE_LIMIT'


def test_no_eligible_blob_means_no_git_batch(fake_git):
    fake_git.add('.env', b'not-opened')
    fake_git.add('link', b'not-opened-link', mode='120000')
    result = build()
    assert not fake_git.state['batches']
    assert result['coverage'] == {'sensitive_excluded': 1, 'unsupported_object': 1}


def test_real_local_git_batch_matches_individual_objects(tmp_path):
    import shutil
    import subprocess
    from app.git_client import GitClient
    git = shutil.which('git')
    if not git: pytest.skip('local Git required')
    subprocess.run([git, 'init', str(tmp_path)], check=True, capture_output=True)
    client = GitClient(git_path=git, allow_local_file=True)
    records, originals = [], [b'plain\r\ntext', b'\0binary\xff\n', b'', '中文\n'.encode()]
    for ordinal, raw in enumerate(originals):
        blob = tmp_path / f'fixture-{ordinal}'
        blob.write_bytes(raw)
        oid = subprocess.run([git, '-C', str(tmp_path), 'hash-object', '--no-filters', '-w', str(blob)], check=True, capture_output=True).stdout.decode().strip()
        records.append(dict(object_sha=oid, size=len(raw)))
    prefix = ['--no-optional-locks', '-C', str(tmp_path), '--no-replace-objects', '--no-lazy-fetch']
    rc, output, _ = client._exec([*prefix, 'cat-file', '--batch'], cwd=tmp_path,
        input_data=''.join(r['object_sha'] + '\n' for r in records).encode())
    assert rc == 0
    batch = repo_map._parse_blob_batch(output, records)
    assert batch == originals
    for record, raw in zip(records, batch):
        rc, single, _ = client._exec([*prefix, 'cat-file', 'blob', record['object_sha']], cwd=tmp_path)
        assert rc == 0 and single == raw
