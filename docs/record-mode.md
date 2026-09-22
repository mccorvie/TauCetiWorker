# Record mode and the TauCetiBench import contract

Record mode preserves the task state TauCetiWorker sees immediately before it
starts an agent. The resulting corpus is intended for offline, reproducible
TauCetiBench evaluations. It contains immutable Git commits, normalized task
context, dependency pins, and a treatment-neutral prompt. It does not contain
the original agent's response or execution environment.

This document describes the implemented `v1` format. It is both an operator
guide for producing captures and a consumer contract for turning them into
benchmark tasks.

## Enable recording

Pass a record-store directory to `tauceti work`:

```bash
tauceti work --record-dir /var/lib/tauceti/records
```

The equivalent environment variable is `TAUCETI_RECORD_DIR`. An explicit
`--record-dir` takes precedence. Loop rounds and persistent workers inherit the
resolved directory.

To disable recording, omit `--record-dir` and leave `TAUCETI_RECORD_DIR` unset
or empty. A worker's `env` table can set `TAUCETI_RECORD_DIR = ""` to override
an inherited recording directory. An explicitly empty `--record-dir` is an error.

Recording occurs after candidate selection and claim setup, but before the
agent starts. It currently applies to:

- `fix`, producing `fix-review-raw` captures; and
- `roadmap`, producing `roadmap-opportunity-raw` captures.

Other phases run normally without producing captures. Recording is fail-open:
a recording failure is logged and the live work continues. Consequently, a
missing capture does not mean the worker did not run.

For this workstation's paced Codex/Claude collection fleet, see
[Recording workers](record-workers.md) and
[`record-workers.toml`](../record-workers.toml).

## What reproducibility means

A complete capture preserves:

- the exact TauCeti commit at which the agent started;
- the comparison base for a fix task;
- the exact Roadmap, Review, and optional supplementary-source commits;
- the exact `lean-toolchain` and `lake-manifest.json` bytes from TauCeti;
- bounded, normalized GitHub context gathered before the agent ran;
- the prompt template, logical substitutions, and a treatment-neutral rendered
  prompt; and
- the TauCetiWorker commit and recorder version that produced the capture.

This is `materialized-context` fidelity, not a replay of the original run. A
capture deliberately does not preserve:

- model output, transcript, token usage, timing, or provider;
- provider-specific prompt wrappers;
- the exact sequence of GitHub queries an agent might later have made;
- a package cache, operating-system image, environment variables, credentials,
  or network responses; or
- offered tools and sandbox policy.

TauCetiBench must pin those execution variables in its own run record. For a
strictly reproducible evaluation, run without network access and record at
least the capture ID, harness revision, task-definition revision, model,
container image, tool policy, resource limits, and random seed.

## Store layout

The record directory has this layout:

```text
<record-root>/
  format.json
  captures/
    tc-<24 lowercase hex>/
      capture.json
      COMPLETE
  blobs/
    sha256/
      <first 2 digest characters>/<remaining 62 characters>
  git/
    TauCeti.git
    TauCetiRoadmap.git
    TauCetiReview.git
    sources/
      <16 lowercase hex>.git
  errors/
    recording-errors.jsonl
```

`format.json` is:

```json
{
  "capture_schema": "tauceti.record.capture/v1",
  "schema": "tauceti.record.store/v1"
}
```

The Git directories are shared bare repositories. Per-capture retained refs
keep referenced commits reachable even if an upstream branch or pull request is
later deleted. Non-Git values are deduplicated in the shared SHA-256 blob store.

`errors/recording-errors.jsonl` is an operational log, not benchmark input. Do
not interpret an error line as a capture and do not expose it to an evaluated
agent.

## Discover complete captures

An importer must enumerate immediate children of `captures/` and accept a
directory only when all of these conditions hold:

1. its name matches `tc-[0-9a-f]{24}`;
2. `capture.json` is a regular file containing valid JSON;
3. `COMPLETE` is a regular, zero-byte file;
4. `capture.json.capture_id` equals the directory name; and
5. the validation procedure below succeeds.

Ignore hidden temporary directories and directories without `COMPLETE`. The
recorder writes both files in a temporary directory and atomically renames that
directory into place. Complete captures are immutable. Re-recording identical
task-defining inputs reuses the same capture ID.

Consumers should reject unknown store or capture schemas. A future schema may
change identity fields, normalization, limits, or extraction semantics.

Operators can run the importer-grade validation locally:

