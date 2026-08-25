#!/usr/bin/env bash
set -euo pipefail

if (( $# == 0 )); then
  echo "usage: loogle.sh '<loogle query>'" >&2
  exit 64
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

curl "${curl_args[@]}" "$url" | jq -c .
