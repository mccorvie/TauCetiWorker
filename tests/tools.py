#!/usr/bin/env python3
"""Trusted worker tools are explicit, phase-scoped, propagated unchanged, and logged at the boundary.

Covers the registry (names, phases, prompts, mounts), the loop/manager propagation of `--tool`, and
the three CLI wrappers end to end: `loogle.sh` against a fake curl, `finder.sh` and `explore.sh`
against a fake search service on a Unix socket (the shape the bench and Bubble mount in), the
service-down path, and the `TAUCETI_TOOL_LOG` line every wrapper appends (docs/tools.md).
"""

from __future__ import annotations

import json
import os
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tauceti_worker.loop as loop
from tauceti_worker.cli import build_parser
from tauceti_worker.tools import (
    TOOL_LOG_ENV,
    TOOL_REGISTRY,
    add_tool_prompt,
    bubble_mounts,
    resolve_mounts,
    resolve_tools,
    stage_tool_scripts,
)
from tauceti_worker.worker_manager import WorkersError, WorkerSpec


def check(label, actual, expected=True):
    if actual != expected:
        raise AssertionError(f"{label}: got {actual!r}, expected {expected!r}")


# A clean base environment: an operator's own TAUCETI_* settings must not leak into these runs.
BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith("TAUCETI_")}

# ---------------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------------

check("registry names", tuple(TOOL_REGISTRY), ("loogle", "finder", "explore"))
check("log env name is the documented one", TOOL_LOG_ENV, "TAUCETI_TOOL_LOG")

parsed = build_parser().parse_args(["work", "--tool", "loogle", "--tool", "loogle"])
check("CLI preserves repeat order", parsed.tool, ["loogle", "loogle"])
check("registry preserves selection", resolve_tools(parsed.tool), ("loogle", "loogle"))
check(
    "registry accepts every search tool",
    resolve_tools(["finder", "explore", "loogle"]),
    ("finder", "explore", "loogle"),
)
try:
    resolve_tools(["shell"])
    raise AssertionError("unknown tool should fail")
except ValueError as exc:
    check("unknown tool diagnostic", "unknown tool" in str(exc))

prompt = add_tool_prompt("task", ("loogle",), "fix", "/trusted/bin")
check("eligible prompt names absolute helper", "\"/trusted/bin/tools/loogle.sh\" '<query>'" in prompt)
check("review does not receive loogle", add_tool_prompt("task", ("loogle",), "review", "/bin"), "task")

for name in ("finder", "explore"):
    for phase in ("roadmap", "fix", "fix-ci", "rebase", "bump"):
        fragment = add_tool_prompt("task", (name,), phase, "/trusted/bin")
        check(
            f"{name} prompt in {phase} names absolute helper", f"\"/trusted/bin/tools/{name}.sh\" '<query>'" in fragment
        )
        check(f"{name} prompt in {phase} says hits are candidates", "candidates, not confirmations" in fragment)
        check(f"{name} prompt in {phase} says empty is not absence", "not evidence that a lemma is missing" in fragment)
        check(f"{name} prompt in {phase} says fixed commit", "fixed commit" in fragment)
    check(f"review does not receive {name}", add_tool_prompt("task", (name,), "review", "/bin"), "task")
check("no leaked placeholder", "__BIN__" not in add_tool_prompt("task", ("loogle", "finder", "explore"), "fix", "/b"))

# ---------------------------------------------------------------------------------------------------
# Staging: a staged bin directory is self-contained (the helpers a wrapper sources travel with it)
# ---------------------------------------------------------------------------------------------------

rounddir = Path(tempfile.mkdtemp(prefix="tauceti-tools-test-"))
stage_tool_scripts(rounddir, ("loogle",), "roadmap")
check("bubble helper staged", (rounddir / "tools" / "loogle.sh").is_file())
check("bubble helper executable", bool((rounddir / "tools" / "loogle.sh").stat().st_mode & 0o100))
check("loogle stages the log helper", (rounddir / "tools" / "_log.sh").is_file())
check("finder not staged when not selected", not (rounddir / "tools" / "finder.sh").exists())

staged = Path(tempfile.mkdtemp(prefix="tauceti-tools-staged-"))
stage_tool_scripts(staged, ("finder", "explore"), "fix")
for helper in ("finder.sh", "explore.sh", "_search_client.sh", "_log.sh"):
    check(f"{helper} staged for the search tools", (staged / "tools" / helper).is_file())
    check(f"{helper} staged executable", bool((staged / "tools" / helper).stat().st_mode & 0o100))
