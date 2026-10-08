#!/usr/bin/env bash
# Sourced by the tool wrappers: the TAUCETI_TOOL_LOG contract (docs/tools.md).
#
# When TAUCETI_TOOL_LOG names a file, a wrapper appends exactly one JSON line per invocation,
# after its call completes:
#
#   {"tool":"finder","at":"2026-10-08T21:14:03Z","ms":412,"rc":0,"args":"<query>",
#    "hits":["IsCompact.image"],"round":"<TAUCETI_ROUND_ID>","phase":"<TAUCETI_PHASE>"}
#
# Unset means no logging. Logging never changes the wrapper's exit code or output: every failure
# here is swallowed, so a missing directory, a full disk, or a malformed hits list costs the
# agent nothing. Needs only bash, jq, and date.

_tool_log_now_ms() {
  if [[ -n "${EPOCHREALTIME:-}" ]]; then          # bash 5: seconds with a fractional part
    local t="${EPOCHREALTIME/[.,]/}"               # locale may print a comma
    printf '%s\n' "${t:0:${#t}-3}"                 # microseconds -> milliseconds
  else
    local n
    n=$(date +%s%N 2>/dev/null || true)
    if [[ "$n" =~ ^[0-9]+$ ]]; then
      printf '%s\n' $(( n / 1000000 ))
    else                                           # BSD date has no %N
      printf '%s\n' $(( $(date +%s) * 1000 ))
    fi
  fi
}

# tool_log_start: remember when the call began. Call once, before the service request.
tool_log_start() {
  _tool_log_t0=$(_tool_log_now_ms)
}

# tool_log_write <tool> <rc> <args> [<hits as a JSON array of names>]
tool_log_write() {
  [[ -n "${TAUCETI_TOOL_LOG:-}" ]] || return 0
  local tool="$1" rc="$2" args="$3" hits="${4:-[]}" ms=0 t1
  {
    t1=$(_tool_log_now_ms)
    if [[ -n "${_tool_log_t0:-}" ]]; then ms=$(( t1 - _tool_log_t0 )); fi
    # A hits list that is not a JSON array of strings is logged as empty rather than breaking the line.
    hits=$(jq -c 'if type == "array" then map(tostring) else [] end' <<<"$hits" 2>/dev/null) || hits='[]'
    jq -cn \
      --arg tool "$tool" \
      --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --argjson ms "$ms" \
      --argjson rc "$rc" \
      --arg args "$args" \
      --argjson hits "$hits" \
      --arg round "${TAUCETI_ROUND_ID:-}" \
      --arg phase "${TAUCETI_PHASE:-}" \
      '{tool: $tool, at: $at, ms: $ms, rc: $rc, args: $args, hits: $hits, round: $round, phase: $phase}' \
      >> "$TAUCETI_TOOL_LOG"
  } 2>/dev/null || true
  return 0
}
