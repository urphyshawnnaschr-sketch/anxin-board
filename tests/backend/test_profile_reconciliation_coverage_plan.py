from copy import deepcopy
import hashlib
import pytest

from app.profile_reconciliation_coverage_plan import plan_coverage_batches, validate_coverage_plan, public_coverage_summary
from app.profile_reconciliation_batches import BatchError, _bytes, _hash

HEAD = "b" * 40
BUDGET = dict(context_window_tokens=12000, max_output_tokens=1000,
              reserved_output_tokens=1000, safety_margin_tokens=1000,
              counting_policy_version="utf8_byte_upper_bound_v1")


def module_plan(count=12):
    return dict(schema_version="project_profile_v2", planned_modules=[
        dict(client_id=f"m{i}", name=f"Module {i}", requirements=[f"Requirement {i}"])
        for i in range(count)])


def evidence(index, text="hello source"):
    return dict(evidence_id=f"repo-code-{index}", path=f"src/{index}.exotic",
                exact_head=HEAD, content=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest())


def build(items, **kwargs):
    return plan_coverage_batches(module_plan(kwargs.pop("count", 12)), items,
        exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET, **kwargs)


@pytest.mark.parametrize("count", [12, 38, 70])
def test_dynamic_catalog_and_large_repository_complete(count):
    items = [evidence(i, "源码🙂" * 300) for i in range(18)]
    value = build(items, count=count)
    validate_coverage_plan(value)
    assert len(value["batches"]) > 1
    fragments = [f for batch in value["batches"] for f in batch["fragments"]]
    assert {f["parent_evidence_id"] for f in fragments} == {i["evidence_id"] for i in items}
    assert all(b["input_upper_bound"] <= 10000 for b in value["batches"])
    assert all(len(b["planned_modules"]) == count for b in value["batches"])
    assert all(f["module_association"] == "unassigned" for f in fragments)
    assert value["provider_calls"] == value["network_model_calls"] == 0


def test_oversized_chunk_splits_without_unicode_loss_or_overlap():
    item = evidence(1, "你好🙂\\\"\n" * 5000)
    value = build([item])
    fragments = [f for b in value["batches"] for f in b["fragments"]]
    assert len(fragments) > 1
    assert "".join(f["content"] for f in fragments) == item["content"]
    assert fragments[0]["char_start"] == 0
    assert fragments[-1]["char_end"] == len(item["content"])
    assert all(a["char_end"] == b["char_start"] for a, b in zip(fragments, fragments[1:]))
    validate_coverage_plan(value)


def test_selection_is_local_not_provider_success_and_order_stable():
    items = [evidence(1), evidence(2), evidence(3)]
    first = build(items, selected_evidence_ids=["repo-code-2", "repo-code-1", "repo-code-2"])
    assert first == build(list(reversed(items)), selected_evidence_ids=["repo-code-1", "repo-code-2"])
    assert first["counts"]["remaining_evidence"] == 1
    assert first["selection_state"] == "planned_only_not_provider_reviewed"
    assert not build(items, selected_evidence_ids=[i["evidence_id"] for i in items])["batches"]
    assert not build([])["batches"]
    with pytest.raises(BatchError):
        build(items, selected_evidence_ids=["repo-code-absent"])


@pytest.mark.parametrize("mutation", ["head", "hash", "missing", "duplicate", "range", "selection", "budget"])
def test_tampering_fails_closed(mutation):
    value = build([evidence(1, "x" * 15000)])
    changed = deepcopy(value)
    if mutation == "head": changed["exact_head"] = "c" * 40
    elif mutation == "hash": changed["batches"][0]["batch_id"] = "0" * 64
    elif mutation == "missing": changed["batches"].pop()
    elif mutation == "duplicate": changed["batches"].append(changed["batches"][0])
    elif mutation == "range": changed["batches"][0]["fragments"][0]["char_start"] = 1
    elif mutation == "selection": changed["selected_evidence_ids"] = ["repo-code-absent"]
    else: changed["budget_record"]["context_window_tokens"] = 100000
    with pytest.raises(BatchError): validate_coverage_plan(changed)


def test_unsafe_or_unsupported_input_cannot_enter_inventory():
    with pytest.raises(BatchError): build([evidence(1) | {"content": b"binary"}])
    with pytest.raises(BatchError): build([evidence(1) | {"exact_head": "c" * 40}])
    with pytest.raises(BatchError): build([evidence(1) | {"path": "../secret"}])


def test_catalog_alone_over_budget_fails_instead_of_truncating():
    with pytest.raises(BatchError, match="COVERAGE_FRAME_OVER_BUDGET"):
        build([evidence(1)], count=200)


def test_recomputed_self_hash_does_not_allow_missing_fragment():
    value = build([evidence(1, "x" * 15000)])
    value["batches"].pop()
    value["coverage_plan_hash"] = _hash({k: v for k, v in value.items() if k != "coverage_plan_hash"})
    with pytest.raises(BatchError): validate_coverage_plan(value)


def test_exact_final_json_size_and_safe_summary():
    value = build([evidence(1, 'customer_private_source🙂"' * 700)])
    for batch in value["batches"]:
        assert batch["input_upper_bound"] == len(_bytes(batch))
    summary = public_coverage_summary(value)
    assert b"customer_private_source" not in _bytes(summary)
    assert b"Requirement" not in _bytes(summary)
    assert b"src/" not in _bytes(summary)
    assert summary["model_analysis_complete"] is False
    assert summary["provider_dispatch_supported"] is False


def test_empty_safe_fragment_and_parent_offsets_are_preserved():
    value = build([evidence(1, ""), evidence(2, "text") | {"char_start": 50, "char_end": 54, "object_sha": "a" * 40}])
    validate_coverage_plan(value)
    fragments = [f for b in value["batches"] for f in b["fragments"]]
    assert len(fragments) == 2
    assert fragments[0]["char_start"] == fragments[0]["char_end"] == 0
    assert fragments[1]["parent_char_start"] == 50
    assert fragments[1]["parent_char_end"] == 54
    assert fragments[1]["object_identity"] == "a" * 40
