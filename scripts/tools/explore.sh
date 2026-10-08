#!/usr/bin/env bash
# explore.sh: LeanExplore semantic search over the frozen Mathlib + canonical Tau Ceti index.
# Usage: explore.sh [--json] [-k N] '<English statement or guessed name fragment>'
# See docs/tools.md for the service contract and the TAUCETI_TOOL_LOG line this writes.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$here/_search_client.sh" explore "$@"
