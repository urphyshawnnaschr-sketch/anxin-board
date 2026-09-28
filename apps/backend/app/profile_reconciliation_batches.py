"""Full-code exact-HEAD reconciliation batching and deterministic aggregation.

Every safe repository evidence fragment is model-visible for every planned-module group.
No lexical retrieval is allowed to omit code before provider analysis.
"""
from copy import deepcopy
import hashlib
import json
import re

from app.context_token_framing import COUNTING_POLICY_VERSION
from app.model_budget_profiles import _validate_limits
from app.repository_evidence import validate_technology_evidence


MAX_MODULES_PER_ANALYSIS_BATCH = 3


class BatchError(ValueError):
    pass


class BatchFailure(Exception):
    """Explicit fake-dispatch result. Any unclassified exception is ambiguous."""
    def __init__(self, status):
        if status not in {"failed_pre_send", "failed_after_send", "unknown"}:
            raise BatchError("INVALID_FAILURE_CLASSIFICATION")
        self.status = status


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _terms(text):
    """Legacy compatibility helper; full-baseline planning no longer uses lexical retrieval."""
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    words = set(re.findall(r"[a-z][a-z0-9]{2,}", expanded.lower()))
    words -= {"the", "and", "with", "for", "from", "return", "class", "def", "function", "true", "false", "null"}
    for sequence in re.findall(r"[\u4e00-\u9fff]+", expanded):
        words.update(sequence[i:i + 2] for i in range(len(sequence) - 1))
    return words


def _normalize_repo_items(repo_items, *, exact_head):
    items = []
    seen = set()
    for item in repo_items:
        evidence_id, path, content = item.get("evidence_id"), item.get("path"), item.get("content")
        if (not isinstance(evidence_id, str) or not evidence_id.startswith("repo-code-")
                or evidence_id in seen or not isinstance(content, str) or not isinstance(path, str)
                or not path or path.startswith(("/", "\\")) or ":" in path or "\\" in path
                or any(part in {".", ".."} for part in path.split("/"))
                or item.get("exact_head") != exact_head):
            raise BatchError("INVALID_REPO_EVIDENCE")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if item.get("content_hash") != content_hash:
            raise BatchError("EVIDENCE_HASH_MISMATCH")
        try:
            validate_technology_evidence(item)
        except (ValueError, KeyError, TypeError):
            raise BatchError("TECHNOLOGY_EVIDENCE_BINDING_INVALID") from None
        seen.add(evidence_id)
        items.append({
            "evidence_id": evidence_id, "path": path, "content": content,
            "content_hash": content_hash, "exact_head": exact_head,
            **{key: item[key] for key in (
                "file_hash", "source_hash", "object_sha", "line_start", "line_end", "char_start", "char_end",
                "evidence_schema_version", "adapter_id", "parser_status", "failed_adapter_id", "structured_metadata",
                "technology_evidence", "terms", "language_hint", "detector_version", "confidence", "semantic_coverage"
            ) if key in item}
        })
    items.sort(key=lambda value: (value["path"], value["evidence_id"]))
    return items


