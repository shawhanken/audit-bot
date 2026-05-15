#!/usr/bin/env bash
# Stop the doc-audit server + tunnel.
#
# Usage: bin/down.sh

set -euo pipefail
source "$(dirname "$0")/_lib.sh"

kill_pid tunnel
kill_pid server
log "all doc-audit services stopped"
