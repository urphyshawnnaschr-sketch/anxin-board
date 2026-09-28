"""Context PRD Block Resolver V1 的 historical identity、Fail Closed 与零副作用测试。"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import context_resolver, db, prd, prd_parser  # noqa: E402

SCHEMA = "prd_structured_evidence_v1"
PARSER = "prd-structured-parser-1.0"
SNAPSHOT_SCHEMA = "evidence_snapshot_core_v2"
NOW = "2026-08-12T00:00:00+00:00"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@pytest.fixture()
def frozen(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    prd_root = tmp_path / "prd-root"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(prd_root))
    db.init_db()

    source_hash = _sha(b"source-v1")
    parsed_hash = _sha(b"parsed-v1")
    fingerprint = prd._document_fingerprint(PARSER, SCHEMA, "md", source_hash)
    block_hash = _sha(b"frozen-block-v1")
    evidence_id = f"prd:block:{fingerprint}:1"
    block = {
        "ordinal": 1,
        "kind": "paragraph",
        "evidence_id": evidence_id,
        "content_hash": block_hash,
        "text": "冻结正文 Alpha",
        "heading_level": None,
        "table_rows": None,
        "page_no": None,
    }
    structured = {
        "schema_version": SCHEMA,
        "parser_version": PARSER,
        "source_format": "md",
        "source_hash": source_hash,
        "document_fingerprint": fingerprint,
        "blocks": [block],
    }
    raw = _canonical(structured)

    with sqlite3.connect(db_path) as conn:
        project_id = conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES ('Resolver', 'active', ?)",
            (NOW,),
        ).lastrowid
        structured_path = f"{project_id}/prd/structured-v1.json"
        target = prd_root / structured_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        prd_id = conn.execute(
            """
            INSERT INTO prd_versions (
                project_id, version_no, original_filename, source_path, source_hash,
                size_bytes, parsed_path, parsed_hash, parser_version,
                structured_path, structured_hash, structured_schema_version,
                structured_parser_version, document_fingerprint, status,
                warnings_json, created_at, confirmed_by, confirmed_at
            ) VALUES (?, 1, 'prd.md', 'source-v1.md', ?, 10, 'parsed-v1.txt', ?,
                      'legacy-parser', ?, ?, ?, ?, ?, 'parse_confirmed', '[]', ?, 'pm', ?)
            """,
            (
                project_id,
                source_hash,
                parsed_hash,
                structured_path,
                _sha(raw),
                SCHEMA,
                PARSER,
                fingerprint,
                NOW,
                NOW,
            ),
        ).lastrowid
        snapshot_id = conn.execute(
            """
            INSERT INTO evidence_snapshots (
                schema_version, project_id, git_snapshot_id, analysis_lineage_id,
                branch, from_commit, to_commit, project_repository_url,
                project_config_hash, git_facts_hash, prd_id, prd_source_hash,
                prd_parsed_hash, prd_structured_hash, prd_document_fingerprint,
                profile_id, profile_content_hash, snapshot_hash, frozen_at
            ) VALUES (?, ?, 1, 1, 'main', ?, ?, 'https://example.invalid/repo.git',
                      ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                SNAPSHOT_SCHEMA,
                project_id,
                "a" * 40,
                "b" * 40,
                "1" * 64,
                "2" * 64,
                prd_id,
                source_hash,
                parsed_hash,
                _sha(raw),
                fingerprint,
                "3" * 64,
                "4" * 64,
                NOW,
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO evidence_items (
                snapshot_id, evidence_id, type, source_ref, content_hash,
                selected, redaction_state
            ) VALUES (?, ?, 'prd_block', ?, ?, 1, 'pending')
            """,
            (
                snapshot_id,
                evidence_id,
                f"prd_structured_block:{prd_id}:1",
                block_hash,
            ),
        )

    return {
        "db_path": db_path,
        "prd_root": prd_root,
        "project_id": project_id,
        "prd_id": prd_id,
        "snapshot_id": snapshot_id,
        "evidence_id": evidence_id,
        "source_hash": source_hash,
        "parsed_hash": parsed_hash,
        "fingerprint": fingerprint,
        "block_hash": block_hash,
        "structured_path": structured_path,
        "structured": structured,
        "target": target,
        "raw": raw,
    }


def _resolve(state):
    return context_resolver.resolve_prd_block(state["snapshot_id"], state["evidence_id"])


def _assert_code(code, state):
    with pytest.raises(HTTPException) as caught:
        _resolve(state)
    assert caught.value.detail["code"] == code


def _sync_artifact(state, structured, *, raw=None, fingerprint=None):
    raw = _canonical(structured) if raw is None else raw
    state["target"].write_bytes(raw)
    updates = ["structured_hash = ?"]
    values = [_sha(raw)]
    if fingerprint is not None:
        updates.append("document_fingerprint = ?")
        values.append(fingerprint)
    with sqlite3.connect(state["db_path"]) as conn:
        conn.execute(
            f"UPDATE prd_versions SET {', '.join(updates)} WHERE id = ?",
            (*values, state["prd_id"]),
        )
        conn.execute(
            f"UPDATE evidence_snapshots SET {', '.join('prd_' + part for part in updates)} WHERE id = ?"
            if fingerprint is None
            else "UPDATE evidence_snapshots SET prd_structured_hash = ?, prd_document_fingerprint = ? WHERE id = ?",
            (*values, state["snapshot_id"]),
        )
    return raw


def _replace_artifact_hash(state, raw):
    state["target"].write_bytes(raw)
    digest = _sha(raw)
    with sqlite3.connect(state["db_path"]) as conn:
        conn.execute(
            "UPDATE prd_versions SET structured_hash = ? WHERE id = ?",
            (digest, state["prd_id"]),
        )
        conn.execute(
            "UPDATE evidence_snapshots SET prd_structured_hash = ? WHERE id = ?",
            (digest, state["snapshot_id"]),
        )


def _db_dump(path):
    with sqlite3.connect(path) as conn:
        return "\n".join(conn.iterdump())


def test_t01_normal_exact_resolution_is_idempotent(frozen):
    first = _resolve(frozen)
    second = _resolve(frozen)
    assert first == second
    assert first == {
        "snapshot_id": frozen["snapshot_id"],
        "evidence_id": frozen["evidence_id"],
        "type": "prd_block",
        "source_ref": f"prd_structured_block:{frozen['prd_id']}:1",
        "content_hash": frozen["block_hash"],
        "prd_id": frozen["prd_id"],
        "ordinal": 1,
        "kind": "paragraph",
        "text": "冻结正文 Alpha",
        "heading_level": None,
        "table_rows": None,
        "page_no": None,
        "prd_structured_hash": _sha(frozen["raw"]),
        "prd_document_fingerprint": frozen["fingerprint"],
    }
    assert "structured_path" not in first


def test_t02_historical_snapshot_does_not_follow_current_prd(frozen):
    new_source_hash = _sha(b"source-v2")
    new_fp = prd._document_fingerprint(PARSER, SCHEMA, "md", new_source_hash)
    new_structured = {
        "schema_version": SCHEMA,
        "parser_version": PARSER,
        "source_format": "md",
        "source_hash": new_source_hash,
        "document_fingerprint": new_fp,
        "blocks": [],
    }
    new_raw = _canonical(new_structured)
    new_path = f"{frozen['project_id']}/prd/structured-v2.json"
    new_target = frozen["prd_root"] / new_path
    new_target.write_bytes(new_raw)
    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute("UPDATE prd_versions SET status = 'superseded' WHERE id = ?", (frozen["prd_id"],))
        conn.execute(
            """
            INSERT INTO prd_versions (
                project_id, version_no, original_filename, source_path, source_hash,
                size_bytes, parsed_path, parsed_hash, parser_version,
                structured_path, structured_hash, structured_schema_version,
                structured_parser_version, document_fingerprint, status,
                warnings_json, created_at
            ) VALUES (?, 2, 'prd-v2.md', 'source-v2.md', ?, 11, 'parsed-v2.txt', ?,
                      'legacy-parser', ?, ?, ?, ?, ?, 'parse_confirmed', '[]', ?)
            """,
            (
                frozen["project_id"],
                new_source_hash,
                _sha(b"parsed-v2"),
                new_path,
                _sha(new_raw),
                SCHEMA,
                PARSER,
                new_fp,
                NOW,
            ),
        )
    assert _resolve(frozen)["text"] == "冻结正文 Alpha"


def test_t03_historical_v1_snapshot_fails_closed(frozen):
    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_snapshots SET schema_version = 'evidence_snapshot_core_v1', "
            "prd_structured_hash = NULL, prd_document_fingerprint = NULL WHERE id = ?",
            (frozen["snapshot_id"],),
        )
    _assert_code("CONTEXT_PRD_SNAPSHOT_UNSUPPORTED", frozen)


@pytest.mark.parametrize("field", ["prd_structured_hash", "prd_document_fingerprint"])
def test_t04_snapshot_frozen_identity_mismatch_fails_closed(frozen, field):
    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute(
            f"UPDATE evidence_snapshots SET {field} = ? WHERE id = ?",
            ("f" * 64, frozen["snapshot_id"]),
        )
    _assert_code("CONTEXT_PRD_ARTIFACT_INVALID", frozen)


@pytest.mark.parametrize("case", ["bytes", "noncanonical", "schema", "parser", "source", "fingerprint"])
def test_t05_artifact_identity_failures_are_closed(frozen, case):
    structured = json.loads(json.dumps(frozen["structured"], ensure_ascii=False))
    if case == "bytes":
        frozen["target"].write_bytes(frozen["raw"] + b"\n")
    elif case == "noncanonical":
        raw = json.dumps(structured, ensure_ascii=False, indent=2).encode("utf-8")
        _replace_artifact_hash(frozen, raw)
    elif case == "schema":
        structured["schema_version"] = "wrong-schema"
        _replace_artifact_hash(frozen, _canonical(structured))
    elif case == "parser":
        structured["parser_version"] = "wrong-parser"
        _replace_artifact_hash(frozen, _canonical(structured))
    elif case == "source":
        structured["source_hash"] = "e" * 64
        _replace_artifact_hash(frozen, _canonical(structured))
    else:
        structured["document_fingerprint"] = "f" * 64
        raw = _canonical(structured)
        frozen["target"].write_bytes(raw)
        with sqlite3.connect(frozen["db_path"]) as conn:
            conn.execute(
                "UPDATE prd_versions SET structured_hash = ?, document_fingerprint = ? WHERE id = ?",
                (_sha(raw), "f" * 64, frozen["prd_id"]),
            )
            conn.execute(
                "UPDATE evidence_snapshots SET prd_structured_hash = ?, prd_document_fingerprint = ? WHERE id = ?",
                (_sha(raw), "f" * 64, frozen["snapshot_id"]),
            )
    _assert_code("CONTEXT_PRD_ARTIFACT_INVALID", frozen)


@pytest.mark.parametrize(
    "source_ref",
    [
        "prd_structured_block:999:1",
        "prd_structured_block:1:0",
        "prd_structured_block:1:01",
        "wrong:1:1",
    ],
)
def test_t06_source_ref_closure_fails_closed(frozen, source_ref):
    if source_ref.startswith("prd_structured_block:1:"):
        source_ref = source_ref.replace(":1:", f":{frozen['prd_id']}:", 1)
    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_items SET source_ref = ? WHERE snapshot_id = ? AND evidence_id = ?",
            (source_ref, frozen["snapshot_id"], frozen["evidence_id"]),
        )
    _assert_code("CONTEXT_PRD_ITEM_INVALID", frozen)


@pytest.mark.parametrize("field,value", [("type", "git_file_fact"), ("selected", 0), ("redaction_state", "safe")])
def test_item_membership_and_redaction_admission_is_strict(frozen, field, value):
    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute(
            f"UPDATE evidence_items SET {field} = ? WHERE snapshot_id = ? AND evidence_id = ?",
            (value, frozen["snapshot_id"], frozen["evidence_id"]),
        )
    _assert_code("CONTEXT_PRD_ITEM_INVALID", frozen)


@pytest.mark.parametrize("case", ["ordinal", "evidence_id", "content_hash"])
def test_t07_block_closure_fails_closed(frozen, case):
    structured = json.loads(json.dumps(frozen["structured"], ensure_ascii=False))
    if case == "ordinal":
        structured["blocks"][0]["ordinal"] = 2
    elif case == "evidence_id":
        structured["blocks"][0]["evidence_id"] = f"prd:block:{frozen['fingerprint']}:2"
    else:
        structured["blocks"][0]["content_hash"] = "f" * 64
    _replace_artifact_hash(frozen, _canonical(structured))
    _assert_code("CONTEXT_PRD_ARTIFACT_INVALID", frozen)


def test_t08_path_escape_preserves_prd_storage_escape_and_outside_file(frozen, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside-secret")
    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute(
            "UPDATE prd_versions SET structured_path = '../outside.json' WHERE id = ?",
            (frozen["prd_id"],),
        )
    with pytest.raises(HTTPException) as caught:
        _resolve(frozen)
    assert caught.value.detail["code"] == "PRD_STORAGE_ESCAPE"
    assert outside.read_bytes() == b"outside-secret"


def test_t09_parser_is_never_rerun(frozen, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("parser must not run")

    assert not hasattr(context_resolver, "parse_prd_bytes")
    assert not hasattr(context_resolver, "parse_prd_structured_bytes")
    monkeypatch.setattr(prd_parser, "parse_prd_bytes", boom)
    monkeypatch.setattr(prd_parser, "parse_prd_structured_bytes", boom)
    monkeypatch.setattr(prd, "parse_prd_bytes", boom)
    monkeypatch.setattr(prd, "parse_prd_structured_bytes", boom)
    assert _resolve(frozen)["text"] == "冻结正文 Alpha"


def test_t10_success_and_failure_have_zero_side_effect(frozen, monkeypatch):
    import socket

    def network_boom(*args, **kwargs):
        raise AssertionError("network must not run")

    monkeypatch.setattr(socket, "create_connection", network_boom)
    before_db = _db_dump(frozen["db_path"])
    before_bytes = frozen["target"].read_bytes()
    before_tables = None
    with sqlite3.connect(frozen["db_path"]) as conn:
        before_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    _resolve(frozen)
    assert _db_dump(frozen["db_path"]) == before_db
    assert frozen["target"].read_bytes() == before_bytes

    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute(
            "UPDATE evidence_items SET source_ref = 'broken' WHERE snapshot_id = ? AND evidence_id = ?",
            (frozen["snapshot_id"], frozen["evidence_id"]),
        )
    failed_before_db = _db_dump(frozen["db_path"])
    failed_before_bytes = frozen["target"].read_bytes()
    _assert_code("CONTEXT_PRD_ITEM_INVALID", frozen)
    assert _db_dump(frozen["db_path"]) == failed_before_db
    assert frozen["target"].read_bytes() == failed_before_bytes

    with sqlite3.connect(frozen["db_path"]) as conn:
        after_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert after_tables == before_tables
    assert "context_manifests" not in after_tables
    assert "reports" not in after_tables


def test_stable_missing_and_required_error_codes(frozen):
    with pytest.raises(HTTPException) as missing_snapshot:
        context_resolver.resolve_prd_block(99999, frozen["evidence_id"])
    assert missing_snapshot.value.detail["code"] == "CONTEXT_PRD_SNAPSHOT_UNSUPPORTED"

    with pytest.raises(HTTPException) as missing_item:
        context_resolver.resolve_prd_block(frozen["snapshot_id"], "prd:block:missing:1")
    assert missing_item.value.detail["code"] == "CONTEXT_PRD_ITEM_NOT_FOUND"

    with sqlite3.connect(frozen["db_path"]) as conn:
        conn.execute(
            "UPDATE prd_versions SET structured_path = NULL WHERE id = ?",
            (frozen["prd_id"],),
        )
    _assert_code("CONTEXT_PRD_ARTIFACT_REQUIRED", frozen)