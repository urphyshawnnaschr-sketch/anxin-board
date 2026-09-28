import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import brownfield_baseline_store as store


@pytest.fixture
def conn(tmp_path):
    value = sqlite3.connect(tmp_path / "ledger.sqlite")
    value.row_factory = sqlite3.Row
    store.ensure_schema(value)
    yield value
    value.close()


def create(conn, nonce="n", identity=None, max_calls=2):
    return store.create_task(conn, project_id=3, authorization_nonce=nonce, identity=identity or {"head": "a", "model": "fixed"}, max_calls=max_calls)


def claim(conn, task, stage="orientation", input_hash="h"):
    return store.claim_stage(conn, task_id=task["task_id"], stage_key=stage, input_hash=input_hash, wire_bytes=42)


def test_authorization_idempotency_and_identity_never_reopens(conn):
    task = create(conn)
    assert create(conn)["task_id"] == task["task_id"]
    with pytest.raises(store.BrownfieldStoreError): create(conn, identity={"head": "other"})
    with pytest.raises(store.BrownfieldStoreError): create(conn, nonce="other")
    store.finish_task(conn, task["task_id"], status="unknown", error_code="AMBIGUOUS_SEND")
    assert create(conn)["status"] == "unknown"
    with pytest.raises(store.BrownfieldStoreError): create(conn, nonce="other")
    with pytest.raises(store.BrownfieldStoreError): store.finish_task(conn, task["task_id"], status="succeeded")


def test_claim_is_committed_once_bound_and_terminal_cache_is_durable(conn):
    task = create(conn)
    first = claim(conn, task)
    assert first["claimed_now"] is True and not conn.in_transaction
    with pytest.raises(store.BrownfieldStoreError): claim(conn, task)
    with pytest.raises(store.BrownfieldStoreError): claim(conn, task, input_hash="other")
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result={"seeds": [0]})
    cached = claim(conn, task)
    assert cached["claimed_now"] is False and cached["result"] == {"seeds": [0]}
    assert store.get_stage(conn, task["task_id"], "orientation")["result"] == {"seeds": [0]}
    assert len(store.list_stages(conn, task["task_id"])) == 1
    assert store.latest_task(conn, 3)["task_id"] == task["task_id"]


def test_budget_and_terminal_results_cannot_be_changed(conn):
    task = create(conn, max_calls=1)
    claim(conn, task)
    with pytest.raises(store.BrownfieldStoreError): claim(conn, task, "verify")
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="unknown", result=None, error_code="NETWORK_UNKNOWN")
    with pytest.raises(store.BrownfieldStoreError): store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result={})
    with pytest.raises(store.BrownfieldStoreError): claim(conn, task)
    store.finish_task(conn, task["task_id"], status="unknown", error_code="NETWORK_UNKNOWN")
    assert store.task_status(conn, task["task_id"])["claimed_calls"] == 1


def test_append_only_database_guards(conn):
    task = create(conn)
    claim(conn, task)
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result=[])
    for sql in ["DELETE FROM brownfield_stage_claims", "UPDATE brownfield_stage_claims SET input_hash='changed'", "DELETE FROM brownfield_stage_results", "UPDATE brownfield_stage_results SET result_json='{}'", "UPDATE brownfield_baseline_tasks SET identity_json='{}'", "DELETE FROM brownfield_baseline_tasks"]:
        with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
        conn.rollback()


def test_concurrent_claim_only_one_wins(conn):
    task = create(conn)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    def worker():
        other = sqlite3.connect(path, timeout=10)
        other.row_factory = sqlite3.Row
        try:
            return claim(other, task)["claimed_now"]
        except store.BrownfieldStoreError:
            return False
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: worker(), range(2))) == [False, True]
    assert store.task_status(conn, task["task_id"])["claimed_calls"] == 1


