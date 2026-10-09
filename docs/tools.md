# Worker tools

`--tool NAME` (or `tools = [...]` in `workers.toml`) gives a work agent a trusted, explicitly
enabled capability. Nothing is enabled by default, unknown names fail before dispatch, and the
selected list is propagated unchanged through `--loop` child rounds and managed workers. This is
separate from `--source`, which supplies untrusted mathematical material: a tool is executable
code TauCetiWorker ships and vouches for.

Enabling a tool does two things: it stages the tool's scripts where the agent can run them, and it
appends a short prompt fragment telling the agent the tool exists and how to call it. Tools are
phase-scoped. The search tools below are enabled in every code-writing phase (`roadmap`, `fix`,
`fix-ci`, `rebase`, `bump`) and never in `review`: a reviewer with better search finds more
duplicates, which would confound any comparison of authoring reuse.

## The registry

`tauceti_worker/tools.py` holds one `ToolDefinition` per tool: the scripts it stages (the wrapper
plus any helper it sources), the environment variables it may read, the host directories it needs
mounted into a sandbox and where, the host commands `tauceti doctor` checks for, its prompt
fragment, and, for an MCP tool, the server to inject (`mcp`) and the host directories a sandbox must
bind at the same path (`host_binds`). Harnesses other than the worker (the benchmark runner) drive
their setup from these fields rather than special-casing names: `enabled_tools`,
`stage_tool_scripts`, `resolve_mounts`, `resolve_host_binds`, `forwarded_environment`,
`add_tool_prompt`, `agent_tool_argv`, `preflight_tools`.

| Name | What the agent gets | Service it talks to |
| --- | --- | --- |
| `loogle` | `tools/loogle.sh '<query>'`: type-directed search over Mathlib and canonical Tau Ceti main | A Loogle process on `/run/tauceti-loogle/loogle.sock` (or `TAUCETI_LOOGLE_URL`) |
| `finder` | `tools/finder.sh [--json] [-k N] '<query>'`: Lean Finder semantic search; English, a goal, or a partial signature | `/run/tauceti-finder/finder.sock` (or `TAUCETI_FINDER_URL`) |
| `explore` | `tools/explore.sh [--json] [-k N] '<query>'`: LeanExplore semantic search; English or a guessed name fragment | `/run/tauceti-explore/explore.sock` (or `TAUCETI_EXPLORE_URL`) |
| `beam` | The `lean_beam` MCP server: `lean_sync`, `lean_run_at`, `lean_goals`, `lean_save`, … | `$TAUCETI_BEAM_MCP`, a [Lean Beam](https://github.com/leanprover/lean-beam) install under `$TAUCETI_TOOLS_ROOT` |
| `leanlsp` | The `lean_lsp` MCP server: `lean_goal`, `lean_diagnostic_messages`, `lean_multi_attempt`, … | `$TAUCETI_LEAN_LSP_MCP`, a [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) install under `$TAUCETI_TOOLS_ROOT` |

Every service indexes Mathlib plus canonical Tau Ceti main at one fixed commit. None sees
branch-local changes, which the prompt fragments say, along with: hits are candidates to confirm
with `grep` or `#check`, and an empty result is not evidence that a lemma is missing.

The wrappers fail open. When a service is down, the wrapper prints one line to stderr, exits
nonzero, and the agent falls back to `grep` and `#check`; the round continues.

## Where the pieces run

A wrapper is a `curl` client. On the host it runs from `scripts/tools/` and reaches the service over
its Unix socket if present, else over loopback. In Bubble the same script is staged under
`/opt/round/tools/` and the service's socket directory is mounted read-only at its fixed
`/run/tauceti-<tool>` path (`TAUCETI_<TOOL>_SOCKET_DIR` names the host directory). The benchmark
runner's bwrap sandbox does the same with `resolve_mounts`. A wrapper never needs network access,
credentials, or model weights: everything neural runs in the host service.

The services themselves are not part of TauCetiWorker. They are built and run separately (the
`workboots` repository holds the Loogle, Lean Finder, and LeanExplore service definitions), each
as a `systemd --user` unit with a socket under `$XDG_RUNTIME_DIR/tauceti-<tool>/`.

Every wrapper forwards round identity as request headers when the variables are set:
`X-TauCeti-Worker`, `X-TauCeti-Phase`, `X-TauCeti-Agent`, `X-TauCeti-Model`, `X-TauCeti-Round`
(from `TAUCETI_WORKER_ID`, `TAUCETI_PHASE`, `TAUCETI_AGENT`, `TAUCETI_MODEL`, `TAUCETI_ROUND_ID`).

## Tool-call log (`TAUCETI_TOOL_LOG`)

Set by the harness to a file path; unset means no logging. Each wrapper appends exactly one JSON
line per invocation, after the call, and never fails because of logging (an unwritable path, a full
disk, or a malformed hit list changes neither its exit code nor its output):

```json
{"tool":"finder","at":"2026-10-08T21:14:03Z","ms":412,"rc":0,"args":"continuous image of compact is compact","hits":["IsCompact.image"],"round":"<TAUCETI_ROUND_ID>","phase":"<TAUCETI_PHASE>"}
```

| Key | Meaning |
| --- | --- |
| `tool` | `loogle`, `finder`, or `explore` |
| `at` | UTC, ISO-8601 to the second, written after the call |
| `ms` | wall time of the service request |
| `rc` | the wrapper's exit code (curl's code when the service was unreachable or answered an error) |
| `args` | the query string, arguments joined with single spaces |
| `hits` | declaration names returned, in order; empty on error |
| `round`, `phase` | `TAUCETI_ROUND_ID` and `TAUCETI_PHASE`, or empty |

