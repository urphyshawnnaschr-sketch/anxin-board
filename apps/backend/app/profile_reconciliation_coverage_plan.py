"""Pure local coverage supplement; prepared input is never model-reviewed evidence.

Inputs are the safe/redacted repository-map outputs, not raw filesystem paths.
This module has no provider, credential, database, filesystem or network effects.
The returned inventory is transient safe text needed to validate lossless splitting.
Only public_coverage_summary is suitable for public diagnostics.
"""
from copy import deepcopy
import hashlib
import re

from app.context_token_framing import COUNTING_POLICY_VERSION
from app.model_budget_profiles import _validate_limits
from app.profile_reconciliation_batches import BatchError, _bytes, _hash
from app.profile_reconciliation_quality_batches import _validate_repo_items

SCHEMA_VERSION = "profile_reconciliation_coverage_plan_v1"
BATCH_SCHEMA_VERSION = "profile_reconciliation_coverage_batch_v1"


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fragment(item, start, end):
    value = {
        "parent_evidence_id": item["evidence_id"],
        "parent_content_hash": item["content_hash"],
        "object_identity": item.get("object_sha", item.get("object_identity", item.get("object_hash"))),
        "parent_char_start": item.get("char_start", 0),
        "parent_char_end": item.get("char_end", len(item["content"])),
        "path": item["path"], "exact_head": item["exact_head"],
        "char_start": start, "char_end": end,
        "content": item["content"][start:end],
        "content_hash": _digest(item["content"][start:end]),
        "module_association": "unassigned",
    }
    value["fragment_id"] = _hash(value)
    return value


def _frame(identity, modules, fragments, index):
    """Count the complete canonical JSON, including its own count and hash fields."""
    value = {
        "schema_version": BATCH_SCHEMA_VERSION, **identity,
        "batch_index": index, "planned_modules": modules,
        "repository_structure": {"fragment_paths": sorted({f["path"] for f in fragments})},
        "analysis_instruction": "Review safe fragments without presuming module association or implementation status.",
        "fragments": fragments, "batch_id": "0" * 64, "input_upper_bound": 0,
    }
    while value["input_upper_bound"] != len(_bytes(value)):
        value["input_upper_bound"] = len(_bytes(value))
    value["batch_id"] = _hash({key: data for key, data in value.items() if key != "batch_id"})
    return value


