#!/bin/bash
# Full rebuild + recreate of docker services on spark-dev. Use when code
# that's COPY'd into a docker image changed:
#   - app/         -> docker-api (rebuild + force-recreate)
#   - ui/          -> nginx      (rebuild --no-cache + force-recreate)
#
# Git pull is intentionally NOT part of this script — pull manually
# before running so you control exactly what's deployed.
#
# When docker-api is rebuilt, it gets a NEW docker bridge IP and nginx's
# cached upstream IP becomes stale (`connect() failed (113: Host is
# unreachable)`). So we ALWAYS force-recreate nginx whenever docker-api
# was rebuilt. This is the deploy-cycle invariant that the manual flow
# kept forgetting.
#
# Usage:
#   ./rebuild.sh                # rebuild EVERYTHING (default — same as `all`)
#   ./rebuild.sh api            # rebuild docker-api only (still recreates nginx for DNS)
#   ./rebuild.sh nginx          # rebuild nginx only
#   ./rebuild.sh editor         # rebuild editor only
#   ./rebuild.sh chat           # recreate ergo (pulled image — no build, just force-recreate)
#   ./rebuild.sh ntp            # recreate scenario-ntp (pulled image, bind-mounted server script)
#   ./rebuild.sh all            # explicit form of the no-arg default
#
# Optional --project <name> flag (positional-aware — may appear before or
# after the target args). When provided:
#   - every `docker compose` invocation runs with `-p <name>` so parallel
#     project stacks (e.g. the default IDX 26 stack and a vcorps stack)
#     don't clobber each other's containers/networks/volumes.
#   - for --project vcorps we additionally layer
#       -f docker-compose.yml -f docker-compose.vcorps.yml
#     so the vcorps override file is merged on top of the base compose.
#   - the nginx-extract throwaway container is suffixed
#     (pytho-nginx-extract-<project>) so two parallel rebuilds can't
#     race on the same container name.
#   - the extracted dist destination is suffixed (ui/dist-<project>) so a
#     vcorps build doesn't overwrite the IDX 26 ui/dist that the default
#     stack's nginx is bind-mounting. The override compose file is
#     responsible for pointing the per-project nginx at the matching
#     ui/dist-<project> on the host.
# When --project is unset, the script behaves identically to the
# pre-flag version (no -p, no extra -f, original container/dist names).
#
# Pulled-image services (ergo / scenario-ntp / kokoro-tts / ollama / es)
# have no `docker compose build` step, so "rebuild" for them is just a
# force-recreate. We use `--pull missing` (Docker default): if the image
# is in cache, use it (the airgap path); if not, pull it (the fresh dev
# box path). The previous `--pull never` was too aggressive — it broke
# fresh dev boxes with "No such image" instead of just fetching the
# upstream tag. Airgap remains safe because spark's images are always
# pre-cached; --pull missing only reaches the network on a true
# cache-miss, which on airgap is a real config error worth surfacing.
#
# For services where the source is bind-mounted (editor scripts, ergo
# config, host systemd units), use restart.sh instead — much faster.

set -euo pipefail
cd "$(dirname "$0")"

# Bootstrap the shared docker network if it's not there yet. Declared
# `external: true` in docker-compose.yml because both pytho-app and
# geo-agent share it (cross-compose Docker DNS so geo-agent's backend can
# reach docker-api:8000). Marking it external means neither compose
# project owns the lifecycle — first invocation on a fresh machine
# creates it, everyone else attaches. Without this, a fresh checkout's
# first `./rebuild.sh` fails with "network pytho-net declared as
# external, but could not be found".
if ! docker network inspect pytho-net >/dev/null 2>&1; then
    echo "==> creating pytho-net (shared docker network, didn't exist yet)"
    docker network create pytho-net
fi


# --- argument parsing -------------------------------------------------
# Accept --project <name> or --project=<name> interleaved with the
# positional target args (api / nginx / editor / chat / ntp / all). We
# walk argv once and split into:
#   project      — the value of --project (empty string = unset)
#   targets      — the remaining positional service targets
project=""
targets=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --project)
            if [ "$#" -lt 2 ]; then
                echo "error: --project requires a value" >&2
                exit 2
            fi
            project="$2"
            shift 2
            ;;
        --project=*)
            project="${1#--project=}"
            shift
            ;;
        --)
            shift
            while [ "$#" -gt 0 ]; do
                targets+=("$1")
                shift
            done
            ;;
        *)
            targets+=("$1")
            shift
            ;;
    esac
