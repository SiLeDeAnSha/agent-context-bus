#!/usr/bin/env bash
# Install a macOS LaunchAgent that pushes daily raw traces to fnOS NAS and refreshes local offline cache.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

MACHINE_ID="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
HOUR="8"
MINUTE="55"
REMOTE_CFG="${HOME}/.daily-memory/remote.json"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.daily-memory.collector.plist"
LOG_DIR="${HOME}/.daily-memory/logs"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --machine) MACHINE_ID="$2"; shift 2 ;;
    --hour) HOUR="$2"; shift 2 ;;
    --minute) MINUTE="$2"; shift 2 ;;
    --remote-config) REMOTE_CFG="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "${HOME}/.daily-memory" "${LOG_DIR}" "${HOME}/Library/LaunchAgents"
if [[ ! -f "${REMOTE_CFG}" ]]; then
  cp "${SCRIPT_DIR}/remote.example.json" "${REMOTE_CFG}"
  chmod 600 "${REMOTE_CFG}"
  echo "Created template ${REMOTE_CFG} — please fill in your NAS endpoints and tokens."
fi

RUNNER="${HOME}/.daily-memory/run_collector.sh"
cat > "${RUNNER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
"${PYTHON_BIN}" "${PROJECT_DIR}/collect.py" --yesterday --missing --skip-pushed --machine "${MACHINE_ID}" --push --remote-config "${REMOTE_CFG}"
"${PYTHON_BIN}" "${PROJECT_DIR}/query_memory.py" --sync-cache --remote-config "${REMOTE_CFG}"
EOF
chmod +x "${RUNNER}"

cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.daily-memory.collector</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${RUNNER}</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict>
      <key>Hour</key>
      <integer>${HOUR}</integer>
      <key>Minute</key>
      <integer>${MINUTE}</integer>
    </dict>
    <dict>
      <key>Hour</key>
      <integer>10</integer>
      <key>Minute</key>
      <integer>30</integer>
    </dict>
    <dict>
      <key>Hour</key>
      <integer>14</integer>
      <key>Minute</key>
      <integer>0</integer>
    </dict>
    <dict>
      <key>Hour</key>
      <integer>19</integer>
      <key>Minute</key>
      <integer>0</integer>
    </dict>
  </array>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/collector.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/collector.err.log</string>
</dict>
</plist>
EOF

launchctl unload "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl load "${PLIST_PATH}"
echo "Installed LaunchAgent ${PLIST_PATH} (primary ${HOUR}:${MINUTE} + catchup 10:30/14:00/19:00, machine=${MACHINE_ID})."
echo "Test now with: bash '${RUNNER}'"