The helper is `scripts/tools/_log.sh`, sourced by every wrapper and staged beside it. Host rounds
inherit `TAUCETI_TOOL_LOG` from the operator's environment. Bubble rounds do not receive it: the
staged scripts live on a read-only mount and no round provides a writable log mount yet.

## Search service wire protocol (`finder`, `explore`)

Loogle keeps its own API (`GET /json?q=`). The semantic search services share one:

- Socket: `$XDG_RUNTIME_DIR/tauceti-<tool>/<tool>.sock` on the host, mounted at
  `/run/tauceti-<tool>/` in a sandbox. Optional loopback fallback: finder on `127.0.0.1:8089`,
  explore on `127.0.0.1:8090` (`TAUCETI_<TOOL>_URL` overrides the whole URL).
- `GET /health` → `{"ok": true, "tool": "finder", "manifest": {...}}`, where `manifest` is the
  service's `MANIFEST.json` (index pin, model revisions, file hashes), so a harness can record
  exactly what was searched.
- `GET /search?q=<query>&k=<n>` →
  `{"tool": "finder", "pin": "b3-11bc1333", "hits": [{"name", "kind", "module", "signature", "description", "score"}]}`.
  `description` may be empty. Errors are HTTP 4xx/5xx with a body `{"error": "..."}`; the
  wrapper repeats that message on its one stderr line.
- Deterministic for a fixed index: no stochastic reranking and no network calls at query time.

The wrapper prints hits as text, one block per hit:

```
IsCompact.image  [theorem, Mathlib.Topology.Compactness.Compact]
  (hs : IsCompact s) (hf : Continuous f) : IsCompact (f '' s)
  The continuous image of a compact set is compact.
```

`--json` passes the service's response through unchanged. `-k N` (or `TAUCETI_SEARCH_K`, default
10) sets the number of hits; `(no hits)` is printed for an empty result.

## Lean LSP tools (`beam`, `leanlsp`)

The two LSP tools are MCP servers rather than CLIs: the agent calls them as tools, and they keep a
Lean server with Mathlib loaded for the whole round, which is what makes a goal query or a tactic
probe cheap. Lean Beam (`leanprover/lean-beam`) is the primary candidate: speculative `lean_run_at`
probes against a saved file, `lean_sync` as a readiness barrier instead of `lake build`, and
`lean_save` module checkpoints. lean-lsp-mcp (`oOo0oOo/lean-lsp-mcp`) is the comparison arm and
the fallback: `lean_goal`, `lean_diagnostic_messages`, `lean_multi_attempt`. Each arm enables one
of them.

**Injection, not configuration.** The worker never writes an agent config file. `agent_tool_argv`
turns each enabled server into per-invocation flags, which `host_agent_argv` appends:

- Codex: dotted overrides, `-c 'mcp_servers.lean_beam.command="…"'`,
  `.env={LEAN_PROJECT_PATH="<checkout>",…}`, `.env_vars=["ELAN_HOME","LAKE_CACHE_DIR",…]` (Codex
  starts stdio servers with a minimal environment, so the toolchain store and Lake cache settings
  the Lean server needs are whitelisted explicitly), `.startup_timeout_sec=60`,
  `.tool_timeout_sec=600`, `.required=true` (a server that cannot start fails the run instead of
  silently running without it), and for Beam `.supports_parallel_tool_calls=true`. `codex exec
  --help` documents the dotted path and TOML value; `codex mcp list` with the same flags shows the
  resulting server.
- Claude Code: one `mcp-servers.json` under the round directory (`{"mcpServers": {...}}`, stdio
  entries with `command`, `args`, `env`) passed as `--mcp-config <file> --strict-mcp-config`, so
  the operator's own MCP servers never reach a round.
- Kiro and the OpenRouter `pi` runner have no per-invocation switch; they get a logged warning and
  no server.

The executable comes from `TAUCETI_BEAM_MCP` / `TAUCETI_LEAN_LSP_MCP`, defaulting under
`TAUCETI_TOOLS_ROOT` (`~/.local/opt/tauceti-tools`, the layout workboots' `local-lsp/install-*.sh`
produce). `{{checkout}}` in a server's environment and in its prompt fragment is rendered to the
absolute path of the Lean project the agent works in (the host checkout; the bench's task repo).

**Fail loud.** A CLI tool fails open; an MCP tool must not, or a treatment arm whose server never
started would silently become a control arm while the prompt still advertises the tool. So
`preflight_tools` runs before dispatch (in `tauceti work` and when a `workers.toml` definition is
validated) and stops the round when the executable is missing or not executable. The same
preflight rejects an MCP tool combined with `--bubble`: Bubble's inner agent command is a frozen
contract that does not yet carry these flags, so MCP tools are **host and benchmark only** for now.

**Sandboxes.** Everything a server needs must be visible with no network and no home directory:
`resolve_host_binds` lists the host directories (the tools root and `ELAN_HOME`) a bwrap sandbox
binds read-only at the same path; the checkout with its `.lake` is already there, and the
Lake/Mathlib cache variables reach the server through `env_vars`.
Beam's wrapper resolves its runtime relative to its own path; lean-lsp-mcp is installed on the
system python so its venv needs only `/usr` and the tools root. lean-lsp-mcp's remote search tools
(`lean_leansearch`, `lean_loogle`, `lean_leanfinder`, `lean_hammer_premise`) are disabled through
`LEAN_MCP_DISABLED_TOOLS`, so an arm never reaches unpinned, current Mathlib and never spends a
turn on a tool that cannot work offline.

**Counting tool calls.** MCP calls do not pass through a wrapper, so they are not in
`TAUCETI_TOOL_LOG`. `tauceti_worker.tool_calls.extract_tool_calls(transcript)` reads the raw
provider transcript (Codex `exec --json` JSONL, Claude `stream-json`) and emits one record per
call, `{provider, kind: mcp|cli, server, tool, ok, latency_ms, error_kind, t}`, counting both MCP
calls and shell invocations of `tools/<name>.sh` (the cross-check for the wrapper log);
`summarize` folds them into per-tool counts. `python -m tauceti_worker.tool_calls <file>` prints
them. Neither transcript format carries per-call timestamps or durations in the observed versions,
so those fields are null until a provider adds them.

| Provider | Search CLIs | MCP tools |
| --- | --- | --- |
| Codex (host, bench) | yes | yes, `-c mcp_servers.*` |
| Claude (host, bench) | yes | yes, `--mcp-config` + `--strict-mcp-config` |
| Kiro, deepseek, minimax | yes | no (warning logged) |
| Any agent under `--bubble` | yes | no (rejected before dispatch) |

## `tauceti doctor`

Doctor lists each registered tool under "optional worker tools" as `ok` when its scripts are
packaged and its host commands (`curl`, `jq`) exist, or, for an MCP tool, when its server
executable resolves (the path is shown). It does not probe the search services: a missing
optional service never fails doctor, and a round with a CLI tool enabled still runs, with the
wrapper failing open. A round with an MCP tool enabled and no executable does not start.