def test_orphan_claim_survives_reopen_and_is_not_resent(conn):
    task = create(conn)
    claim(conn, task)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    other.row_factory = sqlite3.Row
    assert store.get_stage(other, task["task_id"], "orientation")["status"] == "claimed"
    with pytest.raises(store.BrownfieldStoreError): claim(other, task)
    other.close()


def test_unresolved_claim_blocks_different_stage_without_spending_budget(conn):
    task = create(conn, max_calls=3)
    claim(conn, task)
    with pytest.raises(store.BrownfieldStoreError, match="STAGE_IN_FLIGHT"):
        claim(conn, task, "verify")
    assert store.task_status(conn, task["task_id"])["claimed_calls"] == 1
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result={})
    assert claim(conn, task, "verify")["claimed_now"] is True


@pytest.mark.parametrize("result", [{"x": float("nan")}, {"x": object()}, "raw body"])
def test_only_valid_json_containers_are_persisted(conn, result):
    task = create(conn)
    claim(conn, task)
    with pytest.raises(store.BrownfieldStoreError): store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result=result)
    assert store.get_stage(conn, task["task_id"], "orientation")["status"] == "claimed"


def test_task_completion_and_input_validation(conn):
    task = create(conn)
    claim(conn, task)
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result={})
    store.finish_task(conn, task["task_id"], status="succeeded", profile_id=7)
    assert store.get_task(conn, task["task_id"])["profile_id"] == 7
    with pytest.raises(store.BrownfieldStoreError): claim(conn, task, "another")
    assert store.get_task(conn, "missing") is None
    assert store.get_stage(conn, task["task_id"], "missing") is None
    with pytest.raises(store.BrownfieldStoreError): create(conn, nonce="next", identity={"head": "b"}, max_calls=True)


def test_different_stages_concurrently_enforce_one_in_flight(conn):
    task = create(conn, max_calls=3)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    def worker(stage):
        other = sqlite3.connect(path, timeout=10)
        other.row_factory = sqlite3.Row
        try:
            return claim(other, task, stage)["claimed_now"]
        except store.BrownfieldStoreError:
            return False
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, ["a", "b"])) == [False, True]


def test_completed_claim_still_consumes_atomic_budget(conn):
    task = create(conn, max_calls=1)
    claim(conn, task)
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result={})
    with pytest.raises(store.BrownfieldStoreError, match="CALL_BUDGET_EXHAUSTED"):
        claim(conn, task, "verify")


def test_caller_transaction_is_not_committed(conn):
    conn.execute("BEGIN")
    with pytest.raises(store.BrownfieldStoreError, match="TRANSACTION_ACTIVE"):
        create(conn)
    assert conn.in_transaction
    conn.rollback()


@pytest.mark.parametrize("identity", [{1: "a"}, {"values": (1, 2)}, {"nested": [{False: 1}]}])
def test_json_identity_has_no_lossy_coercion(conn, identity):
    with pytest.raises(store.BrownfieldStoreError, match="JSON_INVALID"):
        create(conn, identity=identity)


def test_failed_stage_prevents_further_claim_even_before_task_closeout(conn):
    task = create(conn)
    claim(conn, task)
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="unknown", result=None)
    with pytest.raises(store.BrownfieldStoreError): claim(conn, task, "verify")


def test_terminal_result_reopens_identically_and_completion_is_idempotent(conn):
    task = create(conn)
    claim(conn, task)
    args = dict(task_id=task["task_id"], stage_key="orientation", status="succeeded", result={"x": [1]})
    store.finish_stage(conn, **args)
    store.finish_stage(conn, **args)
    store.finish_task(conn, task["task_id"], status="succeeded", profile_id=9)
    store.finish_task(conn, task["task_id"], status="succeeded", profile_id=9)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    other.row_factory = sqlite3.Row
    try:
        assert store.get_stage(other, task["task_id"], "orientation")["result"] == {"x": [1]}
        assert store.get_task(other, task["task_id"])["profile_id"] == 9
    finally:
        other.close()


