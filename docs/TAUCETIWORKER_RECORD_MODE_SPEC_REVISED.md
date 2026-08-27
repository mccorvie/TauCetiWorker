# TauCetiWorker Record Mode Specification

**Status:** Draft MVP, revision 2
**Date:** 2026-08-26
**Target repository:** `TauCetiWorker`
**Primary consumer:** `TauCetiBench`

## 1. Summary

TauCetiWorker shall support an optional **record mode** that captures source material for a large corpus of realistic Tau Ceti benchmark tasks.

Record mode is not intended to be a time machine or a byte-for-byte replay of the original worker environment. Its purpose is to preserve enough authentic, standardized task state that TauCetiBench can later construct useful, reproducible offline tasks for comparing models, harnesses, prompts, and offered tools.

When enabled, TauCetiWorker shall:

1. select and prepare live work through its ordinary dispatch path;
2. perform a phase-specific, read-only materialization of task-defining context;
3. preserve the exact committed source state using Git commit IDs backed by durable local Git references;
4. copy small task-defining artifacts, including normalized GitHub context and the standard phase prompt, into a content-addressed record store;
5. seal an immutable raw capture before the live agent begins model-generated work; and
6. launch the live agent through the ordinary TauCetiWorker path without changing its prompt, tools, permissions, or publication behavior.

The MVP shall support raw captures for:

- `fix`, represented as `fix-review-raw`; and
- `roadmap`, represented as `roadmap-opportunity-raw`, from which TauCetiBench curation can later construct an exact-target roadmap task.

Review tasks are outside record mode MVP because the first TauCetiBench review slice will use existing TauCetiData entries. Fix-CI, rebase, bump, progress, and full-lifecycle capture are deferred.

## 2. Design principle: preserve the benchmark task, not the entire live world

Record mode shall preserve task-defining inputs according to their natural storage model:

| Input type | Preservation mechanism |
|---|---|
| Tau Ceti and auxiliary repository source state | Commit SHAs plus durable refs in shared bare Git repositories |
| GitHub-only state such as PR descriptions and review findings | Normalized, content-addressed JSON or text blobs |
| Standard TauCetiWorker phase prompts | Exact copied prompt artifacts plus TauCetiWorker SHA provenance |
| Original model, tools, transcript, timing, and result | Not part of record mode; owned by TauCetiBench run records or ordinary worker telemetry |

The recorder shall not try to preserve every fact the live agent could have discovered through arbitrary `gh`, Git, filesystem, or network exploration. It shall preserve a documented, standardized context package that is sufficiently faithful for benchmark use.

The expected fidelity claim is therefore:

> The capture preserves the exact committed code state and a standardized, authentic task context assembled at dispatch time. It does not reproduce every aspect of the original live worker environment or information-gathering trajectory.

Every raw capture shall declare:

```json
{
  "fidelity": "materialized-context",
  "purpose": "benchmark-corpus"
}
```

## 3. Architectural boundary

The intended separation is:

```text
TauCetiWorker live dispatcher
    ├── surveys current work
    ├── selects a candidate
    ├── prepares the ordinary live execution
    ├── RECORD MODE: emits an immutable raw capture
    └── launches the ordinary live agent

TauCetiBench corpus tooling
    ├── validates and imports raw captures
    ├── curates benchmark tasks
    ├── writes exact-target task instructions
    ├── chooses benchmark-visible context
    └── excludes original live solutions

TauCetiBench runner
    ├── runs model/tool/harness configurations
    ├── records timing, usage, outputs, and failures
    └── evaluates and compares runs
```

A raw capture is not necessarily a finished benchmark task. In particular, a roadmap capture records a genuine pre-authoring opportunity; a curator later converts it into a `roadmap-exact` task.

TauCetiWorker shall only be modified to capture task source material. Benchmark execution and run instrumentation belong exclusively to TauCetiBench.

## 4. Terminology

### 4.1 PR head

The **PR head SHA** is the commit at the tip of the pull request's source branch at capture time. In this specification, `head` means the PR source tip, not Git's local symbolic `HEAD` value.

### 4.2 Base and main

