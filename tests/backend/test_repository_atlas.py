import hashlib
import json

import pytest

from app.repository_atlas import (
    AtlasError,
    build_evidence_bundle,
    build_provider_catalog,
    build_repository_atlas,
    expand_path_ids,
    unexplained_safe_path_ids,
)
from app.repository_parsers import PythonHeuristics, WebHeuristics


HEAD = "a" * 40


def ev(identity, path, content, metadata, language="python"):
    return {
        "evidence_id": "repo-code-" + identity,
        "path": path,
        "content": content,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "object_sha": identity.rjust(40, "0")[-40:],
        "exact_head": HEAD,
        "evidence_schema_version": "repository-evidence/1",
        "adapter_id": "test/1",
        "parser_status": "heuristic",
        "language_hint": language,
        "detector_version": "test",
        "structured_metadata": metadata,
        "terms": [],
        "confidence": "heuristic_not_semantic_proof",
        "semantic_coverage": "unknown",
    }


def indexed():
    front = ev(
        "1", "frontend/cameras.ts", "fetch('/api/cameras')\nimport './cameraStore'\n",
        {"api": ["/api/cameras"], "import": ["./cameraStore"], "symbol": [], "route": [], "table": []},
        "javascript-web",
    )
    store = ev(
        "2", "frontend/cameraStore.ts", "export function loadCameras() {}\n",
        {"symbol": ["loadCameras"], "import": [], "api": [], "route": [], "table": []},
        "javascript-web",
    )
    back = ev(
        "3", "backend/cameras.py", "@router.get('/api/cameras')\ndef list_cameras(): pass\nCREATE TABLE cameras(id int)\n",
        {"route": ["/api/cameras"], "symbol": ["list_cameras"], "table": ["cameras"], "import": []},
    )
    repo = ev(
        "4", "backend/camera_repo.py", "CREATE TABLE cameras(id int)\ndef save_camera(): pass\n",
        {"table": ["cameras"], "symbol": ["save_camera"], "route": [], "import": []},
    )
    docs = ev(
        "5", "docs/camera.md", "camera documentation\n",
        {"symbol": [], "route": [], "table": [], "import": []},
        "unknown",
    )
    evidence = [front, store, back, repo, docs]
    files = [
        {"path": item["path"], "coverage_state": "safe_text", "safe_bytes": len(item["content"].encode())}
        for item in evidence
    ]
    files.append({"path": "assets/logo.png", "coverage_state": "binary_hashed"})
    return {
        "schema_version": "profile-repo-map/1",
        "exact_head": HEAD,
        "files": files,
        "evidence": evidence,
        "coverage": {"safe_text": 5, "binary_hashed": 1},
        "tracked_files": 6,
        "safe_text_bytes": sum(len(item["content"].encode()) for item in evidence),
        "complete_inventory": True,
        "complete_safe_analysis": True,
        "stack_detection": {
            "languages": ["python", "typescript"],
            "manifest_clues": [{"path": "package.json", "language_hint": "javascript", "kind": "manifest_filename"}],
        },
        "technology_detection": {
            "classifications": [
                {"scope": ".", "technology": "Axios", "classification": "SOURCE_CONFIRMED"},
                {"scope": ".", "technology": "Vue2", "classification": "UNKNOWN"},
            ]
        },
    }


def test_atlas_catalog_is_code_free_safe_only_compact_and_builds_edges():
    atlas = build_repository_atlas(indexed())
    assert atlas["tracked_files"] == 6
    assert atlas["complete_inventory"] is True
    assert len(atlas["paths"]) == 6
    kinds = {edge["kind"] for edge in atlas["edges"]}
    assert {"api_route", "import", "table"}.issubset(kinds)

    catalog = build_provider_catalog(atlas)
    raw = json.dumps(catalog, ensure_ascii=False)
    assert "fetch('/api/cameras')" not in raw
    assert "repo-code-" not in raw
    assert "content_hash" not in raw
    assert "assets/logo.png" not in raw
    assert catalog["safe_path_count"] == 5
    assert catalog["stack_summary"]["languages"] == ["python", "typescript"]
    assert catalog["stack_summary"]["technologies"] == [{"scope": ".", "technology": "Axios", "classification": "SOURCE_CONFIRMED"}]
    assert any(item["path"] == "backend/cameras.py" and item["routes"] == ["/api/cameras"] for item in catalog["paths"])
    docs = next(item for item in catalog["paths"] if item["path"] == "docs/camera.md")
    assert set(docs) == {"path_id", "path", "role", "languages"}


