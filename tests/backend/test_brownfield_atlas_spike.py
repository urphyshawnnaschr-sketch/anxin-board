import hashlib
import json

import pytest

from app.brownfield_atlas_spike import (
    SpikeContractError,
    aggregate_module_status,
    build_orientation_messages,
    build_requirement_verification_messages,
    choose_spike_module_ids,
    validate_orientation_result,
    validate_requirement_result,
)
from app.repository_atlas import build_evidence_bundle, build_provider_catalog, build_repository_atlas

HEAD = "a" * 40


def plan():
    return {
        "schema_version": "project_profile_v2",
        "project_summary": "camera platform",
        "planned_modules": [
            {"client_id": "m1", "name": "设备", "description": "", "requirements": ["添加设备"], "prd_refs": [], "exclusions": []},
            {"client_id": "m2", "name": "告警", "description": "", "requirements": ["异常告警", "确认告警"], "prd_refs": [], "exclusions": []},
            {"client_id": "m3", "name": "权限", "description": "", "requirements": ["登录", "鉴权", "审计"], "prd_refs": [], "exclusions": []},
            {"client_id": "m4", "name": "报表", "description": "", "requirements": ["导出"], "prd_refs": [], "exclusions": []},
        ],
    }


def ev(identity, path, text, metadata):
    return {
        "evidence_id": "repo-code-" + identity,
        "path": path,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "exact_head": HEAD,
        "evidence_schema_version": "repository-evidence/1",
        "adapter_id": "test/1",
        "parser_status": "heuristic",
        "language_hint": "python",
        "detector_version": "test",
        "structured_metadata": metadata,
        "terms": [],
        "confidence": "heuristic_not_semantic_proof",
        "semantic_coverage": "unknown",
    }


def indexed():
    evidence = [
        ev("a", "backend/device.py", "def add_device(): pass\n", {"symbol": ["add_device"], "route": [], "table": [], "import": []}),
        ev("b", "tests/test_device.py", "def test_add_device(): pass\n", {"test": ["test_add_device"], "symbol": ["test_add_device"], "route": [], "table": [], "import": []}),
    ]
    return {
        "exact_head": HEAD,
        "files": [{"path": item["path"], "coverage_state": "safe_text"} for item in evidence],
        "evidence": evidence,
        "coverage": {"safe_text": 2},
        "tracked_files": 2,
        "safe_text_bytes": sum(len(item["content"].encode()) for item in evidence),
        "complete_inventory": True,
        "complete_safe_analysis": True,
    }


def fixture_catalog_bundle():
    repo = indexed(); atlas = build_repository_atlas(repo); catalog = build_provider_catalog(atlas)
    path_id = next(item["path_id"] for item in atlas["paths"] if item["path"] == "backend/device.py")
    bundle = build_evidence_bundle(repo, atlas, [path_id], max_hops=0)
    return catalog, bundle, path_id


def test_spike_module_selection_spans_requirement_complexity():
    assert choose_spike_module_ids(plan()) == ["m1", "m2", "m3"]


def test_orientation_prompt_contains_no_source_evidence_and_uses_indexed_paths():
    catalog, _bundle, path_id = fixture_catalog_bundle()
    messages = build_orientation_messages(plan(), catalog, ["m1"])
    raw = json.dumps(messages, ensure_ascii=False)
    assert "def add_device" not in raw
    assert json.loads(messages[1]["content"])["repository_atlas"]["paths"][0]["path_index"] == 0
    index = next(i for i, item in enumerate(catalog["paths"]) if item["path_id"] == path_id)
    result = {"modules": [{"planned_module_id": "m1", "seed_path_indexes": [index]}]}
    assert validate_orientation_result(result, catalog=catalog, module_ids=["m1"]) == {"m1": [path_id]}
    forged = {"modules": [{"planned_module_id": "m1", "seed_path_indexes": [999]}]}
    with pytest.raises(SpikeContractError, match="PATH_INVALID"):
        validate_orientation_result(forged, catalog=catalog, module_ids=["m1"])


def test_orientation_requires_exact_module_coverage_and_rejects_extra_modules():
    catalog, _bundle, path_id = fixture_catalog_bundle()
    index = next(i for i, item in enumerate(catalog["paths"]) if item["path_id"] == path_id)
    missing = {"modules": []}
    with pytest.raises(SpikeContractError, match="COVERAGE_INVALID"):
        validate_orientation_result(missing, catalog=catalog, module_ids=["m1"])
    extra = {"modules": [{"planned_module_id": "outside", "seed_path_indexes": [index]}]}
    with pytest.raises(SpikeContractError, match="OUT_OF_SCOPE"):
        validate_orientation_result(extra, catalog=catalog, module_ids=["m1"])


