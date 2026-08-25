"""Schema-driven XLSX exporter — sibling to exporter.py for the 15
doctrinally-XLSX products.

Per scratch/ibl/reports/EXCEL_DOCTRINAL_ALIGNMENT.md:
  * 10 pure XLSX products (HPTL, HVTL, TSS, AGM, TSM, CARVER, ICM, NSL,
    RTL, WEM) — doctrine publishes them as living matrices / ledgers
    sorted, filtered, and edited cell-by-cell by the targeting working
    group.
  * 5 spin-off XLSX siblings (CCA-Matrix, CA-Engagements, Event-Matrix,
    DST-Matrix, Threat-Description-Table) — extracted matrix portions
    of hybrid products. Parent DOCX/PPTX continues to render the
    narrative half.

Output shape:
  Sheet 1 — "<product_name>" — data sheet (rows or matrix)
  Sheet 2 — "Header" — header_fields rendered as label/value pairs,
            with the classification banner in row 1 (filled cell across
            the columns used by the data sheet, matching exporter.py's
            DoDM 5200.01 swatch).
  Sheet 3 — "_Provenance" (hidden) — full _provenance metadata indexed
            by cell address so analysts can drill in, and so the agent
            can patch provenance by addressing a single cell.

Provenance UX:
  * Section-level provenance: the section banner row carries a cell
    comment with the source_doc_tag (or LLM-shorthand id) so the analyst
    sees the source without leaving the row.
  * Cell-level provenance: individual cells carry comments when
    provenance_required is set on the field.
  * LLM shorthand: sections are also written as openpyxl named ranges
    so the agent can address them ("move section 'Fire Support' to
    position 1") without naming every row index.
  * Excel native row grouping (collapsible +/- gutter) wraps each
    section so analysts can fold sections during big-doc review.

The exporter contains ZERO per-product branches. Every dispatch goes
through schema shape inspection (`shape: row` / `shape: matrix` /
`matrix_spec` / `paragraph_fields[].shape: grid_table`).

CLI:
    python xlsx_exporter.py <schema_state.json> <output.xlsx>
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName
    from openpyxl.worksheet.table import Table, TableStyleInfo
except ImportError as e:
    raise RuntimeError(
        "openpyxl is required for XLSX export. Add `openpyxl>=3.1` to "
        "pytho-app/app/requirements.txt."
    ) from e

from templates import PRODUCT_SCHEMAS
from utils.render_regions import (
    xlsx_safe_token as _xlsx_safe_token,
    xlsx_token as _xlsx_token,
    xlsx_header_token as _xlsx_header_token,
    xlsx_register_defined_name as _xlsx_register_defined_name,
)

log = logging.getLogger(__name__)

# Mirrors exporter.py CLASSIFICATION_COLORS. Banner row 1 fills with this.
CLASSIFICATION_COLORS = {
    "UNCLASSIFIED":      {"fill": "007A33", "text": "FFFFFF"},
    "CUI":               {"fill": "522D80", "text": "FFFFFF"},
    "CONFIDENTIAL":      {"fill": "0033A0", "text": "FFFFFF"},
    "SECRET":            {"fill": "C8102E", "text": "FFFFFF"},
    "TOP SECRET":        {"fill": "FF8C00", "text": "000000"},
    "TOP SECRET//SCI":   {"fill": "FFD700", "text": "000000"},
}

# Section banner row fill (one shade lighter than table header so sections
# are visually distinct from the column headers within them).
SECTION_FILL = "DCE6F1"
TABLE_HEADER_FILL = "D9D9D9"

THIN = Side(border_style="thin", color="000000")
BORDER_ALL = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)


def _classification_swatch(marking: Optional[str]) -> dict:
    m = (marking or "UNCLASSIFIED").strip().upper()
    if m in CLASSIFICATION_COLORS:
        return CLASSIFICATION_COLORS[m]
    base = m.split("//", 1)[0].strip()
    return CLASSIFICATION_COLORS.get(base, CLASSIFICATION_COLORS["UNCLASSIFIED"])


def _safe_sheet_name(name: str) -> str:
    """Excel sheet names: ≤31 chars, no \\/?*[]:"""
    bad = set('\\/?*[]:')
    clean = "".join("_" if c in bad else c for c in (name or "Sheet"))
    return clean[:31] or "Sheet"


def _safe_name_token(name: str) -> str:
    """Defined-name token: letters/digits/underscore, can't start with digit."""
    clean = "".join(c if c.isalnum() or c == "_" else "_" for c in (name or "section"))
    if not clean or clean[0].isdigit():
        clean = "s_" + clean
    return clean[:255]


def _build_formula(spec: dict, col_letters: dict[str, str],
                    row_number: int) -> Optional[str]:
    """If the field spec carries a `formula` attribute, resolve it to an
    Excel formula string anchored at `row_number`. Returns None when the
    spec has no formula or the referenced sibling fields aren't visible
    in this sheet (column not in col_letters).

    Supported forms:
        "sum:fieldA,fieldB,fieldC,..."  →  "=SUM(D5,E5,F5)"
        "avg:fieldA,fieldB,..."         →  "=AVERAGE(...)"
        "min:..."   "max:..."            →  "=MIN(...)"   "=MAX(...)"

    Live formulas keep the analyst's edits coherent — change a score,
    the dependent totals recompute. The writer LLM's emitted value (if
    any) is ignored in favor of the formula because the doctrinal
    expectation is computed, not asserted (e.g. CARVER composite = sum
    of the six criterion scores per FM 3-60 App. G Table G-1)."""
    if not isinstance(spec, dict):
        return None
    expr = spec.get("formula")
    if not expr or not isinstance(expr, str):
        return None
    if ":" not in expr:
        return None
    fn, _, args = expr.partition(":")
    fn = fn.strip().lower()
    refs = [a.strip() for a in args.split(",") if a.strip()]
    cells = [f"{col_letters[r]}{row_number}" for r in refs if r in col_letters]
    if not cells:
        return None
    op = {"sum": "SUM", "avg": "AVERAGE", "average": "AVERAGE",
          "min": "MIN", "max": "MAX"}.get(fn)
    if not op:
        return None
    return f"={op}({','.join(cells)})"