The **base SHA** is the commit at the tip of the pull request's target branch at capture time. For ordinary Tau Ceti pull requests the base branch will usually be `main`.

A roadmap task has no PR head. Its authoring base is the exact Tau Ceti `main` commit from which the task begins.

Where the PR base branch is `main`, the manifest shall avoid pretending that `base_sha` and `main_sha` are independent states. It may represent one target-branch object with both the branch name and SHA.

### 4.3 Git object database

A **Git object database** is the shared content-addressed store of commits, trees, and file contents used by a Git repository. It can reconstruct every retained commit while sharing unchanged data between commits.

Record mode shall use one shared bare Git repository per source repository, not one clone per capture.

### 4.4 Retained Git ref

A **retained ref** is a local Git reference such as:

```text
refs/tauceti-record/<capture-id>/head
```

It points to a captured commit and keeps that commit and its reachable objects from becoming eligible for normal Git garbage collection, even if GitHub later deletes or force-pushes the original branch.

The commit SHA is the identity of the source state. The retained ref is the local durability mechanism.

### 4.5 GitHub context

**GitHub context** means task-defining state that is not stored in Git objects, including PR titles and bodies, review comments, review thread status, labels, and review scoreboards. It must be snapshotted separately from repository source history.

### 4.6 Raw capture

An immutable, pre-agent record emitted by TauCetiWorker. It contains retained source-state references, copied prompt artifacts, and normalized task context.

### 4.7 Curated benchmark task

A treatment-neutral task produced by TauCetiBench corpus tooling from a raw capture. It contains the exact instructions and local context presented to each experimental arm.

### 4.8 Run record

The record of one TauCetiBench attempt by one model, harness, prompt, and tool configuration. Run records belong exclusively to TauCetiBench.

### 4.9 Outcome

Later facts about a live PR, such as merge status or subsequent reviews. Outcomes are not part of record mode MVP and must not be mixed into raw benchmark input.

## 5. Goals

Record mode shall:

1. **Capture authentic work.** Cases shall originate from tasks TauCetiWorker actually selected for live execution.
2. **Preserve exact committed source state.** Every capture shall identify the relevant commits and retain their Git objects locally rather than relying on the continued existence of remote branches.
3. **Build an extensive corpus efficiently.** Many captures shall share Git object databases and content-addressed blobs rather than copying whole repositories or repeated context documents.
4. **Materialize context before the benchmark boundary.** TauCetiBench agents shall receive normalized local context rather than requiring live GitHub access.
5. **Copy task-defining prompts.** The capture shall contain the exact standard phase prompt artifacts needed to understand how the task was framed, without requiring an old TauCetiWorker checkout.
6. **Remain treatment-neutral.** Captures shall not depend on which model, effort level, harness, sandbox, or optional tool was used in the original live run.
7. **Avoid original-solution leakage.** The raw capture shall be sealed before the live agent produces a patch or other candidate output.
8. **Be minimally invasive.** With record mode disabled, TauCetiWorker behavior shall remain unchanged. With it enabled, a recording failure shall not block otherwise valid live work.
9. **Support offline benchmark construction.** Once imported and materialized by TauCetiBench, a benchmark task shall not require GitHub access.
10. **Be auditable.** Captured fields, copied artifacts, and retained refs shall have documented origins and integrity hashes.

## 6. Non-goals

Record mode MVP shall not:

- reproduce the original live environment with absolute fidelity;
- record every query or discovery the original agent made;
- provide a frozen GitHub-compatible command facade;
- archive a complete TauCetiWorker Git repository for each capture or as a required replay dependency;
- archive `.lake`, compiler caches, Bubble images, or other performance state;
- record the original model, provider, effort, harness, sandbox, or offered tools as task inputs;
- record model tokens, wall-clock time, cost, transcript, tool calls, or final output;
- evaluate the live run;
- simulate an entire author-review-fix lifecycle;
- change the live agent prompt to force use of captured context;
- automatically replay a task;
- automatically infer an exact roadmap target before the roadmap agent chooses one;
- automatically associate a roadmap raw capture with a later PR;
- capture review tasks already covered by TauCetiData;
- support `fix-ci`, `rebase`, `bump`, or `progress` in the first implementation; or
- define TauCetiBench timing, evaluation, statistical, or experiment schemas.

