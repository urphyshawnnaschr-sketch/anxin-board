"""Local authorization and read-only observation for durable Atlas baseline tasks."""
from contextlib import closing, contextmanager
import json
import re
import sqlite3
import threading

from fastapi import APIRouter, HTTPException, Request

from app import brownfield_baseline as core
from app import brownfield_baseline_store as store
from app import project_profile_generation as profile_generation
from app.db import get_connection
from app.git_client import GitClientError
from app.git_workspace_locks import GitOperationInProgress
from app.local_session_api import require_local_read_request, require_local_write_request
from app.project_profiles import read_current_confirmed_project_profile, _canonicalize
from app.project_profile_v2 import ProjectProfileV2Content
from app.project_state_baseline_api import (
    BaselineExecutePayload, BaselinePreflightPayload, _promote_generated_candidate,
)

router = APIRouter(prefix="/api/projects/{project_id}/project-state-baseline/atlas")
_WORKER_LOCK = threading.Lock()
_WORKERS = {}
_PREPARING = set()
_PENDING = {"queued", "running"}
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_:-]{1,159}$")
_ADMISSION_MESSAGES = {
    "BROWNFIELD_PROJECT_TASK_ACTIVE": "已有分析任务正在进行，请查看原任务，暂不能重新扫描或创建任务。",
    "BROWNFIELD_PREPARATION_IN_PROGRESS": "正在检查本项目的代码范围，请稍后；未发起新的模型请求。",
}


def _error(code, message=None, status=409):
    if message is None:
        message = _ADMISSION_MESSAGES.get(code, "当前代码分析无法安全继续，请核对分析任务和项目配置。")
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@contextmanager
def _safe_errors():
    try:
        yield
    except HTTPException as exc:
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
            raise _error(code, status=exc.status_code) from None
        raise _error("BROWNFIELD_REQUEST_REJECTED", status=exc.status_code) from None
    except GitOperationInProgress:
        raise _error("BROWNFIELD_GIT_BUSY", "Git 工作区正在使用中，未发起新的模型请求。") from None
    except GitClientError:
        raise _error("BROWNFIELD_GIT_WORKSPACE_FAILED", "受控代码工作区无法安全读取，未发起新的模型请求。") from None
    except sqlite3.Error:
        raise _error("BROWNFIELD_DATABASE_UNAVAILABLE", "无法读取分析任务台账，请保留当前任务后重试查询。", 500) from None
    except Exception as exc:
        code = core.safe_code(exc)
        raise _error(code, status=500 if code == "BROWNFIELD_RUNTIME_FAILED" else 409) from None


def ensure_brownfield_baseline_schema():
    with closing(get_connection()) as conn:
        store.ensure_schema(conn)


def _owned_task(conn, project_id, task_id):
    task = store.get_task(conn, task_id)
    if task is None or task["project_id"] != project_id:
        raise _error("BROWNFIELD_TASK_NOT_FOUND", "没有找到本项目的分析任务。", 404)
    return task


def _worker_active(project_id, task_id):
    with _WORKER_LOCK:
        return _WORKERS.get(project_id, {}).get("task_id") == task_id


@contextmanager
def _project_preparation(project_id):
    """Reserve only this project's preparation/admission; never hold a global lock over Git."""
    with _WORKER_LOCK:
        if project_id in _PREPARING:
            raise _error("BROWNFIELD_PREPARATION_IN_PROGRESS")
        _PREPARING.add(project_id)
    try:
        yield
    finally:
        with _WORKER_LOCK:
            _PREPARING.discard(project_id)


def _require_project_idle(conn, project_id, *, resuming_task_id=None):
    with _WORKER_LOCK:
        active = project_id in _WORKERS
    pending = conn.execute(
        "SELECT task_id FROM brownfield_baseline_tasks WHERE project_id=? AND status IN ('queued','running')",
        (project_id,),
    ).fetchall()
    if active or any(row["task_id"] != resuming_task_id for row in pending):
        raise _error("BROWNFIELD_PROJECT_TASK_ACTIVE")


def _replayed_authorization(conn, project_id, payload):
    row = conn.execute("SELECT task_id FROM brownfield_baseline_tasks WHERE authorization_nonce=?",
                       (payload.authorization_nonce,)).fetchone()
    if row is None:
        return None
    task = store.get_task(conn, row["task_id"])
    if (task["project_id"] != project_id or task["identity_hash"] != payload.preflight_identity_hash
            or task["identity"]["plan_profile_id"] != payload.plan_profile_id):
        raise _error("BROWNFIELD_STORE_AUTHORIZATION_MISMATCH")
    # A replay is observation only. A dead worker needs the explicit resume endpoint.
    return {"task": _public_task(conn, task)}


