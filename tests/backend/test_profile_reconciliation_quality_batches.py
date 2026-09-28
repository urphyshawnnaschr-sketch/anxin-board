import hashlib
import json

import app.profile_reconciliation_quality_batches as quality_batches
from app.profile_reconciliation_quality import build_quality_contract
from app.profile_reconciliation_quality_batches import (
    QUALITY_BATCH_SCHEMA_VERSION,
    QUALITY_RETRIEVAL_POLICY,
    plan_quality_reconciliation_batches,
)

HEAD = "b" * 40
BUDGET = {
    "context_window_tokens": 200000,
    "max_output_tokens": 8000,
    "reserved_output_tokens": 8000,
    "safety_margin_tokens": 16000,
    "counting_policy_version": "utf8_byte_upper_bound_v1",
}
PLAN = {
    "schema_version": "project_profile_v2",
    "planned_modules": [
        {
            "client_id": "FR-03-prd-preview",
            "name": "PRD preview",
            "description": "Parse preview and human confirm PRD versions",
            "requirements": [
                "Parse md txt docx PDF and expose a preview",
                "Only a human confirmed parsed version becomes current",
            ],
        }
    ],
}


def evidence(identity, path, text):
    return {
        "evidence_id": "repo-code-" + identity,
        "path": path,
        "content": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "exact_head": HEAD,
    }


def test_role_aware_retrieval_keeps_source_when_readme_has_stronger_lexical_match():
    items = [
        evidence("readme", "README.md", "PRD preview parse md txt docx PDF human confirmed current " * 8),
        evidence("parser", "apps/backend/app/prd_parser.py", "parse md txt docx pdf preview"),
        evidence("confirm", "apps/backend/app/prd.py", "human confirm parsed version current parse_confirmed"),
        evidence("tests", "tests/backend/test_prd_api.py", "preview human confirm parsed current"),
    ]
    batches = plan_quality_reconciliation_batches(
        PLAN, items, exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET,
    )
    assert len(batches) == 1
    batch = batches[0]
    assert batch["schema_version"] == QUALITY_BATCH_SCHEMA_VERSION
    assert batch["quality_retrieval"]["requirements_without_source_matches"] == 0
    assert batch["quality_retrieval"]["requirements_without_selected_source"] == 0
    assert batch["quality_retrieval"]["requirements_with_selected_source"] == 2
    assert batch["quality_retrieval"]["selected_source_requirement_indexes"] == [0, 1]
    assert batch["quality_retrieval"]["selected_role_counts"]["source_code"] >= 2
    roles = {item["role"] for item in build_quality_contract(batch)["evidence_roles"]}
    assert "source_code" in roles
    assert "test_code" in roles
    assert "docs" in roles


def test_quality_retrieval_is_deterministic_and_full_module_not_fragmented():
    items = [
        evidence("source", "apps/backend/app/prd.py", "parse preview human confirm current"),
        evidence("docs", "docs/prd.md", "parse preview human confirm current"),
    ]
    first = plan_quality_reconciliation_batches(
        PLAN, items, exact_head=HEAD, plan_profile_id=9, budget_record=BUDGET,
    )
    second = plan_quality_reconciliation_batches(
        json.loads(json.dumps(PLAN)), json.loads(json.dumps(items)),
        exact_head=HEAD, plan_profile_id=9, budget_record=json.loads(json.dumps(BUDGET)),
    )
    assert first == second
    assert len(first) == 1
    assert "plan_fragment" not in first[0]["planned_modules"][0]
    assert first[0]["batch_id"] == second[0]["batch_id"]
    assert first[0]["coverage"]["retrieval_policy"] == QUALITY_RETRIEVAL_POLICY


def test_docs_only_remains_visible_but_has_zero_source_matches():
    items = [
        evidence("readme", "README.md", "PRD preview parse md txt docx PDF human confirmed current"),
        evidence("design", "docs/design.md", "PRD preview human confirm current"),
    ]
    batch = plan_quality_reconciliation_batches(
        PLAN, items, exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET,
    )[0]
    assert batch["repo_evidence"]
    assert batch["quality_retrieval"]["requirements_with_source_matches"] == 0
    assert batch["quality_retrieval"]["requirements_without_source_matches"] == 2
    assert batch["quality_retrieval"]["requirements_with_selected_source"] == 0
    assert batch["quality_retrieval"]["requirements_without_selected_source"] == 2
    roles = {item["role"] for item in build_quality_contract(batch)["evidence_roles"]}
    assert roles == {"docs"}


