#!/usr/bin/env python3
"""The target spec is the handoff between the roadmap-select and roadmap-author prompts. The bench
and a future split worker both validate it with `target_spec.parse_spec`, so this pins the format:
the marker line shared with PR bodies, the Roadmap line, the fixed section set and order, the
round-trip through `Spec.render`, and that both split prompts render with their placeholder sets.

Exit 0 = every case agrees; 1 = a mismatch.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker import target_spec as ts

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"[{'OK ' if ok else 'XX '}] {name}: got {got!r} want {want!r}")


def error_of(text):
    try:
        ts.parse_spec(text)
    except ts.SpecError as e:
        return str(e)
    return None


GOOD = """\
<!--tauceti-target:v1 {"focus":"Multiquadratic","id":"galois-group-multiquadratic-z2n"}-->
Roadmap: Multiquadratic

## Milestone
"M2: the Galois group of a multiquadratic extension."

## Target
Prove that the Galois group is an elementary abelian 2-group.
Two sentences are enough here.

## Planned declarations
- `TauCeti.Multiquadratic.galoisGroup_isElementaryAbelian`: every element has order dividing 2.

## Reuse
`Mathlib.FieldTheory.Galois.Basic`; grep found no `multiquadratic` in Mathlib.

## Placement
`TauCeti/Multiquadratic/Galois.lean`

## Remaining
M2 then needs the cardinality computation.

