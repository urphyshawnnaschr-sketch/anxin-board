"""Exact-key rows collected within one response, never followed by another request."""
import json
from copy import deepcopy
from dataclasses import dataclass, fields

import httpx
from fastapi import HTTPException

from app.brownfield_responses_adapter import SUPPORTED_MODELS, _receipt
from app.brownfield_collector_json import CollectorConflict, POLICY, normalize_arguments, _identical
from app.model_provider_contract import ProviderReceipt
from app.brownfield_strict_wire import build_strict_wire, decode_strict_wire, StrictWireError
from app.deepseek_live_profile_adapter import (
    _bounded_body, _client, _error, _profile_http_error, _strict_json,
    build_live_deepseek_profile_adapter,
)

URL = 'https://api.deepseek.com/beta/chat/completions'
TOOL_NAME = 'submit_brownfield_evidence'
MAX_COLLECTOR_CALLS = 16
TRANSPORT_POLICY = {'format': 'strict-chat-single-binding/8', 'thinking': 'disabled', 'temperature': 0,
                    'collector_json_policy': POLICY, 'collector_calls_policy': 'identical-complete-single-result/1',
                    'max_collector_calls': MAX_COLLECTOR_CALLS}


@dataclass(frozen=True, slots=True)
class CollectorReceipt(ProviderReceipt):
    normalized_duplicate_fields: int
    normalized_tool_calls: int
    calls_received: int
    # A redundant call introduces no new row; complementary calls are not collapsed.
    calls_collapsed: int
    row_occurrences_received: int
    duplicate_rows_removed: int


def normalization_metadata(receipt):
    """Persist counts only, never a provider-supplied key, value or fragment."""
    if not isinstance(receipt, CollectorReceipt):
        return {}
    counts = (receipt.normalized_duplicate_fields, receipt.normalized_tool_calls, receipt.calls_received, receipt.calls_collapsed,
              receipt.row_occurrences_received, receipt.duplicate_rows_removed)
    if any(type(n) is not int or not 0 <= n <= 2**31 - 1 for n in counts):
        return {}
    return {'policy': POLICY, 'duplicate_fields_removed': counts[0], 'tool_calls_normalized': counts[1],
            'calls_received': counts[2], 'calls_collapsed': counts[3],
            'row_occurrences_received': counts[4], 'duplicate_rows_removed': counts[5], 'unique_rows': counts[4] - counts[5],
            'calls_policy': TRANSPORT_POLICY['collector_calls_policy']}


def invalid(suffix, tool_call_count=None, expected_row_count=None):
    error = _error(502, 'PROFILE_GENERATION_STRICT_' + suffix, 'Strict collector contract validation failed.')
    diagnostic = {key: value for key, value in {'tool_call_count': tool_call_count, 'expected_row_count': expected_row_count}.items()
                  if type(value) is int and 0 <= value <= 2**31 - 1}
    if diagnostic:
        error.detail['diagnostic'] = diagnostic
    return error


def build_payload(*, messages, max_output_tokens, model_id):
    if model_id not in SUPPORTED_MODELS or type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise invalid('REQUEST_INVALID')
    rewritten, schema = build_strict_wire(messages)
    schema = deepcopy(schema)
    verification = 'status' in schema['properties']
    if verification:
        unknown = deepcopy(schema)
        unknown['properties']['status'] = {'type': 'string', 'enum': ['unknown']}
        variants = [unknown]
        last = schema['properties']['evidence_0']['maximum']
        if last >= 0:
            positive = deepcopy(schema)
            positive['properties']['status'] = {'type': 'string', 'enum': ['implemented', 'partial']}
            positive['properties']['evidence_0'] = {'type': 'integer', 'minimum': 0, 'maximum': last}
            variants.append(positive)
        schema['anyOf'] = variants
    source = json.loads(rewritten[-1]['content'])
    source['required_output_schema'] = schema
    system = rewritten[0]['content']
    system += (' Submit ONE complete flat result through the tool for this single locally bound input. '
               'All fields in required_output_schema are mandatory. Do not emit module identifiers, '
               'requirement identifiers, row_index, row or requirement wrappers, arrays or prose. '
               'Do not spread fields across calls or invent defaults.')
    if verification:
        system += (' unknown may cite relevant context without establishing implementation; implemented or partial requires '
                   'evidence_0 to be a supplied citation. Left-pack distinct citations followed by -1.')
    rewritten = ({'role': 'system', 'content': system},
                 {'role': 'user', 'content': json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)})
    return {'model': model_id, 'messages': list(rewritten), 'thinking': {'type': 'disabled'},
            'temperature': 0, 'max_tokens': max_output_tokens, 'stream': False,
            'tools': [{'type': 'function', 'function': {'name': TOOL_NAME, 'strict': True,
                       'description': 'Submit one complete result for the single input bound by this request.', 'parameters': schema}}],
            'tool_choice': {'type': 'function', 'function': {'name': TOOL_NAME}}}


