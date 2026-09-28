"""数据库访问层：数据库路径解析、连接与幂等兼容升级。"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_REPORT_GENERATION_ACTIVE_CHECKPOINT_INDEX = (
    "ux_report_generation_tasks_active_checkpoint"
)
_REPORT_GENERATION_ACTIVE_STATES = ("queued", "running", "unknown")


def get_db_path() -> Path:
    """数据库路径：默认 %LOCALAPPDATA%\\AnxinBoard\\anxinboard.db，可用环境变量覆盖（测试用）。"""
    override = os.environ.get("ANXINBOARD_DB_PATH")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set; cannot locate the database directory")
    return Path(local_app_data) / "AnxinBoard" / "anxinboard.db"


def get_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_report_validation_schema() -> None:
    """Install and attest the append-only ValidationResult schema on a write path."""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS report_validation_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                report_version_id INTEGER NOT NULL,
                report_state_version INTEGER NOT NULL,
                report_content_hash TEXT NOT NULL,
                supplement_version_id INTEGER,
                supplement_content_hash TEXT,
                evidence_snapshot_id INTEGER NOT NULL,
                evidence_snapshot_hash TEXT NOT NULL,
                git_snapshot_id INTEGER NOT NULL,
                git_facts_hash TEXT NOT NULL,
                model_execution_result_id INTEGER NOT NULL,
                execution_result_hash TEXT NOT NULL,
                model_call_id INTEGER NOT NULL,
                call_identity_hash TEXT NOT NULL,
                candidate_hash TEXT NOT NULL,
                current_authority_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('passed', 'blocked')),
                checks_json TEXT NOT NULL,
                blockers_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                UNIQUE(candidate_hash, current_authority_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_report_validation_report
            ON report_validation_results(report_version_id, id DESC);

            CREATE TRIGGER IF NOT EXISTS trg_report_validation_no_update
            BEFORE UPDATE ON report_validation_results
            BEGIN
                SELECT RAISE(ABORT, 'report_validation_results is append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_report_validation_no_delete
            BEFORE DELETE ON report_validation_results
            BEGIN
                SELECT RAISE(ABORT, 'report_validation_results is append-only');
            END;
            """
        )
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(report_validation_results)"
            ).fetchall()
        }
        required = {
            "git_snapshot_id",
            "git_facts_hash",
            "candidate_hash",
            "current_authority_hash",
            "result_hash",
        }
        if not required.issubset(columns):
            raise sqlite3.IntegrityError(
                "report_validation_results has an obsolete schema"
            )