@pytest.mark.parametrize("failure", ["failed_pre_send", "failed_after_send"])
def test_new_explicit_authorization_after_known_failure_preserves_old_task(conn, failure):
    old = create(conn)
    store.finish_task(conn, old["task_id"], status=failure, error_code="KNOWN_FAILURE")
    new = create(conn, nonce="new-explicit-authorization")
    assert new["task_id"] != old["task_id"]
    assert new["status"] == "queued"
    assert create(conn)["task_id"] == old["task_id"]
    assert create(conn)["status"] == failure
    with pytest.raises(store.BrownfieldStoreError): create(conn, nonce="third")


def test_local_output_is_durable_append_only_and_consumes_no_call(conn):
    task = create(conn)
    assert store.get_output(conn, task["task_id"]) is None
    output = {"candidate": {"modules": []}, "coverage": {"safe": 3}}
    saved = store.save_output(conn, task["task_id"], output=output)
    assert saved["output"] == output
    assert store.save_output(conn, task["task_id"], output=output) == saved
    assert store.task_status(conn, task["task_id"])["claimed_calls"] == 0
    with pytest.raises(store.BrownfieldStoreError): store.save_output(conn, task["task_id"], output={"changed": True})
    for sql in ["DELETE FROM brownfield_baseline_outputs", "UPDATE brownfield_baseline_outputs SET output_json='{}'"]:
        with pytest.raises(sqlite3.IntegrityError): conn.execute(sql)
        conn.rollback()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    other.row_factory = sqlite3.Row
    try:
        assert store.get_output(other, task["task_id"]) == saved
    finally:
        other.close()


def test_local_error_does_not_reopen_task_or_enable_more_dispatch(conn):
    task = create(conn)
    store.set_local_error(conn, task["task_id"], "PROMOTION_FAILED")
    assert store.get_task(conn, task["task_id"])["status"] == "queued"
    assert store.get_task(conn, task["task_id"])["error_code"] == "PROMOTION_FAILED"
    store.finish_task(conn, task["task_id"], status="failed_pre_send")
    with pytest.raises(store.BrownfieldStoreError): store.set_local_error(conn, task["task_id"], "ANOTHER")
    with pytest.raises(store.BrownfieldStoreError): store.save_output(conn, task["task_id"], output={})


def test_saved_output_cache_remains_readable_after_terminal_closeout(conn):
    task = create(conn)
    saved = store.save_output(conn, task["task_id"], output={})
    store.finish_task(conn, task["task_id"], status="succeeded", profile_id=1)
    assert store.save_output(conn, task["task_id"], output={}) == saved
    assert store.get_output(conn, task["task_id"]) == saved


def prior_failure(conn, status="failed_after_send"):
    task = create(conn)
    claim(conn, task)
    store.finish_stage(conn, task_id=task["task_id"], stage_key="orientation", status="succeeded", result={"value": {"m1": ["p1"]}})
    store.finish_task(conn, task["task_id"], status=status, error_code="KNOWN_FAILURE")
    return task


def reuse(conn, task, **overrides):
    args = dict(task_id=task["task_id"], stage_key="orientation", input_hash="h", wire_bytes=42)
    args.update(overrides)
    return store.reuse_successful_stage(conn, **args)


def test_known_failure_new_authorization_reuses_only_exact_success(conn):
    old = prior_failure(conn)
    old_stage = store.get_stage(conn, old["task_id"], "orientation")
    new = create(conn, nonce="new-explicit")
    cached = reuse(conn, new)
    assert cached["claimed_now"] is False and cached["status"] == "succeeded"
    assert cached["result"]["value"] == {"m1": ["p1"]}
    assert cached["result"]["reused_from_task_id"] == old["task_id"]
    assert cached["result"]["reused_from_stage_key"] == "orientation"
    assert not conn.in_transaction
    assert reuse(conn, new) == cached
    assert store.get_stage(conn, old["task_id"], "orientation") == old_stage
    assert store.get_task(conn, old["task_id"])["status"] == "failed_after_send"
    with pytest.raises(store.BrownfieldStoreError): reuse(conn, new, input_hash="wrong")


