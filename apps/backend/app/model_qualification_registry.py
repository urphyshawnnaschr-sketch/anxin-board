"""Read-only closed-world qualification admission registry for Page07 AI tasks.

The shipping registry intentionally contains no admitted real-model records. A future
qualification admission requires a reviewed Product code/data change that adds an exact
record after real qualification evidence and independent evidence Review PASS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from types import MappingProxyType
from typing import Final

from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION


QUALIFIED_STATUS: Final = "qualified"

_LOOKUP_FIELDS: Final = (
    "provider",
    "model_id",
    "model_version",
    "task_type",
    "ai_contract_schema_version",
    "output_schema_version",
    "prompt_version",
    "prompt_contract_hash",
    "rule_version",
    "sample_pack_version",
    "sampling_parameters_hash",
)
_EVIDENCE_FIELDS: Final = (
    "sample_manifest_hash",
    "qualification_harness_commit",
    "evidence_manifest_hash",
    "review_ref",
)
_RECORD_FIELDS: Final = _LOOKUP_FIELDS + _EVIDENCE_FIELDS + (
    "qualification_status",
    "record_hash",
)
_HASH_FIELDS: Final = tuple(field for field in _RECORD_FIELDS if field != "record_hash")
_HASH_VALUE_FIELDS: Final = (
    "prompt_contract_hash",
    "sampling_parameters_hash",
    "sample_manifest_hash",
    "evidence_manifest_hash",
    "record_hash",
)

_APPROVED_TASK_IDENTITIES_DATA: Final = {
    "daily_report_regenerate": MappingProxyType(
        {
            "task_type": "daily_report_regenerate",
            "output_schema_version": "daily-report-regenerate/1.0",
            "prompt_version": "page07-regenerate-prompt/1.0",
            "rule_version": "page07-regenerate-rules/2.0",
            "sample_pack_version": "page07-regenerate-qualification-pack/2.0",
        }
    ),
    "report_contradiction_check": MappingProxyType(
        {
            "task_type": "report_contradiction_check",
            "output_schema_version": "report-contradiction-check/1.0",
            "prompt_version": "page07-contradiction-prompt/1.0",
            "rule_version": "page07-contradiction-rules/1.0",
            "sample_pack_version": "page07-contradiction-qualification-pack/1.0",
        }
    ),
}
APPROVED_TASK_IDENTITIES: Final = MappingProxyType(_APPROVED_TASK_IDENTITIES_DATA)

# Deliberately empty in shipping Product code. Do not populate this tuple without a
# separately reviewed qualification-evidence admission change.
_SHIPPING_ADMITTED_RECORDS: Final[tuple[Mapping[str, object], ...]] = ()

__all__ = (
    "APPROVED_TASK_IDENTITIES",
    "QualificationRegistryError",
    "lookup_qualified_record",
)


class QualificationRegistryError(ValueError):
    """Stable fail-closed qualification-registry error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> QualificationRegistryError:
    return QualificationRegistryError(code, message)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_non_blank_utf8_string(value: object) -> bool:
    if type(value) is not str or not value.strip():
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_sha256_hex(value: object) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _is_git_commit_hex(value: object) -> bool:
    if type(value) is not str or len(value) not in (40, 64):
        return False
    return all(char in "0123456789abcdef" for char in value)


