"""The target spec: the handoff from choosing a roadmap target to authoring it.

`tauceti.target-spec/v1` is a small Markdown document (see `docs/target-spec.md`). A selection pass
writes it; an authoring pass receives it inline. It opens with the same `<!--tauceti-target:v1 …-->`
marker a PR body carries, so one regex (`survey.TARGET_MARKER_RE`) validates specs, PR bodies and
fleet PRs alike, and the slug the author claims is the slug the planner chose.

The parser is strict on structure and silent on content: it checks the marker, the `Roadmap:` line,
the heading set and that every section has a body. Whether the target is a good one is the
reviewer's business, not this module's."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .survey import TARGET_MARKER_RE

SCHEMA = "tauceti.target-spec/v1"

# The order is the order sections must appear in.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Milestone",
    "Target",
    "Planned declarations",
    "Reuse",
    "Placement",
    "Remaining",
    "Avoid",
)

# The slug rule from the roadmap prompt: lowercased, every run of non-alphanumerics -> one `-`.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_HEADING_RE = re.compile(r"^## (.+?)\s*$")
_ROADMAP_LINE_RE = re.compile(r"^Roadmap:\s*(\S+)\s*$")
_CONSUMER_LINE_RE = re.compile(r"^Consumer roadmap:\s*(\S+)\s*$")

# A roadmap area is a top-level directory name of TauCetiRoadmap; `any`/`auto` are scopes, not areas.
_AREA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_NOT_AREAS = frozenset({"", "any", "auto", "none"})


class SpecError(ValueError):
    """The text is not a valid target spec. The message says what is wrong, for the agent's report."""


def slugify(phrase: str) -> str:
    """Derive a target id the way the prompt tells the agent to."""
    return _NON_ALNUM_RE.sub("-", phrase.lower()).strip("-")


@dataclass(frozen=True)
class Spec:
    focus: str
    id: str
    consumer_roadmap: str | None
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def marker(self) -> str:
        return marker_line(self.focus, self.id)

    def render(self) -> str:
        """The canonical text form; `parse_spec(spec.render()) == spec`."""
        lines = [self.marker, f"Roadmap: {self.focus}"]
        if self.consumer_roadmap:
            lines.append(f"Consumer roadmap: {self.consumer_roadmap}")
        for name in REQUIRED_SECTIONS:
            lines += ["", f"## {name}", self.sections.get(name, "").strip()]
        return "\n".join(lines) + "\n"


def marker_line(focus: str, slug: str) -> str:
    """The exact marker line the PR wrapper looks for (compact JSON, fixed key order)."""
    return f"<!--tauceti-target:v1 {json.dumps({'focus': focus, 'id': slug}, separators=(',', ':'))}-->"


def _check_area(name: str, what: str) -> str:
    if name in _NOT_AREAS or not _AREA_RE.match(name):
        raise SpecError(f"{what} {name!r} is not a concrete roadmap area")
    return name


def parse_spec(text: str) -> Spec:
    """Parse and validate a `tauceti.target-spec/v1` document; raise SpecError otherwise."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i == len(lines):
        raise SpecError("empty spec")
    m = TARGET_MARKER_RE.fullmatch(lines[i].strip())
    if not m:
        raise SpecError("first line must be the <!--tauceti-target:v1 {...}--> marker")
    try:
        payload = json.loads(m.group(1))
    except ValueError as e:
        raise SpecError(f"marker JSON does not parse: {e}") from None
    if not isinstance(payload, dict):
        raise SpecError("marker JSON must be an object")
    focus, slug = payload.get("focus"), payload.get("id")
    if not isinstance(focus, str) or not isinstance(slug, str):
        raise SpecError('marker needs string "focus" and "id"')
    _check_area(focus, "marker focus")
    if not SLUG_RE.match(slug):
        raise SpecError(f"marker id {slug!r} is not a slug (lowercase alphanumerics joined by single '-')")
    extra = sorted(set(payload) - {"focus", "id"})
    if extra:
        raise SpecError(f"marker has unexpected key(s): {', '.join(extra)}")
    i += 1

    if i == len(lines) or not (rm := _ROADMAP_LINE_RE.match(lines[i])):
        raise SpecError("line after the marker must be `Roadmap: <target-roadmap>`")
    if rm.group(1) != focus:
        raise SpecError(f"Roadmap: line {rm.group(1)!r} does not match the marker focus {focus!r}")
    i += 1

    consumer = None
    if i < len(lines) and (cm := _CONSUMER_LINE_RE.match(lines[i])):
        consumer = _check_area(cm.group(1), "Consumer roadmap:")
        if consumer == focus:
            raise SpecError("Consumer roadmap: must differ from the target roadmap (omit it without a switch)")
        i += 1

    # Sections: `## Name` headings in the fixed order, each with a non-empty body.
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []

    def close() -> None:
        if current is not None:
            if not "\n".join(body).strip():
                raise SpecError(f"section '{current}' is empty")
            sections[current] = "\n".join(body).strip() + "\n"

    for line in lines[i:]:
        hm = _HEADING_RE.match(line)
        if hm:
            close()
            name = hm.group(1)
            if name not in REQUIRED_SECTIONS:
                raise SpecError(f"unknown section '## {name}'")
            if name in sections or name == current:
                raise SpecError(f"duplicate section '## {name}'")
            expected = REQUIRED_SECTIONS[len(sections)]
            if name != expected:
                raise SpecError(f"section '## {name}' out of order: expected '## {expected}'")
            current, body = name, []
        elif current is None:
            if line.strip():
                raise SpecError(f"unexpected text before the first section: {line.strip()[:60]!r}")
        else:
            body.append(line)
    close()
    missing = [s for s in REQUIRED_SECTIONS if s not in sections]
    if missing:
        raise SpecError(f"missing section(s): {', '.join('## ' + s for s in missing)}")
    return Spec(focus=focus, id=slug, consumer_roadmap=consumer, sections=sections)


def validate_spec(text: str) -> str | None:
    """None if `text` is a valid spec, else the error message."""
    try:
        parse_spec(text)
    except SpecError as e:
        return str(e)
    return None
