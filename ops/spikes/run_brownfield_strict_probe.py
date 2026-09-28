"""One synthetic strict collector probe: no project code or PRD is loaded."""
import os
from pathlib import Path
import re
import sys

if os.environ.get('ANXINBOARD_OWNER_LIVE_ATLAS_SPIKE') != 'YES':
    raise SystemExit('STRICT_PROBE_GUARD_REQUIRED')
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps' / 'backend'))
from app.brownfield_atlas_spike import build_requirement_verification_messages, validate_requirement_result
from app.brownfield_baseline import safe_code
from app.brownfield_strict_adapter import build_brownfield_strict_adapter
from app.brownfield_spike_session import SpikeSession
from app.db import get_db_path
from app.deepseek_live_profile_adapter import PROFILE_TASK
from app.project_profile_generation import _read_provider_credential
from app.repository_atlas import BUNDLE_SCHEMA_VERSION


def main():
    session = None
    try:
        nonce = os.environ.get('ANXINBOARD_ATLAS_AUTHORIZATION_ID', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', nonce):
            raise ValueError('BROWNFIELD_STRICT_PROBE_NONCE_REQUIRED')
        adapter = build_brownfield_strict_adapter()
        cap = adapter.get_capability(task_type=PROFILE_TASK, output_schema_version='project-profile-build/2.0')
        module = {'client_id': 'synthetic-module', 'name': 'Synthetic arithmetic',
                  'requirements': ['Return the sum of two inputs.', 'Persist encrypted database records.', 'Retry failed remote calls.']}
        bundle = {'schema_version': BUNDLE_SCHEMA_VERSION, 'complete_for_selected_paths': True,
                  'bundle_hash': 'a'*64, 'exact_head': 'a'*40, 'selected_paths': ['synthetic/add.py'],
                  'evidence': [{'evidence_id': 'synthetic-a', 'path': 'synthetic/add.py', 'exact_head': 'a'*40,
                                'content': 'def add(a, b):\n    return a + b\n'}]}
        messages = build_requirement_verification_messages(module, bundle)
        session = SpikeSession(adapter, cap, lambda: {'purpose': 'strict-fixed-slot-probe', 'version': 5},
            _read_provider_credential, get_db_path().parent/'atlas-spike-claims'/nonce, max_output=4000)
        result = session.send('strict-fixed-slot-probe', messages)
        rows = validate_requirement_result(result.result, module=module, bundle=bundle)
        if not any(row['status'] == 'unknown' for row in rows) or not any(row['status'] in {'implemented', 'partial'} for row in rows):
            raise ValueError('BROWNFIELD_STRICT_PROBE_BRANCH_COVERAGE_MISSING')
        print('STRICT_PROBE_ACCEPTANCE=PASS', flush=True)
        print('STRICT_PROBE_ROW_COUNT=3', flush=True)
        return 0
    except Exception as exc:
        print('STRICT_PROBE_ERROR_CODE=' + safe_code(exc), flush=True)
        print('STRICT_PROBE_ACCEPTANCE=FAIL', flush=True)
        return 1
    finally:
        print('STRICT_PROBE_DISPATCH_ATTEMPTS=' + str(session.attempts if session else 0), flush=True)
        print('STRICT_PROBE_CUSTOMER_DATA_SENT=NO', flush=True)
        print('STRICT_PROBE_AUTO_RETRY=NO', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
