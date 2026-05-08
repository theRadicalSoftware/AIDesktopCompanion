#!/usr/bin/env bash
set -euo pipefail

UNIT="${UNIT_NAME:-ai-desktop-companion}"
systemctl --user stop "${UNIT}.service" >/dev/null 2>&1 || true
echo "Stopped ${UNIT}.service"
