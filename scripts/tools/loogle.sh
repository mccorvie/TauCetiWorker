#!/usr/bin/env bash
set -euo pipefail

if (( $# == 0 )); then
  echo "usage: loogle.sh '<loogle query>'" >&2
  exit 64
fi

# The log helper is staged next to this script; without it (an older staging), logging is a no-op.
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ -r "$here/_log.sh" ]]; then
  # shellcheck source=scripts/tools/_log.sh
  source "$here/_log.sh"
else
  tool_log_start() { :; }
  tool_log_write() { :; }
fi

query="$*"
socket_dir="${TAUCETI_LOOGLE_SOCKET_DIR:-/run/tauceti-loogle}"
socket="${TAUCETI_LOOGLE_SOCKET:-$socket_dir/loogle.sock}"
url="${TAUCETI_LOOGLE_URL:-http://127.0.0.1:8088/json}"

curl_args=(
  --fail
  --silent
  --show-error
  --connect-timeout 2
  --max-time 20
  --retry 2
  --retry-max-time 12
  --get
  --data-urlencode "q=$query"
  --header "User-Agent: taucetiworker-loogle/1"
  --header "X-Loogle-Client: taucetiworker/1"
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
  url="http://localhost/json"
fi

tool_log_start
set +e
out=$(curl "${curl_args[@]}" "$url")
rc=$?
set -e
if (( rc != 0 )); then
  tool_log_write loogle "$rc" "$query" '[]'
  exit "$rc"
fi

# Loogle answers 200 with {"hits": [...]} on success and {"error": ...} on a parse error.
hits=$(jq -c '[.hits[]?.name // empty]' <<<"$out" 2>/dev/null) || hits='[]'
tool_log_write loogle 0 "$query" "$hits"
jq -c . <<<"$out"
