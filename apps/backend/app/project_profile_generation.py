"""Initial Project Profile candidate generation from confirmed PRD + controlled Git facts.

This is deliberately separate from the formal-report EvidenceSnapshot/ModelCall chain:
the first Project Profile is an upstream prerequisite of that chain. The endpoint still
uses the shared AI output contract validator and the shared DeepSeek transport, and it
only persists a *candidate* that requires later human confirmation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.ai_contract_validation import validate_ai_response
from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION
from app.db import get_connection
from app.profile_generation_failure import run_pre_send
from app.model_provider_contract import ProviderCredentialRequest, ProviderReceipt
from app.model_provider_runtime import DEFAULT_PROVIDER_ID, resolve_default_model_provider_adapter
from app.git_client import open_workspace_access, resolve_workspace_paths, validate_workspace_paths
from app.prd import _resolve_storage_target
from app.project_profiles import (
    ProjectProfileContent,
    _PROFILE_COLUMNS,
    _canonicalize,
    _profile_dict,
    _validate_content,
)
from app.secret_store import (
    SecretAccessDeniedError,
    SecretNotFoundError,
    SecretStoreOSError,
    UnsupportedSecretStorePlatformError,
)
from app.windows_credential_store import WindowsCredentialStore


router = APIRouter()
SCHEMA_VERSION = "project_profile_generation_v1"
AUTH_SCHEMA_VERSION = "project_profile_generation_authorization_v1"
TASK_TYPE = "project_profile_build"
OUTPUT_SCHEMA_VERSION = "project-profile-build/1.0"
PURPOSE_ID = "anxin_board_project_profile_build_v1"
_PROVIDER_SECRET_REF = "deepseek-api-key"
_MAX_PRD_BYTES = 1024 * 1024
_MAX_REPO_FILE_BYTES = 96 * 1024
_MAX_REPO_CONTEXT_BYTES = 512 * 1024
_MAX_TOP_LEVEL_ENTRIES = 200
_MAX_AI_OUTPUT_TOKENS = 24_000
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_FEATURE_BINDING_RE = re.compile(r"^\[feature:(?P<name>[^\]\r\n]+)\](?:\s|$)")

_secret_store_factory = WindowsCredentialStore
_provider_adapter_resolver = resolve_default_model_provider_adapter

_COMMON_CONTEXT_FILES = (
    "README.md",
    "README.MD",
    "README.txt",
    "README",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "Cargo.toml",
    "go.mod",
    "composer.json",
)
_SCOPE_TO_PATH_TYPE = {
    "前端": "frontend",
    "后端": "backend",
    "数据库": "data",
    "测试": "test",
    "接口": "backend",
    "配置": "other",
    "跨模块": "other",
    "暂时无法确认": "other",
}


class GenerateProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    authorized: Literal[True]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def ensure_profile_generation_schema() -> None:
    """Install small append-only provenance tables for this pre-profile AI lane."""

    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profile_generation_authorizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                purpose_id TEXT NOT NULL,
                prd_id INTEGER NOT NULL,
                prd_source_hash TEXT NOT NULL,
                git_remote_head TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                authorized_at TEXT NOT NULL,
                authorization_hash TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS profile_generation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                authorization_id INTEGER NOT NULL,
                candidate_profile_id INTEGER NOT NULL UNIQUE,
                local_task_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                provider_response_id TEXT NOT NULL,
                actual_model TEXT NOT NULL,
                provider_runtime_fingerprint TEXT NOT NULL,
                prd_id INTEGER NOT NULL,
                prd_source_hash TEXT NOT NULL,
                git_remote_head TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                validated_result_json TEXT NOT NULL,
                validated_result_hash TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL CHECK (prompt_tokens >= 0),
                completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),
                total_tokens INTEGER NOT NULL CHECK (total_tokens = prompt_tokens + completion_tokens),
                created_at TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS trg_profile_generation_authorization_no_update
            BEFORE UPDATE ON profile_generation_authorizations
            BEGIN SELECT RAISE(ABORT, 'profile generation authorization is append-only'); END;

            CREATE TRIGGER IF NOT EXISTS trg_profile_generation_authorization_no_delete
            BEFORE DELETE ON profile_generation_authorizations
            BEGIN SELECT RAISE(ABORT, 'profile generation authorization is append-only'); END;

            CREATE TRIGGER IF NOT EXISTS trg_profile_generation_run_no_update
            BEFORE UPDATE ON profile_generation_runs
            BEGIN SELECT RAISE(ABORT, 'profile generation run is append-only'); END;

            CREATE TRIGGER IF NOT EXISTS trg_profile_generation_run_no_delete
            BEFORE DELETE ON profile_generation_runs
            BEGIN SELECT RAISE(ABORT, 'profile generation run is append-only'); END;
            """
        )