def _display_value(value: Any, spec: dict) -> Any:
    """Translate a raw schema value into the analyst-facing display form.

    Schemas declare `enum` (canonical machine values like
    "line_of_communication") alongside `enum_labels` (the human-readable
    "LOC" / "Reconnaissance and Surveillance" form). The XLSX surface
    is for the analyst, not the writer LLM — render the label. Falls
    back to a title-cased snake_case → "Title Case" conversion when
    the value is in `enum` but missing from `enum_labels`, and falls
    back to the raw value as a final stop. lists/dicts dump to JSON
    (keeps the cell editable as text without an openpyxl serialization
    error)."""
    if value is None:
        return ""
    labels = spec.get("enum_labels") if isinstance(spec, dict) else None
    if isinstance(labels, dict) and isinstance(value, str):
        if value in labels:
            return labels[value]
    enum = spec.get("enum") if isinstance(spec, dict) else None
    # Numeric enum (e.g. CARVER SCORE_1_10_ENUM = [1..10]) — coerce
    # string-form numeric values to int/float so the cell lands as a
    # NUMBER, not a string. Without this, a writer that emits "5"
    # produces a text cell, and any sibling SUM/AVG formula that
    # references it (CARVER composite_total_score is
    # =SUM(C..R) referencing each of the six score columns) returns
    # 0 because Excel's SUM() silently skips text cells. The schema
    # already constrains the value to the enum; coercion just
    # restores the declared int/float type at the cell-write
    # boundary.
    if isinstance(enum, (list, tuple)) and enum:
        _all_int = all(isinstance(e, int) and not isinstance(e, bool) for e in enum)
        _all_num = all(isinstance(e, (int, float)) and not isinstance(e, bool)
                       for e in enum)
        if _all_num:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                # Already numeric — return as-is (skip the string-enum branch).
                if value in enum:
                    return int(value) if _all_int else value
            if isinstance(value, str):
                try:
                    parsed = int(value) if _all_int else float(value)
                except (TypeError, ValueError):
                    parsed = None
                if parsed is not None and parsed in enum:
                    return parsed
    if isinstance(enum, (list, tuple)) and isinstance(value, str) and value in enum:
        # Enum value with no explicit label — humanize the snake_case
        # form so the analyst doesn't see fire_support / etc.
        return value.replace("_", " ").title()
    # Compact dict render — for capability-summary fields like HVTL's
    # capability_mix column (cougaar composition_by_fm360_category, a
    # dict of {fm360_category: count}) and the equipment-rollup column
    # (cougaar composition_rollup, ~90 items per unit). Default dict
    # render dumps JSON which bloats the cell with quotes + braces;
    # compact_dict renders as "K1: V1 / K2: V2 / ..." sorted by count
    # descending so the analyst sees the heaviest bucket first.
    # `render_limit` on the spec caps the number of entries (composition
    # rollups are 90-item dicts; capping at 10 keeps the cell scannable
    # and the heaviest doctrinally-significant items surface).
    if (isinstance(spec, dict) and spec.get("render") == "compact_dict"
            and isinstance(value, dict) and value):
        items = sorted(
            ((str(k), v) for k, v in value.items() if v),
            key=lambda kv: (-kv[1] if isinstance(kv[1], (int, float)) else 0, kv[0]),
        )
        # Doctrinal count separator is "× N" (Item × N) — reads as
        # "nine T-90A" not "T-90A scored nine" the way "T-90A: 9" does.
        limit = spec.get("render_limit")
        if isinstance(limit, int) and limit > 0 and len(items) > limit:
            kept = items[:limit]
            more = len(items) - limit
            return " / ".join(f"{k} × {v}" for k, v in kept) + f" / (+{more} more)"
        return " / ".join(f"{k} × {v}" for k, v in items)
    if isinstance(value, list):
        # List of scalars (strings / numbers) → comma-separated. PIR's
        # linked_nai_ids / linked_dp_ids and similar ID-list columns
        # emit as lists; rendering ["NAI-VALGA", "NAI-PIKASILLA"] as
        # a JSON literal in the cell is ugly + breaks downstream
        # sort/filter on substring match. Mixed lists or list-of-dicts
        # fall through to the JSON dump (preserves nested structure
        # for analyst editing).
        if all(isinstance(v, (str, int, float)) for v in value):
            return ", ".join(str(v) for v in value)
        return json.dumps(value, default=str)
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return value


def _provenance_summary(prov: Any) -> str:
    """Collapse a _provenance entry (dict, list, or string) into a one-line
    summary suitable for a cell comment. Full metadata stays on the
    _Provenance sheet."""
    if prov is None:
        return ""
    if isinstance(prov, str):
        return prov
    if isinstance(prov, dict):
        # Pull the most useful fields if present.
        for k in ("source_doc_tag", "source", "citation", "kbID", "doc_tag"):
            v = prov.get(k)
            if v:
                if "source_fragment" in prov:
                    frag = str(prov.get("source_fragment") or "")[:80]
                    return f"{v} — {frag}".strip(" —")
                return str(v)
        return json.dumps(prov, default=str)[:240]
    if isinstance(prov, list):
        # List of provenance entries — comma-joined summaries.
        return "; ".join(_provenance_summary(p) for p in prov if p)[:240]
    return str(prov)[:240]


# ---------------------------------------------------------------------------
# Top-of-sheet banner + compact metadata strip (no separate Header tab —
# the reference samples in scratch/docs/ibl/event_samples are single
# sheets with the table starting at row 1, no header chrome at all. We
# keep a classification banner because it's a doctrinal requirement, but
# everything else is one compact metadata row so the data is visible in
# the first viewport).
# ---------------------------------------------------------------------------
def _write_banner_rows(ws, schema: dict, structured: dict, col_span: int) -> int:
    """Write classification banner + compact metadata row at the top of
    the data sheet. Returns the row number where the next thing should
    start (typically the column-header row of the data table).

    Layout:
        Row 1: classification banner (merged across data columns)
        Row 2: compact metadata strip (DTG · Phase · AOR · Prepared by)
        Row 3: blank (visual separator)
        Row 4+: caller's content (data table column headers)
    """
    header = structured.get("header") or {}
    marking = (header.get("classification") or "UNCLASSIFIED").upper()
    sw = _classification_swatch(marking)

    span = max(col_span, 2)

    # Row 1: classification banner. Left-aligned so the text shows up
    # in the leftmost columns (visible without horizontal scroll even
    # when the data table is wider than the visible viewport / PDF page).
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=span)
    banner = ws.cell(row=1, column=1, value=f"  {marking}")
    banner.fill = PatternFill("solid", fgColor=sw["fill"])
    banner.font = Font(name="Arial", size=11, bold=True, color=sw["text"])
    banner.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 18

    # Row 2: compact metadata strip. Pull a curated set of useful header
    # fields, join with bullet separators, render as a single italic line.
    # Avoids the multi-row "Label: Value" table that was eating 20 rows
    # of vertical space in the prior multi-sheet layout.
    parts = []
    short_keys = ("phase_or_critical_event", "phase", "dtg", "as_of_dtg",
                  "area_of_operations", "prepared_by", "approving_commander")
    seen = set()
    for k in short_keys:
        if k in seen:
            continue
        seen.add(k)
        v = header.get(k)
        if not v or v == "{phase}" or v == "{area_of_operations}":
            continue
        if isinstance(v, (list, dict)):
            continue
        # Strip the "TBD —" sentinel the writer emits when a field is
        # genuinely unknown rather than usefully populated.
        sv = str(v).strip()
        if sv.startswith("TBD"):
            continue
        parts.append(sv)
    meta_text = "  ·  ".join(parts)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=span)
    meta_cell = ws.cell(row=2, column=1, value=f"  {meta_text}")
    meta_cell.font = Font(name="Arial", size=9, italic=True, color="595959")
    meta_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[2].height = 14

    # Row 3: thin visual separator.
    ws.row_dimensions[3].height = 4

    return 4  # next-content row


