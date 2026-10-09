"""Trusted, explicitly enabled capabilities exposed to Tau Ceti work agents. See docs/tools.md."""

from __future__ import annotations

import json
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


# Placeholder in MCP env values and prompt fragments for the absolute path of the Lean project the
# agent works in (the host checkout, or the bench's task repo). Rendered per round.
CHECKOUT_PLACEHOLDER = "{{checkout}}"


@dataclass(frozen=True)
class McpServer:
    """A stdio MCP server a tool exposes to the agent (docs/tools.md, "Lean LSP tools").

    The worker never writes agent config files: the server is injected per invocation, as Codex `-c`
    overrides or a Claude `--mcp-config` file, by `agent_tool_argv`. The executable is resolved from
    `command_env` when set, else `command_default` (with `~` expanded), so an operator can relocate an
    install without a code change and the bench can pin one per campaign."""

    name: str  # the server id the agent sees (`mcp_servers.<name>` / `mcpServers.<name>`)
    command_env: str
    command_default: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()  # values may contain CHECKOUT_PLACEHOLDER
    # Parent-environment variables the server must inherit. Codex starts stdio servers with a minimal
    # environment (HOME, PATH, TMPDIR, LANG, …), so anything else is whitelisted through
    # `mcp_servers.<id>.env_vars`; Claude Code forwards the parent environment as is.
    env_vars: tuple[str, ...] = ()
    startup_timeout_sec: int = 60
    tool_timeout_sec: int = 600
    required: bool = True  # Codex: fail the run if the server cannot initialize, never run without it
    parallel_tool_calls: bool = False  # Codex: `supports_parallel_tool_calls` (Lean Beam asks for it)

    def command(self, env: dict[str, str] | None = None) -> Path:
        env = os.environ if env is None else env
        explicit = env.get(self.command_env, "").strip()
        return Path(explicit if explicit else self.command_default).expanduser()

    def rendered_env(self, project_dir: str | None) -> dict[str, str]:
        root = str(project_dir) if project_dir else CHECKOUT_PLACEHOLDER
        return {key: value.replace(CHECKOUT_PLACEHOLDER, root) for key, value in self.env}


@dataclass(frozen=True)
class HostBind:
    """A host directory a non-Bubble sandbox (the bench's bwrap) must bind read-only at the SAME path:
    an MCP server's install root, whose wrappers and interpreters use absolute paths into it."""

    source_env: str
    default: str

    def path(self, env: dict[str, str] | None = None) -> Path:
        env = os.environ if env is None else env
        explicit = env.get(self.source_env, "").strip()
        return Path(explicit if explicit else self.default).expanduser()


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
    # MCP tools (Lean LSP servers). Both default empty so CLI tools and every existing consumer of the
    # fields above are unaffected.
    mcp: McpServer | None = None
    host_binds: tuple[HostBind, ...] = ()


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

# Where the LSP servers are installed on the host (workboots `local-lsp/install-*.sh`): Beam's
# wrappers and runtime under beam/, lean-lsp-mcp's uv tool venv under uv-tools/ with its entry point
# in bin/, on the system python so only this root (plus /usr, which every sandbox has) must be bound.
TOOLS_ROOT_ENV = "TAUCETI_TOOLS_ROOT"
TOOLS_ROOT_DEFAULT = "~/.local/opt/tauceti-tools"
_TOOLS_ROOT_BIND = HostBind(TOOLS_ROOT_ENV, TOOLS_ROOT_DEFAULT)
# Both servers run the project's own `lean`/`lake` through elan, so the toolchain store must be
# visible too (the bench already binds ELAN_HOME for the agent; listed here so the bind list is
# complete on its own).
_ELAN_BIND = HostBind("ELAN_HOME", "~/.elan")
# What a Lean server spawned by the MCP server needs from the round's environment: the toolchain
# store, a UTF-8 locale and the round's TMPDIR, and the Lake/Mathlib cache settings the agent's own
# `lake build` uses.
_LEAN_ENV_VARS = (
    "ELAN_HOME",
    "LANG",
    "TMPDIR",
    "LAKE_CACHE_DIR",
    "MATHLIB_CACHE_DIR",
    "LAKE_ARTIFACT_CACHE",
    "LAKE_RESTORE_ARTIFACTS",
)

