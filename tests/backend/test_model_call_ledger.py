"""Model Call Ledger V1：durable preparation identity、幂等、并发恢复与安全边界测试。"""

from __future__ import annotations

import builtins
from copy import deepcopy
from pathlib import Path
import socket
import sqlite3
import sys

import pytest
from fastapi import HTTPException

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = Path(__file__).resolve().parents[2] / "apps" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from app import ai_contract_validation, context_resolver, db, model_call_ledger  # noqa: E402
from app.git_client import GitClient  # noqa: E402
import test_context_candidate_set as candidate_tests  # noqa: E402


QUALIFICATION = {
    "provider": "provider-a",
    "model_id": "model-a",
    "model_version": "2026-08",
    "rule_version": "rules/1.0",
    "output_schema_version": "daily-report/1.0",
    "benchmark_sample_pack_version": "samples/1.0",
    "qualification_status": "qualified",
}
AUTHORIZATION = {
    "provider": "provider-a",
    "authorized": True,
    "valid": True,
}
EXPECTED_COLUMNS = [
    "id",
    "schema_version",
    "project_id",
    "local_task_id",
    "call_prepare_key",
    "snapshot_id",
    "snapshot_hash",
    "candidate_set_hash",
    "task_type",
    "provider",
    "model_id",
    "model_version",
    "rule_version",
    "output_schema_version",
    "benchmark_sample_pack_version",
    "qualification_status",
    "qualification_hash",
    "authorization_provider",
    "authorization_authorized",
    "authorization_valid",
    "authorization_hash",
    "created_at",
    "call_identity_hash",
]
FUTURE_FIELDS = {
    "status",
    "provider_request_id",
    "attempted_at",
    "completed_at",
}


@pytest.fixture()
def ledger_state(tmp_path, monkeypatch):
    return candidate_tests._make_state(tmp_path, monkeypatch)


def _row_count(state: dict) -> int:
    with sqlite3.connect(state["db_path"]) as conn:
        return conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]