```bash
tauceti records validate /var/lib/tauceti/records
```

The command verifies complete manifests, identities, blobs, connected Git
history, dependency pins, and a fresh fetch/checkout of every retained ref.

## Blob references

Every non-Git payload in a manifest is represented by:

```json
{
  "algorithm": "sha256",
  "digest": "<64 lowercase hex characters>",
  "media_type": "application/json",
  "size_bytes": 1234
}
```

Resolve a reference `ref` as:

```text
<record-root>/blobs/sha256/ref.digest[0:2]/ref.digest[2:]
```

Before decoding or copying a blob, verify that the file length equals
`size_bytes` and its SHA-256 digest equals `digest`. JSON blobs use this single
canonical encoding:

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

The following Python is sufficient to load any blob safely after separately
validating the manifest schema:

```python
from hashlib import sha256
from pathlib import Path

def read_blob(record_root: Path, ref: dict) -> bytes:
    if ref.get("algorithm") != "sha256":
        raise ValueError("unsupported blob algorithm")
    digest = ref.get("digest", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid blob digest")
    path = record_root / "blobs" / "sha256" / digest[:2] / digest[2:]
    data = path.read_bytes()
    if len(data) != ref.get("size_bytes") or sha256(data).hexdigest() != digest:
        raise ValueError("missing or corrupt blob")
    return data
```

Never use `media_type` to skip integrity verification. Current values are
`application/json`, `application/octet-stream`, `text/plain; charset=utf-8`,
and `text/markdown; charset=utf-8`.

## Common capture manifest

Every `capture.json` has these top-level fields:

```json
{
  "schema": "tauceti.record.capture/v1",
  "capture_id": "tc-...",
  "phase": "fix",
  "capture_type": "fix-review-raw",
  "fidelity": "materialized-context",
  "purpose": "benchmark-corpus",
  "captured_at": "2026-08-26T12:34:56Z",
  "provenance": {
    "worker_repository": "https://github.com/kim-em/TauCetiWorker",
    "worker_sha": "<Git commit>",
    "recorder_version": "1",
    "worker_name": "<operational worker ID>"
  },
  "candidate": {},
  "repositories": {},
  "dependency_state": {},
  "context": {},
  "prompt": {},
  "integrity": {}
}
```

The phase-specific fields are documented below. `captured_at` and
`provenance.worker_name` are descriptive provenance and are intentionally not
part of capture identity.

### Dependency state

`dependency_state` contains:

```json
{
  "lean_toolchain": { "algorithm": "sha256", "digest": "...", "media_type": "text/plain; charset=utf-8", "size_bytes": 27 },
  "lake_manifest": { "algorithm": "sha256", "digest": "...", "media_type": "application/json", "size_bytes": 12345 },
  "mathlib_rev": "<resolved revision, when present>"
}
```

The first two values are exact file bytes read from the captured TauCeti commit.
`mathlib_rev`, when present, is the `rev`, `commit`, or `gitRevision` value from
the package named `mathlib` in `lake-manifest.json`.

For defense in depth, an importer should verify that the two blobs equal the
outputs of:

```bash
git --git-dir <record-root>/git/TauCeti.git show <tauceti-sha>:lean-toolchain
git --git-dir <record-root>/git/TauCeti.git show <tauceti-sha>:lake-manifest.json
```

Use these pins to construct the evaluation environment. Dependency package
contents and caches are not stored, so TauCetiBench must provision them from a
pinned, trusted cache or image before disabling network access.

### Prompt state

`prompt` contains `source_path` plus three blob references:

- `template`: exact bytes of the Worker prompt template;
- `rendered_base`: UTF-8 prompt text rendered with treatment-neutral values;
- `logical_inputs`: canonical JSON containing those substitutions.

The neutral values remove the live model/provider name, sandbox-specific paths,
worker identity, and contributor fork. They retain the task framing and task
selection inputs.

For `fix-review-raw`, `logical_inputs` is:

```json
{
  "AGENT": "TauCetiWorker",
  "BIN": "scripts",
  "PR": 123
}
```

For `roadmap-opportunity-raw`, it contains `ONLY`, `SKIP`, `CLAIMED`,
`SOURCE_GUIDANCE`, and these stable harness-facing values:

```json
{
  "AGENT": "TauCetiWorker",
  "BIN": "scripts",
  "FORK": "fork-owner",
  "ROADMAP_DIR": "context/roadmap/TauCetiRoadmap",
  "REVIEW_DIR": "context/review",
  "RUBRICS": "context/review/rubrics.md",
  "WORKERID": "worker"
}
```