def _stale_scope(conn, task):
    """Compare authoritative metadata only; no Git scan, credential or provider access."""
    try:
        project, prd, git = profile_generation._read_current_inputs(task["project_id"])
        confirmed = read_current_confirmed_project_profile(task["project_id"], conn=conn)
        identity = task["identity"]
        adapter = core.build_brownfield_strict_adapter()
        capability = adapter.get_capability(task_type=core.PROFILE_TASK, output_schema_version=core.TRANSPORT_SCHEMA)
        return any((
            identity["capability"] != core.cap_identity(capability),
            identity["plan_profile_id"] != confirmed["id"],
            identity["plan_content_hash"] != confirmed["content_hash"],
            identity["prd_id"] != prd["id"],
            identity["prd_source_hash"] != prd["source_hash"],
            identity["git_url"] != project["git_url"],
            identity["git_branch"] != project["branch"],
            identity["exact_head"] != git["remote_head"],
            identity["exact_head"] != git["local_head"],
        ))
    except Exception:
        return None


def _public_task(conn, task):
    stages = store.list_stages(conn, task["task_id"])
    active = _worker_active(task["project_id"], task["task_id"])
    unresolved = any(stage["status"] == "claimed" for stage in stages)
    orphan = task["status"] in _PENDING and unresolved and not active
    stale = _stale_scope(conn, task)
    result = {
        "plan_profile_id": task["identity"]["plan_profile_id"],
        "exact_head": task["identity"]["exact_head"],
        "stale_scope": stale,
        "task_matches_current_scope": None if stale is None else not stale,
        "task_id": task["task_id"], "status": "unknown" if orphan else task["status"],
        "authorization_nonce": task["authorization_nonce"],
        "preflight_identity_hash": task["identity_hash"],
        "stage_count": len(stages),
        "completed_stages": sum(stage["status"] == "succeeded" for stage in stages),
        "max_calls": task["max_calls"],
        "current_stage": stages[-1]["stage_key"] if stages else None,
        "error_code": "BROWNFIELD_UNRESOLVED_CLAIM" if orphan else task["error_code"],
        "profile_id": task["profile_id"],
        "resume_available": task["status"] in _PENDING and not active and not unresolved and stale is False,
    }
    if (stages and task["status"] in {"failed_pre_send", "failed_after_send", "unknown"}
            and stages[-1]["error_code"] == task["error_code"]):
        diagnostic = core.safe_diagnostic(HTTPException(502, detail=stages[-1].get("result")))
        if diagnostic:
            result["failure_diagnostic"] = diagnostic
    output = store.get_output(conn, task["task_id"])
    if output is not None:
        generated = ProjectProfileV2Content.model_validate(output["output"]["content"])
        result["generated_content_hash"] = _canonicalize(generated)[1]
        result["coverage"] = output["output"].get("coverage")
        result["module_requirements"] = output["output"].get("requirements")
    return result


def _worker(prepared, task, token):
    project_id = task["project_id"]
    try:
        core.run_baseline(
            prepared, task, connection_factory=get_connection,
            scope_reader=lambda: core.read_scope(project_id, prepared["scope"]["plan_profile_id"]),
            credential_reader=profile_generation._read_provider_credential,
            promote=lambda content: _promote_generated_candidate(
                project_id=project_id, prepared=prepared["scope"],
                authorization_hash=core.digest({"task_id": task["task_id"], "identity_hash": task["identity_hash"]}), content=content),
        )
    except Exception as exc:
        # Unexpected worker termination is durable and never logs exception bodies.
        try:
            with closing(get_connection()) as conn:
                current = store.get_task(conn, task["task_id"])
                if current and current["status"] in _PENDING:
                    stages = store.list_stages(conn, task["task_id"])
                    unresolved = any(stage["status"] == "claimed" for stage in stages)
                    store.finish_task(conn, task["task_id"], status="unknown" if unresolved else "failed_pre_send", error_code=core.safe_code(exc))
        except Exception:
            pass  # Read path still presents surviving unresolved claims as UNKNOWN.
    finally:
        with _WORKER_LOCK:
            if _WORKERS.get(project_id, {}).get("token") is token:
                _WORKERS.pop(project_id, None)


def _start_worker(prepared, task):
    project_id = task["project_id"]
    with _WORKER_LOCK:
        if project_id in _WORKERS:
            return False
        with closing(get_connection()) as conn:
            current = _owned_task(conn, project_id, task["task_id"])
            if current["status"] not in _PENDING:
                return False
            if any(stage["status"] == "claimed" for stage in store.list_stages(conn, task["task_id"])):
                return False
        token = object()
        thread = threading.Thread(target=_worker, args=(prepared, current, token),
                                  name="atlas-baseline-worker", daemon=True)
        _WORKERS[project_id] = {"task_id": task["task_id"], "token": token, "thread": thread}
        try:
            thread.start()
        except Exception:
            _WORKERS.pop(project_id, None)
            raise _error("BROWNFIELD_WORKER_START_FAILED", "任务已保存，但本地执行线程未启动；可稍后继续原任务。", 503) from None
    return True


@router.post("/preflight")
def preflight_atlas(project_id: int, payload: BaselinePreflightPayload, request: Request):
    with _safe_errors():
        require_local_write_request(request, require_idempotency_key=False)
        with _project_preparation(project_id):
            with closing(get_connection()) as conn:
                _require_project_idle(conn, project_id)
            prepared = core.prepare_baseline(project_id, payload.plan_profile_id)
            return {"status": "ready", "preflight": prepared["summary"]}