def ensure_report_contradiction_schema() -> None:
              """Install and attest the immutable Page07 contradiction-request schema."""
              with get_connection() as conn:
                  conn.executescript(
                      """
                      CREATE TABLE IF NOT EXISTS report_contradiction_requests (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          schema_version TEXT NOT NULL,
                          project_id INTEGER NOT NULL,
                          report_version_id INTEGER NOT NULL,
                          report_content_hash TEXT NOT NULL,
                          source_model_execution_result_id INTEGER NOT NULL,
                          source_execution_result_hash TEXT NOT NULL,
                          evidence_snapshot_id INTEGER NOT NULL,
                          evidence_snapshot_hash TEXT NOT NULL,
                          supplement_version_id INTEGER NOT NULL,
                          supplement_content_hash TEXT NOT NULL,
                          supplement_source_type TEXT NOT NULL,
                          supplement_provided_by TEXT NOT NULL,
                          supplement_provided_at TEXT NOT NULL,
                          supplement_provided_timezone TEXT NOT NULL,
                          local_task_id TEXT NOT NULL UNIQUE,
                          task_identity_hash TEXT NOT NULL UNIQUE,
                          task_type TEXT NOT NULL CHECK (task_type = 'report_contradiction_check'),
                          created_at TEXT NOT NULL,
                          request_hash TEXT NOT NULL UNIQUE,
                          UNIQUE(report_version_id, supplement_version_id)
                      );
                      CREATE INDEX IF NOT EXISTS idx_report_contradiction_project_report
                      ON report_contradiction_requests(project_id, report_version_id, id DESC);
                      CREATE TRIGGER IF NOT EXISTS trg_report_contradiction_no_update
                      BEFORE UPDATE ON report_contradiction_requests
                      BEGIN
                          SELECT RAISE(ABORT, 'report_contradiction_requests is append-only');
                      END;
                      CREATE TRIGGER IF NOT EXISTS trg_report_contradiction_no_delete
                      BEFORE DELETE ON report_contradiction_requests
                      BEGIN
                          SELECT RAISE(ABORT, 'report_contradiction_requests is append-only');
                      END;

                      CREATE TABLE IF NOT EXISTS report_contradiction_send_claims (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          schema_version TEXT NOT NULL CHECK (schema_version = 'report_contradiction_send_claim_v1'),
                          project_id INTEGER NOT NULL,
                          report_version_id INTEGER NOT NULL,
                          contradiction_request_id INTEGER NOT NULL UNIQUE,
                          local_task_id TEXT NOT NULL,
                          task_identity_hash TEXT NOT NULL,
                          model_call_id INTEGER NOT NULL UNIQUE,
                          call_identity_hash TEXT NOT NULL UNIQUE,
                          claimed_at TEXT NOT NULL,
                          claim_hash TEXT NOT NULL UNIQUE
                      );
                      CREATE INDEX IF NOT EXISTS idx_report_contradiction_send_claim_project
                      ON report_contradiction_send_claims(project_id, report_version_id, id DESC);
                      CREATE TRIGGER IF NOT EXISTS trg_report_contradiction_send_claim_no_update
                      BEFORE UPDATE ON report_contradiction_send_claims
                      BEGIN
                          SELECT RAISE(ABORT, 'report_contradiction_send_claims is append-only');
                      END;
                      CREATE TRIGGER IF NOT EXISTS trg_report_contradiction_send_claim_no_delete
                      BEFORE DELETE ON report_contradiction_send_claims
                      BEGIN
                          SELECT RAISE(ABORT, 'report_contradiction_send_claims is append-only');
                      END;
                      """
                  )
                  columns = {row["name"] for row in conn.execute("PRAGMA table_info(report_contradiction_requests)").fetchall()}
                  required = {
                      "id", "schema_version", "project_id", "report_version_id",
                      "report_content_hash", "source_model_execution_result_id",
                      "source_execution_result_hash", "evidence_snapshot_id",
                      "evidence_snapshot_hash", "supplement_version_id",
                      "supplement_content_hash", "supplement_source_type",
                      "supplement_provided_by", "supplement_provided_at",
                      "supplement_provided_timezone", "local_task_id",
                      "task_identity_hash", "task_type", "created_at", "request_hash",
                  }
                  if columns != required:
                      raise sqlite3.IntegrityError("report_contradiction_requests has an obsolete schema")
                  claim_columns = {row["name"] for row in conn.execute("PRAGMA table_info(report_contradiction_send_claims)").fetchall()}
                  required_claim_columns = {
                      "id", "schema_version", "project_id", "report_version_id",
                      "contradiction_request_id", "local_task_id", "task_identity_hash",
                      "model_call_id", "call_identity_hash", "claimed_at", "claim_hash",
                  }
                  if claim_columns != required_claim_columns:
                      raise sqlite3.IntegrityError("report_contradiction_send_claims has an obsolete schema")


def _ensure_report_generation_active_checkpoint_index(
    conn: sqlite3.Connection,
) -> None:
    invalid_state = conn.execute(
        """
        SELECT id
        FROM report_generation_tasks
        WHERE state IS NULL
           OR state NOT IN ('queued', 'running', 'succeeded', 'failed', 'unknown', 'voided')
        LIMIT 1
        """
    ).fetchone()
    if invalid_state is not None:
        raise sqlite3.IntegrityError(
            "report_generation_tasks contains an unsupported stored state"
        )

    conn.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_REPORT_GENERATION_ACTIVE_CHECKPOINT_INDEX}
        ON report_generation_tasks(project_id, evidence_snapshot_id)
        WHERE state IN {_REPORT_GENERATION_ACTIVE_STATES}
        """
    )

    index_rows = [
        row
        for row in conn.execute(
            "PRAGMA index_list(report_generation_tasks)"
        ).fetchall()
        if row["name"] == _REPORT_GENERATION_ACTIVE_CHECKPOINT_INDEX
    ]
    if (
        len(index_rows) != 1
        or index_rows[0]["unique"] != 1
        or index_rows[0]["partial"] != 1
    ):
        raise sqlite3.IntegrityError(
            "active checkpoint index does not have the required unique partial shape"
        )

    columns = [
        row["name"]
        for row in conn.execute(
            f"PRAGMA index_info({_REPORT_GENERATION_ACTIVE_CHECKPOINT_INDEX})"
        ).fetchall()
    ]
    if columns != ["project_id", "evidence_snapshot_id"]:
        raise sqlite3.IntegrityError(
            "active checkpoint index does not bind the required identity"
        )

    schema_row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index' AND name = ?
        """,
        (_REPORT_GENERATION_ACTIVE_CHECKPOINT_INDEX,),
    ).fetchone()
    if schema_row is None or type(schema_row["sql"]) is not str:
        raise sqlite3.IntegrityError(
            "active checkpoint index schema cannot be attested"
        )
    canonical_sql = "".join(schema_row["sql"].casefold().split())
    predicate = canonical_sql.partition("where")[2]
    if predicate != "statein('queued','running','unknown')":
        raise sqlite3.IntegrityError(
            "active checkpoint index predicate does not match the frozen lifecycle"
        )


