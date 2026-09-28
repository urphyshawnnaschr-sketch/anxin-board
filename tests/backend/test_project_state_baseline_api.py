from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import project_state_baseline_api as baseline


HEAD = "a" * 40
PLAN_HASH = "b" * 64
PRD_HASH = "c" * 64
AUTH_HASH = "d" * 64
GIT_URL = "https://example.invalid/org/repo.git"
GIT_BRANCH = "main"


def _prepared(batch_count=3):
    batches = [{"batch_index": index, "batch_id": f"batch-{index}"} for index in range(batch_count)]
    requests = [
        {
            "batch_index": index,
            "wire_bytes": 1200 + index,
            "evidence_count": 2,
            "module_ids": [f"module-{index}"],
        }
        for index in range(batch_count)
    ]
    plan = {
        "schema_version": "project_profile_v2",
        "project_summary": "safe",
        "planned_modules": [
            {
                "client_id": "login",
                "name": "登录",
                "description": "",
                "prd_refs": ["prd-a"],
                "requirements": ["登录"],
                "exclusions": [],
            }
        ],
        "implementation_mappings": [],
        "unplanned_code_features": [],
        "domain_glossary": [],
        "exclude_patterns": [],
        "notes": "",
    }
    return {
        "project_id": 9,
        "prd_id": 7,
        "prd_source_hash": PRD_HASH,
        "plan_profile_id": 42,
        "plan_content_hash": PLAN_HASH,
        "plan": plan,
        "exact_head": HEAD,
        "git_url": GIT_URL,
        "git_branch": GIT_BRANCH,
        "batches": batches,
        "adapter": object(),
        "execution_plan": {"identity": "frozen"},
        "summary": {
            "provider": "deepseek",
            "model_id": "deepseek-test",
            "model_version": "deepseek-test",
            "total_wire_bytes": sum(item["wire_bytes"] for item in requests),
            "tracked_files": 123,
            "safe_text_bytes": 456789,
            "coverage": {"safe_text": 120, "binary_hashed": 3},
            "requests": requests,
        },
    }


def _candidate_content():
    return {
        "schema_version": "project_profile_v2",
        "project_summary": "safe",
        "planned_modules": [
            {
                "client_id": "login",
                "name": "登录",
                "description": "登录模块",
                "prd_refs": ["prd-a"],
                "requirements": ["用户名密码登录"],
                "exclusions": [],
            }
        ],
        "implementation_mappings": [
            {
                "planned_module_id": "login",
                "status": "implemented",
                "exact_head": HEAD,
                "evidence_ids": ["repo-code-login"],
                "paths": [{"type": "backend", "pattern": "app/login.py", "required": True, "note": ""}],
                "rationale": "exact-HEAD code evidence",
            }
        ],
        "unplanned_code_features": [],
        "domain_glossary": [],
        "exclude_patterns": [],
        "notes": "",
    }


def _request(idempotency="idem-baseline-123456"):
    return SimpleNamespace(headers={"local-idempotency-key": idempotency})


def _freeze_identity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(baseline, "_freeze_repository_scope", lambda value: value)


def test_empty_analysis_maps_to_explicit_quality_failure():
    exc = baseline._map_batch_error(baseline.BatchError("ANALYSIS_EMPTY_NO_CODE_EVIDENCE"), phase="execute")
    assert exc.detail["code"] == "PROJECT_STATE_BASELINE_ANALYSIS_EMPTY"
    assert "没有为任何计划模块返回可核对的代码证据" in exc.detail["message"]


