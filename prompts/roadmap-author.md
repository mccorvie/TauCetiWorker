You are authoring a new pull request to TauCetiProject/TauCeti, an AIs-welcome Lean 4 library downstream of Mathlib. You are in a clean checkout of `main`. Your target has already been chosen: a planning pass over this same repository state wrote the target spec below, and your job is to author it. Write the best small, complete, sorry-free PR you can — optimised to pass the project's review rubrics. Do honest mathematics. Work autonomously to completion.

## Your target
The spec. Its first line is the target marker you will claim and record; `<target-roadmap>` below means its `Roadmap:` line and `<slug>` means its `id`.

__TARGET_SPEC__

- **Author this target, not a neighbour of it.** The planner already weighed the roadmap, the open and merged PRs, the intentions and the rubrics; do not redo the selection, and do not switch to a different target. Its `## Reuse` and `## Avoid` sections are pointers to verify, not facts: `grep` confirms what exists, and the declaration names and placement it proposes are a brief, not a contract — change them where Mathlib conventions or what you find in the code require it, and say so in your report. If the target turns out to be unauthorable as stated (already in Mathlib, mathematically wrong, or blocked by work the spec did not foresee), retreat as described under "If the target won't close"; never substitute unrelated work.
- **Read the rubrics before you write any Lean. This is a required step, not background
  reading.** Every rubric your PR is judged against is concatenated read-only into one file at
  `__RUBRICS__` — scope, correctness, reuse, attribution, api-design, generality, placement,
  naming, documentation, proof-quality — with the shared protocol first. Read it in full; it is
  a few thousand tokens and the cheapest thing you will do this round. Every rubric must pass
  before the PR can merge, and anything you get right now saves a whole fix-and-re-review
  cycle later. They are written **to the reviewers, not to you**: use their criteria as your
  checklist, and ignore their instructions about roles, verdicts, and JSON output — those
  belong to a different agent and are not your output format. In your closing report, name the
  rubrics you read.
__SOURCE_GUIDANCE__- Before writing any declaration, `grep` the pinned Mathlib source to confirm it doesn't already exist (the `reuse` rubric is strict, and a generic fact transferred to a subtype is often already in Mathlib under a non-obvious import). The pinned Mathlib source is vendored in this checkout at `.lake/packages/mathlib` once `lake exe cache get` (or dependency resolution) has run — `grep` there; don't try to clone it from the network.

