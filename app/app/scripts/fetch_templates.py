"""
Cloud-side fetcher for product templates.

Reads the prod pytho-app SQLite DB, filters rows via a SQL WHERE clause,
downloads each row's file from the Uploads S3 bucket via the `aws` CLI, then
writes a bundle directory you can zip and transport to the edge box.

The bundle layout is:
    <output>/manifest.json    -- rows whose S3 file was successfully downloaded
    <output>/files/<file_key> -- one file per row

Run `import_templates.py` on the edge box with --input pointing at this bundle.

Requirements: `aws` CLI and `jq` are NOT required — this uses Python stdlib
(sqlite3, json, subprocess) plus a working `aws s3 cp` command. Configure
credentials with `aws configure` or rely on the EC2 instance role.

Usage:
    python scripts/fetch_templates.py \
        --db ./data/pytho.db \
        --bucket pytho-vite1-uploadsbucket-ucdtoaun \
        --output /tmp/bundle

Options:
    --prefix public/                -- S3 key prefix (default: public/)
    --where "email LIKE '%@pytho.ai'"
                                    -- SQL WHERE clause (no WHERE keyword),
                                       default picks the pytho.ai internal
                                       templates
    --profile <aws-profile>         -- AWS profile passed to `aws` CLI

Notes:
  - Rows whose S3 object returns 404 are silently dropped from the final
    manifest, so the bundle stays consistent (no dangling rows referencing
    missing files).
  - Columns unknown to the edge schema (e.g. is_dow) are ignored by the
    edge import script, so `SELECT *` is fine and stays forward-compatible.
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


DEFAULT_WHERE = "email LIKE '%@pytho.ai'"


def dump_rows(db_path: Path, where: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        sql = f"SELECT * FROM product_types WHERE {where}"
        cur = conn.execute(sql)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def download_file(bucket: str, prefix: str, key: str, dest: Path, profile: str | None) -> bool:
    """Download a single S3 object via `aws s3 cp`. Return True on success."""
    if not shutil.which("aws"):
        sys.stderr.write("aws CLI not found on PATH\n")
        sys.exit(1)

    cmd = ["aws"]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["s3", "cp", f"s3://{bucket}/{prefix}{key}", str(dest)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  OK   {key}")
        return True
    # aws cli writes the fatal error to stderr
    err = (result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout) else "unknown"
    print(f"  FAIL {key}: {err}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="Path to prod SQLite pytho.db")
    ap.add_argument("--bucket", required=True, help="S3 uploads bucket name")
    ap.add_argument("--prefix", default="public/", help="S3 key prefix (default: public/)")
    ap.add_argument("--where", default=DEFAULT_WHERE, help="SQL WHERE clause (no WHERE keyword)")
    ap.add_argument("--profile", default=None, help="AWS CLI profile (optional)")
    ap.add_argument("--output", default="/tmp/bundle", help="Bundle dir (default: /tmp/bundle)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.stderr.write(f"DB not found: {db_path}\n")
        return 1

    out_dir = Path(args.output)
    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    print(f"Querying {db_path} WHERE {args.where}")
    rows = dump_rows(db_path, args.where)
    print(f"Found {len(rows)} row(s)\n")
    if not rows:
        return 0

    print(f"Downloading files from s3://{args.bucket}/{args.prefix}")
    kept: list[dict] = []
    for row in rows:
        key = row.get("file_key")
        if not key:
            print(f"  SKIP {row.get('product_type_id')}: no file_key")
            continue
        dest = files_dir / key
        if download_file(args.bucket, args.prefix, key, dest, args.profile):
            kept.append(row)

    manifest_path = out_dir / "manifest.json"
    with manifest_path.open("w") as f:
        json.dump(kept, f, indent=2, default=str)

    missing = len(rows) - len(kept)
    print()
    print(f"Wrote {len(kept)} row(s) to {manifest_path}")
    if missing:
        print(f"Dropped {missing} row(s) whose S3 files were missing")
    print(f"\nNext: zip -r template_bundle.zip {out_dir.name} (from {out_dir.parent})")
    print("Then transport and run scripts/import_templates.py on edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