done

if [ "${#targets[@]}" -eq 0 ]; then
    targets=(all)
fi

# Build the prefix of args that goes between `docker compose` and the
# subcommand (build / up / ps / ...). We use an array so we can pass it
# safely with "${compose_args[@]}" even when it's empty (the default
# IDX 26 stack, where -p and extra -f files are omitted entirely so
# behavior matches the pre-flag script byte-for-byte).
#
# Compose file resolution — the ibl branch serves BOTH deployment
# targets (docker-compose-edge.yml on spark/edge boxes,
# docker-compose-cloud.yml on the EC2 cloud boxes). Rebuilding a
# running stack with the WRONG file recreates containers under the
# other target's options: the edge file's `no-new-privileges:true`
# hardening crash-loops every container under snap-confined Docker
# (AppArmor denies the profile transition — took staging down
# 2026-07-06). Resolution order:
#   1. PYTHO_COMPOSE_FILE env — explicit override.
#   2. The compose config_files label on the running docker-api
#      container — rebuilds always follow whatever file the live
#      stack was created from.
#   3. docker-compose-edge.yml — bootstrap default (spark behavior
#      unchanged).
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
compose_args=(-f "$base_compose")
if [ -n "$project" ]; then
    compose_args+=(-p "$project")
    if [ "$project" = "vcorps" ]; then
        # Layer the vcorps override on top of the base compose so the
        # override's image/build/volume/env tweaks take effect.
        compose_args+=(-f docker-compose.vcorps.yml)
    fi
fi

# Per-project suffix for resources that two simultaneous rebuilds would
# otherwise collide on. Empty (no leading dash) when --project is unset,
# preserving the original "pytho-nginx-extract" name and "ui/dist" path.
if [ -n "$project" ]; then
    project_suffix="-$project"
else
    project_suffix=""
fi
extract_container="pytho-nginx-extract${project_suffix}"
dist_dir="ui/dist${project_suffix}"
# Container name docker-api runs as. Default stack uses bare `docker-api`;
# the vcorps override sets `container_name: docker-api-vcorps`. The
# health-check exec must hit the right name or it spins forever waiting
# for a container that doesn't exist under this project.
docker_api_container="docker-api${project_suffix}"
# Final public-proxy reach check URL — per-project port. Default IDX 26
# stack binds :443; vcorps override binds :10443.
if [ -n "$project" ] && [ "$project" = "vcorps" ]; then
    reach_url="https://localhost:10443/api/"
else
    reach_url="https://localhost/api/"
fi

want() {
    local svc="$1"
    for t in "${targets[@]}"; do
        [ "$t" = "$svc" ] && return 0
        [ "$t" = "all" ] && return 0
    done
    return 1
}

# Whether the resolved compose file actually defines a service. The
# lineages share this ONE script but not one service set (the cloud
# composes have no editor/ergo/scenario-ntp), and `all` must not die
# on a target the stack never had — that killed the first cloud run
# at `no such service: editor`. Explicit targets still fail loudly:
# asking for a service this compose lacks is a real error.
has_service() {
    docker compose "${compose_args[@]}" config --services 2>/dev/null | grep -qx "$1"
}

# `all` skips a missing optional service with a note; an explicitly
# requested target that this compose lacks is a hard error.
skip_or_die() {
    local svc="$1"
    if [[ " ${targets[*]} " == *" all "* ]]; then
        echo "==> skip ${svc} (not defined in ${base_compose})"
        return 0
    fi
    echo "ERROR: service '${svc}' is not defined in ${base_compose}" >&2
    exit 1
}

rebuilt_api=0
rebuilt_nginx=0

if want api; then
    echo "==> rebuild docker-api (image + force-recreate)"
    docker compose "${compose_args[@]}" build docker-api
    docker compose "${compose_args[@]}" up -d --pull missing --force-recreate docker-api
    rebuilt_api=1
fi