def _rows_for_key(state: dict, key: str) -> list[sqlite3.Row]:
    with sqlite3.connect(state["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM model_calls WHERE project_id = ? AND call_prepare_key = ? ORDER BY id",
            (state["project_id"], key),
        ).fetchall()


def _persisted_row(state: dict, model_call_id: int) -> dict:
    with sqlite3.connect(state["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM model_calls WHERE id = ?", (model_call_id,)).fetchone()
        assert row is not None
        return dict(row)


def _code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return exc.value.detail["code"]


def _prepare(
    state: dict,
    *,
    local_task_id: str = "task-1",
    call_prepare_key: str = "prepare-1",
    snapshot_id: int | None = None,
    task_type: str = "daily_report_generate",
    provider: str = "provider-a",
    model_id: str = "model-a",
    model_version: str = "2026-08",
    rule_version: str = "rules/1.0",
    output_schema_version: str = "daily-report/1.0",
    benchmark_sample_pack_version: str = "samples/1.0",
    qualification_record=None,
    data_sending_authorization=None,
):
    qualification = (
        qualification_record
        if qualification_record is not None
        else {
            "provider": provider,
            "model_id": model_id,
            "model_version": model_version,
            "rule_version": rule_version,
            "output_schema_version": output_schema_version,
            "benchmark_sample_pack_version": benchmark_sample_pack_version,
            "qualification_status": "qualified",
        }
    )
    authorization = (
        data_sending_authorization
        if data_sending_authorization is not None
        else {"provider": provider, "authorized": True, "valid": True}
    )
    return model_call_ledger.prepare_model_call(
        local_task_id=local_task_id,
        call_prepare_key=call_prepare_key,
        snapshot_id=state["snapshot_id"] if snapshot_id is None else snapshot_id,
        task_type=task_type,
        provider=provider,
        model_id=model_id,
        model_version=model_version,
        rule_version=rule_version,
        output_schema_version=output_schema_version,
        benchmark_sample_pack_version=benchmark_sample_pack_version,
        qualification_record=qualification,
        data_sending_authorization=authorization,
    )


def _insert_committed_winner(state: dict, row: dict[str, object], insert_fn) -> int:
    """Commit through a distinct connection so loser rollback cannot erase the winner."""
    with sqlite3.connect(state["db_path"]) as winner_conn:
        winner_conn.row_factory = sqlite3.Row
        return insert_fn(winner_conn, row)


def _assert_http_code(expected: str, callable_):
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert _code(caught) == expected
    return caught


def test_t01_real_happy_path_deterministic_identity_and_single_candidate_closure(
    ledger_state, monkeypatch
):
    original = model_call_ledger.build_context_candidate_set
    calls = 0

    def counted(snapshot_id):
        nonlocal calls
        calls += 1
        return original(snapshot_id)

    monkeypatch.setattr(model_call_ledger, "build_context_candidate_set", counted)
    prepared = _prepare(ledger_state)
    assert calls == 1
    assert prepared["model_call_id"] > 0
    assert prepared["schema_version"] == "model_call_ledger_v1"
    assert prepared["project_id"] == ledger_state["project_id"]
    assert prepared["snapshot_id"] == ledger_state["snapshot_id"]
    assert prepared["preparation_state"] == "prepared"
    for field in (
        "snapshot_hash",
        "candidate_set_hash",
        "qualification_hash",
        "authorization_hash",
        "call_identity_hash",
    ):
        assert len(prepared[field]) == 64
        int(prepared[field], 16)

    readback = model_call_ledger.get_model_call(prepared["model_call_id"])
    assert readback == prepared
    with sqlite3.connect(ledger_state["db_path"]) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(model_calls)")]
    assert columns == EXPECTED_COLUMNS
    assert FUTURE_FIELDS.isdisjoint(columns)


def test_t02_exact_replay_and_deterministic_concurrent_exact_unique_recovery(
    ledger_state, monkeypatch
):
    first = _prepare(ledger_state, call_prepare_key="sequential-exact")
    before = _persisted_row(ledger_state, first["model_call_id"])
    second = _prepare(ledger_state, call_prepare_key="sequential-exact")
    assert second["model_call_id"] == first["model_call_id"]
    assert _persisted_row(ledger_state, first["model_call_id"]) == before
    assert len(_rows_for_key(ledger_state, "sequential-exact")) == 1

    original_insert = model_call_ledger._insert_prepared_row
    injected_winner_id: int | None = None
    collision_seen = False

    def collide(conn, row):
        nonlocal injected_winner_id, collision_seen
        injected_winner_id = _insert_committed_winner(
            ledger_state, dict(row), original_insert
        )
        try:
            return original_insert(conn, row)
        except sqlite3.IntegrityError:
            collision_seen = True
            raise

    monkeypatch.setattr(model_call_ledger, "_insert_prepared_row", collide)
    recovered = _prepare(
        ledger_state,
        local_task_id="task-race-exact",
        call_prepare_key="race-exact",
    )
    assert collision_seen is True
    assert injected_winner_id is not None
    assert recovered["model_call_id"] == injected_winner_id
    assert len(_rows_for_key(ledger_state, "race-exact")) == 1


def test_t03_sequential_drift_retry_separation_and_concurrent_drift_recovery(
    ledger_state, monkeypatch
):
    first = _prepare(ledger_state, call_prepare_key="drift-key")
    first_row = _persisted_row(ledger_state, first["model_call_id"])
    _assert_http_code(
        "MODEL_CALL_IDEMPOTENCY_CONFLICT",
        lambda: _prepare(
            ledger_state,
            call_prepare_key="drift-key",
            model_version="2026-09",
        ),
    )
    assert _persisted_row(ledger_state, first["model_call_id"]) == first_row
    assert len(_rows_for_key(ledger_state, "drift-key")) == 1

    retry = _prepare(
        ledger_state,
        local_task_id="task-1",
        call_prepare_key="retry-key",
    )
    assert retry["model_call_id"] != first["model_call_id"]
    assert retry["local_task_id"] == first["local_task_id"]
    assert _row_count(ledger_state) == 2

    original_insert = model_call_ledger._insert_prepared_row
    winner_snapshot: dict[str, object] | None = None
    collision_seen = False

    def collide_with_drift(conn, row):
        nonlocal winner_snapshot, collision_seen
        winner = dict(row)
        winner["local_task_id"] = "different-logical-task"
        winner["call_identity_hash"] = model_call_ledger._stable_hash(
            model_call_ledger._call_identity_payload(winner)
        )
        winner_id = _insert_committed_winner(
            ledger_state, winner, original_insert
        )
        winner_snapshot = _persisted_row(ledger_state, winner_id)
        try:
            return original_insert(conn, row)
        except sqlite3.IntegrityError:
            collision_seen = True
            raise

    monkeypatch.setattr(model_call_ledger, "_insert_prepared_row", collide_with_drift)
    _assert_http_code(
        "MODEL_CALL_IDEMPOTENCY_CONFLICT",
        lambda: _prepare(
            ledger_state,
            local_task_id="requested-logical-task",
            call_prepare_key="race-drift",
        ),
    )
    assert collision_seen is True
    rows = _rows_for_key(ledger_state, "race-drift")
    assert len(rows) == 1
    assert winner_snapshot is not None
    assert dict(rows[0]) == winner_snapshot


@pytest.mark.parametrize(
    "mutator",
    [
        lambda q: None,
        lambda q: {key: value for key, value in q.items() if key != "model_id"},
        lambda q: {**q, "extra": "x"},
        lambda q: {**q, "provider": "provider-b"},
        lambda q: {**q, "model_id": "model-b"},
        lambda q: {**q, "model_version": "other"},
        lambda q: {**q, "rule_version": "other"},
        lambda q: {**q, "output_schema_version": "other"},
        lambda q: {**q, "benchmark_sample_pack_version": "other"},
        lambda q: {**q, "qualification_status": "failed"},
        lambda q: {**q, "model_id": "   "},
    ],
)
def test_t04_qualification_adversarial_is_fail_closed(ledger_state, mutator):
    qualification = mutator(deepcopy(QUALIFICATION))
    before = _row_count(ledger_state)
    with pytest.raises(HTTPException) as caught:
        model_call_ledger.prepare_model_call(
            local_task_id="task-q",
            call_prepare_key="q-key",
            snapshot_id=ledger_state["snapshot_id"],
            task_type="daily_report_generate",
            provider="provider-a",
            model_id="model-a",
            model_version="2026-08",
            rule_version="rules/1.0",
            output_schema_version="daily-report/1.0",
            benchmark_sample_pack_version="samples/1.0",
            qualification_record=qualification,
            data_sending_authorization=AUTHORIZATION,
        )
    assert _code(caught) == "AI_MODEL_NOT_QUALIFIED"
    assert _row_count(ledger_state) == before


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        {"provider": "provider-a", "authorized": True},
        {**AUTHORIZATION, "extra": "x"},
        {**AUTHORIZATION, "provider": "provider-b"},
        {**AUTHORIZATION, "authorized": False},
        {**AUTHORIZATION, "valid": False},
        {**AUTHORIZATION, "authorized": 1},
        {**AUTHORIZATION, "valid": "true"},
    ],
)
def test_t05_authorization_adversarial_is_preparation_only_and_fail_closed(
    ledger_state, authorization
):
    before = _row_count(ledger_state)
    with pytest.raises(HTTPException) as caught:
        model_call_ledger.prepare_model_call(
            local_task_id="task-a",
            call_prepare_key="a-key",
            snapshot_id=ledger_state["snapshot_id"],
            task_type="daily_report_generate",
            provider="provider-a",
            model_id="model-a",
            model_version="2026-08",
            rule_version="rules/1.0",
            output_schema_version="daily-report/1.0",
            benchmark_sample_pack_version="samples/1.0",
            qualification_record=QUALIFICATION,
            data_sending_authorization=authorization,
        )
    assert _code(caught) == "AI_DATA_AUTHORIZATION_REQUIRED"
    assert _row_count(ledger_state) == before


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("candidate_tamper", "PROJECT_PROGRESS_CONTEXT_INVALID"),
        ("prd_escape", "PRD_STORAGE_ESCAPE"),
        ("git_dirty", "GIT_WORKSPACE_DIRTY"),
    ],
)
def test_t06_candidate_prd_storage_and_git_workspace_errors_propagate_unchanged(
    ledger_state, case, expected
):
    if case == "candidate_tamper":
        with sqlite3.connect(ledger_state["db_path"]) as conn:
            conn.execute(
                "UPDATE evidence_snapshots SET snapshot_hash = ? WHERE id = ?",
                ("f" * 64, ledger_state["snapshot_id"]),
            )
    elif case == "prd_escape":
        with sqlite3.connect(ledger_state["db_path"]) as conn:
            conn.execute(
                "UPDATE prd_versions SET structured_path = '../escape.json' WHERE id = ?",
                (ledger_state["prd_id"],),
            )
    else:
        candidate_tests._write(ledger_state["repo"], "dirty.txt", b"dirty\n")

    before = _row_count(ledger_state)
    with pytest.raises(HTTPException) as caught:
        _prepare(ledger_state, call_prepare_key=f"error-{case}")
    assert _code(caught) == expected
    assert _row_count(ledger_state) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_id", True),
        ("snapshot_id", 0),
        ("snapshot_id", -1),
        ("local_task_id", ""),
        ("local_task_id", " " * 5),
        ("local_task_id", "x" * 129),
        ("call_prepare_key", ""),
        ("call_prepare_key", "x" * 129),
        ("task_type", "unknown"),
        ("task_type", 7),
        ("provider", ""),
        ("provider", 7),
        ("model_id", " "),
        ("model_version", None),
        ("rule_version", ""),
        ("output_schema_version", []),
        ("benchmark_sample_pack_version", ""),
    ],
)
def test_t07_strict_direct_input_rejects_invalid_values(ledger_state, field, value):
    kwargs = {
        "local_task_id": "task-input",
        "call_prepare_key": "input-key",
        "snapshot_id": ledger_state["snapshot_id"],
        "task_type": "daily_report_generate",
        "provider": "provider-a",
        "model_id": "model-a",
        "model_version": "2026-08",
        "rule_version": "rules/1.0",
        "output_schema_version": "daily-report/1.0",
        "benchmark_sample_pack_version": "samples/1.0",
        "qualification_record": QUALIFICATION,
        "data_sending_authorization": AUTHORIZATION,
    }
    kwargs[field] = value
    before = _row_count(ledger_state)
    with pytest.raises(HTTPException) as caught:
        model_call_ledger.prepare_model_call(**kwargs)
    assert _code(caught) == "MODEL_CALL_INPUT_INVALID"
    assert _row_count(ledger_state) == before


