# Approved module narrative

`approved_module_narrative_v1` is a separately confirmed explanation attached to
one approved formal V3 report. It does not change the original AI response,
profile, report, approval snapshot, progress ledger or a previously sent email.
Its attribution is `已确认的模块说明`; display the actor as
`模块说明已由{confirmed_by}确认`. The original report approval does not approve
this later annotation. The annotation has its own confirmation time and hash.

Only one annotation can be appended per project/report. Update and deletion are
blocked by SQLite triggers. Repeating an identical request with the same domain
idempotency key returns the original annotation; changed content or a second
confirmation is rejected. There is no provider or SMTP operation in this API.

## HTTP contract

Both methods use:

`/api/projects/{project_id}/anxin-board/reports/{report_version_id}/module-narrative`

GET requires the active local read session and query parameter `report_hash`
(the exact `anxin_board_report_hash`). It returns the confirmed DTO or JSON `null`
when none exists. This read does not install schema and does not require a
baseline for an unannotated report. Responses use `Cache-Control: no-store`.

POST requires the normal local write guard: exact loopback Host/Origin, active
`X-Anxin-Session`, and `X-Request-Id`. Domain idempotency is supplied in the body;
an additional transport idempotency header is not required. The body contains
exactly these fields:

| Field | Meaning |
| --- | --- |
| `anxin_board_report_hash`, `report_content_hash` | Frozen target V3 and original report content hashes |
| `approval_snapshot_id`, `approval_snapshot_hash` | Exact target approval identity |
| `profile_id`, `profile_content_hash` | Exact report-bound target profile identity and full content hash |
| `source` | Object containing `project_id`, `profile_id`, `profile_content_hash`, `task_id`, `task_identity_hash`, `output_hash`, `exact_head` |
| `confirmed_by` | Nonempty confirmer name, at most 200 characters |
| `cross_project_reuse_ack` | Boolean; must be true when source and target projects differ |
| `idempotency_key` | 1–128 characters from letters, digits, `.`, `_`, `:`, `-` |
| `modules` | One object per exact target module ID, containing `module_id`, `summary`, `remaining`, `requirement_refs` |

`summary` and `remaining` are nonempty plain text, each at most 4,000 characters.
`requirement_refs` is a nonempty sorted unique list of zero-based indices into
that module's frozen requirements. It cites the facts used for the explanation;
it does not omit other requirements from the detailed view. Request modules may
arrive in any order; the DTO follows the target profile's module order.
Unknown fields, unsafe secret-like text and malformed identities are rejected.

## Source and output contract

The server reopens the exact confirmed or superseded profiles, verifies full
content hashes, and requires the source and target `planned_modules` and
`implementation_mappings` to be structurally identical. Matching names, latest
tasks or profile notes are never a provenance link. The explicitly selected
succeeded baseline task must bind the source project/profile, its identity hash
and saved output hash. The task repository URL must equal the target approval's
frozen repository URL, not the current project settings.

Every saved requirement index, status, rationale and evidence list is checked
against the source profile and aggregate mapping. Requirement text states the
plan; it is not itself implementation evidence. Historical baseline HEAD may
differ from the report's approved HEAD. `historical_baseline` records that
difference, and clients must label these as existing baseline facts rather than
new work or a fresh runtime acceptance result. Unknown and partial facts remain
visible. The service does not manufacture evidence paths or line numbers.

The returned DTO includes all request fields plus `schema_version`, `project_id`,
`report_version_id`, `attribution`, `confirmed_at` (server UTC ISO timestamp),
`approved_git_head`, `historical_baseline` and `module_narrative_hash`. Each module
also contains **all** its `source_requirements`, in index order, with exactly
`requirement_index`, `requirement_text`, `status`, `rationale`, `evidence_ids`.
The SHA-256 `module_narrative_hash` covers every other DTO field using UTF-8 JSON,
sorted keys, compact separators and unescaped Unicode.

GET revalidates persisted source/target authorities and every stored fact.
Corrupt or mismatched annotations fail closed; they never fall back to another
version or disappear as an empty success.

Internal consumers call
`get_approved_module_narrative(project_id, report, approval_snapshot, profile, conn=None)`.
Pass an existing SQLite connection to keep validation in the caller's transaction.
It returns `dict | None` and raises `ApprovedModuleNarrativeError` with a stable
non-secret `code`. `validate_approved_module_narrative` is a pure renderer check
of DTO shape, target binding and hash; it is **not** a database provenance check.
Mail/readiness/storage entry points must use the getter's result, never accept a
caller-supplied DTO as confirmation authority. New email rendering identities
must include the annotation hash; historical send records remain unchanged.

## Delivery of a confirmed correction

The displayed annotation hash is sent as `expected_module_narrative_hash` to
`POST /api/projects/{project_id}/mail-send` (`null` for a report without this
annotation). A stale page cannot send a subsequently added explanation.

The v9, v10 and v11 document identities include the formal report hash and annotation
hash. Their Message-IDs are derived from the render hash with distinct `r9`,
`r10` and `r11` prefixes; all historical IDs remain valid. New corrected documents use v11.
When the original document has already been sent, the corrected document must
explicitly link to the most recent **sent** attempt for the same report,
approval and recipients. Both Python source closure and SQLite insertion guards
enforce this exception. Pending, sending, failed or UNKNOWN attempts do not
qualify as previously delivered documents. The original attempts and messages
are immutable, and the same correction cannot open a second send attempt.

## Approved code-change statistics

The header displays added lines, deleted lines and changed files from the exact
GitSnapshot already bound by the report's ApprovalSnapshot. Module status remains
in the module table. Parallel workstream counts are omitted because this report
contract has no authoritative workstream metric.

`GET /api/projects/{project_id}/anxin-board/reports/{report_version_id}/git-metrics`
requires the active local read session and exact `report_hash`. It returns
`approved_report_git_metrics_v1`: target report/approval identities, the frozen
Git facts and their hash, derived counts, and a canonical `metrics_hash`.
`load_approved_report_git_metrics` revalidates the persisted report, approval and
source facts; the UI and current email renderer consume the same projection. There
is no new Git scan, model call, write operation or approval mutation.

Missing, corrupt or mismatched facts return an error, never a numeric zero or
module-count substitute. Genuine zero counts remain zero. The page clears stale
statistics when switching reports and disables sending until the selected
report's statistics validate. Historical v9 emails remain readable with their
original module-hash and inline-image checks. Historical v10 emails also retain
their code-statistics marker validation.

## Report model attribution

The report information section displays the model used for that approved report.
Its provider, actual model identifier and recorded model version come from the
report's existing frozen approval provenance, never current settings or the
current provider catalog. The provider response's `actual_model` identifier and
the catalog version recorded at generation time have different meanings; the
latter is explicitly labelled `版本记录` and is not presented as an independently
verified provider version response.

The page, v11 email and self-contained HTML attachment show the same attribution.
Older reports without those fields show that no model was recorded rather than
borrowing the current model. Invalid V3 provenance remains an error. No report,
approval, saved AI response or previously sent email is rewritten for this
display change, and no additional model request is needed.