def wire_bytes(**kwargs):
    return json.dumps(build_payload(**kwargs), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def estimate_request_bytes(**kwargs):
    return len(wire_bytes(**kwargs))


def receipt(response, request):
    _, fixed_schema = build_strict_wire(request.messages)
    expected_rows = 1
    def fail(suffix, calls=None):
        return invalid(suffix, calls, expected_rows)
    try:
        payload = _strict_json(_bounded_body(response))
    except (HTTPException, UnicodeError, RecursionError):
        raise fail('ENVELOPE_INVALID') from None
    if type(payload) is not dict or payload.get('object') != 'chat.completion':
        raise fail('ENVELOPE_INVALID')
    if payload.get('model') != request.model_id:
        raise fail('MODEL_IDENTITY_MISMATCH')
    choices = payload.get('choices')
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise fail('CHOICES_INVALID')
    choice = choices[0]
    if choice.get('finish_reason') != 'tool_calls' or type(choice.get('index')) is not int or choice.get('index') != 0:
        raise fail('NOT_COMPLETED')
    message = choice.get('message')
    if type(message) is not dict or message.get('role') != 'assistant' or message.get('content') not in (None, ''):
        raise fail('MESSAGE_INVALID')
    calls = message.get('tool_calls')
    if type(calls) is not list or not 1 <= len(calls) <= MAX_COLLECTOR_CALLS:
        raise fail('TOOL_COUNT_INVALID', len(calls) if type(calls) is list else None)
    usage = payload.get('usage')
    if type(usage) is not dict:
        raise fail('USAGE_INVALID', len(calls))
    call_ids = set()
    duplicate_fields, normalized_calls = 0, 0
    collected, parsed = {}, None
    rows_received, rows_collapsed, calls_collapsed = 0, 0, 0
    for ordinal, call in enumerate(calls):
        if type(call) is not dict:
            raise fail('TOOL_INVALID', len(calls))
        function = call.get('function')
        if (call.get('type') != 'function' or type(call.get('id')) is not str or not call['id'].strip()
                or call['id'] in call_ids or type(function) is not dict or function.get('name') != TOOL_NAME
                or type(function.get('arguments')) is not str):
            raise fail('TOOL_INVALID', len(calls))
        call_ids.add(call['id'])
        try:
            argument_text, removed = normalize_arguments(function['arguments'])
        except CollectorConflict:
            error = fail('ARGUMENT_CONFLICT', len(calls))
            error.detail['diagnostic']['tool_call_ordinal'] = ordinal
            raise error from None
        duplicate_fields += removed
        normalized_calls += int(removed > 0)
        # Strict-parse and validate every supplied row before collecting any of them.
        # Usage is the one HTTP response usage, never added per duplicate tool call.
        envelope = {'object': 'response', 'status': 'completed', 'error': None, 'incomplete_details': None,
                    'id': payload.get('id'), 'model': payload.get('model'),
                    'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                                'content': [{'type': 'output_text', 'text': argument_text}]}],
                    'usage': {'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                              'total_tokens': usage.get('total_tokens')}}
        try:
            current = _receipt(httpx.Response(200, content=json.dumps(envelope, ensure_ascii=True).encode('utf-8')), request.model_id, request.max_output_tokens)
        except HTTPException as error:
            error.detail.setdefault('diagnostic', {}).update(tool_call_count=len(calls), expected_row_count=expected_rows)
            raise
        arguments = current.result
        try:
            decoded = decode_strict_wire(arguments, request.messages)
            if 'requirements' in decoded:
                if arguments['status'] != 'unknown' and arguments['evidence_0'] < 0:
                    rule = ('POSITIVE_WITHOUT_CITATIONS' if all(arguments[f'evidence_{i}'] == -1 for i in range(6))
                            else 'POSITIVE_FIRST_SLOT_EMPTY')
                    raise StrictWireError('STATUS_CITATION_INVALID', wire_rule=rule, row_ordinal=0)
        except StrictWireError as exc:
            error = fail('WIRE_INVALID', len(calls))
            error.detail['diagnostic'].update(exc.diagnostic, tool_call_ordinal=ordinal, accepted_row_count=int(parsed is not None))
            raise error from None
        except ValueError:
            error = fail('WIRE_INVALID', len(calls))
            error.detail['diagnostic'].update(tool_call_ordinal=ordinal, accepted_row_count=int(parsed is not None))
            raise error from None
        rows_received += 1
        if parsed is not None:
            if not _identical(collected, arguments):
                raise fail('BATCH_CONFLICT', len(calls))
            rows_collapsed += 1
            calls_collapsed += 1
        else:
            collected = arguments
        parsed = current
    try:
        result = decode_strict_wire(collected, request.messages)
    except StrictWireError as exc:
        error = fail('WIRE_INVALID', len(calls))
        error.detail['diagnostic'].update(exc.diagnostic, accepted_row_count=int(parsed is not None))
        raise error from None
    except ValueError:
        error = fail('WIRE_INVALID', len(calls))
        error.detail['diagnostic']['accepted_row_count'] = int(parsed is not None)
        raise error from None
    values = {field.name: getattr(parsed, field.name) for field in fields(ProviderReceipt)}
    return CollectorReceipt(**dict(values, result=result), normalized_duplicate_fields=duplicate_fields,
                            normalized_tool_calls=normalized_calls, calls_received=len(calls), calls_collapsed=calls_collapsed,
                            row_occurrences_received=rows_received, duplicate_rows_removed=rows_collapsed)



class BrownfieldStrictAdapter:
    provider_id = 'deepseek'
    estimate_request_bytes = staticmethod(estimate_request_bytes)

    def __init__(self):
        self._selected = build_live_deepseek_profile_adapter()

    def get_capability(self, **kwargs):
        cap = self._selected.get_capability(**kwargs)
        if cap.provider != 'deepseek' or cap.model_id not in SUPPORTED_MODELS:
            raise invalid('MODEL_UNSUPPORTED')
        return cap

    def execute_with_credential(self, request, credential):
        cap = self.get_capability(task_type=request.task_type, output_schema_version=request.output_schema_version)
        if (request.provider, request.model_id, request.model_version) != (cap.provider, cap.model_id, cap.model_version):
            raise invalid('MODEL_SELECTION_STALE')
        wire = wire_bytes(messages=request.messages, max_output_tokens=request.max_output_tokens, model_id=request.model_id)
        if request.max_output_tokens > cap.max_output_tokens or len(wire) > cap.context_window_tokens-request.max_output_tokens-16384:
            raise invalid('REQUEST_OVER_BUDGET')
        if type(credential) is not str or not credential.strip():
            raise invalid('CREDENTIAL_REQUIRED')
        try:
            with _client() as client:
                response = client.post(URL, content=wire, headers={'Authorization': 'Bearer '+credential, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        except (httpx.TimeoutException, httpx.RequestError):
            raise _error(502, 'PROFILE_GENERATION_PROVIDER_NETWORK_UNKNOWN', 'Provider send outcome unknown; no automatic retry.') from None
        if response.status_code != 200:
            raise _profile_http_error(response.status_code)
        return receipt(response, request)


def build_brownfield_strict_adapter():
    return BrownfieldStrictAdapter()
