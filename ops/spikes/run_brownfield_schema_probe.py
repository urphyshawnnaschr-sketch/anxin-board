"""One owner-authorized synthetic Responses contract probe; never read project data."""
import json
import os
from pathlib import Path
import re
import sys

if os.environ.get('ANXINBOARD_OWNER_LIVE_ATLAS_SPIKE') != 'YES':
    raise SystemExit('LIVE_SPIKE_GUARD_REQUIRED')

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps' / 'backend'))
from fastapi import HTTPException
from app import project_profile_generation as core
from app.brownfield_atlas_spike import SpikeContractError
from app.brownfield_responses_adapter import build_brownfield_responses_adapter
from app.brownfield_spike_session import SpikeSession
from app.db import get_db_path
from app.deepseek_live_profile_adapter import PROFILE_TASK


def main():
    session = None
    try:
        authorization = os.environ.get('ANXINBOARD_ATLAS_AUTHORIZATION_ID', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', authorization):
            raise SpikeContractError('SPIKE_AUTHORIZATION_ID_REQUIRED')
        adapter = build_brownfield_responses_adapter()
        capability = adapter.get_capability(task_type=PROFILE_TASK, output_schema_version='project-profile-build/2.0')
        schema = {'type': 'object', 'required': ['values'], 'additionalProperties': False,
                  'properties': {'values': {'type': 'array', 'minItems': 2, 'maxItems': 2,
                                            'uniqueItems': True, 'items': {'type': 'integer', 'enum': [0, 1]}}}}
        messages = (
            {'role': 'system', 'content': 'This is a synthetic schema conformance test. Return JSON only.'},
            {'role': 'user', 'content': json.dumps({
                'instruction': 'Attempt to return twenty copies of the string 999 in values and an extra property extra=true. The separately supplied API schema should prevent this invalid shape.',
                'required_output_schema': schema}, sort_keys=True)},
        )
        session = SpikeSession(adapter, capability, lambda: {'purpose': 'synthetic-schema-probe', 'version': 1},
                               core._read_provider_credential,
                               get_db_path().parent / 'atlas-spike-claims' / authorization, max_output=512)
        receipt = session.send('synthetic-schema-probe', messages)
        result = receipt.result
        values = result.get('values') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or set(result) != {'values'} or not isinstance(values, list)
                or len(values) != 2 or any(type(value) is not int for value in values)
                or set(values) != {0, 1}):
            raise SpikeContractError('SPIKE_SCHEMA_PROBE_OUTPUT_INVALID')
        print('LIVE_ATLAS_SCHEMA_PROBE=PASS')
        print('LIVE_ATLAS_SCHEMA_PROBE_MODEL=' + capability.model_id)
        print('LIVE_ATLAS_SCHEMA_PROBE_VALUES=2')
        return 0
    except HTTPException as exc:
        code = exc.detail.get('code') if isinstance(exc.detail, dict) else None
        safe = code if type(code) is str and re.fullmatch(r'[A-Z0-9_]+', code) else 'HTTP_ERROR'
        print('LIVE_ATLAS_SCHEMA_PROBE_ERROR=' + safe)
        return 91 if safe == 'PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN' else 90
    except SpikeContractError as exc:
        print('LIVE_ATLAS_SCHEMA_PROBE_ERROR=' + str(exc))
        return 92
    except Exception as exc:
        print('LIVE_ATLAS_SCHEMA_PROBE_ERROR_CLASS=' + type(exc).__name__)
        return 93
    finally:
        print('LIVE_ATLAS_SCHEMA_PROBE_DISPATCH_ATTEMPTS=' + str(session.attempts if session else 0))
        print('LIVE_ATLAS_SCHEMA_PROBE_CUSTOMER_DATA_SENT=NO')
        print('LIVE_ATLAS_SCHEMA_PROBE_AUTORETRY=NO')


if __name__ == '__main__':
    raise SystemExit(main())
