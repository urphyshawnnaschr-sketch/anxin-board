"""AnxinBoard 最小后端：应用装配、生命周期与公共入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.brownfield_baseline_api import ensure_brownfield_baseline_schema, router as brownfield_baseline_router
from app.brownfield_baseline_report_api import router as brownfield_baseline_report_router
from app.analysis_lineages import router as analysis_lineages_router
from app.anxin_board_generation_api import router as anxin_board_generation_router
from app.anxin_board_reports import router as anxin_board_reports_router
from app.evidence_snapshots import router as evidence_snapshots_router
from app import git_connections as git_connections_module
from app.git_connections import router as git_connections_router
from app.git_human_check_client import HumanCheckGitClient
from app.git_ssh_client import SshEnabledGitClient
from app.git_snapshots import router as git_snapshots_router
from app.local_session_api import (
    configure_local_session_guard,
    invalidate_local_session_guard,
    router as local_session_router,
)
from app.mail_readiness_api import router as mail_readiness_router
from app.mail_settings_api import router as mail_settings_router
from app.model_settings_api import router as model_settings_router
from app.prd import router as prd_router
from app.prd_review_api import router as prd_review_router
from app import project_profile_generation, project_profile_generation_attempt
from app.deepseek_live_profile_adapter import build_live_deepseek_profile_adapter
from app.product_backup_api import router as product_backup_router
from app.project_profile_attempt_diagnostic_api import router as project_profile_attempt_diagnostic_router
from app.project_profile_generation_api import router as project_profile_generation_router
from app.project_profiles import router as project_profiles_router
from app.project_profile_generation_v2 import router as project_profile_v2_router
from app.project_state_baseline_api import (
    ensure_project_state_baseline_schema,
    router as project_state_baseline_router,
)
from app.projects import router as projects_router
from app.recipient_config_api import router as recipient_config_router
from app.report_approval_api import router as report_approval_router
from app.report_generation_preparation_api import router as report_generation_preparation_router
from app.report_generation_task_api import router as report_generation_task_router
from app.report_generation_recovery_api import router as report_generation_recovery_router
from app.report_review import init_report_review_schema
from app import report_review_api as report_review_api_module
from app.report_review_durable_read import (
    get_current_review_bundle as get_current_review_bundle_durable,
    get_review_bundle as get_review_bundle_durable,
)
from app.report_validation_runtime import create_validation_result as create_validation_result_runtime
from app.report_send_authorization_api import router as report_send_authorization_router
from app.wechat_delivery_api import router as wechat_delivery_router
from app.wechat_delivery_store import ensure_wechat_delivery_schema, recover_interrupted_sends
from app.wechat_binding_api import router as wechat_binding_router
from app.wechat_binding_store import ensure_wechat_binding_schema
from app.wechat_binding_service import clear_ephemeral_flows


# Product default: the explicit project Git connection-check action may show the trusted
# Git Credential Manager GUI/browser authorization. Tests and callers that deliberately
# inject another factory remain untouched. Background/base Git clients stay non-interactive.
if git_connections_module._git_client_factory is SshEnabledGitClient:
    git_connections_module._git_client_factory = HumanCheckGitClient

# Owner real-world model convergence: Project Profile generation is upstream of the formal
# ModelCall/qualification chain, so it may use the exact model that the current DeepSeek
# account reports and the user explicitly selects. Formal-report provider authority remains
# on the existing closed-world registry until separately re-qualified.
project_profile_generation._provider_adapter_resolver = build_live_deepseek_profile_adapter

# Basic report viewing closes only against immutable durable report/model facts. It must not
# reacquire the Git workspace or reopen PRD artifacts merely to show an already-generated
# report. Strong source replay remains on validation/reanalysis/send-time action paths.
report_review_api_module.get_current_review_bundle = get_current_review_bundle_durable
report_review_api_module.get_review_bundle = get_review_bundle_durable

# Report-review validation performs Git-backed frozen-candidate closure before taking the
# SQLite writer slot. The API module resolves this global at request time, so assembly owns
# the short-write-window implementation without weakening its existing route contract.
report_review_api_module.create_validation_result = create_validation_result_runtime
report_review_router = report_review_api_module.router

# Preflight happens before project/PRD/code content is sent, so these failures are safely
# retryable after correction. A concrete provider HTTP response after POST is also a known
# failed attempt. Transport EOF/timeout after POST is intentionally absent and stays UNKNOWN.
project_profile_generation_attempt._SAFE_PRE_SEND_CODES.update(
    {
        "PROFILE_GENERATION_MODEL_SELECTION_REQUIRED",
        "PROFILE_GENERATION_MODEL_SELECTION_STALE",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_AUTH_FAILED",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_FORBIDDEN",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_RATE_LIMITED",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_NETWORK_ERROR",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_PROVIDER_UNAVAILABLE",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_HTTP_FAILED",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_RESPONSE_INVALID",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_NO_MODELS",
        "PROFILE_GENERATION_MODEL_PREFLIGHT_FAILED",
        "PROFILE_GENERATION_REPO_TREE_INVALID",
        "PROFILE_GENERATION_REPO_TREE_TOO_LARGE",
        "PROFILE_GENERATION_REPO_FILE_TOO_LARGE",
        "PROFILE_GENERATION_REPO_BLOB_INVALID",
        "PROFILE_GENERATION_REPO_TEXT_ENCODING_UNSUPPORTED",
        "PROFILE_GENERATION_REPO_CONTEXT_TOO_LARGE",
        "SENSITIVE_PATH_POLICY_INTERNAL_POLICY_INCONSISTENT",
    }
)
project_profile_generation_attempt._KNOWN_AFTER_SEND_CODES.update(
    {
        "PROFILE_GENERATION_PROVIDER_REQUEST_REJECTED",
        "PROFILE_GENERATION_PROVIDER_AUTH_FAILED",
        "PROFILE_GENERATION_PROVIDER_FORBIDDEN",
        "PROFILE_GENERATION_PROVIDER_MODEL_UNAVAILABLE",
        "PROFILE_GENERATION_PROVIDER_RATE_LIMITED",
        "PROFILE_GENERATION_PROVIDER_UNAVAILABLE",
        "PROFILE_GENERATION_PROVIDER_HTTP_FAILED",
        "PROFILE_GENERATION_PROVIDER_PAYMENT_REQUIRED",
    }
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_report_review_schema()
    project_profile_generation.ensure_profile_generation_schema()
    ensure_project_state_baseline_schema()
    ensure_brownfield_baseline_schema()
    ensure_wechat_delivery_schema()
    ensure_wechat_binding_schema()
    clear_ephemeral_flows()
    recover_interrupted_sends()
    configure_local_session_guard(enable_handoff=True)
    try:
        yield
    finally:
        clear_ephemeral_flows()
        invalidate_local_session_guard()


app = FastAPI(title="AnxinBoard API", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(local_session_router)
app.include_router(projects_router)
app.include_router(prd_router)
app.include_router(prd_review_router)
app.include_router(git_connections_router)
app.include_router(project_profiles_router)
app.include_router(project_profile_generation_router)
app.include_router(project_profile_attempt_diagnostic_router)
app.include_router(model_settings_router)
app.include_router(mail_settings_router)
app.include_router(recipient_config_router)
app.include_router(mail_readiness_router)
app.include_router(product_backup_router)
app.include_router(analysis_lineages_router)
app.include_router(git_snapshots_router)
app.include_router(evidence_snapshots_router)
app.include_router(anxin_board_reports_router)
app.include_router(anxin_board_generation_router)
app.include_router(report_generation_task_router)
app.include_router(report_generation_recovery_router)
app.include_router(report_generation_preparation_router)
app.include_router(report_send_authorization_router)
app.include_router(report_review_router)
app.include_router(report_approval_router)
app.include_router(project_profile_v2_router)
app.include_router(project_state_baseline_router)
app.include_router(brownfield_baseline_router)
app.include_router(brownfield_baseline_report_router)
app.include_router(wechat_delivery_router)
app.include_router(wechat_binding_router)
