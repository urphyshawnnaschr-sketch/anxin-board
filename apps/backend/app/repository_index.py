"""Compose generic inventory with optional local adapters. No provider dependency."""
from pathlib import PurePosixPath
from dataclasses import replace
from app.profile_repository_map import build_repository_map
from app.repository_evidence import annotate_evidence
from app.repository_parsers import DEFAULT_ADAPTERS,GenericFallback,text_terms
from app.repository_stack_detection import detect_stack,DETECTOR_VERSION


def index_repository_map(repository_map, *, adapters=DEFAULT_ADAPTERS, technology_adapters=None):
    suffix_registry={}
    manifest_registry={}
    for adapter in adapters:
        for extension in adapter.extensions:
            if extension in suffix_registry:raise ValueError('AMBIGUOUS_ADAPTER_REGISTRY')
            suffix_registry[extension]=adapter
        for manifest in adapter.manifests:
            name=manifest.casefold()
            if name in manifest_registry:raise ValueError('AMBIGUOUS_ADAPTER_REGISTRY')
            manifest_registry[name]=adapter
    paths=[f['path'] for f in repository_map['files'] if 'path' in f]
    detection=detect_stack(paths,adapters)
    fallback=GenericFallback();evidence=[];parser_counts={};indexes={}
    for fragment in repository_map['evidence']:
        path=fragment['path'];safe=fragment['content']
        adapter=manifest_registry.get(PurePosixPath(path).name.casefold(),suffix_registry.get(PurePosixPath(path).suffix.casefold(),fallback))
        try:
            parsed=adapter.parse(path,safe)
            if parsed.adapter_id!=adapter.adapter_id:raise ValueError("ADAPTER_ID_MISMATCH")
            record=annotate_evidence(fragment,parsed,language=detection['files'][path]['language_hint'],terms=text_terms(safe),detector_version=DETECTOR_VERSION)
        except Exception:
            parsed=replace(fallback.parse(path,safe),status='parser_failed_fallback')
            record=annotate_evidence(fragment,parsed,language=detection['files'][path]['language_hint'],terms=text_terms(safe),detector_version=DETECTOR_VERSION)
            record['failed_adapter_id']=adapter.adapter_id
        parser_counts[record['parser_status']]=parser_counts.get(record['parser_status'],0)+1
        evidence.append(record)
        for kind,values in record['structured_metadata'].items():
            indexes.setdefault(path,{}).setdefault(kind,set()).update(values)
    files=[{**f,'index':{k:sorted(v) for k,v in indexes.get(f.get('path'),{}).items()}} for f in repository_map['files']]
    result = {**repository_map,'files':files,'evidence':evidence,'stack_detection':detection,
            'parser_coverage':parser_counts,'semantic_analysis_complete':False,
            'provider_calls':0,'network_model_calls':0}
    from app.repository_technology import enrich_technology
    return enrich_technology(result, adapters=technology_adapters)


def build_indexed_repository_map(project_id,project,git_state,*,adapters=DEFAULT_ADAPTERS):
    return index_repository_map(build_repository_map(project_id,project,git_state),adapters=adapters)
