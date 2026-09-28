"""Pure source-confirmed technology orchestration over the Core safe surface.

No readers, provider or execution capability is passed to technology adapters.
Classification is evidence of usage, never a feature-completeness judgment.
"""
from collections import defaultdict
from pathlib import PurePosixPath
import hashlib
import json
import re

SCHEMA = 'repository-technology-evidence/1'
STATES = {'SOURCE_CONFIRMED', 'DECLARED_ONLY', 'UNKNOWN'}
# Declaration metadata only. No adapter is created or activated for these.
DECLARATION_ONLY = {'Jpa': ('spring-boot-starter-data-jpa', 'hibernate-core'),
    'Artemis': ('artemis-jms-client', 'spring-boot-starter-artemis'),
    'VueSocketIo': ('vue-socket.io',), 'WebRtc': ('webrtc-adapter',),
    'FlvJs': ('flv.js',), 'Xgplayer': ('xgplayer',)}


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def safe_sources(repository_map):
    """Reassemble only contiguous hash-validated Core chunks, never reopen files."""
    groups = defaultdict(list)
    head = repository_map['exact_head']
    files = {f['path']: f for f in repository_map['files'] if 'path' in f}
    for fragment in repository_map['evidence']:
        if fragment['exact_head'] != head:
            raise ValueError('TECHNOLOGY_HEAD_MISMATCH')
        if _hash(fragment['content']) != fragment['content_hash']:
            raise ValueError('TECHNOLOGY_HASH_MISMATCH')
        entry = files.get(fragment['path'], {})
        if entry.get('coverage_state') != 'safe_text' or entry.get('object_sha') != fragment['object_sha']:
            raise ValueError('TECHNOLOGY_SOURCE_NOT_CORE_SAFE')
        groups[fragment['path']].append(fragment)
    if any(f.get('coverage_state') == 'safe_text' and f.get('safe_bytes', 0) > 0 and path not in groups
           for path, f in files.items()):
        raise ValueError('TECHNOLOGY_CORE_SOURCE_MISMATCH')
    sources = {}
    for path, chunks in groups.items():
        chunks.sort(key=lambda c: c['char_start'])
        offset = 0
        for chunk in chunks:
            if (chunk['char_start'] != offset or chunk['char_end'] != offset + len(chunk['content'])
                    or chunk['object_sha'] != chunks[0]['object_sha']):
                raise ValueError('TECHNOLOGY_SOURCE_DISCONTINUITY')
            offset = chunk['char_end']
        text = ''.join(c['content'] for c in chunks)
        if _hash(text) != files[path].get('safe_hash') or len(text.encode('utf-8')) != files[path].get('safe_bytes'):
            raise ValueError('TECHNOLOGY_CORE_SOURCE_MISMATCH')
        sources[path] = {'text': text, 'chunks': chunks}
    return sources