## 7. Current-worker boundary and context materialization

TauCetiWorker currently has only a partial dispatch boundary. The outer worker surveys GitHub and selects a candidate, while phase agents may still perform detailed inspection themselves.

Record mode shall not attempt to reproduce the agent's exact information-gathering path. Instead, it shall add a deterministic **context materializer** for each supported phase. The materializer performs a documented set of read-only queries and stores normalized results.

This deliberately defines a benchmark one level after task discovery:

```text
live repository and GitHub state
        ↓
TauCetiWorker candidate selection
        ↓
record-mode context materialization
        ↓
raw capture
        ↓
TauCetiBench curated offline task
```

The benchmark therefore measures how an agent performs when given a standardized authentic task context. It does not measure skill at discovering that context through GitHub commands.

## 8. User interface

### 8.1 CLI

Record mode shall be enabled by supplying a record directory:

```bash
tauceti work --record-dir /var/lib/tauceti/records
```

The same option shall be available to command paths that launch persistent or repeated worker rounds. Child processes shall inherit the resolved record directory.

### 8.2 Environment variable

The equivalent environment variable shall be:

```bash
TAUCETI_RECORD_DIR=/var/lib/tauceti/records
```

An explicit CLI value takes precedence over the environment variable.

### 8.3 Disabled behavior

When neither the flag nor environment variable is present:

- no recorder object is constructed;
- no additional GitHub queries are made;
- no archival Git fetches or filesystem writes occur; and
- live execution behavior remains unchanged.

### 8.4 Failure policy

Record mode is **fail-open** for live work. A capture failure shall:

1. emit a structured warning with the phase, candidate identity, and failure class;
2. leave no apparently complete capture behind; and
3. allow the ordinary live task to continue.

There is no strict or fail-closed mode in the MVP.

## 9. Capture timing

A capture shall be sealed as late as practical before provider invocation, after the live candidate and committed source state have been determined, but before model-generated work can contaminate benchmark input.

### 9.1 Fix

The fix capture hook shall run after:

- the PR candidate has been selected;
- the expected PR head SHA is known;
- ordinary claim or dispatch coordination has succeeded; and
- the worker has enough information to launch the fix task.

It shall run before either the host agent process or Bubble execution is launched.

The recorder shall independently retain the exact PR head and target-branch commits on the host. It shall not depend on later access to a Bubble filesystem.

### 9.2 Roadmap

The roadmap capture hook shall run after:

- the roadmap area has been selected or explicitly pinned;
- the exact Tau Ceti `main` SHA has been resolved;
- roadmap and review repository revisions have been fetched;
- claims, holds, and other ordinary administrative context have been assembled; and
- any configured skip list is known.

It shall run before the roadmap agent is launched.

### 9.3 Consistency recheck

Immediately before sealing, the recorder shall re-read the task-defining mutable identifiers:

- for fix: PR head SHA and target-branch SHA;
- for roadmap: Tau Ceti `main` SHA and the selected roadmap/review revisions.

If a task-defining reference changed during capture, the recorder shall reject the capture with `input_raced`. The live worker may continue using its ordinary race-handling behavior.

## 10. Storage model

### 10.1 Directory layout

```text
<record-dir>/
├── format.json
├── captures/
│   └── <capture-id>/
│       ├── capture.json
│       └── COMPLETE
├── blobs/
│   └── sha256/
│       └── <first-two>/<remaining-digest>
├── git/
│   ├── TauCeti.git/
│   ├── TauCetiRoadmap.git/
│   ├── TauCetiReview.git/
│   └── sources/
└── errors/
    └── recording-errors.jsonl
```

The repositories under `git/` are shared **bare Git repositories**. They have Git object databases and refs but no checked-out working trees.

A capture directory shall not contain a clone of Tau Ceti. Hundreds or thousands of captures shall point into the same shared object database.

### 10.2 Shared Git object stores

