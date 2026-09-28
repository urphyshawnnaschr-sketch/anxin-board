import hashlib
import json
import pytest

from app.profile_reconciliation_batches import BatchError, plan_reconciliation_batches
from app.profile_reconciliation_quality import (
    COMPLETENESS_SCHEMA_VERSION,
    QUALITY_SCHEMA_VERSION,
    build_quality_contract,
    build_quality_messages,
    validate_quality_result,
    verify_requirement_completeness,
)

HEAD = "a" * 40
BUDGET = {
    "context_window_tokens": 200000,
    "max_output_tokens": 8000,
    "reserved_output_tokens": 8000,
    "safety_margin_tokens": 16000,
    "counting_policy_version": "utf8_byte_upper_bound_v1",
}
PLAN = {
    "schema_version": "project_profile_v2",
    "project_summary": "quality fixture",
    "planned_modules": [
        {
            "client_id": "FR-03-prd-preview",
            "name": "PRD preview",
            "description": "Parse, preview and confirm PRD versions.",
            "requirements": [
                "Parse md/txt/docx/PDF and expose a preview.",
                "Only a human-confirmed parsed version becomes current.",
            ],
            "prd_refs": ["prd-fr03"],
            "exclusions": [],
        }
    ],
    "implementation_mappings": [],
    "unplanned_code_features": [],
    "domain_glossary": [],
    "exclude_patterns": [],
    "notes": "",
}


def evidence(identity, path, text):
    return {
        "evidence_id": "repo-code-" + identity,
        "path": path,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "exact_head": HEAD,
    }


def batch():
    batches = plan_reconciliation_batches(
        PLAN,
        [
            evidence("parser", "apps/backend/app/prd_parser.py", "parse md txt docx pdf preview"),
            evidence("confirm", "apps/backend/app/prd.py", "parsed preview confirm parse_confirmed human"),
        ],
        exact_head=HEAD,
        plan_profile_id=1,
        budget_record=BUDGET,
    )
    assert len(batches) == 1
    return batches[0]


def good_result(b):
    contract = build_quality_contract(b)
    reqs = contract["modules"][0]["requirements"]
    refs = [item["evidence_id"] for item in b["repo_evidence"]]
    return {
        "mappings": [
            {
                "planned_module_id": "FR-03-prd-preview",
                "module_summary": "The supplied exact-HEAD code supports both requested behaviors.",
                "requirement_results": [
                    {
                        "requirement_id": reqs[0]["requirement_id"],
                        "state": "supported",
                        "evidence_ids": [refs[0]],
                        "rationale": "The parser evidence explicitly handles the required document formats and preview path.",
                        "gap": "",
                    },
                    {
                        "requirement_id": reqs[1]["requirement_id"],
                        "state": "supported",
                        "evidence_ids": [refs[1]],
                        "rationale": "The confirmation evidence binds the current version to an explicit parsed-to-confirmed transition.",
                        "gap": "",
                    },
                ],
            }
        ]
    }


def one_requirement_batch(items):
    plan = json.loads(json.dumps(PLAN))
    plan["planned_modules"][0]["requirements"] = ["Parse preview confirm"]
    plan["planned_modules"][0]["description"] = "Parse preview confirm"
    batches = plan_reconciliation_batches(
        plan,
        items,
        exact_head=HEAD,
        plan_profile_id=1,
        budget_record=BUDGET,
    )
    assert len(batches) == 1
    return batches[0]


def one_requirement_result(b, refs, *, state="supported", gap=""):
    req = build_quality_contract(b)["modules"][0]["requirements"][0]
    return {
        "mappings": [{
            "planned_module_id": "FR-03-prd-preview",
            "module_summary": "Bounded exact-HEAD assessment.",
            "requirement_results": [{
                "requirement_id": req["requirement_id"],
                "state": state,
                "evidence_ids": list(refs),
                "rationale": "The cited evidence was assessed only within this batch.",
                "gap": gap,
            }],
        }]
    }


def test_contract_has_deterministic_requirement_identities_and_no_module_implemented_field():
    b = batch()
    first = build_quality_contract(b)
    second = build_quality_contract(b)
    assert first == second
    assert first["schema_version"] == QUALITY_SCHEMA_VERSION
    reqs = first["modules"][0]["requirements"]
    assert len(reqs) == 2 and len({item["requirement_id"] for item in reqs}) == 2
    assert {item["role"] for item in first["evidence_roles"]} == {"source_code"}
    assert all(item["role_binding_id"].startswith("evidence-role-") for item in first["evidence_roles"])
    schema_text = json.dumps(first["response_schema"], sort_keys=True)
    assert "rationale" in schema_text and "gap" in schema_text
    assert "implemented" not in schema_text and "not_started" not in schema_text