## Avoid
#6012 covers the degree formula only; distinct because this is the group structure.
"""

spec = ts.parse_spec(GOOD)
check("focus", spec.focus, "Multiquadratic")
check("id", spec.id, "galois-group-multiquadratic-z2n")
check("no consumer", spec.consumer_roadmap, None)
check("section set", tuple(spec.sections), ts.REQUIRED_SECTIONS)
check("target body kept", spec.sections["Target"].startswith("Prove that"), True)
check("round trip", ts.parse_spec(spec.render()), spec)
check("render starts with marker", spec.render().splitlines()[0], spec.marker)
check("marker regex is the survey's", bool(tc.TARGET_MARKER_RE.fullmatch(spec.marker)), True)
check("validate_spec ok", ts.validate_spec(GOOD), None)
check("leading blank lines tolerated", error_of("\n\n" + GOOD), None)

# Extra marker keys (fleet PRs carry `"partial":true`) are kept and round-tripped, never rejected.
partial = GOOD.replace(
    '"id":"galois-group-multiquadratic-z2n"}', '"id":"galois-group-multiquadratic-z2n","partial":true}'
)
check("extra marker key kept", ts.parse_spec(partial).marker_extra, {"partial": True})
check("extra marker key round trips", ts.parse_spec(partial).render().splitlines()[0], partial.splitlines()[0])
check(
    "marker_line with extra",
    ts.marker_line("A", "b", {"partial": True}),
    '<!--tauceti-target:v1 {"focus":"A","id":"b","partial":true}-->',
)

# A prerequisite switch records the consumer roadmap.
switched = GOOD.replace("Roadmap: Multiquadratic\n", "Roadmap: Multiquadratic\nConsumer roadmap: ClassField\n")
check("consumer roadmap", ts.parse_spec(switched).consumer_roadmap, "ClassField")
check("consumer round trip", ts.parse_spec(ts.parse_spec(switched).render()).consumer_roadmap, "ClassField")

# Structural errors, each named so an agent's report says what to fix.
bad = {
    "empty": ("", "empty spec"),
    "no marker": (GOOD.replace("<!--tauceti-target:v1 ", "<!--target:v1 "), "first line must be"),
    "bad json": (GOOD.replace('"id":"galois', '"id":galois'), "marker JSON does not parse"),
    "focus any": (
        GOOD.replace('"focus":"Multiquadratic"', '"focus":"any"').replace("Roadmap: Multiquadratic", "Roadmap: any"),
        "not a concrete roadmap area",
    ),
    "bad slug": (GOOD.replace('"id":"galois-group-multiquadratic-z2n"', '"id":"Galois Group"'), "is not a slug"),
    "roadmap mismatch": (GOOD.replace("Roadmap: Multiquadratic", "Roadmap: ClassField"), "does not match"),
    "roadmap missing": (GOOD.replace("Roadmap: Multiquadratic\n", ""), "line after the marker"),
    "consumer equals target": (
        GOOD.replace("Roadmap: Multiquadratic\n", "Roadmap: Multiquadratic\nConsumer roadmap: Multiquadratic\n"),
        "must differ",
    ),
    "empty section": (GOOD.replace("`TauCeti/Multiquadratic/Galois.lean`\n", ""), "section 'Placement' is empty"),
    "missing section": (GOOD[: GOOD.index("## Avoid")], "missing section(s): ## Avoid"),
    "unknown section": (GOOD.replace("## Avoid", "## Notes"), "unknown section"),
    "out of order": (GOOD.replace("## Target\n", "## Reuse\n", 1), "out of order"),
    "duplicate": (GOOD.replace("## Remaining\n", "## Target\n"), "duplicate section"),
    "text before sections": (GOOD.replace("\n## Milestone", "stray line\n## Milestone"), "unexpected text"),
}
for name, (text, needle) in bad.items():
    err = error_of(text) or ""
    check(f"rejects {name}", needle in err, True)
    if needle not in err:
        print(f"      error was: {err!r}")

# The slug rule from the prompt.
check(
    "slugify prompt example",
    ts.slugify("the Galois group of a multiquadratic field is (ℤ/2)ⁿ"),
    "the-galois-group-of-a-multiquadratic-field-is-2",
)
check("slugify name", ts.slugify("TauCeti.Foo.bar_baz"), "tauceti-foo-bar-baz")
check("slugify is idempotent", ts.slugify(ts.slugify("A  b--C")), "a-b-c")

# Both split prompts render with their documented placeholder sets (fill_prompt fails on a leftover).
common = dict(
    AGENT="codex",
    FORK="fork-owner",
    WORKERID="w1",
    RUBRICS="/opt/rubrics/rubrics.md",
    SOURCE_GUIDANCE="",
    BIN="/opt/bin",
)
select = tc.agents.fill_prompt(
    REPO / "prompts" / "roadmap-select.md",
    ONLY="Multiquadratic",
    SKIP="none",
    CLAIMED="none",
    ROADMAP_DIR="/opt/roadmap/TauCetiRoadmap",
    RUBRICS=common["RUBRICS"],
    SOURCE_GUIDANCE="",
    TARGET_SPEC_OUT="/work/target-spec.md",
)
check("select prompt names the output path", "/work/target-spec.md" in select, True)
check("select prompt has no claim command", "claim.sh" in select and "acquire" in select, False)
check("select prompt lists every section", all(f"## {s}" in select for s in ts.REQUIRED_SECTIONS), True)
author = tc.agents.fill_prompt(REPO / "prompts" / "roadmap-author.md", TARGET_SPEC=GOOD.rstrip(), **common)
check("author prompt inlines the spec", GOOD.splitlines()[0] in author, True)
check("author prompt keeps the claim step", '"/opt/bin/claim.sh" acquire' in author, True)
check("author prompt keeps the PR wrapper", '"/opt/bin/gh-safe-pr-create"' in author, True)
check("author prompt does not list PRs", "gh pr list" in author, False)
# The tail of the author prompt is the fleet prompt's tail: same section headings in the same order.
fleet = (REPO / "prompts" / "roadmap.md").read_text()
heads = lambda t: [ln for ln in t.splitlines() if ln.startswith("## ")]  # noqa: E731
check(
    "author prompt keeps the fleet sections after the target",
    heads(author)[heads(author).index("## Claim your target (so two agents don't author the same thing)") :],
    heads(fleet)[heads(fleet).index("## Claim your target (so two agents don't author the same thing)") :],
)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
