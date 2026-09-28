# Source-confirmed technology evidence

This layer follows the existing generic repository preprocessing. The unchanged
Core inventories exact Git objects, applies sensitive-path and redaction policy,
and produces safe, hash-bound text chunks. Technology adapters never receive a
filesystem, Git client, network client, credential reader or provider capability.

`repository_technology.py` reassembles only contiguous Core-safe chunks with a
single HEAD/object identity. The adapter registry supplies dependency clues and
pure `detect(path, safe_text)` signature recognizers. Source-qualified actual
usage produces `SOURCE_CONFIRMED`; dependency declarations without such usage
produce `DECLARED_ONLY`; insufficient evidence produces `UNKNOWN`. These states
describe framework usage, not whether a planned feature is implemented.

Only confirmed technology activates relation extraction. Optional `scope_eligible`
and `context_eligible` hooks support safe cross-file inference. Scope boundaries
come from the complete tracked manifest inventory, including unreadable manifests;
declarations come only from safe contents. A blocked child manifest cannot inherit
the parent application's framework. Current manifest scopes are package.json and
pom.xml. Other build ecosystems retain the generic fallback until a registry
extension supplies their source detection. No project name or module count is used.

The initial registry includes SpringBootAdapter, SpringMvcAdapter,
SpringDIAdapter, MyBatisAdapter, MyBatisPlusAdapter, Vue2SfcAdapter,
VueRouterAdapter, VuexAdapter, AxiosAdapter and ElementUI usage metadata.
Jpa, Artemis, VueSocketIo, WebRtc, FlvJs and Xgplayer have declaration metadata
only; no deep adapters are implemented for these technologies.

Java recognition qualifies annotations/inheritance/calls through explicit imports
or an unambiguous wildcard import. Vue-family recognition requires imported
framework bindings with actual usage. Ambiguous or shadowed bindings are rejected
conservatively. SFC syntax requires a source-confirmed Vue2 bootstrap in the same
package scope. Router definitions can follow a one-hop explicit import from an
actual router registration. Axios wrappers must call a default import from a
source that exports an actual Axios-created client. Unresolved aliases and more
complex export chains remain unknown; this is not a full compiler or runtime
reachability proof. Current local resolution supports relative imports and the
Vue CLI `@/src` convention; arbitrary build aliases are not interpreted.

## Common evidence

`repository-technology-evidence/1` records technology, adapter id/version,
evidence type, source path, symbol, relation, related symbol/path, character and
line span, exact HEAD, Git object identity, safe source hash, source evidence IDs,
classification, redaction state and a deterministic record identity. No HTTP
address or credential is exported in technology metadata. Parser exceptions
discard that adapter's results for the file and retain generic text evidence;
raw exception messages are not emitted.

Reassembly verifies the complete Core safe-text hash and byte count, including
missing-tail and missing-all-chunks rejection. Each record's main symbol must
occur in its exact source slice, whose hash is bound separately. Related symbols
may occur elsewhere in the same source chunk (for example a store section name);
they cannot borrow text from another file. The planner checks the independent
source hash as well as HEAD, path, object, chunk identity and slice bindings.
Router context follows only a matching default import to an actually exported
literal route array (or a const holding that array); unresolved exports stay unknown.

Retrieval/batching consume this common structure with no technology branches.
Before planning, metadata must match its source chunk and identity. Splitting
removes relations whose locations or symbols are outside the resulting fragment.
Relations spanning separate source chunks are omitted from model-visible metadata
rather than citing unavailable material. Metadata is included in batch input
hashes. This does not change UNKNOWN retry rules, batch resume, deterministic
aggregation or Human confirmation. No absence of parser/retrieval evidence is
converted into `not_started` or `implemented`.

## Validation and limits

Fixture A retains the existing Python/Web/Core/batch regressions. Fixture B uses
`scripts/validate_technology_repository.py` with an isolated external clone and a
fresh explicit HEAD. The harness replaces only Product workspace admission with
local fixture access; the unchanged Core scan loop still reads/checks each exact
object and performs redaction. Git subprocesses are restricted to local read
operations. It does not access the Product DB, authorization or installed runtime.
Its two synthetic retrieval queries measure plumbing and richer evidence, not
customer implementation status or a real paid reconciliation.

Fixture C verifies unfamiliar-stack inventory/index/retrieval/batches/resume and
unknown results. Adversarial tests include declarations without usage, dead
imports, custom annotations, stale dependencies, monorepo boundaries, source
removal, unknown technologies, redaction/blocked paths, HEAD/hash mismatch,
manifest conflicts, adapter failures, cross-file metadata rejection and IO/provider
tripwires. Scanning/detection/retrieval/planning are local: provider calls and
network/model calls are zero. GitHub source acquisition is separate read-only
network activity, not model preprocessing.

Future provider execution still needs real wire-request budgeting, durable paid
batch authorization, production orchestration and candidate promotion, followed
by Human confirmation/report/mail validation. This phase neither enables those
steps nor reuses a consumed authorization. Keep PR Draft; no Ready/Merge/Release.