_FINISH_WITH_BUILD = """\
Before you finish, run `lake build` once from the shell: CI builds from clean
artifacts, and a live-server result is not a build."""

BEAM = ToolDefinition(
    name="beam",
    description="Lean Beam MCP server: goals, speculative probes, sync, module checkpoints",
    phases=_AUTHORING_PHASES,
    scripts=(),
    environment=(),
    bubble_mounts=(),
    doctor_dependencies=(),
    prompt=f"""\
Lean tools come from the `lean_beam` MCP server (Lean Beam). Every call takes
{{"workspace":{{"root":"{CHECKOUT_PLACEHOLDER}"}}}} and a `path` relative to that root.
Positions are 0-based (`line`, `character`, as in LSP): editor line N is `line: N-1`.
1. `lean_sync` the file you are working on first. It waits for elaboration and
   returns the document `version`, diagnostics and `readiness`; pass that
   `version` to every later probe on the same file. A cold sync of a
   Mathlib-heavy file takes tens of seconds; later syncs take a few.
2. Before editing, try candidate tactics with `lean_run_at` (`text` is one tactic
   block or command at `line`/`character`); several probes may run in parallel,
   and none of them changes a file. `lean_goals` (`mode: "before"|"after"`) shows
   the goal at a position, `lean_hover` a type or docstring, `lean_todo` the
   sorries, holes and errors in a range.
3. Beam never edits files: edit with your normal tools, save, then call
   `lean_update` (fast, new `version`) or `lean_sync` (also waits for
   diagnostics) instead of `lake build`. A probe with an old `version` fails with
   `contentModified` or `documentVersionMismatch`: sync again and retry.
4. When a file is error-free (`readiness.save_ready: true`), call `lean_save` on
   it so files importing it see the change without `lake build`. An importing
   file that is already open does not pick that up: its `lean_sync` fails with
   `syncBarrierIncomplete`, so call `lean_refresh` on it, then probe it. Read
   errors from `lean_sync`/`lean_refresh` with `diagnostics_in_result: true`
   (there is no separate diagnostics tool).
5. `lean_run_at` results are speculative: a successful probe is not a saved edit.
{_FINISH_WITH_BUILD}""",
    mcp=McpServer(
        name="lean_beam",
        command_env="TAUCETI_BEAM_MCP",
        command_default=f"{TOOLS_ROOT_DEFAULT}/beam/bin/lean-beam-mcp",
        env_vars=_LEAN_ENV_VARS,
        startup_timeout_sec=60,
        tool_timeout_sec=600,  # a cold lean_sync on a Mathlib-heavy file is the slow call (S4a: 10–40 s)
        required=True,
        parallel_tool_calls=True,  # Beam's own Codex registration sets it (docs/SETUP.md)
    ),
    host_binds=(_TOOLS_ROOT_BIND, _ELAN_BIND),
)

# lean-lsp-mcp's tools that call remote services (LeanSearch, loogle.lean-lang.org, Lean Finder,
# the premise server). Names from the installed 0.31.0 registry; disabled so a bench arm never
# reaches unpinned, current Mathlib. The sandbox has no network anyway, but a disabled tool is
# absent from `tools/list` rather than a wasted, failing call.
LEAN_LSP_REMOTE_TOOLS = ("lean_leansearch", "lean_loogle", "lean_leanfinder", "lean_hammer_premise")

