"""EvidenceSnapshot Core V2 的 structured identity、兼容迁移与 immutable replay 测试。"""

import hashlib
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))

from app import db, evidence_snapshots, main  # noqa: E402


A = "a" * 40
B = "b" * 40
C = "c" * 40
COMMIT_ONE = "1" * 40
COMMITS = [COMMIT_ONE, B]
REPOSITORY_URL = "https://example.invalid/org/repo.git"
PRD_SOURCE_HASH = hashlib.sha256(b"prd-source-v1").hexdigest()
PRD_PARSED_HASH = hashlib.sha256(b"prd-parsed-v1").hexdigest()
PROFILE_CONTENT = {"schema_version": "project_profile_manual_v1", "project_summary": "摘要"}
PROFILE_CONTENT_HASH = hashlib.sha256(
    json.dumps(
        PROFILE_CONTENT, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
).hexdigest()
SOURCE_TABLES = (
    "projects",
    "project_git_connections",
    "analysis_lineages",
    "git_snapshots",
    "prd_versions",
    "project_profiles",
)


def _stable_hash(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _current_file_manifest():
    file_facts = [
        {
            "path": "src/a.py",
            "added_lines": 5,
            "deleted_lines": 1,
            "is_binary": False,
        },
        {
            "path": "src/b.py",
            "added_lines": 3,
            "deleted_lines": 2,
            "is_binary": False,
        },
    ]
    records = []
    for ordinal, facts in enumerate(file_facts, start=1):
        records.append(
            {
                "ordinal": ordinal,
                **facts,
                "file_facts_hash": _stable_hash(facts),
            }
        )
    manifest_hash = _stable_hash(
        {"schema_version": "git_file_manifest_v1", "files": records}
    )
    return manifest_hash, records


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ANXINBOARD_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("ANXINBOARD_PRD_ROOT", str(tmp_path / "prd"))
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        yield test_client, tmp_path / "test.db"


def _prepare(client, db_path, *, name="Evidence 项目"):
    project = client.post("/api/projects", json={"name": name}).json()
    project = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "git_url": REPOSITORY_URL,
            "branch": "main",
            "version": project["version"],
        },
    ).json()["project"]
    now = "2026-08-08T00:00:00+00:00"
    file_manifest_hash, file_records = _current_file_manifest()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO project_git_connections (project_id, status, remote_head, local_head) "
            "VALUES (?, 'connected', ?, ?)",
            (project["id"], B, B),
        )
        lineage_id = conn.execute(
            """
            INSERT INTO analysis_lineages (
                project_id, sequence_no, branch, baseline_commit, status, created_at
            ) VALUES (?, 1, 'main', ?, 'active', ?)
            """,
            (project["id"], A, now),
        ).lastrowid
        git_snapshot_id = conn.execute(
            """
            INSERT INTO git_snapshots (
                project_id, analysis_lineage_id, branch, from_commit, to_commit,
                commits_json, commit_count, changed_file_count, added_lines,
                deleted_lines, diff_bytes, file_manifest_hash, frozen_at
            ) VALUES (?, ?, 'main', ?, ?, ?, 2, 2, 8, 3, 321, ?, ?)
            """,
            (
                project["id"],
                lineage_id,
                A,
                B,
                json.dumps(COMMITS, separators=(",", ":")),
                file_manifest_hash,
                now,
            ),
        ).lastrowid
        for record in file_records:
            conn.execute(
                """
                INSERT INTO git_file_evidence (
                    git_snapshot_id, ordinal, evidence_id, path, added_lines,
                    deleted_lines, is_binary, file_facts_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    git_snapshot_id,
                    record["ordinal"],
                    f"git:file:{git_snapshot_id}:{record['ordinal']:03d}",
                    record["path"],
                    record["added_lines"],
                    record["deleted_lines"],
                    1 if record["is_binary"] else 0,
                    record["file_facts_hash"],
                ),
            )
        prd_id = conn.execute(
            """
            INSERT INTO prd_versions (
                project_id, version_no, original_filename, source_path, source_hash,
                size_bytes, parsed_path, parsed_hash, parser_version, status,
                warnings_json, created_at, confirmed_by, confirmed_at
            ) VALUES (?, 1, 'prd.md', 'prd/source-v1', ?, 13, 'prd/parsed-v1', ?,
                      'test-parser-v1', 'parse_confirmed', '[]', ?, 'pm', ?)
            """,
            (project["id"], PRD_SOURCE_HASH, PRD_PARSED_HASH, now, now),
        ).lastrowid
        profile_id = conn.execute(
            """
            INSERT INTO project_profiles (
                project_id, version_no, source_prd_id, status, content_json,
                content_hash, edit_version, created_at, updated_at,
                confirmed_by, confirmed_at
            ) VALUES (?, 1, ?, 'confirmed', ?, ?, 2, ?, ?, 'pm', ?)
            """,
            (
                project["id"],
                prd_id,
                json.dumps(
                    PROFILE_CONTENT,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                PROFILE_CONTENT_HASH,
                now,
                now,
                now,
            ),
        ).lastrowid
    return {
        "project_id": project["id"],
        "lineage_id": lineage_id,
        "git_snapshot_id": git_snapshot_id,
        "prd_id": prd_id,
        "profile_id": profile_id,
    }


def _payload(facts):
    return {
        "expected_git_snapshot_id": facts["git_snapshot_id"],
        "expected_lineage_id": facts["lineage_id"],
        "expected_prd_id": facts["prd_id"],
        "expected_profile_id": facts["profile_id"],
    }


def _confirm(client, facts):
    return client.post(
        f"/api/projects/{facts['project_id']}/evidence-snapshots",
        json=_payload(facts),
    )


def _source_state(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in SOURCE_TABLES
        }


def _evidence_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM evidence_snapshots ORDER BY id")]


def _evidence_item_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM evidence_items ORDER BY id")]


def _file_evidence_rows(db_path, git_snapshot_id):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM git_file_evidence WHERE git_snapshot_id = ? ORDER BY ordinal",
                (git_snapshot_id,),
            )
        ]


def _switch_prd(db_path, facts):
    now = "2026-08-08T00:01:00+00:00"
    source_hash = hashlib.sha256(b"prd-source-v2").hexdigest()
    parsed_hash = hashlib.sha256(b"prd-parsed-v2").hexdigest()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE prd_versions SET status = 'superseded' WHERE id = ?",
            (facts["prd_id"],),
        )
        return conn.execute(
            """
            INSERT INTO prd_versions (
                project_id, version_no, original_filename, source_path, source_hash,
                size_bytes, parsed_path, parsed_hash, parser_version, status,
                warnings_json, created_at, confirmed_by, confirmed_at
            ) VALUES (?, 2, 'prd-v2.md', 'prd/source-v2', ?, 14, 'prd/parsed-v2', ?,
                      'test-parser-v1', 'parse_confirmed', '[]', ?, 'pm', ?)
            """,
            (facts["project_id"], source_hash, parsed_hash, now, now),
        ).lastrowid


def _switch_profile(db_path, facts, source_prd_id):
    now = "2026-08-08T00:02:00+00:00"
    content = {"schema_version": "project_profile_manual_v1", "project_summary": "新摘要"}
    content_json = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE project_profiles SET status = 'superseded' WHERE id = ?",
            (facts["profile_id"],),
        )
        return conn.execute(
            """
            INSERT INTO project_profiles (
                project_id, version_no, source_prd_id, status, content_json,
                content_hash, edit_version, created_at, updated_at,
                confirmed_by, confirmed_at
            ) VALUES (?, 2, ?, 'confirmed', ?, ?, 2, ?, ?, 'pm', ?)
            """,
            (
                facts["project_id"],
                source_prd_id,
                content_json,
                hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
                now,
                now,
                now,
            ),
        ).lastrowid


def _switch_lineage(db_path, facts):
    now = "2026-08-08T00:03:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE analysis_lineages SET status = 'closed', closed_at = ? WHERE id = ?",
            (now, facts["lineage_id"]),
        )
        return conn.execute(
            """
            INSERT INTO analysis_lineages (
                project_id, sequence_no, branch, baseline_commit, status, created_at
            ) VALUES (?, 2, 'main', ?, 'active', ?)
            """,
            (facts["project_id"], A, now),
        ).lastrowid


def test_t01_normal_freeze_is_atomic_and_hash_is_independently_reproducible(client):
    api, db_path = client
    db.init_db()
    db.init_db()
    facts = _prepare(api, db_path)
    before_sources = _source_state(db_path)

    response = _confirm(api, facts)

    assert response.status_code == 201, response.text
    body = response.json()
    snapshot = body["snapshot"]
    assert body["created"] is True
    project_config_hash = _stable_hash(
        {"repository_url": REPOSITORY_URL, "branch": "main"}
    )
    git_facts_hash = _stable_hash(
        {
            "branch": "main",
            "from_commit": A,
            "to_commit": B,
            "commits": COMMITS,
            "commit_count": 2,
            "changed_file_count": 2,
            "added_lines": 8,
            "deleted_lines": 3,
            "diff_bytes": 321,
        }
    )
    prd_structured_hash = hashlib.sha256(
        _canonical_bytes(facts["structured"])
    ).hexdigest()
    prd_document_fingerprint = facts["structured"]["document_fingerprint"]
    frozen_items = [
        {
            "evidence_id": row["evidence_id"],
            "type": row["type"],
            "source_ref": row["source_ref"],
            "content_hash": row["content_hash"],
            "selected": row["selected"],
            "redaction_state": row["redaction_state"],
        }
        for row in _evidence_item_rows(db_path)
    ]
    frozen_items.sort(key=lambda item: (item["type"], item["source_ref"], item["evidence_id"]))
    evidence_items_hash = _stable_hash(
        {"schema_version": "evidence_items_commitment_v1", "items": frozen_items}
    )
    hash_payload = {
        "schema_version": "evidence_snapshot_core_v3",
        "project_id": facts["project_id"],
        "project_repository_url": REPOSITORY_URL,
        "project_config_hash": project_config_hash,
        "git_snapshot_id": facts["git_snapshot_id"],
        "analysis_lineage_id": facts["lineage_id"],
        "branch": "main",
        "from_commit": A,
        "to_commit": B,
        "git_facts_hash": git_facts_hash,
        "prd_id": facts["prd_id"],
        "prd_source_hash": PRD_SOURCE_HASH,
        "prd_parsed_hash": PRD_PARSED_HASH,
        "prd_structured_hash": prd_structured_hash,
        "prd_document_fingerprint": prd_document_fingerprint,
        "profile_id": facts["profile_id"],
        "profile_content_hash": PROFILE_CONTENT_HASH,
        "evidence_items_hash": evidence_items_hash,
    }
    expected_snapshot_hash = _stable_hash(hash_payload)
    assert expected_snapshot_hash != _stable_hash(
        {
            key: value
            for key, value in hash_payload.items()
            if key not in {"prd_structured_hash", "prd_document_fingerprint"}
        }
    )
    assert expected_snapshot_hash != _stable_hash(
        {**hash_payload, "prd_structured_hash": "f" * 64}
    )
    assert expected_snapshot_hash != _stable_hash(
        {**hash_payload, "prd_document_fingerprint": "e" * 64}
    )
    assert snapshot == {
        "id": snapshot["id"],
        "schema_version": "evidence_snapshot_core_v3",
        "project_id": facts["project_id"],
        "git_snapshot_id": facts["git_snapshot_id"],
        "analysis_lineage_id": facts["lineage_id"],
        "branch": "main",
        "from_commit": A,
        "to_commit": B,
        "project_repository_url": REPOSITORY_URL,
        "project_config_hash": project_config_hash,
        "git_facts_hash": git_facts_hash,
        "prd_id": facts["prd_id"],
        "prd_source_hash": PRD_SOURCE_HASH,
        "prd_parsed_hash": PRD_PARSED_HASH,
        "prd_structured_hash": prd_structured_hash,
        "prd_document_fingerprint": prd_document_fingerprint,
        "profile_id": facts["profile_id"],
        "profile_content_hash": PROFILE_CONTENT_HASH,
        "snapshot_hash": expected_snapshot_hash,
        "frozen_at": snapshot["frozen_at"],
    }
    assert _evidence_rows(db_path) == [snapshot]
    assert _source_state(db_path) == before_sources
    assert "content_json" not in response.text
    assert "commits_json" not in response.text
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence_snapshots)")}
        indexes = conn.execute("PRAGMA index_list(evidence_snapshots)").fetchall()
    assert set(snapshot).issubset(columns)
    assert any(row[2] == 1 for row in indexes)


def test_t02_exact_duplicate_returns_same_immutable_row(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    first = _confirm(api, facts)
    frozen_rows = _evidence_rows(db_path)
    before_sources = _source_state(db_path)

    second = _confirm(api, facts)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["snapshot"] == first.json()["snapshot"]
    assert _evidence_rows(db_path) == frozen_rows
    assert _source_state(db_path) == before_sources


@pytest.mark.parametrize("drift", ["prd_profile", "lineage"])
def test_t03_exact_replay_after_successful_input_drift_returns_original(client, drift):
    api, db_path = client
    facts = _prepare(api, db_path)
    first = _confirm(api, facts)
    frozen_rows = _evidence_rows(db_path)
    if drift == "prd_profile":
        new_prd_id = _switch_prd(db_path, facts)
        _switch_profile(db_path, facts, new_prd_id)
    else:
        _switch_lineage(db_path, facts)
    before_replay_sources = _source_state(db_path)

    replay = _confirm(api, facts)

    assert replay.status_code == 201, replay.text
    assert replay.json()["created"] is False
    assert replay.json()["snapshot"] == first.json()["snapshot"]
    assert _evidence_rows(db_path) == frozen_rows
    assert _source_state(db_path) == before_replay_sources


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [
        ("prd", "EVIDENCE_SNAPSHOT_PRD_STALE"),
        ("profile", "EVIDENCE_SNAPSHOT_PROFILE_STALE"),
    ],
)
def test_t04_unfrozen_prd_or_profile_drift_is_rejected_zero_write(
    client, drift, expected_code
):
    api, db_path = client
    facts = _prepare(api, db_path)
    if drift == "prd":
        _switch_prd(db_path, facts)
    else:
        _switch_profile(db_path, facts, facts["prd_id"])
    before = _source_state(db_path)

    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code
    assert _evidence_rows(db_path) == []
    assert _source_state(db_path) == before


@pytest.mark.parametrize("drift", ["lineage", "baseline"])
def test_t05_unfrozen_lineage_or_baseline_drift_is_rejected_zero_write(client, drift):
    api, db_path = client
    facts = _prepare(api, db_path)
    if drift == "lineage":
        _switch_lineage(db_path, facts)
    else:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE analysis_lineages SET baseline_commit = ? WHERE id = ?",
                (C, facts["lineage_id"]),
            )
    before = _source_state(db_path)

    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_LINEAGE_STALE"
    assert _evidence_rows(db_path) == []
    assert _source_state(db_path) == before


@pytest.mark.parametrize(
    ("source_state", "expected_code"),
    [
        ("prd_missing", "EVIDENCE_SNAPSHOT_PRD_NOT_CONFIRMED"),
        ("parsed_hash_missing", "EVIDENCE_SNAPSHOT_SOURCE_DATA_INVALID"),
        ("profile_missing", "EVIDENCE_SNAPSHOT_PROFILE_NOT_CONFIRMED"),
        ("profile_prd_mismatch", "EVIDENCE_SNAPSHOT_PROFILE_PRD_MISMATCH"),
    ],
)
def test_missing_or_mismatched_current_sources_fail_closed(
    client, source_state, expected_code
):
    api, db_path = client
    facts = _prepare(api, db_path)
    with sqlite3.connect(db_path) as conn:
        if source_state == "prd_missing":
            conn.execute(
                "UPDATE prd_versions SET status = 'superseded' WHERE id = ?",
                (facts["prd_id"],),
            )
        elif source_state == "parsed_hash_missing":
            conn.execute(
                "UPDATE prd_versions SET parsed_hash = NULL WHERE id = ?",
                (facts["prd_id"],),
            )
        elif source_state == "profile_missing":
            conn.execute(
                "UPDATE project_profiles SET status = 'superseded' WHERE id = ?",
                (facts["profile_id"],),
            )
        else:
            conn.execute(
                "UPDATE project_profiles SET source_prd_id = ? WHERE id = ?",
                (facts["prd_id"] + 1000, facts["profile_id"]),
            )
    before = _source_state(db_path)

    response = _confirm(api, facts)

    assert response.status_code in {409, 500}
    assert response.json()["detail"]["code"] == expected_code
    assert _evidence_rows(db_path) == []
    assert _source_state(db_path) == before


def test_t06_cross_project_git_snapshot_is_rejected_without_leak_or_write(client):
    api, db_path = client
    project_a = _prepare(api, db_path, name="项目 A")
    project_b = _prepare(api, db_path, name="项目 B")
    payload = _payload(project_b)
    payload["expected_git_snapshot_id"] = project_a["git_snapshot_id"]
    before = _source_state(db_path)

    response = api.post(
        f"/api/projects/{project_b['project_id']}/evidence-snapshots", json=payload
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "EVIDENCE_SNAPSHOT_GIT_SNAPSHOT_NOT_FOUND",
        "message": "指定的 GitSnapshot 不存在或不属于当前项目。",
    }
    assert A not in response.text and B not in response.text
    assert _evidence_rows(db_path) == []
    assert _source_state(db_path) == before


@pytest.mark.parametrize(
    ("field", "value", "expected_status"),
    [
        ("expected_git_snapshot_id", 0, 400),
        ("expected_lineage_id", True, 422),
        ("expected_prd_id", "1", 422),
    ],
)
def test_confirmation_ids_are_strict_positive_integers(client, field, value, expected_status):
    api, db_path = client
    facts = _prepare(api, db_path)
    payload = _payload(facts)
    payload[field] = value

    response = api.post(
        f"/api/projects/{facts['project_id']}/evidence-snapshots", json=payload
    )

    assert response.status_code == expected_status
    assert _evidence_rows(db_path) == []


def test_client_cannot_submit_frozen_fact_fields(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    payload = _payload(facts)
    payload["branch"] = "forged"

    response = api.post(
        f"/api/projects/{facts['project_id']}/evidence-snapshots", json=payload
    )

    assert response.status_code == 422
    assert _evidence_rows(db_path) == []


def test_database_failure_is_sanitized_and_zero_write(client, monkeypatch):
    api, db_path = client
    facts = _prepare(api, db_path)

    def failing_connection():
        raise sqlite3.OperationalError("sensitive database detail")

    monkeypatch.setattr(evidence_snapshots, "get_connection", failing_connection)
    response = _confirm(api, facts)

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "EVIDENCE_SNAPSHOT_SAVE_FAILED",
        "message": "保存 EvidenceSnapshot 失败，数据库原状态未改变。",
    }
    assert "sensitive" not in response.text
    assert _evidence_rows(db_path) == []


def _update_project_config(api, facts, *, git_url, branch="main", name=None):
    project = api.get(f"/api/projects/{facts['project_id']}").json()
    response = api.put(
        f"/api/projects/{facts['project_id']}",
        json={
            "name": name if name is not None else project["name"],
            "git_url": git_url,
            "branch": branch,
            "version": project["version"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] is True
    return response.json()["project"]


def test_repository_change_closes_lineage_and_rejects_unfrozen_old_snapshot(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    repository_b = "https://example.invalid/org/repo-b.git"

    updated = _update_project_config(api, facts, git_url=repository_b)

    with sqlite3.connect(db_path) as conn:
        lineage = conn.execute(
            "SELECT status, break_reason, closed_at FROM analysis_lineages WHERE id = ?",
            (facts["lineage_id"],),
        ).fetchone()
        git_snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM git_snapshots WHERE id = ?",
            (facts["git_snapshot_id"],),
        ).fetchone()[0]
        connection = conn.execute(
            "SELECT project_id FROM project_git_connections WHERE project_id = ?",
            (facts["project_id"],),
        ).fetchone()
    assert updated["git_url"] == repository_b
    assert lineage[0] == "closed"
    assert lineage[1] == "git_config_changed"
    assert lineage[2]
    assert git_snapshot_count == 1
    assert connection is None

    after_config_change = _source_state(db_path)
    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_LINEAGE_STALE"
    assert _evidence_rows(db_path) == []
    assert _source_state(db_path) == after_config_change


def test_exact_replay_survives_repository_change_after_successful_freeze(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    first = _confirm(api, facts)
    frozen_rows = _evidence_rows(db_path)
    repository_b = "https://example.invalid/org/repo-b.git"

    _update_project_config(api, facts, git_url=repository_b)
    with sqlite3.connect(db_path) as conn:
        lineage = conn.execute(
            "SELECT status, break_reason, closed_at FROM analysis_lineages WHERE id = ?",
            (facts["lineage_id"],),
        ).fetchone()
    assert lineage[0] == "closed"
    assert lineage[1] == "git_config_changed"
    assert lineage[2]
    after_config_change = _source_state(db_path)

    replay = _confirm(api, facts)

    assert first.status_code == 201
    assert replay.status_code == 201, replay.text
    assert replay.json()["created"] is False
    assert replay.json()["snapshot"] == first.json()["snapshot"]
    assert _evidence_rows(db_path) == frozen_rows
    assert _source_state(db_path) == after_config_change


def test_new_repository_lineage_cannot_reactivate_old_git_snapshot(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    repository_b = "https://example.invalid/org/repo-b.git"
    _update_project_config(api, facts, git_url=repository_b)
    now = "2026-08-08T00:04:00+00:00"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO project_git_connections (project_id, status, remote_head, local_head) "
            "VALUES (?, 'connected', ?, ?)",
            (facts["project_id"], C, C),
        )
        new_lineage_id = conn.execute(
            """
            INSERT INTO analysis_lineages (
                project_id, sequence_no, branch, baseline_commit, status, created_at
            ) VALUES (?, 2, 'main', ?, 'active', ?)
            """,
            (facts["project_id"], C, now),
        ).lastrowid
        old_lineage = conn.execute(
            "SELECT status, break_reason FROM analysis_lineages WHERE id = ?",
            (facts["lineage_id"],),
        ).fetchone()
    assert new_lineage_id != facts["lineage_id"]
    assert old_lineage == ("closed", "git_config_changed")

    before = _source_state(db_path)
    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_LINEAGE_STALE"
    assert _evidence_rows(db_path) == []
    assert _source_state(db_path) == before


def test_name_only_project_edit_keeps_lineage_and_connection_valid(client):
    api, db_path = client
    facts = _prepare(api, db_path)

    updated = _update_project_config(
        api,
        facts,
        git_url=REPOSITORY_URL,
        branch="main",
        name="Evidence 项目改名",
    )

    with sqlite3.connect(db_path) as conn:
        lineage = conn.execute(
            "SELECT status, break_reason, closed_at FROM analysis_lineages WHERE id = ?",
            (facts["lineage_id"],),
        ).fetchone()
        connection = conn.execute(
            "SELECT status FROM project_git_connections WHERE project_id = ?",
            (facts["project_id"],),
        ).fetchone()
    assert updated["name"] == "Evidence 项目改名"
    assert lineage == ("active", None, None)
    assert connection == ("connected",)

    response = _confirm(api, facts)
    assert response.status_code == 201, response.text
    assert response.json()["created"] is True


def test_git_file_evidence_materializes_exact_evidence_items(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    source_rows = _file_evidence_rows(db_path, facts["git_snapshot_id"])
    response = _confirm(api, facts)

    assert response.status_code == 201, response.text
    snapshot_id = response.json()["snapshot"]["id"]
    items = _evidence_item_rows(db_path)
    assert len(items) == len(source_rows) == 2
    assert items == [
        {
            "id": items[index]["id"],
            "snapshot_id": snapshot_id,
            "evidence_id": source["evidence_id"],
            "type": "git_file_fact",
            "source_ref": f"git_file_evidence:{facts['git_snapshot_id']}:{source['ordinal']}",
            "content_hash": source["file_facts_hash"],
            "selected": 1,
            "redaction_state": "not_applicable",
        }
        for index, source in enumerate(source_rows)
    ]


def test_missing_file_manifest_fails_closed_without_backfill(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    source_rows = _file_evidence_rows(db_path, facts["git_snapshot_id"])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE git_snapshots SET file_manifest_hash = NULL WHERE id = ?",
            (facts["git_snapshot_id"],),
        )

    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "EVIDENCE_SNAPSHOT_FILE_EVIDENCE_INCOMPLETE",
        "message": "当前 GitSnapshot 不满足 EvidenceItem 映射前置条件，请重新生成符合当前 FileEvidence 合同的新 GitSnapshot。",
    }
    assert _evidence_rows(db_path) == []
    assert _evidence_item_rows(db_path) == []
    assert _file_evidence_rows(db_path, facts["git_snapshot_id"]) == source_rows


def test_file_evidence_count_mismatch_fails_closed_without_repair(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM git_file_evidence WHERE git_snapshot_id = ? AND ordinal = 2",
            (facts["git_snapshot_id"],),
        )
    remaining_rows = _file_evidence_rows(db_path, facts["git_snapshot_id"])

    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_FILE_EVIDENCE_INCOMPLETE"
    assert _evidence_rows(db_path) == []
    assert _evidence_item_rows(db_path) == []
    assert _file_evidence_rows(db_path, facts["git_snapshot_id"]) == remaining_rows
    assert len(remaining_rows) == 1


def test_zero_file_manifest_is_valid_and_creates_no_items(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    empty_manifest_hash = _stable_hash(
        {"schema_version": "git_file_manifest_v1", "files": []}
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM git_file_evidence WHERE git_snapshot_id = ?",
            (facts["git_snapshot_id"],),
        )
        conn.execute(
            """
            UPDATE git_snapshots
            SET changed_file_count = 0, added_lines = 0, deleted_lines = 0,
                diff_bytes = 0, file_manifest_hash = ?
            WHERE id = ?
            """,
            (empty_manifest_hash, facts["git_snapshot_id"]),
        )

    response = _confirm(api, facts)

    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    assert len(_evidence_rows(db_path)) == 1
    assert _evidence_item_rows(db_path) == []


def test_exact_replay_does_not_backfill_missing_evidence_item(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    first = _confirm(api, facts)
    assert first.status_code == 201, first.text
    original_items = _evidence_item_rows(db_path)
    assert len(original_items) == 2
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM evidence_items WHERE id = ?", (original_items[-1]["id"],))
    partial_items = _evidence_item_rows(db_path)

    replay = _confirm(api, facts)

    assert replay.status_code == 500, replay.text
    assert replay.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_INTEGRITY_INVALID"
    assert _evidence_item_rows(db_path) == partial_items
    assert len(partial_items) == 1


def test_evidence_item_insert_failure_rolls_back_new_snapshot(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    source_rows = _file_evidence_rows(db_path, facts["git_snapshot_id"])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_evidence_item_insert
            BEFORE INSERT ON evidence_items
            BEGIN
                SELECT RAISE(ABORT, 'forced evidence item failure');
            END
            """
        )

    response = _confirm(api, facts)

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "EVIDENCE_SNAPSHOT_SAVE_FAILED",
        "message": "保存 EvidenceSnapshot 失败，数据库原状态未改变。",
    }
    assert "forced evidence item failure" not in response.text
    assert _evidence_rows(db_path) == []
    assert _evidence_item_rows(db_path) == []
    assert _file_evidence_rows(db_path, facts["git_snapshot_id"]) == source_rows


