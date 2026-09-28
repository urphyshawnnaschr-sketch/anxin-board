"""Unit tests for the bounded SecretStore port."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.secret_store import (  # noqa: E402
    MAX_SECRET_BYTES,
    InMemorySecretStore,
    InvalidSecretReferenceError,
    InvalidSecretValueError,
    SecretNotFoundError,
    SecretStore,
)


def test_in_memory_put_get_replace_delete_and_not_found():
    store = InMemorySecretStore()
    assert isinstance(store, SecretStore)

    store.put("deepseek.token", "first-secret")
    assert store.get("deepseek.token") == "first-secret"

    store.put("deepseek.token", "replacement-secret")
    assert store.get("deepseek.token") == "replacement-secret"

    store.delete("deepseek.token")
    with pytest.raises(SecretNotFoundError):
        store.get("deepseek.token")
    with pytest.raises(SecretNotFoundError):
        store.delete("deepseek.token")


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "../secret",
        "folder/secret",
        r"folder\\secret",
        "line\nbreak",
        "tab\tbreak",
        "has space",
        "中文",
        "-leading-dash",
        "Model.Token",
        "x" * 65,
    ],
)
def test_reference_validation_fails_closed(ref):
    store = InMemorySecretStore()
    with pytest.raises(InvalidSecretReferenceError) as exc_info:
        store.put(ref, "safe-value")
    assert "safe-value" not in str(exc_info.value)
    assert "safe-value" not in repr(exc_info.value)


def test_case_variant_cannot_alias_lowercase_in_memory_ref():
    store = InMemorySecretStore()
    store.put("model.token", "lowercase-secret")

    for operation in (
        lambda: store.put("Model.Token", "replacement-secret"),
        lambda: store.get("Model.Token"),
        lambda: store.delete("Model.Token"),
    ):
        with pytest.raises(InvalidSecretReferenceError):
            operation()

    assert store.get("model.token") == "lowercase-secret"


def test_secret_empty_nul_and_size_boundaries():
    store = InMemorySecretStore()

    with pytest.raises(InvalidSecretValueError):
        store.put("mail.password", "")
    with pytest.raises(InvalidSecretValueError):
        store.put("mail.password", "before\x00after")

    exact = "x" * (MAX_SECRET_BYTES // 2)
    store.put("mail.password", exact)
    assert store.get("mail.password") == exact

    with pytest.raises(InvalidSecretValueError) as exc_info:
        store.put("mail.password", exact + "x")
    assert exact[:64] not in str(exc_info.value)
    assert exact[:64] not in repr(exc_info.value)


def test_stable_errors_do_not_expose_secret_values():
    store = InMemorySecretStore()
    secret = "never-print-this-secret"

    with pytest.raises(InvalidSecretReferenceError) as invalid_ref:
        store.put("bad/ref", secret)
    with pytest.raises(SecretNotFoundError) as missing:
        store.get("missing")

    for error in (invalid_ref.value, missing.value):
        assert secret not in str(error)
        assert secret not in repr(error)
        assert error.code.startswith("SECRET_")