def _read_current_inputs(project_id: int) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    try:
        with get_connection() as conn:
            project_row = conn.execute(
                "SELECT id, name, git_url, branch FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise _error(404, "PROJECT_NOT_FOUND", "项目不存在或已被删除。")
            prd_row = conn.execute(
                """
                SELECT id, source_hash, parsed_path, parsed_hash, document_fingerprint
                FROM prd_versions
                WHERE project_id = ? AND status = 'parse_confirmed'
                ORDER BY version_no DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if prd_row is None:
                raise _error(409, "PROFILE_GENERATION_PRD_REQUIRED", "请先确认当前 PRD 解析版本。")
            git_row = conn.execute(
                """
                SELECT status, checked_url_hash, checked_branch, remote_head, local_head, workspace_rel_path
                FROM project_git_connections WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _error(500, "PROFILE_GENERATION_INPUT_READ_FAILED", "无法读取项目生成前置状态。") from exc

    project = dict(project_row)
    prd = dict(prd_row)
    if git_row is None:
        raise _error(409, "PROFILE_GENERATION_GIT_CHECK_REQUIRED", "请先在“项目初始化”执行 Git 连接检查。")
    git_state = dict(git_row)
    git_url = project.get("git_url")
    branch = project.get("branch")
    expected_url_hash = hashlib.sha256(str(git_url or "").encode("utf-8")).hexdigest()
    if (
        git_state.get("status") != "connected"
        or not git_url
        or not branch
        or git_state.get("checked_url_hash") != expected_url_hash
        or git_state.get("checked_branch") != branch
        or git_state.get("workspace_rel_path") != "repo"
        or git_state.get("remote_head") != git_state.get("local_head")
        or type(git_state.get("remote_head")) is not str
        or _SHA_RE.fullmatch(git_state["remote_head"]) is None
    ):
        raise _error(
            409,
            "PROFILE_GENERATION_GIT_CHECK_REQUIRED",
            "当前 Git 连接状态与项目配置不一致，请回到“项目初始化”重新检查连接。",
        )
    return project, prd, git_state


def _read_confirmed_prd(prd: dict[str, object]) -> tuple[str, str]:
    parsed_path = prd.get("parsed_path")
    parsed_hash = prd.get("parsed_hash")
    if type(parsed_path) is not str or type(parsed_hash) is not str:
        raise _error(409, "PROFILE_GENERATION_PRD_INVALID", "当前 PRD 缺少可验证的解析文本。")
    try:
        target = _resolve_storage_target(parsed_path)
        data = target.read_bytes()
    except (HTTPException, OSError) as exc:
        raise _error(409, "PROFILE_GENERATION_PRD_INVALID", "当前 PRD 解析文本无法安全读取。") from exc
    if not data or len(data) > _MAX_PRD_BYTES or _hash_bytes(data) != parsed_hash:
        raise _error(409, "PROFILE_GENERATION_PRD_INVALID", "当前 PRD 解析文本完整性无法确认。")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error(409, "PROFILE_GENERATION_PRD_INVALID", "当前 PRD 解析文本编码无效。") from exc
    return text, f"prd-{str(prd['source_hash'])[:24]}"


def _read_small_repo_file(repo_root: Path, name: str) -> str | None:
    target = repo_root / name
    try:
        if not target.exists() or not target.is_file() or target.is_symlink():
            return None
        info = target.stat()
        if info.st_size <= 0 or info.st_size > _MAX_REPO_FILE_BYTES:
            return None
        data = target.read_bytes()
        if b"\x00" in data:
            return None
        return data.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _read_repo_context(project_id: int, project: dict[str, object], git_state: dict[str, object]) -> tuple[list[dict[str, str]], set[str]]:
    attempt_id = "0" * 32
    try:
        paths = resolve_workspace_paths(project_id, attempt_id, create=False)
        validate_workspace_paths(paths)
        if not paths.repo.exists() or not paths.repo.is_dir() or paths.repo.is_symlink():
            raise _error(409, "PROFILE_GENERATION_GIT_WORKSPACE_REQUIRED", "受控 Git 工作区不存在，请重新执行连接检查。")
        access = open_workspace_access(paths, str(project["git_url"]))
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(409, "PROFILE_GENERATION_GIT_WORKSPACE_REQUIRED", "受控 Git 工作区无法安全打开，请重新执行连接检查。") from exc

    items: list[dict[str, str]] = []
    allowed: set[str] = set()
    total_bytes = 0
    try:
        repo_root = access.path
        try:
            names = sorted(
                entry.name
                for entry in repo_root.iterdir()
                if entry.name not in {".git", ".venv", "node_modules", "dist", "build", "__pycache__"}
            )[:_MAX_TOP_LEVEL_ENTRIES]
        except OSError as exc:
            raise _error(409, "PROFILE_GENERATION_GIT_WORKSPACE_REQUIRED", "无法读取受控 Git 工作区顶层结构。") from exc
        structure_text = "\n".join(names)
        structure_id = f"repo-structure-{str(git_state['remote_head'])[:16]}"
        items.append({"evidence_id": structure_id, "path": "<top-level>", "content": structure_text})
        allowed.add(structure_id)
        total_bytes += len(structure_text.encode("utf-8"))

        seen: set[str] = set()
        for name in _COMMON_CONTEXT_FILES:
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            text = _read_small_repo_file(repo_root, name)
            if text is None:
                continue
            encoded_size = len(text.encode("utf-8"))
            if total_bytes + encoded_size > _MAX_REPO_CONTEXT_BYTES:
                break
            digest = hashlib.sha256((name + "\n" + text).encode("utf-8")).hexdigest()
            evidence_id = f"repo-{digest[:24]}"
            items.append({"evidence_id": evidence_id, "path": name, "content": text})
            allowed.add(evidence_id)
            total_bytes += encoded_size
    finally:
        access.close()

    return items, allowed


def _build_prompt(
    *,
    project: dict[str, object],
    prd_text: str,
    prd_evidence_id: str,
    repo_items: list[dict[str, str]],
) -> tuple[list[dict[str, str]], set[str]]:
    allowed = {prd_evidence_id, *(item["evidence_id"] for item in repo_items)}
    context = {
        "project_name": project["name"],
        "prd": {"evidence_id": prd_evidence_id, "content": prd_text},
        "repository_context": repo_items,
        "allowed_evidence_ids": sorted(allowed),
    }
    system = (
        "你是研发项目基础档案候选生成器。只根据给定证据生成候选，不得声称候选已确认或已生效。"
        "你的首要任务是把已确认 PRD 与 repository_context 中的真实源码逐项对账，识别当前代码实际上实现的功能模块，而不是照抄 PRD。"
        "features 代表当前实现候选：每个 feature 必须至少有一个真实仓库代码证据（repo-code-*）；repo-tree-* 只能证明文件清单存在，不能单独证明功能已实现。"
        "仅 PRD 提到但源码中找不到实现证据的需求，不得放进 features 冒充已实现，必须放入 unmapped_areas，并说明是 PRD-only/尚未确认实现。"
        "源码中确实存在但 PRD 未覆盖的真实功能可以进入 features，但必须引用仓库代码证据，并在 description 明确指出代码存在而 PRD 未覆盖，交由项目经理确认。"
        "只返回一个严格 JSON object，不要 Markdown，不要解释。顶层字段必须且只能是："
        "project_summary, features, module_path_candidates, terminology, exclusion_rule_candidates, "
        "analysis_rule_candidates, unmapped_areas, evidence_refs。"
        "features 每项字段必须且只能是 name, description, source_type, evidence_ids, candidate；candidate 必须 true。"
        "module_path_candidates 每项字段必须且只能是 path, description, implementation_scope, source_type, evidence_ids, candidate；candidate 必须 true，并至少引用一个 repo-code-* 证据。"
        "terminology 每项字段必须且只能是 term, definition, source_type, evidence_ids, candidate；candidate 必须 true。"
        "exclusion_rule_candidates 与 analysis_rule_candidates 每项字段必须且只能是 content, reason, source_type, evidence_ids, candidate；candidate 必须 true。"
        "unmapped_areas 每项字段必须且只能是 area, reason, implementation_scope, source_type, evidence_ids, candidate；candidate 必须 true。"
        "source_type 只能使用 git_fact, prd_fact, ai_analysis, pm_external_fact, fixed_disclaimer。"
        "implementation_scope 只能使用 前端, 后端, 数据库, 接口, 测试, 配置, 跨模块, 暂时无法确认。"
        "所有 evidence_ids 和 evidence_refs 只能引用 allowed_evidence_ids 中实际存在的字符串；证据不足就保守列入 unmapped_areas，不得编造路径。"
        "每个 module_path_candidates.description 必须以 [feature:<功能精确名称>] 开头，其中功能精确名称必须逐字等于 features 中唯一一项的 name；标记后再写路径说明。"
        "不要用近似名称、别名或模糊描述代替这个绑定标记；无法唯一绑定到 feature 的路径应保守放入 unmapped_areas，而不是猜测归组。"
    )
    user = "请为以下真实项目生成首次 Project Profile 候选：\n" + _canonical_json(context)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], allowed


def _read_provider_credential() -> str:
    try:
        return _secret_store_factory().get(_PROVIDER_SECRET_REF)
    except SecretNotFoundError as exc:
        raise _error(409, "PROFILE_GENERATION_CREDENTIAL_REQUIRED", "请先在“设置与备份”中保存 DeepSeek API Key。") from exc
    except UnsupportedSecretStorePlatformError as exc:
        raise _error(409, "PROFILE_GENERATION_CREDENTIAL_UNSUPPORTED", "当前系统不支持 Windows 安全凭据读取。") from exc
    except SecretAccessDeniedError as exc:
        raise _error(403, "PROFILE_GENERATION_CREDENTIAL_ACCESS_DENIED", "Windows 拒绝读取该安全凭据。") from exc
    except SecretStoreOSError as exc:
        raise _error(500, "PROFILE_GENERATION_CREDENTIAL_READ_FAILED", "读取模型凭据失败，未回显凭据内容。") from exc


def _authorization_payload(
    project_id: int,
    prd: dict[str, object],
    git_state: dict[str, object],
    allowed: set[str],
    *,
    provider: str,
) -> dict[str, object]:
    scope = {
        "project_id": project_id,
        "provider": provider,
        "purpose_id": PURPOSE_ID,
        "prd_id": prd["id"],
        "prd_source_hash": prd["source_hash"],
        "git_remote_head": git_state["remote_head"],
        "allowed_evidence_ids": sorted(allowed),
    }
    scope_hash = _hash_json(scope)
    authorized_at = _now()
    authorization = {
        "schema_version": AUTH_SCHEMA_VERSION,
        **scope,
        "scope_hash": scope_hash,
        "authorized_at": authorized_at,
    }
    authorization["authorization_hash"] = _hash_json(authorization)
    return authorization


def _record_authorization(authorization: dict[str, object]) -> int:
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO profile_generation_authorizations (
                    schema_version, project_id, provider, purpose_id, prd_id,
                    prd_source_hash, git_remote_head, scope_hash, authorized_at, authorization_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authorization["schema_version"], authorization["project_id"], authorization["provider"],
                    authorization["purpose_id"], authorization["prd_id"], authorization["prd_source_hash"],
                    authorization["git_remote_head"], authorization["scope_hash"], authorization["authorized_at"],
                    authorization["authorization_hash"],
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT id FROM profile_generation_authorizations WHERE authorization_hash = ?",
                    (authorization["authorization_hash"],),
                ).fetchone()
                if row is not None:
                    return int(row["id"])
        except sqlite3.Error:
            pass
        raise _error(500, "PROFILE_GENERATION_AUTHORIZATION_SAVE_FAILED", "无法保存本次数据发送授权记录。")
    except sqlite3.Error as exc:
        raise _error(500, "PROFILE_GENERATION_AUTHORIZATION_SAVE_FAILED", "无法保存本次数据发送授权记录。") from exc


def _receipt_dict(receipt: ProviderReceipt) -> dict[str, object]:
    return {
        "provider": receipt.provider,
        "provider_response_id": receipt.provider_response_id,
        "actual_model": receipt.actual_model,
        "provider_runtime_fingerprint": receipt.provider_runtime_fingerprint,
        "finish_reason": receipt.finish_reason,
        "prompt_tokens": receipt.prompt_tokens,
        "completion_tokens": receipt.completion_tokens,
        "total_tokens": receipt.total_tokens,
        "result": dict(receipt.result),
    }


def _ai_envelope(local_task_id: str, receipt: ProviderReceipt, capability: object) -> dict[str, object]:
    actual_model = receipt.actual_model
    runtime_fingerprint = receipt.provider_runtime_fingerprint
    provider = getattr(capability, "provider", None)
    model_id = getattr(capability, "model_id", None)
    if (
        receipt.provider != provider
        or actual_model != model_id
        or type(runtime_fingerprint) is not str
        or not runtime_fingerprint
    ):
        raise _error(502, "PROFILE_GENERATION_MODEL_IDENTITY_MISMATCH", "模型返回的实际 provider/model 身份与本次候选生成要求不一致。")
    return {
        "local_task_id": local_task_id,
        "provider_request_id": receipt.provider_response_id,
        "task_type": TASK_TYPE,
        "status": "succeeded",
        "schema_version": AI_CONTRACT_SCHEMA_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "actual_model": {"provider": provider, "id": actual_model, "version": runtime_fingerprint},
        "usage": {"input_tokens": receipt.prompt_tokens, "output_tokens": receipt.completion_tokens},
        "cost": None,
        "billing_status": "unknown",
        "warnings": [],
        "result": dict(receipt.result),
    }


def _feature_binding_name(description: str) -> str | None:
    match = _FEATURE_BINDING_RE.match(description)
    if match is None:
        return None
    name = match.group("name").strip()
    return name or None


def _path_description_body(description: str) -> str:
    match = _FEATURE_BINDING_RE.match(description)
    if match is None:
        return description
    return description[match.end():].strip()


def _repo_code_evidence_ids(repo_items: list[dict[str, str]]) -> set[str]:
    return {
        item["evidence_id"]
        for item in repo_items
        if type(item.get("evidence_id")) is str and item["evidence_id"].startswith("repo-code-")
    }


def _validate_repository_backing(result: dict[str, object], repo_items: list[dict[str, str]]) -> None:
    """Reject implemented-feature claims that are not backed by actual code evidence.

    The generic AI contract validator proves that an evidence id is in the allowed envelope.
    This semantic check is stricter: a PRD id or repo-tree inventory id is not implementation
    evidence. Implemented features and their returned code-path candidates must cite at least
    one exact ``repo-code-*`` blob from the controlled exact-HEAD context.
    """

    code_ids = _repo_code_evidence_ids(repo_items)
    if not code_ids:
        raise _error(
            502,
            "PROFILE_GENERATION_AI_RESULT_INVALID",
            "当前候选没有可验证的仓库代码证据，不能形成已实现功能模块候选。",
        )

    for item in result.get("features", []):
        if not isinstance(item, dict):
            continue
        evidence_ids = item.get("evidence_ids") or []
        if not any(type(value) is str and value in code_ids for value in evidence_ids):
            name = str(item.get("name") or "").strip() or "未命名功能"
            raise _error(
                502,
                "PROFILE_GENERATION_AI_RESULT_INVALID",
                f"功能“{name}”缺少真实仓库代码证据，不能作为已实现功能进入候选。",
            )

    for item in result.get("module_path_candidates", []):
        if not isinstance(item, dict):
            continue
        evidence_ids = item.get("evidence_ids") or []
        if not any(type(value) is str and value in code_ids for value in evidence_ids):
            path = str(item.get("path") or "").strip() or "未命名路径"
            raise _error(
                502,
                "PROFILE_GENERATION_AI_RESULT_INVALID",
                f"代码路径候选“{path}”缺少真实仓库代码证据，不能进入功能归组。",
            )


def _unique_feature_match(description: str, features: list[dict[str, object]]) -> int | None:
    bound_name = _feature_binding_name(description)
    if bound_name is not None:
        target = bound_name.casefold()
        exact_matches = [
            index
            for index, feature in enumerate(features)
            if (name := str(feature.get("name") or "").strip()) and name.casefold() == target
        ]
        return exact_matches[0] if len(exact_matches) == 1 else None

    # Backward-compatible fallback for historical/provider responses created before the
    # deterministic marker existed. It may recover an unambiguous legacy response, but
    # an explicit invalid marker never falls through to fuzzy matching.
    if description.startswith("[feature:"):
        return None
    folded = description.casefold()
    matches = [
        index
        for index, feature in enumerate(features)
        if (name := str(feature.get("name") or "").strip()) and name.casefold() in folded
    ]
    return matches[0] if len(matches) == 1 else None


def _to_profile_content(result: dict[str, object]) -> ProjectProfileContent:
    features = [item for item in result.get("features", []) if isinstance(item, dict)]
    modules: list[dict[str, object]] = []
    for index, feature in enumerate(features):
        description = str(feature.get("description") or "").strip()
        modules.append(
            {
                "client_id": f"ai-feature-{index + 1:03d}",
                "name": str(feature.get("name") or "").strip(),
                "description": description,
                "prd_refs": list(feature.get("evidence_ids") or []),
                "requirements": [description] if description else [],
                "paths": [],
                "exclusions": [],
            }
        )

    unmatched_index = 0
    for item in result.get("module_path_candidates", []):
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        visible_description = _path_description_body(description)
        path_value = str(item.get("path") or "").strip().replace("\\", "/")
        path = {
            "type": _SCOPE_TO_PATH_TYPE.get(str(item.get("implementation_scope") or ""), "other"),
            "pattern": path_value,
            "required": True,
            "note": visible_description[:500],
        }
        match = _unique_feature_match(description, features)
        if match is not None and match < len(modules):
            modules[match]["paths"].append(path)
            for evidence_id in item.get("evidence_ids") or []:
                if evidence_id not in modules[match]["prd_refs"]:
                    modules[match]["prd_refs"].append(evidence_id)
            continue
        unmatched_index += 1
        modules.append(
            {
                "client_id": f"ai-unmapped-{unmatched_index:03d}",
                "name": f"待人工归组 {unmatched_index}",
                "description": visible_description or description or "AI 给出了代码路径候选，但未能唯一映射到某个功能。",
                "prd_refs": list(item.get("evidence_ids") or []),
                "requirements": [visible_description or description or "请项目经理确认该代码区域对应的功能模块。"],
                "paths": [path],
                "exclusions": [],
            }
        )

    glossary = []
    for item in result.get("terminology", []):
        if not isinstance(item, dict):
            continue
        glossary.append(
            {
                "term": str(item.get("term") or "")[:100],
                "definition": str(item.get("definition") or "")[:500],
                "aliases": [],
            }
        )

    exclusions = [
        str(item.get("content") or "")[:500]
        for item in result.get("exclusion_rule_candidates", [])
        if isinstance(item, dict) and str(item.get("content") or "").strip()
    ]
    notes_parts = []
    for item in result.get("analysis_rule_candidates", []):
        if isinstance(item, dict) and str(item.get("content") or "").strip():
            notes_parts.append("候选分析规则：" + str(item["content"]).strip())
    for item in result.get("unmapped_areas", []):
        if isinstance(item, dict):
            area = str(item.get("area") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if area or reason:
                notes_parts.append(f"未映射区域：{area}｜{reason}")
    notes = "\n".join(notes_parts)[:2000]

    content = ProjectProfileContent.model_validate(
        {
            "schema_version": "project_profile_manual_v1",
            "project_summary": str(result.get("project_summary") or "")[:2000],
            "modules": modules,
            "domain_glossary": glossary,
            "exclude_patterns": exclusions[:100],
            "notes": notes,
        }
    )
    _validate_content(content)
    return content


def _persist_candidate_and_run(
    *,
    project_id: int,
    prd: dict[str, object],
    git_state: dict[str, object],
    authorization_id: int,
    authorization: dict[str, object],
    local_task_id: str,
    prompt_hash: str,
    receipt: dict[str, object],
    validated_result: dict[str, object],
    content: ProjectProfileContent,
    source_plan: dict | None = None,
) -> dict[str, object]:
    canonical, content_hash = _canonicalize(content)
    result_json = _canonical_json(validated_result)
    result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
    created_at = _now()
    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_prd = conn.execute(
                "SELECT id, source_hash FROM prd_versions WHERE project_id = ? AND status = 'parse_confirmed' ORDER BY version_no DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            current_git = conn.execute(
                "SELECT status, remote_head, local_head FROM project_git_connections WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if (
                current_prd is None
                or int(current_prd["id"]) != int(prd["id"])
                or current_prd["source_hash"] != prd["source_hash"]
                or (git_state["remote_head"] and (current_git is None
                or current_git["status"] != "connected"
                or current_git["remote_head"] != git_state["remote_head"]
                or current_git["local_head"] != git_state["remote_head"]))
            ):
                raise _error(409, "PROFILE_GENERATION_SOURCE_CHANGED", "生成期间 PRD 或 Git 身份发生变化，未保存 AI 候选。")
            if source_plan:
                latest_plan = conn.execute("SELECT content_hash,edit_version,status FROM project_profiles WHERE id=? AND project_id=?", (source_plan['id'], project_id)).fetchone()
                if latest_plan is None or latest_plan['content_hash'] != source_plan['content_hash'] or latest_plan['edit_version'] != source_plan['edit_version'] or latest_plan['status'] != source_plan['status']:
                    raise _error(409, 'PROFILE_GENERATION_SOURCE_CHANGED', '模型返回后计划版本已变化，未保存候选。')
            max_row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) FROM project_profiles WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            version_no = int(max_row[0]) + 1
            conn.execute(
                "UPDATE project_profiles SET status = 'superseded' WHERE project_id = ? AND status = 'candidate'",
                (project_id,),
            )
            cursor = conn.execute(
                """
                INSERT INTO project_profiles (
                    project_id, version_no, source_prd_id, status, content_json, content_hash,
                    edit_version, created_at, updated_at
                ) VALUES (?, ?, ?, 'candidate', ?, ?, 1, ?, ?)
                """,
                (project_id, version_no, prd["id"], canonical, content_hash, created_at, created_at),
            )
            profile_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO profile_generation_runs (
                    schema_version, project_id, authorization_id, candidate_profile_id,
                    local_task_id, provider, provider_response_id, actual_model,
                    provider_runtime_fingerprint, prd_id, prd_source_hash, git_remote_head,
                    scope_hash, prompt_hash, validated_result_json, validated_result_hash,
                    prompt_tokens, completion_tokens, total_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SCHEMA_VERSION, project_id, authorization_id, profile_id, local_task_id,
                    receipt["provider"], receipt["provider_response_id"], receipt["actual_model"],
                    receipt["provider_runtime_fingerprint"], prd["id"], prd["source_hash"],
                    git_state["remote_head"], authorization["scope_hash"], prompt_hash,
                    result_json, result_hash, receipt["prompt_tokens"], receipt["completion_tokens"],
                    receipt["total_tokens"], created_at,
                ),
            )
            row = conn.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM project_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            conn.commit()
            return _profile_dict(row)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise _error(500, "PROFILE_GENERATION_SAVE_FAILED", "AI 候选已生成但无法安全落库，未形成可用候选。") from exc