For each source repository, the recorder shall maintain one bare archival repository. Required commits shall be fetched or copied into that repository before the capture is sealed.

If 1,000 captures refer to the same Tau Ceti `main` commit, they may have 1,000 tiny refs pointing to one commit object. If their PR branches differ by a few commits, Git stores only the additional reachable objects, subject to ordinary Git packing and delta compression.

The archival repository is independent of the worker's ordinary mutable checkout. The worker may reuse its normal checkout for execution, but benchmark durability shall not depend on that checkout remaining intact.

### 10.3 Retained refs

The recorder shall create only the refs required to keep each independently selected source tip reachable.

Typical fix refs are:

```text
refs/tauceti-record/<capture-id>/head
refs/tauceti-record/<capture-id>/base
```

Typical roadmap refs are:

```text
refs/tauceti-record/<capture-id>/main
```

Auxiliary repositories use corresponding refs, for example:

```text
refs/tauceti-record/<capture-id>/roadmap
refs/tauceti-record/<capture-id>/review
refs/tauceti-record/<capture-id>/source-<name>
```

A separate retained ref for the merge base is not required because the merge base is reachable from the retained head and base histories. Its SHA may still be recorded as convenient derived metadata.

Before writing `COMPLETE`, the recorder shall verify that each declared commit resolves in the local bare repository and is reachable from its declared retained ref.

Deleting or force-pushing the corresponding branch on GitHub shall not invalidate an already complete local capture.

Local retained refs protect against loss through remote branch deletion and normal local Git garbage collection. They do not replace ordinary filesystem backup against disk loss.

### 10.4 Content-addressed blobs

Normalized JSON documents, copied prompt artifacts, logs obtained during context materialization, and other non-Git data shall be stored by SHA-256 digest.

Capture manifests shall reference them as:

```json
{
  "algorithm": "sha256",
  "digest": "...",
  "size_bytes": 12345,
  "media_type": "application/json"
}
```

Identical prompts or context documents naturally deduplicate across captures.

### 10.5 Atomic completion

A capture is visible to importers only when both are present:

- a valid `capture.json`; and
- an empty `COMPLETE` marker written after all referenced Git objects and blobs have been verified.

Writers shall build captures in a temporary directory and rename them atomically into `captures/<capture-id>`.

A failed or interrupted capture shall never appear complete.

### 10.6 Retention

Record mode MVP shall not automatically prune complete captures or their retained refs. Retention, backup, bundle export, and corpus release policy are deferred operational concerns.

## 11. Prompt preservation and TauCetiWorker provenance

### 11.1 Required prompt artifacts

Each capture shall copy the following small prompt artifacts into the content-addressed blob store:

1. the exact standard phase prompt template bytes from the TauCetiWorker checkout used at capture time;
2. the exact **treatment-neutral rendered base prompt** after ordinary phase substitutions but before optional tool instructions or provider/harness-specific wrapping; and
3. a canonical JSON document containing the logical substitution inputs.

For example:

```text
prompt/template.md
prompt/rendered-base.md
prompt/logical-inputs.json
```

These names are conceptual; the actual files may be content-addressed blobs referenced by the manifest.

### 11.2 Provenance

The manifest shall also record:

- TauCetiWorker repository identity;
- TauCetiWorker commit SHA;
- prompt source path;
- hashes of the copied template, rendered base prompt, and logical inputs; and
- recorder schema/version.

### 11.3 No TauCetiWorker archival repository requirement

TauCetiBench replay shall not require an archived TauCetiWorker Git object database or the ability to check out the historical Worker commit.

The Worker SHA is provenance. The copied prompt artifacts and materialized context are the replayable task inputs.

This follows the rule:

> Preserve the task produced by the Worker, not all of the machinery that produced it.

A normal TauCetiWorker development clone may retain the relevant history incidentally, but it is not part of the record-mode storage contract.

### 11.4 Treatment-specific prompt material

Tool instruction appendices, provider-specific wrappers, model identity, effort settings, and other treatment-specific material shall not be incorporated into the raw capture's benchmark prompt or capture identity.

If ordinary operational logs retain the full live prompt, that is separate telemetry and not part of record mode.

