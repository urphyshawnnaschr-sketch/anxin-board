from __future__ import annotations

from copy import deepcopy

import pytest

from app import model_qualification_harness as harness


def _pack():
    return harness.load_base_sample_pack()


def _observations(pack):
    return [
        {
            "sample_id": sample["sample_id"],
            "git_objective_stats_correct": True,
            "no_fabrication": True,
            "main_feature_correct": True,
            "stage_correct": True,
            "implementation_scope_correct": True,
            "conservative_when_evidence_limited": True,
            "structural_integrity": True,
            "source_distinction_correct": True,
            "boundary_compliance": True,
            "hard_failures": [],
        }
        for sample in pack["samples"]
    ]


def _repeat_runs(pack):
    result = {}
    for sample in pack["samples"]:
        if sample["sample_id"] not in harness.REPEAT_REPRESENTATIVE_SAMPLE_IDS:
            continue
        gold = sample["gold_facts"]
        result[sample["sample_id"]] = [
            {
                "main_feature": gold["main_feature"],
                "stage": gold["stage"],
                "implementation_scope": gold["implementation_scope"],
                "contradiction": False,
            }
            for _ in range(3)
        ]
    return result


def _human(scores=(4.0, 4.0, 5.0)):
    return [
        {"reviewer_id": f"human-{index + 1}", "score": score}
        for index, score in enumerate(scores)
    ]


def test_base_pack_is_exact_12_with_required_language_distribution_and_conservative_cases():
    pack = _pack()
    assert pack["sample_pack_version"] == harness.BASE_PACK_VERSION
    assert len(pack["samples"]) == 12
    assert harness._stable_hash({key: value for key, value in pack.items() if key != "sample_manifest_hash"}) == pack["sample_manifest_hash"]
    counts = {}
    for sample in pack["samples"]:
        counts[sample["language_family"]] = counts.get(sample["language_family"], 0) + 1
    assert counts == harness.EXPECTED_LANGUAGE_COUNTS
    assert sum(harness._is_conservative_sample(sample) for sample in pack["samples"]) >= 4


def test_machine_pass_without_real_human_review_is_not_qualified():
    pack = _pack()
    result = harness.evaluate_qualification_run(
        observations=_observations(pack),
        repeat_runs=_repeat_runs(pack),
        readability_reviews=None,
        pack=pack,
    )
    assert result["machine_gate_passed"] is True
    assert result["human_gate_passed"] is False
    assert result["qualification_status"] == "human_gate_required"
    assert result["conservative_evidence"]["passed_count"] >= 4


def test_three_real_human_scores_can_complete_dual_gate():
    pack = _pack()
    result = harness.evaluate_qualification_run(
        observations=_observations(pack),
        repeat_runs=_repeat_runs(pack),
        readability_reviews=_human(),
        pack=pack,
    )
    assert result["machine_gate_passed"] is True
    assert result["human_gate_passed"] is True
    assert result["qualification_status"] == "qualified"
    assert result["human_readability"]["mean_score"] >= 4.0
    assert result["human_readability"]["minimum_score"] >= 3.0


def test_any_hard_failure_blocks_qualification_even_with_human_scores():
    pack = _pack()
    observations = _observations(pack)
    observations[0]["hard_failures"] = ["FABRICATED_GIT_FACT"]
    result = harness.evaluate_qualification_run(
        observations=observations,
        repeat_runs=_repeat_runs(pack),
        readability_reviews=_human(),
        pack=pack,
    )
    assert result["machine_gate_passed"] is False
    assert result["qualification_status"] == "not_qualified"
    assert result["hard_failures"] == [{"sample_id": pack["samples"][0]["sample_id"], "code": "FABRICATED_GIT_FACT"}]


def test_repeat_instability_blocks_machine_gate():
    pack = _pack()
    repeats = _repeat_runs(pack)
    first_id = harness.REPEAT_REPRESENTATIVE_SAMPLE_IDS[0]
    repeats[first_id][0]["main_feature"] = "A"
    repeats[first_id][1]["main_feature"] = "B"
    repeats[first_id][2]["main_feature"] = "C"
    result = harness.evaluate_qualification_run(
        observations=_observations(pack),
        repeat_runs=repeats,
        readability_reviews=_human(),
        pack=pack,
    )
    assert result["metric_gates"]["repeat_stability"] is False
    assert result["machine_gate_passed"] is False


def test_repeat_gate_requires_exact_three_representatives():
    pack = _pack()
    repeats = _repeat_runs(pack)
    repeats.pop(next(iter(repeats)))
    result = harness.evaluate_qualification_run(
        observations=_observations(pack),
        repeat_runs=repeats,
        readability_reviews=_human(),
        pack=pack,
    )
    assert result["metric_gates"]["repeat_stability"] is False
    assert result["machine_gate_passed"] is False


def test_human_gate_rejects_low_individual_score_even_when_mean_is_four():
    pack = _pack()
    result = harness.evaluate_qualification_run(
        observations=_observations(pack),
        repeat_runs=_repeat_runs(pack),
        readability_reviews=_human((2.0, 5.0, 5.0)),
        pack=pack,
    )
    assert result["human_gate_passed"] is False
    assert result["qualification_status"] == "human_gate_required"


def test_pack_identity_corruption_fails_closed():
    pack = _pack()
    broken = deepcopy(pack)
    broken["samples"][1]["sample_id"] = broken["samples"][0]["sample_id"]
    with pytest.raises(harness.QualificationHarnessError) as exc_info:
        harness.validate_sample_pack(broken)
    assert exc_info.value.code == "QUALIFICATION_SAMPLE_PACK_INVALID"

def test_repeat_representative_authority_is_exact_cross_language_set():
    pack = _pack()
    by_id = {sample["sample_id"]: sample for sample in pack["samples"]}
    assert tuple(_repeat_runs(pack)) == harness.REPEAT_REPRESENTATIVE_SAMPLE_IDS
    assert {by_id[sample_id]["language_family"] for sample_id in harness.REPEAT_REPRESENTATIVE_SAMPLE_IDS} == set(harness.EXPECTED_LANGUAGE_COUNTS)


def test_repeat_gate_rejects_non_authoritative_pack_sample_substitution():
    pack = _pack()
    repeats = _repeat_runs(pack)
    removed = harness.REPEAT_REPRESENTATIVE_SAMPLE_IDS[0]
    replacement_runs = repeats.pop(removed)
    repeats["BASE-JSTS-01"] = replacement_runs
    result = harness.evaluate_qualification_run(
        observations=_observations(pack),
        repeat_runs=repeats,
        readability_reviews=_human(),
        pack=pack,
    )
    assert result["metric_gates"]["repeat_stability"] is False
    assert result["repeat_stability"]["representative_ids_match"] is False


def test_repeat_gate_rejects_unknown_three_id_substitution():
    pack = _pack()
    valid = _repeat_runs(pack)
    template = next(iter(valid.values()))
    repeats = {f"UNKNOWN-{index}": deepcopy(template) for index in range(3)}
    result = harness.evaluate_qualification_run(
        observations=_observations(pack),
        repeat_runs=repeats,
        readability_reviews=_human(),
        pack=pack,
    )
    assert result["metric_gates"]["repeat_stability"] is False
    assert result["repeat_stability"]["representative_ids_match"] is False