LEANLSP = ToolDefinition(
    name="leanlsp",
    description="lean-lsp-mcp MCP server: goals, diagnostics, multi-attempt",
    phases=_AUTHORING_PHASES,
    scripts=(),
    environment=(),
    bubble_mounts=(),
    doctor_dependencies=(),
    prompt=f"""\
Lean tools come from the `lean_lsp` MCP server (lean-lsp-mcp) for the project at
`{CHECKOUT_PLACEHOLDER}`. `file_path` is relative to that root or absolute; positions
are 1-based (`line`, `column`), as in an editor.
- `lean_goal` (line, optional column) shows the goal before and after a tactic
  line; `lean_term_goal`, `lean_hover_info`, `lean_declaration_file` and
  `lean_completions` work as in an IDE.
- `lean_diagnostic_messages` returns a file's current errors and warnings. It
  re-reads the file from disk and waits for elaboration (a cold file can take
  minutes), so call it after each edit instead of `lake build`.
- `lean_multi_attempt` tries several tactic snippets at a line in scratch copies
  (three or more at a time is cheapest) and reports each one's goal and
  diagnostics; your file is not changed.
- `lean_run_code` compiles a self-contained snippet with its own imports.
  `lean_build` runs `lake build`; prefer the shell `lake build` at the end.
- There is no checkpoint: a file importing a changed module sees the change only
  after `lake build`. The remote search tools are disabled in this run; the
  sandbox has no network, so do not retry any tool that would need it.
{_FINISH_WITH_BUILD}""",
    mcp=McpServer(
        name="lean_lsp",
        command_env="TAUCETI_LEAN_LSP_MCP",
        command_default=f"{TOOLS_ROOT_DEFAULT}/bin/lean-lsp-mcp",
        env=(
            ("LEAN_PROJECT_PATH", CHECKOUT_PLACEHOLDER),
            ("LEAN_MCP_DISABLED_TOOLS", ",".join(LEAN_LSP_REMOTE_TOOLS)),
            ("LEAN_LOG_LEVEL", "WARNING"),
        ),
        env_vars=_LEAN_ENV_VARS,
        startup_timeout_sec=60,
        tool_timeout_sec=600,  # a cold lean_diagnostic_messages waits for the whole file (S4a: minutes)
        required=True,
    ),
    host_binds=(_TOOLS_ROOT_BIND, _ELAN_BIND),
)

TOOL_REGISTRY: dict[str, ToolDefinition] = {tool.name: tool for tool in (LOOGLE, FINDER, EXPLORE, BEAM, LEANLSP)}


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


def add_tool_prompt(
    prompt: str, names: tuple[str, ...], phase: str, bin_dir: str, project_dir: str | Path | None = None
) -> str:
    """Append the short, trusted capability prelude for tools usable in ``phase``.

    ``project_dir`` renders the checkout placeholder the MCP fragments carry; without it the agent is
    told to use the absolute path of its checkout, which it can always discover itself."""
    root = str(project_dir) if project_dir else "<absolute path of this checkout>"
    fragments = [
        tool.prompt.replace("__BIN__", bin_dir).replace(CHECKOUT_PLACEHOLDER, root)
        for tool in enabled_tools(names, phase)
    ]
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


def _selected(names: tuple[str, ...], phase: str | None) -> tuple[ToolDefinition, ...]:
    """Enabled definitions for ``phase``; ``phase=None`` means every selected tool (pre-dispatch)."""
    if phase is None:
        return tuple(TOOL_REGISTRY[name] for name in names)
    return enabled_tools(names, phase)


def mcp_tools(names: tuple[str, ...], phase: str | None) -> tuple[ToolDefinition, ...]:
    """Enabled definitions that expose an MCP server."""
    return tuple(tool for tool in _selected(names, phase) if tool.mcp is not None)


def resolve_host_binds(names: tuple[str, ...], phase: str | None, env: dict[str, str] | None = None) -> list[Path]:
    """Host directories a non-Bubble sandbox must bind read-only at the same path for the enabled
    tools, deduplicated in order. Missing directories are left out; `preflight_tools` is what fails."""
    binds: list[Path] = []
    for tool in _selected(names, phase):
        for bind in tool.host_binds:
            path = bind.path(env)
            if path.is_dir() and path.resolve() not in [b.resolve() for b in binds]:
                binds.append(path)
    return binds


def preflight_tools(names: tuple[str, ...], phase: str | None = None, *, bubble: bool = False) -> None:
    """Fail loudly, before dispatch, when an MCP tool cannot actually run.

    A CLI tool fails open (the wrapper exits nonzero and the agent falls back to grep). An MCP tool
    must not: a server that silently never started would turn a treatment arm into a control arm
    while the prompt still advertises the tool. So the executable must exist and be executable, and
    Bubble rounds reject MCP tools outright until the frozen inner command learns to carry them."""
    tools = mcp_tools(names, phase)
    if not tools:
        return
    if bubble:
        named = ", ".join(tool.name for tool in tools)
        raise ValueError(f"MCP tools are not supported inside --bubble yet: {named} (run on the host)")
    for tool in tools:
        assert tool.mcp is not None
        command = tool.mcp.command()
        if not command.is_file() or not os.access(command, os.X_OK):
            raise ValueError(
                f"tool {tool.name!r}: MCP server executable {command} is missing or not executable "
                f"(set ${tool.mcp.command_env} or install it; see docs/tools.md)"
            )


