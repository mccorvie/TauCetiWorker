# TauCetiBench test harness design notes

This document records the initial corpus inventory and design considerations for
a test harness built from TauCetiWorker record-mode captures. It is a planning
document, not a replacement for the record format contract.

The canonical format, validation, materialization, security, and reproducibility
requirements are in [Record mode and the TauCetiBench import
contract](record-mode.md). A harness should implement that contract directly and
treat this document's inventory as a dated snapshot.

## Corpus snapshot

The following inventory was observed in the local `state/records` store on
2026-09-14 after approximately one week of collection:

| Item | Observed value |
| --- | ---: |
| Complete captures | 408 |
| `fix-review-raw` captures | 338 |
| Unique fix PR numbers | 66 |
| `roadmap-opportunity-raw` captures | 70 |
| Unique roadmap candidate objects | 38 |
| Content-addressed blobs | 2,354 |
| Store size | 571 MB |
| Recording errors | 21 |

The first capture was recorded at `2026-09-07T20:57:19Z` and the latest in this
snapshot at `2026-09-14T16:55:18Z`. Full UTC days produced between 49 and 66
captures, with no unexplained full-day gap.

All 408 capture directories had a regular, zero-byte `COMPLETE` marker, and all
capture-time integrity Boolean values in their manifests were `true`. The 21
operational errors were all `input_raced`: the recorder detected that an input
ref changed while it was being archived and rejected the attempt. They do not
represent corrupt or partially admitted captures. They affected 19 unique
candidates and should be retained for coverage accounting.

An importer-grade run of the following command is still the authority for
admission; this inventory is not a substitute for a successful run:

```bash
tauceti records validate state/records
```

That command checks manifests, canonical identities, blobs, dependency pins,
Git history and retained refs, including a fresh fetch and checkout. Record the
command result and validator revision with any frozen corpus release.

### Corpus shape

Capture count is not task count. A fix PR can produce a new capture whenever its
task-defining inputs change. In this snapshot, 338 fix captures came from only
66 PRs. The most frequently captured PR had 23 states; several others had 9 or
more. These states are useful for longitudinal experiments, but are highly
correlated samples.

Similarly, a roadmap capture is an opportunity snapshot, not necessarily a
concrete implementation task. The 70 raw roadmap captures contained 38 unique
candidate objects and require curation unless the test explicitly measures
target selection.

## How records are organized

At a high level, a record store contains:

```text
<record-root>/
  format.json                         # store and capture schema versions
  captures/
    tc-<24 lowercase hex>/
      capture.json                    # manifest and references
      COMPLETE                        # zero-byte atomic completion marker
  blobs/sha256/<2 hex>/<62 hex>       # deduplicated non-Git content
  git/
    TauCeti.git                       # shared bare object store
    TauCetiRoadmap.git
    TauCetiReview.git
    sources/<16 hex>.git              # optional supplementary repositories
  errors/recording-errors.jsonl       # operational log; never task input
```

The capture manifest identifies immutable Git commits and retained refs, blob
digests, dependency pins, normalized context, prompt material, provenance, and
capture-time integrity results. A capture ID is derived from a canonical digest
of task-defining inputs. `captured_at` and the operational worker name are
provenance and are deliberately excluded from identity.

There are currently two capture types:

- `fix-review-raw` records a PR head, its observed base and merge base, review
  context, the authoritative unresolved-finding set, dependency state, and
  neutral prompt material. Evaluation starts from the captured PR head, not a
  current upstream ref.
- `roadmap-opportunity-raw` records the exact TauCeti, Roadmap, and Review
  commits; the historical roadmap/GitHub opportunity context; optional source;
  dependency state; and neutral prompt material. It records what work was
  available, not which implementation the original agent eventually chose.

See these sections of the format contract for normative details:

- [store layout and complete-capture discovery](record-mode.md#store-layout);
- [common manifest and prompt policies](record-mode.md#common-capture-manifest);
- [`fix-review-raw` materialization](record-mode.md#fix-review-raw);
- [`roadmap-opportunity-raw` curation](record-mode.md#roadmap-opportunity-raw);
- [required importer validation](record-mode.md#required-importer-validation);
- [security and trust](record-mode.md#security-and-trust); and
- [the minimum run record](record-mode.md#minimal-taucetibench-run-record).

## Suggested harness boundaries

Keep four stages separate so validation and scoring decisions remain auditable:

1. **Importer/admission.** Validate a frozen store and produce an immutable
   index of admitted capture IDs plus manifest identities. No captured content
   should be interpreted or executed before admission succeeds.
2. **Task definition and curation.** Map an admitted capture to a versioned task
   definition. Declare the prompt policy, visible context, writable repository,
   acceptance criteria, and grouping keys. Roadmap implementation tasks need a
   human- or policy-curated target; fix tasks need an explicit finding set.
3. **Runner.** Materialize fresh local checkouts, mount only declared inputs,
   provision dependencies from a pinned image/cache, disable network access,
   apply resource limits, invoke the model, and save the complete run record.
4. **Evaluator.** Run hidden checks outside the agent sandbox and report raw
   measurements separately from aggregate scores. Hidden tests, later outcomes,
   reference patches, and evaluator credentials must never enter agent-visible
   paths.

This separation permits importer and materializer tests without model calls,
and makes it possible to revise scoring without silently changing task inputs.

## Test design considerations

### Define the capability being measured

Do not mix materially different capabilities into one unexplained score.
Candidate benchmark families include:

- repairing the captured unresolved findings for a fix PR;
- completing a curated, bounded roadmap implementation task;
- selecting a useful unblocked roadmap target from a raw opportunity snapshot;
- producing a correct patch under different prompt or context policies; and
- robustness across successive states of the same PR or roadmap area.

Target selection and implementation should be scored separately when both are
part of one run. Publish per-family results before considering an aggregate.

### Prevent leakage and correlated splits

Never split randomly by `capture_id`. At minimum, group all fix captures for the
same repository and PR number into one split. For roadmap tasks, group snapshots
that lead to the same curated target, and consider grouping by roadmap area and
overlapping commits as well. If later review outcomes or merged patches are
used to build hidden evaluators, ensure those artifacts cannot appear in the
training or agent-visible side of another split.

Freeze and version a split manifest containing capture IDs, task-definition
IDs, group keys, exclusions, and the selection algorithm. Report both raw
capture counts and independent task/group counts.

### Choose snapshots deliberately

Repeated captures of a PR are legitimate state transitions. Decide in advance
whether an experiment uses:

- one deterministic snapshot per PR, such as the earliest eligible state;
- one snapshot per materially distinct finding set;
- every state, analyzed as a longitudinal series; or
- a seeded sample with at most a declared number of states per PR.

Weighting every capture equally would allow frequently changing PRs to dominate
the score. Prefer macro-averaging by independent task or PR, with capture-level
results retained for diagnosis.

### Keep treatments comparable

Use one declared prompt policy: either the captured treatment-neutral
`rendered_base`, or a separately versioned arm-invariant benchmark instruction.
Do not reconstruct provider-specific wrappers from the original live run. Give
evaluation arms the same captured context and tools unless exposure itself is
the treatment.

Pin the model identifier, harness commit, task-definition revision, container
image digest, dependencies, tool policy, network policy, resource limits, and
seed. Randomize treatment assignment within task and use paired comparisons
where practical. Repeat stochastic runs enough times to report uncertainty.

### Score outcomes, not resemblance alone

For implementation tasks, a useful scorecard should distinguish:

- task-specific hidden acceptance tests;
- project build and regression tests;
- whether the captured findings or acceptance criteria were actually resolved;
- patch validity and scope, including forbidden-file changes;
- safety and policy violations;
- completion rate and failure category; and
- cost, tokens, wall time, tool calls, and resource use.

Exact patch match should generally be diagnostic rather than the sole success
criterion: multiple correct Lean implementations may exist. Preserve raw test
results and patches so evaluator changes can be audited. Version hidden tests
and review them for false positives, solution overfitting, and dependence on
post-capture repository state.

### Handle dependencies and the network reproducibly

Captures pin `lean-toolchain` and `lake-manifest.json`, but do not include
package caches or an operating-system image. Build a trusted image/cache for
each required dependency state before execution, identify it by digest, and
then run offline. A network-enabled arm should use a deterministic recorded
facade where possible and must be labeled as a different treatment.

Start each run from fresh materialization. Only the main TauCeti checkout should
be writable; captured context, Roadmap, Review, and supplementary source trees
should be read-only. Do not leave record-store object paths configured as Git
remotes, and do not mount the record root into the agent sandbox.

### Treat captures as untrusted input

PR text, comments, roadmap prose, and supplementary repositories may contain
prompt injection or malicious code. Parse only documented fields. Do not honor
agent-configuration files from supplementary sources or execute their setup
scripts. Keep hidden tests, credentials, reference solutions, and orchestration
instructions outside the sandbox. Re-run secret scanning before distributing a
frozen corpus even when `secret_scan_passed` was true at capture time.

### Account for missing captures

Recording is fail-open, so the corpus is a sample of worker opportunities rather
than a complete worker activity log. Preserve `recording-errors.jsonl` for
coverage analysis, but never expose it to the evaluated agent or import it as a
task. Track attempted, admitted, curated, runnable, and scored counts separately,
with an exclusion reason at each stage.

## Recommended first milestone

A small end-to-end pilot should precede large model comparisons:

1. Freeze a validated copy or immutable index of this store.
2. Select 10-20 fix tasks from distinct PRs, stratified by finding count and
   apparent difficulty.
3. Write versioned task definitions and hidden acceptance tests without using
   current network state at run time.
4. Implement offline materialization and a no-agent smoke mode that proves the
   starting tree, context exposure, dependency image, and evaluator isolation.
5. Run one baseline model with multiple seeds, inspect failures manually, and
   revise task definitions or evaluators before freezing a larger suite.
6. Add curated roadmap tasks only after the fix pipeline and run-record format
   are stable; roadmap curation introduces substantially more judgment.

The pilot's primary deliverable should be confidence in reproducibility and
scoring validity, not a leaderboard number.

## Minimum artifacts to version

A reproducible benchmark release should version or content-address:

- the admitted-corpus index and validator result;
- split/group manifests and selection policy;
- task definitions and capture-to-task mappings;
- prompt and context-exposure policy;
- runner and evaluator source revisions;
- hidden evaluator assets in a quarantined store;
- container images and dependency caches by digest; and
- per-attempt run records using the schema recommended in
  [the record-mode contract](record-mode.md#minimal-taucetibench-run-record).
