import hashlib
import json
from copy import deepcopy

import pytest

from app.brownfield_atlas_spike import SpikeContractError, build_orientation_messages, validate_orientation_result
from app.repository_atlas import build_provider_catalog, build_repository_atlas

HEAD = "a" * 40


def _catalog():
    text = "def add_device(): pass\n"
    evidence = {
        "evidence_id": "repo-code-a",
        "path": "backend/device.py",
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "exact_head": HEAD,
        "evidence_schema_version": "repository-evidence/1",
        "adapter_id": "test/1",
        "parser_status": "heuristic",
        "language_hint": "python",
        "detector_version": "test",
        "structured_metadata": {"symbol": ["add_device"], "route": [], "table": [], "import": []},
        "terms": [],
        "confidence": "heuristic_not_semantic_proof",
        "semantic_coverage": "unknown",
    }
    indexed = {
        "exact_head": HEAD,
        "files": [{"path": "backend/device.py", "coverage_state": "safe_text"}],
        "evidence": [evidence],
        "coverage": {"safe_text": 1},
        "tracked_files": 1,
        "safe_text_bytes": len(text.encode()),
        "complete_inventory": True,
        "complete_safe_analysis": True,
    }
    return build_provider_catalog(build_repository_atlas(indexed))


def _plan():
    return {"schema_version": "project_profile_v2", "project_summary": "camera", "planned_modules": [{"client_id": "m1", "name": "设备", "requirements": ["添加设备"]}]}


def test_model_catalog_assigns_ephemeral_deterministic_path_indexes():
    catalog = _catalog()
    messages = build_orientation_messages(_plan(), catalog, ["m1"])
    assert '"path_index":0' in messages[1]["content"]
    assert "def add_device" not in messages[1]["content"]
    path_id = catalog["paths"][0]["path_id"]
    result = {"modules": [{"planned_module_id": "m1", "seed_path_indexes": [0]}]}
    assert validate_orientation_result(result, catalog=catalog, module_ids=["m1"]) == {"m1": [path_id]}


def test_string_path_or_opaque_id_is_no_longer_accepted_from_model():
    catalog = _catalog()
    for value in ("backend/device.py", catalog["paths"][0]["path_id"], "0"):
        result = {"modules": [{"planned_module_id": "m1", "seed_path_indexes": [value]}]}
        with pytest.raises(SpikeContractError, match="SPIKE_ORIENTATION_PATH_INVALID"):
            validate_orientation_result(result, catalog=catalog, module_ids=["m1"])


def test_out_of_range_duplicate_and_bool_indexes_fail_closed():
    catalog = _catalog()
    for values in ([1], [-1], [0, 0], [True]):
        result = {"modules": [{"planned_module_id": "m1", "seed_path_indexes": values}]}
        with pytest.raises(SpikeContractError, match="SPIKE_ORIENTATION_PATH_INVALID"):
            validate_orientation_result(result, catalog=catalog, module_ids=["m1"])


def test_orientation_prompt_states_validation_limits():
    messages = build_orientation_messages(_plan(), _catalog(), ["m1"])
    assert "12" in messages[0]["content"]
    assert "rationale" not in messages[0]["content"]