def plan_reconciliation_batches(plan, repo_items, *, exact_head, plan_profile_id, budget_record, repository_coverage=None, framed_byte_limit=None):
    """Build repository-centered batches with complete safe-code model coverage.

    Every safe evidence fragment is presented to every planned-module group. In the
    common case the full module catalog fits one group, so each code fragment is sent
    exactly once. If the plan itself must be split for budget, code is deliberately
    repeated across plan groups rather than omitted.
    """
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", exact_head or ""):
        raise BatchError("INVALID_HEAD")
    if type(plan_profile_id) is not int or plan_profile_id <= 0:
        raise BatchError("INVALID_PLAN_ID")
    if plan.get("schema_version") != "project_profile_v2":
        raise BatchError("INVALID_PLAN")
    modules = plan.get("planned_modules", [])
    ids = [module.get("client_id") for module in modules]
    if not ids or any(not isinstance(identity, str) or not identity for identity in ids) or len(ids) != len(set(ids)):
        raise BatchError("INVALID_MODULE_IDENTITIES")
    if budget_record.get("counting_policy_version") != COUNTING_POLICY_VERSION:
        raise BatchError("UNSUPPORTED_COUNTING_POLICY")
    budget_limit = _validate_limits(budget_record)
    if framed_byte_limit is None:
        limit = budget_limit
    elif type(framed_byte_limit) is not int or not 0 < framed_byte_limit <= budget_limit:
        raise BatchError("INVALID_FRAMED_BYTE_LIMIT")
    else:
        limit = framed_byte_limit
    items = _normalize_repo_items(repo_items, exact_head=exact_head)
    coverage = {
        "repository_evidence_count": len(items),
        "assigned_evidence_count": len(items),
        "unassigned_evidence_count": 0,
        "modules_without_matches": 0,
        "retrieval_policy": "full_safe_evidence_coverage_v1",
        "coverage_state": "full_safe_evidence_covered",
        "repository_coverage": deepcopy(repository_coverage or {}),
    }
    base = {
        "schema_version": "profile_reconciliation_batch_v1",
        "exact_head": exact_head,
        "plan_profile_id": plan_profile_id,
        "plan_hash": _hash(plan),
        "counting_policy_version": COUNTING_POLICY_VERSION,
        "max_input_tokens": limit,
        "budget_hash": _hash(budget_record),
        "coverage": coverage,
        "system_instruction": "Assess supplied exact-HEAD code against supplied planned modules. All safe repository evidence is covered across the complete batch set; do not infer absence from one fragment.",
        "response_schema": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["planned_module_id", "status", "evidence_ids", "rationale"], "properties": {
                "planned_module_id": {"type": "string"}, "status": {"enum": ["partial", "implemented", "unknown"]},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string", "minLength": 1, "maxLength": 2000}}}},
        "structure_summary": {"file_count": len({item["path"] for item in items}), "evidence_count": len(items)},
    }
    placeholder = dict(batch_set_hash="0" * 64, batch_index=999999999999, batch_count=999999999999,
                       all_module_ids=ids, input_hash="0" * 64, batch_id="0" * 64)
    def frame(module_group, evidence_group):
        return {**base, "planned_modules": deepcopy(module_group), "repo_evidence": deepcopy(evidence_group)}
    def fits(value):
        return len(_bytes({**value, **placeholder})) <= limit
    # Plan text must never consume the whole frame: reserve enough room for at
    # least one real code-evidence fragment. This keeps oversized Chinese PRD
    # modules splittable without producing code-free model calls.
    plan_evidence_reserve = min(8192, max(2048, limit // 3)) if items else 0
    def plan_fits(value):
        return len(_bytes({**value, **placeholder})) <= limit - plan_evidence_reserve

    def split_module(module):
        if plan_fits(frame([module], [])):
            return [deepcopy(module)]
        raw = _bytes(module).decode("utf-8")
        template = {"client_id": module["client_id"], "plan_module_hash": _hash(module), "plan_fragment": "",
                    "fragment_char_start": 0, "fragment_char_end": 0}
        fragments = []
        offset = 0
        module_hash = _hash(module)
        while offset < len(raw):
            low, high, best = 1, len(raw) - offset, 0
            while low <= high:
                size = (low + high) // 2
                fragment = {"client_id": module["client_id"], "plan_module_hash": module_hash,
                            "plan_fragment": raw[offset:offset + size], "fragment_char_start": offset,
                            "fragment_char_end": offset + size}
                if plan_fits(frame([fragment], [])):
                    best = size; low = size + 1
                else:
                    high = size - 1
            if best <= 0:
                raise BatchError("BUDGET_CANNOT_FIT_IDENTITY_FRAME")
            fragments.append({"client_id": module["client_id"], "plan_module_hash": module_hash,
                              "plan_fragment": raw[offset:offset + best], "fragment_char_start": offset,
                              "fragment_char_end": offset + best})
            offset += best
        return fragments

    module_fragments = [fragment for module in modules for fragment in split_module(module)]
    module_groups, current = [], []
    for fragment in module_fragments:
        candidate = current + [fragment]
        duplicate_identity = any(item["client_id"] == fragment["client_id"] for item in current)
        if current and (duplicate_identity or len({item["client_id"] for item in current}) >= MAX_MODULES_PER_ANALYSIS_BATCH or not plan_fits(frame(candidate, []))):
            module_groups.append(current); current = []; candidate = [fragment]
        if not plan_fits(frame(candidate, [])):
            raise BatchError("BUDGET_CANNOT_FIT_IDENTITY_FRAME")
        current = candidate
    if current:
        module_groups.append(current)

    def split_item(item, module_group, start=None):
        if start is None:
            start = item.get("char_start", 0)
        if fits(frame(module_group, [item])):
            return [item]
        text = item["content"]
        if len(text) < 2:
            raise BatchError("BUDGET_CANNOT_FIT_EVIDENCE_FRAME")
        middle = len(text) // 2
        newline = text.rfind("\n", 0, middle)
        if newline > 0:
            middle = newline + 1
        pieces = []
        for offset, part in ((0, text[:middle]), (middle, text[middle:])):
            source_id = item.get("parent_evidence_id", item["evidence_id"])
            digest = hashlib.sha256(part.encode("utf-8")).hexdigest()
            line = item.get("line_start", 1) + text[:offset].count("\n")
            fragment = {**item, "evidence_id": "repo-code-" + _hash([source_id, start + offset, digest]),
                        "parent_evidence_id": source_id,
                        "parent_content_hash": item.get("parent_content_hash", item["content_hash"]),
                        "content": part, "content_hash": digest, "char_start": start + offset,
                        "char_end": start + offset + len(part), "line_start": line,
                        "line_end": line + part.count("\n") - int(part.endswith("\n"))}
            if "structured_metadata" in fragment:
                fragment["structured_metadata"] = {k: [v for v in values if v in part] for k, values in fragment["structured_metadata"].items()}
            if "terms" in fragment:
                fragment["terms"] = [term for term in fragment["terms"] if term in part.casefold()]
            if "technology_evidence" in fragment:
                fragment["technology_evidence"] = [record for record in fragment["technology_evidence"]
                    if fragment["char_start"] <= record["char_start"] < record["char_end"] <= fragment["char_end"]
                    and all(not record.get(k) or record[k] in part for k in ("symbol", "related_symbol", "related_path"))]
            pieces.extend(split_item(fragment, module_group, start + offset))
        return pieces

    batches = []
    for module_group in module_groups:
        if not items:
            batches.append(frame(module_group, [])); continue
        fragments = [fragment for item in items for fragment in split_item(item, module_group)]
        chunk = []
        for item in fragments:
            if chunk and not fits(frame(module_group, chunk + [item])):
                batches.append(frame(module_group, chunk)); chunk = []
            if not fits(frame(module_group, [item])):
                raise BatchError("BUDGET_CANNOT_FIT_EVIDENCE_FRAME")
            chunk.append(item)
        if chunk:
            batches.append(frame(module_group, chunk))

    manifest = _hash([_hash(batch) for batch in batches])
    for index, batch in enumerate(batches):
        batch.update(batch_set_hash=manifest, batch_index=index, batch_count=len(batches), all_module_ids=ids)
        batch["input_hash"] = _hash(batch); batch["batch_id"] = batch["input_hash"]
        _validate_batch(batch)
    return batches


def _validate_batch(batch):
    if batch.get("schema_version") not in {"profile_reconciliation_batch_v1", "profile_reconciliation_quality_batch_v1"}:
        raise BatchError("UNSUPPORTED_BATCH_SCHEMA")
    framed = {key: value for key, value in batch.items() if key not in {"input_hash", "batch_id"}}
    if batch.get("input_hash") != _hash(framed) or batch.get("batch_id") != batch["input_hash"]:
        raise BatchError("BATCH_IDENTITY_MISMATCH")
    if len(_bytes(batch)) > batch["max_input_tokens"]:
        raise BatchError("BATCH_EXCEEDS_BUDGET")


def _schema(conn):
    if conn.in_transaction:
        raise BatchError("CONNECTION_TRANSACTION_ACTIVE")
    conn.execute("""CREATE TABLE IF NOT EXISTS profile_reconciliation_batches (
        batch_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, exact_head TEXT NOT NULL,
        plan_profile_id INTEGER NOT NULL, plan_hash TEXT NOT NULL, module_ids_json TEXT NOT NULL,
        evidence_hash TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
        result_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()


def _validated_result(batch, result):
    expected = {module["client_id"] for module in batch["planned_modules"]}
    evidence = {item["evidence_id"]: item for item in batch["repo_evidence"]}
    if not isinstance(result, list) or len(result) != len(expected):
        raise BatchError("INVALID_BATCH_RESULT")
    seen, clean = set(), []
    for mapping in result:
        if not isinstance(mapping, dict) or set(mapping) != {"planned_module_id", "status", "evidence_ids", "rationale"}:
            raise BatchError("INVALID_BATCH_RESULT")
        identity, status, refs, rationale = mapping["planned_module_id"], mapping["status"], mapping["evidence_ids"], mapping["rationale"]
        if identity not in expected or identity in seen or status not in {"partial", "implemented", "unknown"}:
            raise BatchError("INVALID_BATCH_RESULT")
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in evidence for ref in refs):
            raise BatchError("CROSS_BATCH_EVIDENCE")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale.strip()) > 2000:
            raise BatchError("INVALID_BATCH_RATIONALE")
        if status != "unknown" and not refs:
            raise BatchError("IMPLEMENTATION_WITHOUT_EVIDENCE")
        seen.add(identity)
        clean.append({"planned_module_id": identity, "status": status, "evidence_ids": sorted(set(refs)), "rationale": rationale.strip()})
    return clean


def run_batch(conn, batch, *, fake_dispatch, current_head, retry_failed=False):
    """Durably claim before a caller-supplied fake callback; never retry ambiguity."""
    _validate_batch(batch)
    if current_head() != batch["exact_head"]:
        raise BatchError("HEAD_CHANGED")
    _schema(conn); conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status FROM profile_reconciliation_batches WHERE batch_id = ?", (batch["batch_id"],)).fetchone()
        if row is not None:
            status = row[0]
            if status in {"claimed", "unknown", "succeeded"} or not retry_failed:
                conn.commit(); return {"status": "unknown" if status == "claimed" else status, "batch_id": batch["batch_id"]}
            if status not in {"failed_pre_send", "failed_after_send"}:
                raise BatchError("INVALID_DURABLE_STATUS")
            conn.execute("UPDATE profile_reconciliation_batches SET status='claimed', attempts=attempts+1, updated_at=CURRENT_TIMESTAMP WHERE batch_id=?", (batch["batch_id"],))
        else:
            conn.execute("""INSERT INTO profile_reconciliation_batches
                (batch_id,input_hash,exact_head,plan_profile_id,plan_hash,module_ids_json,evidence_hash,status)
                VALUES (?,?,?,?,?,?,?,'claimed')""", (batch["batch_id"], batch["input_hash"], batch["exact_head"], batch["plan_profile_id"], batch["plan_hash"],
                _bytes([module["client_id"] for module in batch["planned_modules"]]).decode(),
                _hash([{k: v for k, v in item.items() if k != "content"} for item in batch["repo_evidence"]])))
        conn.commit()
    except BaseException:
        conn.rollback(); raise
    result = None
    try:
        if current_head() != batch["exact_head"]: raise BatchFailure("failed_pre_send")
        returned = fake_dispatch(deepcopy(batch))
        if current_head() != batch["exact_head"]: raise BatchFailure("unknown")
    except BatchFailure as exc:
        status = exc.status
    except Exception:
        status = "unknown"
    else:
        try:
            result = _validated_result(batch, returned); status = "succeeded"
        except (BatchError, TypeError, ValueError):
            status = "failed_after_send"
    conn.execute("UPDATE profile_reconciliation_batches SET status=?,result_json=?,updated_at=CURRENT_TIMESTAMP WHERE batch_id=? AND status='claimed'",
                 (status, _bytes(result).decode() if result is not None else None, batch["batch_id"]))
    conn.commit(); return {"status": status, "batch_id": batch["batch_id"]}


def aggregate_batches(conn, batches, *, current_head):
    """Aggregate full-code coverage; positive evidence survives unrelated unknown fragments."""
    if not batches or len({batch["batch_id"] for batch in batches}) != len(batches):
        raise BatchError("INVALID_BATCH_SET")
    if current_head() != batches[0]["exact_head"]:
        raise BatchError("HEAD_CHANGED")
    if any(batch.get("batch_count") != len(batches) for batch in batches) or sorted(batch.get("batch_index") for batch in batches) != list(range(len(batches))):
        raise BatchError("INCOMPLETE_BATCH_SET")
    ordered = sorted(batches, key=lambda batch: batch["batch_index"])
    manifest = _hash([_hash({k: v for k, v in batch.items() if k not in {"batch_set_hash", "batch_index", "batch_count", "all_module_ids", "input_hash", "batch_id"}}) for batch in ordered])
    if any(batch["batch_set_hash"] != manifest for batch in batches):
        raise BatchError("BATCH_SET_IDENTITY_MISMATCH")
    identities = {(batch["exact_head"], batch["plan_profile_id"], batch["plan_hash"], batch["budget_hash"]) for batch in batches}
    if len(identities) != 1:
        raise BatchError("MIXED_BATCH_IDENTITY")
    expected_all = set(batches[0]["all_module_ids"])
    collected = {identity: [] for identity in expected_all}
    for batch in batches:
        _validate_batch(batch)
        row = conn.execute("SELECT status,result_json FROM profile_reconciliation_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()
        if row is None or row[0] != "succeeded":
            raise BatchError("BATCHES_NOT_COMPLETE")
        for mapping in _validated_result(batch, json.loads(row[1])):
            collected.setdefault(mapping["planned_module_id"], []).append(mapping)
    if set(collected) != expected_all or any(not values for values in collected.values()):
        raise BatchError("MODULE_COVERAGE_INCOMPLETE")
    aggregated = []
    for identity in batches[0]["all_module_ids"]:
        positive = [item for item in collected[identity] if item["status"] in {"partial", "implemented"} and item["evidence_ids"]]
        refs = sorted({ref for item in positive for ref in item["evidence_ids"]})
        rationale_source = positive if refs else [item for item in collected[identity] if item["status"] == "unknown"]
        rationales = list(dict.fromkeys(item["rationale"] for item in rationale_source if item.get("rationale")))
        rationale = "；".join(rationales)[:2000] or "模型没有返回可核对的分析理由。"
        aggregated.append({"planned_module_id": identity, "status": "partial" if refs else "unknown",
                           "evidence_ids": refs, "exact_head": batches[0]["exact_head"], "rationale": rationale})
    if current_head() != batches[0]["exact_head"]:
        raise BatchError("HEAD_CHANGED")
    return aggregated