# Structured PRD fixture adapter：保持既有 Core V1 测试的默认零 block 语义，
# 同时让所有新 snapshot 测试都满足正式 structured admission 前置条件。
_BASE_PREPARE = _prepare
_PRD_STRUCTURED_SCHEMA_VERSION = "prd_structured_evidence_v1"
_PRD_STRUCTURED_PARSER_VERSION = "prd-structured-parser-1.0"


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _document_fingerprint(parser_version, schema_version, source_format, source_hash):
    return hashlib.sha256(
        _canonical_bytes(
            {
                "parser_version": parser_version,
                "schema_version": schema_version,
                "source_format": source_format,
                "source_hash": source_hash,
            }
        )
    ).hexdigest()


def _make_structured(*, schema_version=None, parser_version=None, blocks=None):
    schema_version = schema_version or _PRD_STRUCTURED_SCHEMA_VERSION
    parser_version = parser_version or _PRD_STRUCTURED_PARSER_VERSION
    fingerprint = _document_fingerprint(
        parser_version, schema_version, "md", PRD_SOURCE_HASH
    )
    return {
        "schema_version": schema_version,
        "parser_version": parser_version,
        "source_format": "md",
        "source_hash": PRD_SOURCE_HASH,
        "document_fingerprint": fingerprint,
        "blocks": [] if blocks is None else blocks,
    }


