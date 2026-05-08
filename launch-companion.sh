#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="${UNIT_NAME:-ai-desktop-companion}"
PET_VALUE="${PET:-starter-buddy}"
SCALE_VALUE="${SCALE:-1.1}"
DISPLAY_VALUE="${DISPLAY:-:1}"
XAUTHORITY_VALUE="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"
CODEX_SESSION_VALUE="${CODEX_SESSION:-current}"
CODEX_BINARY_VALUE="${CODEX_BINARY:-$(command -v codex 2>/dev/null || true)}"
PATH_VALUE="${PATH:-/usr/local/bin:/usr/bin:/bin}"

EXTRA_PATHS=()
if [[ -n "${CODEX_BINARY_VALUE}" ]]; then
  EXTRA_PATHS+=("$(dirname "${CODEX_BINARY_VALUE}")")
fi
for node_bin in "${HOME}/.nvm/versions/node"/*/bin; do
  [[ -d "${node_bin}" ]] && EXTRA_PATHS+=("${node_bin}")
done
EXTRA_PATHS+=("${HOME}/.local/bin" "${HOME}/bin" "${HOME}/.cargo/bin" "${HOME}/.npm-global/bin")
if (( ${#EXTRA_PATHS[@]} > 0 )); then
  EXTRA_PATH_VALUE="$(IFS=:; echo "${EXTRA_PATHS[*]}")"
  PATH_VALUE="${EXTRA_PATH_VALUE}:${PATH_VALUE}"
fi

systemctl --user stop "${UNIT}.service" >/dev/null 2>&1 || true

SYSTEMD_RUN_ARGS=(
  --user
  --collect
  --unit "${UNIT}"
  --working-directory "${ROOT}"
  --property "StandardOutput=append:${ROOT}/ai-desktop-companion.log"
  --property "StandardError=append:${ROOT}/ai-desktop-companion.log"
  --setenv "DISPLAY=${DISPLAY_VALUE}"
  --setenv "XAUTHORITY=${XAUTHORITY_VALUE}"
  --setenv "PATH=${PATH_VALUE}"
)
if [[ -n "${CODEX_BINARY_VALUE}" ]]; then
  SYSTEMD_RUN_ARGS+=(--setenv "CODEX_BINARY=${CODEX_BINARY_VALUE}")
fi
for forwarded_env in \
  ANTHROPIC_API_KEY \
  CLAUDE_API_KEY \
  AI_DESKTOP_COMPANION_CLAUDE_KEY \
  DESKTOP_COMPANION_CLAUDE_KEY \
  SLACK_API_TOKEN \
  SLACK_ACCESS_TOKEN \
  SLACK_BOT_TOKEN \
  AI_DESKTOP_COMPANION_SLACK_BOT_TOKEN \
  DESKTOP_COMPANION_SLACK_TOKEN \
  SLACK_USER_TOKEN \
  AI_DESKTOP_COMPANION_SLACK_USER_TOKEN \
  DESKTOP_COMPANION_SLACK_USER_TOKEN \
  SSH_AUTH_SOCK \
  GIT_SSH_COMMAND; do
  if [[ -n "${!forwarded_env:-}" ]]; then
    SYSTEMD_RUN_ARGS+=(--setenv "${forwarded_env}=${!forwarded_env}")
  fi
done

systemd-run \
  "${SYSTEMD_RUN_ARGS[@]}" \
  /usr/bin/python3 "${ROOT}/run.py" run "${PET_VALUE}" --scale "${SCALE_VALUE}" --codex-session "${CODEX_SESSION_VALUE}"

echo "Launched ${UNIT}.service with pet ${PET_VALUE}"
