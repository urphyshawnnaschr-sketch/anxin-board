"""Exact-HEAD Repository Atlas for brownfield baseline discovery.

The atlas is local-only structural metadata. It never replaces source evidence and it never
claims that a PRD requirement is implemented. Model-facing catalog records intentionally omit
raw source content; selected source is fetched later by evidence id/path and remains bound to
the exact analyzed HEAD.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import posixpath
import re
from pathlib import PurePosixPath


ATLAS_SCHEMA_VERSION = "repository-atlas/1"
CATALOG_SCHEMA_VERSION = "repository-atlas-catalog/1"
BUNDLE_SCHEMA_VERSION = "repository-atlas-evidence-bundle/1"
_HEAD_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class AtlasError(ValueError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _path_id(head: str, path: str) -> str:
    return "atlas-path-" + hashlib.sha256(f"{head}\n{path}".encode("utf-8")).hexdigest()[:24]


def _role(path: str) -> str:
    p = path.casefold()
    name = PurePosixPath(path).name.casefold()
    suffix = PurePosixPath(path).suffix.casefold()
    if "/test" in f"/{p}" or "/tests/" in f"/{p}/" or name.startswith("test_") or name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return "test"
    if name in {"readme.md", "readme.txt"} or p.startswith("docs/") or suffix in {".md", ".rst"}:
        return "docs"
    if name in {"package.json", "pyproject.toml", "requirements.txt", "vite.config.js", "vite.config.ts"} or suffix in {".toml", ".yaml", ".yml"}:
        return "config"
    if suffix in {".vue", ".tsx", ".jsx", ".html", ".css", ".scss"}:
        return "frontend"
    if suffix in {".py", ".js", ".ts", ".mjs", ".cjs", ".sql"}:
        return "source"
    return "other"


def _normalize_endpoint(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    value = re.sub(r"https?://[^/]+", "", value)
    value = value.split("?", 1)[0]
    value = re.sub(r"//+", "/", value)
    return value.rstrip("/") or "/"


def _resolve_python_import(source_path: str, value: str, all_paths: set[str]) -> str | None:
    leading = len(value) - len(value.lstrip("."))
    module = value[leading:]
    base_dir = posixpath.dirname(source_path)
    for _ in range(max(0, leading - 1)):
        base_dir = posixpath.dirname(base_dir)
    relative = module.replace(".", "/") if module else ""
    base = posixpath.normpath(posixpath.join(base_dir, relative)) if relative else base_dir
    candidates = [base + ".py", posixpath.join(base, "__init__.py")]
    return next((candidate for candidate in candidates if candidate in all_paths), None)


def _resolve_web_import(source_path: str, value: str, all_paths: set[str]) -> str | None:
    if value.startswith("@/"):
        marker = "/src/"
        if marker in source_path:
            root = source_path.split(marker, 1)[0] + "/src"
            base = posixpath.normpath(posixpath.join(root, value[2:]))
        elif source_path.startswith("src/"):
            base = posixpath.normpath(posixpath.join("src", value[2:]))
        else:
            return None
    elif value.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), value))
    else:
        return None
    candidates = [base, base + ".js", base + ".ts", base + ".tsx", base + ".jsx", base + ".vue"]
    candidates.extend([posixpath.join(base, "index.js"), posixpath.join(base, "index.ts")])
    matched = [candidate for candidate in candidates if candidate in all_paths]
    return matched[0] if len(matched) == 1 else None


def _resolve_import(source_path: str, value: str, all_paths: set[str]) -> str | None:
    """Resolve only deterministic local imports; unresolved package imports remain hints."""
    value = value.strip()
    if not value:
        return None
    suffix = PurePosixPath(source_path).suffix.casefold()
    if suffix == ".py":
        if value.startswith("."):
            return _resolve_python_import(source_path, value, all_paths)
        module = value.replace(".", "/")
        candidates = [module + ".py", module + "/__init__.py", "apps/backend/" + module + ".py", "apps/backend/" + module + "/__init__.py"]
        return next((candidate for candidate in candidates if candidate in all_paths), None)
    return _resolve_web_import(source_path, value, all_paths)


def _stack_summary(indexed_map: dict[str, object]) -> dict[str, object]:
    stack = indexed_map.get("stack_detection") or {}
    technology = indexed_map.get("technology_detection") or {}
    languages = stack.get("languages") if isinstance(stack, dict) else []
    manifests = stack.get("manifest_clues") if isinstance(stack, dict) else []
    classifications = technology.get("classifications") if isinstance(technology, dict) else []
    safe_manifests = []
    if isinstance(manifests, list):
        for item in manifests:
            if isinstance(item, dict) and type(item.get("path")) is str and type(item.get("language_hint")) is str:
                safe_manifests.append({"path": item["path"], "language": item["language_hint"]})
    technologies = []
    if isinstance(classifications, list):
        for item in classifications:
            if not isinstance(item, dict) or item.get("classification") not in {"SOURCE_CONFIRMED", "DECLARED_ONLY"}:
                continue
            scope, name, state = item.get("scope"), item.get("technology"), item.get("classification")
            if all(type(value) is str and value for value in (scope, name, state)):
                technologies.append({"scope": scope, "technology": name, "classification": state})
    return {
        "languages": sorted({value for value in (languages or []) if type(value) is str and value}),
        "manifests": sorted(safe_manifests, key=lambda value: (value["path"], value["language"])),
        "technologies": sorted(technologies, key=lambda value: (value["scope"], value["technology"], value["classification"])),
    }


def build_repository_atlas(indexed_map: dict[str, object]) -> dict[str, object]:
    head = indexed_map.get("exact_head")
    if type(head) is not str or _HEAD_RE.fullmatch(head) is None:
        raise AtlasError("ATLAS_INVALID_HEAD")
    raw_files = indexed_map.get("files")
    evidence = indexed_map.get("evidence")
    if not isinstance(raw_files, list) or not isinstance(evidence, list):
        raise AtlasError("ATLAS_INVALID_INDEX")

    by_path: dict[str, dict[str, object]] = {}
    seen_evidence: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise AtlasError("ATLAS_INVALID_EVIDENCE")
        path, evidence_id = item.get("path"), item.get("evidence_id")
        if type(path) is not str or not path or type(evidence_id) is not str or not evidence_id or evidence_id in seen_evidence or item.get("exact_head") != head:
            raise AtlasError("ATLAS_INVALID_EVIDENCE")
        content = item.get("content")
        if type(content) is not str or hashlib.sha256(content.encode("utf-8")).hexdigest() != item.get("content_hash"):
            raise AtlasError("ATLAS_EVIDENCE_HASH_MISMATCH")
        seen_evidence.add(evidence_id)
        record = by_path.setdefault(path, {
            "evidence_ids": [], "safe_bytes": 0, "languages": set(), "parser_statuses": set(),
            "metadata": defaultdict(set), "technology_symbols": set(), "technology_related_paths": set(),
        })
        record["evidence_ids"].append(evidence_id)
        record["safe_bytes"] += len(content.encode("utf-8"))
        if type(item.get("language_hint")) is str and item["language_hint"]:
            record["languages"].add(item["language_hint"])
        if type(item.get("parser_status")) is str and item["parser_status"]:
            record["parser_statuses"].add(item["parser_status"])
        metadata = item.get("structured_metadata") or {}
        if not isinstance(metadata, dict):
            raise AtlasError("ATLAS_INVALID_METADATA")
        for kind, values in metadata.items():
            if type(kind) is not str or not isinstance(values, list) or any(type(value) is not str for value in values):
                raise AtlasError("ATLAS_INVALID_METADATA")
            record["metadata"][kind].update(values)
        for tech in item.get("technology_evidence") or []:
            if isinstance(tech, dict):
                for key in ("symbol", "related_symbol"):
                    value = tech.get(key)
                    if type(value) is str and value:
                        record["technology_symbols"].add(value)
                related_path = tech.get("related_path")
                relation = tech.get("relation")
                if type(related_path) is str and related_path and type(relation) is str and relation:
                    record["technology_related_paths"].add((related_path, relation))

    file_state: dict[str, str] = {}
    for item in raw_files:
        if not isinstance(item, dict):
            raise AtlasError("ATLAS_INVALID_FILE")
        path = item.get("path")
        if type(path) is str and path:
            state = item.get("coverage_state")
            file_state[path] = state if type(state) is str else "unknown"

    all_paths = set(file_state) | set(by_path)
    records: list[dict[str, object]] = []
    for path in sorted(all_paths):
        source = by_path.get(path)
        metadata = source["metadata"] if source else {}
        record = {
            "path_id": _path_id(head, path),
            "path": path,
            "role": _role(path),
            "coverage_state": file_state.get(path, "safe_text" if source else "unknown"),
            "safe_text": source is not None,
            "safe_bytes": int(source["safe_bytes"]) if source else 0,
            "evidence_ids": sorted(source["evidence_ids"]) if source else [],
            "languages": sorted(source["languages"]) if source else [],
            "parser_statuses": sorted(source["parser_statuses"]) if source else [],
            "metadata": {kind: sorted(values) for kind, values in sorted(metadata.items())},
            "technology_symbols": sorted(source["technology_symbols"]) if source else [],
            "technology_related_paths": sorted(source["technology_related_paths"]) if source else [],
        }
        records.append(record)

    path_to_id = {record["path"]: record["path_id"] for record in records}
    endpoints_routes: dict[str, set[str]] = defaultdict(set)
    endpoints_api: dict[str, set[str]] = defaultdict(set)
    tables: dict[str, set[str]] = defaultdict(set)
    edges: set[tuple[str, str, str]] = set()
    for record in records:
        metadata = record["metadata"]
        for value in metadata.get("route", []):
            normalized = _normalize_endpoint(value)
            if normalized:
                endpoints_routes[normalized].add(record["path_id"])
        for value in metadata.get("api", []):
            normalized = _normalize_endpoint(value)
            if normalized:
                endpoints_api[normalized].add(record["path_id"])
        for value in metadata.get("table", []):
            tables[value.casefold()].add(record["path_id"])
        for value in metadata.get("import", []):
            target = _resolve_import(record["path"], value, all_paths)
            if target and target != record["path"]:
                a, b = sorted((record["path_id"], path_to_id[target]))
                edges.add((a, b, "import"))
        for related_path, _relation in record.get("technology_related_paths") or []:
            target = _resolve_web_import(record["path"], related_path, all_paths)
            if target and target != record["path"]:
                a, b = sorted((record["path_id"], path_to_id[target]))
                edges.add((a, b, "technology_relation"))

    for endpoint in sorted(set(endpoints_routes) & set(endpoints_api)):
        for left in sorted(endpoints_routes[endpoint]):
            for right in sorted(endpoints_api[endpoint]):
                if left != right:
                    a, b = sorted((left, right)); edges.add((a, b, "api_route"))
    for ids in tables.values():
        ordered = sorted(ids)
        if 1 < len(ordered) <= 12:
            for index, left in enumerate(ordered):
                for right in ordered[index + 1:]:
                    edges.add((left, right, "table"))

    edge_records = [{"left": left, "right": right, "kind": kind} for left, right, kind in sorted(edges, key=lambda value: (value[2], value[0], value[1]))]
    neighbor_map: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge in edge_records:
        neighbor_map[edge["left"]].append({"path_id": edge["right"], "kind": edge["kind"]})
        neighbor_map[edge["right"]].append({"path_id": edge["left"], "kind": edge["kind"]})
    for record in records:
        record["neighbors"] = sorted(neighbor_map.get(record["path_id"], []), key=lambda value: (value["kind"], value["path_id"]))

    directory_counts: dict[str, int] = defaultdict(int)
    for record in records:
        directory_counts[str(PurePosixPath(record["path"]).parent)] += 1
    atlas = {
        "schema_version": ATLAS_SCHEMA_VERSION,
        "exact_head": head,
        "tracked_files": indexed_map.get("tracked_files"),
        "safe_text_bytes": indexed_map.get("safe_text_bytes"),
        "coverage": deepcopy(indexed_map.get("coverage") or {}),
        "complete_inventory": indexed_map.get("complete_inventory") is True,
        "complete_safe_analysis": indexed_map.get("complete_safe_analysis") is True,
        "stack_summary": _stack_summary(indexed_map),
        "paths": records,
        "edges": edge_records,
        "directories": [{"path": path, "file_count": count} for path, count in sorted(directory_counts.items())],
    }
    atlas["atlas_hash"] = _digest({key: value for key, value in atlas.items() if key != "atlas_hash"})
    return atlas


def build_provider_catalog(atlas: dict[str, object]) -> dict[str, object]:
    """Return compact, code-free orientation context while keeping every safe path visible."""
    if atlas.get("schema_version") != ATLAS_SCHEMA_VERSION or type(atlas.get("atlas_hash")) is not str:
        raise AtlasError("ATLAS_INVALID")
    records = []
    for item in atlas.get("paths") or []:
        if not isinstance(item, dict):
            raise AtlasError("ATLAS_INVALID")
        if item.get("safe_text") is not True:
            continue
        metadata = item.get("metadata") or {}
        record = {"path_id": item["path_id"], "path": item["path"], "role": item["role"]}
        optional = {
            "languages": item.get("languages") or [],
            "symbols": metadata.get("symbol") or [],
            "routes": metadata.get("route") or [],
            "apis": metadata.get("api") or [],
            "tables": metadata.get("table") or [],
            "tests": metadata.get("test") or [],
            "imports": metadata.get("import") or [],
            "technology_symbols": item.get("technology_symbols") or [],
            "neighbors": item.get("neighbors") or [],
        }
        record.update({key: deepcopy(value) for key, value in optional.items() if value})
        records.append(record)
    catalog = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "atlas_hash": atlas["atlas_hash"],
        "exact_head": atlas["exact_head"],
        "tracked_files": atlas.get("tracked_files"),
        "safe_path_count": len(records),
        "safe_text_bytes": atlas.get("safe_text_bytes"),
        "coverage": deepcopy(atlas.get("coverage") or {}),
        "stack_summary": deepcopy(atlas.get("stack_summary") or {}),
        "paths": records,
    }
    catalog["catalog_hash"] = _digest({key: value for key, value in catalog.items() if key != "catalog_hash"})
    return catalog


def expand_path_ids(atlas: dict[str, object], seed_path_ids: list[str], *, max_hops: int = 1, max_paths: int = 32) -> dict[str, object]:
    if type(max_hops) is not int or not 0 <= max_hops <= 3 or type(max_paths) is not int or max_paths <= 0:
        raise AtlasError("ATLAS_EXPANSION_INVALID")
    records = {item["path_id"]: item for item in atlas.get("paths") or [] if isinstance(item, dict) and type(item.get("path_id")) is str}
    if not isinstance(seed_path_ids, list) or not seed_path_ids or any(type(value) is not str or value not in records or records[value].get("safe_text") is not True for value in seed_path_ids):
        raise AtlasError("ATLAS_SEED_INVALID")
    seeds = list(dict.fromkeys(seed_path_ids))
    if len(seeds) > max_paths:
        raise AtlasError("ATLAS_SEED_OVER_LIMIT")
    selected = list(seeds); selected_set = set(seeds); queue = deque((value, 0) for value in seeds)
    omitted: set[str] = set()
    priority = {"api_route": 0, "technology_relation": 1, "import": 2, "table": 3}
    while queue:
        current, depth = queue.popleft()
        if depth >= max_hops:
            continue
        neighbors = sorted(records[current].get("neighbors") or [], key=lambda value: (priority.get(value.get("kind"), 99), value.get("path_id", "")))
        for neighbor in neighbors:
            path_id = neighbor.get("path_id")
            if path_id in selected_set or path_id not in records or records[path_id].get("safe_text") is not True:
                continue
            if len(selected) >= max_paths:
                omitted.add(path_id); continue
            selected.append(path_id); selected_set.add(path_id); queue.append((path_id, depth + 1))
    return {"seed_path_ids": seeds, "selected_path_ids": selected, "omitted_neighbor_path_ids": sorted(omitted), "max_hops": max_hops, "max_paths": max_paths}


def build_evidence_bundle(indexed_map: dict[str, object], atlas: dict[str, object], seed_path_ids: list[str], *, max_hops: int = 1, max_paths: int = 32) -> dict[str, object]:
    if indexed_map.get("exact_head") != atlas.get("exact_head"):
        raise AtlasError("ATLAS_SOURCE_HEAD_DRIFT")
    expansion = expand_path_ids(atlas, seed_path_ids, max_hops=max_hops, max_paths=max_paths)
    selected_ids = set(expansion["selected_path_ids"])
    record_by_id = {item["path_id"]: item for item in atlas.get("paths") or []}
    if any(record_by_id[path_id].get("safe_text") is not True for path_id in selected_ids):
        raise AtlasError("ATLAS_SELECTED_PATH_NOT_SAFE_TEXT")
    selected_paths = {record_by_id[path_id]["path"] for path_id in selected_ids}
    evidence = [deepcopy(item) for item in indexed_map.get("evidence") or [] if isinstance(item, dict) and item.get("path") in selected_paths]
    expected_ids = {evidence_id for path_id in selected_ids for evidence_id in record_by_id[path_id].get("evidence_ids") or []}
    actual_ids = {item.get("evidence_id") for item in evidence}
    if actual_ids != expected_ids:
        raise AtlasError("ATLAS_SELECTED_EVIDENCE_INCOMPLETE")
    bundle = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "atlas_hash": atlas["atlas_hash"], "exact_head": atlas["exact_head"],
        **expansion,
        "selected_paths": [record_by_id[path_id]["path"] for path_id in expansion["selected_path_ids"]],
        "evidence": evidence,
        "evidence_count": len(evidence),
        "safe_text_bytes": sum(len(str(item.get("content") or "").encode("utf-8")) for item in evidence),
        "complete_for_selected_paths": True,
    }
    bundle["bundle_hash"] = _digest({key: value for key, value in bundle.items() if key != "bundle_hash"})
    return bundle


def unexplained_safe_path_ids(atlas: dict[str, object], explained_path_ids: set[str] | list[str]) -> list[str]:
    explained = set(explained_path_ids)
    return [item["path_id"] for item in atlas.get("paths") or [] if isinstance(item, dict) and item.get("safe_text") is True and item.get("path_id") not in explained]