review_dir = Path(tempfile.mkdtemp(prefix="tauceti-tools-review-"))
stage_tool_scripts(review_dir, ("loogle", "finder", "explore"), "review")
check("review stages nothing", sorted(review_dir.iterdir()), [])

# ---------------------------------------------------------------------------------------------------
# loogle.sh: query joining, metadata headers, log line
# ---------------------------------------------------------------------------------------------------

fakebin = rounddir / "fakebin"
fakebin.mkdir()
(fakebin / "curl").write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
(fakebin / "jq").write_text("#!/usr/bin/env bash\ncat\n")
for command in (fakebin / "curl", fakebin / "jq"):
    command.chmod(0o755)
wrapper_env = {
    **BASE_ENV,
    "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
    "TAUCETI_LOOGLE_SOCKET": str(rounddir / "missing.sock"),
    "TAUCETI_LOOGLE_URL": "http://loogle.invalid/json",
    "TAUCETI_WORKER_ID": "mathos",
    "TAUCETI_PHASE": "fix",
    "TAUCETI_AGENT": "codex",
    "TAUCETI_MODEL": "test-model",
    "TAUCETI_ROUND_ID": "round-123",
}
wrapped = subprocess.run(
    [REPO / "scripts" / "tools" / "loogle.sh", "?a", "→", "?b"],
    env=wrapper_env,
    capture_output=True,
    text=True,
    check=True,
).stdout
check("wrapper joins query arguments", "q=?a → ?b" in wrapped)
for header in (
    "X-TauCeti-Worker: mathos",
    "X-TauCeti-Phase: fix",
    "X-TauCeti-Agent: codex",
    "X-TauCeti-Model: test-model",
    "X-TauCeti-Round: round-123",
):
    check(f"wrapper sends {header}", header in wrapped)

# The log line: a fake curl answering with Loogle's real JSON shape, the real jq, a log path set.
jsonbin = rounddir / "jsonbin"
jsonbin.mkdir()
(jsonbin / "curl").write_text(
    "#!/usr/bin/env bash\n"
    'printf \'%s\\n\' \'{"hits":[{"name":"List.map","module":"Init.Prelude","type":" (f : α → β) (l : List α) : List β","doc":"d"},'
    '{"name":"List.map_map","module":"Init.Data.List.Lemmas","type":"...","doc":""}],"count":2,"header":"Found 2"}\'\n'
)
(jsonbin / "curl").chmod(0o755)
loogle_log = rounddir / "loogle.jsonl"
loogle_env = {**wrapper_env, "PATH": f"{jsonbin}:{os.environ.get('PATH', '')}", TOOL_LOG_ENV: str(loogle_log)}
out = subprocess.run(
    [REPO / "scripts" / "tools" / "loogle.sh", "List.map"], env=loogle_env, capture_output=True, text=True, check=True
)
check("loogle output stays compact JSON", json.loads(out.stdout)["count"], 2)
check("loogle output is one line", out.stdout.count("\n"), 1)
loogle_lines = loogle_log.read_text().splitlines()
check("loogle writes exactly one log line", len(loogle_lines), 1)
entry = json.loads(loogle_lines[0])
check("loogle log keys", sorted(entry), ["args", "at", "hits", "ms", "phase", "rc", "round", "tool"])
check("loogle log tool", entry["tool"], "loogle")
check("loogle log rc", entry["rc"], 0)
check("loogle log args", entry["args"], "List.map")
check("loogle log hits", entry["hits"], ["List.map", "List.map_map"])
check("loogle log round", entry["round"], "round-123")
check("loogle log phase", entry["phase"], "fix")
check("loogle log ms is a non-negative int", isinstance(entry["ms"], int) and entry["ms"] >= 0)
check("loogle log at is UTC ISO-8601", bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["at"])))

# Unset log variable: no file appears, output unchanged.
quiet_env = {k: v for k, v in loogle_env.items() if k != TOOL_LOG_ENV}
subprocess.run(
    [REPO / "scripts" / "tools" / "loogle.sh", "List.map"], env=quiet_env, capture_output=True, text=True, check=True
)
check("no log without the variable", len(loogle_log.read_text().splitlines()), 1)