# ---------------------------------------------------------------------------
# Provenance sheet (hidden, full metadata)
# ---------------------------------------------------------------------------
def _write_provenance_sheet(wb: Workbook, structured: dict) -> None:
    ws = wb.create_sheet("_Provenance")
    ws.sheet_state = "hidden"
    ws.cell(row=1, column=1, value="cell_path").font = Font(bold=True)
    ws.cell(row=1, column=2, value="source_doc_tag").font = Font(bold=True)
    ws.cell(row=1, column=3, value="source_fragment").font = Font(bold=True)
    ws.cell(row=1, column=4, value="full_json").font = Font(bold=True)
    r = 2
    prov_root = structured.get("_provenance") or {}

    def _walk(prefix: str, node: Any):
        nonlocal r
        if isinstance(node, dict):
            # If this dict looks like a provenance leaf, write it; otherwise recurse.
            leaf_keys = {"source_doc_tag", "source", "source_fragment", "citation",
                         "kbID", "doc_tag"}
            if any(k in node for k in leaf_keys):
                ws.cell(row=r, column=1, value=prefix)
                ws.cell(row=r, column=2,
                         value=str(node.get("source_doc_tag") or node.get("source")
                                    or node.get("kbID") or ""))
                ws.cell(row=r, column=3,
                         value=str(node.get("source_fragment") or "")[:240])
                ws.cell(row=r, column=4,
                         value=json.dumps(node, default=str)[:1000])
                r += 1
            else:
                for k, v in node.items():
                    _walk(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(f"{prefix}[{i}]", v)
        else:
            ws.cell(row=r, column=1, value=prefix)
            ws.cell(row=r, column=4, value=str(node)[:240])
            r += 1

    _walk("", prov_root)
    for c, w in ((1, 36), (2, 24), (3, 60), (4, 60)):
        ws.column_dimensions[get_column_letter(c)].width = w


# ---------------------------------------------------------------------------
# Canonical-on-disk shadow sheets — _Header (header-field markers),
# _Workpaper (full marker registry + workpaper-view fields). Both hidden
# from the analyst tab strip. The importer reads these as the
# authoritative shadow registry when on-disk defined names have been
# lost to analyst edits (e.g. Cut+Paste in Calc).
# ---------------------------------------------------------------------------


def _write_header_sheet(wb: Workbook, schema: dict, structured: dict) -> None:
    """Hidden `_Header` sheet — one row per header_fields entry, column A
    = field name, column B = value. Defined names point at column B.

    Per the XLSX marker spec §3, this is Option A: hidden sheet carries
    stable scalar cells, the data sheet's banner row is a display-only
    mirror. The importer reads `pytho_header__<field_name>` markers and
    falls back to `_Header` column lookup when a marker is missing.
    """
    ws = wb.create_sheet("_Header")
    ws.sheet_state = "hidden"
    header = structured.get("header") or {}
    header_specs = schema.get("header_fields") or {}
    ws.cell(row=1, column=1, value="field_name").font = Font(bold=True)
    ws.cell(row=1, column=2, value="value").font = Font(bold=True)
    ws.cell(row=1, column=3, value="view").font = Font(bold=True)
    ws.cell(row=1, column=4, value="render_label").font = Font(bold=True)
    r = 2
    for fname, fspec in header_specs.items():
        ws.cell(row=r, column=1, value=fname)
        val = header.get(fname)
        if isinstance(val, (list, dict)):
            val = json.dumps(val, default=str)
        ws.cell(row=r, column=2, value="" if val is None else str(val))
        ws.cell(row=r, column=3, value=(fspec or {}).get("view") or "")
        ws.cell(row=r, column=4, value=(fspec or {}).get("render_label") or "")
        token = _xlsx_header_token(fname)
        _xlsx_register_defined_name(wb, token, "_Header", f"$B${r}")
        r += 1


def _write_workpaper_sheet(wb: Workbook, schema: dict, structured: dict,
                           registry: list[dict]) -> None:
    """Hidden `_Workpaper` sheet — full marker registry shadow + workpaper-
    view row_fields. Columns A-G per XLSX slice §8.1:

      A: marker_name          (e.g. "pytho_CARVER__row_3__criticality_score")
      B: sheet_name           (e.g. "CARVER")
      C: cell_ref             (e.g. "$E$8" — the expected static ref)
      D: row_index            (the `i` suffix, blank for non-row markers)
      E: field_name           (e.g. "criticality_score")
      F: view                 ("published" / "workpaper" / "internal")
      G: value_snapshot       (the value at render time)

    Plus workpaper-view row_fields appended below the registry section,
    with their own pytho_<slug>__row_<i>__<workpaper_field> markers
    pointing at column G of the workpaper-shadow row.
    """
    ws = wb.create_sheet("_Workpaper")
    ws.sheet_state = "hidden"
    headers = ["marker_name", "sheet_name", "cell_ref", "row_index",
               "field_name", "view", "value_snapshot"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=h).font = Font(bold=True)
    r = 2
    for entry in registry:
        ws.cell(row=r, column=1, value=entry.get("marker_name", ""))
        ws.cell(row=r, column=2, value=entry.get("sheet_name", ""))
        ws.cell(row=r, column=3, value=entry.get("cell_ref", ""))
        ri = entry.get("row_index")
        ws.cell(row=r, column=4, value=ri if isinstance(ri, int) else "")
        ws.cell(row=r, column=5, value=entry.get("field_name", ""))
        ws.cell(row=r, column=6, value=entry.get("view", "published"))
        val = entry.get("value_snapshot")
        if isinstance(val, (list, dict)):
            val = json.dumps(val, default=str)
        ws.cell(row=r, column=7, value="" if val is None else str(val))
        r += 1

    # Workpaper-view row_fields shadow.
    workpaper_fields = []
    for fname, fspec in (schema.get("row_fields") or {}).items():
        if (fspec or {}).get("view") == "workpaper":
            workpaper_fields.append((fname, fspec or {}))
    if workpaper_fields:
        ws.cell(row=r, column=1, value="--- workpaper rows ---").font = Font(italic=True)
        r += 1
        rows = structured.get("rows") or []
        sheet_slug = _xlsx_safe_token(schema.get("name") or "Sheet")
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for fname, fspec in workpaper_fields:
                val = row.get(fname)
                if isinstance(val, (list, dict)):
                    val = json.dumps(val, default=str)
                ws.cell(row=r, column=1, value=f"{sheet_slug}.workpaper.row_{i}.{fname}")
                ws.cell(row=r, column=2, value="_Workpaper")
                ws.cell(row=r, column=3, value=f"$G${r}")
                ws.cell(row=r, column=4, value=i)
                ws.cell(row=r, column=5, value=fname)
                ws.cell(row=r, column=6, value="workpaper")
                ws.cell(row=r, column=7, value="" if val is None else str(val))
                token = _xlsx_token(schema.get("name") or "Sheet",
                                     ["rows", i, fname])
                _xlsx_register_defined_name(wb, token, "_Workpaper", f"$G${r}")
                r += 1


def _write_field_map_sheet(wb: Workbook, schema: dict) -> None:
    """Hidden `_FieldMap` sheet — column index → schema field name.

    The XLSX importer uses this to invert column position to field name
    without inverting render_label heuristically. One row per
    (sheet_slug, column_index, field_name, render_label) tuple.
    """
    ws = wb.create_sheet("_FieldMap")
    ws.sheet_state = "hidden"
    ws.cell(row=1, column=1, value="sheet_slug").font = Font(bold=True)
    ws.cell(row=1, column=2, value="column_index").font = Font(bold=True)
    ws.cell(row=1, column=3, value="field_name").font = Font(bold=True)
    ws.cell(row=1, column=4, value="render_label").font = Font(bold=True)
    r = 2
    sheet_slug = _xlsx_safe_token(schema.get("name") or "Sheet")
    for i, (fname, fspec) in enumerate(_published_row_fields(schema), start=1):
        ws.cell(row=r, column=1, value=sheet_slug)
        ws.cell(row=r, column=2, value=i)
        ws.cell(row=r, column=3, value=fname)
        ws.cell(row=r, column=4, value=(fspec or {}).get("render_label") or fname)
        r += 1


def _ensure_marker_registry(wb: Workbook) -> list[dict]:
    """Lazy attach a marker registry list to the workbook. Per-cell write
    loops append to this list; _write_workpaper_sheet flushes it to the
    hidden registry sheet at the end of render."""
    reg = getattr(wb, "_pytho_marker_registry", None)
    if reg is None:
        reg = []
        try:
            setattr(wb, "_pytho_marker_registry", reg)
        except Exception:
            pass
    return reg


def _record_marker(wb: Workbook, marker_name: str, sheet_name: str,
                   cell_ref: str, row_index: Optional[int], field_name: str,
                   view: str, value: Any) -> None:
    """Append one marker to the workbook's registry. Used by per-cell
    write loops to build the `_Workpaper` shadow registry."""
    reg = _ensure_marker_registry(wb)
    reg.append({
        "marker_name": marker_name,
        "sheet_name": sheet_name,
        "cell_ref": cell_ref,
        "row_index": row_index,
        "field_name": field_name,
        "view": view,
        "value_snapshot": value,
    })


# ---------------------------------------------------------------------------
# Row-shape data sheet
# ---------------------------------------------------------------------------
def _published_row_fields(schema: dict) -> list[tuple[str, dict]]:
    """Return (field_name, spec) tuples for row_fields that appear in
    the analyst-facing XLSX surface. ONLY `view: "published"` makes it
    in — workpaper fields are analytical scratch (justification text,
    upstream cross-refs, dependency-chain workings) and don't belong
    on the doctrinal deliverable.

    Per doctrine, the reference samples in scratch/docs/ibl/event_samples
    publish only the canonical columns:
        CARVER:  Target Node | C | A | R | V | E | R | Total Score
        HVTL:    Threat Element | High-Value Target | Tactical Capability
    Rationale / justification / upstream-artifact-ref fields are the
    analyst's workpaper trail, not the published surface."""
    out = []
    for name, spec in (schema.get("row_fields") or {}).items():
        view = (spec or {}).get("view")
        if view != "published":
            continue
        out.append((name, spec or {}))
    return out


def _write_row_data_sheet(wb: Workbook, schema: dict, structured: dict,
                          sheet_name: str) -> int:
    """Write a row-shape data sheet. Returns column_count.

    Rows live under structured_data["rows"]. group_by groups rows into
    sections, each wrapped in an Excel Table (sort/filter) with a
    section banner row above its column headers carrying section-level
    provenance + LLM shorthand id as a cell comment.

    When there's only one section (either no group_by, or group_by
    produced a single bucket), the section banner is SUPPRESSED — it
    would just say "All" and waste a row. The reference samples
    (event_samples/CARVER Example.xlsx, HVTL Example.xlsx) start the
    column-header row immediately below the classification banner with
    no section chrome.
    """
    ws = wb.create_sheet(_safe_sheet_name(sheet_name))
    fields = _published_row_fields(schema)
    col_count = len(fields)
    rows = list(structured.get("rows") or [])
    group_by = schema.get("group_by")

    # Top-of-sheet chrome (classification banner + compact metadata).
    next_row = _write_banner_rows(ws, schema, structured, col_span=col_count or 2)

    # Group rows by group_by value (or single section "All" if unset).
    # Honor `group_collapse_map` — many products bucket by an underlying
    # field (HVTL.category, FM 3-60 App. C value) but DISPLAY rows under
    # a coarser doctrinal grouping (HVTL → ADP 3-0 warfighting function,
    # so fire_support and air_defense_artillery collapse into "fires").
    # Without this collapse the section banners read like FM 3-60
    # categories instead of ATP 2-01.3 Fig 6-9 warfighting-function
    # buckets the published HVTL is supposed to use.
    sections: list[tuple[str, list[dict]]] = []
    collapse_map = schema.get("group_collapse_map") or {}
    section_labels = (schema.get("group_collapse_labels")
                       or schema.get("group_labels") or {})
    if group_by:
        # Preserve first-seen order.
        seen_order: list[str] = []
        bucket: dict[str, list[dict]] = {}
        for row in rows:
            raw = (row or {}).get(group_by)
            key = str(collapse_map.get(raw, raw) if raw else "Unassigned")
            if key not in bucket:
                bucket[key] = []
                seen_order.append(key)
            bucket[key].append(row)
        # Honor `group_collapse_order` (preferred) / `group_order` so
        # banners ship in doctrinal sequence (HVTL → ATP 2-01.3 Fig 6-9
        # warfighting-function order: C2, M&M, Intel, Fires, Sustain,
        # Protection), not insertion order.
        order = schema.get("group_collapse_order") or schema.get("group_order")
        if isinstance(order, (list, tuple)) and order:
            ordered = [k for k in order if k in bucket]
            ordered += [k for k in seen_order if k not in ordered]
            seen_order = ordered
        for key in seen_order:
            sections.append((key, bucket[key]))
    else:
        sections.append(("", rows))

    # Single-section products skip the banner row entirely (it would
    # just say "All" — useless). Multi-section products still get the
    # banner per group so analysts can fold by category.
    suppress_section_banners = len(sections) <= 1

    current_row = next_row
    table_idx = 0
    for section_key, section_rows in sections:
        if suppress_section_banners:
            section_start = current_row
        else:
            # Section banner row: merge across all columns, fill, bold.
            # Display via `group_labels` when declared, else humanize the
            # snake_case key so analysts don't see "command_and_control"
            # in a doctrinal deliverable.
            banner_row = current_row
            ws.merge_cells(start_row=banner_row, start_column=1,
                           end_row=banner_row, end_column=max(col_count, 1))
            banner_label = (section_labels.get(section_key)
                            or section_key.replace("_", " ").title())
            banner = ws.cell(row=banner_row, column=1, value=banner_label)
            banner.fill = PatternFill("solid", fgColor=SECTION_FILL)
            banner.font = Font(name="Arial", size=11, bold=True)
            banner.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[banner_row].height = 20
            section_prov = (
                (structured.get("_provenance") or {}).get(f"section.{section_key}")
                or (structured.get("section_provenance") or {}).get(section_key)
            )
            prov_summary = _provenance_summary(section_prov)
            shorthand = _safe_name_token(f"sec_{section_key}")
            comment_text = f"section_id: {shorthand}"
            if prov_summary:
                comment_text += f"\nprovenance: {prov_summary}"
            banner.comment = Comment(comment_text, "Pytho")
            section_start = banner_row
            current_row += 1

        # Header row: render_label per field.
        header_row = current_row
        for i, (name, spec) in enumerate(fields, start=1):
            c = ws.cell(row=header_row, column=i,
                        value=(spec.get("render_label") or name))
            c.fill = PatternFill("solid", fgColor=TABLE_HEADER_FILL)
            c.font = Font(name="Arial", size=10, bold=True)
            c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            c.border = BORDER_ALL
        current_row += 1

        # Column index lookup so `formula` specs can address sibling
        # fields by name. Maps field_name → 1-based column letter.
        col_letters = {name: get_column_letter(i)
                       for i, (name, _) in enumerate(fields, start=1)}

        # Data rows.
        data_start = current_row
        # Track the row-index-within-structured-rows so the marker `i`
        # index is the stable identity (not the rebucketed sheet position).
        # rows_idx_in_structured maps each row in section_rows back to its
        # index in the original structured_data["rows"] list.
        try:
            structured_rows = structured.get("rows") or []
            row_id_index = {id(r): pos for pos, r in enumerate(structured_rows)}
        except Exception:
            row_id_index = {}
        sheet_slug = schema.get("name") or sheet_name
        for row in section_rows:
            stable_i = row_id_index.get(id(row), -1)
            for i, (name, spec) in enumerate(fields, start=1):
                formula_expr = _build_formula(spec, col_letters, current_row)
                if formula_expr is not None:
                    cell = ws.cell(row=current_row, column=i, value=formula_expr)
                    cell_value_for_marker = formula_expr
                else:
                    value = (row or {}).get(name)
                    value = _display_value(value, spec)
                    cell = ws.cell(row=current_row, column=i, value=value)
                    cell_value_for_marker = value
                cell.font = Font(name="Arial", size=10)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.border = BORDER_ALL
                # Cell-level provenance comment when provenance_required.
                if spec.get("provenance_required"):
                    prov = (row or {}).get("_provenance", {}).get(name)
                    summary = _provenance_summary(prov)
                    if summary:
                        cell.comment = Comment(summary, "Pytho")
                # Canonical-on-disk defined-name marker per (row, field).
                if stable_i >= 0:
                    col_ltr = get_column_letter(i)
                    cell_ref = f"${col_ltr}${current_row}"
                    token = _xlsx_token(sheet_slug, ["rows", stable_i, name])
                    _xlsx_register_defined_name(wb, token, ws.title, cell_ref)
                    _record_marker(wb, token, ws.title, cell_ref, stable_i,
                                    name, spec.get("view") or "published",
                                    cell_value_for_marker)
            current_row += 1

        data_end = current_row - 1 if section_rows else data_start
        # Register an Excel Table over [header_row..data_end] so analysts
        # get sort/filter chips. Tables require at least 1 data row.
        if section_rows:
            table_idx += 1
            ref = (f"{get_column_letter(1)}{header_row}:"
                   f"{get_column_letter(max(col_count, 1))}{data_end}")
            tbl = Table(displayName=f"{_safe_name_token(section_key)}_{table_idx}",
                        ref=ref)
            tbl.tableStyleInfo = TableStyleInfo(
                name="TableStyleLight15", showRowStripes=True,
                showFirstColumn=False, showLastColumn=False,
                showColumnStripes=False,
            )
            try:
                ws.add_table(tbl)
            except ValueError:
                # Duplicate displayName — pathological; let it slide rather
                # than fail the whole render.
                pass

        # Excel row grouping: section is collapsible from banner+1 through
        # last data row. Outline by default at the data rows (header stays
        # visible).
        if data_end >= data_start and section_rows:
            try:
                ws.row_dimensions.group(data_start, data_end, outline_level=1,
                                          hidden=False)
            except Exception:
                pass

        # Named range over the whole section block including banner —
        # only when we actually drew the banner (suppress_section_banners
        # path has no shorthand because there's just one anonymous section).
        if not suppress_section_banners:
            try:
                ref = f"'{ws.title}'!$A${section_start}:${get_column_letter(max(col_count, 1))}${data_end if section_rows else section_start}"
                wb.defined_names[shorthand] = DefinedName(name=shorthand, attr_text=ref)
            except Exception:
                pass

        # Blank spacer row between sections (skip when only one section).
        if not suppress_section_banners:
            current_row += 1

    # Column widths — heuristic per render_label length.
    for i, (name, spec) in enumerate(fields, start=1):
        label = spec.get("render_label") or name
        ws.column_dimensions[get_column_letter(i)].width = max(14, min(40, len(label) + 6))

    return max(col_count, 2)


# ---------------------------------------------------------------------------
# Matrix-shape data sheet
# ---------------------------------------------------------------------------
_MD_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_MD_SEP_LINE = re.compile(r"^\s*\|[\s:|\-]+\|\s*$")


def _parse_markdown_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Best-effort parse of a markdown pipe-table into (header_cols,
    data_rows). The writer LLM emits grid_table paragraph_fields as
    markdown tables for matrix-shape products (AGM, TSS, TSM, etc.)
    when it doesn't have a richer doctrinal dict structure. Returns
    ([], []) if no markdown table is detected so the caller falls back
    to other shapes."""
    if not isinstance(text, str) or "|" not in text:
        return [], []
    lines = [ln for ln in text.splitlines() if _MD_TABLE_LINE.match(ln)]
    # Drop the separator line ("|---|---|...").
    data_lines = [ln for ln in lines if not _MD_SEP_LINE.match(ln)]
    if len(data_lines) < 2:
        return [], []

    def _split(ln: str) -> list[str]:
        # Strip leading/trailing pipe + split, then trim each cell.
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    headers = _split(data_lines[0])
    rows = [_split(ln) for ln in data_lines[1:]]
    # Pad short rows / truncate long rows to header width.
    width = len(headers)
    norm_rows = []
    for r in rows:
        if len(r) < width:
            r = r + [""] * (width - len(r))
        norm_rows.append(r[:width])
    return headers, norm_rows


def _write_matrix_data_sheet(wb: Workbook, schema: dict, structured: dict,
                             field_name: str, field_spec: dict,
                             sheet_name: str) -> int:
    """Render a single grid_table / matrix paragraph_field. Row axis →
    sheet rows; col axis → sheet columns. Cell contents are bullet lists
    of entries.

    matrix_spec preferred:
      {"row_axis": [...], "col_axis": [...]}
    Falls back to introspecting the structured payload if matrix_spec
    isn't declared, or — for matrix-shape products whose writer emitted
    a markdown table string — parses the markdown into a flat table.
    """
    ws = wb.create_sheet(_safe_sheet_name(sheet_name))
    matrix_spec = (schema.get("matrix_spec")
                   or field_spec.get("matrix_spec")
                   or {})
    # Pull the matrix payload from structured_data[field_name]. Two
    # accepted shapes:
    #   1. dict of row_label -> dict of col_label -> str | list[str] (canonical)
    #   2. str carrying a markdown pipe-table (AGM/TSS/TSM writer output)
    raw_payload = structured.get(field_name)
    payload: dict = {}
    md_headers: list[str] = []
    md_rows: list[list[str]] = []
    if isinstance(raw_payload, dict):
        payload = raw_payload
    elif isinstance(raw_payload, str):
        # JSON-fenced shape: writer LLM wraps a canonical dict in
        # ```json ... ``` markdown fences (e.g. ICSM_20260611T090208_978Z's
        # icsm_matrix). Strip the fences, json.loads — if it parses to a
        # dict, treat as the canonical payload. Falls through to markdown-
        # table parsing only when the string isn't JSON.
        import re as _re, json as _json
        s = raw_payload.strip()
        rm = _re.match(r"^```(?:json)?\s*(.*?)```\s*$", s,
                          flags=_re.DOTALL | _re.IGNORECASE)
        if rm:
            s = rm.group(1).strip()
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = _json.loads(s)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                pass
        if not payload:
            md_headers, md_rows = _parse_markdown_table(raw_payload)

    # If we parsed a markdown table, render it as a flat header+row sheet
    # and short-circuit (the matrix_spec axes don't apply to this shape).
    if md_headers and md_rows:
        next_row = _write_banner_rows(ws, schema, structured,
                                        col_span=len(md_headers))
        header_row = next_row
        for ci, h in enumerate(md_headers, start=1):
            c = ws.cell(row=header_row, column=ci, value=h)
            c.fill = PatternFill("solid", fgColor=TABLE_HEADER_FILL)
            c.font = Font(name="Arial", size=10, bold=True)
            c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            c.border = BORDER_ALL
        # Data rows
        for ri, row in enumerate(md_rows, start=header_row + 1):
            for ci, val in enumerate(row, start=1):
                cell = ws.cell(row=ri, column=ci, value=val)
                cell.font = Font(name="Arial", size=10)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.border = BORDER_ALL
        # Wrap as Excel Table for sort/filter
        try:
            ref = (f"{get_column_letter(1)}{header_row}:"
                   f"{get_column_letter(len(md_headers))}{header_row + len(md_rows)}")
            tbl = Table(displayName=_safe_name_token(f"tbl_{field_name}"), ref=ref)
            tbl.tableStyleInfo = TableStyleInfo(name="TableStyleLight15",
                                                  showRowStripes=True)
            ws.add_table(tbl)
        except Exception:
            pass
        for ci, h in enumerate(md_headers, start=1):
            ws.column_dimensions[get_column_letter(ci)].width = max(14, min(48, len(h) + 12))
        return len(md_headers)

    row_axis = list(matrix_spec.get("row_axis") or [])
    col_axis = list(matrix_spec.get("col_axis") or [])

    # Fall back: derive axes from payload keys.
    if not row_axis:
        row_axis = list(payload.keys())

    # Derive col_axis from the writer's actual payload keys when those
    # keys aren't already represented in matrix_spec.col_axis. Writers
    # that key by DST decision points (e.g. ICSM with a bound DST emits
    # 'DP-1 (Commit 1st ID)', 'DP-2 (Commit 1st AD)' columns) need
    # their own keys honored — the schema default (e.g. ICSM_DEFAULT_
    # TIME_PHASES = T-72 / T-48 / ...) only applies when no DST is
    # bound. Verified against ICSM_20260611T104220_577Z which rendered
    # 9 row labels but ZERO cell values because col_axis was the T-72
    # fallback while payload keyed by 'DP-1 (Commit 1st ID)' etc.
    payload_cols: list[str] = []
    for row_key in row_axis:
        sub = payload.get(row_key) or {}
        if isinstance(sub, dict):
            for c in sub.keys():
                if c.startswith("_"):
                    continue
                if c not in payload_cols:
                    payload_cols.append(c)
    if payload_cols:
        # If matrix_spec.col_axis exists AND every payload column matches,
        # the writer used the schema axis — keep it (preserves canonical
        # ordering). Otherwise the writer chose its own columns; use them.
        if not col_axis or not all(c in col_axis for c in payload_cols):
            col_axis = payload_cols
    elif not col_axis:
        col_axis = payload_cols

    if not row_axis or not col_axis:
        # No data — render banner + stub message.
        next_row = _write_banner_rows(ws, schema, structured, col_span=2)
        ws.cell(row=next_row, column=1, value="(no matrix data available)")
        return 2

    next_row = _write_banner_rows(ws, schema, structured,
                                    col_span=len(col_axis) + 1)
    header_row = next_row

    # Header row: blank + col_axis labels.
    ws.cell(row=header_row, column=1, value="").fill = PatternFill("solid", fgColor=TABLE_HEADER_FILL)
    for i, col_label in enumerate(col_axis, start=2):
        c = ws.cell(row=header_row, column=i, value=col_label)
        c.fill = PatternFill("solid", fgColor=TABLE_HEADER_FILL)
        c.font = Font(name="Arial", size=10, bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER_ALL
    ws.row_dimensions[header_row].height = 28

    matrix_sheet_slug = field_spec.get("render_label") or field_name
    for ri, row_label in enumerate(row_axis, start=header_row + 1):
        label_cell = ws.cell(row=ri, column=1, value=row_label)
        label_cell.font = Font(name="Arial", size=10, bold=True)
        label_cell.fill = PatternFill("solid", fgColor=SECTION_FILL)
        label_cell.alignment = Alignment(vertical="top", wrap_text=True)
        label_cell.border = BORDER_ALL
        # Row-label marker.
        row_slug = _xlsx_safe_token(str(row_label))
        row_label_token = _xlsx_token(matrix_sheet_slug,
                                       ["matrix", row_slug, "row_label"])
        _xlsx_register_defined_name(wb, row_label_token, ws.title, f"$A${ri}")
        _record_marker(wb, row_label_token, ws.title, f"$A${ri}", None,
                        f"matrix.{row_slug}.row_label", "published", row_label)
        sub = payload.get(row_label) or {}
        if not isinstance(sub, dict):
            sub = {}
        for ci, col_label in enumerate(col_axis, start=2):
            entries = sub.get(col_label)
            if entries is None:
                value = ""
            elif isinstance(entries, list):
                value = "\n".join(f"• {e}" for e in entries if e)
            elif isinstance(entries, dict):
                value = json.dumps(entries, default=str)
            else:
                value = str(entries)
            cell = ws.cell(row=ri, column=ci, value=value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER_ALL
            # Cell-level provenance (per-cell when supplied).
            prov_path = f"{field_name}.{row_label}.{col_label}"
            prov = (structured.get("_provenance") or {}).get(prov_path)
            if not prov and isinstance(sub.get("_provenance"), dict):
                prov = sub["_provenance"].get(col_label)
            summary = _provenance_summary(prov)
            if summary:
                cell.comment = Comment(summary, "Pytho")
            # Canonical-on-disk matrix-cell marker.
            col_slug = _xlsx_safe_token(str(col_label))
            cell_token = _xlsx_token(matrix_sheet_slug,
                                      ["matrix", row_slug, col_slug])
            cell_ref = f"${get_column_letter(ci)}${ri}"
            _xlsx_register_defined_name(wb, cell_token, ws.title, cell_ref)
            _record_marker(wb, cell_token, ws.title, cell_ref, None,
                            f"matrix.{row_slug}.{col_slug}", "published", value)
        ws.row_dimensions[ri].height = 60

    ws.column_dimensions["A"].width = 22
    for ci in range(2, len(col_axis) + 2):
        ws.column_dimensions[get_column_letter(ci)].width = 28

    # LLM shorthand: named range over the matrix interior (excludes labels)
    # so the agent can refer to "matrix" without spelling every cell.
    try:
        first_data_row = header_row + 1
        last_data_row = header_row + len(row_axis)
        ref = (f"'{ws.title}'!$B${first_data_row}:${get_column_letter(len(col_axis) + 1)}"
               f"${last_data_row}")
        token = _safe_name_token(f"matrix_{field_name}")
        wb.defined_names[token] = DefinedName(name=token, attr_text=ref)
    except Exception:
        pass

    return len(col_axis) + 1


# ---------------------------------------------------------------------------
# list_of_dicts paragraph_field → secondary sheet
# ---------------------------------------------------------------------------
def _normalize_item_subfields(raw: Any) -> dict[str, dict]:
    """Schemas declare `item_subfields` as a LIST of {"name": ..., ...} dicts
    (see templates/products/*.py — every schema uses this shape).
    Normalize to `{name: spec}` so the column loop can address by field name.

    Accepts dict-of-name→spec as well for legacy back-compat, plus an empty
    list/dict (caller falls back to deriving from the first item's keys).
    """
    if isinstance(raw, list):
        out: dict[str, dict] = {}
        for sf in raw:
            if not isinstance(sf, dict):
                continue
            nm = sf.get("name")
            if not nm:
                continue
            out[nm] = sf
        return out
    if isinstance(raw, dict):
        return raw
    return {}


def _flatten_item_for_nested_list_of_dicts(
    item: dict, scalar_cols: list[tuple[str, dict]],
    nested_col_name: str, nested_subfields: dict[str, dict],
) -> list[dict]:
    """Cross-join a parent item with its ONE nested list_of_dicts subfield.

    For an item like
        {"operation": "aviation_rotary",
         "parameters": [{"parameter": "Visibility", ...}, {"parameter": "Ceiling", ...}]}

    given scalar_cols == [("operation", spec)] and nested_col_name == "parameters"
    with nested_subfields == {parameter:..., favorable_threshold:..., ...},
    returns one flat dict per nested row carrying:
        scalar_cols values + nested_subfield values + a sentinel
        `_parent_provenance` carrying the parent item's _provenance keyed
        by scalar_col name so the renderer can reattach scalar-column
        comments to the first row of each operation group.

    Empty nested list emits a single row with scalar columns populated
    and nested columns blank so the operation still appears in the table.
    """
    nested = (item or {}).get(nested_col_name)
    if not isinstance(nested, list):
        nested = []
    parent_scalars = {
        sname: (item or {}).get(sname)
        for sname, _spec in scalar_cols
    }
    parent_prov = (item or {}).get("_provenance") or {}
    if not nested:
        merged = dict(parent_scalars)
        merged["_provenance"] = parent_prov
        return [merged]
    out = []
    for sub in nested:
        if not isinstance(sub, dict):
            continue
        merged = dict(parent_scalars)
        for nname in nested_subfields.keys():
            merged[nname] = sub.get(nname)
        # Preserve provenance keyed by subfield. Subfield-level provenance
        # from the nested item takes precedence; parent provenance is
        # propagated for scalar columns.
        sub_prov = sub.get("_provenance") or {}
        merged_prov = {}
        for sname, _ in scalar_cols:
            if sname in parent_prov:
                merged_prov[sname] = parent_prov[sname]
        for nname in nested_subfields.keys():
            if nname in sub_prov:
                merged_prov[nname] = sub_prov[nname]
        merged["_provenance"] = merged_prov
        out.append(merged)
    return out


def _write_list_of_dicts_sheet(wb: Workbook, structured: dict,
                                field_name: str, field_spec: dict,
                                sheet_name: str) -> Optional[int]:
    """A paragraph_field with shape=list_of_dicts becomes its own sheet
    with one row per item and one column per item_subfield.

    Nested list_of_dicts handling: when EXACTLY ONE item_subfield is itself
    shape=list_of_dicts (e.g. WEM operations → parameters; AGM targets →
    tasks), the row set is flattened — one row per (parent × nested) pair,
    with scalar parent columns repeated on each row. This produces the
    ATP 2-01.3 Fig 4-17 WEM layout naturally:
        Operation | Parameter | Favorable | Unfavorable | Current | Assessment
    instead of dumping the nested list as a JSON blob in one cell.

    When TWO+ item_subfields are list_of_dicts (rare; would require an
    N×M×... cross-join) we degrade to the legacy behavior and emit those
    nested lists as JSON in their cells — keeping the doc renderable
    rather than choosing an arbitrary flattening order.
    """
    items = structured.get(field_name)
    if not isinstance(items, list) or not items:
        return None

    subfields = _normalize_item_subfields(field_spec.get("item_subfields"))
    if not subfields:
        # Derive subfields from first item's keys.
        subfields = {k: {"render_label": k.replace("_", " ").title()}
                     for k in (items[0] or {}).keys() if not k.startswith("_")}

    # Filter out internal-view subfields up front.
    visible = {n: s for n, s in subfields.items()
               if (s or {}).get("view") != "internal"}
    if not visible:
        return None

    # Detect nested list_of_dicts subfield(s).
    nested_names = [n for n, s in visible.items()
                    if (s or {}).get("shape") == "list_of_dicts"]
    use_flatten = (len(nested_names) == 1)
    # Pre-declare so type-checker sees these bound when the second
    # `if use_flatten` block (below) references them. Body of the first
    # block populates them on the flatten path.
    nested_visible: dict[str, dict] = {}

    if use_flatten:
        nested_name = nested_names[0]
        nested_spec = visible[nested_name]
        nested_subfields = _normalize_item_subfields(
            nested_spec.get("item_subfields"))
        if not nested_subfields:
            # Nested subfields not declared — derive from first nested item.
            for it in items:
                if not isinstance(it, dict):
                    continue
                nested_list = it.get(nested_name) or []
                if nested_list and isinstance(nested_list[0], dict):
                    nested_subfields = {
                        k: {"render_label": k.replace("_", " ").title()}
                        for k in nested_list[0].keys() if not k.startswith("_")
                    }
                    break
        nested_visible = {n: s for n, s in nested_subfields.items()
                          if (s or {}).get("view") != "internal"}
        if not nested_visible:
            # Fall through to non-flatten path — nested col will JSON-dump.
            use_flatten = False

    ws = wb.create_sheet(_safe_sheet_name(sheet_name))

    if use_flatten:
        # Scalar (non-nested) parent columns retain their schema order.
        scalar_cols = [(n, s) for n, s in visible.items()
                       if n != nested_names[0]]
        # Final column order = scalar parent cols + nested subfield cols.
        cols = scalar_cols + list(nested_visible.items())
        flat_rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            flat_rows.extend(_flatten_item_for_nested_list_of_dicts(
                item, scalar_cols, nested_names[0], nested_visible))
        render_items = flat_rows
    else:
        cols = list(visible.items())
        render_items = [it for it in items if isinstance(it, dict)]

    if not cols:
        return None

    for i, (name, spec) in enumerate(cols, start=1):
        c = ws.cell(row=1, column=i,
                    value=(spec.get("render_label") or name))
        c.fill = PatternFill("solid", fgColor=TABLE_HEADER_FILL)
        c.font = Font(name="Arial", size=10, bold=True)
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        c.border = BORDER_ALL

    list_sheet_slug = sheet_name
    for ri, item in enumerate(render_items, start=2):
        item_idx = ri - 2
        for i, (name, spec) in enumerate(cols, start=1):
            value = (item or {}).get(name)
            value = _display_value(value, spec)
            cell = ws.cell(row=ri, column=i, value=value)
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER_ALL
            if spec.get("provenance_required"):
                prov = (item or {}).get("_provenance", {}).get(name)
                summary = _provenance_summary(prov)
                if summary:
                    cell.comment = Comment(summary, "Pytho")
            # Canonical-on-disk defined-name marker per (item, field).
            token = _xlsx_token(list_sheet_slug, ["items", item_idx, name])
            cell_ref = f"${get_column_letter(i)}${ri}"
            _xlsx_register_defined_name(wb, token, ws.title, cell_ref)
            _record_marker(wb, token, ws.title, cell_ref, item_idx, name,
                            spec.get("view") or "published", value)

    # Wrap as a Table for sort/filter.
    try:
        ref = (f"{get_column_letter(1)}1:"
               f"{get_column_letter(len(cols))}{len(render_items) + 1}")
        tbl = Table(displayName=_safe_name_token(f"tbl_{field_name}"), ref=ref)
        tbl.tableStyleInfo = TableStyleInfo(
            name="TableStyleLight15", showRowStripes=True,
        )
        ws.add_table(tbl)
    except Exception:
        pass

    for i, (name, spec) in enumerate(cols, start=1):
        label = spec.get("render_label") or name
        ws.column_dimensions[get_column_letter(i)].width = max(14, min(40, len(label) + 6))

    return len(cols)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def render_artifact_xlsx(artifact_type: str, structured_data: dict,
                         out_path: str | Path, *,
                         artifact_id: Optional[str] = None,
                         user_id: Optional[str] = None) -> str:
    """Render the artifact to XLSX at `out_path`. Returns the absolute path.

    Schema-driven: dispatches by shape. Row-shape products produce a
    single grouped data sheet; matrix-shape products produce a matrix
    sheet per matrix paragraph_field; products with list_of_dicts
    paragraph_fields produce one secondary sheet per such field.
    """
    schema = PRODUCT_SCHEMAS.get(artifact_type) or {}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    # The default sheet will be replaced; remove it so the Header sheet
    # we insert at index 0 lands first.
    default = wb.active
    wb.remove(default)

    # Decide top-level shape.
    shape = (schema.get("shape") or "").lower()
    data_col_count = 2

    if shape == "row":
        sheet_name = schema.get("name") or artifact_type
        data_col_count = _write_row_data_sheet(wb, schema, structured_data, sheet_name)
    elif shape == "matrix":
        # Find the first grid_table paragraph_field and render it as the
        # primary data sheet. Other paragraph_fields with shape=list_of_dicts
        # get their own sheets afterwards.
        primary_rendered = False
        for pf in (schema.get("paragraph_fields") or []):
            pf_shape = (pf or {}).get("shape") or ""
            if pf_shape in ("grid_table", "matrix") and not primary_rendered:
                cc = _write_matrix_data_sheet(wb, schema, structured_data,
                                                pf["name"], pf,
                                                sheet_name=pf.get("render_label")
                                                or pf["name"])
                data_col_count = max(data_col_count, cc)
                primary_rendered = True
            elif pf_shape == "list_of_dicts":
                cc = _write_list_of_dicts_sheet(wb, structured_data,
                                                 pf["name"], pf,
                                                 sheet_name=pf.get("render_label")
                                                 or pf["name"])
                if cc:
                    data_col_count = max(data_col_count, cc)
        if not primary_rendered:
            # Fallback: dump structured_data top-level keys as label/value.
            ws = wb.create_sheet("Data")
            r = 1
            for k, v in structured_data.items():
                if k.startswith("_"):
                    continue
                ws.cell(row=r, column=1, value=k).font = Font(bold=True)
                ws.cell(row=r, column=2, value=json.dumps(v, default=str)[:300]
                          if isinstance(v, (list, dict)) else str(v))
                r += 1
            data_col_count = 2
    else:
        # Unknown / hybrid: emit every list_of_dicts paragraph_field as a
        # sheet, every grid_table as a sheet, and fall back to a Data
        # tab with header for anything else.
        any_rendered = False
        for pf in (schema.get("paragraph_fields") or []):
            pf_shape = (pf or {}).get("shape") or ""
            if pf_shape in ("grid_table", "matrix"):
                cc = _write_matrix_data_sheet(wb, schema, structured_data,
                                                pf["name"], pf,
                                                sheet_name=pf.get("render_label")
                                                or pf["name"])
                data_col_count = max(data_col_count, cc)
                any_rendered = True
            elif pf_shape == "list_of_dicts":
                cc = _write_list_of_dicts_sheet(wb, structured_data,
                                                 pf["name"], pf,
                                                 sheet_name=pf.get("render_label")
                                                 or pf["name"])
                if cc:
                    data_col_count = max(data_col_count, cc)
                    any_rendered = True
        if not any_rendered:
            ws = wb.create_sheet("Data")
            r = 1
            for k, v in structured_data.items():
                if k.startswith("_"):
                    continue
                ws.cell(row=r, column=1, value=k).font = Font(bold=True)
                ws.cell(row=r, column=2,
                          value=(json.dumps(v, default=str)[:300]
                                  if isinstance(v, (list, dict)) else str(v)))
                r += 1
            data_col_count = 2

    # Hidden _Provenance sheet appended — never shows in tab strip
    # for the analyst (sheet_state="hidden") but is queryable by the
    # agent and by anyone unhiding sheets in Calc / Excel.
    _write_provenance_sheet(wb, structured_data)

    # Canonical-on-disk shadow sheets — _Header (header-field markers),
    # _FieldMap (column → schema-field name inverter), _Workpaper (full
    # marker registry). All hidden. Order: _Header is read first by the
    # importer to populate structured_data["header"]; _FieldMap is used
    # to invert column index → field name; _Workpaper is the shadow
    # registry the importer falls back on when defined names have been
    # lost.
    _write_header_sheet(wb, schema, structured_data)
    _write_field_map_sheet(wb, schema)
    _write_workpaper_sheet(wb, schema, structured_data,
                            getattr(wb, "_pytho_marker_registry", []) or [])

    # Land the analyst on the data sheet, not the hidden shadow sheets.
    if wb.sheetnames:
        wb.active = 0

    wb.save(str(out_path))
    log.info("xlsx_exporter: wrote %s (%d sheets)", out_path, len(wb.sheetnames))
    return str(out_path)


# ---------------------------------------------------------------------------
# CLI for manual rendering against a schema_state.json dump
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python xlsx_exporter.py <schema_state.json> <output.xlsx>")
        sys.exit(2)
    schema_state = json.loads(Path(sys.argv[1]).read_text())
    artifact_type = schema_state.get("product_type") or schema_state.get("artifact_type") or ""
    structured = schema_state.get("structured_data") or {}
    out = render_artifact_xlsx(artifact_type, structured, sys.argv[2])
    print(out)
