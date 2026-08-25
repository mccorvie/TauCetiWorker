#!/usr/bin/env python3
"""Trusted worker tools are explicit, phase-scoped, and propagated unchanged."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tauceti_worker.loop as loop
from tauceti_worker.cli import build_parser
from tauceti_worker.tools import add_tool_prompt, bubble_mounts, resolve_tools, stage_tool_scripts
from tauceti_worker.worker_manager import WorkersError, WorkerSpec


def check(label, actual, expected=True):
    if actual != expected:
        raise AssertionError(f"{label}: got {actual!r}, expected {expected!r}")


parsed = build_parser().parse_args(["work", "--tool", "loogle", "--tool", "loogle"])
check("CLI preserves repeat order", parsed.tool, ["loogle", "loogle"])
check("registry preserves selection", resolve_tools(parsed.tool), ("loogle", "loogle"))
try:
    resolve_tools(["shell"])
    raise AssertionError("unknown tool should fail")
except ValueError as exc:
    check("unknown tool diagnostic", "unknown tool" in str(exc))

prompt = add_tool_prompt("task", ("loogle",), "fix", "/trusted/bin")
check("eligible prompt names absolute helper", '"/trusted/bin/tools/loogle.sh" \'<query>\'' in prompt)
check("review does not receive loogle", add_tool_prompt("task", ("loogle",), "review", "/bin"), "task")

rounddir = Path(tempfile.mkdtemp(prefix="tauceti-tools-test-"))
stage_tool_scripts(rounddir, ("loogle",), "roadmap")
check("bubble helper staged", (rounddir / "tools" / "loogle.sh").is_file())
check("bubble helper executable", bool((rounddir / "tools" / "loogle.sh").stat().st_mode & 0o100))

fakebin = rounddir / "fakebin"
fakebin.mkdir()
(fakebin / "curl").write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@"\n')
(fakebin / "jq").write_text("#!/usr/bin/env bash\ncat\n")
for command in (fakebin / "curl", fakebin / "jq"):
    command.chmod(0o755)
wrapper_env = {
    **os.environ,
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

spec = WorkerSpec.from_dict({"id": "mathos", "tools": ["loogle"]}, 0)
check("managed config parses tools", spec.tools, ("loogle",))
check("managed config serializes tools", spec.as_dict()["tools"], ["loogle"])
check("managed worker emits tool flag", spec.work_argv()[-2:], ["--tool", "loogle"])
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