## 12. Capture identity and immutability

### 12.1 Canonical identity

The capture ID shall be derived from a canonical JSON identity document and SHA-256:

```text
capture_id = "tc-" + sha256(canonical_identity_json)[0:24]
```

The canonical identity shall include:

- capture schema version;
- phase and capture subtype;
- exact task-defining Git SHAs;
- hashes of normalized task context documents;
- hashes of the standard prompt template and treatment-neutral rendered base prompt;
- designated roadmap area or PR number, as applicable; and
- any supplementary source-repository SHA that is part of the task.

It shall not include:

- capture timestamp;
- worker name;
- model or provider;
- effort level;
- enabled tools;
- sandbox mode;
- live run result; or
- wall time, tokens, or cost.

### 12.2 Idempotence

Recording the same canonical task twice shall resolve to the same capture ID. If the existing capture validates, the recorder shall reuse it rather than rewrite it.

If an existing capture directory has the same ID but different canonical contents, the recorder shall report `identity_collision` and leave the original untouched.

### 12.3 Immutability

A complete capture shall never be modified in place. New comments, a new PR head, changed claims, a changed standard prompt, or other task-defining context changes create a new capture ID.

## 13. Common capture manifest

Every `capture.json` shall conform to `tauceti.record.capture/v1`.

Illustrative fix capture:

```json
{
  "schema": "tauceti.record.capture/v1",
  "capture_id": "tc-0123456789abcdef01234567",
  "phase": "fix",
  "capture_type": "fix-review-raw",
  "fidelity": "materialized-context",
  "purpose": "benchmark-corpus",
  "captured_at": "2026-08-26T20:15:00Z",

  "provenance": {
    "worker_repository": "https://github.com/kim-em/TauCetiWorker",
    "worker_sha": "...",
    "recorder_version": "1",
    "worker_name": "mathos",
    "round_id": "..."
  },

  "candidate": {
    "repository": "TauCetiProject/TauCeti",
    "pr": 412,
    "expected_head_sha": "..."
  },

  "repositories": {
    "tauceti": {
      "pr_head_sha": "...",
      "head_retained_ref": "refs/tauceti-record/.../head",
      "base": {
        "branch": "main",
        "sha": "...",
        "retained_ref": "refs/tauceti-record/.../base"
      },
      "merge_base_sha": "..."
    },
    "roadmap": {
      "sha": "...",
      "retained_ref": "refs/tauceti-record/.../roadmap"
    },
    "review": {
      "sha": "...",
      "retained_ref": "refs/tauceti-record/.../review"
    }
  },

  "dependency_state": {
    "lean_toolchain": {"algorithm": "sha256", "digest": "..."},
    "lake_manifest": {"algorithm": "sha256", "digest": "..."},
    "mathlib_rev": "..."
  },

  "context": {
    "pr": {"algorithm": "sha256", "digest": "..."},
    "review_threads": {"algorithm": "sha256", "digest": "..."},
    "unresolved_findings": {"algorithm": "sha256", "digest": "..."}
  },

  "prompt": {
    "source_path": "prompts/fix.md",
    "template": {"algorithm": "sha256", "digest": "..."},
    "rendered_base": {"algorithm": "sha256", "digest": "..."},
    "logical_inputs": {"algorithm": "sha256", "digest": "..."}
  },

  "integrity": {
    "canonical_identity_sha256": "...",
    "git_objects_verified": true,
    "retained_refs_verified": true,
    "context_blobs_verified": true,
    "prompt_blobs_verified": true,
    "secret_scan_passed": true
  }
}
```

Optional values shall be omitted rather than represented by misleading empty strings.

Operational provenance such as worker name and round ID may be stored for traceability but shall not be part of canonical task identity.

## 14. Treatment neutrality

The raw capture describes the benchmark source task, not the original experimental treatment.

It may retain the TauCetiWorker source SHA and copied standard phase prompt because they explain how the task was framed and materialized. It shall not encode the following as task-defining fields:

- original model;
- original provider;
- original reasoning effort;
- original model harness;
- original offered tools;
- original sandbox mode;
- original transcript;
- original output; or
- original completion status.