When a supplementary source is present, `SOURCE_GUIDANCE` names
`context/source` and describes it as read-only. The captured JSON blob is
authoritative; the examples above explain the normalization and are not a
second source from which consumers should regenerate the prompt.

TauCetiBench has two valid prompt policies, and must record which it uses:

1. Use `rendered_base` to evaluate the historical Worker task framing.
2. Use a separately versioned, arm-invariant TauCetiBench instruction and retain
   all three captured prompt values only as provenance.

Do not combine the captured base with provider-specific instructions from the
original run: those instructions were intentionally not recorded. Do not expose
different subsets of captured context to different evaluation arms unless that
difference is the treatment being measured.

### Integrity state

The manifest records:

```json
{
  "git_objects_verified": true,
  "retained_refs_verified": true,
  "context_blobs_verified": true,
  "prompt_blobs_verified": true,
  "secret_scan_passed": true,
  "canonical_identity_sha256": "<64 lowercase hex>"
}
```

These values describe checks performed at capture time. They do not replace
import-time verification. Require each Boolean to be `true`, then independently
verify all blobs, Git objects, retained refs, and canonical identity.

## `fix-review-raw`

A fix capture represents a selected pull request at its exact head commit,
together with the base and review state available before the agent began.

Its candidate is:

```json
{
  "repository": "TauCetiProject/TauCeti",
  "pr": 123,
  "expected_head_sha": "<commit>"
}
```

### Git repositories

`repositories.tauceti` has this shape:

```json
{
  "pr_head_sha": "<commit to check out>",
  "head_retained_ref": "refs/tauceti-record/tc-.../head",
  "base": {
    "branch": "main",
    "sha": "<base commit observed at capture time>",
    "retained_ref": "refs/tauceti-record/tc-.../base"
  },
  "merge_base_sha": "<merge base of head and base>"
}
```

The object store is `git/TauCeti.git`. A fix capture may also contain
`repositories.roadmap` and `repositories.review`, each with `sha` and
`retained_ref`; their stores are `git/TauCetiRoadmap.git` and
`git/TauCetiReview.git` respectively.

Validate every declared pair using both commands:

```bash
git --git-dir <store> cat-file -e '<sha>^{commit}'
git --git-dir <store> rev-parse '<retained-ref>^{commit}'
```

The second command must print exactly the declared SHA. Also require:

```bash
git --git-dir <record-root>/git/TauCeti.git merge-base <head-sha> <base-sha>
```

to equal `merge_base_sha`.

### Context blobs

`context` maps the following names to JSON blob references:

| Name | Contents |
| --- | --- |
| `pr` | PR number, title, body, URL, author, base/head branch and SHA, sorted labels, and state. |
| `issue_comments` | PR conversation comments in deterministic order. |
| `submitted_reviews` | Submitted review summaries/comments available from GitHub's reviews endpoint. |
| `review_threads` | Review threads, resolution/outdated status, location, and comments. |
| `scoreboard` | The last issue comment containing `<!--tauceti-scoreboard-->`; omitted when absent. |
| `unresolved_findings` | The deterministic subset of review threads selected for repair. |
| `survey_metadata` | Worker's selection reason plus head branch and repository. |

Comment objects use an allowlist. A field is omitted when GitHub did not provide
a non-null value. Possible fields are `id`, `body`, `created_at`, `updated_at`,
`path`, `line`, `original_line`, `side`, `start_line`, `start_side`,
`in_reply_to_id`, `commit_id`, `original_commit_id`, `author_association`,
`state`, `submitted_at`, and `author`.

`review_threads` normally contains objects of this form:

```json
{
  "is_resolved": false,
  "is_outdated": false,
  "path": "TauCeti/Foo.lean",
  "line": 42,
  "start_line": 40,
  "diff_side": "RIGHT",
  "comments": [ ... ]
}
```

The materializer rejects more than 100 threads or more than 100 comments in one
thread rather than silently truncating. A compatibility fallback may emit
`is_resolved: null` and omit some location/status fields. `null` means unknown,
not resolved.

`unresolved_findings` has schema-by-algorithm:

```json
{
  "algorithm": "tauceti.record.unresolved-findings/v1",
  "findings": [ ...review thread objects... ]
}
```

