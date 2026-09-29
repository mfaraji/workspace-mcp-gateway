#!/usr/bin/env bash
# Deploy the gateway with Docker Compose: pull the latest main, rebuild, restart,
# and wait for /health. Migrations run in the container entrypoint.
#
# Run it on the server, or from anywhere with DEPLOY_HOST=user@host (it then
# re-runs itself over SSH in DEPLOY_DIR).
#
#   scripts/deploy.sh                       # local checkout
#   DEPLOY_HOST=deploy@example.com DEPLOY_DIR=/srv/workspace-mcp-gateway scripts/deploy.sh
set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:-}"
DEPLOY_DIR="${DEPLOY_DIR:-}"
BRANCH="${BRANCH:-main}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"

if [[ -n "${DEPLOY_HOST}" ]]; then
  : "${DEPLOY_DIR:?DEPLOY_DIR is required with DEPLOY_HOST}"
  exec ssh "${DEPLOY_HOST}" \
    "cd '${DEPLOY_DIR}' && BRANCH='${BRANCH}' HEALTH_URL='${HEALTH_URL}' \
HEALTH_TIMEOUT='${HEALTH_TIMEOUT}' bash scripts/deploy.sh"
fi

cd "${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

[[ -f .env ]] || { echo "error: .env not found in $(pwd)" >&2; exit 1; }
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "error: working tree has local changes; commit or stash them first" >&2
  exit 1
fi

echo "==> Updating ${BRANCH}"
git fetch --quiet origin "${BRANCH}"
git checkout --quiet "${BRANCH}"
git merge --ff-only "origin/${BRANCH}"
echo "    at $(git rev-parse --short HEAD): $(git log -1 --format=%s)"

echo "==> Building and restarting"
docker compose up -d --build --remove-orphans

echo "==> Waiting for ${HEALTH_URL} (up to ${HEALTH_TIMEOUT}s)"
deadline=$((SECONDS + HEALTH_TIMEOUT))
until curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "error: gateway did not become healthy; recent logs:" >&2
    docker compose logs --tail=40 app >&2 || true
    exit 1
  fi
  sleep 2
done

docker compose ps
echo "==> Deployed $(git rev-parse --short HEAD)"
