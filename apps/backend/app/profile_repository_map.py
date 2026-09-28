"""Local-only exact-object repository map, independent of model context capacity.

Every tracked entry is accounted for. Sensitive paths are never opened; unsupported,
binary and redaction-blocked objects retain explicit coverage states, never code claims.
"""
from __future__ import annotations
import hashlib
import re
from pathlib import PurePosixPath
from app import project_profile_bootstrap_context as legacy
from app.context_redaction import _redact_text
from app.git_client import GitClient, _assert_workspace_access, _locked_safe_local_config
from app.git_workspace_locks import project_workspace_lock

MAP_VERSION = 'profile-repo-map/1'
# Resource bound, not a model-window bound. Exceeding it stops instead of omitting.
MAX_LOCAL_BLOB_BYTES = 64 * 1024 * 1024
# Blob-body budget; the bounded per-object framing is additional.
MAX_BATCH_BLOB_BYTES = 8 * 1024 * 1024
MAX_BATCH_OBJECTS = 128


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def chunk_text(text, *, head, path, blob_hash, max_bytes=8192):
    # Chunking is only local storage/index granularity. The planner must budget the
    # complete framed request and split further if this granularity is still too big.
    chunks, current, start, current_bytes = [], [], 1, 0
    char_offset = 0
    def emit(end):
        nonlocal char_offset
        content = ''.join(current)
        h = digest(content)
        chunks.append({'evidence_id': 'repo-code-' + digest(f'{head}\n{path}\n{blob_hash}\n{start}\n{end}\n{h}')[:24],
                       'path': path, 'content': content, 'content_hash': h,
                       'object_sha': blob_hash, 'exact_head': head, 'line_start': start, 'line_end': end, 'char_start': char_offset, 'char_end': char_offset+len(content)})
        char_offset += len(content)
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        length = len(line.encode('utf-8'))
        if current and current_bytes + length > max_bytes:
            emit(line_no - 1); current=[]; start=line_no; current_bytes=0
        current.append(line); current_bytes += length
    if current: emit(line_no)
    return chunks


def _eligible_blob(record):
    return (legacy._sensitive_path_first_match(str(record['path'])) is None
            and record['object_type'] == 'blob' and record['mode'] != '120000')


def _blob_groups(records):
    group, byte_count, object_count = [], 0, 0
    for record in records:
        if _eligible_blob(record):
            size = record['size']
            if type(size) is not int or not 0 <= size <= MAX_LOCAL_BLOB_BYTES:
                raise legacy._git_context_error('PROFILE_RECONCILIATION_LOCAL_RESOURCE_LIMIT')
            if not isinstance(record['object_sha'], str) or not re.fullmatch('[0-9a-f]{40}', record['object_sha']):
                raise legacy._git_context_error('PROFILE_GENERATION_REPO_BLOB_INVALID')
            if object_count and (object_count >= MAX_BATCH_OBJECTS or byte_count + size > MAX_BATCH_BLOB_BYTES):
                yield group
                group, byte_count, object_count = [], 0, 0
            byte_count += size
            object_count += 1
        group.append(record)
    if group:
        yield group


def _parse_blob_batch(output, records):
    """Exact ordered framing: do not ignore missing, additional, or malformed bytes."""
    offset, blobs = 0, []
    for record in records:
        size, oid = record['size'], record['object_sha']
        header = f'{oid} blob {size}\n'.encode('ascii')
        if output[offset:offset + len(header)] != header:
            raise legacy._git_context_error('PROFILE_GENERATION_REPO_BLOB_INVALID')
        offset += len(header)
        raw = output[offset:offset + size]
        offset += size
        if (len(raw) != size or output[offset:offset + 1] != b'\n'
                or hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + raw).hexdigest() != oid):
            raise legacy._git_context_error('PROFILE_GENERATION_REPO_BLOB_INVALID')
        offset += 1
        blobs.append(raw)
    if offset != len(output):
        raise legacy._git_context_error('PROFILE_GENERATION_REPO_BLOB_INVALID')
    return blobs


