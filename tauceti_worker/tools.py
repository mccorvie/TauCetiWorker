"""Trusted, explicitly enabled capabilities exposed to Tau Ceti work agents."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .paths import HERE


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


LOOGLE = ToolDefinition(
    name="loogle",
    description="shared Tau Ceti declaration search",
    phases=frozenset({"roadmap", "fix", "fix-ci", "rebase", "bump"}),
    scripts=(ToolScript("scripts/tools/loogle.sh", "tools/loogle.sh"),),
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

TOOL_REGISTRY: dict[str, ToolDefinition] = {tool.name: tool for tool in (LOOGLE,)}


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


def bubble_mounts(names: tuple[str, ...], phase: str | None) -> list[str]:
    """Return narrowly scoped Bubble mounts requested by enabled tools."""
    mounts: list[str] = []
    for tool in enabled_tools(names, phase):
        for mount in tool.bubble_mounts:
            source = os.environ.get(mount.source_env, "").strip()
            if source and Path(source).is_dir():
                mounts.append(f"{Path(source).resolve()}:{mount.target}:ro")
    return mounts


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