def test_quality_messages_are_requirement_level_and_do_not_grant_completeness_authority():
    messages = build_quality_messages(batch())
    assert len(messages) == 2
    assert "do not declare the whole module implemented" in messages[0]["content"]
    assert "docs/config/manifest/other cannot by themselves establish implementation" in messages[0]["content"]
    user = json.loads(messages[1]["content"])
    assert len(user["quality_requirements"][0]["requirements"]) == 2
    assert user["quality_evidence_roles"]
    assert "required_output" in user


def test_all_requirements_supported_is_still_formally_partial_until_completeness_verifier():
    b = batch()
    validated = validate_quality_result(b, good_result(b))
    mapping = validated["mappings"][0]
    assert mapping["support_state"] == "all_requirements_supported"
    assert mapping["formal_status"] == "partial"
    assert mapping["evidence_ids"]
    assert len(validated["validated_quality_hash"]) == 64
    completeness = verify_requirement_completeness(b, validated)
    assert completeness["schema_version"] == COMPLETENESS_SCHEMA_VERSION
    assert completeness["mappings"][0]["status"] == "implemented"
    assert completeness["mappings"][0]["source_backed_requirement_count"] == 2
    assert len(completeness["completeness_hash"]) == 64


def test_every_requirement_must_be_answered_exactly_once():
    b = batch()
    result = good_result(b)
    result["mappings"][0]["requirement_results"].pop()
    with pytest.raises(BatchError, match="QUALITY_REQUIREMENT_COVERAGE_INVALID"):
        validate_quality_result(b, result)

    result = good_result(b)
    result["mappings"][0]["requirement_results"][1]["requirement_id"] = result["mappings"][0]["requirement_results"][0]["requirement_id"]
    with pytest.raises(BatchError, match="QUALITY_REQUIREMENT_COVERAGE_INVALID"):
        validate_quality_result(b, result)


def test_supported_and_partial_require_cited_batch_evidence():
    b = batch()
    result = good_result(b)
    result["mappings"][0]["requirement_results"][0]["evidence_ids"] = []
    with pytest.raises(BatchError, match="QUALITY_SUPPORT_WITHOUT_EVIDENCE"):
        validate_quality_result(b, result)

    result = good_result(b)
    result["mappings"][0]["requirement_results"][0]["evidence_ids"] = ["repo-code-not-in-batch"]
    with pytest.raises(BatchError, match="QUALITY_CROSS_BATCH_EVIDENCE"):
        validate_quality_result(b, result)


def test_partial_and_unknown_require_explicit_gap_and_rationale():
    b = batch()
    result = good_result(b)
    item = result["mappings"][0]["requirement_results"][0]
    item.update(state="partial", gap="", rationale="Some implementation is visible.")
    with pytest.raises(BatchError, match="QUALITY_GAP_REQUIRED"):
        validate_quality_result(b, result)

    result = good_result(b)
    item = result["mappings"][0]["requirement_results"][0]
    item.update(state="unknown", evidence_ids=[], gap="Need evidence for format parsing.", rationale="")
    with pytest.raises(BatchError, match="QUALITY_RATIONALE_INVALID"):
        validate_quality_result(b, result)


def test_supported_must_not_claim_a_gap():
    b = batch()
    result = good_result(b)
    result["mappings"][0]["requirement_results"][0]["gap"] = "Contradictory missing work"
    with pytest.raises(BatchError, match="QUALITY_SUPPORTED_WITH_GAP"):
        validate_quality_result(b, result)


def test_unknown_is_conservative_not_not_started():
    b = batch()
    contract = build_quality_contract(b)
    reqs = contract["modules"][0]["requirements"]
    result = {
        "mappings": [{
            "planned_module_id": "FR-03-prd-preview",
            "module_summary": "The supplied evidence is insufficient for either requirement.",
            "requirement_results": [
                {
                    "requirement_id": req["requirement_id"],
                    "state": "unknown",
                    "evidence_ids": [],
                    "rationale": "The available batch does not establish this requirement.",
                    "gap": "Need exact implementation evidence for this requirement.",
                }
                for req in reqs
            ],
        }]
    }
    validated = validate_quality_result(b, result)
    assert validated["mappings"][0]["support_state"] == "unknown"
    assert validated["mappings"][0]["formal_status"] == "unknown"
    assert verify_requirement_completeness(b, validated)["mappings"][0]["status"] == "unknown"


