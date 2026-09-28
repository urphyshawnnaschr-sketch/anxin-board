"""Offline structural checks. A structural pass never grants model qualification.

Semantic facts, correction handling and readability remain explicit review work.
This module has no provider, credential, database or registry mutation code.
"""
from collections import Counter
from app.ai_contract_validation import _validate_result_contract

SEMANTIC_REVIEW_FIELDS = (
    "git_objective_stats_correct", "no_fabrication", "main_feature_correct",
    "conservative_when_evidence_limited", "source_distinction_correct",
    "boundary_compliance", "correction_handled_or_reasonably_refused",
    "unaffected_correct_facts_preserved", "no_new_unsupported_claims",
)


def _references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids":
                yield from child
            else:
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def evaluate_result(sample, result):
    """Use only frozen exact assertions; never infer semantic truth from keywords."""
    issues = _validate_result_contract("daily_report_regenerate", result)
    failures = sorted({issue.code for issue in issues})
    evaluation = {
        "sample_id": sample["sample_id"],
        "structural_contract_valid": not issues,
        "hard_failures": failures,
        "semantic_review": {field: None for field in SEMANTIC_REVIEW_FIELDS},
        "human_readability_reviews": None,
        "qualification_status": "not_evaluated",
        "admission": "not_admitted",
    }
    if issues:
        return evaluation
    allowed_ids = {e["evidence_id"] for e in sample["evidence"]}
    allowed_ids.add(sample["human_correction"]["evidence_id"])
    if not set(_references(result)) <= allowed_ids:
        failures.append("CROSS_SAMPLE_OR_UNKNOWN_EVIDENCE")
    new_report = result["new_report"]
    progress = new_report["feature_progress"]
    names = [row["feature"] for row in progress]
    if Counter(names) != Counter(sample["expected_module_names"]):
        failures.append("EXACT_MODULE_COVERAGE_MISMATCH")
    stage_valid = all(row["stage"] in sample["base_allowed_stage_set"] for row in progress)
    scope_valid = all(row["implementation_scope"] in sample["base_allowed_implementation_scope_set"] for row in progress)
    # Stage/scope are scored by the original aggregate 10-of-12 threshold.
    # They are not silently promoted to new per-sample hard gates here.
    evaluation.update({
        "stage_allowed": stage_valid,
        "implementation_scope_allowed": scope_valid,
        "reference_whitelist_valid": "CROSS_SAMPLE_OR_UNKNOWN_EVIDENCE" not in failures,
        "exact_module_coverage_valid": "EXACT_MODULE_COVERAGE_MISMATCH" not in failures,
        "hard_failures": sorted(set(failures)),
        "local_structural_checks_passed": not failures,
        "semantic_review_state": "required",
        "qualification_status": "not_qualified" if failures else "semantic_and_human_review_required",
    })
    return evaluation