def test_preflight_is_local_only_and_exposes_safe_counts(monkeypatch: pytest.MonkeyPatch):
    prepared = _prepared(4)
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(baseline, "prepare_product_execution", lambda project_id, profile_id: prepared)
    _freeze_identity(monkeypatch)
    monkeypatch.setattr(
        baseline.profile_generation,
        "_read_provider_credential",
        lambda: pytest.fail("preflight must not read API key"),
    )

    result = baseline.preflight_project_state_baseline(
        9,
        baseline.BaselinePreflightPayload(plan_profile_id=42),
        _request(),
    )

    summary = result["preflight"]
    assert result["status"] == "ready"
    assert len(summary["preflight_identity_hash"]) == 64
    assert summary["preflight_identity_hash"] == baseline._preflight_identity_hash(prepared)
    assert summary["exact_head"] == HEAD
    assert summary["git_branch"] == GIT_BRANCH
    assert summary["tracked_files"] == 123
    assert summary["safe_text_bytes"] == 456789
    assert summary["batch_count"] == 4
    assert summary["provider_calls"] == 0
    assert summary["credential_read"] is False
    assert summary["candidate_ready"] is False
    assert summary["auto_confirm"] is False
    assert all(set(item) == {"batch_index", "wire_bytes", "evidence_count", "module_count"} for item in summary["batches"])



def test_preflight_uses_explicit_non_idempotent_local_write_guard(monkeypatch: pytest.MonkeyPatch):
    prepared = _prepared(1)
    calls = []

    def strict_guard(_request, *, require_idempotency_key):
        calls.append(require_idempotency_key)

    monkeypatch.setattr(baseline, "require_local_write_request", strict_guard)
    monkeypatch.setattr(baseline, "prepare_product_execution", lambda project_id, profile_id: prepared)
    _freeze_identity(monkeypatch)
    monkeypatch.setattr(
        baseline.profile_generation,
        "_read_provider_credential",
        lambda: pytest.fail("preflight must not read API key"),
    )

    result = baseline.preflight_project_state_baseline(
        9,
        baseline.BaselinePreflightPayload(plan_profile_id=42),
        _request(),
    )

    assert result["status"] == "ready"
    assert calls == [False]
    assert result["preflight"]["provider_calls"] == 0
    assert result["preflight"]["credential_read"] is False


def test_preflight_late_runtime_error_is_structured_and_never_reads_key(monkeypatch: pytest.MonkeyPatch):
    prepared = _prepared(1)
    monkeypatch.setattr(
        baseline,
        "require_local_write_request",
        lambda _request, *, require_idempotency_key: None,
    )
    monkeypatch.setattr(baseline, "_prepare_product_scope", lambda *_args, **_kwargs: prepared)
    monkeypatch.setattr(baseline, "_public_summary", lambda _prepared: (_ for _ in ()).throw(TypeError("late-summary-failure")))
    monkeypatch.setattr(
        baseline.profile_generation,
        "_read_provider_credential",
        lambda: pytest.fail("preflight must not read API key"),
    )

    with pytest.raises(HTTPException) as exc:
        baseline.preflight_project_state_baseline(
            9,
            baseline.BaselinePreflightPayload(plan_profile_id=42),
            _request(),
        )

    assert exc.value.status_code == 500
    assert exc.value.detail["code"] == "PROJECT_STATE_BASELINE_PREFLIGHT_RUNTIME_FAILED"
    assert "未调用模型" in exc.value.detail["message"]