def _exact_mapping(value: object, fields: tuple[str, ...], *, code: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _fail(code, "qualification registry value must be a closed-world Mapping")
    try:
        keys = set(value.keys())
    except (TypeError, ValueError) as exc:
        raise _fail(code, "qualification registry fields are invalid") from exc
    if keys != set(fields):
        raise _fail(code, "qualification registry fields must match the exact closed-world schema")
    try:
        result = {field: value[field] for field in fields}
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail(code, "qualification registry fields cannot be read exactly") from exc
    if any(not _is_non_blank_utf8_string(result[field]) for field in fields):
        raise _fail(code, "qualification registry string fields must be non-blank UTF-8")
    return result


def _validate_approved_task_identity(value: Mapping[str, object]) -> None:
    task_type = value["task_type"]
    approved = APPROVED_TASK_IDENTITIES.get(task_type)
    if approved is None:
        raise _fail(
            "QUALIFICATION_IDENTITY_MISMATCH",
            "task_type is not an approved Page07 qualification identity",
        )
    if value["ai_contract_schema_version"] != AI_CONTRACT_SCHEMA_VERSION:
        raise _fail(
            "QUALIFICATION_IDENTITY_MISMATCH",
            "AI contract schema version does not match current Product authority",
        )
    for field in (
        "task_type",
        "output_schema_version",
        "prompt_version",
        "rule_version",
        "sample_pack_version",
    ):
        if value[field] != approved[field]:
            raise _fail(
                "QUALIFICATION_IDENTITY_MISMATCH",
                f"{field} does not match the approved exact task qualification identity",
            )


def _validate_lookup_identity(lookup_identity: object) -> dict[str, object]:
    value = _exact_mapping(
        lookup_identity,
        _LOOKUP_FIELDS,
        code="QUALIFICATION_LOOKUP_INVALID",
    )
    _validate_approved_task_identity(value)
    for field in ("prompt_contract_hash", "sampling_parameters_hash"):
        if not _is_sha256_hex(value[field]):
            raise _fail(
                "QUALIFICATION_LOOKUP_INVALID",
                f"{field} must be a lowercase SHA-256 hex digest",
            )
    return value


def _validate_and_reclose_record(record: object) -> Mapping[str, object]:
    value = _exact_mapping(
        record,
        _RECORD_FIELDS,
        code="QUALIFICATION_REGISTRY_INVALID",
    )
    _validate_approved_task_identity(value)
    if value["qualification_status"] != QUALIFIED_STATUS:
        raise _fail(
            "QUALIFICATION_REGISTRY_INVALID",
            "an admitted record must have exact qualified status",
        )
    for field in _HASH_VALUE_FIELDS:
        if not _is_sha256_hex(value[field]):
            raise _fail(
                "QUALIFICATION_REGISTRY_INVALID",
                f"{field} must be a lowercase SHA-256 hex digest",
            )
    if not _is_git_commit_hex(value["qualification_harness_commit"]):
        raise _fail(
            "QUALIFICATION_REGISTRY_INVALID",
            "qualification_harness_commit must be an exact lowercase Git commit id",
        )
    expected_hash = _stable_hash({field: value[field] for field in _HASH_FIELDS})
    if value["record_hash"] != expected_hash:
        raise _fail(
            "QUALIFICATION_REGISTRY_INVALID",
            "qualification admission record hash does not reclose",
        )
    return MappingProxyType(value)


def _lookup_exact_record_from_registry(
    lookup_identity: object,
    records: Sequence[object],
) -> Mapping[str, object]:
    """Pure exact-match primitive; production calls it only with the shipping registry.

    The ``records`` parameter is intentionally private so tests can exercise positive
    reclosure using synthetic fixtures without creating a production runtime admission API.
    """
    lookup = _validate_lookup_identity(lookup_identity)
    if isinstance(records, (str, bytes, bytearray)) or not isinstance(records, Sequence):
        raise _fail(
            "QUALIFICATION_REGISTRY_INVALID",
            "qualification registry must be an immutable sequence of exact records",
        )

    validated_records = tuple(_validate_and_reclose_record(record) for record in records)
    matches = tuple(
        record
        for record in validated_records
        if all(record[field] == lookup[field] for field in _LOOKUP_FIELDS)
    )
    if not matches:
        raise _fail(
            "QUALIFICATION_NOT_ADMITTED",
            "no exact admitted qualification record exists for this identity",
        )
    if len(matches) != 1:
        raise _fail(
            "QUALIFICATION_AMBIGUOUS",
            "multiple exact admitted qualification records exist for this identity",
        )
    return matches[0]


def lookup_qualified_record(lookup_identity: Mapping[str, object]) -> Mapping[str, object]:
    """Return exactly one shipping qualification admission record or fail closed.

    The caller supplies only the exact lookup identity. It cannot supply status, evidence,
    review provenance, registry rows, fallback candidates, or a runtime ``qualified`` flag.
    This slice does not grant provider-send authority and does not mutate any state.
    """
    return _lookup_exact_record_from_registry(lookup_identity, _SHIPPING_ADMITTED_RECORDS)