Version 1 includes a thread when `is_resolved` is not `true`, it has at least one
comment, and the root comment contains either `tauceti-rubric:` or the
case-insensitive word fragment `request`. This is the authoritative recorded
repair set. TauCetiBench should not recompute it with a newer algorithm.

### Fix capture identity

Reconstruct this JSON object using the manifest and the digests of its blob
references; omit optional `roadmap` and `review` Git keys when those repositories
are absent:

```json
{
  "schema": "tauceti.record.capture/v1",
  "phase": "fix",
  "capture_type": "fix-review-raw",
  "candidate": {
    "repository": "TauCetiProject/TauCeti",
    "pr": 123
  },
  "git": {
    "head": "<repositories.tauceti.pr_head_sha>",
    "base_branch": "<repositories.tauceti.base.branch>",
    "base": "<repositories.tauceti.base.sha>",
    "roadmap": "<repositories.roadmap.sha, if present>",
    "review": "<repositories.review.sha, if present>"
  },
  "context": {
    "pr": "<context.pr.digest>",
    "issue_comments": "<context.issue_comments.digest>",
    "submitted_reviews": "<context.submitted_reviews.digest>",
    "review_threads": "<context.review_threads.digest>",
    "scoreboard": "<context.scoreboard.digest, if present>",
    "unresolved_findings": "<context.unresolved_findings.digest>",
    "survey_metadata": "<context.survey_metadata.digest>"
  },
  "prompt": {
    "template": "<prompt.template.digest>",
    "rendered_base": "<prompt.rendered_base.digest>",
    "logical_inputs": "<prompt.logical_inputs.digest>"
  }
}
```

Canonicalize it with the JSON encoding above. Its SHA-256 must equal
`integrity.canonical_identity_sha256`, and the capture ID must equal `tc-`
followed by the first 24 characters of that digest.

### Materialize a fix evaluation

Use the captured head, not a current PR ref:

```bash
git init -q <task-dir>/repo
git -C <task-dir>/repo fetch --no-tags <record-root>/git/TauCeti.git \
  <repositories.tauceti.head_retained_ref> \
  <repositories.tauceti.base.retained_ref>
git -C <task-dir>/repo update-ref refs/tauceti-eval/head <repositories.tauceti.pr_head_sha>
git -C <task-dir>/repo update-ref refs/tauceti-eval/base <repositories.tauceti.base.sha>
git -C <task-dir>/repo checkout --detach <repositories.tauceti.pr_head_sha>
```

If the agent requires a writable branch, create a deterministic local branch
from the same head after checkout. The one-shot fetch does not configure an
origin. Do not fetch the PR again.

Decode the context blobs into a read-only directory with stable names, for
example:

```text
<task-dir>/context/pr.json
<task-dir>/context/issue_comments.json
<task-dir>/context/submitted_reviews.json
<task-dir>/context/review_threads.json
<task-dir>/context/scoreboard.json             # only when declared
<task-dir>/context/unresolved_findings.json
<task-dir>/context/survey_metadata.json
```

Auxiliary Roadmap and Review repositories can be initialized, fetched from
their retained refs, and detached at their declared SHAs if the chosen task
instruction needs them. The minimum direct fix task is the TauCeti checkout
plus `pr`, `review_threads`, and
`unresolved_findings`; use one consistent exposure policy across eval arms.

The evaluation instruction should identify the captured PR number and tell the
agent to repair the supplied unresolved findings in the checked-out head. Agent
changes are measured relative to the captured head; the total PR diff is
measured from `repositories.tauceti.merge_base_sha`. Do not substitute a later
upstream branch. Any hidden tests, expected patch, or later review outcome
belongs in a quarantined TauCetiBench evaluator and must not be mounted into the
task.

## `roadmap-opportunity-raw`

A roadmap capture is an opportunity snapshot: it records everything needed to
understand what work was available, blocked, duplicated, or recently completed
when the Worker started. It does not identify the exact implementation the live
agent eventually chose.

Its candidate is:

```json
{
  "repository": "TauCetiProject/TauCeti",
  "roadmap_area": "Algebra"
}
```

### Git repositories

The required entries are:

- `repositories.tauceti`, stored in `git/TauCeti.git`;
- `repositories.roadmap`, stored in `git/TauCetiRoadmap.git`; and
- `repositories.review`, stored in `git/TauCetiReview.git`.

Each has `sha` and `retained_ref`. The TauCeti retained ref ends in `/main`, and
the others end in `/roadmap` and `/review`.