def _declarations(path, text):
    name = PurePosixPath(path).name
    if name == 'package.json':
        try:
            value = json.loads(text)
            return set().union(*(set(value.get(k, {})) for k in
                ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies')))
        except (ValueError, TypeError, AttributeError):
            return set()
    if name == 'pom.xml':
        # Parse dependency coordinates only; never resolve entities or build plugins.
        dependencies = re.findall(r'<dependency\b[^>]*>(.*?)</dependency>', text, re.S)
        return {v for block in dependencies for v in re.findall(
            r'<(?:groupId|artifactId)>\s*([^<\s]+)\s*</(?:groupId|artifactId)>', block)}
    return set()


def _scope(path, manifest_dirs):
    parent = PurePosixPath(path).parent
    return next((str(p) for p in (parent, *parent.parents) if str(p) in manifest_dirs), '.')


def _record(match, source, path, head):
    text = source['text']
    start, end = match['char_start'], match['char_end']
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise ValueError('INVALID_TECHNOLOGY_LOCATION')
    if not match.get('symbol') or match['symbol'] not in text[start:end]:
        raise ValueError('TECHNOLOGY_SYMBOL_OUTSIDE_SOURCE_SLICE')
    for key in ('symbol', 'related_symbol', 'related_path'):
        value = match.get(key, '')
        if not isinstance(value, str) or (value and value not in text):
            raise ValueError('TECHNOLOGY_METADATA_OUTSIDE_SAFE_SOURCE')
        if re.search(r'://|\b(?:\d{1,3}\.){3}\d{1,3}\b|\[REDACTED', value):
            raise ValueError('TECHNOLOGY_UNSAFE_METADATA')
    chunks = [c for c in source['chunks'] if c['char_start'] < end and c['char_end'] > start]
    record = {k: match.get(k, '') for k in ('technology', 'adapter_id', 'adapter_version',
              'evidence_type', 'symbol', 'relation', 'related_path', 'related_symbol')}
    if any(not isinstance(record[k], str) or not record[k] for k in
           ('technology', 'adapter_id', 'adapter_version', 'evidence_type', 'relation')):
        raise ValueError('INVALID_TECHNOLOGY_CONTRACT')
    record.update(schema_version=SCHEMA, source_path=path, exact_head=head,
                  object_sha=chunks[0]['object_sha'], source_hash=_hash(text), source_slice_hash=_hash(text[start:end]),
                  char_start=start, char_end=end, line_start=text.count('\n', 0, start)+1,
                  line_end=text.count('\n', 0, end-1)+1,
                  source_evidence_ids=[c['evidence_id'] for c in chunks],
                  classification='SOURCE_CONFIRMED', redaction_state='core_safe_surface',
                  confidence='source_usage_not_feature_completeness')
    record['evidence_id'] = 'technology-' + _hash(json.dumps(record, sort_keys=True))[:24]
    return record


def enrich_technology(repository_map, *, adapters=None):
    if adapters is None:
        from app import repository_java_adapters, repository_vue_adapters
        adapters = (repository_java_adapters, repository_vue_adapters)
    sources = safe_sources(repository_map)
    # An unreadable manifest still establishes a project boundary. Its source
    # cannot supply declarations, and must not inherit a parent's framework.
    manifest_dirs = {str(PurePosixPath(f['path']).parent) for f in repository_map['files']
                     if 'path' in f and PurePosixPath(f['path']).name in {'package.json', 'pom.xml'}}
    declarations = defaultdict(set)
    for path, source in sources.items():
        declarations[_scope(path, manifest_dirs)].update(_declarations(path, source['text']))
    technologies = {**DECLARATION_ONLY, **{name: clues for adapter in adapters for name, clues in adapter.TECHNOLOGIES.items()}}
    confirmed = defaultdict(set)
    detections = {}
    failures = []
    for path, source in sources.items():
        scope = _scope(path, manifest_dirs)
        for adapter in adapters:
            try:
                detected = set(adapter.detect(path, source['text']))
                if not detected <= set(adapter.TECHNOLOGIES):
                    raise ValueError('UNKNOWN_DETECTOR_TECHNOLOGY')
            except Exception:
                detected = set()
                failures.append({'path': path, 'stage': 'detect', 'status': 'generic_fallback'})
            detections[path, id(adapter)] = detected
            confirmed[scope].update(detected)
    records = []
    scoped_sources = defaultdict(dict)
    for path, source in sources.items():
        scoped_sources[_scope(path, manifest_dirs)][path] = source['text']
    for path, source in sources.items():
        scope = _scope(path, manifest_dirs)
        for adapter in adapters:
            enabled = detections[path, id(adapter)].copy()
            # Adapter-declared cross-file eligibility is constrained to this
            # nearest manifest scope; no language branch enters Core or planner.
            try:
                scope_eligible = getattr(adapter, 'scope_eligible', lambda p, t: set())
                eligible = set(scope_eligible(path, source['text']))
                if not eligible <= set(adapter.TECHNOLOGIES):
                    raise ValueError('UNKNOWN_SCOPE_TECHNOLOGY')
                enabled.update(eligible & confirmed[scope])
                context_eligible = getattr(adapter, 'context_eligible', lambda p, t, s: set())
                contextual = set(context_eligible(path, source['text'], scoped_sources[scope]))
                if not contextual <= set(adapter.TECHNOLOGIES):
                    raise ValueError('UNKNOWN_CONTEXT_TECHNOLOGY')
                enabled.update(contextual & confirmed[scope])
            except Exception:
                failures.append({'path': path, 'stage': 'scope', 'status': 'generic_fallback'})
                continue
            if not enabled:
                continue
            try:
                contextual_analyzer = getattr(adapter, 'analyze_context', None)
                matches = (contextual_analyzer(path, source['text'], scoped_sources[scope], enabled)
                    if contextual_analyzer else adapter.analyze(path, source['text'], enabled=enabled))
                bound = []
                for match in matches:
                    if match['technology'] not in enabled:
                        raise ValueError('UNCONFIRMED_ADAPTER_ACTIVATION')
                    bound.append(_record(match, source, path, repository_map['exact_head']))
                records.extend(bound)
            except Exception:
                failures.append({'path': path, 'stage': 'parse', 'status': 'generic_fallback'})
    classifications = []
    scopes = sorted({_scope(p, manifest_dirs) for p in sources})
    for scope in scopes:
        for technology, clues in sorted(technologies.items()):
            declared = any(clue in declarations[scope] for clue in clues)
            status = ('SOURCE_CONFIRMED' if technology in confirmed[scope] else
                      'DECLARED_ONLY' if declared else 'UNKNOWN')
            proof = sorted({c['evidence_id'] for path, source in sources.items()
                if _scope(path, manifest_dirs) == scope and any(
                    technology in detections[path, id(a)] for a in adapters)
                for c in source['chunks']}) if status == 'SOURCE_CONFIRMED' else []
            classifications.append({'scope': scope, 'technology': technology,
                                    'classification': status, 'source_evidence_ids': proof})
    by_chunk = defaultdict(list)
    for record in records:
        for evidence_id in record['source_evidence_ids']:
            chunk = next(c for c in sources[record['source_path']]['chunks'] if c['evidence_id'] == evidence_id)
            if (len(record['source_evidence_ids']) == 1 and
                    all(not record.get(k) or record[k] in chunk['content']
                        for k in ('symbol', 'related_symbol', 'related_path'))):
                by_chunk[evidence_id].append(record)
    evidence = [{**c, 'source_hash': _hash(sources[c['path']]['text']),
                 'technology_evidence': by_chunk[c['evidence_id']]} for c in repository_map['evidence']]
    return {**repository_map, 'evidence': evidence, 'technology_detection': {
        'detector_version': 'source-confirmed-stack/1', 'classifications': classifications,
        'enabled_adapters': sorted({r['adapter_id'] for r in records}),
        'fallbacks': failures, 'evidence_count': len(records),
        'provider_calls': 0, 'network_model_calls': 0}}