def test_execute_runs_all_batches_under_one_authorization_and_reads_key_once(monkeypatch: pytest.MonkeyPatch):
    prepared = _prepared(3)
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(baseline, "prepare_product_execution", lambda project_id, profile_id: prepared)
    _freeze_identity(monkeypatch)
    monkeypatch.setattr(baseline, "get_connection", lambda: nullcontext(object()))
    authorizations = []

    def authorize(_conn, **kwargs):
        authorizations.append(kwargs)
        return {"authorization_hash": AUTH_HASH}

    monkeypatch.setattr(baseline, "record_authorization", authorize)
    credential_reads = []
    monkeypatch.setattr(
        baseline.profile_generation,
        "_read_provider_credential",
        lambda: credential_reads.append(1) or "secret-in-memory",
    )
    monkeypatch.setattr(
        baseline,
        "_current_state",
        lambda _project_id, **_kwargs: {
            "exact_head": HEAD,
            "prd_id": 7,
            "prd_source_hash": PRD_HASH,
            "plan_profile_id": 42,
            "plan_content_hash": PLAN_HASH,
        },
    )
    dispatched = []

    def dispatch(_conn, batch, **kwargs):
        assert kwargs["credential_reader"]() == "secret-in-memory"
        assert kwargs["current_state"]()["exact_head"] == HEAD
        dispatched.append(batch["batch_index"])
        return {"status": "succeeded", "batch_id": batch["batch_id"]}

    monkeypatch.setattr(baseline, "dispatch_authorized_batch", dispatch)
    aggregates = []
    monkeypatch.setattr(
        baseline,
        "aggregate_authorized_candidate",
        lambda _conn, batches, plan, **kwargs: aggregates.append((batches, plan, kwargs)) or {
            "status": "candidate",
            "content": _candidate_content(),
            "content_hash": "e" * 64,
        },
    )
    promotions = []
    monkeypatch.setattr(
        baseline,
        "_promote_generated_candidate",
        lambda **kwargs: promotions.append(kwargs) or {"id": 88, "status": "candidate"},
    )

    result = baseline.execute_project_state_baseline(
        9,
        baseline.BaselineExecutePayload(
            plan_profile_id=42,
            authorized=True,
            authorization_nonce="idem-baseline-123456",
            preflight_identity_hash=baseline._preflight_identity_hash(prepared),
        ),
        _request(),
    )

    assert result["status"] == "candidate_ready"
    assert result["profile"] == {"id": 88, "status": "candidate"}
    assert result["batch_count"] == 3
    assert result["git_branch"] == GIT_BRANCH
    assert result["auto_confirm"] is False
    assert dispatched == [0, 1, 2]
    assert credential_reads == [1]
    assert len(authorizations) == 1
    assert authorizations[0]["authorization_nonce"] == "idem-baseline-123456"
    assert authorizations[0]["authorized"] is True
    assert len(aggregates) == 1
    assert len(promotions) == 1
    assert "secret-in-memory" not in repr(result)


