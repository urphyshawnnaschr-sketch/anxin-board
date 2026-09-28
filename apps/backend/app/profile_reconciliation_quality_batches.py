"""Role-aware local retrieval and batching for Phase 3 Quality V1.

Legacy provider-v1 batch identities are intentionally untouched. Quality batches are a
new additive contract that prioritizes exact-HEAD production source per requirement,
uses tests as strengthening evidence, and keeps docs/config/manifests auxiliary.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Mapping, Sequence

from app.context_token_framing import COUNTING_POLICY_VERSION
from app.model_budget_profiles import _validate_limits
from app.profile_reconciliation_batches import BatchError, _bytes, _hash, _terms, _validate_batch
from app.profile_reconciliation_evidence_roles import bind_evidence_role
from app.repository_evidence import validate_technology_evidence

QUALITY_BATCH_SCHEMA_VERSION = "profile_reconciliation_quality_batch_v1"
QUALITY_RETRIEVAL_POLICY = "requirement_role_aware_module_supplement_v3"

_ROLE_WEIGHT = {
    "source_code": 6.0,
    "test_code": 3.0,
    "docs": 0.75,
    "config": 0.4,
    "manifest": 0.4,
    "other": 0.2,
}
_ROLE_LIMIT_PER_REQUIREMENT = {
    "source_code": 3,
    "test_code": 2,
    "docs": 1,
    "config": 1,
    "manifest": 1,
    "other": 0,
}


def _validate_repo_items(repo_items: Sequence[Mapping[str, object]], exact_head: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in repo_items:
        item = deepcopy(dict(raw))
        evidence_id = item.get("evidence_id")
        path = item.get("path")
        content = item.get("content")
        if (
            type(evidence_id) is not str
            or not evidence_id.startswith("repo-code-")
            or evidence_id in seen
            or type(path) is not str
            or not path
            or path.startswith(("/", "\\"))
            or ":" in path
            or "\\" in path
            or any(part in {".", ".."} for part in path.split("/"))
            or type(content) is not str
            or item.get("exact_head") != exact_head
        ):
            raise BatchError("QUALITY_INVALID_REPO_EVIDENCE")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if item.get("content_hash") != digest:
            raise BatchError("QUALITY_EVIDENCE_HASH_MISMATCH")
        try:
            validate_technology_evidence(item)
            role_binding = bind_evidence_role(item)
        except (ValueError, KeyError, TypeError):
            raise BatchError("QUALITY_EVIDENCE_BINDING_INVALID") from None
        item["quality_evidence_role"] = role_binding["role"]
        item["quality_role_binding_id"] = role_binding["role_binding_id"]
        seen.add(evidence_id)
        items.append(item)
    items.sort(key=lambda value: (value["path"], value["evidence_id"]))
    return items


def _requirement_texts(module: Mapping[str, object]) -> list[str]:
    requirements = module.get("requirements", [])
    if isinstance(requirements, list) and requirements and all(type(value) is str for value in requirements):
        values = [value.strip() for value in requirements if value.strip()]
        if values:
            return values
    for key in ("description", "name"):
        value = module.get(key)
        if type(value) is str and value.strip():
            return [value.strip()]
    raise BatchError("QUALITY_REQUIREMENTS_MISSING")


def _identifier_terms(text: str) -> set[str]:
    """Conservative identifier hints, never requirement coverage or semantic proof."""
    terms = {term for term in _terms(text) if re.fullmatch(r"[a-z]{3,}", term)}
    terms -= {"public", "private", "static", "async", "module", "feature", "service", "data", "get", "set", "api", "app", "src", "test", "tests"}
    return {
        term[:-1] if len(term) > 4 and term.endswith("s") and not term.endswith(("ss", "us", "is")) else term
        for term in terms
    }


def _file_diverse(ranked):
    """Prefer one chunk per file before returning to additional chunks of a file."""
    ordered = sorted(ranked, key=lambda pair: (-pair[0], pair[1]["path"], pair[1]["evidence_id"]))
    visits: dict[str, int] = {}
    indexed = []
    for index, value in enumerate(ordered):
        path = value[1]["path"]
        visit = visits.get(path, 0)
        indexed.append((visit, index, value))
        visits[path] = visit + 1
    return [value for _, _, value in sorted(indexed)]


def _rank_for_requirements(
    module: Mapping[str, object],
    items: Sequence[Mapping[str, object]],
) -> tuple[list[tuple[dict[str, object], tuple[int, ...]]], dict[str, object]]:
    """Rank evidence and retain the exact requirement indexes each item matched.

    Evidence is admitted to a requirement only when it overlaps that requirement's
    distinguishing terms (terms not shared by other requirements, with a full-text
    fallback for a single/non-distinguishable requirement). Module-wide/common terms
    may improve ranking after that admission, but cannot create false cross-requirement
    coverage by themselves.

    The requirement-index binding is intentionally kept local to retrieval diagnostics;
    repository evidence identity itself remains the immutable legacy evidence object plus
    the additive role binding. This lets the budget stage prove that source candidates
    actually survived into the final request, not merely that they existed pre-budget.
    """
    item_terms = {
        item["evidence_id"]: _terms(
            str(item["path"]) + " " + str(item["content"]) + " "
            + json.dumps(item.get("structured_metadata", {}), ensure_ascii=False, sort_keys=True)
        )
        for item in items
    }
    frequency: dict[str, int] = {}
    for terms in item_terms.values():
        for term in terms:
            frequency[term] = frequency.get(term, 0) + 1

    selected: dict[str, tuple[float, dict[str, object], set[int]]] = {}
    requirement_texts = _requirement_texts(module)
    requirement_terms = [_terms(text) for text in requirement_texts]
    module_terms = _terms(" ".join(str(module.get(key, "")) for key in ("name", "description")))
    anchor_terms: list[set[str]] = []
    for requirement_index, terms in enumerate(requirement_terms):
        other_terms: set[str] = set()
        for other_index, other in enumerate(requirement_terms):
            if other_index != requirement_index:
                other_terms.update(other)
        distinguishing = set(terms) - other_terms
        anchor_terms.append(distinguishing or set(terms))

    source_matches_by_requirement: list[int] = []
    for requirement_index, terms in enumerate(requirement_terms):
        query = module_terms | terms
        anchors = anchor_terms[requirement_index]
        ranked_by_role: dict[str, list[tuple[float, dict[str, object]]]] = {}
        for item in items:
            terms_for_item = item_terms[item["evidence_id"]]
            if not (anchors & terms_for_item):
                continue
            overlap = query & terms_for_item
            if not overlap:
                continue
            lexical = sum(math.log(1 + (len(items) + 1) / (frequency[term] + 1)) for term in overlap)
            role = str(item["quality_evidence_role"])
            score = lexical * _ROLE_WEIGHT[role]
            ranked_by_role.setdefault(role, []).append((score, item))
        source_matches_by_requirement.append(len(ranked_by_role.get("source_code", [])))
        for role, ranked in ranked_by_role.items():
            limit = _ROLE_LIMIT_PER_REQUIREMENT[role]
            if limit <= 0:
                continue
            ordered = _file_diverse(ranked) if role == "source_code" else sorted(
                ranked, key=lambda pair: (-pair[0], pair[1]["path"], pair[1]["evidence_id"]),
            )
            for score, item in ordered[:limit]:
                old = selected.get(item["evidence_id"])
                if old is None:
                    selected[item["evidence_id"]] = (score, deepcopy(item), {requirement_index})
                else:
                    selected[item["evidence_id"]] = (max(score, old[0]), old[1], old[2] | {requirement_index})

    # Module identifiers bridge non-English requirements and code identifiers without
    # granting any requirement match. Only path stems or source-bound metadata qualify;
    # free text, directory names and metadata labels cannot admit supplemental evidence.
    hints = _identifier_terms(" ".join(str(module.get(key, "")) for key in ("client_id", "name", "description")))
    strong_paths: dict[str, int] = {}
    for item in items:
        if item["quality_evidence_role"] != "source_code":
            continue
        stem = str(item["path"]).rsplit("/", 1)[-1].rsplit(".", 1)[0]
        identifiers = _identifier_terms(stem)
        metadata = item.get("structured_metadata", {})
        if isinstance(metadata, dict):
            for values in metadata.values():
                if isinstance(values, list):
                    for value in values:
                        if type(value) is str and value and value in item["content"]:
                            identifiers.update(_identifier_terms(value))
        overlap = hints & identifiers
        if len(overlap) >= 2:
            strong_paths[item["path"]] = max(strong_paths.get(item["path"], 0), len(overlap))
    supplemental_count = 0
    for item in items:
        if item["quality_evidence_role"] == "source_code" and item["path"] in strong_paths:
            if item["evidence_id"] not in selected:
                selected[item["evidence_id"]] = (float(strong_paths[item["path"]]), deepcopy(item), set())
                supplemental_count += 1

    ranked = sorted(
        selected.values(),
        key=lambda value: (
            0 if value[1]["quality_evidence_role"] == "source_code" else 1 if value[1]["quality_evidence_role"] == "test_code" else 2,
            -len(value[2]),
            -value[0],
            value[1]["path"],
            value[1]["evidence_id"],
        ),
    )
    # Preserve role/requirement ranking within each file, but do not let a large
    # source file exhaust the budget before other admitted source files get a turn.
    visits: dict[str, int] = {}
    diverse = []
    for index, value in enumerate(ranked):
        source = value[1]["quality_evidence_role"] == "source_code"
        path = value[1]["path"]
        visit = visits.get(path, 0) if source else 0
        diverse.append((0 if source else 1, visit, index, value))
        if source:
            visits[path] = visit + 1
    ranked = [value for _, _, _, value in sorted(diverse)]
    role_counts: dict[str, int] = {}
    for _, item, _ in ranked:
        role = str(item["quality_evidence_role"])
        role_counts[role] = role_counts.get(role, 0) + 1
    diagnostics = {
        "requirement_count": len(requirement_texts),
        "requirement_anchor_term_counts": [len(value) for value in anchor_terms],
        "requirements_with_source_matches": sum(count > 0 for count in source_matches_by_requirement),
        "requirements_without_source_matches": sum(count == 0 for count in source_matches_by_requirement),
        "selected_role_counts_before_budget": role_counts,
        "module_supplemental_source_count": supplemental_count,
        "module_supplemental_file_count": len(strong_paths),
    }
    return [
        (item, tuple(sorted(matched_requirement_indexes)))
        for _, item, matched_requirement_indexes in ranked
    ], diagnostics


def plan_quality_reconciliation_batches(
    plan: Mapping[str, object],
    repo_items: Sequence[Mapping[str, object]],
    *,
    exact_head: str,
    plan_profile_id: int,
    budget_record: Mapping[str, object],
    repository_coverage: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Create one full-module, role-aware quality batch per planned module."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", exact_head or ""):
        raise BatchError("INVALID_HEAD")
    if type(plan_profile_id) is not int or plan_profile_id <= 0:
        raise BatchError("INVALID_PLAN_ID")
    if plan.get("schema_version") != "project_profile_v2":
        raise BatchError("INVALID_PLAN")
    modules = plan.get("planned_modules")
    if not isinstance(modules, list) or not modules:
        raise BatchError("INVALID_PLAN")
    ids = [module.get("client_id") for module in modules]
    if any(type(value) is not str or not value for value in ids) or len(ids) != len(set(ids)):
        raise BatchError("INVALID_MODULE_IDENTITIES")
    if budget_record.get("counting_policy_version") != COUNTING_POLICY_VERSION:
        raise BatchError("UNSUPPORTED_COUNTING_POLICY")
    limit = _validate_limits(dict(budget_record))
    items = _validate_repo_items(repo_items, exact_head)

    common = {
        "schema_version": QUALITY_BATCH_SCHEMA_VERSION,
        "exact_head": exact_head,
        "plan_profile_id": plan_profile_id,
        "plan_hash": _hash(plan),
        "counting_policy_version": COUNTING_POLICY_VERSION,
        "max_input_tokens": limit,
        "budget_hash": _hash(budget_record),
        "coverage": {
            "repository_evidence_count": len(items),
            "retrieval_policy": QUALITY_RETRIEVAL_POLICY,
            "coverage_state": "role_aware_requirement_retrieval",
            "repository_coverage": deepcopy(dict(repository_coverage or {})),
        },
        "system_instruction": "Quality V1 uses requirement-level role-aware evidence. Docs/config/manifests are auxiliary and cannot independently prove implementation.",
        "response_schema": {},
        "structure_summary": {
            "file_count": len({item["path"] for item in items}),
            "evidence_count": len(items),
        },
    }
    placeholder = {
        "batch_set_hash": "0" * 64,
        "batch_index": 999999999999,
        "batch_count": 999999999999,
        "all_module_ids": ids,
        "input_hash": "0" * 64,
        "batch_id": "0" * 64,
    }

    def fits(frame: Mapping[str, object]) -> bool:
        return len(_bytes(dict(frame) | placeholder)) <= limit

    batches: list[dict[str, object]] = []
    for module in modules:
        ranked, diagnostics = _rank_for_requirements(module, items)
        frame = deepcopy(common) | {
            "planned_modules": [deepcopy(module)],
            "repo_evidence": [],
            "quality_retrieval": diagnostics,
        }
        if not fits(frame):
            raise BatchError("QUALITY_MODULE_OVER_BUDGET")
        selected: list[dict[str, object]] = []
        selected_source_requirements: set[int] = set()
        omitted: list[dict[str, object]] = []
        for item, matched_requirement_indexes in ranked:
            candidate = frame | {"repo_evidence": selected + [item]}
            if fits(candidate):
                selected.append(item)
                if item["quality_evidence_role"] == "source_code":
                    selected_source_requirements.update(matched_requirement_indexes)
            else:
                omitted.append(
                    {
                        "evidence_id": item["evidence_id"],
                        "role": item["quality_evidence_role"],
                        "matched_requirement_indexes": list(matched_requirement_indexes),
                    }
                )
        frame["repo_evidence"] = selected
        role_counts: dict[str, int] = {}
        for item in selected:
            role = str(item["quality_evidence_role"])
            role_counts[role] = role_counts.get(role, 0) + 1
        requirement_count = int(diagnostics["requirement_count"])
        selected_source_indexes = sorted(selected_source_requirements)
        frame["quality_retrieval"] = frame["quality_retrieval"] | {
            "selected_evidence_count": len(selected),
            "selected_role_counts": role_counts,
            "requirements_with_selected_source": len(selected_source_indexes),
            "requirements_without_selected_source": requirement_count - len(selected_source_indexes),
            "selected_source_requirement_indexes": selected_source_indexes,
            "budget_omitted_count": len(omitted),
            "budget_omitted": omitted,
        }
        batches.append(frame)

    manifest = _hash([_hash(batch) for batch in batches])
    for index, batch in enumerate(batches):
        batch.update(
            batch_set_hash=manifest,
            batch_index=index,
            batch_count=len(batches),
            all_module_ids=ids,
        )
        batch["input_hash"] = _hash(batch)
        batch["batch_id"] = batch["input_hash"]
        _validate_batch(batch)
    return batches
