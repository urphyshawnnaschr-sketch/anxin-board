"""Language-neutral evidence and parser extension contract. Local data only."""
from dataclasses import dataclass
from typing import Protocol
import hashlib
import json

EVIDENCE_VERSION = 'repository-evidence/1'


def validate_technology_evidence(fragment):
    """Language-neutral source binding at the retrieval/planning trust boundary."""
    for record in fragment.get('technology_evidence', []):
        start, end = record['char_start'], record['char_end']
        if type(start) is not int or type(end) is not int:
            raise ValueError('TECHNOLOGY_EVIDENCE_BINDING_INVALID')
        source_slice = fragment['content'][start-fragment['char_start']:end-fragment['char_start']]
        expected = 'technology-' + hashlib.sha256(json.dumps(
            {k: v for k, v in record.items() if k != 'evidence_id'}, sort_keys=True
        ).encode()).hexdigest()[:24]
        if (record.get('evidence_id') != expected
                or record.get('source_path') != fragment['path']
                or record.get('exact_head') != fragment['exact_head']
                or record.get('object_sha') != fragment.get('object_sha')
                or record.get('source_hash') != fragment.get('source_hash')
                or record.get('source_slice_hash') != hashlib.sha256(source_slice.encode()).hexdigest()
                or not record.get('symbol') or record['symbol'] not in source_slice
                or record.get('source_evidence_ids') != [fragment['evidence_id']]
                or record.get('classification') != 'SOURCE_CONFIRMED'
                or not fragment['char_start'] <= record['char_start'] < record['char_end'] <= fragment['char_end']
                or any(record.get(k) and record[k] not in fragment['content']
                       for k in ('symbol', 'related_symbol', 'related_path'))):
            raise ValueError('TECHNOLOGY_EVIDENCE_BINDING_INVALID')

@dataclass(frozen=True)
class ParseResult:
    adapter_id: str
    status: str
    structured: dict[str, list[str]]

class ParserAdapter(Protocol):
    adapter_id: str
    language: str
    extensions: frozenset[str]
    manifests: frozenset[str]
    def parse(self, path: str, safe_text: str) -> ParseResult: ...


def annotate_evidence(fragment, result, *, language, terms, detector_version):
    """An adapter annotates safe evidence, never replaces source identity/content."""
    if result.status not in {'heuristic','fallback','parser_failed_fallback'}:
        raise ValueError('INVALID_PARSER_STATUS')
    if not isinstance(result.adapter_id,str) or not result.adapter_id:
        raise ValueError('INVALID_ADAPTER_ID')
    if hashlib.sha256(fragment['content'].encode('utf-8')).hexdigest()!=fragment['content_hash']:
        raise ValueError('EVIDENCE_HASH_MISMATCH')
    for kind,values in result.structured.items():
        if not isinstance(kind,str) or not isinstance(values,list) or any(not isinstance(v,str) for v in values):
            raise ValueError('INVALID_STRUCTURED_METADATA')
        if any(v not in fragment['content'] for v in values):
            raise ValueError('METADATA_OUTSIDE_SAFE_FRAGMENT')
    return {**fragment,'evidence_schema_version':EVIDENCE_VERSION,
            'adapter_id':result.adapter_id,'parser_status':result.status,
            'language_hint':language,'detector_version':detector_version,
            'structured_metadata':result.structured,'terms':sorted(set(terms)),
            'confidence':'lexical_only' if result.status!='heuristic' else 'heuristic_not_semantic_proof',
            'semantic_coverage':'unknown'}
