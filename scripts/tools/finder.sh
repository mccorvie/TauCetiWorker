#!/usr/bin/env bash
# finder.sh: Lean Finder semantic search over the frozen Mathlib + canonical Tau Ceti index.
# Usage: finder.sh [--json] [-k N] '<English statement, goal, or partial signature>'
# See docs/tools.md for the service contract and the TAUCETI_TOOL_LOG line this writes.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$here/_search_client.sh" finder "$@"