An optional `repositories.source` has:

```json
{
  "identity": { "kind": "git-url", "url": "https://..." },
  "sha": "<commit>",
  "retained_ref": "refs/tauceti-record/tc-.../source-<16 hex>",
  "store": "sources/<16 hex>.git"
}
```

For a local source without a portable remote, `identity` instead has
`{"kind":"local-git","name":"<basename>"}`. Resolve its object store relative
to `<record-root>/git/`; reject absolute paths and `..` components.

### Roadmap context

`context.roadmap` points to one JSON object:

```json
{
  "schema": "tauceti.record.roadmap-context/v1",
  "designated_area": "Algebra",
  "skip_targets": ["..."],
  "claims_and_holds": "...",
  "intentions": [ ... ],
  "roadmap_files": {
    "TauCetiRoadmap/Algebra/README.md": "<decoded text>"
  },
  "open_prs": { "limit": 100, "items": [ ... ] },
  "recently_merged_prs": { "limit": 50, "items": [ ... ] },
  "survey_metadata": {
    "candidate_reason": "...",
    "open_pr_count": 4
  },
  "rubric_bundle": "<text or null>",
  "source_repository": { "kind": "git-url", "url": "https://..." }
}
```

`source_repository` is omitted without a supplementary source.

`roadmap_files` contains every `.md` and `.lean` file below the staged
`TauCetiRoadmap/` directory in path order. Version 1 limits each file to 1 MB and
the total to 5 MB. The corresponding Git commit remains authoritative; the text
snapshot lets a curator inspect the opportunity without checking out the repo.

`intentions` contains up to 200 open issues bearing `intention` and, for a
pinned area, `roadmap/<area>`. Each entry contains number, URL, title, body,
sorted assignees, sorted labels, and state. `claims_and_holds` is the exact
avoidance text placed into the neutral prompt: binding administrative holds in
scope, followed when applicable by ordinary foreign claims.

Open and recently merged PR items contain number, title, body, URL, author, head
SHA, base branch, sorted labels, and `merged_at` when available. The manifest
records the fixed 100/50 limits. Consumers must not supplement these snapshots
with present-day GitHub data when reconstructing the historical task.

`rubric_bundle` is the staged combined review rubric when it was available,
otherwise `null`. The exact Review repository is also retained.

### Roadmap capture identity

Reconstruct:

```json
{
  "schema": "tauceti.record.capture/v1",
  "phase": "roadmap",
  "capture_type": "roadmap-opportunity-raw",
  "area": "<candidate.roadmap_area>",
  "git": {
    "main": "<repositories.tauceti.sha>",
    "roadmap": "<repositories.roadmap.sha>",
    "review": "<repositories.review.sha>",
    "source": "<repositories.source.sha, if present>"
  },
  "context": {
    "roadmap": "<context.roadmap.digest>"
  },
  "prompt": {
    "template": "<prompt.template.digest>",
    "rendered_base": "<prompt.rendered_base.digest>",
    "logical_inputs": "<prompt.logical_inputs.digest>"
  }
}
```

Canonicalize and validate the digest and `tc-` prefix exactly as for fix
captures.

### Curate and materialize a roadmap evaluation

Do not pass a raw roadmap capture directly to a benchmark that claims to measure
implementation of an exact task. First perform a curation step:

1. Inspect the captured roadmap files, designated area, intentions, holds,
   open PRs, recently merged PRs, rubric, and exact TauCeti tree.
2. Select one concrete, bounded target that was genuinely open and not blocked
   at capture time.
3. Write a versioned, treatment-neutral task instruction with objective
   acceptance criteria.
4. Store the mapping from that task definition to `capture_id` in TauCetiBench.
5. Keep any source PR, later implementation, expected patch, and hidden tests in
   the evaluator quarantine, outside the agent-visible filesystem.

If target-selection ability itself is what the benchmark measures, the raw
opportunity and captured `rendered_base` may be used directly, but the benchmark
must label the task as open-ended and score selection separately from
implementation.

Materialize the captured repositories without network access:

```bash
git init -q <task-dir>/repo
git -C <task-dir>/repo fetch --no-tags <record-root>/git/TauCeti.git \
  <repositories.tauceti.retained_ref>
git -C <task-dir>/repo checkout --detach <repositories.tauceti.sha>

git init -q <task-dir>/context/roadmap
git -C <task-dir>/context/roadmap fetch --no-tags \
  <record-root>/git/TauCetiRoadmap.git <repositories.roadmap.retained_ref>
git -C <task-dir>/context/roadmap checkout --detach <repositories.roadmap.sha>

git init -q <task-dir>/context/review
git -C <task-dir>/context/review fetch --no-tags \
  <record-root>/git/TauCetiReview.git <repositories.review.retained_ref>
git -C <task-dir>/context/review checkout --detach <repositories.review.sha>
```

