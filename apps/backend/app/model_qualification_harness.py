"""Fail-closed fixed-sample model qualification evaluator.

This module evaluates evidence only. It never calls a provider, reads credentials, or
writes the shipping qualification registry. The readability gate requires three real
human reviews and cannot be synthesized by this code.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

SCHEMA_VERSION = "model_qualification_harness_v1"
PACK_SCHEMA_VERSION = "model_qualification_sample_pack_v1"
BASE_PACK_VERSION = "anxin-board-base-qualification/1.0"
EXPECTED_SAMPLE_COUNT = 12
EXPECTED_LANGUAGE_COUNTS = {"javascript_typescript": 4, "java": 4, "python": 4}
HUMAN_REVIEWER_COUNT = 3
HUMAN_MEAN_MIN = 4.0
HUMAN_INDIVIDUAL_MIN = 3.0
MIN_MAIN_FEATURE_PASS = 10
MIN_STAGE_PASS = 10
MIN_SCOPE_PASS = 10
MIN_CONSERVATIVE_PASS = 4
REPEAT_REPRESENTATIVE_SAMPLE_IDS = (
    "BASE-JSTS-04",
    "BASE-JAVA-02",
    "BASE-PY-04",
)
REPEAT_REPRESENTATIVE_COUNT = len(REPEAT_REPRESENTATIVE_SAMPLE_IDS)
REPEAT_RUNS_PER_SAMPLE = 3
MIN_REPEAT_CONSISTENCY = 2
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


class QualificationHarnessError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise QualificationHarnessError("QUALIFICATION_CANONICALIZATION_FAILED", "qualification evidence 无法 canonicalize") from exc


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _non_empty(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "固定样板包无法安全读取") from exc
    if type(value) is not dict:
        raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "固定样板包顶层必须是 object")
    return value


def _is_nonfunctional_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return normalized.endswith((".md", ".txt")) or name in {".editorconfig", ".gitattributes", ".gitignore"}


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.startswith("tests/") or "/tests/" in normalized or normalized.endswith((".test.ts", ".test.js", "_test.py", ".spec.ts", ".spec.js"))


def _is_conservative_sample(sample: Mapping[str, object]) -> bool:
    """Recognize evidence-limited scenarios from frozen facts, not a hand-set pass bit."""
    git_facts = sample.get("git_facts")
    if not isinstance(git_facts, Mapping):
        return False
    if git_facts.get("evidence_limited") is True:
        return True
    allowed_stage = sample.get("allowed_stage_set")
    if allowed_stage == ["暂时无法确认"]:
        return True
    changed_files = git_facts.get("changed_files")
    if type(changed_files) is list and changed_files and all(type(path) is str for path in changed_files):
        if all(_is_nonfunctional_path(path) for path in changed_files):
            return True
        if bool(git_facts.get("tests_changed")) and all(_is_test_path(path) for path in changed_files):
            return True
    return False


def load_base_sample_pack(path: Path | None = None) -> dict[str, object]:
    target = path or Path(__file__).resolve().parents[3] / "qualification" / "base_samples_v1.json"
    pack = _load_json(target)
    validate_sample_pack(pack)
    result = deepcopy(pack)
    result["sample_manifest_hash"] = _stable_hash(pack)
    return result


def validate_sample_pack(pack: object) -> None:
    if not isinstance(pack, Mapping):
        raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "固定样板包必须是 mapping")
    if pack.get("schema_version") != PACK_SCHEMA_VERSION or pack.get("sample_pack_version") != BASE_PACK_VERSION:
        raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "固定样板包 schema/version 漂移")
    samples = pack.get("samples")
    if type(samples) is not list or len(samples) != EXPECTED_SAMPLE_COUNT or pack.get("minimum_sample_count") != EXPECTED_SAMPLE_COUNT:
        raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "基础固定样板必须 exact 12 项")
    required = {"sample_id", "language_family", "title", "prd", "baseline_commit", "delta_commit", "active_project_profile", "git_facts", "gold_facts", "allowed_stage_set", "allowed_implementation_scope_set", "must_appear", "forbidden_conclusions"}
    ids: set[str] = set()
    counts: Counter[str] = Counter()
    for sample in samples:
        if not isinstance(sample, Mapping) or not required.issubset(sample):
            raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "样板 required fields 不完整")
        sample_id = sample.get("sample_id")
        language = sample.get("language_family")
        if not _non_empty(sample_id) or sample_id in ids or language not in EXPECTED_LANGUAGE_COUNTS:
            raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "样板 identity/language 无效")
        ids.add(str(sample_id)); counts[str(language)] += 1
        baseline = str(sample.get("baseline_commit", "")); delta = str(sample.get("delta_commit", ""))
        if _SHA40_RE.fullmatch(baseline) is None or _SHA40_RE.fullmatch(delta) is None or baseline == delta:
            raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "样板 baseline/delta 必须是不同的冻结 40-char SHA")
        for field in ("allowed_stage_set", "allowed_implementation_scope_set", "must_appear", "forbidden_conclusions"):
            value = sample.get(field)
            if type(value) is not list or not value or any(not _non_empty(item) for item in value):
                raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", f"样板 {field} 无效")
        gold = sample.get("gold_facts")
        if not isinstance(gold, Mapping) or any(not _non_empty(gold.get(field)) for field in ("main_feature", "stage", "implementation_scope")):
            raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "样板 Gold Facts 无效")
    if dict(counts) != EXPECTED_LANGUAGE_COUNTS:
        raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "基础样板语言分布必须为 JS/TS 4 + Java 4 + Python 4")
    sample_by_id = {str(sample["sample_id"]): sample for sample in samples}
    representative_samples = [sample_by_id.get(sample_id) for sample_id in REPEAT_REPRESENTATIVE_SAMPLE_IDS]
    if (
        len(REPEAT_REPRESENTATIVE_SAMPLE_IDS) != 3
        or len(set(REPEAT_REPRESENTATIVE_SAMPLE_IDS)) != 3
        or any(sample is None for sample in representative_samples)
        or {sample["language_family"] for sample in representative_samples if sample is not None} != set(EXPECTED_LANGUAGE_COUNTS)
    ):
        raise QualificationHarnessError(
            "QUALIFICATION_SAMPLE_PACK_INVALID",
            "重复稳定性代表样板必须是固定 3 项且覆盖 JS/TS、Java、Python。",
        )
    if sum(_is_conservative_sample(sample) for sample in samples) < MIN_CONSERVATIVE_PASS:
        raise QualificationHarnessError("QUALIFICATION_SAMPLE_PACK_INVALID", "基础样板必须包含至少 4 个证据不足/保守输出场景")


def _bool_field(observation: Mapping[str, object], field: str) -> bool:
    value = observation.get(field)
    if type(value) is not bool:
        raise QualificationHarnessError("QUALIFICATION_OBSERVATION_INVALID", f"observation.{field} 必须为 strict bool")
    return value


def _normalize_observations(pack: Mapping[str, object], observations: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    samples = pack["samples"]
    if type(samples) is not list or len(observations) != len(samples):
        raise QualificationHarnessError("QUALIFICATION_OBSERVATION_INVALID", "每个基础样板必须且只能有一条 machine observation")
    by_id: dict[str, Mapping[str, object]] = {}
    for item in observations:
        if not isinstance(item, Mapping) or not _non_empty(item.get("sample_id")) or str(item["sample_id"]) in by_id:
            raise QualificationHarnessError("QUALIFICATION_OBSERVATION_INVALID", "observation sample_id 必须 non-empty unique")
        by_id[str(item["sample_id"])] = item
    result: list[dict[str, object]] = []
    for sample in samples:
        sample_id = str(sample["sample_id"]); item = by_id.get(sample_id)
        if item is None:
            raise QualificationHarnessError("QUALIFICATION_OBSERVATION_INVALID", f"缺少 {sample_id} observation")
        hard_failures = item.get("hard_failures")
        if type(hard_failures) is not list or any(not _non_empty(value) for value in hard_failures):
            raise QualificationHarnessError("QUALIFICATION_OBSERVATION_INVALID", "hard_failures 必须为 string list")
        result.append({
            "sample_id": sample_id,
            **{field: _bool_field(item, field) for field in (
                "git_objective_stats_correct", "no_fabrication", "main_feature_correct", "stage_correct",
                "implementation_scope_correct", "conservative_when_evidence_limited", "structural_integrity",
                "source_distinction_correct", "boundary_compliance")},
            "hard_failures": list(hard_failures),
        })
    return result


def _repeat_gate(repeat_runs: object) -> tuple[bool, dict[str, object]]:
    expected_ids = frozenset(REPEAT_REPRESENTATIVE_SAMPLE_IDS)
    if not isinstance(repeat_runs, Mapping):
        return False, {
            "representative_count": 0,
            "representative_ids_match": False,
            "expected_sample_ids": list(REPEAT_REPRESENTATIVE_SAMPLE_IDS),
            "received_sample_ids": [],
            "all_representatives_pass": False,
        }
    received_ids = set(repeat_runs.keys())
    received_text_ids = sorted(str(value) for value in received_ids)
    if (
        len(repeat_runs) != REPEAT_REPRESENTATIVE_COUNT
        or received_ids != expected_ids
        or any(type(value) is not str for value in received_ids)
    ):
        return False, {
            "representative_count": len(repeat_runs),
            "representative_ids_match": False,
            "expected_sample_ids": list(REPEAT_REPRESENTATIVE_SAMPLE_IDS),
            "received_sample_ids": received_text_ids,
            "all_representatives_pass": False,
        }
    details: list[dict[str, object]] = []
    all_pass = True
    for sample_id in REPEAT_REPRESENTATIVE_SAMPLE_IDS:
        runs = repeat_runs[sample_id]
        if type(runs) is not list or len(runs) != REPEAT_RUNS_PER_SAMPLE:
            raise QualificationHarnessError("QUALIFICATION_REPEAT_INVALID", "重复稳定性必须 3 个固定代表样板，每个 exact 3 次")
        triples: list[tuple[str, str, str]] = []
        contradiction_free = True
        for run in runs:
            if not isinstance(run, Mapping) or type(run.get("contradiction")) is not bool:
                raise QualificationHarnessError("QUALIFICATION_REPEAT_INVALID", "repeat run shape 无效")
            values = tuple(run.get(field) for field in ("main_feature", "stage", "implementation_scope"))
            if any(not _non_empty(value) for value in values):
                raise QualificationHarnessError("QUALIFICATION_REPEAT_INVALID", "repeat run 核心字段不完整")
            triples.append((str(values[0]), str(values[1]), str(values[2])))
            contradiction_free = contradiction_free and not bool(run["contradiction"])
        matching = Counter(triples).most_common(1)[0][1]
        passed = matching >= MIN_REPEAT_CONSISTENCY and contradiction_free
        all_pass = all_pass and passed
        details.append({"sample_id": sample_id, "matching_runs": matching, "contradiction_free": contradiction_free, "passed": passed})
    return all_pass, {
        "representative_count": len(repeat_runs),
        "representative_ids_match": True,
        "expected_sample_ids": list(REPEAT_REPRESENTATIVE_SAMPLE_IDS),
        "received_sample_ids": list(REPEAT_REPRESENTATIVE_SAMPLE_IDS),
        "all_representatives_pass": all_pass,
        "details": details,
    }


def _human_gate(readability_reviews: object) -> tuple[bool, dict[str, object]]:
    if type(readability_reviews) is not list or len(readability_reviews) != HUMAN_REVIEWER_COUNT:
        return False, {"state": "human_review_required", "reviewer_count": 0 if type(readability_reviews) is not list else len(readability_reviews)}
    scores: list[float] = []; reviewers: set[str] = set()
    for review in readability_reviews:
        if not isinstance(review, Mapping) or not _non_empty(review.get("reviewer_id")) or str(review["reviewer_id"]) in reviewers:
            raise QualificationHarnessError("QUALIFICATION_HUMAN_REVIEW_INVALID", "真实可读性评审 reviewer_id 必须 non-empty unique")
        score = review.get("score")
        if type(score) not in {int, float} or isinstance(score, bool) or not 1 <= float(score) <= 5:
            raise QualificationHarnessError("QUALIFICATION_HUMAN_REVIEW_INVALID", "真实可读性评分必须在 1..5")
        reviewers.add(str(review["reviewer_id"])); scores.append(float(score))
    mean = sum(scores) / len(scores); passed = mean >= HUMAN_MEAN_MIN and min(scores) >= HUMAN_INDIVIDUAL_MIN
    return passed, {"state": "passed" if passed else "failed", "reviewer_count": len(scores), "mean_score": mean, "minimum_score": min(scores), "scores": scores}


def evaluate_qualification_run(*, observations: Sequence[Mapping[str, object]], repeat_runs: Mapping[str, object], readability_reviews: list[Mapping[str, object]] | None, pack: Mapping[str, object] | None = None) -> dict[str, object]:
    """Evaluate frozen evidence. Missing Human readability always blocks qualification."""
    sample_pack = dict(pack or load_base_sample_pack()); validate_sample_pack(sample_pack)
    normalized = _normalize_observations(sample_pack, observations)
    hard_failures = [{"sample_id": item["sample_id"], "code": code} for item in normalized for code in item["hard_failures"]]
    count_fields = ("git_objective_stats_correct", "no_fabrication", "main_feature_correct", "stage_correct", "implementation_scope_correct", "structural_integrity", "source_distinction_correct", "boundary_compliance")
    counts = {field: sum(bool(item[field]) for item in normalized) for field in count_fields}
    conservative_ids = {str(sample["sample_id"]) for sample in sample_pack["samples"] if isinstance(sample, Mapping) and _is_conservative_sample(sample)}
    conservative_count = sum(bool(item["conservative_when_evidence_limited"]) for item in normalized if item["sample_id"] in conservative_ids)
    repeat_passed, repeat_detail = _repeat_gate(repeat_runs); human_passed, human_detail = _human_gate(readability_reviews)
    metric_gates = {
        "git_objective_stats": counts["git_objective_stats_correct"] == EXPECTED_SAMPLE_COUNT,
        "no_fabrication": counts["no_fabrication"] == EXPECTED_SAMPLE_COUNT,
        "main_feature_mapping": counts["main_feature_correct"] >= MIN_MAIN_FEATURE_PASS,
        "stage": counts["stage_correct"] >= MIN_STAGE_PASS,
        "implementation_scope": counts["implementation_scope_correct"] >= MIN_SCOPE_PASS,
        "conservative_evidence": len(conservative_ids) >= MIN_CONSERVATIVE_PASS and conservative_count >= MIN_CONSERVATIVE_PASS,
        "structural_integrity": counts["structural_integrity"] == EXPECTED_SAMPLE_COUNT,
        "source_distinction": counts["source_distinction_correct"] == EXPECTED_SAMPLE_COUNT,
        "boundary_compliance": counts["boundary_compliance"] == EXPECTED_SAMPLE_COUNT,
        "repeat_stability": repeat_passed,
        "human_readability": human_passed,
    }
    machine_passed = not hard_failures and all(value for key, value in metric_gates.items() if key != "human_readability")
    qualified = machine_passed and human_passed
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "sample_pack_version": sample_pack["sample_pack_version"],
        "sample_manifest_hash": _stable_hash({key: deepcopy(sample_pack[key]) for key in sample_pack if key != "sample_manifest_hash"}),
        "sample_count": EXPECTED_SAMPLE_COUNT,
        "hard_failures": hard_failures,
        "counts": counts,
        "conservative_evidence": {"scenario_count": len(conservative_ids), "passed_count": conservative_count, "pack_requirement_met": len(conservative_ids) >= MIN_CONSERVATIVE_PASS},
        "repeat_stability": repeat_detail,
        "human_readability": human_detail,
        "metric_gates": metric_gates,
        "machine_gate_passed": machine_passed,
        "human_gate_passed": human_passed,
        "qualification_status": "qualified" if qualified else ("human_gate_required" if machine_passed and not human_passed else "not_qualified"),
    }
    result["evidence_manifest_hash"] = _stable_hash(result)
    return result
