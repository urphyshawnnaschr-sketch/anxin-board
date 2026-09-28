# Generic local repository preprocessing

The product flow remains each customer's PRD -> that project's planned modules ->
repository evidence -> future authorized batch analysis -> Human confirmation ->
AnxinBoard HTML -> authorized mail with traceable delivery records. This checkpoint
only implements the local preprocessing foundation. It does not dispatch a provider,
reuse authorization, confirm a candidate, generate a real report, or send mail.

## Layers

1. `profile_repository_map`: technology-neutral controlled Git inventory and exact
   blob verification, sensitive-path exclusion, UTF-8 decoding, redaction, safe text
   chunking and explicit coverage. It has no symbol/route/API/parser rules.
2. `repository_stack_detection`: local extension and manifest-filename hints. It
   neither executes build files nor installs/resolves dependencies. Java/C#/Go/Rust
   and other recognized hints do not imply a dedicated parser exists.
3. `repository_parsers`: migrated Python regex heuristics and JS/TS/Web regex
   heuristics (including the existing SQL CREATE TABLE rule). These are deliberately
   heuristic adapters, not full AST/semantic parsers. Generic text fallback covers
   all remaining readable safe text, including manifests and unknown extensions.
4. `repository_index`: chooses a registered adapter and safely annotates fragments.
   An adapter failure falls back to generic text without exposing exception content
   or losing evidence. Adapters receive only already-redacted fragments.
5. `repository_evidence`: the uniform contract consumed by generic retrieval and
   `profile_reconciliation_batches`. The planner never branches on language.

## Extension and evidence contract

A `ParserAdapter` declares a versioned `adapter_id`, `language`, `extensions`,
`manifests`, and `parse(path, safe_text) -> ParseResult`. Register an instance in the
composition layer (or pass an explicit adapter tuple). A future stack adds an adapter
and registration, not changes to the Git core, planner, provider scheduler or report
flow. Registration is application code: customer repositories cannot load plugins.
No Java, C#, Go, Rust, Kotlin, Dart or C++ deep parser is added here.

Every annotated fragment retains `evidence_id`, `exact_head`, `path`, `object_sha`,
`content_hash`, `content`, inclusive one-based line range and half-open Unicode
code-point `char_start/char_end` in the safe decoded text. It also records evidence
contract version, detector version, adapter identity, parser status, optional
`structured_metadata`, generic text terms and explicit confidence/semantic coverage.
Adapters cannot replace source identity or inject metadata outside the safe fragment.
Further budget splits preserve object identity and character offsets and filter
structured metadata to the resulting fragment. Parser identities and metadata are
included in batch hashes, so changed analysis cannot reuse earlier batch success.

Fallback retains tree/files/directories, object/content hashes, manifest clues,
readable safe text, generic terms and all normal retrieval/budget/resume capabilities.
It loses stack-specific symbol/route/API/table/test recognition. Both fallback and
heuristic parsers have semantic coverage `unknown`: missing structure is not evidence
that a feature is missing, unstarted or implemented. Retrieval remains partial;
aggregation stays conservative and never turns partial retrieval into implemented.

## Coverage, budget and module count

Inventory coverage, safe-text coverage, parser coverage and semantic understanding
are different fields. Redaction-blocked, binary, unsupported and sensitive entries
remain explicitly accounted for. The resource bounds of the previous exact-object
reader remain fail-closed. No model window silently limits local inventory.

Module IDs and counts are input data; no production logic assumes 38. End-to-end
Python/TS/Vue/unknown-stack fixtures use 12, 25 and 70 modules. The existing Product
V2 document's maximum-100 schema constraint is a separate product contract, not a
repository scanner or planner rule.

All preprocessing is local: provider_calls=0, network/model calls=0. Tests replace
network, credential and provider boundaries with failing tripwires; repository
fixtures use in-memory exact Git objects. Token estimates are local conservative
UTF-8 byte upper bounds, not paid model usage. The report budget formula and safety
margin are retained. UNKNOWN and interrupted claims never automatically retry.

## Remaining before real provider analysis

Still required: reviewed actual-provider wire request budgeting, a durable scoped
multi-batch authorization/billing contract, production job orchestration and candidate
promotion through existing Human gates, and a genuine requirement-completeness rule
before `implemented` is permitted. Real repository redaction blockers remain explicit.
This checkpoint proves local fixtures and regressions, not real AI code understanding,
real report generation, email delivery or final release readiness.
