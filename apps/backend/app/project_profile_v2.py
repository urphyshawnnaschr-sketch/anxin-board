"""Versioned plan/repository reconciliation; absence of evidence is unknown."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION_V2 = "project_profile_v2"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MappingPath(StrictModel):
    type: Literal["frontend", "backend", "data", "test", "other"]
    pattern: str = Field(min_length=1, max_length=500)
    required: bool = True
    note: str = Field(default="", max_length=500)


class PlannedModule(StrictModel):
    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    prd_refs: list[str] = Field(default_factory=list, max_length=100)
    requirements: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)


class ImplementationMapping(StrictModel):
    planned_module_id: str = Field(min_length=1, max_length=64)
    status: Literal["not_started", "partial", "implemented", "unknown"] = "unknown"
    exact_head: str = Field(default="", pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})?$")
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    paths: list[MappingPath] = Field(default_factory=list, max_length=100)
    rationale: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def backed_status(self):
        if self.status != "unknown" and (not self.exact_head or not self.rationale):
            raise ValueError("implementation assessment requires exact HEAD and rationale")
        if self.status in {"partial", "implemented"} and (
            not self.paths or not any(value.startswith("repo-code-") for value in self.evidence_ids)
        ):
            raise ValueError("implementation claim requires code evidence and paths")
        if self.status == "not_started" and not self.evidence_ids:
            raise ValueError("not_started requires explicit assessment evidence")
        return self


class UnplannedCodeFeature(StrictModel):
    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    exact_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    paths: list[MappingPath] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def code_backed(self):
        if not any(value.startswith("repo-code-") for value in self.evidence_ids):
            raise ValueError("unplanned code requires code evidence")
        return self


class V2Glossary(StrictModel):
    term: str = Field(default="", max_length=100)
    definition: str = Field(default="", max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=50)


class ProjectProfileV2Content(StrictModel):
    schema_version: Literal["project_profile_v2"]
    project_summary: str = Field(default="", max_length=2000)
    planned_modules: list[PlannedModule] = Field(default_factory=list, max_length=100)
    implementation_mappings: list[ImplementationMapping] = Field(default_factory=list, max_length=100)
    unplanned_code_features: list[UnplannedCodeFeature] = Field(default_factory=list, max_length=100)
    domain_glossary: list[V2Glossary] = Field(default_factory=list, max_length=200)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=100)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def unique_bindings(self):
        ids = [module.client_id for module in self.planned_modules]
        mapped = [mapping.planned_module_id for mapping in self.implementation_mappings]
        extra = [feature.client_id for feature in self.unplanned_code_features]
        if len(ids) != len(set(ids)) or len(mapped) != len(set(mapped)) or len(extra) != len(set(extra)):
            raise ValueError("duplicate profile identity")
        if set(mapped) - set(ids) or set(extra) & set(ids):
            raise ValueError("invalid implementation binding")
        return self


def profile_planned_modules(content) -> list[dict]:
    """Project planned scope for legacy consumers without claiming implementation."""
    raw = content.model_dump() if isinstance(content, BaseModel) else content
    if raw.get("schema_version") != SCHEMA_VERSION_V2:
        return raw.get("modules", [])
    mappings = {item["planned_module_id"]: item for item in raw.get("implementation_mappings", [])}
    return [dict(module, paths=mappings.get(module["client_id"], {}).get("paths", []))
            for module in raw.get("planned_modules", [])]