Initialize the optional source repository in `<task-dir>/context/source`, fetch
its declared retained ref from its declared store, and detach at its declared
SHA. These commands use one-shot local fetches and therefore do not configure an
origin. Expose source and context repositories read-only if the runner supports
it. The main TauCeti checkout is the only writable repository.

Also decode `context.roadmap` to a stable path such as
`<task-dir>/context/roadmap-context.json`. The checked-out repositories are the
source of truth for files; the JSON supplies the historical GitHub snapshot and
selection metadata.

## Required importer validation

Before admitting a capture to a corpus, TauCetiBench should perform all of the
following checks:

1. Validate `format.json` and reject unknown schema versions.
2. Apply the complete-capture discovery rules.
3. Require the declared `phase`/`capture_type` pair to be either
   `fix`/`fix-review-raw` or `roadmap`/`roadmap-opportunity-raw`.
4. Require `fidelity == "materialized-context"` and
   `purpose == "benchmark-corpus"`.
5. Walk the entire manifest, find every object with
   `algorithm == "sha256"` and a `digest`, and verify its blob.
6. Validate every Git store path, commit object, and retained ref. Resolve
   source stores only below `<record-root>/git/`.
7. For a fix capture, validate the merge base. For both capture types, compare
   dependency blobs with the files at the captured TauCeti SHA.
8. Reconstruct the phase-specific identity, canonicalize it, verify the full
   identity digest, and derive the capture ID.
9. Require every capture-time integrity Boolean to be `true`.
10. Decode JSON blobs strictly as UTF-8 JSON. Check the roadmap-context and
    unresolved-findings algorithm/schema strings before using their contents.
11. Copy or fetch only verified content into a fresh task directory. Ensure no
    object-store remote is configured and do not mount the record root into the
    agent sandbox.
12. Record exactly which blobs, repositories, prompt policy, and curated task
    definition were exposed for that eval.

Validation must finish before any blob content is interpreted as instructions
or any captured repository code is executed.

## Security and trust

Capture context and supplementary repositories are untrusted public input. PR
bodies, comments, roadmap prose, and source files may contain prompt injection.
Treat them as task data, not harness instructions. A benchmark runner should:

- parse only the documented manifest fields;
- keep evaluator secrets and hidden references outside the agent sandbox;
- disable network access or provide a deterministic facade;
- avoid executing setup scripts from supplementary sources;
- ignore agent-configuration files in supplementary sources; and
- re-scan a corpus for credentials before distributing it.

The recorder uses an allowlist and rejects known secret-shaped keys and values,
but that scan is defense in depth rather than a proof that a corpus contains no
sensitive data.

## Recording failures

Failures append canonical JSON lines with schema `tauceti.record.error/v1` to
`errors/recording-errors.jsonl`. Current stable codes include:

- `storage_failed`
- `identity_collision`
- `validation_failed`
- `git_object_missing`
- `git_fetch_failed`
- `retained_ref_failed`
- `prompt_capture_failed`
- `secret_scan_failed`
- `input_raced`
- `github_read_failed`
- `context_normalization_failed`
- `unsupported_source`

The log is useful for store operations and coverage accounting. It never makes
an incomplete directory importable and must not be treated as task context.

## Minimal TauCetiBench run record

For each attempted evaluation, retain at least:

```json
{
  "capture_id": "tc-...",
  "capture_identity_sha256": "...",
  "task_definition_id": "<versioned TauCetiBench task>",
  "capture_type": "fix-review-raw",
  "starting_commit": "...",
  "base_commit": "...",
  "exposed_blob_digests": ["..."],
  "exposed_repository_commits": {"tauceti": "..."},
  "prompt_policy": "captured-rendered-base",
  "harness_revision": "...",
  "container_image_digest": "...",
  "model": "...",
  "tool_policy": "...",
  "network_policy": "disabled",
  "resource_limits": {},
  "seed": 0
}
```

This run record, together with a validated record store and any separately
versioned hidden evaluator assets, defines the reproducible evaluation. The raw
capture alone does not.