def _block(fingerprint, ordinal, *, kind="paragraph", content_hash=None):
    return {
        "ordinal": ordinal,
        "kind": kind,
        "evidence_id": f"prd:block:{fingerprint}:{ordinal}",
        "content_hash": content_hash
        or hashlib.sha256(f"block-{ordinal}".encode("utf-8")).hexdigest(),
        "text": f"block {ordinal}",
        "heading_level": 1 if kind == "heading" else None,
        "table_rows": [["a", "b"]] if kind == "table" else None,
        "page_no": None,
    }


def _structured_target(db_path, structured_path):
    return db_path.parent / "prd" / Path(structured_path)


def _persist_structured(db_path, facts, structured, *, db_overrides=None, raw_bytes=None):
    raw = _canonical_bytes(structured) if raw_bytes is None else raw_bytes
    target = _structured_target(db_path, facts["structured_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    updates = {
        "structured_hash": hashlib.sha256(raw).hexdigest(),
        "structured_schema_version": structured.get("schema_version"),
        "structured_parser_version": structured.get("parser_version"),
        "document_fingerprint": structured.get("document_fingerprint"),
    }
    if db_overrides:
        updates.update(db_overrides)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE prd_versions SET {assignments} WHERE id = ?",
            (*updates.values(), facts["prd_id"]),
        )
    facts["structured"] = structured
    return raw


