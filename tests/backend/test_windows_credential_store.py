"""Offline tests for the Windows Credential Manager SecretStore adapter."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app import windows_credential_store as module  # noqa: E402
from app.secret_store import (  # noqa: E402
    MAX_SECRET_BYTES,
    InvalidSecretReferenceError,
    SecretAccessDeniedError,
    SecretNotFoundError,
    SecretStoreOSError,
    UnsupportedSecretStorePlatformError,
)


class FakeNative:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.failure = None

    def write(self, *, target, secret_blob):
        self.calls.append(("write", target, secret_blob))
        if self.failure:
            raise self.failure()
        self.values[target] = secret_blob

    def read(self, *, target):
        self.calls.append(("read", target))
        if self.failure:
            raise self.failure()
        try:
            return self.values[target]
        except KeyError:
            raise SecretNotFoundError() from None

    def delete(self, *, target):
        self.calls.append(("delete", target))
        if self.failure:
            raise self.failure()
        try:
            del self.values[target]
        except KeyError:
            raise SecretNotFoundError() from None


def _store(monkeypatch):
    fake = FakeNative()
    monkeypatch.setattr(module, "_load_native_api", lambda: fake)
    return module.WindowsCredentialStore(), fake


def test_production_constructor_exposes_no_target_persistence_or_native_flags():
    signature = inspect.signature(module.WindowsCredentialStore)
    assert list(signature.parameters) == []


def test_target_namespace_and_put_get_delete_use_only_exact_target(monkeypatch):
    store, fake = _store(monkeypatch)
    secret = "opaque-秘密"

    store.put("model.token", secret)
    assert fake.calls == [
        ("write", "AnxinBoard/model.token", secret.encode("utf-16-le"))
    ]

    assert store.get("model.token") == secret
    assert fake.calls[-1] == ("read", "AnxinBoard/model.token")

    store.delete("model.token")
    assert fake.calls[-1] == ("delete", "AnxinBoard/model.token")


def test_reference_cannot_escape_fixed_namespace(monkeypatch):
    store, fake = _store(monkeypatch)
    for ref in ("../OtherApp", "Other/App", r"Other\\App", "line\nbreak", "Model.Token"):
        with pytest.raises(InvalidSecretReferenceError):
            store.get(ref)
    assert fake.calls == []


def test_case_variant_cannot_alias_native_target(monkeypatch):
    store, fake = _store(monkeypatch)
    secret = "lowercase-secret"
    store.put("model.token", secret)
    expected_write = ("write", "AnxinBoard/model.token", secret.encode("utf-16-le"))
    assert fake.calls == [expected_write]

    for operation in (
        lambda: store.put("Model.Token", "replacement-secret"),
        lambda: store.get("Model.Token"),
        lambda: store.delete("Model.Token"),
    ):
        with pytest.raises(InvalidSecretReferenceError):
            operation()
        assert fake.calls == [expected_write]

    assert store.get("model.token") == secret
    assert fake.calls[-1] == ("read", "AnxinBoard/model.token")
    assert all("AnxinBoard/Model.Token" not in call for call in fake.calls)


@pytest.mark.parametrize(
    "native_blob",
    [
        b"",
        b"x",
        b"\x00\x00",
        b"\x00\xd8",
        b"x" * (MAX_SECRET_BYTES + 2),
        "not-bytes",
    ],
)
def test_malformed_native_read_blob_fails_closed_as_os_error(monkeypatch, native_blob):
    store, fake = _store(monkeypatch)
    fake.values["AnxinBoard/model.token"] = native_blob

    with pytest.raises(SecretStoreOSError) as exc_info:
        store.get("model.token")

    assert exc_info.value.code == "SECRET_STORE_OS_FAILURE"
    assert "not-bytes" not in str(exc_info.value)
    assert "not-bytes" not in repr(exc_info.value)
    assert fake.calls == [("read", "AnxinBoard/model.token")]


def test_native_failure_taxonomy_stays_stable_and_secret_free(monkeypatch):
    store, fake = _store(monkeypatch)
    secret = "do-not-leak-this"

    for error_type in (SecretAccessDeniedError, SecretStoreOSError):
        fake.failure = error_type
        with pytest.raises(error_type) as exc_info:
            store.put("smtp.password", secret)
        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value)

    fake.failure = None
    with pytest.raises(SecretNotFoundError):
        store.get("smtp.password")


def test_non_windows_import_is_safe_and_production_instantiation_is_unsupported(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    with pytest.raises(UnsupportedSecretStorePlatformError):
        module._load_native_api()


def test_tests_never_instantiate_real_native_boundary(monkeypatch):
    def forbidden_native():
        raise AssertionError("tests must not touch real Windows Credential Manager")

    monkeypatch.setattr(module, "_CtypesCredentialApi", forbidden_native)
    fake = FakeNative()
    monkeypatch.setattr(module, "_load_native_api", lambda: fake)
    store = module.WindowsCredentialStore()
    store.put("test.secret", "fake-only")
    assert fake.calls[0][0] == "write"


def test_source_has_no_credential_enumeration_or_other_vault_access():
    source = Path(module.__file__).read_text(encoding="utf-8")
    forbidden = (
        "CredEnumerate",
        ".ssh",
        "Login Data",
        "Windows Vault",
        "sqlite3",
        "tempfile",
    )
    for token in forbidden:
        assert token not in source
