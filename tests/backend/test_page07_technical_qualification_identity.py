"""Task-specific technical qualification must not reuse the prior admission identity."""
import pytest

from app import deepseek_current_authority as authority
from app import model_gateway as gateway
from app import model_qualification_registry as registry
from app import page07_model_preparation as preparation
from app import page07_send_authorization as authorization
from test_model_qualification_registry import _lookup


def test_all_regenerate_paths_bind_the_distinct_technical_qualification_identity():
    identity = registry.APPROVED_TASK_IDENTITIES['daily_report_regenerate']
    assert identity['sample_pack_version'] == 'page07-regenerate-qualification-pack/2.0'
    assert identity['rule_version'] == 'page07-regenerate-rules/2.0'
    assert preparation.SAMPLE_PACK_VERSION == gateway._REGENERATE_SAMPLE_PACK_VERSION == authority.REGENERATE_SAMPLE_PACK_VERSION == authorization._REGENERATE_SAMPLE_PACK == identity['sample_pack_version']
    assert preparation.RULE_VERSION == gateway._REGENERATE_RULE_VERSION == identity['rule_version']
    assert registry.APPROVED_TASK_IDENTITIES['report_contradiction_check']['sample_pack_version'] == 'page07-contradiction-qualification-pack/1.0'


def test_legacy_regenerate_identity_cannot_be_relabelled_as_technical_qualification():
    lookup = _lookup(rule_version='page07-regenerate-rules/1.0', sample_pack_version='page07-regenerate-qualification-pack/1.0')
    with pytest.raises(registry.QualificationRegistryError) as exc:
        registry.lookup_qualified_record(lookup)
    assert exc.value.code == 'QUALIFICATION_IDENTITY_MISMATCH'
