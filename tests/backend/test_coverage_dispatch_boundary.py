import pytest

from app.profile_reconciliation_batches import BatchError, _hash, _validate_batch
from app.profile_reconciliation_provider_quality import dispatch_authorized_quality_batch
from app.profile_reconciliation_quality import build_quality_contract
from app.profile_reconciliation_quality_candidate import aggregate_quality_candidate


def test_unknown_schema_cannot_enter_legacy_quality_or_candidate_paths():
    plan = {'schema_version': 'project_profile_v2', 'planned_modules': []}
    batch = {'schema_version': 'repository_coverage_supplement_v1',
             'plan_hash': _hash(plan), 'max_input_tokens': 100000}
    batch['input_hash'] = _hash(batch)
    batch['batch_id'] = batch['input_hash']
    def forbidden(*args, **kwargs):
        pytest.fail('Coverage preflight must not touch credentials, provider, or runtime state')
    calls = [lambda: _validate_batch(batch), lambda: build_quality_contract(batch),
             lambda: dispatch_authorized_quality_batch(None, batch, authorization_hash='unused',
                 adapter=None, credential_reader=forbidden, current_state=forbidden),
             lambda: aggregate_quality_candidate(None, [batch], plan,
                 authorization_hash='unused', current_state=forbidden)]
    for call in calls:
        with pytest.raises(BatchError, match='UNSUPPORTED_BATCH_SCHEMA'):
            call()