if want nginx; then
    echo "==> rebuild nginx (image --no-cache + force-recreate)"
    # --no-cache: Vite bundle hashes change every build; cached layers
    # serve stale assets. See feedback_ibl_nginx_deploy_full_cycle.md.
    docker compose "${compose_args[@]}" build nginx --no-cache
    # CRITICAL: docker-compose bind-mounts ./ui/dist over the image's
    # baked-in /usr/share/nginx/html (see docker-compose.yml — the mount
    # exists so out-of-band scp'd tarballs can hot-swap dist without
    # rebuilding the image). The mount MASKS whatever the Dockerfile
    # just built into the image. Without the extract step below, every
    # `./rebuild.sh nginx` was a no-op for what the analyst actually
    # sees — the freshly-built dist lived in the image, but nginx kept
    # serving the stale host-side ./ui/dist from the last scp tarball.
    # PR #63's nav cleanup + PR #64's modal copy both shipped to spark
    # but stayed invisible because of this. Extract the just-built dist
    # from the image to the host so the bind mount serves what was
    # actually compiled.
    #
    # When --project is set we extract into ui/dist-<project> (and use a
    # project-suffixed throwaway container name) so a parallel vcorps
    # rebuild doesn't overwrite the IDX 26 stack's ui/dist. The override
    # compose file points the per-project nginx at the matching host
    # path.
    echo "==> extract built dist from new image to host ./${dist_dir} (the mount masks the image otherwise)"
    docker rm -f "$extract_container" >/dev/null 2>&1 || true
    # The built image's name follows compose's project-name convention:
    # `<project>-<service>`. When --project is unset, compose defaults
    # to the directory name (pytho-app); when set, the image tag is
    # `<project>-nginx`. Resolve the right tag for either path.
    if [ -n "$project" ]; then
        nginx_image="${project}-nginx"
    else
        nginx_image="pytho-app-nginx"
    fi
    docker create --name "$extract_container" "$nginx_image" >/dev/null
    rm -rf "${dist_dir}.new"
    mkdir -p "${dist_dir}.new"
    docker cp "${extract_container}:/usr/share/nginx/html/." "${dist_dir}.new/"
    docker rm "$extract_container" >/dev/null
    # Atomic swap so nginx never reads a partially-populated dir.
    if [ -d "$dist_dir" ]; then
        rm -rf "${dist_dir}.old"
        mv "$dist_dir" "${dist_dir}.old"
    fi
    mv "${dist_dir}.new" "$dist_dir"
    rm -rf "${dist_dir}.old"
    docker compose "${compose_args[@]}" up -d --pull missing --force-recreate nginx
    rebuilt_nginx=1
fi

# DNS-re-resolve invariant: docker-api's IP changed on the new bridge
# attach, so nginx's cached upstream IP is now wrong (502 Bad Gateway,
# `Host is unreachable`). Force-recreate nginx to re-resolve.
if [ "$rebuilt_api" = "1" ] && [ "$rebuilt_nginx" = "0" ]; then
    echo "==> force-recreate nginx (re-resolve docker-api IP after rebuild)"
    docker compose "${compose_args[@]}" up -d --pull missing --force-recreate nginx
fi

if want editor; then
    if has_service editor; then
        echo "==> rebuild editor (image + force-recreate)"
        docker compose "${compose_args[@]}" build editor
        docker compose "${compose_args[@]}" up -d --pull missing --force-recreate editor
    else
        skip_or_die editor
    fi
fi

# Pulled-image services: no build step, just force-recreate from the
# cached image so any bind-mounted config (ircd.yaml / scenario_ntp_server.py)
# is re-read on container start. --pull missing keeps this airgap-safe.
if want chat; then
    if has_service ergo; then
        echo "==> recreate ergo / agent-chat (pulled image, no build)"
        docker compose "${compose_args[@]}" up -d --pull missing --force-recreate ergo
    else
        skip_or_die ergo
    fi
fi

if want ntp; then
    if has_service scenario-ntp; then
        echo "==> recreate scenario-ntp (pulled image, bind-mounted server script)"
        docker compose "${compose_args[@]}" up -d --pull missing --force-recreate scenario-ntp
    else
        skip_or_die scenario-ntp
    fi
fi

# Wait for docker-api health if we touched it.
if [ "$rebuilt_api" = "1" ]; then
    echo "==> waiting for docker-api to come back healthy"
    until docker exec "$docker_api_container" curl -sf http://localhost:8000/api/ > /dev/null 2>&1; do
        sleep 1
    done
    echo "    docker-api healthy"
fi

echo ""
echo "==> services:"
docker compose "${compose_args[@]}" ps --format "table {{.Service}}\t{{.Status}}" 2>&1 | head -15

echo ""
echo "==> verify public proxy reach:"
curl -sk -o /dev/null -w "    GET ${reach_url} -> HTTP %{http_code}\n" "$reach_url" --max-time 4 || true