If ordinary logs already contain those facts, record mode need not delete or conceal them. It simply shall not incorporate them into the raw capture or canonical identity.

The benchmark's primary treatment is later defined by TauCetiBench, including whether a tool is **offered**, regardless of whether the benchmark agent actually uses it.

## 15. Fix capture

### 15.1 Capture type

```text
fix-review-raw
```

### 15.2 Required Git state

A fix capture shall retain:

- exact PR head SHA;
- exact target/base branch name and SHA visible at capture time;
- merge-base SHA as derived metadata;
- `lean-toolchain` contents and hash;
- `lake-manifest.json` contents and hash;
- resolved Mathlib revision;
- exact TauCetiRoadmap revision used by the worker, when available; and
- exact TauCetiReview/rubric revision used by the worker, when available.

The PR head and base commits shall be retained in the shared Tau Ceti bare repository. Roadmap and review commits shall be retained in their respective shared bare repositories.

The capture is invalid if the retained PR head does not equal the candidate's expected head.

### 15.3 Required GitHub context

The context materializer shall save normalized representations of:

- PR number, title, body, URL, author, base branch, and head branch;
- labels and current state;
- issue comments attached to the PR;
- submitted reviews;
- inline review comments and thread relationships;
- current sticky review scoreboard, if present;
- rubric verdicts and dispositions represented in the scoreboard;
- unresolved blocking or request-changes findings;
- replies to those findings; and
- task-defining metadata already produced by the worker survey.

The materializer shall preserve normalized source documents. It may additionally produce a derived document such as `unresolved_findings.json`, but every derived context object shall identify its algorithm/version and source blob hashes.

### 15.4 Benchmark curation

A TauCetiBench importer may turn a valid fix capture into a `fix-review` task by:

1. materializing the captured PR head from the local bare Git repository;
2. selecting the exact unresolved findings supplied to the benchmark agent;
3. writing a treatment-neutral task instruction;
4. choosing which captured context documents are benchmark-visible; and
5. excluding all later PR commits, replies, reviews, and outcomes.

The importer need not reproduce the live agent's original `gh` command behavior.

## 16. Roadmap capture

### 16.1 Capture type

```text
roadmap-opportunity-raw
```

This is deliberately not called `roadmap-exact`. The exact implementation target is not known before the live roadmap agent starts.

### 16.2 Required Git state

A roadmap capture shall retain:

- exact Tau Ceti `main` SHA used as the authoring base;
- exact TauCetiRoadmap revision;
- exact TauCetiReview/rubric revision;
- `lean-toolchain` contents and hash;
- `lake-manifest.json` contents and hash;
- resolved Mathlib revision; and
- exact SHA of any supplementary `--source` repository included in the task.

If a configured supplementary source cannot be pinned and retained, the recorder shall mark the capture unsupported and shall not seal it.

### 16.3 Required roadmap context

The context materializer shall save:

- designated roadmap area;
- roadmap files visible to the worker;
- current skip-target configuration;
- active claims, intentions, and administrative holds relevant to the area;
- a bounded normalized snapshot of open Tau Ceti PRs;
- a bounded normalized snapshot of recently merged Tau Ceti PRs;
- survey summaries already used by the worker to select the area;
- staged review rubric bundle and hash; and
- source-repository description and revision, when applicable.

The query fields and list limits used for open and merged PR snapshots shall be fixed by recorder schema version and documented in code.

### 16.4 Exact-target curation

TauCetiBench corpus tooling shall later create a `roadmap-exact` task from a raw roadmap capture. Curation may use, outside the benchmark-visible task package:

- the live PR's target marker;
- PR title and body;
- the corresponding roadmap milestone;
- the original diff; and
- human editing.

The curator shall write a self-contained exact target with bounded scope and clear expected artifacts.

The original live patch may be retained in a quarantined provenance store for evaluator reference, but it shall never be mounted or exposed to benchmark candidate agents.

Record mode MVP does not automatically associate a raw capture with a later PR. Curation tooling shall accept an explicit mapping such as:

```bash
tauceti-bench corpus curate-roadmap \
  --capture tc-0123456789abcdef01234567 \
  --source-pr 412
```

This mapping is corpus provenance, not a TauCetiWorker run record.

## 17. Review phase

TauCetiWorker record mode MVP shall not emit review captures.

The first TauCetiBench review suite shall instead import rubric-level cases from TauCetiData, where the natural task identity is an exact `(PR, head SHA, rubric, rubric revision)` context.

A future record-mode version may capture additional review context if TauCetiBench requirements diverge from what TauCetiData preserves.

## 18. Bubble and host execution

Record mode runs in the outer TauCetiWorker process regardless of whether the live agent will execute on the host or in Bubble.

This has two consequences:

1. capture uses host-side read-only Git/GitHub access and the shared archival stores; and
2. no capture artifact may rely on paths or transient files that exist only inside a Bubble container.

The ordinary Bubble security policy, mounts, GitHub proxy, and agent capabilities remain unchanged. Record mode does not attempt to constrain or emulate them.

TauCetiBench may later use Bubble with a stricter offline capability profile, but that is outside this specification.

## 19. Secrets and data hygiene

Record mode shall use an explicit allowlist of fields. It shall never archive:

- GitHub authentication tokens;
- provider API keys;
- complete process environments;
- credential-helper output;
- SSH keys or configuration;
- cookies;
- arbitrary files from `$HOME`;
- raw Bubble proxy credentials; or
- model-provider request or response payloads.

Public GitHub identities and comments are legitimate task context, but canonical manifests shall avoid workstation-specific absolute paths. Local paths may appear only in non-portable recorder logs.

Before writing `COMPLETE`, the recorder shall scan manifests and context documents for known secret variable names and token patterns. This is defense in depth, not a substitute for allowlisted collection.

## 20. Error handling

The recorder shall classify failures using stable codes, including at least:

- `input_raced`;
- `github_read_failed`;
- `git_object_missing`;
- `git_fetch_failed`;
- `retained_ref_failed`;
- `context_normalization_failed`;
- `prompt_capture_failed`;
- `unsupported_source`;
- `secret_scan_failed`;
- `identity_collision`;
- `storage_failed`; and
- `validation_failed`.

Errors shall be appended as JSON lines under `errors/recording-errors.jsonl`. Error records may contain operational diagnostics but shall not contain secrets.

No failed or incomplete directory may be accepted by a TauCetiBench importer.

## 21. Implementation outline

### 21.1 New module

Add a module such as:

```text
tauceti_worker/recording.py
```

Suggested abstractions:

```python
@dataclass(frozen=True)
class RecordConfig:
    root: Path

class BlobStore: ...
class GitObjectStore: ...
class PromptCapture: ...
class CaptureBuilder: ...
class FixContextMaterializer: ...
class RoadmapContextMaterializer: ...
class TaskRecorder: ...
```

### 21.2 Worker options

Add `record_dir: Path | None` to resolved worker/round options. The value shall be passed to supported work-unit handlers but not to model-provider adapters.

### 21.3 Phase hooks

Add narrowly scoped hooks immediately before existing agent-launch calls:

```python
recorder.maybe_capture_fix(...)
recorder.maybe_capture_roadmap(...)
```

The hooks shall receive already-resolved candidate metadata, repository/configuration objects, and the treatment-neutral rendered base prompt. They may perform additional read-only queries needed by their context materializer.

### 21.4 Prompt seam

Prompt construction shall expose a treatment-neutral base representation before optional tool instructions or provider-specific wrapping are applied. Record mode copies that representation; normal execution continues using the existing final prompt path.

This is task-input capture, not model-run instrumentation.

### 21.5 No model or publication-layer changes

Record mode MVP shall not require changes to:

- model invocation adapters;
- token or timing accounting;
- tool-call collection;
- transcript collection;
- TauCetiReview execution;
- PR publication; or
- push logic.

### 21.6 Canonical JSON

Identity and manifest hashing shall use one canonical JSON implementation with:

- UTF-8 encoding;
- sorted object keys;
- no insignificant whitespace; and
- stable treatment of timestamps and omitted optional fields.

