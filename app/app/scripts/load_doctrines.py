"""
Admin loader: bulk-import doctrine documents via the backend API.

Doctrine documents are read-only in the UI; this script is the supported way
to seed and refresh the doctrine corpus.

Usage:
    cd app/app
    python scripts/load_doctrines.py /path/to/usmc_doctrine_index.csv
    python scripts/load_doctrines.py /path/to/usmc_doctrine_index.csv --purge
    python scripts/load_doctrines.py /path/to/index.csv --api-url http://myhost:8000

The CSV MUST have these columns (any other columns are ignored):
    title    — curated, human-readable title for the document
    file_id  — path to the document file, resolved relative to the CSV's
               parent directory (e.g. "pdfs/fm2-0.pdf" or "subdir/mcdp-1.pdf").
               Only *.pdf / *.docx / *.txt files are supported for chunking.

For each row, the file is uploaded to the backend via POST /api/upload_doctrine
which handles copying to the uploads dir, indexing into the doctrine_chunks ES
index, and embedding the title into each chunk.

Pass --purge to wipe the doctrine index entirely before loading (calls
DELETE /api/purge_doctrines).
"""
import argparse
import csv
import logging
import sys
from pathlib import Path

import requests

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt"}
REQUIRED_COLUMNS = ("title", "file_id")
DEFAULT_API_URL = "http://localhost:8000"


def _read_manifest(csv_path: Path) -> list[tuple[str, Path]]:
    """
    Parse the CSV and return [(title, resolved_source_path), ...].

    `file_id` column values are resolved relative to the CSV's parent dir.
    """
    base_dir = csv_path.parent
    rows: list[tuple[str, Path]] = []

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise SystemExit(f"CSV is empty: {csv_path}")

        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise SystemExit(
                f"CSV {csv_path} is missing required column(s): {', '.join(missing)}. "
                f"Expected columns: {', '.join(REQUIRED_COLUMNS)}."
            )

        for lineno, row in enumerate(reader, start=2):  # header is line 1
            title = (row.get("title") or "").strip()
            rel = (row.get("file_id") or "").strip()
            if not title or not rel:
                print(f"  [skip] line {lineno}: blank title or file_id")
                continue

            source = (base_dir / rel).resolve()
            if not source.exists() or not source.is_file():
                print(f"  [skip] line {lineno}: file not found — {source}")
                continue
            if source.suffix.lower() not in SUPPORTED_SUFFIXES:
                print(f"  [skip] line {lineno}: unsupported type {source.suffix} — {source.name}")
                continue

            rows.append((title, source))

    return rows


def load(csv_path: Path, api_url: str, purge: bool = False) -> None:
    if purge:
        print("Purging doctrine index...")
        resp = requests.delete(f"{api_url}/api/purge_doctrines")
        resp.raise_for_status()
        result = resp.json()
        if result.get("deleted"):
            print("  Doctrine index deleted.")
        else:
            print("  No existing doctrine index to delete.")

    manifest = _read_manifest(csv_path)
    if not manifest:
        print(f"No usable rows found in {csv_path}")
        return

    print(f"Uploading {len(manifest)} doctrine file(s) via {api_url}")

    total_chunks = 0
    for title, source in manifest:
        try:
            with open(source, "rb") as f:
                resp = requests.post(
                    f"{api_url}/api/upload_doctrine",
                    files={"file": (source.name, f)},
                    data={"title": title},
                    timeout=300,
                )
            resp.raise_for_status()
            result = resp.json()
            chunks = result.get("chunks_indexed") or result.get("inserted") or 0
            total_chunks += chunks
            print(f"  [{chunks:>4} chunks] {title}  ({source.name})")
            if result.get("errors"):
                print(f"      errors: {result['errors']}")
        except Exception as e:
            logging.error(f"Failed to upload {source}: {e}", exc_info=True)

    print(f"Done. {total_chunks} chunks indexed across {len(manifest)} file(s).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "csv_path",
        type=Path,
        help="CSV manifest with 'title' and 'file_id' columns. "
             "file_id paths are resolved relative to the CSV's directory.",
    )
    ap.add_argument("--purge", action="store_true", help="Drop and recreate the doctrine index before loading")
    ap.add_argument("--api-url", type=str, default=DEFAULT_API_URL, help=f"Backend API base URL (default: {DEFAULT_API_URL})")
    args = ap.parse_args()

    if not args.csv_path.exists() or not args.csv_path.is_file():
        ap.error(f"CSV path does not exist or is not a file: {args.csv_path}")
    if args.csv_path.suffix.lower() != ".csv":
        ap.error(f"Expected a .csv file, got: {args.csv_path}")

    load(args.csv_path, api_url=args.api_url, purge=args.purge)


if __name__ == "__main__":
    main()