def plan_coverage_batches(plan, repo_items, *, exact_head, plan_profile_id,
                          budget_record, selected_evidence_ids=()):
    """Cover every safe input omitted by module retrieval, splitting by actual budget.

    Selected IDs mean only that an existing local plan includes those inputs. They
    do not mean a provider read them, and do not grant authorization to dispatch.
    The catalog is never truncated; an over-budget catalog fails explicitly.
    """
    if type(exact_head) is not str or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", exact_head):
        raise BatchError("COVERAGE_INVALID_HEAD")
    if type(plan_profile_id) is not int or plan_profile_id <= 0:
        raise BatchError("COVERAGE_INVALID_PLAN_ID")
    if not isinstance(plan, dict) or plan.get("schema_version") != "project_profile_v2":
        raise BatchError("COVERAGE_INVALID_PLAN")
    modules = deepcopy(plan.get("planned_modules"))
    if not isinstance(modules, list) or not modules or any(not isinstance(m, dict) for m in modules):
        raise BatchError("COVERAGE_INVALID_MODULES")
    ids = [m.get("client_id") for m in modules]
    if any(type(i) is not str or not i for i in ids) or len(ids) != len(set(ids)):
        raise BatchError("COVERAGE_INVALID_MODULES")
    if any(type(m.get("name")) is not str or not isinstance(m.get("requirements"), list)
           or any(type(r) is not str for r in m["requirements"]) for m in modules):
        raise BatchError("COVERAGE_INVALID_MODULES")
    budget = deepcopy(dict(budget_record))
    if budget.get("counting_policy_version") != COUNTING_POLICY_VERSION:
        raise BatchError("COVERAGE_UNSUPPORTED_COUNTING_POLICY")
    try:
        limit = _validate_limits(budget)
    except (ValueError, KeyError, AssertionError, TypeError):
        raise BatchError("COVERAGE_INVALID_BUDGET") from None
    items = _validate_repo_items(repo_items, exact_head)
    supplied = list(selected_evidence_ids)
    if any(type(i) is not str for i in supplied):
        raise BatchError("COVERAGE_INVALID_SELECTION")
    selected = sorted(set(supplied))
    item_ids = {item["evidence_id"] for item in items}
    if not set(selected).issubset(item_ids):
        raise BatchError("COVERAGE_INVALID_SELECTION")
    remaining = [item for item in items if item["evidence_id"] not in selected]
    identity = dict(exact_head=exact_head, plan_profile_id=plan_profile_id,
                    plan_hash=_hash(plan), budget_hash=_hash(budget),
                    inventory_hash=_hash(items), selection_hash=_hash(selected))
    batches = []
    pending = []
    for item in remaining:
        start = 0
        length = len(item["content"])
        # Empty safe text still has an inventory identity and a zero-length fragment.
        while start < length or (length == 0 and start == 0):
            whole = _fragment(item, start, length)
            candidate = _frame(identity, modules, pending + [whole], len(batches))
            if candidate["input_upper_bound"] <= limit:
                pending.append(whole)
                break
            if pending:
                batches.append(_frame(identity, modules, pending, len(batches)))
                pending = []
                continue
            low, high = start, length
            while low < high:
                mid = (low + high + 1) // 2
                candidate = _frame(identity, modules, [_fragment(item, start, mid)], len(batches))
                if candidate["input_upper_bound"] <= limit:
                    low = mid
                else:
                    high = mid - 1
            if low == start:
                raise BatchError("COVERAGE_FRAME_OVER_BUDGET")
            batches.append(_frame(identity, modules, [_fragment(item, start, low)], len(batches)))
            start = low
    if pending:
        batches.append(_frame(identity, modules, pending, len(batches)))
    value = {
        "schema_version": SCHEMA_VERSION, **identity,
        "plan": deepcopy(plan), "budget_record": budget, "repo_inventory": items,
        "selected_evidence_ids": selected,
        "selection_state": "planned_only_not_provider_reviewed",
        "provider_calls": 0, "network_model_calls": 0,
        "model_analysis_complete": False, "provider_dispatch_supported": False,
        "batches": batches,
        "counts": {"inventory_evidence": len(items), "selected_evidence": len(selected),
                   "remaining_evidence": len(remaining), "planned_modules": len(modules),
                   "fragment_count": sum(len(b["fragments"]) for b in batches),
                   "batch_count": len(batches)},
    }
    value["coverage_plan_hash"] = _hash(value)
    return value


def validate_coverage_plan(plan):
    """Rebuild all identities, slices and budgets; hashes alone cannot hide omissions.

    The caller must additionally compare exact_head/plan_hash/inventory_hash with
    its current authoritative inputs before using a prepared plan.
    """
    try:
        expected = plan_coverage_batches(
            plan["plan"], plan["repo_inventory"], exact_head=plan["exact_head"],
            plan_profile_id=plan["plan_profile_id"], budget_record=plan["budget_record"],
            selected_evidence_ids=plan["selected_evidence_ids"],
        )
        if _bytes(expected) != _bytes(plan):
            raise BatchError("COVERAGE_PLAN_INVALID")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise BatchError("COVERAGE_PLAN_INVALID") from None


def public_coverage_summary(plan):
    """Safe counts/identities only: no paths, source fragments or PRD text."""
    validate_coverage_plan(plan)
    return {key: deepcopy(plan[key]) for key in (
        "schema_version", "exact_head", "plan_profile_id", "coverage_plan_hash", "counts",
        "plan_hash", "inventory_hash", "selection_hash", "budget_hash",
        "provider_calls", "network_model_calls", "model_analysis_complete", "provider_dispatch_supported",
    )} | {"credential_read": False, "candidate_ready": False,
          "budget_scope": "local_canonical_json_not_provider_wire_request",
          "batches": [{key: b[key] for key in ("batch_index", "batch_id", "input_upper_bound")}
                     for b in plan["batches"]]}
