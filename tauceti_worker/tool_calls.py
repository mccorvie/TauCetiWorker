"""Count tool use from an agent's own transcript (docs/tools.md, "Counting tool calls").

CLI tools write `TAUCETI_TOOL_LOG` themselves. MCP servers do not: Codex and Claude own those calls,
so the only record is the provider transcript the worker already keeps (`codex exec --json` JSONL
or Claude `--output-format stream-json`). This module reads either and emits one record per call:

    {"provider", "kind": "mcp" | "cli" | "tool_search", "server", "tool", "ok", "latency_ms",
     "error_kind", "t"}

`kind: "mcp"` is an MCP tool call (`server`/`tool` as the agent saw them); `kind: "cli"` is a shell
invocation of a staged `tools/<name>.sh` wrapper, the cross-check for the wrapper's own log;
`kind: "tool_search"` is Claude's deferred-tool lookup (`ToolSearch`), which loads MCP tool schemas
and is reported apart from the calls themselves.

Shapes, from captured transcripts (tests/fixtures/codex_exec_mcp_0_160.jsonl, claude_stream_mcp_2_1.jsonl):

- Codex `exec --json`: `item.started` then `item.completed`, both with `item.type == "mcp_tool_call"`,
  `item.server`, bare `item.tool`, `item.arguments`, `item.status` (`in_progress` -> `completed`),
  `item.error` (null on success), and on completion `item.result.content[]`. One call is counted per
  `item.completed`. Codex events carry no timestamps, so `t` and `latency_ms` are None.
- Claude `stream-json`: `assistant` events with `message.content[]` `tool_use` blocks named
  `mcp__<server>__<tool>`; the matching `user` event's `tool_result` block (by `tool_use_id`) gives
  `ok` via `is_error`. Events carry an ISO `timestamp`: `t` is the call's, `latency_ms` is the gap
  to its result event (approximate). The `system`/`init` event lists `mcp_servers`
  (`transcript_mcp_servers`).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

CLI_TOOL_RE = re.compile(r"(?<![\w/.-])(?:[\w./-]*/)?tools/([A-Za-z0-9_-]+)\.sh(?![\w.-])")
# Claude Code names an MCP tool `mcp__<server>__<tool>`; server names may themselves contain `_`,
# so split on the LAST `__` only when the first segment is the literal `mcp`.
CLAUDE_MCP_RE = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>[^_].*)$")

_ERROR_KINDS = (
    ("timeout", re.compile(r"timed? ?out|deadline", re.I)),
    ("stale_version", re.compile(r"contentModified|stale|version", re.I)),
    ("unknown_tool", re.compile(r"unknown tool|not found|no such tool", re.I)),
    ("startup", re.compile(r"failed to start|could not (?:start|connect)|initialize", re.I)),
)


def error_kind(message: str | None) -> str | None:
    """A coarse label for a failed call, for aggregation; None for a successful one."""
    if not message:
        return None
    for kind, pattern in _ERROR_KINDS:
        if pattern.search(message):
            return kind
    return "error"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("message"), str):
            return value["message"]
        return json.dumps(value)[:500]
    return "" if value is None else str(value)


def _record(
    provider: str, kind: str, *, server: str | None, tool: str, ok: bool | None, latency_ms=None, error=None, t=None
):
    return {
        "provider": provider,
        "kind": kind,
        "server": server,
        "tool": tool,
        "ok": ok,
        "latency_ms": latency_ms if isinstance(latency_ms, int | float) else None,
        "error_kind": error_kind(error) if ok is False else None,
        "t": t,
    }


def _seconds(stamp: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _codex(event: dict[str, Any], out: list[dict[str, Any]]) -> None:
    if event.get("type") != "item.completed" or not isinstance(event.get("item"), dict):
        return
    item = event["item"]
    kind = item.get("type")
    if kind == "mcp_tool_call":
        error = item.get("error")
        message = error.get("message") if isinstance(error, dict) else (_text(error) if error else None)
        status = str(item.get("status", "completed"))
        ok = status in {"completed", "success", "succeeded"} and not message
        out.append(
            _record(
                "codex",
                "mcp",
                server=str(item.get("server", "?")),
                tool=str(item.get("tool", "?")),
                ok=ok,
                latency_ms=item.get("duration_ms"),
                error=message or (status if not ok else None),
                t=event.get("timestamp") or item.get("timestamp"),
            )
        )
    elif kind == "command_execution":
        command = str(item.get("command", ""))
        exit_code = item.get("exit_code")
        for name in CLI_TOOL_RE.findall(command):
            ok = exit_code == 0 if isinstance(exit_code, int) else None
            out.append(_record("codex", "cli", server=None, tool=name, ok=ok, latency_ms=item.get("duration_ms")))


def _claude(event: dict[str, Any], out: list[dict[str, Any]], pending: dict[str, dict[str, Any]]) -> None:
    kind = event.get("type")
    message = event.get("message") if isinstance(event.get("message"), dict) else None
    if message is None:
        return
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    stamp = event.get("timestamp")
    if kind == "assistant":
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name", ""))
            tool_id = str(block.get("id", ""))
            mcp = CLAUDE_MCP_RE.match(name)
            if mcp or name == "ToolSearch":
                if mcp:
                    rec = _record("claude", "mcp", server=mcp["server"], tool=mcp["tool"], ok=None, t=stamp)
                else:
                    rec = _record("claude", "tool_search", server=None, tool=name, ok=None, t=stamp)
                out.append(rec)
                if tool_id:
                    pending[tool_id] = rec
                continue
            inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
            command = str(inputs.get("command", "")) if name == "Bash" else ""
            names = CLI_TOOL_RE.findall(command)
            for cli_name in names:
                rec = _record("claude", "cli", server=None, tool=cli_name, ok=None, t=stamp)
                out.append(rec)
                if tool_id and len(names) == 1:
                    pending[tool_id] = rec
    elif kind == "user":
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            rec = pending.pop(str(block.get("tool_use_id", "")), None)
            if rec is None:
                continue
            failed = bool(block.get("is_error"))
            rec["ok"] = not failed
            start, end = _seconds(rec["t"]), _seconds(stamp)
            if start is not None and end is not None and end >= start:
                rec["latency_ms"] = round((end - start) * 1000)
            rec["error_kind"] = error_kind(_text(block.get("content"))) if failed else None


def extract_tool_calls(path: str | Path) -> list[dict[str, Any]]:
    """Every tool call in a Codex or Claude transcript, in order. Non-JSON lines are skipped, so the
    worker's rendered logs (which keep raw lines only when no events were recognised) are tolerated
    but yield nothing; point this at the raw JSONL the harness captured."""
    out: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in {"item.completed", "item.started", "item.updated"}:
            _codex(event, out)
        elif kind in {"assistant", "user"}:
            _claude(event, out, pending)
    return out


def summarize(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """`{"mcp": {"<server>/<tool>": n}, "cli": {"<tool>": n}, "tool_search": n, "failed": n}`."""
    mcp: dict[str, int] = {}
    cli: dict[str, int] = {}
    tool_search = 0
    failed = 0
    for call in calls:
        if call["kind"] == "tool_search":
            tool_search += 1
        elif call["kind"] == "mcp":
            key = f"{call['server']}/{call['tool']}"
            mcp[key] = mcp.get(key, 0) + 1
        else:
            cli[call["tool"]] = cli.get(call["tool"], 0) + 1
        if call["ok"] is False:
            failed += 1
    return {"mcp": mcp, "cli": cli, "tool_search": tool_search, "failed": failed}


def transcript_mcp_servers(path: str | Path) -> dict[str, str]:
    """`{server: status}` from a Claude transcript's `system`/`init` event (`mcp_servers`), e.g.
    `{"lean_beam": "connected"}`; empty for a Codex transcript, which lists no servers."""
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
            servers = event.get("mcp_servers")
            return {
                str(s["name"]): str(s.get("status", "?")) for s in servers or [] if isinstance(s, dict) and "name" in s
            }
    return {}


def _cli_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in {"-h", "--help"}:
        print("usage: python -m tauceti_worker.tool_calls <transcript.jsonl>", file=sys.stderr)
        return 64
    calls = extract_tool_calls(args[0])
    for call in calls:
        print(json.dumps(call, separators=(",", ":")))
    print(json.dumps({"summary": summarize(calls)}, separators=(",", ":")), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