def _prepare_generation(project_id, payload):
    if project_id <= 0 or payload.authorized is not True:
        raise _error(400, "PROFILE_GENERATION_AUTHORIZATION_REQUIRED", "必须由项目经理明确授权本次数据发送。")
    ensure_profile_generation_schema()
    project, prd, git_state = _read_current_inputs(project_id)
    prd_text, prd_evidence_id = _read_confirmed_prd(prd)
    repo_items, repo_evidence = _read_repo_context(project_id, project, git_state)
    messages, allowed = _build_prompt(
        project=project,
        prd_text=prd_text,
        prd_evidence_id=prd_evidence_id,
        repo_items=repo_items,
    )
    if repo_evidence - allowed:
        raise _error(500, "PROFILE_GENERATION_CONTEXT_INVALID", "候选生成上下文证据集合无法闭合。")

    adapter = _provider_adapter_resolver()
    capability = adapter.get_capability(
        task_type=TASK_TYPE, output_schema_version=OUTPUT_SCHEMA_VERSION
    )
    if capability.provider != DEFAULT_PROVIDER_ID:
        raise _error(409, "PROFILE_GENERATION_PROVIDER_INVALID", "当前默认 provider identity 无法闭合。")
    authorization = _authorization_payload(
        project_id, prd, git_state, allowed, provider=capability.provider
    )
    authorization_id = _record_authorization(authorization)
    credential = _read_provider_credential()
    local_task_id = uuid.uuid4().hex
    prompt_hash = _hash_json(messages)
    request = ProviderCredentialRequest(
        local_task_id=local_task_id,
        provider=capability.provider,
        model_id=capability.model_id,
        model_version=capability.model_version,
        task_type=TASK_TYPE,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        messages=tuple(messages),
        max_output_tokens=_MAX_AI_OUTPUT_TOKENS,
    )

    return project, prd, git_state, repo_items, allowed, adapter, capability, authorization, authorization_id, credential, local_task_id, prompt_hash, request


