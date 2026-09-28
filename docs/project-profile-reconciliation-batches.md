# Project Profile V2 repository reconciliation: offline checkpoint

The confirmed PRD plan remains fixed. This lane inventories the entire local exact-HEAD
tracked tree before deciding what goes into a model request. It does not use the old
2 MiB all-code prompt. The legacy `generate-v2` implementation option now stops before
schema writes, authorization, credential lookup or provider execution.

`profile_repository_map` checks the controlled workspace, exact Git objects and hashes;
records directories, symbols, routes, APIs, tables and tests; applies existing sensitive
path and redaction policy; and produces hash/line-bound safe text evidence. Inventory
coverage and safe-analysis coverage are separate. Sensitive paths are not read; binary,
unsupported and quarantined files never silently become evidence of non-implementation.
A 10,000-entry tree bound and 64 MiB per-object local resource bound remain fail-closed.
These are local resource limits, not model context limits.

`profile_reconciliation_batches` retrieves relevant evidence using local plan terms,
path/symbol text and inverse document frequency. This is partial retrieval, not a proof
that absent code does not exist. It dynamically splits evidence and large plan fragments
against the complete serialized batch contract. It reuses the report budget formula and
`utf8_byte_upper_bound_v1`: context minus reserved output minus one safety margin. Byte
counts are conservative token upper bounds, not tokenizer measurements. The offline
profile uses the committed Product model window, the smaller of report output reserve
and profile max output, and the existing report safety margin. No live capability or
credential lookup is performed. Any future provider adapter must re-admit its actual
wire request against the approved budget before dispatch; this checkpoint has no real
provider executor.

Every batch binds HEAD, plan ID/hash, fixed module IDs, evidence hashes, request hash,
budget hash and complete batch-set manifest. SQLite claims before dispatch and commits
successful batches independently. Recreating the same deterministic plan recovers rows.
Successful batches are reused; explicit known failures require explicit resume; claimed
or UNKNOWN batches never automatically run again. HEAD changes stop dispatch/aggregation.
Cross-batch references are rejected. Results cannot invent module IDs or `not_started`.

Aggregation is deterministic, requires every batch and module, and never calls a summary
model. Because partial retrieval/fragment agreement cannot establish complete requirement
coverage, current aggregate output is at most `partial`, otherwise `unknown`; a future
independent completeness verifier is required to permit `implemented`. Conflicts remain
unknown. Fixed PRD modules are unchanged. The candidate is persisted only to the isolated
reconciliation task store; no Product confirmation, profile promotion, HTML/report call,
SMTP or external authorization is performed. Unplanned-code discovery is not implemented
by this module-reconciliation preflight and is explicitly reported as unassessed.

Run locally with the existing backend environment:

    python scripts/profile_reconciliation_preflight.py --project-id 2 --output <isolated-output-directory>

The script reads Product SQLite in read-only mode, invokes no provider and writes safe
local map, fake-batch store and metadata summary only to the named output directory.
Fake results are intentionally unknown; they validate plumbing, not development status.
The final summary contains per-batch input upper bounds and complete coverage accounting.
Do not commit the actual project's map, plan text, fake database or private metadata.

This checkpoint intentionally ends before any new paid call. Existing paid generation,
Human confirmation and historical UNKNOWN remain untouched. The eventual business flow
remains repository evidence -> 38 planned modules -> Human review -> AnxinBoard HTML ->
mail, subject to a later explicit authorization and the existing confirmation/send gates.