def test_plan_fragment_is_rejected_instead_of_pretending_requirement_completeness():
    b = batch()
    b["planned_modules"][0] = {
        "client_id": "FR-03-prd-preview",
        "plan_module_hash": "f" * 64,
        "plan_fragment": "serialized partial module",
        "fragment_char_start": 0,
        "fragment_char_end": 25,
    }
    # Rebind batch identity after constructing an adversarial-but-structurally plausible frame.
    from app.profile_reconciliation_batches import _hash
    frame = {k: v for k, v in b.items() if k not in {"input_hash", "batch_id"}}
    b["input_hash"] = _hash(frame)
    b["batch_id"] = b["input_hash"]
    with pytest.raises(BatchError, match="QUALITY_REQUIRES_UNSPLIT_PLAN_MODULE"):
        build_quality_contract(b)


def test_empty_requirement_list_uses_description_as_explicit_fallback_scope():
    local_plan = json.loads(json.dumps(PLAN))
    local_plan["planned_modules"][0]["requirements"] = []
    bs = plan_reconciliation_batches(
        local_plan,
        [evidence("parser", "prd.py", "parse preview confirm")],
        exact_head=HEAD,
        plan_profile_id=1,
        budget_record=BUDGET,
    )
    contract = build_quality_contract(bs[0])
    req = contract["modules"][0]["requirements"][0]
    assert req["source"] == "description"
    assert req["text"] == "Parse, preview and confirm PRD versions."


def test_readme_only_cannot_establish_implementation():
    b = one_requirement_batch([
        evidence("readme", "README.md", "Parse preview confirm is implemented and complete"),
    ])
    ref = b["repo_evidence"][0]["evidence_id"]
    assert build_quality_contract(b)["evidence_roles"][0]["role"] == "docs"
    with pytest.raises(BatchError, match="QUALITY_IMPLEMENTATION_WITHOUT_SOURCE_CODE"):
        validate_quality_result(b, one_requirement_result(b, [ref]))


def test_docs_and_manifest_only_cannot_establish_implementation():
    b = one_requirement_batch([
        evidence("readme", "docs/feature.md", "Parse preview confirm implementation"),
        evidence("manifest", "package.json", "parse preview confirm"),
    ])
    roles = {item["role"] for item in build_quality_contract(b)["evidence_roles"]}
    assert roles == {"docs", "manifest"}
    refs = [item["evidence_id"] for item in b["repo_evidence"]]
    with pytest.raises(BatchError, match="QUALITY_IMPLEMENTATION_WITHOUT_SOURCE_CODE"):
        validate_quality_result(b, one_requirement_result(b, refs))


def test_test_only_cannot_establish_implementation():
    b = one_requirement_batch([
        evidence("test", "tests/backend/test_prd.py", "parse preview confirm"),
    ])
    assert build_quality_contract(b)["evidence_roles"][0]["role"] == "test_code"
    ref = b["repo_evidence"][0]["evidence_id"]
    with pytest.raises(BatchError, match="QUALITY_IMPLEMENTATION_WITHOUT_SOURCE_CODE"):
        validate_quality_result(b, one_requirement_result(b, [ref]))


def test_source_plus_docs_can_support_and_verify_requirement():
    b = one_requirement_batch([
        evidence("source", "apps/backend/app/prd.py", "parse preview confirm"),
        evidence("readme", "README.md", "parse preview confirm"),
    ])
    roles = {item["evidence_id"]: item["role"] for item in build_quality_contract(b)["evidence_roles"]}
    refs = [item["evidence_id"] for item in b["repo_evidence"]]
    assert set(roles.values()) == {"source_code", "docs"}
    validated = validate_quality_result(b, one_requirement_result(b, refs))
    assert validated["mappings"][0]["support_state"] == "all_requirements_supported"
    assert verify_requirement_completeness(b, validated)["mappings"][0]["status"] == "implemented"


def test_source_plus_test_can_support_and_verify_requirement():
    b = one_requirement_batch([
        evidence("source", "apps/backend/app/prd.py", "parse preview confirm"),
        evidence("test", "tests/backend/test_prd.py", "parse preview confirm"),
    ])
    roles = {item["role"] for item in build_quality_contract(b)["evidence_roles"]}
    assert roles == {"source_code", "test_code"}
    refs = [item["evidence_id"] for item in b["repo_evidence"]]
    validated = validate_quality_result(b, one_requirement_result(b, refs))
    complete = verify_requirement_completeness(b, validated)
    assert complete["mappings"][0]["status"] == "implemented"
    assert complete["mappings"][0]["reason"] == "all_requirements_supported_with_source_code"


def test_partial_source_backed_result_stays_partial_in_completeness_verifier():
    b = one_requirement_batch([
        evidence("source", "apps/backend/app/prd.py", "parse preview confirm"),
    ])
    ref = b["repo_evidence"][0]["evidence_id"]
    validated = validate_quality_result(
        b,
        one_requirement_result(b, [ref], state="partial", gap="The exact confirmation transition is not established."),
    )
    assert verify_requirement_completeness(b, validated)["mappings"][0]["status"] == "partial"
