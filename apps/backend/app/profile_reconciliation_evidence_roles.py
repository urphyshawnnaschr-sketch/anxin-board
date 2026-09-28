"""Deterministic evidence-role binding for Phase 3 quality reconciliation.

This module is additive.  Legacy repository evidence IDs remain immutable; Quality V1
binds each legacy evidence object to a deterministic role identity derived from its
existing source identity, exact path and content hash.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Mapping

ROLE_POLICY_VERSION = "profile-reconciliation-evidence-role/1"
EVIDENCE_ROLES = frozenset({"source_code", "test_code", "docs", "config", "manifest", "other"})

_DOC_NAMES = frozenset({
    "readme", "license", "changelog", "contributing", "authors", "notice", "security",
})
_DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc"})
_MANIFEST_NAMES = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "poetry.lock", "pipfile", "pipfile.lock", "setup.py", "setup.cfg",
    "requirements.txt", "cargo.toml", "cargo.lock", "go.mod", "go.sum", "pom.xml",
    "build.gradle", "build.gradle.kts", "gradle.properties", "composer.json", "composer.lock",
    "gemfile", "gemfile.lock", "pubspec.yaml", "pubspec.lock",
})
_CONFIG_SUFFIXES = frozenset({".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env"})
_SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".java", ".kt", ".kts", ".go", ".rs", ".cs", ".c", ".cc", ".cpp", ".cxx",
    ".h", ".hh", ".hpp", ".hxx", ".swift", ".rb", ".php", ".scala", ".dart",
    ".sql", ".ps1", ".sh", ".bash", ".zsh",
})
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "e2e", "integration-tests"})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def classify_evidence_role(path: str) -> str:
    """Classify a repository path conservatively without inspecting untrusted content."""
    if type(path) is not str or not path or "\\" in path or path.startswith("/"):
        raise ValueError("QUALITY_EVIDENCE_PATH_INVALID")
    pure = PurePosixPath(path)
    lowered_parts = tuple(part.casefold() for part in pure.parts)
    name = pure.name.casefold()
    suffix = pure.suffix.casefold()
    stem = pure.stem.casefold()

    if any(part in _TEST_DIRS for part in lowered_parts[:-1]) or name.startswith("test_") or stem.endswith((".test", ".spec", "_test")):
        return "test_code"
    if name in _MANIFEST_NAMES or name.startswith("requirements-") and suffix == ".txt":
        return "manifest"
    if any(part in {"docs", "doc", "documentation"} for part in lowered_parts[:-1]) or suffix in _DOC_SUFFIXES or stem in _DOC_NAMES:
        return "docs"
    if suffix in _SOURCE_SUFFIXES:
        return "source_code"
    if suffix in _CONFIG_SUFFIXES or name.startswith(".") and name not in {".gitignore", ".gitattributes"}:
        return "config"
    return "other"


def bind_evidence_role(evidence: Mapping[str, object]) -> dict[str, str]:
    evidence_id = evidence.get("evidence_id")
    path = evidence.get("path")
    content_hash = evidence.get("content_hash")
    exact_head = evidence.get("exact_head")
    if type(evidence_id) is not str or not evidence_id:
        raise ValueError("QUALITY_EVIDENCE_ID_INVALID")
    if type(path) is not str or not path:
        raise ValueError("QUALITY_EVIDENCE_PATH_INVALID")
    if type(content_hash) is not str or len(content_hash) != 64:
        raise ValueError("QUALITY_EVIDENCE_CONTENT_HASH_INVALID")
    if type(exact_head) is not str or len(exact_head) not in {40, 64}:
        raise ValueError("QUALITY_EVIDENCE_HEAD_INVALID")
    role = classify_evidence_role(path)
    frozen = {
        "policy_version": ROLE_POLICY_VERSION,
        "evidence_id": evidence_id,
        "exact_head": exact_head,
        "path": path,
        "content_hash": content_hash,
        "role": role,
    }
    return frozen | {"role_binding_id": "evidence-role-" + _digest(frozen)}


def bind_batch_evidence_roles(batch: Mapping[str, object]) -> tuple[dict[str, str], ...]:
    raw = batch.get("repo_evidence")
    if not isinstance(raw, list):
        raise ValueError("QUALITY_EVIDENCE_INVALID")
    bindings = [bind_evidence_role(item) for item in raw]
    if len({item["evidence_id"] for item in bindings}) != len(bindings):
        raise ValueError("QUALITY_EVIDENCE_ID_DUPLICATE")
    return tuple(sorted(bindings, key=lambda item: (item["path"], item["evidence_id"])))


def evidence_roles_by_id(batch: Mapping[str, object]) -> dict[str, str]:
    return {item["evidence_id"]: item["role"] for item in bind_batch_evidence_roles(batch)}