def _toml_string(value: str) -> str:
    # TOML basic strings share JSON's escape syntax for everything a path or env value can contain.
    return json.dumps(value, ensure_ascii=False)


def _toml_inline_table(items: dict[str, str]) -> str:
    return "{" + ",".join(f"{key}={_toml_string(value)}" for key, value in items.items()) + "}"


def agent_tool_argv(
    provider: str,
    names: tuple[str, ...],
    phase: str | None,
    *,
    project_dir: str | Path | None,
    rounddir: str | Path | None,
) -> list[str]:
    """Extra argv that gives ``provider`` the enabled MCP servers for this round, without touching any
    config file.

    Codex takes dotted `-c` overrides (`codex exec --help`: a dotted path overrides a nested value and
    the value is parsed as TOML), so each server becomes `mcp_servers.<name>.<key>=<toml>`. Claude
    takes `--mcp-config <json>`; one file under ``rounddir`` holds every server, and
    `--strict-mcp-config` keeps the operator's own servers out. Other providers get nothing and a
    logged warning: Kiro and the pi runner have no equivalent switch."""
    tools = mcp_tools(names, phase)
    if not tools:
        return []
    project = str(project_dir) if project_dir else None
    if provider == "codex":
        argv: list[str] = []
        for tool in tools:
            server = tool.mcp
            assert server is not None
            prefix = f"mcp_servers.{server.name}."
            argv += ["-c", f"{prefix}command={_toml_string(str(server.command()))}"]
            if server.args:
                argv += ["-c", f"{prefix}args=[{','.join(_toml_string(a) for a in server.args)}]"]
            env = server.rendered_env(project)
            if env:
                argv += ["-c", f"{prefix}env={_toml_inline_table(env)}"]
            if server.env_vars:
                argv += ["-c", f"{prefix}env_vars=[{','.join(_toml_string(v) for v in server.env_vars)}]"]
            argv += ["-c", f"{prefix}startup_timeout_sec={server.startup_timeout_sec}"]
            argv += ["-c", f"{prefix}tool_timeout_sec={server.tool_timeout_sec}"]
            if server.required:
                # Documented in the Codex config reference ("fail startup/resume if this enabled MCP
                # server cannot initialize"); `codex mcp get` does not echo it, so it is not probed.
                argv += ["-c", f"{prefix}required=true"]
            if server.parallel_tool_calls:
                argv += ["-c", f"{prefix}supports_parallel_tool_calls=true"]
        return argv
    if provider == "claude":
        if rounddir is None:
            raise ValueError("claude MCP tools need a rounddir to hold the --mcp-config file")
        servers = {}
        for tool in tools:
            server = tool.mcp
            assert server is not None
            entry: dict[str, object] = {"type": "stdio", "command": str(server.command())}
            if server.args:
                entry["args"] = list(server.args)
            env = server.rendered_env(project)
            if env:
                entry["env"] = env
            servers[server.name] = entry
        rounddir = Path(rounddir)
        rounddir.mkdir(parents=True, exist_ok=True)
        config = rounddir / "mcp-servers.json"
        config.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8")
        return ["--mcp-config", str(config), "--strict-mcp-config"]
    from .config import log  # local import: config is a leaf the rest of this module never needs

    log(f"tools: {provider} cannot load MCP servers; {', '.join(t.name for t in tools)} will be unavailable")
    return []


def doctor_rows() -> list[tuple[str, bool, str]]:
    """Discover packaged optional tools without making optional service failures fatal. An MCP tool
    is `ok` when its server executable resolves; a CLI tool when its scripts and host commands exist."""
    rows = []
    for tool in TOOL_REGISTRY.values():
        packaged = all((HERE / script.source).is_file() for script in tool.scripts)
        dependencies = all(shutil.which(command) for command in tool.doctor_dependencies)
        ok = bool(packaged and dependencies)
        note = tool.description
        if tool.mcp is not None:
            command = tool.mcp.command()
            ok = ok and command.is_file() and os.access(command, os.X_OK)
            note += f" ({command})"
        rows.append((tool.name, ok, note))
    return rows
