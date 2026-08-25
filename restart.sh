#!/bin/bash
# Quick container recreate on spark-dev. NO image rebuild — use this
# when the changed code is bind-mounted into the container so a fresh
# container picks up the new file without needing a rebuild:
#   - editor/scripts/*       -> recreate editor (bind-mount /pytho-scripts)
#   - editor/patch-editor.sh -> recreate editor (bind-mount + re-runs at boot)
#   - ergo/ircd.yaml         -> recreate agent-chat
#   - nginx config drift     -> recreate nginx (also re-resolves docker-api IP)
#
# Git pull is intentionally NOT part of this script.
#
# If the change was in app/ or ui/ (COPY'd into the image, not bind-
# mounted), restart.sh WILL NOT pick it up. Use rebuild.sh instead.
#
# Usage:
#   ./restart.sh                  # recreate EVERYTHING (default — same as `all`)
#   ./restart.sh api              # recreate docker-api only (also nginx for DNS)
#   ./restart.sh nginx            # recreate nginx only (re-resolves DNS)
#   ./restart.sh editor           # recreate editor (re-runs patch-editor.sh)
#   ./restart.sh chat             # recreate ergo IRC daemon
#   ./restart.sh ntp              # recreate scenario-ntp (re-reads scripts/scenario_ntp_server.py)
#   ./restart.sh cdp              # restart host chromium-cdp systemd unit
#   ./restart.sh all              # explicit form of the no-arg default

set -euo pipefail
cd "$(dirname "$0")"

# Compose file resolution — same rule as rebuild.sh: explicit
# PYTHO_COMPOSE_FILE wins, else follow whatever compose file created the
# running docker-api (label), else the edge default. Recreating a cloud
# stack from the edge file applies edge-only hardening
# (no-new-privileges) that crash-loops under snap-confined Docker.
if [ -n "${PYTHO_COMPOSE_FILE:-}" ]; then
    base_compose="$PYTHO_COMPOSE_FILE"
else
    base_compose="$(docker inspect docker-api \
        --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' \
        2>/dev/null | tr ',' '\n' | grep -oE '[^/]+\.ya?ml$' | head -1)"
    if [ -z "$base_compose" ] || [ ! -f "$base_compose" ]; then
        base_compose="docker-compose-edge.yml"
    fi
fi
echo "==> compose file: $base_compose"

targets=("$@")
if [ "${#targets[@]}" -eq 0 ]; then
    targets=(all)
fi

want() {
    local svc="$1"
    for t in "${targets[@]}"; do
        [ "$t" = "$svc" ] && return 0
        [ "$t" = "all" ] && return 0
    done
    return 1
}

recreated_api=0

if want api; then
    echo "==> recreate docker-api"
    docker compose -f "$base_compose" up -d --pull missing --force-recreate docker-api
    recreated_api=1
fi

# DNS-re-resolve invariant — see rebuild.sh for why nginx ALWAYS
# follows a docker-api change.
if want nginx || [ "$recreated_api" = "1" ]; then
    echo "==> recreate nginx (re-resolve upstream DNS)"
    docker compose -f "$base_compose" up -d --pull missing --force-recreate nginx
fi

if want editor; then
    echo "==> recreate editor (re-runs patch-editor.sh at boot)"
    docker compose -f "$base_compose" up -d --pull missing --force-recreate editor
fi

if want chat; then
    echo "==> recreate agent-chat (ergo IRC daemon)"
    docker compose -f "$base_compose" up -d --pull missing --force-recreate ergo
fi

if want ntp; then
    echo "==> recreate scenario-ntp (re-reads bind-mounted scenario_ntp_server.py)"
    docker compose -f "$base_compose" up -d --pull missing --force-recreate scenario-ntp
fi

if want cdp; then
    echo "==> restart host chromium-cdp + cdp-forward systemd units"
    if ! sudo -n true 2>/dev/null; then
        echo "    [needs sudo — re-run as: sudo systemctl restart chromium-cdp cdp-forward]"
    else
        sudo systemctl restart chromium-cdp.service cdp-forward.service
    fi
fi

# Wait for docker-api health if we touched it.
if [ "$recreated_api" = "1" ]; then
    echo "==> waiting for docker-api to come back healthy"
    until docker exec docker-api curl -sf http://localhost:8000/api/ > /dev/null 2>&1; do
        sleep 1
    done
    echo "    docker-api healthy"
fi

echo ""
echo "==> services:"
docker compose -f "$base_compose" ps --format "table {{.Service}}\t{{.Status}}" 2>&1 | head -15

echo ""
echo "==> verify public proxy reach:"
curl -sk -o /dev/null -w "    GET /api/ -> HTTP %{http_code}\n" "https://localhost/api/" --max-time 4 || true
