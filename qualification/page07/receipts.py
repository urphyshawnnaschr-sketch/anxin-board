"""Inspect a frozen synthetic run; export findings, never admit a model."""
import argparse
import hashlib
import json
from pathlib import Path

from .common import canonical, digest
from .result import evaluate_result
from app.model_qualification_harness import REPEAT_REPRESENTATIVE_SAMPLE_IDS


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evaluate_run(manifest, sidecar, directory, *, expected_manifest_hash):
    # The pin comes from the separately reviewed pre-send checkpoint, not this file.
    if manifest["manifest_hash"] != expected_manifest_hash:
        raise ValueError("FROZEN_MANIFEST_PIN_MISMATCH")
    if digest({k: v for k, v in manifest.items() if k != "manifest_hash"}) != manifest["manifest_hash"]:
        raise ValueError("MANIFEST_HASH_INVALID")
    if manifest["sidecar_hash"] != digest(sidecar):
        raise ValueError("SIDECAR_DRIFT")
    rows = {s["sample_id"]: s for s in sidecar["samples"]}
    expected = [("base-" + sid, sid, "base") for sid in rows]
    expected += [(f"repeat-{sid}-{n}", sid, "repeat") for sid in REPEAT_REPRESENTATIVE_SAMPLE_IDS for n in range(1, 4)]
    actual = [(s["slot_id"], s["sample_id"], s["phase"]) for s in manifest["slots"]]
    if len(rows) != 12 or len(expected) != 21 or actual != expected:
        raise ValueError("SLOT_SET_INVALID")
    if set(manifest["requests"]) != set(rows):
        raise ValueError("REQUEST_SET_INVALID")
    if any(manifest[k] != manifest["capability"][k] for k in ("provider", "model_id", "model_version")):
        raise ValueError("CAPABILITY_IDENTITY_INVALID")
    for slot in manifest["slots"]:
        request = manifest["requests"][slot["sample_id"]]
        plan = request["plan"]
        if (digest(request["wire"]) != request["wire_hash"]
                or request["wire_hash"] != slot["wire_hash"]
                or request["request_hash"] != slot["request_hash"]
                or plan["request_envelope_hash"] != slot["request_hash"]
                or request["wire"]["model"] != manifest["model_id"]
                or request["wire"]["messages"] != plan["messages"]
                or digest(plan["messages"]) != plan["messages_hash"]):
            raise ValueError("REQUEST_WIRE_BINDING_INVALID")
    directory = Path(directory)
    intent = read_json(directory / "intent.json")
    if intent.get("synthetic_diagnostic_only") is True or intent.get("qualification_eligible") is False:
        raise ValueError("DIAGNOSTIC_NOT_QUALIFICATION")
    if intent["manifest_hash"] != manifest["manifest_hash"] or intent["max_attempts"] != 21:
        raise ValueError("RUN_IDENTITY_INVALID")
    assessments, missing, provider_ids = [], [], set()
    for slot in manifest["slots"]:
        path = directory / (slot["slot_id"] + ".receipt.json")
        if not path.exists():
            missing.append(slot["slot_id"])
            continue
        claim = read_json(directory / (slot["slot_id"] + ".claim.json"))
        if claim != slot:
            raise ValueError("CLAIM_IDENTITY_INVALID")
        receipt = read_json(path)
        if receipt.get("synthetic_diagnostic_only") is True or receipt.get("qualification_eligible") is False:
            raise ValueError("DIAGNOSTIC_NOT_QUALIFICATION")
        if digest({k: v for k, v in receipt.items() if k != "response_hash"}) != receipt["response_hash"]:
            raise ValueError("RECEIPT_HASH_INVALID")
        if any(receipt[k] != slot[k] for k in ("slot_id", "sample_id", "phase", "request_hash", "wire_hash")):
            raise ValueError("RECEIPT_IDENTITY_INVALID")
        item = {"slot_id": slot["slot_id"], "response_hash": receipt["response_hash"], "transport_status": receipt["status"]}
        if receipt["status"] == "succeeded":
            response_id = receipt.get("provider_response_id")
            if type(response_id) is not str or not response_id or response_id in provider_ids:
                raise ValueError("PROVIDER_RESPONSE_ID_INVALID_OR_REUSED")
            provider_ids.add(response_id)
            if type(receipt.get("provider_runtime_fingerprint")) is not str or not receipt["provider_runtime_fingerprint"].strip():
                raise ValueError("PROVIDER_FINGERPRINT_MISSING")
            if receipt["actual_model"] != manifest["model_id"]:
                raise ValueError("MODEL_IDENTITY_INVALID")
            usage = receipt["usage"]
            if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
                raise ValueError("USAGE_INVALID")
            if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
                raise ValueError("USAGE_INVALID")
            item["assessment"] = evaluate_result(rows[slot["sample_id"]], receipt["result"])
        assessments.append(item)
    expected_receipts = {s["slot_id"] + ".receipt.json" for s in manifest["slots"]}
    if any(p.name not in expected_receipts for p in directory.glob("*.receipt.json")):
        raise ValueError("UNEXPECTED_RECEIPT")
    complete = not missing and len(assessments) == 21
    structural = complete and all(a.get("assessment", {}).get("local_structural_checks_passed") is True for a in assessments)
    result = {
        "schema_version": "private_regenerate_evaluation_v1", "synthetic_only": True,
        "manifest_hash": manifest["manifest_hash"], "sidecar_hash": manifest["sidecar_hash"],
        "receipt_count": len(assessments), "missing_slots": missing, "assessments": assessments,
        "all_slots_complete": complete, "structural_checks_passed": structural,
        "semantic_review_state": "required", "human_readability_reviews": None,
        "qualification_status": "not_qualified", "automatic_admission": False,
        "note": "Structural checks alone do not satisfy the original semantic metrics, repeat stability or three real Human readability reviews.",
    }
    result["evaluation_hash"] = digest(result)
    return result