def test_execute_rejects_browser_and_human_authorization_identity_mismatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        baseline,
        "prepare_product_execution",
        lambda *_args, **_kwargs: pytest.fail("scope preparation must not run after identity mismatch"),
    )
    with pytest.raises(baseline.HTTPException) as exc:
        baseline.execute_project_state_baseline(
            9,
            baseline.BaselineExecutePayload(
                plan_profile_id=42,
                authorized=True,
                authorization_nonce="human-authorization-1",
                preflight_identity_hash="0" * 64,
            ),
            _request("different-browser-id"),
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "PROJECT_STATE_BASELINE_AUTHORIZATION_IDENTITY_MISMATCH"


def test_unknown_batch_stops_immediately_and_never_promotes_partial_candidate(monkeypatch: pytest.MonkeyPatch):
    prepared = _prepared(4)
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(baseline, "prepare_product_execution", lambda project_id, profile_id: prepared)
    _freeze_identity(monkeypatch)
    monkeypatch.setattr(baseline, "get_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(baseline, "record_authorization", lambda *_args, **_kwargs: {"authorization_hash": AUTH_HASH})
    monkeypatch.setattr(baseline.profile_generation, "_read_provider_credential", lambda: "secret")
    monkeypatch.setattr(baseline, "_current_state", lambda _project_id, **_kwargs: {})
    dispatched = []

    def dispatch(_conn, batch, **_kwargs):
        dispatched.append(batch["batch_index"])
        return {"status": "unknown", "error_code": "SOURCE_OR_MODEL_CHANGED_AFTER_PROVIDER_DISPATCH"} if batch["batch_index"] == 1 else {"status": "succeeded"}

    monkeypatch.setattr(baseline, "dispatch_authorized_batch", dispatch)
    monkeypatch.setattr(
        baseline,
        "aggregate_authorized_candidate",
        lambda *_args, **_kwargs: pytest.fail("UNKNOWN must not aggregate"),
    )
    monkeypatch.setattr(
        baseline,
        "_promote_generated_candidate",
        lambda **_kwargs: pytest.fail("UNKNOWN must not promote"),
    )

    with pytest.raises(baseline.HTTPException) as exc:
        baseline.execute_project_state_baseline(
            9,
            baseline.BaselineExecutePayload(
                plan_profile_id=42,
                authorized=True,
                authorization_nonce="idem-baseline-unknown",
                preflight_identity_hash=baseline._preflight_identity_hash(prepared),
            ),
            _request("idem-baseline-unknown"),
        )

    assert exc.value.detail["code"] == "PROJECT_STATE_BASELINE_BATCH_UNKNOWN"
    assert exc.value.detail["current"]["error_code"] == "SOURCE_OR_MODEL_CHANGED_AFTER_PROVIDER_DISPATCH"
    assert dispatched == [0, 1]


def test_known_failed_batch_does_not_create_partial_candidate(monkeypatch: pytest.MonkeyPatch):
    prepared = _prepared(2)
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(baseline, "prepare_product_execution", lambda project_id, profile_id: prepared)
    _freeze_identity(monkeypatch)
    monkeypatch.setattr(baseline, "get_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(baseline, "record_authorization", lambda *_args, **_kwargs: {"authorization_hash": AUTH_HASH})
    monkeypatch.setattr(baseline.profile_generation, "_read_provider_credential", lambda: "secret")
    monkeypatch.setattr(baseline, "_current_state", lambda _project_id, **_kwargs: {})
    monkeypatch.setattr(
        baseline,
        "dispatch_authorized_batch",
        lambda _conn, batch, **_kwargs: {"status": "failed_after_send", "batch_id": batch["batch_id"], "error_code": "PROVIDER_RESULT_MAPPING_INVALID"},
    )
    monkeypatch.setattr(
        baseline,
        "aggregate_authorized_candidate",
        lambda *_args, **_kwargs: pytest.fail("failed batch must not aggregate"),
    )
    monkeypatch.setattr(
        baseline,
        "_promote_generated_candidate",
        lambda **_kwargs: pytest.fail("failed batch must not promote"),
    )

    with pytest.raises(baseline.HTTPException) as exc:
        baseline.execute_project_state_baseline(
            9,
            baseline.BaselineExecutePayload(
                plan_profile_id=42,
                authorized=True,
                authorization_nonce="idem-baseline-failed",
                preflight_identity_hash=baseline._preflight_identity_hash(prepared),
            ),
            _request("idem-baseline-failed"),
        )
    assert exc.value.detail["code"] == "PROJECT_STATE_BASELINE_BATCH_FAILED"
    assert exc.value.detail["current"]["error_code"] == "PROVIDER_RESULT_MAPPING_INVALID"



def test_execute_rejects_scope_that_changed_after_human_preflight(monkeypatch: pytest.MonkeyPatch):
    before = _prepared(1)
    after = _prepared(1)
    after["exact_head"] = "f" * 40
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(baseline, "prepare_product_execution", lambda *_args, **_kwargs: after)
    _freeze_identity(monkeypatch)
    monkeypatch.setattr(baseline, "record_authorization", lambda *_args, **_kwargs: pytest.fail("scope drift must stop before authorization"))
    monkeypatch.setattr(baseline.profile_generation, "_read_provider_credential", lambda: pytest.fail("scope drift must stop before API key read"))
    with pytest.raises(HTTPException) as caught:
        baseline.execute_project_state_baseline(
            9,
            baseline.BaselineExecutePayload(
                plan_profile_id=42,
                authorized=True,
                authorization_nonce="idem-preflight-scope-change",
                preflight_identity_hash=baseline._preflight_identity_hash(before),
            ),
            _request("idem-preflight-scope-change"),
        )
    assert caught.value.detail["code"] == "PROJECT_STATE_BASELINE_PREFLIGHT_SCOPE_CHANGED"
    assert "未读取 API Key" in caught.value.detail["message"]
    assert "未发送模型请求" in caught.value.detail["message"]

def _db_connection(database):
    @contextmanager
    def connection():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    return connection


def _create_product_tables(connection):
    url_hash = hashlib.sha256(GIT_URL.encode("utf-8")).hexdigest()
    with connection() as conn:
        conn.executescript(
            f"""
            CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT, git_url TEXT, branch TEXT);
            INSERT INTO projects(id, name, git_url, branch) VALUES(9, 'demo', '{GIT_URL}', '{GIT_BRANCH}');
            CREATE TABLE project_git_connections(
                project_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                checked_url_hash TEXT,
                checked_branch TEXT,
                remote_head TEXT
            );
            INSERT INTO project_git_connections(project_id,status,checked_url_hash,checked_branch,remote_head)
            VALUES(9,'connected','{url_hash}','{GIT_BRANCH}','{HEAD}');
            CREATE TABLE project_profiles(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                source_prd_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                content_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                edit_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_by TEXT,
                confirmed_at TEXT
            );
            CREATE TABLE analysis_lineages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL,
                branch TEXT NOT NULL,
                baseline_commit TEXT NOT NULL,
                status TEXT NOT NULL,
                break_reason TEXT,
                source_git_checked_at TEXT,
                created_at TEXT NOT NULL,
                closed_at TEXT
            );
            """
        )
        conn.commit()


def test_promote_generated_candidate_is_idempotent_for_same_authorization(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)

    monkeypatch.setattr(baseline, "get_connection", connection)
    monkeypatch.setattr(baseline, "_read_active_prd_id", lambda _conn, _project_id: 7)
    monkeypatch.setattr(baseline, "_require_project_in_tx", lambda _conn, _project_id: None)
    monkeypatch.setattr(baseline, "read_current_confirmed_project_profile", lambda _project_id, conn=None: {
        "id": 42,
        "project_id": 9,
        "version_no": 1,
        "source_prd_id": 7,
        "status": "confirmed",
        "content_hash": PLAN_HASH,
        "content": _prepared(1)["plan"],
    })
    prepared = _prepared(1)

    first = baseline._promote_generated_candidate(
        project_id=9,
        prepared=prepared,
        authorization_hash=AUTH_HASH,
        content=_candidate_content(),
    )
    second = baseline._promote_generated_candidate(
        project_id=9,
        prepared=prepared,
        authorization_hash=AUTH_HASH,
        content=_candidate_content(),
    )

    assert first["id"] == second["id"]
    assert first["status"] == second["status"] == "candidate"
    with connection() as conn:
        assert conn.execute("SELECT count(*) FROM project_profiles").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM project_state_baseline_candidates").fetchone()[0] == 1


def test_confirm_baseline_candidate_binds_exact_analyzed_head_as_new_increment_origin(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-confirm.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    monkeypatch.setattr(baseline, "_read_active_prd_id", lambda _conn, _project_id: 7)
    baseline.ensure_project_state_baseline_schema()

    model = baseline.ProjectProfileV2Content.model_validate(_candidate_content())
    canonical, content_hash = baseline._canonicalize(model)
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO project_profiles(
                project_id,version_no,source_prd_id,status,content_json,content_hash,
                edit_version,created_at,updated_at
            ) VALUES(9,2,7,'candidate',?,?,1,'2026-09-20T00:00:00+00:00','2026-09-20T00:00:00+00:00')
            """,
            (canonical, content_hash),
        )
        profile_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO project_state_baseline_candidates(
                authorization_hash,schema_version,project_id,exact_head,git_url,git_branch,
                source_profile_id,source_profile_content_hash,source_prd_id,
                generated_content_hash,profile_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                AUTH_HASH,
                baseline.SCHEMA_VERSION,
                9,
                HEAD,
                GIT_URL,
                GIT_BRANCH,
                42,
                PLAN_HASH,
                7,
                content_hash,
                profile_id,
                "2026-09-20T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO analysis_lineages(
                project_id,sequence_no,branch,baseline_commit,status,break_reason,
                source_git_checked_at,created_at,closed_at
            ) VALUES(9,1,'main',?,'active',NULL,'old-check','2026-09-19T00:00:00+00:00',NULL)
            """,
            ("1" * 40,),
        )
        conn.commit()

    confirmed = baseline._confirm_baseline_candidate(
        profile_id,
        baseline.ProfileConfirmPayload(edit_version=1, confirmed_by="local"),
    )

    assert confirmed is not None
    assert confirmed["status"] == "confirmed"
    with connection() as conn:
        active = conn.execute(
            "SELECT branch,baseline_commit,status FROM analysis_lineages WHERE project_id=9 AND status='active'"
        ).fetchone()
        closed = conn.execute(
            "SELECT break_reason,status FROM analysis_lineages WHERE project_id=9 AND sequence_no=1"
        ).fetchone()
        assert active["branch"] == GIT_BRANCH
        assert active["baseline_commit"] == HEAD
        assert closed["status"] == "closed"
        assert closed["break_reason"] == "project_state_baseline_rebound"



def test_confirm_baseline_candidate_rejects_plan_edit_that_dropped_mapping(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-stale-mapping.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    monkeypatch.setattr(baseline, "_read_active_prd_id", lambda _conn, _project_id: 7)
    baseline.ensure_project_state_baseline_schema()

    stale = _candidate_content()
    stale["implementation_mappings"] = []
    model = baseline.ProjectProfileV2Content.model_validate(stale)
    canonical, content_hash = baseline._canonicalize(model)
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO project_profiles(
                project_id,version_no,source_prd_id,status,content_json,content_hash,
                edit_version,created_at,updated_at
            ) VALUES(9,2,7,'candidate',?,?,2,'2026-09-20T00:00:00+00:00','2026-09-20T00:01:00+00:00')
            """,
            (canonical, content_hash),
        )
        profile_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO project_state_baseline_candidates(
                authorization_hash,schema_version,project_id,exact_head,git_url,git_branch,
                source_profile_id,source_profile_content_hash,source_prd_id,
                generated_content_hash,profile_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                AUTH_HASH,
                baseline.SCHEMA_VERSION,
                9,
                HEAD,
                GIT_URL,
                GIT_BRANCH,
                42,
                PLAN_HASH,
                7,
                "e" * 64,
                profile_id,
                "2026-09-20T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO analysis_lineages(
                project_id,sequence_no,branch,baseline_commit,status,break_reason,
                source_git_checked_at,created_at,closed_at
            ) VALUES(9,1,'main',?,'active',NULL,'old-check','2026-09-19T00:00:00+00:00',NULL)
            """,
            ("1" * 40,),
        )
        conn.commit()

    with pytest.raises(baseline.HTTPException) as exc:
        baseline._confirm_baseline_candidate(
            profile_id,
            baseline.ProfileConfirmPayload(edit_version=2, confirmed_by="local"),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "PROJECT_STATE_BASELINE_RECONCILIATION_STALE"

    with connection() as conn:
        candidate = conn.execute("SELECT status FROM project_profiles WHERE id=?", (profile_id,)).fetchone()
        active = conn.execute(
            "SELECT baseline_commit,status FROM analysis_lineages WHERE project_id=9 AND status='active'"
        ).fetchone()
        assert candidate["status"] == "candidate"
        assert active["baseline_commit"] == "1" * 40
        assert active["status"] == "active"

def _insert_confirmed_profile(connection, content: dict, *, version_no: int = 1, source_prd_id: int = 7) -> int:
    model = baseline.project_profiles_module.parse_profile_content(content)
    canonical, content_hash = baseline._canonicalize(model)
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO project_profiles(
                project_id,version_no,source_prd_id,status,content_json,content_hash,
                edit_version,created_at,updated_at,confirmed_by,confirmed_at
            ) VALUES(9,?,?, 'confirmed',?,?,2,'2026-09-20T00:00:00+00:00','2026-09-20T00:00:00+00:00','local','2026-09-20T00:00:00+00:00')
            """,
            (version_no, source_prd_id, canonical, content_hash),
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_baseline_status_requires_upgrade_for_legacy_confirmed_profile(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-status-legacy.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    baseline.ensure_project_state_baseline_schema()
    legacy = {
        "schema_version": "project_profile_manual_v1",
        "project_summary": "legacy",
        "modules": [{
            "client_id": "login", "name": "登录", "description": "", "prd_refs": ["prd-a"],
            "requirements": ["登录"], "paths": [{"type": "backend", "pattern": "app/login.py", "required": True, "note": ""}],
            "exclusions": []
        }],
        "domain_glossary": [], "exclude_patterns": [], "notes": ""
    }
    _insert_confirmed_profile(connection, legacy)
    result = baseline.get_project_state_baseline_status(9)
    assert result["status"] == "upgrade_required"
    assert result["has_confirmed_baseline"] is False


def test_baseline_status_v2_without_provenance_is_missing(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-status-missing.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    baseline.ensure_project_state_baseline_schema()
    _insert_confirmed_profile(connection, _candidate_content())
    result = baseline.get_project_state_baseline_status(9)
    assert result["status"] == "missing"
    assert result["has_confirmed_baseline"] is False


def test_baseline_status_established_requires_provenance_coverage_and_active_lineage(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-status-established.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    profile_id = _insert_confirmed_profile(connection, _candidate_content())
    baseline.ensure_project_state_baseline_schema()
    with connection() as conn:
        content_hash = conn.execute("SELECT content_hash FROM project_profiles WHERE id=?", (profile_id,)).fetchone()[0]
        conn.execute(
            """
            INSERT INTO project_state_baseline_candidates(
                authorization_hash,schema_version,project_id,exact_head,git_url,git_branch,
                source_profile_id,source_profile_content_hash,source_prd_id,
                generated_content_hash,profile_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (AUTH_HASH, baseline.SCHEMA_VERSION, 9, HEAD, GIT_URL, GIT_BRANCH, 42, PLAN_HASH, 7, content_hash, profile_id, '2026-09-20T00:00:00+00:00'),
        )
        conn.execute(
            """
            INSERT INTO analysis_lineages(project_id,sequence_no,branch,baseline_commit,status,break_reason,source_git_checked_at,created_at,closed_at)
            VALUES(9,1,?,?, 'active',NULL,'checked','2026-09-20T00:00:00+00:00',NULL)
            """,
            (GIT_BRANCH, HEAD),
        )
        conn.commit()
    result = baseline.get_project_state_baseline_status(9)
    assert result["status"] == "established"
    assert result["has_confirmed_baseline"] is True
    assert result["exact_head"] == HEAD


def test_baseline_status_surfaces_pending_full_analysis_candidate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-status-pending.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    _insert_confirmed_profile(connection, _prepared(1)["plan"])
    baseline.ensure_project_state_baseline_schema()
    model = baseline.ProjectProfileV2Content.model_validate(_candidate_content())
    canonical, content_hash = baseline._canonicalize(model)
    with connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO project_profiles(project_id,version_no,source_prd_id,status,content_json,content_hash,edit_version,created_at,updated_at)
            VALUES(9,2,7,'candidate',?,?,1,'2026-09-20T01:00:00+00:00','2026-09-20T01:00:00+00:00')
            """,
            (canonical, content_hash),
        )
        candidate_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO project_state_baseline_candidates(
                authorization_hash,schema_version,project_id,exact_head,git_url,git_branch,
                source_profile_id,source_profile_content_hash,source_prd_id,generated_content_hash,profile_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (AUTH_HASH, baseline.SCHEMA_VERSION, 9, HEAD, GIT_URL, GIT_BRANCH, 1, PLAN_HASH, 7, content_hash, candidate_id, '2026-09-20T01:00:00+00:00'),
        )
        conn.commit()
    result = baseline.get_project_state_baseline_status(9)
    assert result["status"] == "candidate_pending"
    assert result["profile_id"] == candidate_id

def _install_established_baseline(connection, profile_id: int, content_hash: str) -> None:
    baseline.ensure_project_state_baseline_schema()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO project_state_baseline_candidates(
                authorization_hash,schema_version,project_id,exact_head,git_url,git_branch,
                source_profile_id,source_profile_content_hash,source_prd_id,
                generated_content_hash,profile_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (AUTH_HASH, baseline.SCHEMA_VERSION, 9, HEAD, GIT_URL, GIT_BRANCH, 42, PLAN_HASH, 7, content_hash, profile_id, '2026-09-20T00:00:00+00:00'),
        )
        conn.execute(
            """
            INSERT INTO analysis_lineages(project_id,sequence_no,branch,baseline_commit,status,break_reason,source_git_checked_at,created_at,closed_at)
            VALUES(9,1,?,?, 'active',NULL,'checked','2026-09-20T00:00:00+00:00',NULL)
            """,
            (GIT_BRANCH, HEAD),
        )
        conn.commit()


def _insert_followup_confirmed_profile(connection, content: dict, *, version_no: int) -> int:
    model = baseline.ProjectProfileV2Content.model_validate(content)
    canonical, content_hash = baseline._canonicalize(model)
    with connection() as conn:
        conn.execute("UPDATE project_profiles SET status='superseded' WHERE project_id=9 AND status='confirmed'")
        cursor = conn.execute(
            """
            INSERT INTO project_profiles(
                project_id,version_no,source_prd_id,status,content_json,content_hash,
                edit_version,created_at,updated_at,confirmed_by,confirmed_at
            ) VALUES(9,?,7,'confirmed',?,?,2,'2026-09-21T00:00:00+00:00','2026-09-21T00:00:00+00:00','local','2026-09-21T00:00:00+00:00')
            """,
            (version_no, canonical, content_hash),
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_baseline_status_allows_summary_only_followup_profile_without_reanalysis(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-status-summary-only.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    original = _candidate_content()
    profile_id = _insert_confirmed_profile(connection, original)
    with connection() as conn:
        content_hash = conn.execute("SELECT content_hash FROM project_profiles WHERE id=?", (profile_id,)).fetchone()[0]
    _install_established_baseline(connection, profile_id, content_hash)

    followup = _candidate_content()
    followup["project_summary"] = "summary text changed only"
    followup_id = _insert_followup_confirmed_profile(connection, followup, version_no=2)

    monkeypatch.setattr(
        baseline,
        "ensure_project_state_baseline_schema",
        lambda: pytest.fail("status GET must remain read-only and must not ensure schema"),
    )
    result = baseline.get_project_state_baseline_status(9)
    assert result["status"] == "established"
    assert result["has_confirmed_baseline"] is True
    assert result["profile_id"] == followup_id


def test_baseline_status_rejects_fabricated_followup_mapping_with_changed_plan_semantics(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "baseline-status-fabricated.sqlite3"
    connection = _db_connection(database)
    _create_product_tables(connection)
    monkeypatch.setattr(baseline, "get_connection", connection)
    original = _candidate_content()
    profile_id = _insert_confirmed_profile(connection, original)
    with connection() as conn:
        content_hash = conn.execute("SELECT content_hash FROM project_profiles WHERE id=?", (profile_id,)).fetchone()[0]
    _install_established_baseline(connection, profile_id, content_hash)

    fabricated = _candidate_content()
    fabricated["planned_modules"][0]["requirements"] = ["changed plan semantics"]
    followup_id = _insert_followup_confirmed_profile(connection, fabricated, version_no=2)

    result = baseline.get_project_state_baseline_status(9)
    assert result["status"] == "missing"
    assert result["has_confirmed_baseline"] is False
    assert result["profile_id"] == followup_id


def test_preflight_classifies_git_workspace_busy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        baseline,
        "prepare_product_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(baseline.GitOperationInProgress()),
    )
    with pytest.raises(HTTPException) as caught:
        baseline.preflight_project_state_baseline(9, baseline.BaselinePreflightPayload(plan_profile_id=42), _request())
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROJECT_STATE_BASELINE_GIT_BUSY"
    assert "未调用模型" in caught.value.detail["message"]


def test_preflight_classifies_git_workspace_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        baseline,
        "prepare_product_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(baseline.GitClientError("GIT_WORKSPACE_CONFLICT")),
    )
    with pytest.raises(HTTPException) as caught:
        baseline.preflight_project_state_baseline(9, baseline.BaselinePreflightPayload(plan_profile_id=42), _request())
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "PROJECT_STATE_BASELINE_GIT_WORKSPACE_FAILED"
    assert "工作区" in caught.value.detail["message"]


def test_preflight_unexpected_runtime_error_is_safe_structured_500(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(baseline, "require_local_write_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        baseline,
        "prepare_product_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("SYNTHETIC_PRIVATE_DETAIL")),
    )
    with pytest.raises(HTTPException) as caught:
        baseline.preflight_project_state_baseline(9, baseline.BaselinePreflightPayload(plan_profile_id=42), _request())
    assert caught.value.status_code == 500
    assert caught.value.detail["code"] == "PROJECT_STATE_BASELINE_PREPARE_RUNTIME_FAILED"
    assert "SYNTHETIC_PRIVATE_DETAIL" not in str(caught.value.detail)
    assert "未调用模型" in caught.value.detail["message"]
