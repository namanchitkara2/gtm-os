#!/usr/bin/env bash
# Start GTM-OS locally: Postgres, control plane, and the LinkedIn bridge.
#
#   ./run-local.sh          start everything
#   ./run-local.sh stop     stop control plane + bridge
#   ./run-local.sh status   show what is running
#
# Local mode has NO authentication and binds to 127.0.0.1 only. The deployed
# version puts Cognito behind FastMCP's OAuthProxy; dev mode is gated on an
# explicit env var so a misconfigured deployment fails closed rather than
# serving every anonymous caller the dev workspace.
set -euo pipefail
cd "$(dirname "$0")"

export DATABASE_URL="${DATABASE_URL:-postgresql://gtmos_app:app@localhost:55432/gtmos}"
export DATABASE_SYSTEM_URL="${DATABASE_SYSTEM_URL:-postgresql://gtmos_system:system@localhost:55432/gtmos}"
export GTMOS_DEV_MODE=1
export GTMOS_PUBLIC_URL="${GTMOS_PUBLIC_URL:-http://127.0.0.1:8080}"
export GTMOS_URL="$GTMOS_PUBLIC_URL"
mkdir -p .run

stop() {
  pkill -f "uvicorn gtmos_control" 2>/dev/null || true
  pkill -f "gtmos_bridge.cli run" 2>/dev/null || true
  echo "stopped"
}

status() {
  curl -sf "$GTMOS_PUBLIC_URL/healthz" >/dev/null 2>&1 \
    && echo "control plane : up" || echo "control plane : down"
  [ -f apps/web/dist/index.html ] && echo "dashboard     : built" || echo "dashboard     : not built"
  pgrep -f "gtmos_bridge.cli run" >/dev/null && echo "bridge        : running" || echo "bridge        : down"
  docker ps --filter name=gtmos-pg --format '{{.Names}} {{.Status}}' | sed 's/^/postgres      : /'
}

case "${1:-start}" in
  stop) stop; exit 0 ;;
  status) status; exit 0 ;;
esac

echo "→ postgres"
docker start gtmos-pg >/dev/null 2>&1 || \
  docker run -d --name gtmos-pg -e POSTGRES_PASSWORD=gtmos -e POSTGRES_USER=gtmos \
    -e POSTGRES_DB=gtmos -p 55432:5432 postgres:16-alpine >/dev/null
until docker exec gtmos-pg pg_isready -U gtmos >/dev/null 2>&1; do sleep 1; done

# The control plane serves the built dashboard, so the bridge's "approve this
# code" link resolves on the same origin it already knows about. Built only when
# missing — `npm run build` on every start would add seconds to every restart.
if [ ! -f apps/web/dist/index.html ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "→ dashboard (first build)"
    ( cd apps/web && [ -d node_modules ] || npm install --silent; npm run build --silent ) \
      || echo "  dashboard build failed — the API still runs, the UI will 503"
  else
    echo "→ dashboard skipped (no npm); /  will return 503"
  fi
fi

echo "→ control plane on $GTMOS_PUBLIC_URL"
pkill -f "uvicorn gtmos_control" 2>/dev/null || true
nohup .venv/bin/python -m uvicorn gtmos_control.app:app \
  --host 127.0.0.1 --port 8080 --log-level warning > .run/control-plane.log 2>&1 &
until curl -sf "$GTMOS_PUBLIC_URL/healthz" >/dev/null 2>&1; do sleep 1; done

if [ ! -f "$HOME/.gtm-bridge/credentials.json" ]; then
  echo "→ pairing bridge (dev auto-approve)"
  .venv/bin/python scripts/dev_pair.py
fi

echo "→ bridge (LinkedIn session stays on this machine)"
pkill -f "gtmos_bridge.cli run" 2>/dev/null || true
nohup .venv/bin/python -m gtmos_bridge.cli run > .run/bridge.log 2>&1 &
for _ in $(seq 1 60); do
  grep -q "tunnel connected" .run/bridge.log 2>/dev/null && break; sleep 2
done

echo
status
echo
echo "Dashboard:                 $GTMOS_PUBLIC_URL"
echo "Connect from Claude Code:  claude mcp add --transport http gtm-os $GTMOS_PUBLIC_URL/mcp"
echo "Logs: .run/control-plane.log  .run/bridge.log"