@router.post("/api/projects/{project_id}/profile-candidates/generate", status_code=201)
def generate_profile_candidate(project_id: int, payload: GenerateProfilePayload) -> dict[str, object]:
    """Explicit Human-authorized one-shot Project Profile candidate generation."""

    (project, prd, git_state, repo_items, allowed, adapter, capability, authorization, authorization_id, credential, local_task_id, prompt_hash, request) = run_pre_send(lambda: _prepare_generation(project_id, payload))

    try:
        provider_receipt = adapter.execute_with_credential(request, credential)
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(502, "PROFILE_GENERATION_PROVIDER_FAILED", "模型调用未完成，未保存候选。") from exc

    receipt = _receipt_dict(provider_receipt)
    envelope = _ai_envelope(local_task_id, provider_receipt, capability)
    validation = validate_ai_response(envelope, allowed)
    if not validation.valid:
        first = validation.issues[0] if validation.issues else None
        message = first.message if first is not None else "AI 返回内容不符合项目档案候选契约。"
        raise _error(502, "PROFILE_GENERATION_AI_RESULT_INVALID", message)
    result = envelope["result"]
    if not isinstance(result, dict):
        raise _error(502, "PROFILE_GENERATION_AI_RESULT_INVALID", "AI 返回的候选 result 无效。")
    _validate_repository_backing(result, repo_items)
    content = _to_profile_content(result)
    profile = _persist_candidate_and_run(
        project_id=project_id,
        prd=prd,
        git_state=git_state,
        authorization_id=authorization_id,
        authorization=authorization,
        local_task_id=local_task_id,
        prompt_hash=prompt_hash,
        receipt=receipt,
        validated_result=result,
        content=content,
    )
    return {
        "profile": profile,
        "generation": {
            "task_type": TASK_TYPE,
            "provider": receipt["provider"],
            "actual_model": receipt["actual_model"],
            "scope_hash": authorization["scope_hash"],
            "authorization_recorded": True,
        },
    }
