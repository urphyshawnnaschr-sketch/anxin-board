# Project Profile V2: plan and implementation reconciliation

The PRD defines planned scope even when no code exists. Code supports a separate, exact-HEAD implementation assessment. This child change is serialized after `product/profile-bootstrap-full-code-context-v1`; the owner E2E evidence branch is not edited.

## Product contract

- `project_profile_v2.planned_modules`: stable module ID, name, PRD references, requirements, description and exclusions. No code path is required to confirm planned scope.
- `implementation_mappings`: explicit planned module ID, `not_started`, `partial`, `implemented` or `unknown`, exact HEAD, evidence, paths and rationale. Missing mapping means unknown. AI generation cannot conclude not_started merely because no matching code was found; that state requires a later explicit Human assessment.
- `unplanned_code_features`: code-backed features outside the PRD plan, displayed separately and excluded from planned report scope.
- Default V2 generation sends redacted confirmed PRD only and needs no Git connection. The old implementation opt-in is blocked before authorization or credentials. Code reconciliation now requires local generic inventory, optional parser adapters/fallback, retrieval and budgeted batches; the current checkpoint has no real provider batch executor. No automatic second call occurs.
- Code read/redaction/admission errors remain deterministic pre-send failures. The redactor is not relaxed. Unknown attempts retain their durable identity and the Product page provides no reset-and-paid-retry shortcut.
- Generated results remain candidates. Plan confirmation does not certify code implementation or authorize report publication.

## Compatibility and trust

V1 content serialization/hashes are unchanged. Version-aware parsing and planned-scope accessors let the existing profile, context and report paths consume V2 without adding extra code to the formal plan. Generation validates all PRD references, module bindings, exact HEAD and code-path/evidence correspondence. Provider output is untrusted input.

Local request preparation uses a trusted Python exception type to carry failed_pre_send; error detail strings cannot claim a phase. Public error messages come from a bounded Product-owned catalog and do not retain arbitrary exception text, customer paths, credential values or provider bodies. Historical attempts are never reclassified in-place.

## Validation / handoff

All tests use synthetic PRD/SQLite/Git fixtures and fake providers. No real paid E2E, credential access, SMTP, installed lifecycle mutation, Ready, Merge or Release is authorized by this change. A future real paid generation needs new explicit Human authorization and applicable independent Review / physical gates. Independent Review and exact-current CI must resolve before this candidate can be considered complete.
