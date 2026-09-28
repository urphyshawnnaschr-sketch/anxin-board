"""Public candidate delivery boundaries; metadata is never executable release evidence."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "installer" / "windows-delivery-gate-v1.json"

EXPECTED_MATRIX = {
    "clean_windows_10",
    "clean_windows_11",
    "install",
    "first_run",
    "stop_restart",
    "upgrade",
    "credential_persistence_boundary",
    "uninstall",
    "backup_restore",
    "failure_diagnostics",
    "interruption_restart_recovery",
}

NON_CLOSED_STATES = {
    "not_started",
    "blocked",
    "implementation_ready_evidence_pending",
}


def _load_manifest() -> dict:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_public_candidate_still_cannot_claim_a_final_package() -> None:
    payload = _load_manifest()
    assert payload["schema_version"] == "anxin_windows_delivery_gate_v1"
    assert payload["snapshot_kind"] == "public_source_candidate_contract"
    assert payload["final_package_allowed"] is False
    runtime = payload["candidate_runtime"]
    assert runtime["formal_non_developer_delivery"] is False
    assert runtime["classification"] == "standalone_onedir_runtime_candidate"
    assert runtime["normal_user_machine_prerequisites"] == ["Git for Windows 2.45+ with --no-lazy-fetch capability"]
    assert "Git 2.45+ with --no-lazy-fetch capability" in runtime["build_machine_prerequisites"]
    assert "Vite" not in runtime["backend_and_ui"]
    assert "single runtime-manifest-bound" in runtime["diagnostics_binding"]
    assert "returns only a relative executable path" in runtime["installed_runtime_resolver"]
    assert not {"lane", "reconciled_through_lane_head", "implementation_commits"} & payload.keys()


def test_dependencies_require_source_bound_launcher_review_and_git_contract() -> None:
    dependencies = _load_manifest()["dependencies"]
    launcher = dependencies["secure_launcher"]
    assert launcher["status"] == "implementation_ready_evidence_pending"
    assert launcher["required_evidence"] == "independent exact-source review and executable isolated start/stop/restart evidence"
    git_runtime = dependencies["git_runtime"]
    assert git_runtime["minimum_supported_version"] == "2.45"
    assert git_runtime["required_capability"] == "git --no-lazy-fetch --version"
    assert "before Setup" in git_runtime["normal_user_contract"]
    assert "before UI mount/server start" in git_runtime["normal_user_contract"]
    assert "--no-lazy-fetch" in git_runtime["security_intent"]
    assert "never made green by dropping that control" in git_runtime["security_intent"]
    restore = dependencies["restore_capability"]
    assert restore["status"] == "implementation_ready_evidence_pending"
    assert "SQLite-only product wrapper" in restore["implementation"]
    assert "approved non-reparse Storage root" in restore["implementation"]


def test_candidate_program_data_backup_and_secret_boundaries_remain_separate() -> None:
    payload = _load_manifest()
    runtime = payload["candidate_runtime"]
    boundaries = payload["reusable_security_boundaries"]

    assert runtime["program_root"] == "%LOCALAPPDATA%\\Programs\\AnxinBoard\\"
    assert runtime["data_root"] == "%LOCALAPPDATA%\\AnxinBoard\\"
    assert "Windows Credential Manager" in boundaries["credential_store"]
    assert boundaries["ordinary_backup_scope"] == "SQLite product database snapshot only; Credential Manager is not read or serialized"
    assert boundaries["backup_export_authorization"].startswith("guarded by existing same-origin local session")
    assert boundaries["restore_route"] == "offline_controller_only_no_browser_restore_route"
    assert "ancestor chains" in boundaries["program_path_reparse_policy"]
    assert "DB/runtime-state/runtime-manifest" in boundaries["diagnostics_path_reparse_policy"]
    assert boundaries["secret_in_installer_payload"] == "forbidden"
    assert boundaries["secret_in_plain_config"] == "forbidden"
    assert boundaries["secret_in_logs_or_diagnostics"] == "forbidden"
    assert boundaries["real_credential_access_without_gate"] == "forbidden"
    assert boundaries["real_user_restore_without_gate"] == "forbidden"
    assert boundaries["real_user_install_mutation_without_gate"] == "forbidden"
    assert "never terminate by port alone" in boundaries["stop_entry"]


def test_delivery_matrix_is_complete_and_static_implementation_never_counts_as_pass() -> None:
    rows = _load_manifest()["validation_matrix"]
    by_id = {row["id"]: row for row in rows}

    assert set(by_id) == EXPECTED_MATRIX
    assert len(rows) == len(EXPECTED_MATRIX)
    assert by_id["clean_windows_10"]["status"] == "not_started"
    assert by_id["clean_windows_11"]["status"] == "not_started"
    assert by_id["stop_restart"]["status"] == "implementation_ready_evidence_pending"
    assert by_id["backup_restore"]["status"] == "implementation_ready_evidence_pending"
    assert "apps/backend/app/product_backup.py" in by_id["backup_restore"]["implementation_refs"]
    assert "apps/backend/app/product_backup_api.py" in by_id["backup_restore"]["implementation_refs"]
    assert "installer/windows/test-clean-client-evidence.ps1" in by_id["clean_windows_10"]["implementation_refs"]
    assert "apps/backend/app/git_runtime_capability.py" in by_id["first_run"]["implementation_refs"]

    for row in rows:
        assert row["status"] in NON_CLOSED_STATES | {"evidence_closed"}
        if row["status"] == "evidence_closed":
            assert row["evidence"], f"{row['id']} cannot close without executable evidence"
        else:
            assert row["evidence"] == [], f"{row['id']} must not use static refs as delivery evidence"


def test_public_carriers_do_not_inherit_private_or_unrun_evidence() -> None:
    carriers = _load_manifest()["validation_carriers"]
    assert set(carriers) == {"github_hosted_windows", "local_windows"}
    assert carriers["github_hosted_windows"]["runner_labels"] == ["windows-2022", "windows-2025"]
    for carrier in carriers.values():
        assert carrier["status"] == "not_started"
        assert carrier["evidence"] == []
        assert not {"runner_name", "latest_attempt_run", "attempt_head", "jobs"} & carrier.keys()


def test_capabilities_distinguish_implementation_from_executable_evidence() -> None:
    capabilities = _load_manifest()["capability_snapshot"]
    for name in ("delivery_candidate_bundle", "installed_runtime_resolver", "backup_export", "restore_import", "launcher_cross_session_hardening"):
        assert capabilities[name].endswith("evidence_pending")
    assert capabilities["clean_client_harness"].endswith("not_executed")
    assert capabilities["git_runtime_dependency_contract"] == "explicit_prerequisite_plus_prebusiness_capability_probe_implemented_evidence_pending"


def test_final_package_gate_requires_all_dependencies_and_matrix_evidence() -> None:
    payload = _load_manifest()
    dependency_states = {item["status"] for item in payload["dependencies"].values()}
    matrix_states = {row["status"] for row in payload["validation_matrix"]}

    if payload["final_package_allowed"]:
        assert dependency_states == {"passed"}
        assert matrix_states == {"evidence_closed"}
    else:
        assert dependency_states - {"passed"} or matrix_states - {"evidence_closed"}


def test_evidence_policy_forbids_builder_static_or_physical_shortcuts() -> None:
    policy = _load_manifest()["evidence_policy"]

    assert policy["static_read_counts_as_delivery_proof"] is False
    assert policy["implementation_ref_counts_as_delivery_proof"] is False
    assert policy["disposable_vm_or_ci_preferred"] is True
    assert policy["real_user_machine_requires_explicit_gate"] is True
    assert policy["real_credentials_require_explicit_gate"] is True
    assert policy["builder_self_pass_forbidden"] is True


def test_manifest_contains_no_embedded_secret_value_fields() -> None:
    payload = _load_manifest()
    forbidden_exact_keys = {
        "password",
        "secret",
        "secret_value",
        "token",
        "api_key",
        "credential_value",
        "bootstrap_secret",
        "session_token",
    }

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                assert key.casefold() not in forbidden_exact_keys
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