def _prepare(client, db_path, *, name="Evidence 项目"):
    facts = _BASE_PREPARE(client, db_path, name=name)
    facts["structured_path"] = f"{facts['project_id']}/prd/structured-v1.json"
    structured = _make_structured()
    raw = _canonical_bytes(structured)
    target = _structured_target(db_path, facts["structured_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE prd_versions
            SET structured_path = ?, structured_hash = ?, structured_schema_version = ?,
                structured_parser_version = ?, document_fingerprint = ?
            WHERE id = ?
            """,
            (
                facts["structured_path"],
                hashlib.sha256(raw).hexdigest(),
                structured["schema_version"],
                structured["parser_version"],
                structured["document_fingerprint"],
                facts["prd_id"],
            ),
        )
    facts["structured"] = structured
    return facts


def _assert_structured_invalid(response, db_path):
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_PRD_STRUCTURED_INVALID"
    assert _evidence_rows(db_path) == []
    assert _evidence_item_rows(db_path) == []


def test_prd_structured_blocks_materialize_exact_evidence_items(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    fingerprint = facts["structured"]["document_fingerprint"]
    blocks = [
        _block(fingerprint, 1, kind="heading"),
        _block(fingerprint, 2, kind="paragraph"),
        _block(fingerprint, 3, kind="table"),
    ]
    structured = {**facts["structured"], "blocks": blocks}
    _persist_structured(db_path, facts, structured)

    response = _confirm(api, facts)

    assert response.status_code == 201, response.text
    snapshot_id = response.json()["snapshot"]["id"]
    items = [row for row in _evidence_item_rows(db_path) if row["type"] == "prd_block"]
    assert len(items) == len(blocks) == 3
    assert items == [
        {
            "id": items[index]["id"],
            "snapshot_id": snapshot_id,
            "evidence_id": block["evidence_id"],
            "type": "prd_block",
            "source_ref": f"prd_structured_block:{facts['prd_id']}:{block['ordinal']}",
            "content_hash": block["content_hash"],
            "selected": 1,
            "redaction_state": "pending",
        }
        for index, block in enumerate(blocks)
    ]
    assert all("block 1" not in str(item) for item in items)


@pytest.mark.parametrize(
    "missing_field",
    [
        "structured_path",
        "structured_hash",
        "structured_schema_version",
        "structured_parser_version",
        "document_fingerprint",
    ],
)
def test_structured_metadata_missing_requires_reimport_zero_write(client, missing_field):
    api, db_path = client
    facts = _prepare(api, db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE prd_versions SET {missing_field} = NULL WHERE id = ?",
            (facts["prd_id"],),
        )

    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_PRD_STRUCTURED_REQUIRED"
    assert _evidence_rows(db_path) == []
    assert _evidence_item_rows(db_path) == []


def test_structured_hash_mismatch_is_invalid_zero_write(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    target = _structured_target(db_path, facts["structured_path"])
    target.write_bytes(target.read_bytes() + b"\n")

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


def test_noncanonical_structured_json_is_invalid_even_when_hash_matches(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    pretty = json.dumps(facts["structured"], ensure_ascii=False, indent=2).encode("utf-8")
    target = _structured_target(db_path, facts["structured_path"])
    target.write_bytes(pretty)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE prd_versions SET structured_hash = ? WHERE id = ?",
            (hashlib.sha256(pretty).hexdigest(), facts["prd_id"]),
        )

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


def test_document_fingerprint_closure_mismatch_is_invalid_zero_write(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    structured = {**facts["structured"], "document_fingerprint": "f" * 64}
    raw = _canonical_bytes(structured)
    target = _structured_target(db_path, facts["structured_path"])
    target.write_bytes(raw)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE prd_versions SET structured_hash = ? WHERE id = ?",
            (hashlib.sha256(raw).hexdigest(), facts["prd_id"]),
        )

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


def test_self_consistent_unsupported_schema_is_still_invalid(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    schema = "prd_structured_evidence_v999"
    structured = _make_structured(schema_version=schema)
    _persist_structured(db_path, facts, structured)

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


def test_self_consistent_unsupported_parser_is_still_invalid(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    parser = "prd-structured-parser-999.0"
    structured = _make_structured(parser_version=parser)
    _persist_structured(db_path, facts, structured)

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


def test_self_consistent_unsupported_source_extension_is_still_invalid(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    fingerprint = _document_fingerprint(
        _PRD_STRUCTURED_PARSER_VERSION,
        _PRD_STRUCTURED_SCHEMA_VERSION,
        None,
        PRD_SOURCE_HASH,
    )
    structured = {
        **facts["structured"],
        "source_format": None,
        "document_fingerprint": fingerprint,
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE prd_versions SET original_filename = ? WHERE id = ?",
            ("prd.unsupported", facts["prd_id"]),
        )
    _persist_structured(db_path, facts, structured)

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


@pytest.mark.parametrize("non_object", ["text", ["row"], None])
def test_non_object_block_is_controlled_invalid_after_hash_closure(client, non_object):
    api, db_path = client
    facts = _prepare(api, db_path)
    structured = {**facts["structured"], "blocks": [non_object]}
    _persist_structured(db_path, facts, structured)

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


@pytest.mark.parametrize("violation", ["ordinal", "evidence_id", "duplicate", "content_hash", "kind"])
def test_block_identity_closure_is_fail_closed(client, violation):
    api, db_path = client
    facts = _prepare(api, db_path)
    fingerprint = facts["structured"]["document_fingerprint"]
    blocks = [_block(fingerprint, 1), _block(fingerprint, 2)]
    if violation == "ordinal":
        blocks[1]["ordinal"] = 3
        blocks[1]["evidence_id"] = f"prd:block:{fingerprint}:3"
    elif violation == "evidence_id":
        blocks[0]["evidence_id"] = "prd:block:wrong:1"
    elif violation == "duplicate":
        blocks[1]["evidence_id"] = blocks[0]["evidence_id"]
    elif violation == "content_hash":
        blocks[0]["content_hash"] = "not-a-hash"
    else:
        blocks[0]["kind"] = "unknown"
    _persist_structured(db_path, facts, {**facts["structured"], "blocks": blocks})

    response = _confirm(api, facts)

    _assert_structured_invalid(response, db_path)


def test_structured_path_escape_preserves_prd_storage_escape(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE prd_versions SET structured_path = '../outside.json' WHERE id = ?",
            (facts["prd_id"],),
        )

    response = _confirm(api, facts)

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRD_STORAGE_ESCAPE"
    assert _evidence_rows(db_path) == []
    assert _evidence_item_rows(db_path) == []


def test_evidence_snapshot_path_does_not_rerun_prd_parsers(client, monkeypatch):
    from app import prd as prd_module

    api, db_path = client
    facts = _prepare(api, db_path)

    def parser_must_not_run(*args, **kwargs):
        raise AssertionError("EvidenceSnapshot must not rerun PRD parsers")

    monkeypatch.setattr(prd_module, "parse_prd_bytes", parser_must_not_run)
    monkeypatch.setattr(prd_module, "parse_prd_structured_bytes", parser_must_not_run)

    response = _confirm(api, facts)

    assert response.status_code == 201, response.text


def test_prd_evidence_item_failure_rolls_back_snapshot_and_prior_git_items(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    fingerprint = facts["structured"]["document_fingerprint"]
    _persist_structured(
        db_path,
        facts,
        {**facts["structured"], "blocks": [_block(fingerprint, 1)]},
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_prd_evidence_item_insert
            BEFORE INSERT ON evidence_items
            WHEN NEW.type = 'prd_block'
            BEGIN
                SELECT RAISE(ABORT, 'forced prd evidence item failure');
            END
            """
        )

    response = _confirm(api, facts)

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "EVIDENCE_SNAPSHOT_SAVE_FAILED",
        "message": "保存 EvidenceSnapshot 失败，数据库原状态未改变。",
    }
    assert "forced prd evidence item failure" not in response.text
    assert _evidence_rows(db_path) == []
    assert _evidence_item_rows(db_path) == []


