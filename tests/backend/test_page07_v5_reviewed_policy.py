import inspect

from app import anxin_board_report_store, anxin_board_v3_materialization, report_validation_runtime


def test_reviewed_v5_policy_boundaries_are_narrow():
    assert report_validation_runtime._VALIDATION_POLICY_VERSION == "page07_low_friction_v5"
    assert "REPORT_STAGE_EVIDENCE_NOT_CLOSED" in report_validation_runtime._ADVISORY_BLOCKER_CODES

    materialize = inspect.getsource(
        anxin_board_v3_materialization.materialize_approved_anxin_board_v3_in_transaction
    )
    assert "read_bound_project_profile_for_report" in materialize
    assert "read_current_confirmed_project_profile" not in materialize

    persist = inspect.getsource(
        anxin_board_report_store.persist_anxin_board_report_v3_in_transaction
    )
    assert "require_current_profile=True" not in persist
    assert persist.count("require_current_profile=False") == 3

    stored = inspect.getsource(anxin_board_report_store._validate_stored_row)
    assert 'report["project_name"] != expected_project_name' in stored


def test_stage_evidence_is_advisory_but_identity_corruption_stays_hard():
    blockers = [
        {"code": "REPORT_STAGE_EVIDENCE_NOT_CLOSED", "message": "quality"},
        {"code": "MODEL_AUTHORITY_IDENTITY_INVALID", "message": "identity"},
    ]
    assert report_validation_runtime._hard_blockers(blockers) == [blockers[1]]