def test_orientation_rejects_string_indexes_and_duplicates():
    catalog, _bundle, _path_id = fixture_catalog_bundle()
    for indexes in (["0"], [0, 0], [-1]):
        result = {"modules": [{"planned_module_id": "m1", "seed_path_indexes": indexes}]}
        with pytest.raises(SpikeContractError, match="PATH_INVALID"):
            validate_orientation_result(result, catalog=catalog, module_ids=["m1"])


def test_verification_is_requirement_level_and_positive_requires_exact_evidence():
    _catalog, bundle, _path_id = fixture_catalog_bundle()
    module = plan()["planned_modules"][0]
    messages = build_requirement_verification_messages(module, bundle)
    assert "def add_device" in messages[1]["content"]
    assert "at most 6" in messages[0]["content"]
    assert "1200" in messages[0]["content"]
    evidence_id = bundle["evidence"][0]["evidence_id"]
    result = {"requirements": [{"requirement_index": 0, "status": "implemented", "evidence_indexes": [0], "rationale": "function adds device"}]}
    clean = validate_requirement_result(result, module=module, bundle=bundle)
    assert clean[0]["status"] == "implemented"
    assert clean[0]["evidence_ids"] == [evidence_id]
    bad = {"requirements": [{"requirement_index": 0, "status": "implemented", "evidence_indexes": [], "rationale": "guess"}]}
    with pytest.raises(SpikeContractError, match="POSITIVE_WITHOUT_EVIDENCE"):
        validate_requirement_result(bad, module=module, bundle=bundle)


def test_unknown_context_refs_preserve_status_and_cross_bundle_refs_fail_closed():
    _catalog, bundle, _path_id = fixture_catalog_bundle(); module = plan()["planned_modules"][0]
    evidence_id = bundle["evidence"][0]["evidence_id"]
    result = validate_requirement_result({"requirements": [{"requirement_index": 0, "status": "unknown", "evidence_indexes": [0], "rationale": "not enough"}]}, module=module, bundle=bundle)
    assert result[0]["status"] == "unknown" and result[0]["evidence_ids"] == [evidence_id]
    with pytest.raises(SpikeContractError, match="EVIDENCE_INVALID"):
        validate_requirement_result({"requirements": [{"requirement_index": 0, "status": "partial", "evidence_indexes": [1], "rationale": "x"}]}, module=module, bundle=bundle)


def test_module_status_is_deterministic_from_requirement_results():
    assert aggregate_module_status([{"status": "implemented"}, {"status": "implemented"}]) == "implemented"
    assert aggregate_module_status([{"status": "implemented"}, {"status": "unknown"}]) == "partial"
    assert aggregate_module_status([{"status": "partial"}, {"status": "unknown"}]) == "partial"
    assert aggregate_module_status([{"status": "unknown"}, {"status": "unknown"}]) == "unknown"


def test_verification_prompt_numbers_original_requirements_and_requires_all_rows():
    _catalog, bundle, _path_id = fixture_catalog_bundle()
    module = plan()["planned_modules"][1]
    messages = build_requirement_verification_messages(module, bundle)
    payload = json.loads(messages[1]["content"])
    assert payload["planned_module"] == module
    assert payload["requirement_catalog"] == [{"requirement_index": i, "text": text} for i, text in enumerate(module["requirements"])]
    rows = payload["required_output_schema"]["properties"]["requirements"]
    assert rows["minItems"] == rows["maxItems"] == 2
    fields = rows["items"]["properties"]
    assert fields["requirement_index"] == {"type": "integer", "minimum": 0, "maximum": 1}
    assert set(fields["status"]["enum"]) == {"implemented", "partial", "unknown"}
    assert fields["evidence_indexes"]["maxItems"] == 6
    assert fields["rationale"]["maxLength"] == 1200
    assert "all 2 requirements" in messages[0]["content"]
    assert "unknown" in messages[0]["content"] and "omit" in messages[0]["content"]


@pytest.mark.parametrize(("indexes", "suffix"), [([0], "COUNT"), ([0, 0], "INDEX_SET"), ([0, 2], "INDEX_SET"), ([0, True], "INDEX_SET")])
def test_verification_coverage_safe_subcodes(indexes, suffix):
    _catalog, bundle, _path_id = fixture_catalog_bundle()
    module = plan()["planned_modules"][1]
    result = {"requirements": [{"requirement_index": i, "status": "unknown", "evidence_indexes": [], "rationale": "private text"} for i in indexes]}
    with pytest.raises(SpikeContractError) as error:
        validate_requirement_result(result, module=module, bundle=bundle)
    assert str(error.value) == "SPIKE_VERIFICATION_COVERAGE_INVALID_" + suffix


