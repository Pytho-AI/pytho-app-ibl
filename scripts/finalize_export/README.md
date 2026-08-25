# finalize_export — host-side TrueNAS export hook

Host-side daemon that watches the docker-api uploads bind-mount and copies
finalized exports (the `.docx` / `.pptx` / `.xlsx` an analyst hits FINAL on) into
the SCIF's TrueNAS share at `$PYTHO_FINALIZE_EXPORT_DIR/<product_type>/<file>`.

Zero code change inside docker-api. Zero `docker-compose.yml` change. The watcher
reads the existing on-disk artifact state (`schema_state.json` →
`header.status == "published"`) and copies the matching export file. Atomic copy
via `.tmp` sibling + `mv`.

**This directory is the entire feature.** Removing it removes the feature.

## Install (on the SCIF host)

```bash
# 1. Make the script executable.
chmod +x /home/pytho/pytho-app/scripts/finalize_export/finalize_to_truenas.sh

# 2. Drop the systemd unit in place. Edit PYTHO_FINALIZE_EXPORT_DIR= to point
#    at the TrueNAS mount path.
sudo install -m 644 \
    /home/pytho/pytho-app/scripts/finalize_export/pytho-finalize-export.service \
    /etc/systemd/system/pytho-finalize-export.service
sudoedit /etc/systemd/system/pytho-finalize-export.service
#    → set Environment=PYTHO_FINALIZE_EXPORT_DIR=/mnt/truenas/pytho (or whatever)

# 3. Apt prereq.
sudo apt install -y inotify-tools

# 4. Start.
sudo systemctl daemon-reload
sudo systemctl enable --now pytho-finalize-export

# 5. Verify.
journalctl -u pytho-finalize-export -f
```

## Watch the live stream

```bash
journalctl -u pytho-finalize-export -f
```

Each export logs one line: `exported <product_type>/<filename>`.

## Remove (post-event)

```bash
# On the SCIF host.
sudo systemctl disable --now pytho-finalize-export
sudo rm /etc/systemd/system/pytho-finalize-export.service
sudo systemctl daemon-reload

# In the repo (any host).
rm -rf scripts/finalize_export/
git commit -am "remove finalize_export host hook (event-scoped)"
```

That's it. No other repo file references this directory, nothing in
docker-api links into it, nothing in `docker-compose.yml` mounts it.

## How it works

1. `inotifywait` watches `uploads/_autodraft/` recursively for
   `close_write` / `moved_to` events on any file.
2. Triggers on two filename shapes:
   - export files (`.docx` / `.pptx` / `.xlsx`) — the docker-api exporter
     just finished writing them.
   - `schema_state.json` — the publish endpoint just rewrote it; we
     handle the sibling export file if status is now `published`.
3. Reads `schema_state.json` from the artifact's directory; grep for
   `"status": "published"`. If not published, skip.
4. Derives `product_type` from `schema_state.json`'s top-level
   `product_type` field (falls back to splitting the artifact_id on the
   `_YYYYMMDDTHHMMSS_msZ` suffix if the JSON is unreadable).
5. `cp` to `$DEST/<product_type>/.<filename>.tmp` → `mv` to
   `$DEST/<product_type>/<filename>` (atomic on POSIX).

## Failure handling

- TrueNAS unmounted / read-only / full → `cp` fails, logged to journald,
  watcher keeps running. Next finalize retries.
- Missing `inotify-tools` → service exits 1 at start, journald shows the
  apt-install hint. systemd will retry per `Restart=on-failure`.
- Re-finalizing the same artifact → `mv` overwrite is atomic; analysts
  tailing the share never see a partial write.
- docker-api restart / rebuild → no impact on the watcher (independent
  process). docker-api eventually re-writes `schema_state.json` and the
  next `close_write` fires another export. Idempotent.

## What it intentionally does NOT do

- No subscription to docker-api HTTP. The watcher is a filesystem
  observer; doesn't care if the API is up.
- No artifact dir copy (`schema_state.json`, llm_calls, page-NN.png,
  tool_results, ...). Only the analyst-facing export file. To grab the
  whole dir post-hoc: `cp -r uploads/_autodraft/<id> <wherever>`.
- No retry queue. If TrueNAS is down at finalize time, the file isn't
  copied. Next time the analyst republishes (or someone touches
  `schema_state.json`), the watcher will retry.
