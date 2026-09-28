"""Owner script orchestration against synthetic model/data and temporary SQLite only."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import runpy
import sqlite3
from types import SimpleNamespace

import pytest

from app import brownfield_baseline as core
from app import brownfield_baseline_store as store
from test_brownfield_atlas_spike import indexed, plan
from test_brownfield_baseline import Model

SCRIPT = Path(__file__).resolve().parents[2] / "ops/spikes/run_brownfield_baseline_live.py"


@pytest.fixture
def owner_script(tmp_path, monkeypatch):
    monkeypatch.setenv("ANXINBOARD_OWNER_LIVE_ATLAS_BASELINE", "YES")
    monkeypatch.setenv("ANXINBOARD_ATLAS_BASELINE_AUTHORIZATION_ID", "synthetic-owner-run-1")
    loaded = runpy.run_path(str(SCRIPT), run_name="synthetic_script")
    namespace = loaded["main"].__globals__
    source = indexed()
    source["exact_head"] = loaded["EXPECTED_HEAD"]
    for item in source["evidence"]:
        item["exact_head"] = loaded["EXPECTED_HEAD"]
    planned = plan()
    planned["planned_modules"] = [dict(deepcopy(planned["planned_modules"][0]), client_id=f"m{n}") for n in range(16)]
    scope = dict(project_id=3, plan_profile_id=2, exact_head=loaded["EXPECTED_HEAD"],
                 plan_content_hash="b"*64, prd_id=7, prd_source_hash="c"*64,
                 git_url="https://example.invalid/synthetic", git_branch="main")
    model = Model()
    prepared = core.prepare_from_inputs(scope=scope, plan=planned, indexed=source, adapter=model)
    def connect():
        conn = sqlite3.connect(tmp_path / "owner.db")
        conn.row_factory = sqlite3.Row
        return conn
    candidates, keys = {}, []
    def promote(**kwargs):
        candidates[501] = dict(id=501, project_id=3, source_prd_id=7, status="candidate", content=kwargs["content"])
        return candidates[501]
    def credential():
        keys.append("read")
        return "synthetic-only-not-a-secret"
    class Observer:
        ident = None
        def __init__(self, **kwargs): pass
        def start(self): pass
        def join(self, **kwargs): pass
    monkeypatch.setitem(namespace, "get_connection", connect)
    monkeypatch.setitem(namespace, "get_profile", lambda identity: candidates[identity])
    monkeypatch.setitem(namespace, "_promote_generated_candidate", promote)
    monkeypatch.setattr(core, "read_scope", lambda *a: deepcopy(scope))
    monkeypatch.setattr(core, "prepare_baseline", lambda *a: prepared)
    monkeypatch.setattr(namespace["profile"], "_read_provider_credential", credential)
    monkeypatch.setattr(namespace["threading"], "Thread", Observer)
    return SimpleNamespace(main=loaded["main"], namespace=namespace, prepared=prepared, scope=scope,
                           connect=connect, model=model, candidates=candidates, keys=keys)


def test_guard_refuses_before_importing_product(monkeypatch):
    monkeypatch.delenv("ANXINBOARD_OWNER_LIVE_ATLAS_BASELINE", raising=False)
    with pytest.raises(SystemExit, match="LIVE_BASELINE_GUARD_REQUIRED"):
        runpy.run_path(str(SCRIPT), run_name="blocked_script")


def test_owner_product_core_creates_only_unconfirmed_complete_candidate(owner_script, capsys):
    owner = owner_script
    assert owner.main() == 0
    assert owner.keys == ["read"]
    candidate = owner.candidates[501]
    assert candidate["status"] == "candidate"
    assert len(candidate["content"]["implementation_mappings"]) == 16
    assert all(row["exact_head"] == owner.scope["exact_head"] for row in candidate["content"]["implementation_mappings"])
    with closing(owner.connect()) as conn:
        task = store.latest_task(conn, 3)
        assert task["status"] == "succeeded" and task["profile_id"] == 501
    output = capsys.readouterr().out
    assert "LIVE_BASELINE_ACCEPTANCE=PASS" in output
    assert "LIVE_BASELINE_AUTO_CONFIRM=NO" in output
    assert "synthetic-only-not-a-secret" not in output and "def add_device" not in output
    call_count = len(owner.model.calls)
    assert call_count > 0
    assert owner.main() == 0
    assert len(owner.model.calls) == call_count


def test_unknown_cannot_be_retried_or_bypassed_with_new_nonce(owner_script, monkeypatch, capsys):
    owner = owner_script
    owner.model.fail = RuntimeError("raw-private-provider-body")
    assert owner.main() == 1
    assert len(owner.model.calls) == 1
    assert owner.main() == 1
    monkeypatch.setenv("ANXINBOARD_ATLAS_BASELINE_AUTHORIZATION_ID", "synthetic-owner-run-2")
    assert owner.main() == 1
    assert len(owner.model.calls) == 1
    output = capsys.readouterr().out
    assert "BROWNFIELD_STORE_IDENTITY_ALREADY_AUTHORIZED" in output
    assert "raw-private-provider-body" not in output
    assert not owner.candidates


@pytest.mark.parametrize("nonce", ["", "short", "run with spaces", "run/escape", "x"*101])
def test_invalid_authorization_is_zero_call(owner_script, monkeypatch, nonce):
    monkeypatch.setenv("ANXINBOARD_ATLAS_BASELINE_AUTHORIZATION_ID", nonce)
    assert owner_script.main() == 1
    assert not owner_script.model.calls and not owner_script.keys


@pytest.mark.parametrize("drift", ["head", "model", "modules"])
def test_pins_reject_before_credential_and_task(owner_script, drift):
    owner = owner_script
    if drift == "head":
        owner.scope["exact_head"] = "a"*40
    elif drift == "model":
        owner.model.cap.model_id = "deepseek-v4-pro"
    else:
        owner.prepared["plan"]["planned_modules"].pop()
    assert owner.main() == 1
    assert not owner.model.calls and not owner.keys


def test_final_confirmed_candidate_is_not_reported_as_acceptance(owner_script, monkeypatch, capsys):
    owner = owner_script
    def confirmed(identity):
        return dict(owner.candidates[identity], status="confirmed")
    monkeypatch.setitem(owner.namespace, "get_profile", confirmed)
    assert owner.main() == 1
    assert "LIVE_BASELINE_ACCEPTANCE=PASS" not in capsys.readouterr().out
