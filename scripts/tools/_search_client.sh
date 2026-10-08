#!/usr/bin/env bash
# Shared client for the semantic search services (finder, explore). Invoked by finder.sh and
# explore.sh as `_search_client.sh <tool> [--json] [-k N] <query...>`. Wire protocol: docs/tools.md.
#
#   GET /search?q=<query>&k=<k>  ->  {"tool": ..., "pin": ..., "hits": [{name, kind, module,
#                                     signature, description, score}]}
#
# The service is reached over its Unix socket when present (the sandbox mounts the host's socket
# directory at /run/tauceti-<tool>), else over the loopback URL. The wrapper fails open: when the
# service is unreachable it prints one line to stderr, exits nonzero, and the agent falls back to
# grep and #check.
set -euo pipefail

tool="${1:-}"
if [[ -z "$tool" ]]; then
  echo "usage: _search_client.sh <tool> [--json] [-k N] '<query>'" >&2
  exit 64
fi
shift
TOOL=$(printf '%s' "$tool" | tr '[:lower:]' '[:upper:]')

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ -r "$here/_log.sh" ]]; then
  # shellcheck source=scripts/tools/_log.sh
  source "$here/_log.sh"
else
  tool_log_start() { :; }
  tool_log_write() { :; }
fi

json=0
k=""
while (( $# )); do
  case "$1" in
    --json) json=1; shift ;;
    -k|--k) k="${2:-}"; shift 2 ;;
    -k*) k="${1#-k}"; shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done
if (( $# == 0 )); then
  echo "usage: $tool.sh [--json] [-k N] '<query>'" >&2
  exit 64
fi
query="$*"
k="${k:-${TAUCETI_SEARCH_K:-10}}"
if ! [[ "$k" =~ ^[0-9]+$ ]]; then
  echo "$tool.sh: -k must be a positive integer, got '$k'" >&2
  exit 64
fi

case "$tool" in
  finder)  default_url="http://127.0.0.1:8089/search" ;;
  explore) default_url="http://127.0.0.1:8090/search" ;;
  *)       default_url="http://127.0.0.1:8080/search" ;;
esac
socket_dir_var="TAUCETI_${TOOL}_SOCKET_DIR"
socket_var="TAUCETI_${TOOL}_SOCKET"
url_var="TAUCETI_${TOOL}_URL"
socket_dir="${!socket_dir_var:-/run/tauceti-$tool}"
socket="${!socket_var:-$socket_dir/$tool.sock}"
url="${!url_var:-$default_url}"

# No --retry: a 5xx from the index service (index not loaded, bad query) is not transient, and a
# retried request would also repeat the body into $out.
curl_args=(
  --fail-with-body
  --silent
  --show-error
  --connect-timeout 2
  --max-time 30
  --get
  --data-urlencode "q=$query"
  --data-urlencode "k=$k"
  --header "User-Agent: taucetiworker-$tool/1"
  --header "X-TauCeti-Client: taucetiworker/1"
)

for metadata in \
  "TAUCETI_WORKER_ID:X-TauCeti-Worker" \
  "TAUCETI_PHASE:X-TauCeti-Phase" \
  "TAUCETI_AGENT:X-TauCeti-Agent" \
  "TAUCETI_MODEL:X-TauCeti-Model" \
  "TAUCETI_ROUND_ID:X-TauCeti-Round"; do
  variable="${metadata%%:*}"
  header="${metadata#*:}"
  value="${!variable:-}"
  if [[ -n "$value" ]]; then
    curl_args+=(--header "$header: $value")
  fi
done

if [[ -S "$socket" ]]; then
  curl_args+=(--unix-socket "$socket")
  url="http://localhost/search"
fi

errf=$(mktemp "${TMPDIR:-/tmp}/tauceti-$tool.XXXXXX")
trap 'rm -f "$errf"' EXIT

tool_log_start
set +e
out=$(curl "${curl_args[@]}" "$url" 2>"$errf")
rc=$?
set -e
if (( rc != 0 )); then
  # One line: the service's own error when it sent one, else curl's first complaint.
  detail=$(jq -r '.error // empty' <<<"$out" 2>/dev/null | head -n 1 || true)
  if [[ -z "$detail" ]]; then
    detail=$(head -n 1 "$errf" 2>/dev/null || true)
  fi
  echo "$tool.sh: search service unavailable (curl exit $rc)${detail:+: $detail}" >&2
  tool_log_write "$tool" "$rc" "$query" '[]'
  exit "$rc"
fi

hits=$(jq -c '[.hits[]?.name // empty]' <<<"$out" 2>/dev/null) || hits='[]'
tool_log_write "$tool" 0 "$query" "$hits"

if (( json )); then
  jq -c . <<<"$out"
  exit 0
fi

jq -r '
  if ((.hits // []) | length) == 0 then
    "(no hits)"
  else
    .hits[]
    | "\(.name)  [\(.kind // "?"), \(.module // "?")]"
      + (if (.signature // "") != "" then "\n  \(.signature)" else "" end)
      + (if (.description // "") != "" then "\n  \(.description)" else "" end)
      + "\n"
  end' <<<"$out"