def test_exact_replay_does_not_backfill_missing_prd_evidence_item(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    fingerprint = facts["structured"]["document_fingerprint"]
    _persist_structured(
        db_path,
        facts,
        {
            **facts["structured"],
            "blocks": [_block(fingerprint, 1), _block(fingerprint, 2)],
        },
    )
    first = _confirm(api, facts)
    assert first.status_code == 201, first.text
    original_items = _evidence_item_rows(db_path)
    prd_items = [row for row in original_items if row["type"] == "prd_block"]
    assert len(prd_items) == 2
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM evidence_items WHERE id = ?", (prd_items[-1]["id"],))
    partial_items = _evidence_item_rows(db_path)

    replay = _confirm(api, facts)

    assert replay.status_code == 500, replay.text
    assert replay.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_INTEGRITY_INVALID"
    assert _evidence_item_rows(db_path) == partial_items
    assert len([row for row in partial_items if row["type"] == "prd_block"]) == 1


def _insert_historical_v1_snapshot(db_path, facts):
    now = "2026-08-08T00:00:30+00:00"
    project_config_hash = _stable_hash(
        {"repository_url": REPOSITORY_URL, "branch": "main"}
    )
    legacy_snapshot_hash = hashlib.sha256(b"legacy-v1-snapshot").hexdigest()
    with sqlite3.connect(db_path) as conn:
        snapshot_id = conn.execute(
            """
            INSERT INTO evidence_snapshots (
                schema_version, project_id, git_snapshot_id, analysis_lineage_id,
                branch, from_commit, to_commit, project_repository_url,
                project_config_hash, git_facts_hash, prd_id, prd_source_hash,
                prd_parsed_hash, profile_id, profile_content_hash, snapshot_hash,
                frozen_at
            ) VALUES (
                'evidence_snapshot_core_v1', ?, ?, ?, 'main', ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                facts["project_id"],
                facts["git_snapshot_id"],
                facts["lineage_id"],
                A,
                B,
                REPOSITORY_URL,
                project_config_hash,
                hashlib.sha256(b"legacy-git-facts").hexdigest(),
                facts["prd_id"],
                PRD_SOURCE_HASH,
                PRD_PARSED_HASH,
                facts["profile_id"],
                PROFILE_CONTENT_HASH,
                legacy_snapshot_hash,
                now,
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO evidence_items (
                snapshot_id, evidence_id, type, source_ref, content_hash,
                selected, redaction_state
            ) VALUES (?, ?, 'git_file_fact', ?, ?, 1, 'not_applicable')
            """,
            (
                snapshot_id,
                f"legacy:v1:{snapshot_id}",
                f"legacy-v1:{snapshot_id}",
                hashlib.sha256(b"legacy-item").hexdigest(),
            ),
        )
    return snapshot_id


def test_v2_columns_migrate_old_v1_without_backfill(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    legacy_snapshot_hash = hashlib.sha256(b"legacy-v1-row").hexdigest()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE evidence_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                git_snapshot_id INTEGER NOT NULL,
                analysis_lineage_id INTEGER NOT NULL,
                branch TEXT NOT NULL,
                from_commit TEXT NOT NULL,
                to_commit TEXT NOT NULL,
                project_repository_url TEXT NOT NULL,
                project_config_hash TEXT NOT NULL,
                git_facts_hash TEXT NOT NULL,
                prd_id INTEGER NOT NULL,
                prd_source_hash TEXT NOT NULL,
                prd_parsed_hash TEXT NOT NULL,
                profile_id INTEGER NOT NULL,
                profile_content_hash TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                frozen_at TEXT NOT NULL,
                UNIQUE (
                    project_id, git_snapshot_id, prd_id, profile_id, schema_version
                )
            )
            """
        )
        conn.execute(
            """
            INSERT INTO evidence_snapshots (
                schema_version, project_id, git_snapshot_id, analysis_lineage_id,
                branch, from_commit, to_commit, project_repository_url,
                project_config_hash, git_facts_hash, prd_id, prd_source_hash,
                prd_parsed_hash, profile_id, profile_content_hash, snapshot_hash,
                frozen_at
            ) VALUES (
                'evidence_snapshot_core_v1', 1, 2, 3, 'main', ?, ?,
                'https://example.invalid/legacy.git', ?, ?, 4, ?, ?, 5, ?, ?, ?
            )
            """,
            (
                A,
                B,
                hashlib.sha256(b"legacy-config").hexdigest(),
                hashlib.sha256(b"legacy-git").hexdigest(),
                PRD_SOURCE_HASH,
                PRD_PARSED_HASH,
                PROFILE_CONTENT_HASH,
                legacy_snapshot_hash,
                "2026-08-08T00:00:00+00:00",
            ),
        )

    db.init_db()
    db.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence_snapshots)")}
        row = conn.execute(
            """
            SELECT schema_version, prd_structured_hash, prd_document_fingerprint,
                   snapshot_hash
            FROM evidence_snapshots
            """
        ).fetchone()
    assert {"prd_structured_hash", "prd_document_fingerprint"}.issubset(columns)
    assert row == (
        "evidence_snapshot_core_v1",
        None,
        None,
        legacy_snapshot_hash,
    )


def test_historical_v1_and_new_v2_coexist_without_backfill(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    legacy_id = _insert_historical_v1_snapshot(db_path, facts)
    legacy_before = _evidence_rows(db_path)[0]
    legacy_items_before = [
        row for row in _evidence_item_rows(db_path) if row["snapshot_id"] == legacy_id
    ]

    response = _confirm(api, facts)

    assert response.status_code == 201, response.text
    assert response.json()["created"] is True
    assert response.json()["snapshot"]["schema_version"] == "evidence_snapshot_core_v3"
    rows = _evidence_rows(db_path)
    assert [row["schema_version"] for row in rows] == [
        "evidence_snapshot_core_v1",
        "evidence_snapshot_core_v3",
    ]
    assert rows[0] == legacy_before
    assert rows[0]["prd_structured_hash"] is None
    assert rows[0]["prd_document_fingerprint"] is None
    assert [
        row for row in _evidence_item_rows(db_path) if row["snapshot_id"] == legacy_id
    ] == legacy_items_before


def test_v2_exact_replay_does_not_read_current_structured_artifact(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    first = _confirm(api, facts)
    assert first.status_code == 201, first.text
    frozen_rows = _evidence_rows(db_path)
    frozen_items = _evidence_item_rows(db_path)
    target = _structured_target(db_path, facts["structured_path"])
    target.write_bytes(target.read_bytes() + b"\n")

    replay = _confirm(api, facts)

    assert replay.status_code == 201, replay.text
    assert replay.json()["created"] is False
    assert replay.json()["snapshot"] == first.json()["snapshot"]
    assert _evidence_rows(db_path) == frozen_rows
    assert _evidence_item_rows(db_path) == frozen_items


def test_historical_v1_is_not_replayed_as_v2_and_creation_still_admits_structured(client):
    api, db_path = client
    facts = _prepare(api, db_path)
    legacy_id = _insert_historical_v1_snapshot(db_path, facts)
    target = _structured_target(db_path, facts["structured_path"])
    target.write_bytes(target.read_bytes() + b"\n")

    response = _confirm(api, facts)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EVIDENCE_SNAPSHOT_PRD_STRUCTURED_INVALID"
    rows = _evidence_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["id"] == legacy_id
    assert rows[0]["schema_version"] == "evidence_snapshot_core_v1"
    assert rows[0]["prd_structured_hash"] is None
    assert rows[0]["prd_document_fingerprint"] is None


def test_v2_slice_does_not_create_context_model_or_report_tables(client):
    api, db_path = client
    facts = _prepare(api, db_path)

    response = _confirm(api, facts)

    assert response.status_code == 201, response.text
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "context_manifests" not in tables
    assert "reports" not in tables
