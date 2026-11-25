#!/usr/bin/env bash
# One-click SSH deploy & historical data migration script for fnOS (飞牛 NAS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SSH_TARGET=""
SSH_PORT="22"
SSH_KEY=""
REMOTE_APP_DIR="/vol1/1000/docker/daily-memory-hub"
REMOTE_DATA_DIR="/vol1/1000/DailyMemory"
LOCAL_DATA_DIR="${PROJECT_DIR}/data"
LOCAL_REPORT_DIR="${HOME}/Documents/DailyReport"
MIGRATE_DATA="0"
COMPOSE_PROFILE=""
DRY_RUN="0"

usage() {
  cat <<'EOF'
Usage: nas_deploy.sh --host <user@nas-ip-or-alias> [options]

Options:
  --host <target>         SSH target (e.g. admin@192.168.1.100 or ~/.ssh/config alias)
  --port <port>           SSH port (default: 22)
  --key <path>            SSH private key path (optional)
  --app-dir <path>        Remote Docker app directory (default: /vol1/1000/docker/daily-memory-hub)
  --data-dir <path>       Remote shared storage root (default: /vol1/1000/DailyMemory)
  --local-data <path>     Local data/ directory to migrate (default: <project>/data)
  --local-report <path>   Local DailyReport/ directory to migrate (default: ~/Documents/DailyReport)
  --migrate-data          Rsync local historical data/ and DailyReport/ to NAS before startup
  --profile <name>        Optional Docker Compose profile: cloudflared | frpc
  --dry-run               Print commands without executing remote changes
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) SSH_TARGET="$2"; shift 2 ;;
    --port) SSH_PORT="$2"; shift 2 ;;
    --key) SSH_KEY="$2"; shift 2 ;;
    --app-dir) REMOTE_APP_DIR="$2"; shift 2 ;;
    --data-dir) REMOTE_DATA_DIR="$2"; shift 2 ;;
    --local-data) LOCAL_DATA_DIR="$2"; shift 2 ;;
    --local-report) LOCAL_REPORT_DIR="$2"; shift 2 ;;
    --migrate-data) MIGRATE_DATA="1"; shift ;;
    --profile) COMPOSE_PROFILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "${SSH_TARGET}" ]]; then
  echo "ERROR: --host is required." >&2
  usage
  exit 1
fi

SSH_OPTS=(-p "${SSH_PORT}" -o StrictHostKeyChecking=accept-new)
if [[ -n "${SSH_KEY}" ]]; then
  SSH_OPTS+=(-i "${SSH_KEY}")
fi

RSYNC_SSH="ssh -p ${SSH_PORT} -o StrictHostKeyChecking=accept-new"
if [[ -n "${SSH_KEY}" ]]; then
  RSYNC_SSH+=" -i ${SSH_KEY}"
fi

run_cmd() {
  echo "+ $*"
  if [[ "${DRY_RUN}" == "0" ]]; then
    "$@"
  fi
}

echo "[1/5] Preparing remote directories on ${SSH_TARGET}..."
run_cmd ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" \
  "mkdir -p '${REMOTE_APP_DIR}/server' '${REMOTE_APP_DIR}/deploy' '${REMOTE_DATA_DIR}/data' '${REMOTE_DATA_DIR}/DailyReport'"

echo "[2/5] Syncing server application code..."
run_cmd rsync -avz --delete \
  -e "${RSYNC_SSH}" \
  --include='/multi_machine.py' \
  --include='/remote_client.py' \
  --include='/query_memory.py' \
  --include='/write_excel.py' \
  --include='/summarizer.py' \
  --include='/export_report.py' \
  --include='/server/' \
  --include='/server/app.py' \
  --include='/server/Dockerfile' \
  --include='/server/docker-compose.yml' \
  --include='/server/.env.example' \
  --include='/deploy/' \
  --include='/deploy/frpc.example.toml' \
  --exclude='*' \
  "${PROJECT_DIR}/" "${SSH_TARGET}:${REMOTE_APP_DIR}/"

if [[ "${MIGRATE_DATA}" == "1" ]]; then
  echo "[3/5] Migrating historical data/ and DailyReport/ to ${REMOTE_DATA_DIR}..."
  if [[ -d "${LOCAL_DATA_DIR}" ]]; then
    run_cmd rsync -avz -e "${RSYNC_SSH}" --exclude='__pycache__' "${LOCAL_DATA_DIR}/" "${SSH_TARGET}:${REMOTE_DATA_DIR}/data/"
  fi
  if [[ -d "${LOCAL_REPORT_DIR}" ]]; then
    run_cmd rsync -avz -e "${RSYNC_SSH}" --exclude='.DS_Store' "${LOCAL_REPORT_DIR}/" "${SSH_TARGET}:${REMOTE_DATA_DIR}/DailyReport/"
  fi
else
  echo "[3/5] Skipping historical data migration (pass --migrate-data to include)."
fi

echo "[4/5] Bootstrapping server/.env (if missing) and launching Docker Compose..."
PROFILE_FLAG=""
if [[ -n "${COMPOSE_PROFILE}" ]]; then
  PROFILE_FLAG="--profile ${COMPOSE_PROFILE}"
fi

REMOTE_BOOT_SCRIPT=$(cat <<EOF
set -euo pipefail
cd '${REMOTE_APP_DIR}/server'
if [[ ! -f .env ]]; then
  cp .env.example .env
  READ_TOK="\$(openssl rand -hex 32)"
  WRITE_TOK="\$(openssl rand -hex 32)"
  sed -i "s|^DAILY_MEMORY_DATA_ROOT=.*|DAILY_MEMORY_DATA_ROOT=${REMOTE_DATA_DIR}|" .env
  sed -i "s|^DAILY_MEMORY_READ_TOKEN=.*|DAILY_MEMORY_READ_TOKEN=\${READ_TOK}|" .env
  sed -i "s|^DAILY_MEMORY_WRITE_TOKEN=.*|DAILY_MEMORY_WRITE_TOKEN=\${WRITE_TOK}|" .env
  chmod 600 .env
  echo "GENERATED_NEW_ENV=1"
  echo "READ_TOKEN=\${READ_TOK}"
  echo "WRITE_TOKEN=\${WRITE_TOK}"
else
  echo "EXISTING_ENV_PRESERVED=1"
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "ERROR: docker compose not found on remote host." >&2
  exit 1
fi

\${COMPOSE} ${PROFILE_FLAG} up -d --build
EOF
)

run_cmd ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "${REMOTE_BOOT_SCRIPT}"

echo "[5/5] Running post-deploy health & auth verification..."
REMOTE_VERIFY_SCRIPT=$(cat <<EOF
set -euo pipefail
cd '${REMOTE_APP_DIR}/server'
source .env
PORT="\${DAILY_MEMORY_PORT:-8765}"
for i in \$(seq 1 15); do
  if curl -fsS "http://127.0.0.1:\${PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
echo -n "healthz: "
curl -fsS "http://127.0.0.1:\${PORT}/healthz"
echo ""
UNAUTH_CODE="\$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:\${PORT}/api/v1/status")"
echo "unauthenticated_status_code: \${UNAUTH_CODE}"
test "\${UNAUTH_CODE}" = "401"
echo -n "status: "
curl -fsS -H "Authorization: Bearer \${DAILY_MEMORY_READ_TOKEN}" "http://127.0.0.1:\${PORT}/api/v1/status"
echo ""
EOF
)

run_cmd ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "${REMOTE_VERIFY_SCRIPT}"
echo "Deployment complete."
