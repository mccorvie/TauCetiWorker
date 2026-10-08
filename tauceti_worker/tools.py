"""Trusted, explicitly enabled capabilities exposed to Tau Ceti work agents. See docs/tools.md."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .paths import HERE

# Where every CLI tool appends one JSON line per invocation (docs/tools.md, "Tool-call log"). The
# harness sets it; unset means no logging. Host rounds inherit it from the operator's environment.
# It is deliberately NOT forwarded into Bubble: the wrappers there run from the read-only /opt/round
# mount and would need a writable log mount, which no round provides yet.
TOOL_LOG_ENV = "TAUCETI_TOOL_LOG"

# Phases in which a search tool makes sense: every code-writing phase, never review (a reviewer
# with better search finds more duplicates, which would confound a reuse comparison).
_AUTHORING_PHASES = frozenset({"roadmap", "fix", "fix-ci", "rebase", "bump"})


@dataclass(frozen=True)
class ToolScript:
    """A packaged script and the command name exposed to the agent."""

    source: str
    exposed_name: str


@dataclass(frozen=True)
class ToolMount:
    """A host directory selected by environment and its fixed read-only Bubble target."""

    source_env: str
    target: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    phases: frozenset[str]
    scripts: tuple[ToolScript, ...]
    environment: tuple[str, ...]
    bubble_mounts: tuple[ToolMount, ...]
    doctor_dependencies: tuple[str, ...]
    prompt: str


# Helpers the wrappers source or exec at run time. They are staged beside every wrapper that needs
# them, so a staged bin directory is self-contained (the bench copies a tool's scripts, nothing else).
_LOG_HELPER = ToolScript("scripts/tools/_log.sh", "tools/_log.sh")
_SEARCH_CLIENT = ToolScript("scripts/tools/_search_client.sh", "tools/_search_client.sh")

_CONFIRM_HITS = """\
Hits are candidates, not confirmations: check each one with `grep` in
`.lake/packages/mathlib` or `TauCeti/`, or with `#check`, and check its direction
and hypotheses. The index does not contain branch-local changes, and an empty
result is not evidence that a lemma is missing."""

LOOGLE = ToolDefinition(
    name="loogle",
    description="shared Tau Ceti declaration search",
    phases=_AUTHORING_PHASES,
    scripts=(ToolScript("scripts/tools/loogle.sh", "tools/loogle.sh"), _LOG_HELPER),
    environment=("TAUCETI_LOOGLE_URL",),
    bubble_mounts=(ToolMount("TAUCETI_LOOGLE_SOCKET_DIR", "/run/tauceti-loogle"),),
    doctor_dependencies=("curl", "jq"),
    prompt="""\
A shared Tau Ceti Loogle service is available through:

  "__BIN__/tools/loogle.sh" '<query>'

Use it to search Mathlib and canonical Tau Ceti main for declarations.
It does not contain branch-local changes. Confirm results in the current
checkout before relying on them.""",
)

FINDER = ToolDefinition(
    name="finder",
    description="Lean Finder semantic search over Mathlib and Tau Ceti main",
    phases=_AUTHORING_PHASES,
    scripts=(ToolScript("scripts/tools/finder.sh", "tools/finder.sh"), _SEARCH_CLIENT, _LOG_HELPER),
    environment=("TAUCETI_FINDER_URL", "TAUCETI_SEARCH_K"),
    bubble_mounts=(ToolMount("TAUCETI_FINDER_SOCKET_DIR", "/run/tauceti-finder"),),
    doctor_dependencies=("curl", "jq"),
    prompt=f"""\
Lean Finder, a semantic search over Mathlib and canonical Tau Ceti main at a
fixed commit, is available through:

  "__BIN__/tools/finder.sh" '<query>'