## 22. Testing requirements

### 22.1 Unit tests

Tests shall cover:

- deterministic canonical capture IDs;
- exclusion of timestamp and worker identity from task identity;
- inclusion of task-defining prompt hashes in task identity;
- content-addressed blob deduplication;
- shared Git object-store behavior;
- retained-ref creation and verification;
- recovery of a commit after its simulated remote branch is deleted;
- atomic completion-marker behavior;
- secret-field rejection;
- identity-collision handling;
- normalization of review threads and findings;
- race detection; and
- unsupported supplementary sources.

### 22.2 Integration tests

Using local Git fixtures and mocked GitHub responses, tests shall demonstrate:

- reconstruction of an exact fix PR head and target-branch state;
- computation of the expected merge base from retained history;
- preservation of comments, scoreboard, and inline review context;
- reconstruction of an exact roadmap base with roadmap and review revisions;
- many captures sharing one bare object database without per-capture clones;
- copied standard prompt template and rendered base prompt retrieval without a TauCetiWorker checkout;
- equivalent capture behavior before host and Bubble dispatch;
- fail-open live execution when recording fails; and
- idempotent reuse when the same task is selected again.

### 22.3 Regression test

A no-recording test shall verify that, without `--record-dir` or `TAUCETI_RECORD_DIR`, no recorder queries, archival fetches, or writes occur and the existing work-unit call sequence is unchanged.

## 23. Acceptance criteria

Record mode MVP is complete when all of the following hold:

1. `tauceti work --record-dir PATH` is supported and documented.
2. Record mode disabled produces no behavioral or I/O changes.
3. A live fix dispatch can emit a complete `fix-review-raw` capture before agent launch.
4. The fix capture can reconstruct the exact PR head locally without consulting GitHub.
5. The fix capture includes normalized review findings and their source discussion.
6. A live roadmap dispatch can emit a complete `roadmap-opportunity-raw` capture before agent launch.
7. The roadmap capture retains exact Tau Ceti, roadmap, review, dependency, and supplementary-source revisions as applicable.
8. Captures share bare Git object databases; no complete Tau Ceti clone is stored per capture.
9. Local retained refs keep captured commits reconstructible after simulated remote branch deletion or force-push.
10. Each capture contains the exact standard phase prompt template, treatment-neutral rendered base prompt, and logical substitution inputs.
11. TauCetiBench can read those prompt artifacts without checking out the historical TauCetiWorker revision.
12. A capture never includes model output, transcript, timing, tokens, cost, or original offered-tool state.
13. A capture failure does not prevent ordinary live execution.
14. Re-recording an unchanged task deduplicates to the same capture ID.
15. A changed PR head, task-defining context, or standard prompt produces a different capture or is rejected as a race.
16. No complete capture contains credentials or workstation-specific private data.
17. TauCetiBench corpus tooling can validate and import both supported capture types.

## 24. Deferred work

Potential later extensions include:

- `fix-ci`, rebase, and dependency-bump context materializers;
- an optional capture-to-live-PR association record;
- portable Git-bundle export;
- backup, retention, and pruning policy;
- a frozen GitHub facade for higher-fidelity live-prompt replay;
- review capture beyond TauCetiData;
- direct two-stage roadmap selection and authoring; and
- automatic exact-target drafting with mandatory human approval.

## 25. Source references

This specification is based on the current division of responsibility in:

- [TauCetiWorker work-unit dispatch](https://github.com/kim-em/TauCetiWorker/blob/main/tauceti_worker/work_units.py)
- [TauCetiWorker fix prompt](https://github.com/kim-em/TauCetiWorker/blob/main/prompts/fix.md)
- [TauCetiWorker roadmap prompt](https://github.com/kim-em/TauCetiWorker/blob/main/prompts/roadmap.md)
- [TauCetiWorker sandbox documentation](https://github.com/kim-em/TauCetiWorker/blob/main/docs/sandbox.md)
- [TauCetiData](https://github.com/TauCetiProject/TauCetiData)
- [TauCetiReview](https://github.com/TauCetiProject/TauCetiReview)
