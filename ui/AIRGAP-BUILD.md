# Airgap UI rebuild procedure

The IBL SCIF deployment has NO internet. Without changes,
`docker compose up --build nginx` fails three different ways on spark:

1. `yarn install` cannot reach `registry.yarnpkg.com`
2. `apk add openssl` (for the self-signed TLS cert) cannot reach Alpine repos
3. Even when those are bypassed, native deps (esbuild, sharp) ship
   PLATFORM-SPECIFIC binaries; macOS node_modules will not run on Linux.

This doc is the procedure that lets spark rebuild offline. Three pieces
work together:

## 1. Vendored `node_modules` matching the target arch

`ui/.dockerignore` no longer excludes `/node_modules`, and `ui/Dockerfile`
detects an existing `/app/node_modules` and skips `yarn install`. So
shipping pre-installed deps into the build context is the offline path.

THE CATCH: `esbuild`, `sharp`, etc. ship per-platform native binaries.
**Do NOT `yarn install` on macOS and ship that node_modules to spark
(linux-aarch64) — the build will fail at `yarn build` with esbuild
ENOENT.** Build node_modules on the same arch as spark.

On a Mac with Docker, use QEMU emulation to build linux/arm64
node_modules without needing access to a real Linux host:

```
mkdir -p /tmp/ui-linux && cd /tmp/ui-linux
cp ~/pytho-app/ui/package.json ~/pytho-app/ui/yarn.lock .
docker run --rm --platform=linux/arm64 \
    -v /tmp/ui-linux:/work -w /work \
    node:20-alpine \
    yarn install --network-timeout 600000
tar czf /tmp/node_modules-linux-arm64.tgz node_modules/
```

The resulting tarball is ~100 MB compressed / ~600 MB extracted as of
2026-06-04. Verify the right arch:

```
file /tmp/ui-linux/node_modules/@esbuild/linux-arm64/bin/esbuild
# expect: ELF 64-bit LSB executable, ARM aarch64, ...
```

If spark is x86_64 instead, swap `linux/arm64` for `linux/amd64` and
expect a `@esbuild/linux-x64/` subdir instead.

Native Linux host with the right arch? Just `cd ui && yarn install`.
Don't need docker.

## 2. Pre-generated TLS cert

The previous Dockerfile ran `apk add openssl` + `openssl req -x509` in
the nginx stage to mint the self-signed cert for the :8443 internal
listener. The `apk add` half fails on airgap.

Now `ui/nginx/ssl/{cert.pem, key.pem}` are pre-generated and the
Dockerfile just `COPY`s them in. Regenerate the pair any time via:

```
cd ~/pytho-app/ui/nginx/ssl
./regenerate.sh
```

`cert.pem` is committed (self-signed dev cert, no chain of trust;
acceptable to expose). `key.pem` is gitignored — keep it on disk per
target.

## 3. Conditional install in the Dockerfile

```
RUN if [ -d node_modules ] && [ -n "$(ls -A node_modules)" ]; then \
        echo "skipping yarn install"; \
    else \
        yarn install --network-timeout 600000; \
    fi
RUN yarn build
```

Vendored deps → noop. Dev host with internet → falls through to a
normal install. Same Dockerfile, both paths.

## Full offline rebuild on spark

```
# (one-time per dependency change) ship the right-arch node_modules
scp /tmp/node_modules-linux-arm64.tgz spark-dev:/tmp/
ssh spark-dev "cd ~/pytho-app/ui && rm -rf node_modules && tar xzf /tmp/node_modules-linux-arm64.tgz"

# rebuild
ssh spark-dev "cd ~/pytho-app && docker compose up -d --build nginx"
```

Watch the build log — expect to see:

```
> [stage-0 4/6] RUN if [ -d node_modules ] ...
skipping yarn install
> [stage-0 5/6] RUN yarn build
✓ built in N.NNs
> [stage-1 ...] COPY nginx/ssl/cert.pem /etc/nginx/ssl/cert.pem
> [stage-1 ...] COPY nginx/ssl/key.pem /etc/nginx/ssl/key.pem
```

Sanity check after the rebuild:

```
ssh spark-dev "docker ps --filter name=proxy-app --format '{{.Status}}' &&
    curl -k -s -o /dev/null -w 'index HTTP %{http_code}\n' https://localhost:8080/ &&
    curl -k -s -o /dev/null -w 'font  HTTP %{http_code} size=%{size_download}\n' \
        https://localhost:8080/fonts/SourceSans3-VariableFont_wght.woff2"
```

Expect `Up N seconds (healthy)` + both HTTPs 200.

## When `package.json` changes (added/removed/upgraded dep)

`yarn.lock` regenerates from `package.json` on whichever host does the
fresh `yarn install`. If you bump a dep:

1. On a connected host (or dev mac with QEMU per the recipe above):
   `yarn install` → rebuilds node_modules to match the new lockfile.
2. Tar + ship to spark (same scp + extract as above).
3. `git commit ui/package.json ui/yarn.lock` and push so spark can
   pick up the lockfile via patch when it has temporary connectivity.
4. Trigger the offline rebuild.

The repo does NOT commit node_modules (would balloon git history).
The tarball is shipped out-of-band each time deps change.

## Fast path for source-only changes (no dep changes)

The above is for cases where the IMAGE must rebuild. For day-to-day
source iteration (CSS tweak, new page, etc.), the lighter path from
commit `bee7cd6` is still in place:

1. `yarn build` on dev mac → `dist/`
2. `tar czf - dist/ | ssh spark-dev 'cd ~/pytho-app/ui && rm -rf dist && tar xzf -'`
3. `ssh spark-dev 'docker compose restart nginx'` (no `--build`)

`docker-compose.yml` bind-mounts `./ui/dist:/usr/share/nginx/html:ro`,
so a fresh dist immediately serves. Use this when you do not need a
true image rebuild.

## File inventory

- `ui/.dockerignore` — does not exclude `/node_modules`
- `ui/Dockerfile` — conditional install, `COPY` cert pair, no apk
- `ui/nginx/ssl/cert.pem` — committed self-signed dev cert
- `ui/nginx/ssl/key.pem` — gitignored, regenerate per target
- `ui/nginx/ssl/regenerate.sh` — one-shot cert regeneration

## Related runbooks on spark

- `~/audit-docs/audit-2026-06-04-airgap-lan.md` — full airgap audit
- `~/audit-docs/artifact-audit-report.md` — artifact pipeline audit
- `~/audit-docs/running-log-2026-06-04.md` — UIUX walkthrough log
- `~/audit-docs/spark-code-server-autostart.md` — code-server systemd