Query with a one-sentence English statement of what you need, a goal
(`⊢ ...`), or a partial Lean signature. {_CONFIRM_HITS}""",
)

EXPLORE = ToolDefinition(
    name="explore",
    description="LeanExplore semantic search over Mathlib and Tau Ceti main",
    phases=_AUTHORING_PHASES,
    scripts=(ToolScript("scripts/tools/explore.sh", "tools/explore.sh"), _SEARCH_CLIENT, _LOG_HELPER),
    environment=("TAUCETI_EXPLORE_URL", "TAUCETI_SEARCH_K"),
    bubble_mounts=(ToolMount("TAUCETI_EXPLORE_SOCKET_DIR", "/run/tauceti-explore"),),
    doctor_dependencies=("curl", "jq"),
    prompt=f"""\
LeanExplore, a semantic search over Mathlib and canonical Tau Ceti main at a
fixed commit, is available through:

  "__BIN__/tools/explore.sh" '<query>'

Query in English, or with a guessed name fragment such as `continuousOn compact
bounded`; it matches declaration names as well as meaning. {_CONFIRM_HITS}""",
)

TOOL_REGISTRY: dict[str, ToolDefinition] = {tool.name: tool for tool in (LOOGLE, FINDER, EXPLORE)}


def resolve_tools(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Validate a requested tool list without silently changing its order or multiplicity."""
    unknown = [name for name in names if name not in TOOL_REGISTRY]
    if unknown:
        valid = ", ".join(TOOL_REGISTRY)
        raise ValueError(f"unknown tool name(s): {', '.join(unknown)} (valid: {valid})")
    return tuple(names)


def enabled_tools(names: tuple[str, ...], phase: str | None) -> tuple[ToolDefinition, ...]:
    """Definitions enabled for this phase, preserving the operator's requested order."""
    return tuple(TOOL_REGISTRY[name] for name in names if phase in TOOL_REGISTRY[name].phases)


def add_tool_prompt(prompt: str, names: tuple[str, ...], phase: str, bin_dir: str) -> str:
    """Append the short, trusted capability prelude for tools usable in ``phase``."""
    fragments = [tool.prompt.replace("__BIN__", bin_dir) for tool in enabled_tools(names, phase)]
    if not fragments:
        return prompt
    return prompt.rstrip() + "\n\n## Enabled Tau Ceti tools\n\n" + "\n\n".join(fragments) + "\n"


def stage_tool_scripts(rounddir: Path, names: tuple[str, ...], phase: str | None) -> None:
    """Copy selected packaged helpers into Bubble's existing read-only /opt/round mount."""
    for tool in enabled_tools(names, phase):
        for script in tool.scripts:
            destination = rounddir / script.exposed_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(HERE / script.source, destination)
            destination.chmod(0o755)


def resolve_mounts(
    names: tuple[str, ...], phase: str | None, env: dict[str, str] | None = None
) -> list[tuple[Path, str]]:
    """(host directory, fixed in-sandbox target) for every enabled tool whose source variable names
    an existing directory. Sandboxes other than Bubble (the bench's bwrap) bind these read-only."""
    env = os.environ if env is None else env
    resolved: list[tuple[Path, str]] = []
    for tool in enabled_tools(names, phase):
        for mount in tool.bubble_mounts:
            source = env.get(mount.source_env, "").strip()
            if source and Path(source).is_dir():
                resolved.append((Path(source).resolve(), mount.target))
    return resolved


def bubble_mounts(names: tuple[str, ...], phase: str | None) -> list[str]:
    """Return narrowly scoped Bubble mounts requested by enabled tools."""
    return [f"{source}:{target}:ro" for source, target in resolve_mounts(names, phase)]


def forwarded_environment(names: tuple[str, ...], phase: str | None) -> tuple[str, ...]:
    """Environment variables a definition explicitly allows into its Bubble command."""
    return tuple(name for tool in enabled_tools(names, phase) for name in tool.environment)


def doctor_rows() -> list[tuple[str, bool, str]]:
    """Discover packaged optional tools without making optional service failures fatal."""
    rows = []
    for tool in TOOL_REGISTRY.values():
        packaged = all((HERE / script.source).is_file() for script in tool.scripts)
        dependencies = all(shutil.which(command) for command in tool.doctor_dependencies)
        rows.append((tool.name, bool(packaged and dependencies), tool.description))
    return rows
