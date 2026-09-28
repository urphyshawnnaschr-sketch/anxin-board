"""Bounded, one-shot spike sends. Durable metadata only; never persist provider bodies."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from app.brownfield_atlas_spike import SpikeContractError
from app.deepseek_live_profile_adapter import PROFILE_TASK
from app.deepseek_profile_transport_policy import estimate_profile_request_utf8_bytes
from app.model_provider_contract import ProviderCredentialRequest

SCHEMA = 'project-profile-build/2.0'


def _identity(cap):
    return (cap.provider, cap.model_id, cap.model_version, cap.context_window_tokens, cap.max_output_tokens)


def estimate_spike_wire_bytes(adapter, capability, messages, max_output):
    estimator = getattr(adapter, 'estimate_request_bytes', None)
    size = (estimator(messages=messages, max_output_tokens=max_output, model_id=capability.model_id)
            if callable(estimator) else estimate_profile_request_utf8_bytes(
                messages=messages, max_tokens=max_output, model_id=capability.model_id))
    if type(size) is not int or size <= 0:
        raise SpikeContractError('SPIKE_WIRE_ESTIMATE_INVALID')
    return size


class SpikeSession:
    def __init__(self, live, capability, scope_reader, credential_reader, claim_dir, *, max_output=4000, expected_scope=None):
        self.live, self.capability = live, capability
        self.scope_reader, self.credential_reader = scope_reader, credential_reader
        self.scope = deepcopy(scope_reader())
        if expected_scope is not None and self.scope != expected_scope:
            raise SpikeContractError('SPIKE_SCOPE_CHANGED')
        self.cap_identity = _identity(capability)
        self.claim_dir = Path(claim_dir)
        self.max_output = max_output
        self.attempts = 0
        self.stopped = False
        self.started = False
        self.credential = None
        self.max_input = capability.context_window_tokens - max_output - 16384

    def check_scope(self):
        current = self.live.get_capability(task_type=PROFILE_TASK, output_schema_version=SCHEMA)
        if self.scope_reader() != self.scope or _identity(current) != self.cap_identity:
            raise SpikeContractError('SPIKE_SCOPE_CHANGED')

    def send(self, stage, messages):
        if self.stopped or self.attempts >= 8:
            raise SpikeContractError('SPIKE_SESSION_STOPPED')
        self.check_scope()
        wire = estimate_spike_wire_bytes(self.live, self.capability, messages, self.max_output)
        if wire > self.max_input or self.max_output > self.capability.max_output_tokens:
            raise SpikeContractError('SPIKE_REQUEST_OVER_BUDGET')
        if not self.started:
            self.claim_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.claim_dir.mkdir()
            except FileExistsError:
                raise SpikeContractError('SPIKE_AUTHORIZATION_ALREADY_CLAIMED') from None
            self.started = True
        try:
            if self.credential is None:
                self.credential = self.credential_reader()
                if type(self.credential) is not str or not self.credential.strip():
                    raise SpikeContractError('SPIKE_CREDENTIAL_INVALID')
            self.check_scope()
            self.attempts += 1
            request_hash = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            # Exclusive record precedes dispatch, including timeouts and interrupted sends.
            with (self.claim_dir / f'{self.attempts}.json').open('x', encoding='utf-8') as stream:
                json.dump({'stage': stage, 'request_hash': request_hash, 'wire_bytes': wire, 'status': 'claimed'}, stream)
            request = ProviderCredentialRequest(
                local_task_id=f'atlas-{request_hash[:24]}', provider=self.capability.provider,
                model_id=self.capability.model_id, model_version=self.capability.model_version,
                task_type=PROFILE_TASK, output_schema_version=SCHEMA, messages=messages,
                max_output_tokens=self.max_output)
            receipt = self.live.execute_with_credential(request, self.credential)
            self.check_scope()
            return receipt
        except BaseException:
            self.stopped = True
            raise