def test_budget_diagnostics_distinguish_source_candidate_from_source_actually_packed(monkeypatch):
    plan = {
        "schema_version": "project_profile_v2",
        "planned_modules": [{
            "client_id": "FR-budget-proof",
            "name": "Neutral module",
            "description": "Neutral feature",
            "requirements": ["alphaunique behavior", "betaunique behavior"],
        }],
    }
    items = [
        evidence("alpha", "apps/backend/app/a.py", "alphaunique behavior"),
        evidence("beta", "apps/backend/app/b.py", "betaunique behavior"),
    ]
    tight_budget = {
        "context_window_tokens": 9500,
        "max_output_tokens": 4000,
        "reserved_output_tokens": 4000,
        "safety_margin_tokens": 3000,
        "counting_policy_version": "utf8_byte_upper_bound_v1",
    }

    # Make this regression about budget-stage bookkeeping rather than incidental JSON size:
    # empty frame fits, one evidence fits, a second evidence does not.
    monkeypatch.setattr(
        quality_batches,
        "_bytes",
        lambda frame: b"x" * (1000 + 1000 * len(frame.get("repo_evidence", []))),
    )
    batch = plan_quality_reconciliation_batches(
        plan, items, exact_head=HEAD, plan_profile_id=1, budget_record=tight_budget,
    )[0]
    retrieval = batch["quality_retrieval"]
    assert retrieval["requirements_without_source_matches"] == 0
    assert retrieval["requirements_with_source_matches"] == 2
    assert retrieval["selected_role_counts"]["source_code"] == 1
    assert retrieval["requirements_with_selected_source"] == 1
    assert retrieval["requirements_without_selected_source"] == 1
    assert len(retrieval["selected_source_requirement_indexes"]) == 1
    assert retrieval["budget_omitted_count"] == 1
    assert retrieval["budget_omitted"][0]["role"] == "source_code"
    assert len(retrieval["budget_omitted"][0]["matched_requirement_indexes"]) == 1


def test_unrelated_source_is_not_added_just_because_it_is_source_code():
    items = [
        evidence("unrelated", "apps/backend/app/billing.py", "invoice payment settlement tax"),
        evidence("docs", "README.md", "PRD preview parse human confirm current"),
    ]
    batch = plan_quality_reconciliation_batches(
        PLAN, items, exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET,
    )[0]
    ids = {item["evidence_id"] for item in batch["repo_evidence"]}
    assert "repo-code-unrelated" not in ids
    assert "repo-code-docs" in ids


def test_each_module_gets_its_own_role_aware_quality_batch():
    plan = json.loads(json.dumps(PLAN))
    plan["planned_modules"].append({
        "client_id": "FR-04-report",
        "name": "Report",
        "description": "Generate report html",
        "requirements": ["Generate final report HTML"],
    })
    items = [
        evidence("prd", "apps/backend/app/prd.py", "parse preview human confirm current"),
        evidence("report", "apps/backend/app/report.py", "generate final report html"),
    ]
    batches = plan_quality_reconciliation_batches(
        plan, items, exact_head=HEAD, plan_profile_id=2, budget_record=BUDGET,
    )
    assert len(batches) == 2
    assert [b["planned_modules"][0]["client_id"] for b in batches] == ["FR-03-prd-preview", "FR-04-report"]
    assert all(b["batch_count"] == 2 for b in batches)
    assert len({b["batch_set_hash"] for b in batches}) == 1
    assert all(b["all_module_ids"] == ["FR-03-prd-preview", "FR-04-report"] for b in batches)


def semantic_plan(identity="feature-audit-snapshot"):
    return {"schema_version": "project_profile_v2", "planned_modules": [{
        "client_id": identity, "name": "审计快照", "description": "保存历史事实",
        "requirements": ["冻结已确认的数据", "历史记录保持不变"],
    }]}


