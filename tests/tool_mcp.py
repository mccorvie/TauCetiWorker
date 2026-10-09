#!/usr/bin/env python3
"""MCP tools (`beam`, `leanlsp`) are injected per invocation, fail loudly when absent, and are
countable from the transcript (docs/tools.md, "Lean LSP tools").

Covers: the registry entries and their phases; `host_agent_argv` growing exactly the Codex `-c`
overrides / the Claude `--mcp-config` file and nothing else; no injection for Kiro or the
OpenRouter runner; `preflight_tools` (missing executable, Bubble, CLI-only selections); host binds;
prompt fragments rendering the project directory; managed-worker validation; and
`extract_tool_calls` on real captured transcripts (tests/fixtures) of each provider, plus synthetic
failure and CLI-wrapper shapes the captures do not contain.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tauceti_worker.agents import host_agent_argv
from tauceti_worker.tool_calls import extract_tool_calls, summarize, transcript_mcp_servers
from tauceti_worker.tools import (
    BEAM,
    CHECKOUT_PLACEHOLDER,
    LEAN_LSP_REMOTE_TOOLS,
    LEANLSP,
    TOOL_REGISTRY,
    add_tool_prompt,
    agent_tool_argv,
    doctor_rows,
    mcp_tools,
    preflight_tools,
    resolve_host_binds,
    resolve_tools,
    stage_tool_scripts,
)
from tauceti_worker.worker_manager import WorkersError, WorkerSpec


def check(label, actual, expected=True):
    if actual != expected:
        raise AssertionError(f"{label}: got {actual!r}, expected {expected!r}")


# ---------------------------------------------------------------------------------------------------
# Fixtures: a fake tools root with executable server stubs, selected through the documented env vars
# ---------------------------------------------------------------------------------------------------

root = Path(tempfile.mkdtemp(prefix="tauceti-mcp-test-"))
tools_root = root / "tauceti-tools"
beam_bin = tools_root / "beam" / "bin" / "lean-beam-mcp"
lsp_bin = tools_root / "bin" / "lean-lsp-mcp"
for stub in (beam_bin, lsp_bin):
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(0o755)
elan_home = root / "elan"
elan_home.mkdir()
os.environ["TAUCETI_TOOLS_ROOT"] = str(tools_root)
os.environ["TAUCETI_BEAM_MCP"] = str(beam_bin)
os.environ["TAUCETI_LEAN_LSP_MCP"] = str(lsp_bin)
os.environ["ELAN_HOME"] = str(elan_home)
LEAN_ENV_VARS = (
    '["ELAN_HOME","LANG","TMPDIR","LAKE_CACHE_DIR","MATHLIB_CACHE_DIR","LAKE_ARTIFACT_CACHE","LAKE_RESTORE_ARTIFACTS"]'
)
project = root / "TauCeti"
project.mkdir()
rounddir = root / "round"

# ---------------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------------

check("registry names", tuple(TOOL_REGISTRY), ("loogle", "finder", "explore", "beam", "leanlsp"))
check("beam and leanlsp resolve", resolve_tools(["beam", "leanlsp"]), ("beam", "leanlsp"))
for tool in (BEAM, LEANLSP):
    check(f"{tool.name} is an MCP tool", tool.mcp is not None)
    check(f"{tool.name} stages no scripts", tool.scripts, ())
    check(f"{tool.name} never reaches review", "review" in tool.phases, False)
    check(f"{tool.name} is a fix-phase tool", "fix" in tool.phases)
    check(
        f"{tool.name} binds the tools root and elan",
        [b.source_env for b in tool.host_binds],
        ["TAUCETI_TOOLS_ROOT", "ELAN_HOME"],
    )
check("CLI tools carry no MCP server", all(TOOL_REGISTRY[n].mcp is None for n in ("loogle", "finder", "explore")))
check("mcp_tools filters by phase", [t.name for t in mcp_tools(("loogle", "beam"), "fix")], ["beam"])
check("mcp_tools ignores review", mcp_tools(("beam",), "review"), ())
check(
    "mcp_tools with phase None takes every selection",
    [t.name for t in mcp_tools(("beam", "leanlsp"), None)],
    ["beam", "leanlsp"],
)
check(
    "remote lean-lsp-mcp tools are disabled",
    dict(LEANLSP.mcp.env)["LEAN_MCP_DISABLED_TOOLS"],
    ",".join(LEAN_LSP_REMOTE_TOOLS),
)
check("lean-lsp-mcp gets the project path", dict(LEANLSP.mcp.env)["LEAN_PROJECT_PATH"], CHECKOUT_PLACEHOLDER)
check("beam command honours its env var", BEAM.mcp.command(), beam_bin)
check(
    "beam command default under the tools root",
    BEAM.mcp.command({}),
    Path("~/.local/opt/tauceti-tools/beam/bin/lean-beam-mcp").expanduser(),
)

# ---------------------------------------------------------------------------------------------------
# Codex: dotted -c overrides, before --sandbox, prompt still last
# ---------------------------------------------------------------------------------------------------

plain, _ = host_agent_argv("do the thing", "codex")
argv, _ = host_agent_argv(
    "do the thing", "codex", tools=("beam", "leanlsp"), phase="fix", project_dir=project, rounddir=rounddir
)
check("codex prompt stays last", argv[-1], "do the thing")
check("codex sandbox flags stay in place", argv[-4:-1], ["--sandbox", "danger-full-access", "--skip-git-repo-check"])
overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
expected_overrides = [
    f'mcp_servers.lean_beam.command="{beam_bin}"',
    f"mcp_servers.lean_beam.env_vars={LEAN_ENV_VARS}",
    "mcp_servers.lean_beam.startup_timeout_sec=60",
    "mcp_servers.lean_beam.tool_timeout_sec=600",
    "mcp_servers.lean_beam.required=true",
    "mcp_servers.lean_beam.supports_parallel_tool_calls=true",
    f'mcp_servers.lean_lsp.command="{lsp_bin}"',
    'mcp_servers.lean_lsp.env={LEAN_PROJECT_PATH="%s",LEAN_MCP_DISABLED_TOOLS="%s",LEAN_LOG_LEVEL="WARNING"}'
    % (project, ",".join(LEAN_LSP_REMOTE_TOOLS)),
    f"mcp_servers.lean_lsp.env_vars={LEAN_ENV_VARS}",
    "mcp_servers.lean_lsp.startup_timeout_sec=60",
    "mcp_servers.lean_lsp.tool_timeout_sec=600",
    "mcp_servers.lean_lsp.required=true",
]
plain_overrides = [plain[i + 1] for i, a in enumerate(plain) if a == "-c"]
check("codex MCP overrides are exactly the documented set", overrides[len(plain_overrides) :], expected_overrides)
check("codex keeps its model/effort overrides first", overrides[: len(plain_overrides)], plain_overrides)
check("codex without tools is byte-identical to before", host_agent_argv("do the thing", "codex")[0], plain)
check(
    "codex without eligible tools adds nothing",
    host_agent_argv("p", "codex", tools=("beam",), phase="review")[0],
    host_agent_argv("p", "codex")[0],
)
check(
    "CLI-only tools add no codex flags",
    host_agent_argv("p", "codex", tools=("loogle",), phase="fix")[0],
    host_agent_argv("p", "codex")[0],
)

# ---------------------------------------------------------------------------------------------------
# Claude: one --mcp-config file for every enabled server, strict, before --dangerously-skip-permissions
# ---------------------------------------------------------------------------------------------------

argv, _ = host_agent_argv(
    "do the thing", "claude", tools=("beam", "leanlsp"), phase="fix", project_dir=project, rounddir=rounddir
)
config_path = rounddir / "mcp-servers.json"
check(
    "claude gets --mcp-config",
    argv[-4:],
    ["--mcp-config", str(config_path), "--strict-mcp-config", "--dangerously-skip-permissions"],
)
check("claude prompt follows -p", argv[argv.index("-p") + 1], "do the thing")
config = json.loads(config_path.read_text())
check("claude config lists both servers", sorted(config["mcpServers"]), ["lean_beam", "lean_lsp"])
check("claude beam entry", config["mcpServers"]["lean_beam"], {"type": "stdio", "command": str(beam_bin)})
check(
    "claude lean_lsp entry renders the checkout",
    config["mcpServers"]["lean_lsp"],
    {
        "type": "stdio",
        "command": str(lsp_bin),
        "env": {
            "LEAN_PROJECT_PATH": str(project),
            "LEAN_MCP_DISABLED_TOOLS": ",".join(LEAN_LSP_REMOTE_TOOLS),
            "LEAN_LOG_LEVEL": "WARNING",
        },
    },
)
check(
    "claude without tools is unchanged",
    host_agent_argv("p", "claude")[0],
    host_agent_argv("p", "claude", tools=(), phase="fix")[0],
)
try:
    agent_tool_argv("claude", ("beam",), "fix", project_dir=project, rounddir=None)
    raise AssertionError("claude needs a rounddir")
except ValueError as exc:
    check("claude rounddir diagnostic", "rounddir" in str(exc))

# ---------------------------------------------------------------------------------------------------
# Providers without an MCP switch get nothing
# ---------------------------------------------------------------------------------------------------

check("kiro gets no MCP flags", agent_tool_argv("kiro", ("beam",), "fix", project_dir=project, rounddir=rounddir), [])
check(
    "openrouter runner gets no MCP flags",
    agent_tool_argv("deepseek", ("beam",), "fix", project_dir=project, rounddir=rounddir),
    [],
)
check(
    "deepseek argv unchanged by MCP tools",
    host_agent_argv("p", "deepseek", tools=("beam",), phase="fix", project_dir=project, rounddir=rounddir)[0],
    host_agent_argv("p", "deepseek")[0],
)

# ---------------------------------------------------------------------------------------------------
# Preflight: fail loudly for MCP tools, never for CLI tools
# ---------------------------------------------------------------------------------------------------

preflight_tools(("beam", "leanlsp", "loogle"), None)
preflight_tools(("beam",), "fix")
preflight_tools(("loogle", "finder"), None, bubble=True)  # CLI tools are fine in Bubble
try:
    preflight_tools(("beam",), None, bubble=True)
    raise AssertionError("MCP tools must be rejected in Bubble")
except ValueError as exc:
    check("bubble diagnostic names the tool", "beam" in str(exc) and "bubble" in str(exc))
os.environ["TAUCETI_BEAM_MCP"] = str(root / "missing" / "lean-beam-mcp")
try:
    preflight_tools(("beam",), None)
    raise AssertionError("a missing executable must fail preflight")
except ValueError as exc:
    check("missing executable diagnostic", "lean-beam-mcp" in str(exc) and "TAUCETI_BEAM_MCP" in str(exc))
check("doctor reports the missing server", [ok for name, ok, _ in doctor_rows() if name == "beam"], [False])
not_exec = root / "not-exec"
not_exec.write_text("#!/bin/sh\n")
not_exec.chmod(0o644)
os.environ["TAUCETI_BEAM_MCP"] = str(not_exec)
try:
    preflight_tools(("beam",), None)
    raise AssertionError("a non-executable file must fail preflight")
except ValueError:
    pass
os.environ["TAUCETI_BEAM_MCP"] = str(beam_bin)
check("doctor reports the present server", [ok for name, ok, _ in doctor_rows() if name == "beam"], [True])
check("review phase preflights nothing", preflight_tools(("beam",), "review", bubble=True), None)

# ---------------------------------------------------------------------------------------------------
# Host binds and staging
# ---------------------------------------------------------------------------------------------------

check(
    "host binds resolve the tools root and elan once",
    resolve_host_binds(("beam", "leanlsp"), "fix"),
    [tools_root, elan_home],
)
check("review phase binds nothing", resolve_host_binds(("beam",), "review"), [])
check("CLI tools bind nothing", resolve_host_binds(("loogle",), "fix"), [])
check(
    "binds skip a missing root",
    resolve_host_binds(("beam",), "fix", {"TAUCETI_TOOLS_ROOT": str(root / "nope"), "ELAN_HOME": str(root / "nope")}),
    [],
)
staged = root / "staged"
stage_tool_scripts(staged, ("beam", "loogle"), "fix")
check(
    "MCP tools stage nothing, CLI tools still do",
    sorted(p.name for p in (staged / "tools").iterdir()),
    ["_log.sh", "loogle.sh"],
)

# ---------------------------------------------------------------------------------------------------
# Prompt fragments
# ---------------------------------------------------------------------------------------------------

prompt = add_tool_prompt("task", ("beam", "leanlsp"), "fix", "/bin", project_dir=project)
check("beam fragment names the workspace root", f'{{"workspace":{{"root":"{project}"}}}}' in prompt)
check("beam fragment says 0-based", "0-based" in prompt)
check("beam fragment covers sync and save", "lean_sync" in prompt and "lean_save" in prompt and "lean_run_at" in prompt)
check(
    "beam fragment names both stale-version errors", "contentModified" in prompt and "documentVersionMismatch" in prompt
)
check("leanlsp fragment names the project root", f"project at\n`{project}`" in prompt)
check("leanlsp fragment says 1-based", "1-based" in prompt)
check(
    "leanlsp fragment covers the three tools",
    all(t in prompt for t in ("lean_goal", "lean_diagnostic_messages", "lean_multi_attempt")),
)
check("placeholder fully rendered", CHECKOUT_PLACEHOLDER not in prompt)
check("no fragment for review", add_tool_prompt("task", ("beam",), "review", "/bin", project_dir=project), "task")
check(
    "without a project dir the agent is told to find it",
    "<absolute path of this checkout>" in add_tool_prompt("task", ("beam",), "fix", "/bin"),
)
check(
    "positional signature still works",
    add_tool_prompt("task", ("loogle",), "fix", "/bin").count("/bin/tools/loogle.sh"),
    1,
)

# ---------------------------------------------------------------------------------------------------
# Managed workers: validation applies the same preflight
# ---------------------------------------------------------------------------------------------------

spec = WorkerSpec.from_dict({"id": "mathos", "tools": ["beam"]}, 0)
check("managed host worker accepts beam", spec.tools, ("beam",))
try:
    WorkerSpec.from_dict({"id": "mathos", "tools": ["beam"], "sandbox": "bubble"}, 0)
    raise AssertionError("managed bubble worker with an MCP tool must be rejected")
except WorkersError as exc:
    check("managed bubble diagnostic", "bubble" in str(exc))
os.environ["TAUCETI_BEAM_MCP"] = str(root / "missing" / "lean-beam-mcp")
try:
    WorkerSpec.from_dict({"id": "mathos", "tools": ["beam"]}, 0)
    raise AssertionError("managed worker with a missing server must be rejected")
except WorkersError:
    pass
os.environ["TAUCETI_BEAM_MCP"] = str(beam_bin)

# ---------------------------------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------------------------------

FIXTURES = REPO / "tests" / "fixtures"
codex_real = FIXTURES / "codex_exec_mcp_0_160.jsonl"
calls = extract_tool_calls(codex_real)
check(
    "codex fixture: one record per item.completed",
    [(c["provider"], c["kind"], c["server"], c["tool"], c["ok"], c["latency_ms"], c["t"]) for c in calls],
    [
        ("codex", "mcp", "lean_beam", "lean_sync", True, None, None),
        ("codex", "mcp", "lean_beam", "lean_goals", True, None, None),
    ],
)
check(
    "codex fixture summary",
    summarize(calls),
    {"mcp": {"lean_beam/lean_sync": 1, "lean_beam/lean_goals": 1}, "cli": {}, "tool_search": 0, "failed": 0},
)
check("codex fixture lists no servers", transcript_mcp_servers(codex_real), {})

claude_real = FIXTURES / "claude_stream_mcp_2_1.jsonl"
calls = extract_tool_calls(claude_real)
check(
    "claude fixture: ToolSearch is not an MCP call",
    [(c["kind"], c["server"], c["tool"], c["ok"]) for c in calls],
    [
        ("tool_search", None, "ToolSearch", True),
        ("mcp", "lean_beam", "lean_sync", True),
        ("mcp", "lean_beam", "lean_goals", True),
    ],
)
check("claude fixture timestamps", all(c["t"] and c["latency_ms"] is not None for c in calls), True)
check(
    "claude fixture summary",
    summarize(calls),
    {"mcp": {"lean_beam/lean_sync": 1, "lean_beam/lean_goals": 1}, "cli": {}, "tool_search": 1, "failed": 0},
)
check("claude fixture connected servers", transcript_mcp_servers(claude_real), {"lean_beam": "connected"})

# Failure and CLI-wrapper shapes: not in the captures (no call failed), so synthetic; the error shape
# is the Codex `McpToolCallError` `{"message": ...}`.
codex_lines = [
    {"type": "thread.started", "thread_id": "t1"},
    {
        "type": "item.started",
        "item": {"id": "i1", "type": "mcp_tool_call", "server": "lean_beam", "tool": "lean_sync", "arguments": {}},
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i1",
            "type": "mcp_tool_call",
            "server": "lean_beam",
            "tool": "lean_sync",
            "status": "completed",
            "result": {"content": [{"type": "text", "text": "ok"}]},
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i2",
            "type": "mcp_tool_call",
            "server": "lean_beam",
            "tool": "lean_run_at",
            "status": "failed",
            "error": {"message": "contentModified: version 3 is stale"},
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i3",
            "type": "command_execution",
            "command": "/opt/tauceti-bench/bin/tools/loogle.sh 'List.map'",
            "exit_code": 0,
            "aggregated_output": "{}",
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "i4", "type": "command_execution", "command": "lake build", "exit_code": 0},
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i5",
            "type": "command_execution",
            "command": "\"/opt/tauceti-bench/bin/tools/finder.sh\" 'compact image' && echo done",
            "exit_code": 1,
        },
    },
    {"type": "turn.completed", "usage": {"input_tokens": 1}},
]
codex_path = root / "codex.jsonl"
codex_path.write_text("\n".join(json.dumps(e) for e in codex_lines) + "\nnot json\n")
calls = extract_tool_calls(codex_path)
check("codex call count", len(calls), 4)
check(
    "codex mcp records",
    [(c["kind"], c["server"], c["tool"], c["ok"], c["error_kind"]) for c in calls[:2]],
    [("mcp", "lean_beam", "lean_sync", True, None), ("mcp", "lean_beam", "lean_run_at", False, "stale_version")],
)
check(
    "codex cli records",
    [(c["kind"], c["tool"], c["ok"]) for c in calls[2:]],
    [("cli", "loogle", True), ("cli", "finder", False)],
)
check(
    "codex summary",
    summarize(calls),
    {
        "mcp": {"lean_beam/lean_sync": 1, "lean_beam/lean_run_at": 1},
        "cli": {"loogle": 1, "finder": 1},
        "tool_search": 0,
        "failed": 2,
    },
)

claude_lines = [
    {"type": "system", "subtype": "init", "session_id": "s"},
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "u1",
                    "name": "mcp__lean_lsp__lean_goal",
                    "input": {"file_path": "A.lean", "line": 3},
                },
                {
                    "type": "tool_use",
                    "id": "u2",
                    "name": "Bash",
                    "input": {"command": "/opt/tauceti-bench/bin/tools/explore.sh 'bounded'"},
                },
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": [{"type": "text", "text": "⊢ True"}]},
                {
                    "type": "tool_result",
                    "tool_use_id": "u2",
                    "is_error": True,
                    "content": "explore.sh: search service unavailable (curl exit 7)",
                },
            ]
        },
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "u3", "name": "mcp__lean_lsp__lean_multi_attempt", "input": {}},
                {"type": "tool_use", "id": "u4", "name": "Bash", "input": {"command": "lake build"}},
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "u3",
                    "is_error": True,
                    "content": "MCP error -32001: Request timed out",
                }
            ]
        },
    },
    {"type": "result", "subtype": "success"},
]
claude_path = root / "claude.jsonl"
claude_path.write_text("\n".join(json.dumps(e) for e in claude_lines) + "\n")
calls = extract_tool_calls(claude_path)
check(
    "claude records",
    [(c["kind"], c["server"], c["tool"], c["ok"], c["error_kind"]) for c in calls],
    [
        ("mcp", "lean_lsp", "lean_goal", True, None),
        ("cli", None, "explore", False, "error"),
        ("mcp", "lean_lsp", "lean_multi_attempt", False, "timeout"),
    ],
)
check(
    "claude summary",
    summarize(calls),
    {
        "mcp": {"lean_lsp/lean_goal": 1, "lean_lsp/lean_multi_attempt": 1},
        "cli": {"explore": 1},
        "tool_search": 0,
        "failed": 2,
    },
)
check("empty transcript", extract_tool_calls(root / "codex.jsonl") != [], True)

print("tool_mcp: ok")
