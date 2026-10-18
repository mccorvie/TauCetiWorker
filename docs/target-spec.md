# The target spec

`tauceti.target-spec/v1` is the handoff between choosing a roadmap target and authoring it. The
fleet's `prompts/roadmap.md` does both in one agent session, so the only record of what the agent
decided to build is the PR it eventually opens. The split prompts make the decision an artifact:

- `prompts/roadmap-select.md` reads the roadmap, the intentions, the open and merged PRs and the
  rubrics, and writes a spec to `__TARGET_SPEC_OUT__`. It claims nothing, writes no Lean and runs no
  build.
- `prompts/roadmap-author.md` receives a spec inline as `__TARGET_SPEC__` and is otherwise the fleet
  prompt from "Claim your target" onward: it claims the spec's slug, authors, builds, pushes and
  opens the PR with the spec's marker in the body.

TauCetiBench runs the two halves as separate tasks to measure them separately (the fleet's own
captures hold nothing of the agent's choice). A future `do_roadmap --split` can run them in sequence
in the fleet with a cheaper selection agent and record the spec with the capture.

## Format

A Markdown document of about 1–4 KB:

```
<!--tauceti-target:v1 {"focus":"<target-roadmap>","id":"<slug>"}-->
Roadmap: <target-roadmap>
Consumer roadmap: <designated-roadmap>

## Milestone
## Target
## Planned declarations
## Reuse
## Placement
## Remaining
## Avoid
```

- **Line 1** is the target marker, exactly as a PR body carries it, so `survey.TARGET_MARKER_RE`
  validates specs and PR bodies alike and the slug the author claims is the slug the planner chose.
  `focus` is a canonical top-level directory of TauCetiRoadmap (never `any`/`auto`); `id` is the
  slug rule from the roadmap prompt: lowercase, every run of non-alphanumerics replaced by one `-`
  (`target_spec.slugify`). Other keys (fleet PRs have carried `"partial":true`) are kept as
  `marker_extra` and written back, never interpreted.
- **Line 2** is `Roadmap: <target-roadmap>` and must equal `focus`.
- **`Consumer roadmap:`** appears only after a prerequisite switch and names the roadmap the planner
  was assigned; it must differ from the target roadmap.
- **Sections** are the seven `## ` headings above, in that order, each with a non-empty body and no
  other headings anywhere. Milestone: the README milestone served, quoted (and after a switch, the
  consumer milestone and the dependency edge). Target: 2–5 sentences in planning voice. Planned
  declarations: one bullet per intended declaration with its informal statement. Reuse: Mathlib and
  TauCeti declarations to build on and what a grep ruled out. Placement: the module path under
  `TauCeti/`. Remaining: what the milestone still needs after this PR. Avoid: the PRs and intentions
  judged overlapping and why this target is distinct.

The parser (`tauceti_worker/target_spec.py`) is strict on structure and silent on content:
`parse_spec(text)` returns a `Spec` (`focus`, `id`, `consumer_roadmap`, `sections`) or raises
`SpecError` with a message fit for an agent's report; `validate_spec(text)` returns that message or
`None`; `Spec.render()` is the canonical text form and round-trips. The author prompt treats the
spec's names and placement as a brief to verify, not a contract, and reports where it departed.