def test_selected_bundle_keeps_every_fragment_for_selected_and_expanded_paths():
    repo = indexed(); atlas = build_repository_atlas(repo)
    front = next(item for item in atlas["paths"] if item["path"] == "frontend/cameras.ts")
    expansion = expand_path_ids(atlas, [front["path_id"]], max_hops=1, max_paths=8)
    selected_paths = {next(item["path"] for item in atlas["paths"] if item["path_id"] == path_id) for path_id in expansion["selected_path_ids"]}
    assert "frontend/cameraStore.ts" in selected_paths
    assert "backend/cameras.py" in selected_paths

    bundle = build_evidence_bundle(repo, atlas, [front["path_id"]], max_hops=1, max_paths=8)
    assert bundle["complete_for_selected_paths"] is True
    assert {item["path"] for item in bundle["evidence"]} == set(bundle["selected_paths"])
    expected = {
        item["evidence_id"] for item in repo["evidence"] if item["path"] in set(bundle["selected_paths"])
    }
    assert {item["evidence_id"] for item in bundle["evidence"]} == expected


def test_expansion_reports_omitted_neighbors_instead_of_silently_dropping():
    repo = indexed(); atlas = build_repository_atlas(repo)
    backend = next(item for item in atlas["paths"] if item["path"] == "backend/cameras.py")
    expansion = expand_path_ids(atlas, [backend["path_id"]], max_hops=1, max_paths=2)
    assert len(expansion["selected_path_ids"]) == 2
    assert expansion["omitted_neighbor_path_ids"]


def test_unexplained_safe_paths_excludes_binary_and_explained():
    atlas = build_repository_atlas(indexed())
    explained = {next(item["path_id"] for item in atlas["paths"] if item["path"] == "frontend/cameras.ts")}
    remaining = set(unexplained_safe_path_ids(atlas, explained))
    assert explained.isdisjoint(remaining)
    binary = next(item["path_id"] for item in atlas["paths"] if item["path"] == "assets/logo.png")
    assert binary not in remaining


def test_head_drift_binary_seed_and_seed_over_limit_fail_closed():
    repo = indexed(); atlas = build_repository_atlas(repo)
    changed = dict(repo, exact_head="b" * 40)
    seed = next(item["path_id"] for item in atlas["paths"] if item["safe_text"])
    with pytest.raises(AtlasError, match="ATLAS_SOURCE_HEAD_DRIFT"):
        build_evidence_bundle(changed, atlas, [seed])
    binary = next(item["path_id"] for item in atlas["paths"] if not item["safe_text"])
    with pytest.raises(AtlasError, match="ATLAS_SEED_INVALID"):
        expand_path_ids(atlas, [binary])
    safe_ids = [item["path_id"] for item in atlas["paths"] if item["safe_text"]]
    with pytest.raises(AtlasError, match="ATLAS_SEED_OVER_LIMIT"):
        expand_path_ids(atlas, safe_ids[:2], max_paths=1)


def test_python_relative_imports_build_local_edges():
    service = ev("6", "backend/pkg/service.py", "def run(): pass\n", {"symbol": ["run"], "import": [], "route": [], "table": []})
    caller = ev("7", "backend/pkg/caller.py", "from .service import run\ndef call(): run()\n", {"symbol": ["call"], "import": [".service"], "route": [], "table": []})
    repo = indexed()
    repo["evidence"].extend([service, caller])
    repo["files"].extend([{"path": service["path"], "coverage_state": "safe_text"}, {"path": caller["path"], "coverage_state": "safe_text"}])
    repo["tracked_files"] += 2
    repo["safe_text_bytes"] += len(service["content"].encode()) + len(caller["content"].encode())
    atlas = build_repository_atlas(repo)
    ids = {item["path"]: item["path_id"] for item in atlas["paths"]}
    expected = set(sorted((ids["backend/pkg/service.py"], ids["backend/pkg/caller.py"])))
    assert any(edge["kind"] == "import" and {edge["left"], edge["right"]} == expected for edge in atlas["edges"])


def test_parsers_expose_imports_as_structural_metadata():
    py = PythonHeuristics().parse("backend/a.py", "from app.service import run\nimport sqlite3\ndef f(): pass\n")
    assert py.structured["import"] == ["app.service", "sqlite3"]
    web = WebHeuristics().parse("frontend/a.ts", "import api from './api'\nconst x=require('../store')\n")
    assert web.structured["import"] == ["../store", "./api"]