# An unwritable log path never changes the wrapper's result.
broken_env = {**loogle_env, TOOL_LOG_ENV: str(rounddir / "no-such-dir" / "log.jsonl")}
out = subprocess.run(
    [REPO / "scripts" / "tools" / "loogle.sh", "List.map"], env=broken_env, capture_output=True, text=True
)
check("unwritable log keeps rc 0", out.returncode, 0)
check("unwritable log keeps stdout", json.loads(out.stdout)["count"], 2)
check("unwritable log keeps stderr empty", out.stderr, "")

# ---------------------------------------------------------------------------------------------------
# finder.sh / explore.sh: a fake search service on a Unix socket, per the wire protocol
# ---------------------------------------------------------------------------------------------------

HITS = [
    {
        "name": "IsCompact.image",
        "kind": "theorem",
        "module": "Mathlib.Topology.Compactness.Compact",
        "signature": "(hs : IsCompact s) (hf : Continuous f) : IsCompact (f '' s)",
        "description": "The continuous image of a compact set is compact.",
        "score": 0.91,
    },
    {
        "name": "TauCeti.Topology.foo",
        "kind": "def",
        "module": "TauCeti.Topology.Foo",
        "signature": "(X : Type*) [TopologicalSpace X] : Prop",
        "description": "",
        "score": 0.42,
    },
]
seen: list[dict] = []


