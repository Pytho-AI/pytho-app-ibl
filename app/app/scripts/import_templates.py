"""
Edge-side importer for product templates.

Reads a bundle containing:
    <input>/manifest.json   -- rows in edge schema shape (column names match
                               the ProductType model: product_type_id,
                               user_id, email, role, product_name,
                               product_overview, file_key, file_name,
                               created_at, updated_at)
    <input>/files/<file_key> -- one file per row

Copies files into the edge uploads dir, inserts rows into local SQLite, and
runs _generate_template_files() for supported extensions (.pdf / .docx).
Other extensions (.xlsx, etc.) are still copied and inserted, but HTML
generation is skipped with a warning.

Run from app/app so relative paths (uploads/, data/pytho.db) match the
running backend:
    cd pytho-app/app/app
    python scripts/import_templates.py --input /tmp/template_bundle

Flags:
    --overwrite       replace rows that already exist by product_type_id
    --skip-existing   (default) leave existing rows alone
    --dry-run         print what would happen, don't touch DB or disk
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# Ensure we can import from the edge backend package regardless of cwd.
_HERE = Path(__file__).resolve()
_APP_ROOT = _HERE.parent.parent  # .../pytho-app/app/app
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from utils.database import SessionLocal, ProductType, init_db  # noqa: E402
from routers.api.product_types import _generate_template_files, UPLOAD_DIR  # noqa: E402

EDGE_COLUMNS = [
    "product_type_id",
    "user_id",
    "email",
    "role",
    "product_name",
    "product_overview",
    "file_key",
    "file_name",
    "created_at",
    "updated_at",
]
SUPPORTED_EXTS = (".pdf", ".docx")


def _row_fields(item: dict) -> dict:
    now_ms = int(time.time() * 1000)
    fields = {col: item.get(col) for col in EDGE_COLUMNS}
    if not fields.get("user_id"):
        fields["user_id"] = "__migrated__"
    if fields.get("product_overview") is None:
        fields["product_overview"] = ""
    if not fields.get("created_at"):
        fields["created_at"] = now_ms
    if not fields.get("updated_at"):
        fields["updated_at"] = now_ms
    return fields


def _copy_file(src_dir: Path, file_key: str, dry_run: bool) -> Path:
    src = src_dir / file_key
    if not src.exists():
        raise FileNotFoundError(f"Missing source file: {src}")
    dest_dir = Path(UPLOAD_DIR)
    dest = dest_dir / file_key
    if dry_run:
        print(f"    [dry-run] copy {src} -> {dest}")
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def _is_supported(file_key: str) -> bool:
    return file_key.lower().endswith(SUPPORTED_EXTS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Bundle dir (manifest.json + files/)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--overwrite", action="store_true")
    mode.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--include-unsupported",
        action="store_true",
        help="Import rows whose file extension isn't .pdf/.docx (xlsx, etc.). "
             "By default these are skipped since the backend parser can't "
             "generate HTML for them.",
    )
    args = ap.parse_args()

    bundle = Path(args.input)
    manifest_path = bundle / "manifest.json"
    files_dir = bundle / "files"
    if not manifest_path.exists():
        print(f"manifest.json not found at {manifest_path}", file=sys.stderr)
        return 1
    if not files_dir.exists():
        print(f"files dir not found at {files_dir}", file=sys.stderr)
        return 1

    with manifest_path.open() as f:
        items = json.load(f)

    if not args.dry_run:
        init_db()

    db = SessionLocal()
    imported = 0
    skipped = 0
    skipped_unsupported = 0
    updated = 0
    failed = 0
    html_skipped = 0

    try:
        for item in items:
            pt_id = item.get("product_type_id")
            name = item.get("product_name")
            file_key = item.get("file_key")
            if not pt_id or not file_key:
                print(f"  SKIP (bad row): {item}")
                failed += 1
                continue

            print(f"- {name} [{pt_id}]")

            if not _is_supported(file_key) and not args.include_unsupported:
                print(f"    SKIP (unsupported ext): {file_key}")
                skipped_unsupported += 1
                continue

            existing = (
                db.query(ProductType)
                .filter(ProductType.product_type_id == pt_id)
                .first()
            )

            if existing and not args.overwrite:
                print("    exists, skipping")
                skipped += 1
                continue

            try:
                _copy_file(files_dir, file_key, args.dry_run)
            except FileNotFoundError as e:
                print(f"    FAIL: {e}")
                failed += 1
                continue

            fields = _row_fields(item)

            if args.dry_run:
                print(f"    [dry-run] upsert ProductType {fields}")
                if _is_supported(file_key):
                    print(f"    [dry-run] generate HTML for {file_key}")
                else:
                    print(f"    [dry-run] SKIP HTML generation (unsupported ext)")
                imported += 1
                continue

            if existing:
                for k, v in fields.items():
                    setattr(existing, k, v)
                updated += 1
            else:
                db.add(ProductType(**fields))
                imported += 1

            if _is_supported(file_key):
                try:
                    _generate_template_files(file_key)
                except Exception as e:
                    print(f"    WARN: HTML generation failed: {e}")
            else:
                print(f"    SKIP HTML generation (unsupported ext: {file_key})")
                html_skipped += 1

        if not args.dry_run:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print()
    print(
        f"Done. imported={imported} updated={updated} skipped={skipped} "
        f"skipped_unsupported={skipped_unsupported} failed={failed} "
        f"html_skipped={html_skipped}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