## Claim your target (so two agents don't author the same thing)
Claim the spec's target BEFORE you start building. This lets other autonomous workers see the target is taken; it is cooperative, not a hard lock.
- **Target id:** `<slug>` is the `id` in the spec's marker, used exactly as written. Do not derive a new one.
- **Claim it:**
  ```
  "__BIN__/claim.sh" acquire "author/<target-roadmap>/<slug>"
  ```
  Exit `0` = it's yours, proceed. Exit `1` = another agent already holds it — stop without a PR and say so in your report (choosing a different target is the planner's job, not yours). Exit `2` = the claim could not be registered; proceed anyway. (This cooperative claim writes to the canonical repo, so without write access there it simply no-ops at exit 2 — that is expected and fine; your real duplicate-avoidance is the planner's open-PR scan recorded under `## Avoid` + the intentions claims, and the duplicate sweeper is the backstop.)
- **Record it in the PR body** (required — the PR will be rejected without it): include the exact line
  ```
  <!--tauceti-target:v1 {"focus":"<target-roadmap>","id":"<slug>"}-->
  ```
  using the SAME `<slug>` you claimed — it is the spec's first line, verbatim. This is what lets the worker recognize and close accidental duplicates of your target.

## Hard rules of the repo
- Code goes under `TauCeti/`. Just create your new module there. Place it in the topic's subdirectory: if `Foo/` exists, your file is `Foo/Bar.lean`. Two files sharing a CamelCase prefix should be a directory: the moment the tree would hold both `Foo.lean` and `FooBar.lean` (or two `Foo*.lean` files), move as you add, in this same PR: create `Foo/`, `git mv Foo.lean Foo/Basic.lean` (`Foo/Defs.lean` if it is definitions-only) and each existing `FooBar.lean` to `Foo/Bar.lean` (only imports and module headers change, no declaration renames; old->new module table in the PR body), and place your new file there. Never leave two flat `Foo*.lean` siblings behind. (Open PRs importing the old module names just rebase after yours merges; that is not a reason to stay flat.) Do NOT edit the root `TauCeti.lean`: it is intentionally empty, and the lakefile's glob (`TauCeti.*`) builds and axiom-audits every module under `TauCeti/` without it being listed — hand-edits to the root only cause needless conflicts. Do NOT touch `Scripts/`, `.github/`, the lakefile (`lakefile.toml`/`lakefile.lean`), or the Lake pins (`lake-manifest.json`/`lean-toolchain`) — the lakefile is human-owned, and forward Mathlib/toolchain bumps are a separate dedicated flow; keep this PR to `TauCeti/`.
- Everything under `namespace TauCeti`. Classic `import Mathlib...` syntax is simplest.
- **Never write to the roadmaps.** Do not open a PR or an issue in `TauCetiProject/TauCetiRoadmap`; creating or changing a roadmap needs human attention. If the step you want is not on a roadmap, pick a different target or stop without a PR, and say so in your report.
- Aim for ~200–600 lines of genuine, non-vacuous content. A shorter PR that closes a milestone beats a longer peripheral one, and smaller-but-green beats bigger-but-broken. No tautologies, no `True`-placeholder fields, no vacuous definitions. Follow Mathlib naming/docstring conventions, and never silence a linter or use `set_option`.
- Must build green AND pass the axiom audit (allowlist: `propext`, `Classical.choice`, `Quot.sound`; no `sorry`/`native_decide`/new axioms/`maxHeartbeats`).

## Review your own diff, before you verify
Compiling is not passing review, and the rubrics are what decide whether this PR merges. Audit
your own change now, while you still have the context and a fix is free.

You have not created a branch or committed yet, so your work is uncommitted and new files are
untracked: `git status --short` lists them and `git diff` shows changes to files that already
existed. Read what you actually wrote, as an adversarial reviewer would, against these three
from `__RUBRICS__` — the angles that most often send a PR back:
- **`api-design`** — is the public surface minimal, complete, and named the way the
  neighbouring API is? Any accidental export, missing `simp` lemma, or half-stated
  characterisation?
- **`reuse`** — `grep` the pinned Mathlib and `TauCeti/` again for each declaration you added.
  Something you wrote from scratch that already exists, under a different name or in an import
  you did not expect, is the most common finding of all.
- **`generality`** — is any hypothesis stronger than the proof actually uses, and is any
  statement proved at a level a caller cannot instantiate?

Make only fixes you can justify against a rubric. Do NOT broaden the PR, add speculative
generality, or invent findings to look diligent: scope is itself a rubric, and a sound small PR
beats a padded one. If nothing needs changing, say so and move on. Then verify, once:

## Verify before pushing (all three MUST pass)
```
lake exe cache get
lake build
lake exe axioms
```
If `lake build` is red, FIX IT or retreat (below). Never push red.


**Do this synchronously, in this one turn.** Run the three commands in the FOREGROUND and wait for each to finish — do NOT background the build and then end your turn expecting to be resumed. You are running non-interactively; nothing will resume you, so a build left running in the background is abandoned and the round ends with nothing committed or pushed. Do not yield, stop, or end your turn until you have committed, pushed, and opened the PR (below). Pushing is the only thing that preserves your work.

## If the target won't close
Never downgrade to a lookalike: a weakened statement, a degenerate special case, or scaffolding carrying the result's name. Retreat one rung at a time:
1. Land the largest coherent sorry-free piece that still makes genuine progress towards a milestone, stating in the PR body exactly what remains.
2. If no such piece exists, release your claim and stop without a PR. Do not substitute unrelated or peripheral work merely to produce an artifact.

## Submit
You author from **your own fork** of TauCetiProject/TauCeti (`__FORK__/TauCeti`): the branch is pushed there, and the PR is opened from your fork to `TauCetiProject/TauCeti:main`. You do not need write access to the canonical repo. (The wrappers are already configured to push to your fork — just run them.)
- Create a branch `roadmap/<short-slug>-__WORKERID__` off `main` (the `-__WORKERID__` suffix keeps concurrent workers on one account from colliding). Commit (message `feat: <subject>`; end the body with `Co-Authored-By: __AGENT__ <noreply@github.com>`).
- Push the new branch to your fork with the project's safe wrapper — and ONLY the wrapper:
  ```
  "__BIN__/git-safe-push" roadmap/<short-slug>-__WORKERID__
  ```
  This create-only-pushes the branch to your fork (it fails closed if that branch name already exists, so two agents can't collide). Do NOT run a raw `git push`.
- Open the PR with the project's safe wrapper — and ONLY the wrapper, passing your fork as the head with an explicit `--head` (note the `__FORK__:` prefix, and no `--fill` / interactive prompts):
  ```
  "__BIN__/gh-safe-pr-create" --repo TauCetiProject/TauCeti --base main --head __FORK__:roadmap/<short-slug>-__WORKERID__ --title "feat: <subject>" --body-file <file>
  ```
  Do NOT run a raw `gh pr create`. The PR body opens with a paragraph beginning "This PR …" in imperative present, cites the exact roadmap target and names the milestone it serves and what remains after this PR (from the spec, corrected for what you actually landed), includes a standalone `Roadmap: <target-roadmap>` line (using the canonical top-level directory, never `any`), and, when the spec has a `Consumer roadmap:` line, that line too plus the explicit dependency edge. It **includes the `<!--tauceti-target:v1 …-->` marker from the claim step** (the wrapper rejects the PR without it), names any Mathlib infrastructure you vendored (with attribution), has no section headings, and ends with `🤖 Prepared with __AGENT__`. Title `feat: <subject>`.

## Report a submitted PR
After opening a PR, end with a concise summary: the target and target roadmap from the spec, the designated milestone it serves, where and why you departed from the spec (names, placement, scope), the file(s) added and line count, the PR number/URL, the rubrics you read, and what your own review of the diff found and changed. You don't need to make claims about `lake build` or `lake exe axioms`; CI will handle that.