def init_db() -> None:
    """幂等兼容升级：全新库建表，老库只补缺失字段并回填数据。"""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "git_url" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN git_url TEXT NULL")
        if "branch" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN branch TEXT NOT NULL DEFAULT 'main'")
        if "updated_at" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        if "version" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE projects SET updated_at = created_at WHERE updated_at = ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prd_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                parsed_path TEXT NULL,
                parsed_hash TEXT NULL,
                parser_version TEXT NULL,
                structured_path TEXT NULL,
                structured_hash TEXT NULL,
                structured_schema_version TEXT NULL,
                structured_parser_version TEXT NULL,
                document_fingerprint TEXT NULL,
                status TEXT NOT NULL,
                warnings_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                confirmed_by TEXT NULL,
                confirmed_at TEXT NULL,
                UNIQUE (project_id, version_no)
            )
            """
        )
        prd_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(prd_versions)").fetchall()
        }
        structured_prd_columns = {
            "structured_path": "TEXT NULL",
            "structured_hash": "TEXT NULL",
            "structured_schema_version": "TEXT NULL",
            "structured_parser_version": "TEXT NULL",
            "document_fingerprint": "TEXT NULL",
        }
        for column_name, column_type in structured_prd_columns.items():
            if column_name not in prd_columns:
                conn.execute(f"ALTER TABLE prd_versions ADD COLUMN {column_name} {column_type}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_git_connections (
                project_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                attempt_id TEXT NULL,
                checked_url_hash TEXT NULL,
                checked_branch TEXT NULL,
                git_version TEXT NULL,
                remote_head TEXT NULL,
                local_head TEXT NULL,
                workspace_rel_path TEXT NULL,
                started_at TEXT NULL,
                last_checked_at TEXT NULL,
                error_code TEXT NULL,
                error_summary TEXT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                source_prd_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                content_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                edit_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_by TEXT NULL,
                confirmed_at TEXT NULL,
                UNIQUE (project_id, version_no)
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_project_profiles_confirmed
            ON project_profiles(project_id)
            WHERE status = 'confirmed'
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_project_profiles_candidate
            ON project_profiles(project_id)
            WHERE status = 'candidate'
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_lineages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL,
                branch TEXT NOT NULL,
                baseline_commit TEXT NOT NULL,
                status TEXT NOT NULL,
                break_reason TEXT NULL,
                source_git_checked_at TEXT NULL,
                created_at TEXT NOT NULL,
                closed_at TEXT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_analysis_lineages_active
            ON analysis_lineages(project_id)
            WHERE status = 'active'
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS git_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                analysis_lineage_id INTEGER NOT NULL,
                branch TEXT NOT NULL,
                from_commit TEXT NOT NULL,
                to_commit TEXT NOT NULL,
                commits_json TEXT NOT NULL,
                commit_count INTEGER NOT NULL,
                changed_file_count INTEGER NOT NULL,
                added_lines INTEGER NOT NULL,
                deleted_lines INTEGER NOT NULL,
                diff_bytes INTEGER NOT NULL,
                file_manifest_hash TEXT NULL,
                frozen_at TEXT NOT NULL,
                UNIQUE (
                    project_id,
                    analysis_lineage_id,
                    from_commit,
                    to_commit
                )
            )
            """
        )
        git_snapshot_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(git_snapshots)").fetchall()
        }
        if "file_manifest_hash" not in git_snapshot_columns:
            conn.execute(
                "ALTER TABLE git_snapshots ADD COLUMN file_manifest_hash TEXT NULL"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS git_file_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                git_snapshot_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                evidence_id TEXT NOT NULL,
                path TEXT NOT NULL,
                added_lines INTEGER NULL,
                deleted_lines INTEGER NULL,
                is_binary INTEGER NOT NULL CHECK (is_binary IN (0, 1)),
                file_facts_hash TEXT NOT NULL,
                CHECK (
                    (is_binary = 1 AND added_lines IS NULL AND deleted_lines IS NULL)
                    OR
                    (
                        is_binary = 0
                        AND added_lines IS NOT NULL
                        AND deleted_lines IS NOT NULL
                        AND added_lines >= 0
                        AND deleted_lines >= 0
                    )
                ),
                UNIQUE (git_snapshot_id, ordinal),
                UNIQUE (git_snapshot_id, path),
                UNIQUE (evidence_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_snapshots (
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
                prd_structured_hash TEXT NULL,
                prd_document_fingerprint TEXT NULL,
                profile_id INTEGER NOT NULL,
                profile_content_hash TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                frozen_at TEXT NOT NULL,
                UNIQUE (
                    project_id,
                    git_snapshot_id,
                    prd_id,
                    profile_id,
                    schema_version
                )
            )
            """
        )
        evidence_snapshot_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(evidence_snapshots)").fetchall()
        }
        structured_snapshot_columns = {
            "prd_structured_hash": "TEXT NULL",
            "prd_document_fingerprint": "TEXT NULL",
        }
        for column_name, column_type in structured_snapshot_columns.items():
            if column_name not in evidence_snapshot_columns:
                conn.execute(
                    f"ALTER TABLE evidence_snapshots ADD COLUMN {column_name} {column_type}"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                evidence_id TEXT NOT NULL,
                type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
                redaction_state TEXT NOT NULL,
                UNIQUE (snapshot_id, evidence_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                local_task_id TEXT NOT NULL,
                call_prepare_key TEXT NOT NULL,
                snapshot_id INTEGER NOT NULL,
                snapshot_hash TEXT NOT NULL,
                candidate_set_hash TEXT NOT NULL,
                task_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                output_schema_version TEXT NOT NULL,
                benchmark_sample_pack_version TEXT NOT NULL,
                qualification_status TEXT NOT NULL,
                qualification_hash TEXT NOT NULL,
                authorization_provider TEXT NOT NULL,
                authorization_authorized INTEGER NOT NULL CHECK (authorization_authorized IN (0, 1)),
                authorization_valid INTEGER NOT NULL CHECK (authorization_valid IN (0, 1)),
                authorization_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                call_identity_hash TEXT NOT NULL UNIQUE,
                UNIQUE (project_id, call_prepare_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_execution_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                model_call_id INTEGER NOT NULL UNIQUE,
                project_id INTEGER NOT NULL,
                snapshot_id INTEGER NOT NULL,
                local_task_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                call_identity_hash TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                provider_response_id TEXT NOT NULL,
                actual_model TEXT NOT NULL,
                provider_runtime_fingerprint TEXT NOT NULL,
                finish_reason TEXT NOT NULL CHECK (finish_reason = 'stop'),
                prompt_tokens INTEGER NOT NULL CHECK (prompt_tokens >= 0),
                completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),
                total_tokens INTEGER NOT NULL CHECK (
                    total_tokens >= 0 AND total_tokens = prompt_tokens + completion_tokens
                ),
                formal_response_json TEXT NOT NULL,
                formal_response_hash TEXT NOT NULL,
                validated_result_json TEXT NOT NULL,
                validated_result_hash TEXT NOT NULL,
                execution_result_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS report_generation_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                local_task_id TEXT NOT NULL,
                evidence_snapshot_id INTEGER NOT NULL,
                task_type TEXT NOT NULL,
                create_key TEXT NOT NULL,
                current_attempt_id TEXT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'succeeded', 'failed', 'unknown', 'voided')
                ),
                identity_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (project_id, create_key),
                UNIQUE (project_id, local_task_id, task_type)
            )
            """
        )
        _ensure_report_generation_active_checkpoint_index(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS report_generation_task_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                attempt_id TEXT NOT NULL UNIQUE,
                task_id INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'succeeded', 'failed', 'unknown', 'voided')
                ),
                created_at TEXT NOT NULL,
                started_at TEXT NULL,
                finished_at TEXT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (task_id, sequence_no)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_report_generation_task_attempts_task
            ON report_generation_task_attempts(task_id, sequence_no)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_send_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL CHECK (schema_version = 'mail_send_attempt_v1'),
                send_attempt_id TEXT NOT NULL UNIQUE,
                project_id INTEGER NOT NULL CHECK (project_id > 0),
                approval_snapshot_id INTEGER NOT NULL CHECK (approval_snapshot_id > 0),
                approval_snapshot_hash TEXT NOT NULL,
                report_version_id INTEGER NOT NULL CHECK (report_version_id > 0),
                report_content_hash TEXT NOT NULL,
                render_identity TEXT NOT NULL,
                render_hash TEXT NOT NULL,
                html_sha256 TEXT NOT NULL,
                message_id TEXT NOT NULL,
                recipients_json TEXT NOT NULL,
                recipients_hash TEXT NOT NULL,
                subject TEXT NOT NULL,
                from_identity TEXT NOT NULL,
                predecessor_send_attempt_id TEXT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('prepared', 'sending', 'sent', 'partial', 'failed', 'unknown', 'voided')
                ),
                identity_hash TEXT NOT NULL,
                terminal_code TEXT NULL,
                terminal_summary TEXT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (terminal_code IS NULL AND terminal_summary IS NULL)
                    OR (terminal_code IS NOT NULL AND terminal_summary IS NOT NULL)
                )
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_mail_send_attempts_project_history
            ON mail_send_attempts(project_id, id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_mail_send_attempts_predecessor
            ON mail_send_attempts(predecessor_send_attempt_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_send_recipient_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                send_attempt_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                recipient TEXT NOT NULL,
                recipient_hash TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'rejected', 'unknown')),
                error_code TEXT NOT NULL,
                summary TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE (send_attempt_id, ordinal),
                UNIQUE (send_attempt_id, recipient)
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_mail_send_predecessor_lineage_required
            BEFORE INSERT ON mail_send_attempts
            WHEN EXISTS (
                SELECT 1
                FROM mail_send_attempts AS prior
                WHERE prior.project_id = NEW.project_id
                  AND prior.send_attempt_id <> NEW.send_attempt_id
                  AND (
                      (
                          prior.approval_snapshot_id = NEW.approval_snapshot_id
                          AND prior.approval_snapshot_hash = NEW.approval_snapshot_hash
                      )
                      OR (
                          prior.report_version_id = NEW.report_version_id
                          AND prior.report_content_hash = NEW.report_content_hash
                      )
                  )
            )
            AND (
                NEW.predecessor_send_attempt_id IS NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM mail_send_attempts AS predecessor
                    WHERE predecessor.send_attempt_id = NEW.predecessor_send_attempt_id
                      AND predecessor.project_id = NEW.project_id
                      AND predecessor.state IN ('sent', 'partial', 'failed', 'unknown', 'voided')
                      AND (
                          (
                              predecessor.approval_snapshot_id = NEW.approval_snapshot_id
                              AND predecessor.approval_snapshot_hash = NEW.approval_snapshot_hash
                          )
                          OR (
                              predecessor.report_version_id = NEW.report_version_id
                              AND predecessor.report_content_hash = NEW.report_content_hash
                          )
                      )
                      AND predecessor.id = (
                          SELECT MAX(prior.id)
                          FROM mail_send_attempts AS prior
                          WHERE prior.project_id = NEW.project_id
                            AND prior.send_attempt_id <> NEW.send_attempt_id
                            AND (
                                (
                                    prior.approval_snapshot_id = NEW.approval_snapshot_id
                                    AND prior.approval_snapshot_hash = NEW.approval_snapshot_hash
                                )
                                OR (
                                    prior.report_version_id = NEW.report_version_id
                                    AND prior.report_content_hash = NEW.report_content_hash
                                )
                            )
                      )
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'later mail send attempt requires matching terminal predecessor');
            END
            """
        )
        from app.approved_module_narrative import ensure_approved_module_narrative_schema
        from app.mail_document_revision import revision_sql
        conn.execute('SAVEPOINT mail_document_revision_schema')
        ensure_approved_module_narrative_schema(conn)
        revision_gate = revision_sql()
        conn.execute('DROP TRIGGER IF EXISTS trg_mail_send_same_approval_identity')
        conn.execute('DROP TRIGGER IF EXISTS trg_mail_send_same_report_message_id')
        conn.execute(
            f"""
            CREATE TRIGGER trg_mail_send_same_approval_identity
            BEFORE INSERT ON mail_send_attempts
            WHEN EXISTS (
                SELECT 1
                FROM mail_send_attempts AS prior
                WHERE prior.project_id = NEW.project_id
                  AND prior.approval_snapshot_id = NEW.approval_snapshot_id
                  AND prior.approval_snapshot_hash = NEW.approval_snapshot_hash
                  AND (
                      prior.report_version_id <> NEW.report_version_id
                      OR prior.report_content_hash <> NEW.report_content_hash
                      OR prior.render_identity <> NEW.render_identity
                      OR prior.render_hash <> NEW.render_hash
                      OR prior.html_sha256 <> NEW.html_sha256
                      OR prior.recipients_json <> NEW.recipients_json
                      OR prior.recipients_hash <> NEW.recipients_hash
                  )
                  AND NOT {revision_gate}
            )
            BEGIN
                SELECT RAISE(ABORT, 'same approval snapshot cannot change frozen send identity');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER trg_mail_send_same_report_message_id
            BEFORE INSERT ON mail_send_attempts
            WHEN EXISTS (
                SELECT 1
                FROM mail_send_attempts AS prior
                WHERE prior.project_id = NEW.project_id
                  AND prior.report_version_id = NEW.report_version_id
                  AND prior.report_content_hash = NEW.report_content_hash
                  AND prior.message_id <> NEW.message_id
                  AND NOT {revision_gate}
            )
            BEGIN
                SELECT RAISE(ABORT, 'same report cannot change message id');
            END
            """
        )
        conn.execute('RELEASE SAVEPOINT mail_document_revision_schema')
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_mail_send_attempt_identity_immutable
            BEFORE UPDATE OF
                schema_version, send_attempt_id, project_id,
                approval_snapshot_id, approval_snapshot_hash,
                report_version_id, report_content_hash,
                render_identity, render_hash, html_sha256, message_id,
                recipients_json, recipients_hash, subject, from_identity,
                predecessor_send_attempt_id, identity_hash, created_at
            ON mail_send_attempts
            BEGIN
                SELECT RAISE(ABORT, 'mail send attempt identity is immutable');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_mail_send_attempt_state_transition
            BEFORE UPDATE OF state ON mail_send_attempts
            WHEN NOT (
                (OLD.state = 'prepared' AND NEW.state = 'sending')
                OR (
                    OLD.state = 'sending'
                    AND NEW.state IN ('sent', 'partial', 'failed', 'unknown')
                )
                OR (OLD.state = 'unknown' AND NEW.state = 'voided')
            )
            BEGIN
                SELECT RAISE(ABORT, 'illegal mail send attempt state transition');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_mail_send_attempt_no_delete
            BEFORE DELETE ON mail_send_attempts
            BEGIN
                SELECT RAISE(ABORT, 'mail send attempt history is append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_mail_send_recipient_result_no_update
            BEFORE UPDATE ON mail_send_recipient_results
            BEGIN
                SELECT RAISE(ABORT, 'mail recipient result ledger is append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_mail_send_recipient_result_no_delete
            BEFORE DELETE ON mail_send_recipient_results
            BEGIN
                SELECT RAISE(ABORT, 'mail recipient result ledger is append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipient_config_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL CHECK (version_no > 0),
                to_recipients_json TEXT NOT NULL,
                recipients_hash TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                predecessor_version_id INTEGER NULL,
                row_hash TEXT NOT NULL,
                UNIQUE (project_id, version_no)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_recipient_config_project_version
            ON recipient_config_versions(project_id, version_no DESC)
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_recipient_config_no_update
            BEFORE UPDATE ON recipient_config_versions
            BEGIN
                SELECT RAISE(ABORT, 'recipient config history is append-only');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_recipient_config_no_delete
            BEFORE DELETE ON recipient_config_versions
            BEGIN
                SELECT RAISE(ABORT, 'recipient config history is append-only');
            END
            """
        )
        recipient_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(recipient_config_versions)").fetchall()
        }
        required_recipient_columns = {
            "id",
            "schema_version",
            "project_id",
            "version_no",
            "to_recipients_json",
            "recipients_hash",
            "created_by",
            "created_at",
            "predecessor_version_id",
            "row_hash",
        }
        if recipient_columns != required_recipient_columns:
            raise sqlite3.IntegrityError("recipient_config_versions has an obsolete schema")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anxin_board_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                report_date TEXT NOT NULL,
                report_hash TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (project_id, report_hash)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_anxin_board_reports_project_latest
            ON anxin_board_reports(project_id, id DESC)
            """
        )
        interrupted_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE project_git_connections
            SET status = 'failed', attempt_id = NULL, last_checked_at = ?,
                error_code = 'GIT_CHECK_INTERRUPTED',
                error_summary = '上次 Git 检查因应用中断而未完成，请重新测试连接。'
            WHERE status = 'testing'
            """,
            (interrupted_at,),
        )