def test_verification_all_unknown_still_covers_every_requirement():
    _catalog, bundle, _path_id = fixture_catalog_bundle()
    module = plan()["planned_modules"][1]
    rows = [{"requirement_index": i, "status": "unknown", "evidence_indexes": [], "rationale": "insufficient evidence"} for i in [1, 0]]
    assert [item["requirement_index"] for item in validate_requirement_result({"requirements": rows}, module=module, bundle=bundle)] == [0, 1]

@pytest.mark.parametrize(("refs", "suffix"), [("0", "NOT_LIST"), (list(range(7)), "TOO_MANY"), ([True], "TYPE"), ([0.0], "TYPE"), (["0"], "TYPE"), ([{}], "TYPE"), ([[]], "TYPE"), ([0, 0], "DUPLICATE"), ([-1], "OUT_OF_RANGE"), ([1], "OUT_OF_RANGE")])
def test_evidence_indexes_fail_closed_with_safe_codes(refs, suffix):
    _catalog, bundle, _path_id = fixture_catalog_bundle()
    result = {"requirements": [{"requirement_index": 0, "status": "partial", "evidence_indexes": refs, "rationale": "private"}]}
    with pytest.raises(SpikeContractError) as error:
        validate_requirement_result(result, module=plan()["planned_modules"][0], bundle=bundle)
    assert str(error.value) == "SPIKE_VERIFICATION_EVIDENCE_INVALID_" + suffix

def test_verification_evidence_indexes_are_local_to_bundle_and_opaque_ids_are_hidden():
    _catalog, bundle, _path_id = fixture_catalog_bundle()
    module = plan()["planned_modules"][0]
    messages = build_requirement_verification_messages(module, bundle)
    payload = json.loads(messages[1]["content"])
    assert payload["schema_version"] == "brownfield-requirement-verification/3"
    assert payload["source_evidence"][0]["evidence_index"] == 0
    assert "evidence_id" not in payload["source_evidence"][0]
    assert bundle["evidence"][0]["evidence_id"] not in messages[1]["content"]
    field = payload["required_output_schema"]["properties"]["requirements"]["items"]["properties"]["evidence_indexes"]
    assert field["items"] == {"type": "integer", "minimum": 0, "maximum": 0}
    other = dict(bundle, evidence=[indexed()["evidence"][1]])
    result = {"requirements": [{"requirement_index": 0, "status": "partial", "evidence_indexes": [0], "rationale": "source"}]}
    assert validate_requirement_result(result, module=module, bundle=other)[0]["evidence_ids"] == ["repo-code-b"]
    result["requirements"][0]["evidence_indexes"] = ["repo-code-a"]
    with pytest.raises(SpikeContractError, match="EVIDENCE_INVALID_TYPE"):
        validate_requirement_result(result, module=module, bundle=other)

@pytest.mark.parametrize("identity", ["", None, [], "repo-code-a"])
def test_bundle_evidence_identity_must_be_nonempty_unique_string(identity):
    _catalog, bundle, _path_id = fixture_catalog_bundle()
    bundle["evidence"].append(dict(bundle["evidence"][0], evidence_id=identity))
    module = plan()["planned_modules"][0]
    with pytest.raises(SpikeContractError, match="SPIKE_BUNDLE_INVALID"):
        build_requirement_verification_messages(module, bundle)
    result = {"requirements": [{"requirement_index": 0, "status": "unknown", "evidence_indexes": [], "rationale": "insufficient"}]}
    with pytest.raises(SpikeContractError, match="SPIKE_BUNDLE_INVALID"):
        validate_requirement_result(result, module=module, bundle=bundle)

@pytest.mark.parametrize('refs', [[0,0],[True],[-1],[99999],['0'],[[]]])
def test_unknown_context_citations_keep_strict_index_contract(refs):
    _,bundle,_=fixture_catalog_bundle();module=plan()['planned_modules'][0]
    value={'requirements':[{'requirement_index':0,'status':'unknown','evidence_indexes':refs,'rationale':'uncertain'}]}
    with pytest.raises(SpikeContractError,match='EVIDENCE_INVALID'):
        validate_requirement_result(value,module=module,bundle=bundle)