def test_t08_additive_db_init_is_idempotent_and_has_exact_v1_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO projects (name, status, created_at) VALUES ('legacy', 'active', '2026-01-01T00:00:00+00:00')"
        )
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(db_path))
    db.init_db()
    db.init_db()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        project = conn.execute("SELECT id, name, status, created_at FROM projects").fetchone()
        assert dict(project) == {
            "id": 1,
            "name": "legacy",
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        columns = [row[1] for row in conn.execute("PRAGMA table_info(model_calls)").fetchall()]
        assert columns == EXPECTED_COLUMNS
        assert FUTURE_FIELDS.isdisjoint(columns)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='model_calls'"
        ).fetchone()[0]
        normalized = " ".join(sql.split()).lower()
        assert "unique (project_id, call_prepare_key)" in normalized
        assert "call_identity_hash text not null unique" in normalized
        assert "authorization_authorized in (0, 1)" in normalized
        assert "authorization_valid in (0, 1)" in normalized


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("candidate_set_hash", "f" * 64),
        ("qualification_hash", "e" * 64),
        ("authorization_valid", 0),
        ("call_prepare_key", "tampered-key"),
        ("call_identity_hash", "d" * 64),
    ],
)
def test_t09_persisted_tamper_fails_self_closure_without_candidate_rerun(
    ledger_state, monkeypatch, column, value
):
    prepared = _prepare(ledger_state, call_prepare_key=f"tamper-{column}")
    with sqlite3.connect(ledger_state["db_path"]) as conn:
        conn.execute(
            f"UPDATE model_calls SET {column} = ? WHERE id = ?",
            (value, prepared["model_call_id"]),
        )

    def no_candidate(*_args, **_kwargs):
        raise AssertionError("get_model_call must not rerun Candidate Set")

    monkeypatch.setattr(model_call_ledger, "build_context_candidate_set", no_candidate)
    with pytest.raises(HTTPException) as caught:
        model_call_ledger.get_model_call(prepared["model_call_id"])
    assert _code(caught) == "MODEL_CALL_LEDGER_INVALID"


