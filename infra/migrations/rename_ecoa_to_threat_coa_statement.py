"""One-shot data migration to follow the code rename ECOA -> Threat COA Statement.

Run AFTER the renamed code is deployed on spark-dev. Idempotent — safe to
re-run if the first pass aborted mid-way.

Three passes:
  1. DB: walk every SessionDetail row, JSON-mutate any 'ECOA' artifact_type
     fields + any 'ECOA_<ts>' artifact_id strings nested anywhere in the
     dispatch / emit / structured_data tree.
  2. Filesystem: rename every uploads/<owner>/ECOA_<ts>/ dir to
     uploads/<owner>/Threat_COA_Statement_<ts>/.
  3. Sidecar JSON files: scan every artifact dir's schema_state.json,
     map_embeds.json, render_params_*.json for ECOA references and
     rewrite them in-place. Catches cross-references where one product
     points at an ECOA artifact_id by string.

The 'ECOA' -> 'Threat COA Statement' string substitution is bracketed by
word boundaries so we don't mangle unrelated text. The 'ECOA_<ts>'
artifact-id substitution requires an 8-char date + 'T' suffix so a bare
'ECOA' in human prose doesn't trigger it.

Run inside the docker-api container so the sqlalchemy + UPLOAD_DIR
imports resolve against the production session.

Usage (on spark-dev):
    docker exec docker-api python3 /app/infra/migrations/rename_ecoa_to_threat_coa_statement.py
"""
from __future__ import annotations
import json
import re
import shutil
import sys
from pathlib import Path

# When running inside the docker-api container, /app is the app root.
sys.path.insert(0, "/app")

# Lazy imports so this can be run from outside the container for dry-run
# checks too (catch import errors before --apply).
try:
    from utils import database
    from handlers.product import UPLOAD_DIR
except Exception as exc:
    sys.stderr.write(f"DB imports failed (must run inside docker-api): {exc}\n")
    sys.exit(2)


OLD = "ECOA"
NEW = "Threat COA Statement"
OLD_AID_PATTERN = re.compile(r"\bECOA_(\d{8}T\d{6}Z(?:_\d+)?)\b")
NEW_AID_TEMPLATE = r"Threat_COA_Statement_\1"


def rewrite_string(s: str) -> str:
    """Apply both ECOA -> Threat COA Statement substitutions to a string."""
    s = OLD_AID_PATTERN.sub(NEW_AID_TEMPLATE, s)
    # Standalone OLD as artifact_type / product_type. Use word boundaries
    # so 'ECOA_xxx' (handled above) and 'somethingECOAother' aren't hit.
    s = re.sub(r"\bECOA\b", NEW, s)
    return s


def rewrite_tree(obj):
    """Recursively rewrite any string values in a nested dict/list."""
    if isinstance(obj, str):
        return rewrite_string(obj)
    if isinstance(obj, list):
        return [rewrite_tree(x) for x in obj]
    if isinstance(obj, dict):
        return {k: rewrite_tree(v) for k, v in obj.items()}
    return obj


# --- Pass 1: DB SessionDetail rows --------------------------------------

def migrate_db() -> dict:
    """Walk SessionDetail rows; rewrite .data JSON in-place."""
    db = database.SessionLocal()
    try:
        rows = db.query(database.SessionDetail).all()
        scanned = 0
        mutated = 0
        for row in rows:
            scanned += 1
            raw = row.data or ""
            if "ECOA" not in raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            new_payload = rewrite_tree(payload)
            new_raw = database.null_safe_json_dumps(new_payload)
            if new_raw != raw:
                row.data = new_raw
                db.add(row)
                mutated += 1
        db.commit()
        return {"scanned": scanned, "mutated": mutated}
    finally:
        db.close()


# --- Pass 2: filesystem dir renames -------------------------------------

def migrate_fs_dirs() -> dict:
    """Rename every uploads/<owner>/ECOA_<ts>/ dir to the new name."""
    root = Path(UPLOAD_DIR)
    renamed = 0
    skipped_collision = 0
    for owner_dir in root.iterdir():
        if not owner_dir.is_dir():
            continue
        for artifact_dir in owner_dir.iterdir():
            if not artifact_dir.is_dir():
                continue
            new_name = rewrite_string(artifact_dir.name)
            if new_name == artifact_dir.name:
                continue
            target = owner_dir / new_name
            if target.exists():
                # Don't clobber a pre-existing dir under the new name.
                # (Could happen if migration runs twice with overlapping
                # state, or if both names somehow coexist.)
                sys.stderr.write(
                    f"  SKIP collision: {artifact_dir} -> {target} "
                    f"(target already exists)\n"
                )
                skipped_collision += 1
                continue
            artifact_dir.rename(target)
            renamed += 1
    return {"renamed": renamed, "skipped_collision": skipped_collision}


# --- Pass 3: sidecar JSON files inside artifact dirs --------------------

# File-name patterns whose contents we rewrite. Anything else gets left
# alone — random PNGs / DOCX / PPTX are binary and stay as-is. The PPTX
# click-to-edit hyperlinks DO point at artifact_ids by string, but those
# live inside the zip and rewriting them would require unzip/rezip; left
# as a known minor regression (Edit-Map deep links from old artifacts
# carry stale from_artifact=... param until those artifacts are
# regenerated).
SIDECAR_PATTERNS = (
    "schema_state.json",
    "map_embeds.json",
)
SIDECAR_GLOBS = (
    "render_params_*.json",
    "overlays_*.json",
)


def migrate_sidecars() -> dict:
    root = Path(UPLOAD_DIR)
    scanned = 0
    mutated = 0
    for owner_dir in root.iterdir():
        if not owner_dir.is_dir():
            continue
        for artifact_dir in owner_dir.iterdir():
            if not artifact_dir.is_dir():
                continue
            sidecars: list[Path] = []
            for name in SIDECAR_PATTERNS:
                p = artifact_dir / name
                if p.is_file():
                    sidecars.append(p)
            for glob in SIDECAR_GLOBS:
                sidecars.extend(p for p in artifact_dir.glob(glob) if p.is_file())
            for sc in sidecars:
                scanned += 1
                try:
                    raw = sc.read_text(encoding="utf-8")
                except Exception:
                    continue
                if "ECOA" not in raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    # Not JSON? Fall back to plain string replace so we
                    # don't strand orphan ECOA references.
                    new_raw = rewrite_string(raw)
                    if new_raw != raw:
                        sc.write_text(new_raw, encoding="utf-8")
                        mutated += 1
                    continue
                new_payload = rewrite_tree(payload)
                new_raw = json.dumps(new_payload, indent=2)
                if new_raw != raw:
                    sc.write_text(new_raw, encoding="utf-8")
                    mutated += 1
    return {"scanned": scanned, "mutated": mutated}


def main():
    print(f"migration: {OLD!r} -> {NEW!r}")
    print()
    print("== pass 1: DB SessionDetail rows ==")
    r1 = migrate_db()
    print(f"   scanned={r1['scanned']} mutated={r1['mutated']}")
    print()
    print("== pass 2: filesystem artifact dirs ==")
    r2 = migrate_fs_dirs()
    print(f"   renamed={r2['renamed']} skipped_collision={r2['skipped_collision']}")
    print()
    print("== pass 3: sidecar JSON files ==")
    r3 = migrate_sidecars()
    print(f"   scanned={r3['scanned']} mutated={r3['mutated']}")
    print()
    print("done")


if __name__ == "__main__":
    main()