@pytest.mark.parametrize("binding", [{"input_hash": "other"}, {"wire_bytes": 43}, {"stage_key": "other"}])
def test_reuse_does_not_approximate_prior_input(conn, binding):
    prior_failure(conn)
    new = create(conn, nonce="new-explicit")
    assert reuse(conn, new, **binding) is None
    assert store.list_stages(conn, new["task_id"]) == []


@pytest.mark.parametrize("status", ["queued", "running", "unknown", "succeeded"])
def test_reuse_never_sources_active_unknown_or_success_task(conn, status):
    old = create(conn)
    claim(conn, old)
    store.finish_stage(conn, task_id=old["task_id"], stage_key="orientation", status="succeeded", result={"value": {}})
    if status in {"unknown", "succeeded"}:
        store.finish_task(conn, old["task_id"], status=status)
    with pytest.raises(store.BrownfieldStoreError): create(conn, nonce="new-explicit")
    foreign = create(conn, nonce="different-scope", identity={"head": "b", "model": "fixed"})
    assert reuse(conn, foreign) is None


def test_reuse_atomic_failure_leaves_no_half_claim(conn):
    prior_failure(conn)
    new = create(conn, nonce="new-explicit")
    conn.executescript("CREATE TRIGGER reject_reused_result BEFORE INSERT ON brownfield_stage_results BEGIN SELECT RAISE(ABORT,'TEST'); END;")
    with pytest.raises(store.BrownfieldStoreError): reuse(conn, new)
    assert store.get_stage(conn, new["task_id"], "orientation") is None
    assert store.get_task(conn, new["task_id"])["status"] == "queued"


def test_reuse_spends_budget_and_blocks_inflight(conn):
    prior_failure(conn)
    new = create(conn, nonce="new-explicit", max_calls=1)
    reuse(conn, new)
    assert store.task_status(conn, new["task_id"])["claimed_calls"] == 1
    with pytest.raises(store.BrownfieldStoreError, match="CALL_BUDGET_EXHAUSTED"):
        claim(conn, new, "next")


def test_reuse_refuses_any_unresolved_target_claim(conn):
    prior_failure(conn)
    new = create(conn, nonce="new-explicit")
    claim(conn, new, "other")
    with pytest.raises(store.BrownfieldStoreError, match="STAGE_IN_FLIGHT"):
        reuse(conn, new)


def test_reuse_never_crosses_project_and_selects_newest_known_failure(conn):
    first = prior_failure(conn, status="failed_pre_send")
    foreign = store.create_task(conn, project_id=4, authorization_nonce="foreign", identity=first["identity"], max_calls=2)
    assert reuse(conn, foreign) is None
    second = create(conn, nonce="second")
    claim(conn, second)
    store.finish_stage(conn, task_id=second["task_id"], stage_key="orientation", status="succeeded", result={"value": {"m1": ["new"]}})
    store.finish_task(conn, second["task_id"], status="failed_after_send")
    third = create(conn, nonce="third")
    copied = reuse(conn, third)
    assert copied["result"]["reused_from_task_id"] == second["task_id"]
    assert copied["result"]["value"] == {"m1": ["new"]}


def test_reuse_itself_cannot_exceed_budget(conn):
    prior_failure(conn)
    new = create(conn, nonce="new-explicit", max_calls=1)
    claim(conn, new, "already-used")
    store.finish_stage(conn, task_id=new["task_id"], stage_key="already-used", status="succeeded", result={})
    with pytest.raises(store.BrownfieldStoreError, match="CALL_BUDGET_EXHAUSTED"):
        reuse(conn, new)
    assert store.get_stage(conn, new["task_id"], "orientation") is None
