"""Opaque bounded credential bundles for iLink tokens and cursors.

Each native credential retains the existing 2048-byte limit. A manifest and its
derived children exist only inside the current user's credential store; callers
persist the random root reference, never these values, in SQLite.
"""
import hashlib
import hmac
import json
import re
from uuid import uuid4

from app.secret_store import (
    MAX_SECRET_BYTES, InvalidSecretValueError, SecretNotFoundError,
    SecretStoreError, SecretStoreOSError, validate_secret_ref, validate_secret_value,
)

_ROOT = re.compile(r'wcb1-[0-9a-f]{32}\Z')
_MAX_BYTES = 32768
# A surrogate pair can leave two bytes unused at a native entry boundary.
_MIN_FULL_CHUNK = MAX_SECRET_BYTES - 2
_MAX_CHUNKS = (_MAX_BYTES + _MIN_FULL_CHUNK - 1) // _MIN_FULL_CHUNK


class _RollbackFailure(SecretStoreOSError):
    code = 'SECRET_BUNDLE_ROLLBACK_FAILED'
    message = 'secret bundle rollback failed'

    def __init__(self, ref):
        self.cleanup_refs = (ref,)
        super().__init__()


def _child(ref, index):
    return f'{ref}-c{index:02d}'


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError
        out[key] = value
    return out


def _manifest(store, ref):
    try:
        text = store.get(ref)
        validate_secret_value(text)
        value = json.loads(text, object_pairs_hook=_unique)
        if (type(value) is not dict or set(value) != {'v','n','bytes','sha256'}
                or type(value['v']) is not int or value['v'] != 1
                or type(value['n']) is not int or not 1 <= value['n'] <= _MAX_CHUNKS
                or type(value['bytes']) is not int or not 2 <= value['bytes'] <= _MAX_BYTES
                or value['bytes'] % 2 or type(value['sha256']) is not str
                or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
            raise ValueError
        return value
    except SecretNotFoundError:
        raise
    except (ValueError, TypeError, RecursionError, InvalidSecretValueError):
        raise SecretStoreOSError() from None


def put_secret(store, value, kind):
    if type(value) is not str or not value or '\x00' in value or kind not in ('bot','context','cursor'):
        raise InvalidSecretValueError()
    try:
        raw = value.encode('utf-16-le', errors='strict')
    except UnicodeError:
        raise InvalidSecretValueError() from None
    if len(raw) > _MAX_BYTES:
        raise InvalidSecretValueError()
    chunks, part, size = [], [], 0
    for char in value:
        width = 4 if ord(char) > 0xffff else 2
        if size + width > MAX_SECRET_BYTES:
            chunks.append(''.join(part)); part, size = [], 0
        part.append(char); size += width
    chunks.append(''.join(part))
    ref = 'wcb1-' + uuid4().hex
    metadata = json.dumps({'v':1,'n':len(chunks),'bytes':len(raw),
                           'sha256':hashlib.sha256(raw).hexdigest()}, separators=(',',':'))
    try:
        # Write the recovery manifest first. It is not usable by the application
        # until the caller atomically persists the reference after this returns.
        store.put(ref, metadata)
        for index, chunk in enumerate(chunks):
            store.put(_child(ref, index), chunk)
    except SecretStoreError:
        try:
            delete_secret(store, ref)
        except SecretNotFoundError:
            pass
        except SecretStoreError:
            raise _RollbackFailure(ref) from None
        raise
    return ref


def get_secret(store, ref):
    validate_secret_ref(ref)
    if not _ROOT.fullmatch(ref):
        return store.get(ref)
    metadata = _manifest(store, ref)
    try:
        chunks = []
        for index in range(metadata['n']):
            part = store.get(_child(ref, index))
            validate_secret_value(part)
            chunks.append(part)
        value = ''.join(chunks)
        raw = value.encode('utf-16-le', errors='strict')
        if len(raw) != metadata['bytes'] or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), metadata['sha256']):
            raise SecretStoreOSError()
        return value
    except (SecretNotFoundError, InvalidSecretValueError, UnicodeError):
        raise SecretStoreOSError() from None


def delete_secret(store, ref):
    validate_secret_ref(ref)
    if not _ROOT.fullmatch(ref):
        return store.delete(ref)
    metadata = _manifest(store, ref)
    failed = False
    for index in range(metadata['n']):
        try:
            store.delete(_child(ref, index))
        except SecretNotFoundError:
            pass
        except SecretStoreError:
            failed = True
    if failed:
        # Retain exact ownership metadata; never enumerate or broaden deletion.
        raise SecretStoreOSError()
    store.delete(ref)
