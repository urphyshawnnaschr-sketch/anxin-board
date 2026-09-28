import inspect

from app import main, report_review_api, report_validation_runtime


def test_validation_runtime_never_replays_git_or_prd_context_candidate():
    source = inspect.getsource(report_validation_runtime.create_validation_result)
    closure_source = inspect.getsource(report_validation_runtime.build_approval_closure)
    assert "build_context_candidate_set" not in source
    assert "build_context_candidate_set" not in closure_source
    assert "get_durable_review_bundle" in source


def test_normal_project_evolution_and_optional_contradiction_are_advisory():
    blockers = [
        {"code": "PROJECT_GIT_AUTHORITY_DRIFT", "message": "git changed"},
        {"code": "CURRENT_PRD_AUTHORITY_DRIFT", "message": "prd changed"},
        {"code": "REPORT_CONTRADICTION_CHECK_REQUIRED", "message": "optional"},
        {"code": "REPORT_STAGE_EVIDENCE_NOT_CLOSED", "message": "semantic advisory"},
        {"code": "MODEL_AUTHORITY_IDENTITY_INVALID", "message": "hard"},
    ]
    assert report_validation_runtime._hard_blockers(blockers) == [blockers[-1]]


def test_report_review_api_is_bound_to_low_friction_validation_runtime():
    assert report_review_api.create_validation_result is report_validation_runtime.create_validation_result
    assert main.report_review_router is report_review_api.router


def test_low_friction_validation_identity_is_policy_versioned(monkeypatch):
    monkeypatch.setattr(report_validation_runtime.base, "_candidate_payload", lambda *args, **kwargs: {"legacy_candidate": "same"})
    monkeypatch.setattr(report_validation_runtime.base, "_stable_hash", lambda value: value)
    identity = report_validation_runtime._validation_candidate_hash(
        {}, git_snapshot_id=1, git_facts_hash="a" * 64, candidate_set_hash="b" * 64
    )
    assert identity == {
        "validation_policy_version": "page07_low_friction_v5",
        "candidate": {"legacy_candidate": "same"},
    }