class FakeSearch(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def address_string(self):  # AF_UNIX peers have no (host, port)
        return "unix"

    def log_message(self, *_):
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        seen.append({"path": url.path, "query": query, "headers": dict(self.headers)})
        if url.path == "/health":
            self._send(200, {"ok": True, "tool": "fake", "manifest": {"pin": "b3-11bc1333"}})
        elif url.path == "/search":
            if query.get("q") == "boom":
                self._send(500, {"error": "index not loaded"})
            else:
                k = int(query.get("k", "10"))
                self._send(200, {"tool": "fake", "pin": "b3-11bc1333", "hits": HITS[:k]})
        else:
            self._send(404, {"error": "no such route"})


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


socket_root = Path(tempfile.mkdtemp(prefix="tcs-"))
services: list[UnixServer] = []
for tool in ("finder", "explore"):
    (socket_root / f"tauceti-{tool}").mkdir()
    server = UnixServer(str(socket_root / f"tauceti-{tool}" / f"{tool}.sock"), FakeSearch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    services.append(server)

try:
    for tool in ("finder", "explore"):
        log_path = socket_root / f"{tool}.jsonl"
        env = {
            **BASE_ENV,
            f"TAUCETI_{tool.upper()}_SOCKET_DIR": str(socket_root / f"tauceti-{tool}"),
            f"TAUCETI_{tool.upper()}_URL": "http://search.invalid/search",
            TOOL_LOG_ENV: str(log_path),
            "TAUCETI_WORKER_ID": "mathos",
            "TAUCETI_PHASE": "fix",
            "TAUCETI_AGENT": "codex",
            "TAUCETI_MODEL": "test-model",
            "TAUCETI_ROUND_ID": "round-123",
        }
        script = REPO / "scripts" / "tools" / f"{tool}.sh"

        # Readable output.
        seen.clear()
        out = subprocess.run([script, "continuous", "image", "of", "compact"], env=env, capture_output=True, text=True)
        check(f"{tool} readable rc", out.returncode, 0)
        check(f"{tool} readable stderr empty", out.stderr, "")
        check(
            f"{tool} readable names the hit",
            "IsCompact.image  [theorem, Mathlib.Topology.Compactness.Compact]" in out.stdout,
        )
        check(
            f"{tool} readable shows the signature",
            "  (hs : IsCompact s) (hf : Continuous f) : IsCompact (f '' s)" in out.stdout,
        )
        check(
            f"{tool} readable shows the description",
            "  The continuous image of a compact set is compact." in out.stdout,
        )
        check(
            f"{tool} readable omits empty descriptions",
            "TauCeti.Topology.foo  [def, TauCeti.Topology.Foo]\n  (X : Type*) [TopologicalSpace X] : Prop\n\n"
            in out.stdout,
        )
        check(f"{tool} readable has no score", "0.91" not in out.stdout)
        check(f"{tool} queried /search", seen[0]["path"], "/search")
        check(f"{tool} joined the query", seen[0]["query"]["q"], "continuous image of compact")
        check(f"{tool} default k", seen[0]["query"]["k"], "10")
        for header, value in (
            ("X-TauCeti-Worker", "mathos"),
            ("X-TauCeti-Phase", "fix"),
            ("X-TauCeti-Agent", "codex"),
            ("X-TauCeti-Model", "test-model"),
            ("X-TauCeti-Round", "round-123"),
            ("X-TauCeti-Client", "taucetiworker/1"),
            ("User-Agent", f"taucetiworker-{tool}/1"),
        ):
            check(f"{tool} sends {header}", seen[0]["headers"].get(header), value)

        # --json passthrough and -k.
        seen.clear()
        out = subprocess.run(
            [script, "--json", "-k", "1", "compact image"], env=env, capture_output=True, text=True, check=True
        )
        payload = json.loads(out.stdout)
        check(f"{tool} --json passes the response through", payload["hits"], HITS[:1])
        check(f"{tool} --json is one line", out.stdout.count("\n"), 1)
        check(f"{tool} -k reaches the service", seen[0]["query"]["k"], "1")

        # TAUCETI_SEARCH_K.
        seen.clear()
        subprocess.run([script, "x"], env={**env, "TAUCETI_SEARCH_K": "3"}, capture_output=True, text=True, check=True)
        check(f"{tool} TAUCETI_SEARCH_K reaches the service", seen[0]["query"]["k"], "3")

        # Empty result.
        out = subprocess.run(
            [script, "--json", "-k", "0", "nothing"], env=env, capture_output=True, text=True, check=True
        )
        check(f"{tool} empty --json", json.loads(out.stdout)["hits"], [])
        out = subprocess.run([script, "-k", "0", "nothing"], env=env, capture_output=True, text=True, check=True)
        check(f"{tool} empty readable", out.stdout.strip(), "(no hits)")

        # Service error with a body: one stderr line carrying the service's message, nonzero exit.
        out = subprocess.run([script, "boom"], env=env, capture_output=True, text=True)
        check(f"{tool} service error rc nonzero", out.returncode != 0)
        check(f"{tool} service error stdout empty", out.stdout, "")
        check(f"{tool} service error one stderr line", out.stderr.count("\n"), 1)
        check(f"{tool} service error names the cause", "index not loaded" in out.stderr)

        # No usage, no service call.
        out = subprocess.run([script], env=env, capture_output=True, text=True)
        check(f"{tool} usage exits 64", out.returncode, 64)

        # The log: one line per invocation, in order, with hits by name and rc from the call.
        lines = [json.loads(line) for line in log_path.read_text().splitlines()]
        check(f"{tool} one log line per call", len(lines), 6)
        check(f"{tool} log keys", sorted(lines[0]), ["args", "at", "hits", "ms", "phase", "rc", "round", "tool"])
        check(f"{tool} log tool", {line["tool"] for line in lines}, {tool})
        check(f"{tool} log args", lines[0]["args"], "continuous image of compact")
        check(f"{tool} log hits", lines[0]["hits"], ["IsCompact.image", "TauCeti.Topology.foo"])
        check(f"{tool} log hits for -k 1", lines[1]["hits"], ["IsCompact.image"])
        check(f"{tool} log empty hits", lines[3]["hits"], [])
        check(f"{tool} log error rc", lines[5]["rc"] != 0)
        check(f"{tool} log error hits", lines[5]["hits"], [])
        check(f"{tool} log round and phase", (lines[0]["round"], lines[0]["phase"]), ("round-123", "fix"))
        check(f"{tool} log ms ints", all(isinstance(line["ms"], int) and line["ms"] >= 0 for line in lines))

        # Service down: no socket, a refused loopback port. One stderr line, nonzero, no traceback, logged.
        down_log = socket_root / f"{tool}-down.jsonl"
        down_env = {
            **env,
            f"TAUCETI_{tool.upper()}_SOCKET_DIR": str(socket_root / "empty"),
            f"TAUCETI_{tool.upper()}_URL": "http://127.0.0.1:1/search",
            TOOL_LOG_ENV: str(down_log),
        }
        out = subprocess.run([script, "anything"], env=down_env, capture_output=True, text=True)
        check(f"{tool} down rc nonzero", out.returncode != 0)
        check(f"{tool} down stdout empty", out.stdout, "")
        check(f"{tool} down one stderr line", out.stderr.count("\n"), 1)
        check(f"{tool} down names the tool", out.stderr.startswith(f"{tool}.sh: search service unavailable"))
        check(f"{tool} down no traceback", "Traceback" not in out.stderr)
        down = json.loads(down_log.read_text())
        check(f"{tool} down logged rc", down["rc"], out.returncode)
        check(f"{tool} down logged hits", down["hits"], [])

        # The staged copy works on its own: the helpers it sources were staged beside it.
        out = subprocess.run(
            [staged / "tools" / f"{tool}.sh", "--json", "staged"], env=env, capture_output=True, text=True
        )
        check(f"{tool} staged copy rc", out.returncode, 0)
        check(f"{tool} staged copy answers", json.loads(out.stdout)["pin"], "b3-11bc1333")
finally:
    for server in services:
        server.shutdown()
        server.server_close()

# ---------------------------------------------------------------------------------------------------
# Mounts: each tool gets only its own socket directory, in a fixed place, only in eligible phases
# ---------------------------------------------------------------------------------------------------

socket_dir = Path(tempfile.mkdtemp(prefix="tauceti-loogle-socket-"))
old_socket_dir = os.environ.get("TAUCETI_LOOGLE_SOCKET_DIR")
os.environ["TAUCETI_LOOGLE_SOCKET_DIR"] = str(socket_dir)
try:
    check(
        "loogle gets only its socket directory",
        bubble_mounts(("loogle",), "fix"),
        [f"{socket_dir.resolve()}:/run/tauceti-loogle:ro"],
    )
    check("ineligible phase gets no mount", bubble_mounts(("loogle",), "review"), [])
finally:
    if old_socket_dir is None:
        os.environ.pop("TAUCETI_LOOGLE_SOCKET_DIR", None)
    else:
        os.environ["TAUCETI_LOOGLE_SOCKET_DIR"] = old_socket_dir

mount_env = {
    "TAUCETI_FINDER_SOCKET_DIR": str(socket_root / "tauceti-finder"),
    "TAUCETI_EXPLORE_SOCKET_DIR": str(socket_root / "tauceti-explore"),
    "TAUCETI_LOOGLE_SOCKET_DIR": str(socket_root / "does-not-exist"),
}
check(
    "resolve_mounts gives (source, target) per enabled tool with an existing directory",
    resolve_mounts(("loogle", "finder", "explore"), "fix", mount_env),
    [
        ((socket_root / "tauceti-finder").resolve(), "/run/tauceti-finder"),
        ((socket_root / "tauceti-explore").resolve(), "/run/tauceti-explore"),
    ],
)
check("resolve_mounts respects phases", resolve_mounts(("finder",), "review", mount_env), [])
check("resolve_mounts with nothing set", resolve_mounts(("finder", "explore"), "fix", {}), [])

# ---------------------------------------------------------------------------------------------------
# Managed workers and the loop propagate the selection unchanged
# ---------------------------------------------------------------------------------------------------

spec = WorkerSpec.from_dict({"id": "mathos", "tools": ["loogle"]}, 0)
check("managed config parses tools", spec.tools, ("loogle",))
check("managed config serializes tools", spec.as_dict()["tools"], ["loogle"])
check("managed worker emits tool flag", spec.work_argv()[-2:], ["--tool", "loogle"])
spec = WorkerSpec.from_dict({"id": "mathos", "tools": ["loogle", "finder", "explore"]}, 0)
check(
    "managed worker emits every tool flag",
    spec.work_argv()[-6:],
    ["--tool", "loogle", "--tool", "finder", "--tool", "explore"],
)
try:
    WorkerSpec.from_dict({"id": "mathos", "tools": ["shell"]}, 0)
    raise AssertionError("managed unknown tool should fail")
except WorkersError:
    pass

# A loop reconstructs child argv explicitly, so verify the selected list crosses that boundary intact.
captured = {}
real_budget = loop.github_budget
real_round = loop.run_round_subprocess
loop.github_budget = lambda: {}


def capture_round(tail):
    captured["tail"] = tail
    raise KeyboardInterrupt


loop.run_round_subprocess = capture_round
try:
    rc = loop.cmd_loop(
        SimpleNamespace(
            ignore_quota=False,
            bubble=False,
            quota_cmd=None,
            tool=["loogle", "loogle"],
            author_model=None,
            author_effort=None,
            account=None,
            source=None,
        ),
        SimpleNamespace(wid="mathos"),
        only=["fix"],
        agent="kiro",
    )
finally:
    loop.github_budget = real_budget
    loop.run_round_subprocess = real_round
check("interrupted loop exits cleanly", rc, 130)
tool_positions = [i for i, item in enumerate(captured["tail"]) if item == "--tool"]
check("loop preserves both tool selections", [captured["tail"][i + 1] for i in tool_positions], ["loogle", "loogle"])

print("tools: ok")