def test_semantic_identifier_recovers_all_file_chunks_without_claiming_requirement_coverage():
    items = [evidence(str(i), "backend/audit_snapshots.py", "persist frozen records " + str(i)) for i in range(5)]
    batch = plan_quality_reconciliation_batches(semantic_plan(), items, exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET)[0]
    assert len(batch["repo_evidence"]) == 5
    assert batch["quality_retrieval"]["requirements_with_selected_source"] == 0
    assert batch["quality_retrieval"]["requirements_with_source_matches"] == 0


def test_supplemental_metadata_must_exist_in_safe_text_and_opaque_ids_fall_back():
    good = evidence("good", "backend/service.py", "def auditSnapshots(): pass")
    good["structured_metadata"] = {"symbol": ["auditSnapshots"]}
    forged = evidence("forged", "backend/other.py", "unrelated code")
    forged["structured_metadata"] = {"symbol": ["auditSnapshots"]}
    one_term = evidence("one", "backend/audit.py", "unrelated code")
    for plan, expected in [(semantic_plan(), {"repo-code-good"}), (semantic_plan("82ea5522-78e0-4567-ae44-787776654321"), set())]:
        batch = plan_quality_reconciliation_batches(plan, [good, forged, one_term], exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET)[0]
        assert {x["evidence_id"] for x in batch["repo_evidence"]} == expected


def test_requirement_source_selection_prioritizes_distinct_files():
    items = [evidence(str(i), "frontend/view.js", "alphaunique extraspecial " * 5) for i in range(4)]
    items.append(evidence("backend", "backend/service.py", "alphaunique"))
    plan = semantic_plan("opaque")
    plan["planned_modules"][0]["requirements"] = ["alphaunique extraspecial"]
    batch = plan_quality_reconciliation_batches(plan, items, exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET)[0]
    assert "backend/service.py" in {x["path"] for x in batch["repo_evidence"]}


def test_supplemental_budget_omission_is_explicit_and_identity_is_order_independent():
    items = [evidence(str(i), "backend/audit_snapshots.py", "x" * 12000 + str(i)) for i in range(3)]
    budget = dict(BUDGET, context_window_tokens=44000)
    first = plan_quality_reconciliation_batches(semantic_plan(), items, exact_head=HEAD, plan_profile_id=1, budget_record=budget)[0]
    second = plan_quality_reconciliation_batches(semantic_plan(), list(reversed(items)), exact_head=HEAD, plan_profile_id=1, budget_record=budget)[0]
    assert first == second
    assert 0 < len(first["repo_evidence"]) < 3
    assert first["quality_retrieval"]["budget_omitted_count"] + len(first["repo_evidence"]) == 3
    assert all(x["matched_requirement_indexes"] == [] for x in first["quality_retrieval"]["budget_omitted"])


def test_common_requirement_terms_and_public_identifiers_do_not_create_source_coverage():
    plan = semantic_plan("public-static")
    plan["planned_modules"][0]["requirements"] = ["alphaunique shared", "betaunique shared"]
    items = [evidence("common", "backend/public_static.py", "public static shared")]
    batch = plan_quality_reconciliation_batches(plan, items, exact_head=HEAD, plan_profile_id=1, budget_record=BUDGET)[0]
    assert batch["repo_evidence"] == []
    assert batch["quality_retrieval"]["requirements_with_source_matches"] == 0
    assert batch["quality_retrieval"]["requirements_with_selected_source"] == 0


def test_budget_gives_supplemental_file_a_turn_before_repeating_requirement_file():
    plan = semantic_plan()
    plan["planned_modules"][0]["requirements"] = ["alphaunique"]
    items = [evidence(str(i), "frontend/view.js", "alphaunique " + "x" * 12000) for i in range(3)]
    items.append(evidence("backend", "backend/audit_snapshots.py", "persist " + "x" * 12000))
    batch = plan_quality_reconciliation_batches(plan, items, exact_head=HEAD, plan_profile_id=1, budget_record=dict(BUDGET, context_window_tokens=56000))[0]
    assert {x["path"] for x in batch["repo_evidence"]} == {"frontend/view.js", "backend/audit_snapshots.py"}
    assert len(batch["repo_evidence"]) == 2
    assert batch["quality_retrieval"]["requirements_with_selected_source"] == 1
