import pytest

from app.profile_reconciliation_retrieval_coverage import retrieval_coverage


def test_duplicate_batch_selection_does_not_hide_unselected_code():
    items = [{'evidence_id': 'a', 'content': 'abc'}, {'evidence_id': 'b', 'content': '文本'}]
    result = retrieval_coverage(items, [{'repo_evidence': [items[0]]}, {'repo_evidence': [items[0]]}])
    assert result == dict(safe_evidence_chunks=2, selected_unique_chunks=1,
        unselected_safe_chunks=1, selected_unique_safe_bytes=3, unselected_safe_bytes=6,
        full_safe_text_selected=False, model_analysis_complete=False)


def test_all_selected_is_still_not_proof_of_model_analysis():
    items = [{'evidence_id': 'a', 'content': 'abc'}]
    result = retrieval_coverage(items, [{'repo_evidence': items}])
    assert result['full_safe_text_selected'] is True
    assert result['model_analysis_complete'] is False


def test_unknown_or_changed_evidence_fails_closed():
    items = [{'evidence_id': 'a', 'content': 'abc'}]
    for selection in ([{'evidence_id': 'b', 'content': 'abc'}], [{'evidence_id': 'a', 'content': 'changed'}]):
        with pytest.raises(ValueError, match='RETRIEVAL_COVERAGE_IDENTITY_MISMATCH'):
            retrieval_coverage(items, [{'repo_evidence': selection}])


def test_empty_inventory_does_not_claim_complete_selection():
    assert retrieval_coverage([], [])['full_safe_text_selected'] is False
