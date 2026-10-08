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
mounted into a sandbox and where, the host commands `tauceti doctor` checks for, and its prompt
fragment. Harnesses other than the worker (the benchmark runner) drive their setup from these
fields rather than special-casing names: `enabled_tools`, `stage_tool_scripts`, `resolve_mounts`,
`forwarded_environment`, `add_tool_prompt`.

| Name | What the agent gets | Service it talks to |
| --- | --- | --- |
| `loogle` | `tools/loogle.sh '<query>'`: type-directed search over Mathlib and canonical Tau Ceti main | A Loogle process on `/run/tauceti-loogle/loogle.sock` (or `TAUCETI_LOOGLE_URL`) |
| `finder` | `tools/finder.sh [--json] [-k N] '<query>'`: Lean Finder semantic search; English, a goal, or a partial signature | `/run/tauceti-finder/finder.sock` (or `TAUCETI_FINDER_URL`) |
| `explore` | `tools/explore.sh [--json] [-k N] '<query>'`: LeanExplore semantic search; English or a guessed name fragment | `/run/tauceti-explore/explore.sock` (or `TAUCETI_EXPLORE_URL`) |

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

## `tauceti doctor`

Doctor lists each registered tool under "optional worker tools" as `ok` when its scripts are
packaged and its host commands (`curl`, `jq`) exist. It does not probe the services: a missing
optional service never fails doctor, and a round with the tool enabled still runs, with the
wrapper failing open.
