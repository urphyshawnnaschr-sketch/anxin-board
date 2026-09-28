"""Model upgrades preserve old evidence; only unsent queued tasks can re-prepare."""
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "backend"))
from app import report_generation_preparation as preparation, report_generation_batches as batches, model_execution

TASK = {"project_id": 7, "local_task_id": "task", "state": "queued"}
CAP = SimpleNamespace(provider="deepseek", model_id="new-model", model_version="new-version")

@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "upgrade.db"))
    batches._init()
    with batches.get_connection() as conn:
        conn.executescript("""CREATE TABLE model_calls(id INTEGER PRIMARY KEY,project_id INTEGER,local_task_id TEXT);
            CREATE TABLE model_execution_results(model_call_id INTEGER);
            INSERT INTO model_calls VALUES(1,7,'task');
            INSERT INTO model_calls VALUES(2,7,'task');""")
    monkeypatch.setattr(preparation, "get_report_generation_task", lambda **_: dict(TASK))
    monkeypatch.setattr(preparation.model_call_ledger, "_read_by_key", lambda *a, **k: None if k["call_prepare_key"].startswith("report-model:") else {
        "provider": "deepseek", "model_id": "old-model", "model_version": "old-version"})
    return tmp_path

def install_conflict(monkeypatch):
    seen=[]
    def prepare(**kwargs):
        seen.append(kwargs["call_prepare_key"])
        if kwargs["call_prepare_key"] == "report-generate:identity":
            raise HTTPException(409, detail={"code": "MODEL_CALL_IDEMPOTENCY_CONFLICT"})
        return {"model_call_id": 3}
    monkeypatch.setattr(preparation.model_call_ledger, "prepare_model_call", prepare)
    return seen

def run():
    return preparation._prepare_current_model_call(task=TASK, capability=CAP,
        call_prepare_key="report-generate:identity")

def test_unsent_queued_upgrade_gets_stable_new_identity(database, monkeypatch):
    seen=install_conflict(monkeypatch)
    assert run() == run() == {"model_call_id": 3}
    assert seen[1] == seen[3] != seen[0]
    with batches.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 2

@pytest.mark.parametrize("state", ["running", "succeeded", "unknown", "failed"])
def test_nonqueued_never_migrates(database, monkeypatch, state):
    seen=install_conflict(monkeypatch)
    monkeypatch.setattr(preparation, "get_report_generation_task", lambda **_: {**TASK,"state":state})
    with pytest.raises(HTTPException): run()
    assert len(seen)==1

@pytest.mark.parametrize("evidence", ["result", "claim", "claimed", "unknown", "succeeded"])
def test_any_send_evidence_prevents_migration(database, monkeypatch, evidence):
    seen=install_conflict(monkeypatch)
    with batches.get_connection() as conn:
        if evidence=="result": conn.execute("INSERT INTO model_execution_results VALUES(1)")
        elif evidence!="claim": conn.execute("INSERT INTO report_generation_batches VALUES(1,1,2,?,NULL)",(evidence,))
    if evidence=="claim": monkeypatch.setattr(model_execution,"_SEND_CLAIMED_MODEL_CALL_IDS",{1})
    with pytest.raises(HTTPException): run()
    assert len(seen)==1

def test_same_model_other_identity_conflict_is_not_bypassed(database, monkeypatch):
    seen=install_conflict(monkeypatch)
    monkeypatch.setattr(preparation.model_call_ledger,"_read_by_key",lambda *a,**k: None if k["call_prepare_key"].startswith("report-model:") else vars(CAP))
    with pytest.raises(HTTPException): run()
    assert len(seen)==1