def test_gap_catalog_dense_indexes_bind_to_remaining_real_ids_and_hash():
    from app.brownfield_atlas_spike import build_gap_catalog, build_gap_orientation_messages

    catalog = _catalog()
    template = catalog["paths"][0]
    catalog["paths"] = [dict(template, path_id=f"p{i}", path=f"src/{i}.py") for i in range(4)]
    catalog["paths"][3]["neighbors"] = [{"path_id": "p0", "kind": "import"}, {"path_id": "p1", "kind": "import"}, {"path_id": "outside", "kind": "import"}]
    plan = _plan()
    plan["planned_modules"].append(dict(plan["planned_modules"][0], client_id="m2"))
    checked = {"m1": ["p0"], "m2": ["p2"]}
    before = deepcopy(catalog)
    gap = build_gap_catalog(catalog, ["m1", "m2"], checked)
    assert gap == build_gap_catalog(catalog, ["m1", "m2"], checked)
    assert catalog == before
    assert [item["path_id"] for item in gap["paths"]] == ["p1", "p3"]
    assert gap["parent_catalog_hash"] == catalog["catalog_hash"]
    assert gap["catalog_hash"] != catalog["catalog_hash"]
    assert gap["exact_head"] == catalog["exact_head"]
    unhashed = {key: value for key, value in gap.items() if key != "catalog_hash"}
    assert gap["catalog_hash"] == hashlib.sha256(json.dumps(unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    messages = build_gap_orientation_messages(plan, catalog, ["m1", "m2"], checked)
    payload = json.loads(messages[1]["content"])
    shown = payload["repository_atlas"]
    assert [item["path_index"] for item in shown["paths"]] == [0, 1]
    assert shown["full_safe_path_count"] == 4
    assert shown["safe_path_count"] == 2
    assert shown["excluded_checked_path_count"] == 2
    assert shown["paths"][1]["neighbors"] == [{"path_index": 0, "kind": "import"}]
    assert shown["paths"][1]["excluded_checked_neighbor_count"] == 1
    assert shown["excluded_checked_neighbor_count"] == 1
    assert shown["excluded_neighbor_count"] == 2
    assert "not proof of full repository semantic coverage" in messages[0]["content"]
    seeds = payload["required_output_schema"]["properties"]["modules"]["items"]["properties"]["seed_path_indexes"]
    assert seeds["items"] == {"type": "integer", "minimum": 0, "maximum": 1}
    result = {"modules": [{"planned_module_id": name, "seed_path_indexes": [0]} for name in ["m1", "m2"]]}
    assert validate_orientation_result(result, catalog=gap, module_ids=["m1", "m2"]) == {"m1": ["p1"], "m2": ["p1"]}
    result["modules"][0]["seed_path_indexes"] = [2]
    with pytest.raises(SpikeContractError, match="OUT_OF_RANGE"):
        validate_orientation_result(result, catalog=gap, module_ids=["m1", "m2"])


def test_gap_empty_remaining_pool_requires_empty_seeds():
    from app.brownfield_atlas_spike import build_gap_orientation_messages

    catalog = _catalog()
    messages = build_gap_orientation_messages(_plan(), catalog, ["m1"], {"m1": [catalog["paths"][0]["path_id"]]})
    payload = json.loads(messages[1]["content"])
    assert payload["repository_atlas"]["paths"] == []
    assert payload["repository_atlas"]["safe_path_count"] == 0
    assert payload["required_output_schema"]["properties"]["modules"]["items"]["properties"]["seed_path_indexes"]["maxItems"] == 0
    assert "pool is empty" in messages[0]["content"]


@pytest.mark.parametrize("checked", [{}, {"outside": []}, {"m1": ["unknown"]}, {"m1": [True]}, {"m1": "wrong"}])
def test_gap_orientation_rejects_invalid_checked_scope(checked):
    from app.brownfield_atlas_spike import build_gap_orientation_messages

    with pytest.raises(SpikeContractError, match="CHECKED_PATHS_INVALID"):
        build_gap_orientation_messages(_plan(), _catalog(), ["m1"], checked)


def test_model_catalog_compacts_ids_preserves_metadata_and_accounts_for_excluded_neighbors():
    catalog = _catalog()
    first_id = catalog["paths"][0]["path_id"]
    second_id = "atlas-path-" + "b" * 24
    catalog["paths"].append(dict(catalog["paths"][0], path_id=second_id, path="backend/other.py"))
    catalog["paths"][0]["neighbors"] = [
        {"path_id": second_id, "kind": "import"},
        {"path_id": "atlas-path-unsafe", "kind": "import"},
    ]
    catalog["paths"][1]["neighbors"] = [{"path_id": first_id, "kind": "import"}]
    before = deepcopy(catalog)
    payload = json.loads(build_orientation_messages(_plan(), catalog, ["m1"])[1]["content"])
    compact = payload["repository_atlas"]
    assert catalog == before
    assert len(compact["paths"]) == len(catalog["paths"])
    assert [item["path_index"] for item in compact["paths"]] == [0, 1]
    assert all("path_id" not in item for item in compact["paths"])
    assert compact["paths"][0]["neighbors"] == [{"path_index": 1, "kind": "import"}]
    assert compact["paths"][1]["neighbors"] == [{"path_index": 0, "kind": "import"}]
    assert compact["paths"][0]["excluded_neighbor_count"] == 1
    assert compact["excluded_neighbor_count"] == 1
    assert "excluded_neighbor_count" not in compact["paths"][1]
    for original, converted in zip(catalog["paths"], compact["paths"]):
        for key, value in original.items():
            if key not in {"path_id", "neighbors"}:
                assert converted[key] == value
    assert len(json.dumps(compact)) < len(json.dumps(catalog))


def test_model_catalog_rejects_duplicate_path_ids():
    catalog = _catalog()
    catalog["paths"].append(dict(catalog["paths"][0], path="backend/duplicate.py"))
    with pytest.raises(SpikeContractError, match="CATALOG_INVALID"):
        build_orientation_messages(_plan(), catalog, ["m1"])


def test_import_dictionary_losslessly_preserves_order_and_duplicate_values():
    catalog = _catalog()
    catalog["paths"][0]["imports"] = ["java.util.List", "java.util.Map", "java.util.List"]
    catalog["paths"].append(dict(catalog["paths"][0], path_id="atlas-path-other", path="backend/other.py", imports=["java.util.Map", "app.service"]))
    catalog["paths"].append(dict(catalog["paths"][0], path_id="atlas-path-empty", path="backend/empty.py", imports=[]))
    messages = build_orientation_messages(_plan(), catalog, ["m1"])
    compact = json.loads(messages[1]["content"])["repository_atlas"]
    assert compact["import_dictionary"] == ["app.service", "java.util.List", "java.util.Map"]
    assert "import_dictionary" in messages[0]["content"]
    assert "import_indexes" in messages[0]["content"]
    for original, converted in zip(catalog["paths"], compact["paths"]):
        assert "imports" not in converted
        assert [compact["import_dictionary"][i] for i in converted["import_indexes"]] == original["imports"]


@pytest.mark.parametrize(("indexes", "suffix"), [
    ("0", "NOT_LIST"),
    (list(range(13)), "TOO_MANY"),
    (["0"], "INDEX_TYPE"),
    ([True], "INDEX_TYPE"),
    ([0.0], "INDEX_TYPE"),
    ([{}], "INDEX_TYPE"),
    ([0, 0], "DUPLICATE"),
    ([-1], "OUT_OF_RANGE"),
    ([1], "OUT_OF_RANGE"),
])
def test_orientation_safe_error_subcodes(indexes, suffix):
    result = {"modules": [{"planned_module_id": "m1", "seed_path_indexes": indexes}]}
    with pytest.raises(SpikeContractError) as error:
        validate_orientation_result(result, catalog=_catalog(), module_ids=["m1"])
    assert str(error.value) == "SPIKE_ORIENTATION_PATH_INVALID_" + suffix


def test_orientation_prompt_explains_json_integer_range_and_self_check():
    catalog = _catalog()
    catalog["paths"].append(dict(catalog["paths"][0], path_id="atlas-path-other", path="backend/other.py"))
    system = build_orientation_messages(_plan(), catalog, ["m1"])[0]["content"]
    assert "unique JSON integers" in system
    assert "not strings" in system
    assert "0..1" in system
    assert "[0,1]" in system
    assert "Before returning" in system
    assert "not path indexes" in system
    payload = json.loads(build_orientation_messages(_plan(), catalog, ["m1"])[1]["content"])
    seed_schema = payload["required_output_schema"]["properties"]["modules"]["items"]["properties"]["seed_path_indexes"]
    assert seed_schema == {"type": "array", "maxItems": 12, "uniqueItems": True, "items": {"type": "integer", "minimum": 0, "maximum": 1}}


@pytest.mark.parametrize(("result", "suffix"), [
    ({"modules": [], "schema_version": "wrong"}, "TOP_LEVEL"),
    ({"modules": [{}]}, "ROW_FIELDS"),
    ({"modules": [{"planned_module_id": [], "seed_path_indexes": []}]}, "IDENTITY_TYPE"),
    ({"modules": [{"planned_module_id": {}, "seed_path_indexes": []}]}, "IDENTITY_TYPE"),
    ({"modules": [{"planned_module_id": "m1", "seed_path_indexes": [], "rationale": []}]}, "ROW_FIELDS"),
    ({"modules": [{"planned_module_id": "m1", "seed_path_indexes": [], "rationale": " "}]}, "ROW_FIELDS"),
    ({"modules": [{"planned_module_id": "m1", "seed_path_indexes": [], "rationale": "x" * 1201}]}, "ROW_FIELDS"),
])
def test_orientation_generic_safe_subcodes(result, suffix):
    with pytest.raises(SpikeContractError) as error:
        validate_orientation_result(result, catalog=_catalog(), module_ids=["m1"])
    assert str(error.value) == "SPIKE_ORIENTATION_INVALID_" + suffix


def test_orientation_output_example_uses_all_actual_module_ids_and_valid_rows():
    plan = _plan()
    plan["planned_modules"].append(dict(plan["planned_modules"][0], client_id="m2"))
    messages = build_orientation_messages(plan, _catalog(), ["m1", "m2"])
    example = json.loads(messages[1]["content"])["required_output"]
    assert [row["planned_module_id"] for row in example["modules"]] == ["m1", "m2"]
    assert validate_orientation_result(example, catalog=_catalog(), module_ids=["m1", "m2"]) == {"m1": [], "m2": []}
    assert "exactly two fields" in messages[0]["content"]
    assert "exactly two fields" in messages[0]["content"]