def _records_with_blobs(client, access, records):
    for group in _blob_groups(records):
        eligible = [record for record in group if _eligible_blob(record)]
        blobs = []
        if eligible:
            request = ''.join(record['object_sha'] + '\n' for record in eligible).encode('ascii')
            _assert_workspace_access(access)
            with _locked_safe_local_config(access.path, access.expected_url):
                rc, output, _ = client._exec(
                    [*legacy._exact_object_prefix(access), 'cat-file', '--batch'],
                    cwd=access.path, input_data=request)
            _assert_workspace_access(access)
            if rc:
                raise legacy._git_context_error('PROFILE_GENERATION_REPO_BLOB_INVALID')
            blobs = _parse_blob_batch(output, eligible)
            del output
        iterator = iter(blobs)
        for record in group:
            yield record, next(iterator) if _eligible_blob(record) else None
        del blobs, iterator


def build_repository_map(project_id, project, git_state):
    head = git_state.get('remote_head')
    if not isinstance(head,str) or not re.fullmatch('[0-9a-f]{40}',head):
        raise legacy._git_context_error()
    legacy._validate_sensitive_path_policy()
    with project_workspace_lock(project_id, acquire_timeout=3.0):
        paths = legacy.resolve_workspace_paths(project_id, '0'*32, create=False)
        legacy.validate_workspace_paths(paths)
        access = legacy.open_workspace_access(paths, str(project['git_url']))
        client = GitClient()
        files, evidence, counts = [], [], {}
        def assert_head():
            client.assert_clean(access)
            if client.get_local_head(access) != head:
                raise legacy._git_context_error('PROFILE_RECONCILIATION_HEAD_CHANGED')
        try:
            client.inspect_workspace(access); assert_head()
            records = legacy._list_exact_head_tree(client, access, head)
            for record, raw in _records_with_blobs(client, access, records):
                path=str(record['path']); state=''
                entry={**record,'exact_head':head}
                if legacy._sensitive_path_first_match(path) is not None:
                    # Never expose the sensitive path in a model-visible map.
                    state='sensitive_excluded';entry={'exact_head':head,'path_hash':digest(path)}
                elif record['object_type'] != 'blob' or record['mode'] == '120000':
                    state='unsupported_object'
                else:
                    entry['raw_sha256']=hashlib.sha256(raw).hexdigest()
                    if b'\0' in raw or PurePosixPath(path).suffix.casefold() in legacy._BINARY_SUFFIXES:
                        state='binary_hashed'
                    else:
                        try: text=raw.decode('utf-8-sig')
                        except UnicodeDecodeError: state='unsupported_encoding'
                        else:
                            try: safe,_=_redact_text(text,include_assignments=True)
                            except Exception as exc:
                                # Do not export raw exception text, partial redactions, or
                                # unredacted symbols. Quarantine this file, preserve coverage.
                                if getattr(exc,'detail',{}).get('code','').startswith('CONTEXT_REDACTION_'):
                                    state='redaction_blocked'
                                else: raise
                            else:
                                state='safe_text'; entry['safe_bytes']=len(safe.encode('utf-8'))
                                entry['safe_hash']=digest(safe)
                                evidence.extend(chunk_text(safe,head=head,path=path,blob_hash=record['object_sha']))
                entry['coverage_state']=state;files.append(entry);counts[state]=counts.get(state,0)+1
            assert_head()
        finally: access.close()
    return {'schema_version':MAP_VERSION,'exact_head':head,'files':files,'evidence':evidence,
            'directories':sorted({str(PurePosixPath(f['path']).parent) for f in files if 'path' in f}),
            'coverage':counts,'tracked_files':len(records),
            'safe_text_bytes':sum(f.get('safe_bytes',0) for f in files),
            'complete_inventory':len(files)==len(records),
            'complete_safe_analysis':not any(counts.get(x,0) for x in ('redaction_blocked','unsupported_encoding','unsupported_object'))}