@router.post("/tasks", status_code=202)
def create_atlas_task(project_id: int, payload: BaselineExecutePayload, request: Request):
    with _safe_errors():
        require_local_write_request(request, require_idempotency_key=True)
        if request.headers.get("local-idempotency-key") != payload.authorization_nonce:
            raise _error("BROWNFIELD_AUTHORIZATION_IDENTITY_MISMATCH", status=400)
        with _project_preparation(project_id):
            with closing(get_connection()) as conn:
                replayed = _replayed_authorization(conn, project_id, payload)
                if replayed is not None:
                    return replayed
                _require_project_idle(conn, project_id)
            prepared = core.prepare_baseline(project_id, payload.plan_profile_id)
            if core.digest(prepared["identity"]) != payload.preflight_identity_hash:
                raise _error("BROWNFIELD_PREFLIGHT_SCOPE_CHANGED", "代码、档案或模型已变化，请重新扫描后再授权。")
            with closing(get_connection()) as conn:
                task = store.create_task(conn, project_id=project_id,
                                         authorization_nonce=payload.authorization_nonce,
                                         identity=prepared["identity"], max_calls=prepared["identity"]["max_calls"])
            _start_worker(prepared, task)
            with closing(get_connection()) as conn:
                return {"task": _public_task(conn, _owned_task(conn, project_id, task["task_id"]))}


@router.get("/tasks/latest")
def latest_atlas_task(project_id: int):
    with _safe_errors():
        with closing(get_connection()) as conn:
            task = store.latest_task(conn, project_id)
            return {"task": _public_task(conn, task) if task else None}


@router.get("/results/{profile_id}")
def get_profile_result(project_id: int, profile_id: int, request: Request):
    """Read a displayed profile's saved result without replacing current work."""
    require_local_read_request(request)
    with _safe_errors():
        with closing(get_connection()) as conn:
            conn.execute('PRAGMA query_only = ON')
            conn.execute('BEGIN')
            profile = conn.execute(
                'SELECT content_json,content_hash FROM project_profiles WHERE id=? AND project_id=?',
                (profile_id, project_id),
            ).fetchone()
            if profile is None:
                raise _error('BROWNFIELD_RESULT_NOT_FOUND', status=404)
            rows = conn.execute(
                "SELECT task_id FROM brownfield_baseline_tasks WHERE project_id=? AND profile_id=? AND status='succeeded' LIMIT 2",
                (project_id, profile_id),
            ).fetchall()
            if not rows:
                return {'task': None}
            if len(rows) != 1:
                raise _error('BROWNFIELD_RESULT_AMBIGUOUS')
            task = _owned_task(conn, project_id, rows[0]['task_id'])
            saved = store.get_output(conn, task['task_id'])
            try:
                content = ProjectProfileV2Content.model_validate(json.loads(profile['content_json']))
                generated = ProjectProfileV2Content.model_validate(saved['output']['content'])
                valid = (core.digest(saved['output']) == saved['output_hash'] and
                         _canonicalize(content)[1] == profile['content_hash'] == _canonicalize(generated)[1])
            except (TypeError, KeyError, ValueError):
                valid = False
            if not valid:
                raise _error('BROWNFIELD_RESULT_IDENTITY_INVALID')
            return {'task': _public_task(conn, task)}


@router.get("/tasks/{task_id}")
def get_atlas_task(project_id: int, task_id: str):
    with _safe_errors():
        with closing(get_connection()) as conn:
            return {"task": _public_task(conn, _owned_task(conn, project_id, task_id))}


@router.post("/tasks/{task_id}/resume")
def resume_atlas_task(project_id: int, task_id: str, request: Request):
    with _safe_errors():
        require_local_write_request(request, require_idempotency_key=True)
        with _project_preparation(project_id):
            with closing(get_connection()) as conn:
                task = _owned_task(conn, project_id, task_id)
                if task["status"] not in _PENDING:
                    raise _error("BROWNFIELD_TASK_NOT_RESUMABLE", "此任务已停止，不能重新发送已发起的模型阶段。")
                if _worker_active(project_id, task_id):
                    return {"task": _public_task(conn, task)}
                if any(stage["status"] == "claimed" for stage in store.list_stages(conn, task_id)):
                    store.finish_task(conn, task_id, status="unknown", error_code="BROWNFIELD_UNRESOLVED_CLAIM")
                    return {"task": _public_task(conn, _owned_task(conn, project_id, task_id))}
                _require_project_idle(conn, project_id, resuming_task_id=task_id)
            prepared = core.prepare_baseline(project_id, task["identity"]["plan_profile_id"])
            if core.digest(prepared["identity"]) != task["identity_hash"]:
                raise _error("BROWNFIELD_SCOPE_CHANGED", "原任务的代码、档案或模型已变化，不能继续。")
            _start_worker(prepared, task)
            with closing(get_connection()) as conn:
                return {"task": _public_task(conn, _owned_task(conn, project_id, task_id))}