def test_t10_schema_and_persisted_row_contain_no_raw_context_body(ledger_state):
    prepared = _prepare(ledger_state, call_prepare_key="no-raw")
    row = _persisted_row(ledger_state, prepared["model_call_id"])
    columns = set(row)
    assert columns == set(EXPECTED_COLUMNS)
    forbidden_fragments = (
        "冻结项目档案",
        "冻结正文一",
        "冻结正文二",
        "alpha",
        "bravo",
        "diff --git",
    )
    text_values = [value for value in row.values() if isinstance(value, str)]
    assert all(
        fragment not in value
        for fragment in forbidden_fragments
        for value in text_values
    )
    assert not any(
        name in columns
        for name in (
            "profile_content",
            "prd_text",
            "table_rows",
            "diff_text",
            "candidate_json",
            "request_body",
            "response_body",
        )
    )


def test_t11_success_and_failure_paths_have_no_external_downstream_side_effects(
    ledger_state, monkeypatch
):
    def no_network(*_args, **_kwargs):
        raise AssertionError("external network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", no_network)
    seen_git: list[list[str]] = []

    class GuardedGitClient(GitClient):
        @staticmethod
        def _guard(args):
            forbidden = {
                "fetch",
                "clone",
                "pull",
                "checkout",
                "switch",
                "reset",
                "merge",
                "push",
            }
            if any(arg in forbidden for arg in args):
                raise AssertionError(f"forbidden Git network/mutation command: {args}")

        def _exec(self, args, *, cwd):
            self._guard(args)
            seen_git.append(list(args))
            return super()._exec(args, cwd=cwd)

        def _run_bounded_process(self, args, **kwargs):
            self._guard(args)
            seen_git.append(list(args))
            return super()._run_bounded_process(args, **kwargs)

    monkeypatch.setattr(context_resolver, "_git_client_factory", GuardedGitClient)
    original_import = builtins.__import__
    forbidden_runtime_modules = ("redaction", "smtp", "provider_sdk", "model_sdk", "report_renderer")

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        lowered = name.casefold()
        if any(fragment in lowered for fragment in forbidden_runtime_modules):
            raise AssertionError(f"forbidden downstream capability import: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    success = _prepare(ledger_state, call_prepare_key="side-effect-success")
    assert success["preparation_state"] == "prepared"
    before_failure = _row_count(ledger_state)
    with pytest.raises(HTTPException) as caught:
        model_call_ledger.prepare_model_call(
            local_task_id="side-effect-failure",
            call_prepare_key="side-effect-failure",
            snapshot_id=ledger_state["snapshot_id"],
            task_type="daily_report_generate",
            provider="provider-a",
            model_id="model-a",
            model_version="2026-08",
            rule_version="rules/1.0",
            output_schema_version="daily-report/1.0",
            benchmark_sample_pack_version="samples/1.0",
            qualification_record=QUALIFICATION,
            data_sending_authorization={**AUTHORIZATION, "valid": False},
        )
    assert _code(caught) == "AI_DATA_AUTHORIZATION_REQUIRED"
    assert _row_count(ledger_state) == before_failure
    assert seen_git


def test_t12_candidate_and_existing_ai_validation_contracts_remain_shared_and_unchanged(
    ledger_state
):
    before = context_resolver.build_context_candidate_set(ledger_state["snapshot_id"])
    _prepare(ledger_state, call_prepare_key="compatibility")
    after = context_resolver.build_context_candidate_set(ledger_state["snapshot_id"])
    assert after == before
    assert model_call_ledger._validate_qualification is ai_contract_validation._validate_qualification
    assert model_call_ledger._validate_authorization is ai_contract_validation._validate_authorization