def test_summary_uses_current_plan_and_preserves_old_pending(database, monkeypatch):
    from app import model_call_ledger
    for parent, child in ((1,2),(3,4)):
        plan={"parent_call_id":parent,"batches":[{"ordinal":1,"model_call_id":child,"estimated_input_tokens":100}]}
        with batches.get_connection() as conn:
            conn.execute("INSERT INTO report_batch_plans VALUES (?,7,'task',?,?)",(parent,batches._hash(plan),batches._json(plan)))
            conn.execute("INSERT INTO report_generation_batches VALUES (?,1,?,'pending',NULL)",(parent,child))
    monkeypatch.setattr(preparation,"_resolve_capability",lambda:CAP)
    monkeypatch.setattr(model_call_ledger,"get_model_call",lambda n: vars(CAP) if n==3 else {"provider":"deepseek","model_id":"old","model_version":"old"})
    result=batches.task_summary(TASK)
    assert result["batches"][0]["model_call_id"]==4
    assert batches.get_plan(1)["states"][0]["state"]=="pending"
    monkeypatch.setattr(preparation,"_resolve_capability",lambda:SimpleNamespace(provider="deepseek",model_id="future",model_version="future"))
    assert batches.task_summary(TASK) is None
    monkeypatch.setattr(preparation,"_resolve_capability",lambda:CAP)
    with batches.get_connection() as conn:
        conn.execute("UPDATE report_generation_batches SET state='succeeded', result_id=10 WHERE parent_call_id=3")
    assert batches.task_summary({**TASK,"state":"running"})["completed_batch_count"]==1
    with batches.get_connection() as conn:
        conn.execute("UPDATE report_generation_batches SET state='unknown' WHERE parent_call_id=1")
    with pytest.raises(HTTPException): batches.task_summary(TASK)


def test_real_ledger_upgrade_replay_and_running_resume(tmp_path, monkeypatch):
    from app import db, model_call_ledger as ledger
    monkeypatch.setenv("ANXINBOARD_DB_PATH", str(tmp_path / "real-ledger.db"))
    db.init_db()
    monkeypatch.setattr(ledger,"build_context_candidate_set",lambda _: {
        "project_id":7,"snapshot_id":11,"snapshot_hash":"a"*64,"candidate_set_hash":"b"*64})
    task=dict(TASK)
    monkeypatch.setattr(preparation,"get_report_generation_task",lambda **_: dict(task))
    def kwargs(model,version):
        q={"provider":"deepseek","model_id":model,"model_version":version,"rule_version":"rules/1",
           "output_schema_version":"daily-report/1.0","benchmark_sample_pack_version":"samples/1"}
        return {**q,"local_task_id":"task","call_prepare_key":"report-generate:"+"c"*64,
          "snapshot_id":11,"task_type":"daily_report_generate", "qualification_record":{**q,"qualification_status":"qualified"},
          "data_sending_authorization":{"provider":"deepseek","authorized":True,"valid":True}}
    old=ledger.prepare_model_call(**kwargs("old-model","old-version"))
    before=ledger.get_model_call(old["model_call_id"])
    current=preparation._prepare_current_model_call(task=task,capability=CAP,**kwargs(CAP.model_id,CAP.model_version))
    assert current["model_call_id"]!=old["model_call_id"]
    assert len(current["call_prepare_key"])<=128
    assert preparation._prepare_current_model_call(task=task,capability=CAP,**kwargs(CAP.model_id,CAP.model_version))==current
    task["state"]="running"
    # A running migrated chain must replay its current identity, never create a
    # third call or try to rewrite the legacy key after one batch has completed.
    assert preparation._prepare_current_model_call(task=task,capability=CAP,**kwargs(CAP.model_id,CAP.model_version))==current
    assert ledger.get_model_call(old["model_call_id"])==before
    with batches.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]==2


def test_old_only_pending_plan_does_not_advertise_old_model_batch(database,monkeypatch):
    from app import model_call_ledger
    plan={"parent_call_id":1,"batches":[{"ordinal":1,"model_call_id":2,"estimated_input_tokens":100}]}
    with batches.get_connection() as conn:
        conn.execute("INSERT INTO report_batch_plans VALUES (1,7,'task',?,?)",(batches._hash(plan),batches._json(plan)))
        conn.execute("INSERT INTO report_generation_batches VALUES (1,1,2,'pending',NULL)")
    monkeypatch.setattr(preparation,"_resolve_capability",lambda:CAP)
    monkeypatch.setattr(model_call_ledger,"get_model_call",lambda _: {"provider":"deepseek","model_id":"old","model_version":"old"})
    assert batches.task_summary(TASK) is None
    assert batches.get_plan(1)["states"][0]["state"]=="pending"
