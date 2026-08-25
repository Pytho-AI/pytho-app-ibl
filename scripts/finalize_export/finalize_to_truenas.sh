#!/usr/bin/env bash
# Watch the docker-api bind-mounted uploads dir; when an artifact's
# export file (.docx / .pptx / .xlsx) gets a close_write event AND
# the artifact's schema_state.json has header.status == "published",
# copy it into the TrueNAS share at
#   $PYTHO_FINALIZE_EXPORT_DIR/<product_type>/<filename>
# Atomic: copy to `.<name>.tmp` then `mv` (rename is atomic on
# POSIX) so an analyst tailing the share never sees a partial file.
#
# Driven by a systemd unit on the SCIF host (see pytho-finalize-export.service
# in the same directory). Independent of docker-api lifecycle.
#
# REMOVE: rm -rf scripts/finalize_export/ in this repo, then on the
# SCIF host `systemctl disable --now pytho-finalize-export` and
# delete /etc/systemd/system/pytho-finalize-export.service. No other
# code in pytho-app references this directory.

set -euo pipefail

WATCH_ROOT="${PYTHO_FINALIZE_WATCH_DIR:-$HOME/pytho-app/uploads/_autodraft}"
DEST="${PYTHO_FINALIZE_EXPORT_DIR:-}"

if [ -z "$DEST" ]; then
    echo "PYTHO_FINALIZE_EXPORT_DIR unset — exiting 0 (feature disabled)." >&2
    exit 0
fi
if [ ! -d "$WATCH_ROOT" ]; then
    echo "watch root $WATCH_ROOT does not exist — exiting 1." >&2
    exit 1
fi
if [ ! -d "$DEST" ]; then
    echo "export dir $DEST does not exist — exiting 1." >&2
    exit 1
fi

command -v inotifywait >/dev/null 2>&1 || {
    echo "inotifywait not installed (apt install inotify-tools) — exiting 1." >&2
    exit 1
}

echo "finalize-export watching $WATCH_ROOT → $DEST/<product_type>/" >&2

# inotifywait emits one line per event; we care about new export files
# (close_write covers both fresh writes and renames-in). We also need
# the FINAL state — the publish endpoint flips status AFTER the export
# file already exists, so we ALSO need to watch schema_state.json
# close_writes (that's when the publish hook persisted the new status).
inotifywait -m -q -r -e close_write -e moved_to --format '%w%f' "$WATCH_ROOT" |
while IFS= read -r FILE; do
    # Two trigger paths:
    #   (a) the export file (.docx / .pptx / .xlsx) was written — handle if
    #       schema_state.json says published.
    #   (b) schema_state.json was rewritten with a new status — handle the
    #       sibling export file if status is now published.
    case "$FILE" in
        *.docx|*.pptx|*.xlsx)
            ARTIFACT_DIR="$(dirname "$FILE")"
            STATE="$ARTIFACT_DIR/schema_state.json"
            EXPORT_FILE="$FILE"
            ;;
        */schema_state.json)
            ARTIFACT_DIR="$(dirname "$FILE")"
            STATE="$FILE"
            # Find the export file in this dir (.docx / .pptx / .xlsx).
            EXPORT_FILE=""
            for EXT in docx pptx xlsx; do
                cand="$ARTIFACT_DIR/$(basename "$ARTIFACT_DIR").$EXT"
                if [ -f "$cand" ]; then
                    EXPORT_FILE="$cand"
                    break
                fi
            done
            [ -z "$EXPORT_FILE" ] && continue
            ;;
        *)
            continue
            ;;
    esac

    [ -f "$STATE" ] || continue

    # Cheap published check — no jq dependency. Grep tolerates the JSON
    # being on one line or many.
    if ! grep -q '"status"[[:space:]]*:[[:space:]]*"published"' "$STATE"; then
        continue
    fi

    # Per-product subfolder. Derive product_type from schema_state.json's
    # top-level product_type field (canonical). Falls back to splitting
    # the artifact_id on the timestamp suffix if the JSON read fails.
    PRODUCT_TYPE=$(grep -oE '"product_type"[[:space:]]*:[[:space:]]*"[^"]+"' "$STATE" |
                      head -1 |
                      sed -E 's/.*"product_type"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
    if [ -z "$PRODUCT_TYPE" ]; then
        # Fallback: artifact dir name is <PRODUCT_TYPE>_<DTG>_<ms>Z.
        # Strip the trailing _YYYYMMDDTHHMMSS_msZ portion.
        BASENAME="$(basename "$ARTIFACT_DIR")"
        PRODUCT_TYPE="$(echo "$BASENAME" | sed -E 's/_[0-9]{8}T[0-9]{6}_[0-9]+Z$//')"
    fi
    # Sanitize for filesystem use — replace anything that's not [A-Za-z0-9_-]
    # with an underscore. Keeps "Threat_COAs", "Threat_COA_Statement", etc.
    # safe even if the schema's product_type string had spaces.
    PRODUCT_TYPE="$(echo "$PRODUCT_TYPE" | tr -c 'A-Za-z0-9_-' '_' | sed 's/_*$//')"
    [ -z "$PRODUCT_TYPE" ] && {
        echo "skip: could not derive product_type for $EXPORT_FILE" >&2
        continue
    }

    DEST_DIR="$DEST/$PRODUCT_TYPE"
    mkdir -p "$DEST_DIR" || {
        echo "skip: mkdir $DEST_DIR failed" >&2
        continue
    }

    NAME="$(basename "$EXPORT_FILE")"
    TMP="$DEST_DIR/.$NAME.tmp"
    DST="$DEST_DIR/$NAME"

    if cp -- "$EXPORT_FILE" "$TMP" && mv -- "$TMP" "$DST"; then
        echo "exported $PRODUCT_TYPE/$NAME" >&2
    else
        echo "skip: copy failed for $EXPORT_FILE → $DST" >&2
        rm -f -- "$TMP" 2>/dev/null || true
    fi
done
