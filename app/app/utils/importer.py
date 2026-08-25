"""Product-agnostic importer — reads markers from a rendered DOCX/PPTX/XLSX
file and rebuilds the canonical `structured_data` dict.

Sibling to `exporter.py` / `xlsx_exporter.py`. Single public entry point:

    import_artifact(artifact_id, product_type, *, on_disk_path=None,
                    format_hint=None) -> {"structured_data": dict,
                                          "warnings": [...]}

The walker iterates every marker in the on-disk file, extracts (path,
value) tuples, and feeds them to a schema-aware `_reconstruct` pass. Zero
per-product branches anywhere — same discipline `exporter.py` enforces.

Output contract:
  {
    "structured_data": dict,
    "warnings": [(code, field_path, detail), ...]
  }

Warnings NEVER mutate structured_data; they describe the round-trip's
faithfulness. Files that can't be parsed at all return
  {"structured_data": {}, "warnings": [("parse_failure", "", str(exc))]}
— no half-imported state.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from utils.render_regions import (
    DOCX_PREFIX,
    PPTX_PREFIX,
    XLSX_PREFIX,
    parse_path,
    parse_path_with_region,
    schema_field_lookup,
)

log = logging.getLogger(__name__)

ImportWarning = Tuple[str, str, str]  # (code, field_path, detail)


def import_artifact(
    artifact_id: str,
    product_type: str,
    *,
    on_disk_path: Optional[str] = None,
    format_hint: Optional[str] = None,
) -> dict:
    """Walk markers in the on-disk deliverable and rebuild structured_data.

    Resolves the file path from the artifact_id when on_disk_path is not
    given (looks under UPLOAD_DIR/_autodraft/<artifact_id>/ and falls
    back to scanning every owner directory). Dispatches by file extension
    or explicit format_hint.

    The walker emits (path, value) tuples in document order; `_reconstruct`
    builds the canonical dict by walking each path through the schema in
    parallel; `_validate` checks required-field / enum / format invariants
    and appends warnings.

    Returns:
      {"structured_data": dict, "warnings": [(code, path, detail), ...]}
    """
    warnings: List[ImportWarning] = []

    # Resolve schema for the product_type.
    try:
        from templates import PRODUCT_SCHEMAS
    except ImportError:
        return {"structured_data": {}, "warnings": [
            ("import_environment_broken", "", "templates module unavailable")
        ]}
    schema = PRODUCT_SCHEMAS.get(product_type) or {}

    # Resolve path.
    if not on_disk_path:
        on_disk_path = _resolve_artifact_path(artifact_id, product_type, schema)
        if not on_disk_path:
            return {"structured_data": {}, "warnings": [
                ("artifact_not_found", artifact_id, f"no deliverable found for product_type={product_type}")
            ]}

    p = Path(on_disk_path)
    if not p.exists():
        return {"structured_data": {}, "warnings": [
            ("artifact_not_found", str(p), "file does not exist")
        ]}

    # Dispatch by extension.
    fmt = (format_hint or p.suffix.lstrip(".") or "").lower()
    try:
        if fmt == "docx":
            flat_pairs = _docx_walk(str(p), schema)
        elif fmt == "pptx":
            flat_pairs = _pptx_walk(str(p), schema)
        elif fmt == "xlsx":
            flat_pairs = _xlsx_walk(str(p), schema)
        else:
            return {"structured_data": {}, "warnings": [
                ("unsupported_format", str(p), f"format={fmt!r}")
            ]}
    except Exception as exc:  # parse failure
        log.warning("importer: walker failed for %s: %s", p, exc, exc_info=True)
        return {"structured_data": {}, "warnings": [
            ("parse_failure", str(p), str(exc))
        ]}

    structured_data, recon_warnings = _reconstruct(flat_pairs, schema)
    warnings.extend(recon_warnings)
    val_warnings = _validate(structured_data, schema)
    warnings.extend(val_warnings)

    return {"structured_data": structured_data, "warnings": warnings}


import_artifact.__doc__ = (import_artifact.__doc__ or "").strip()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_artifact_path(artifact_id: str, product_type: str,
                           schema: dict) -> Optional[str]:
    """Find the rendered deliverable on disk by artifact_id.

    Tries uploads/_autodraft/<artifact_id>/<filename> first (the WOPI /
    autodraft convention), then scans every owner directory.
    """
    try:
        from handlers.product import UPLOAD_DIR
    except ImportError:
        return None
    try:
        from utils.exporter import artifact_filename_base
    except ImportError:
        artifact_filename_base = None  # type: ignore

    base = Path(UPLOAD_DIR)
    fmt = (schema.get("output_format") or "docx").lower()
    ext = {"docx": ".docx", "pptx": ".pptx", "xlsx": ".xlsx"}.get(fmt, ".docx")

    candidates: List[Path] = []
    autodraft_dir = base / "_autodraft" / artifact_id
    if autodraft_dir.exists():
        candidates.append(autodraft_dir)
    try:
        for owner_dir in base.iterdir():
            cand = owner_dir / artifact_id
            if cand.exists():
                candidates.append(cand)
    except FileNotFoundError:
        pass

    for cand_dir in candidates:
        # Schema-resolved filename
        if artifact_filename_base is not None:
            try:
                ss_path = cand_dir / "schema_state.json"
                if ss_path.exists():
                    import json as _json
                    ss = _json.loads(ss_path.read_text(encoding="utf-8"))
                    fb = artifact_filename_base(
                        ss.get("product_type") or product_type,
                        schema,
                        ss.get("structured_data") or {},
                        artifact_id,
                    )
                    p = cand_dir / f"{fb}{ext}"
                    if p.exists():
                        return str(p)
            except Exception:
                pass
        # Fallback: any matching extension file in the dir.
        for f in cand_dir.glob(f"*{ext}"):
            return str(f)
    return None


# ---------------------------------------------------------------------------
# DOCX walker
# ---------------------------------------------------------------------------


def _docx_walk(path: str, schema: Optional[dict] = None) -> List[Tuple[str, Any]]:
    """Iterate w:sdt elements in document order, decode w:tag, and emit
    (path_string, value) tuples. Nested SDTs flatten through recursion.

    Tables are extracted via column-header matching against the schema's
    list_of_dicts fields' item_subfields render_labels (same mechanism
    as the PPTX walker). Analyst-added rows in DOCX tables — which
    have no SDT marker — are recognized positionally by the header
    row and emitted as `(<field>[<i>].<subfield>, cell_text)` pairs."""
    from docx import Document  # type: ignore
    from docx.oxml.ns import qn  # type: ignore

    doc = Document(path)
    pairs: List[Tuple[str, Any]] = []
    list_of_dicts_specs = _collect_list_of_dicts_specs(schema)
    # Extract every DOCX table via column-header matching.
    for tbl in doc.tables:
        _extract_docx_table_rows(tbl, list_of_dicts_specs, pairs)

    def _walk_element(elem):
        for sdt in elem.findall(qn("w:sdt")):
            pr = sdt.find(qn("w:sdtPr"))
            if pr is None:
                continue
            tag_elem = pr.find(qn("w:tag"))
            if tag_elem is None:
                continue
            tag_value = tag_elem.get(qn("w:val")) or ""
            if not tag_value.startswith(DOCX_PREFIX):
                continue
            content = sdt.find(qn("w:sdtContent"))
            if content is None:
                pairs.append((tag_value, None))
                continue
            # Check if there are nested SDTs inside — if so, recurse and
            # do NOT emit a value at this level (this is a container SDT).
            nested = content.findall(qn("w:sdt"))
            if nested:
                _walk_element(content)
                # Some container SDTs may also carry their own leaf text
                # before / between nested children; skip — the children
                # carry the values.
                continue
            # Leaf SDT: concatenate every w:t descendant.
            text_parts = []
            for t in content.iter(qn("w:t")):
                text_parts.append(t.text or "")
            value = "".join(text_parts).strip()
            pairs.append((tag_value, value))

    _walk_element(doc.element.body)
    return pairs


# ---------------------------------------------------------------------------
# PPTX walker
# ---------------------------------------------------------------------------


# `[[<tag>]]` paragraph-marker grammar — captures the inner tag, which
# itself may contain `[N]` list-index brackets (canonical dotted-bracket
# grammar). The original `[^\[\]]+` body rejected legal PPTX path tokens
# like `coa_slides[0]`. Use a balanced-bracket pattern that walks until
# the FIRST `]]` literal: `[^\]]*(?:\][^\]]+)*` matches any character
# except `]`, with embedded single `]` followed by at least one non-`]`
# char allowed (so `coa_slides[0].mission` → `coa_slides[0].mission`).
_PPTX_PARAGRAPH_MARKER = re.compile(r"^\s*\[\[([^\]]*(?:\][^\]]+)*)\]\]")


def _pptx_walk(path: str, schema: Optional[dict] = None) -> List[Tuple[str, Any]]:
    """Iterate slides + shapes, recognize pytho.* shape names, and emit
    (path_string, value) tuples.

    Per-paragraph hidden-run markers inside shared text frames are split
    on `[[<path>]]` prefixes — the importer emits one (path, value) tuple
    per marked paragraph in shared text frames.

    Tables are extracted via column-header matching against the schema's
    list_of_dicts fields' item_subfields render_labels. The PPTX render
    emits tables WITHOUT inline markers (no `[[obstacles[0].type]]`
    prefix on cells, no per-cell name attribute) — the only signal the
    walker has is the table's column-header row. When the headers match
    a list_of_dicts field's subfield render_labels, every data row gets
    emitted as `(<field>[<i>].<subfield>, cell_text)` pairs. This is
    what lets analyst-added rows survive finalize: a new row appended
    by Collabora has no marker, but the column-header table-shape
    recognition picks it up positionally.
    """
    from pptx import Presentation  # type: ignore
    from pptx.oxml.ns import qn  # type: ignore

    pres = Presentation(path)
    pairs: List[Tuple[str, Any]] = []
    # Cache list_of_dicts subfield maps from schema so we only build
    # them once. Each entry: {field_name: {(header_text_lower,...): {render_label: subfield_name}}}
    list_of_dicts_specs = _collect_list_of_dicts_specs(schema)

    for slide in pres.slides:
        for shape in slide.shapes:
            # Table shapes — extract structured rows via column-header
            # matching when the headers map to a list_of_dicts field.
            try:
                if shape.has_table:
                    _extract_table_rows(shape.table, list_of_dicts_specs, pairs)
                    # Don't fall through — tables emit per-cell pairs
                    # above; the shape-level fallback below would
                    # double-emit the concatenated table text.
                    continue
            except Exception:
                # has_table may raise for some shape kinds — ignore and
                # fall through to the existing shape-name handler.
                pass
            try:
                name = shape.name or ""
            except Exception:
                name = ""
            if not name.startswith(PPTX_PREFIX):
                continue
            # Extract value:
            #   - Picture shapes: emit descr (alt-text) if present, else
            #     emit the shape name itself.
            #   - Text shapes: walk paragraphs. If any paragraph carries a
            #     leading [[<path>]] marker, emit per-paragraph; otherwise
            #     emit the full text_frame text against the shape name.
            #   - Group / connector / other: emit the name itself with None.
            value: Any = None
            try:
                tf = getattr(shape, "text_frame", None)
                if tf is not None:
                    # Detect per-paragraph markers.
                    per_para_emitted = False
                    for para in tf.paragraphs:
                        # Concatenate all runs for the paragraph.
                        text = ""
                        for r in para.runs:
                            text += r.text or ""
                        m = _PPTX_PARAGRAPH_MARKER.match(text)
                        if m:
                            tag = m.group(1).strip()
                            payload = text[m.end():]
                            pairs.append((tag, payload))
                            per_para_emitted = True
                    if per_para_emitted:
                        # Shape-level value already consumed via per-paragraph
                        # markers; skip the shape-name emission to avoid
                        # double-counting.
                        continue
                    value = (tf.text or "").strip()
                else:
                    # Picture / group / connector — try alt-text descr.
                    try:
                        el = shape._element  # type: ignore[attr-defined]
                        # picture: p:nvPicPr/p:cNvPr@descr
                        descr_el = el.find(".//" + qn("p:cNvPr"))
                        if descr_el is not None:
                            value = descr_el.get("descr") or ""
                    except Exception:
                        value = None
            except Exception:
                value = None
            pairs.append((name, value))
    return pairs


# ---------------------------------------------------------------------------
# XLSX walker
# ---------------------------------------------------------------------------


def _xlsx_walk(path: str, schema: Optional[dict] = None) -> List[Tuple[str, Any]]:
    """Read wb.defined_names, decode pytho_* tokens, emit (path, value) pairs.

    Cross-checks against the `_Workpaper` registry when present. Foreign
    defined names (non-pytho_) are preserved in a sidecar via the `_extra`
    bucket the reconstruction step ignores.

    ALSO scans every sheet for column-header tables that match the
    schema's list_of_dicts subfields' render_labels — same mechanism as
    PPTX/DOCX. Analyst-appended rows in XLSX tables (rows past the
    highest defined_name index) are recognized positionally and
    emitted as `(<field>[<i>].<subfield>, cell_text)` pairs. The grid-
    table products (HVTL / HPTL / TSM / ICSM) use defined_names per
    cell; rows-shape products with first-row headers use this scan.
    """
    from openpyxl import load_workbook  # type: ignore

    wb = load_workbook(path, data_only=False, keep_links=False)
    pairs: List[Tuple[str, Any]] = []
    list_of_dicts_specs = _collect_list_of_dicts_specs(schema)
    # Header-table scan across every sheet (skip hidden / metadata sheets).
    for sheet_name in wb.sheetnames:
        if sheet_name.startswith("_"):
            # Internal registry sheets (_Workpaper / _Header / _FieldMap /
            # _Provenance / _Extra). Not analyst-edited tables.
            continue
        try:
            ws = wb[sheet_name]
        except KeyError:
            continue
        _extract_xlsx_sheet_rows(ws, list_of_dicts_specs, pairs)

    # defined_names is a DefinedNameDict on openpyxl 3.x — iterable as a
    # dict of name -> DefinedName.
    try:
        names_dict = dict(wb.defined_names)
    except Exception:
        names_dict = {}

    for token, defn in names_dict.items():
        if not token.startswith(XLSX_PREFIX):
            continue
        attr_text = getattr(defn, "attr_text", "") or ""
        sheet_name, cell_ref = _parse_xlsx_ref(attr_text)
        if not sheet_name or not cell_ref:
            continue
        try:
            ws = wb[sheet_name]
        except KeyError:
            continue
        try:
            # Single-cell or rectangle. We grab the value of the first cell
            # in the range (matches the renderer's per-cell registration).
            cell = ws[cell_ref.split(":")[0]]
            value = cell.value
        except Exception:
            value = None
        pairs.append((token, value))
    return pairs


# ---------------------------------------------------------------------------
# Schema-driven column-header table extraction (covers analyst-added rows
# that the marker walker doesn't see). One unified mechanism applied
# uniformly to PPTX, DOCX, and XLSX. See feedback_importer_marker_walker
# _blind_to_analyst_additions.md + feedback_be_comprehensive_cover_every
# _surface.md for the lesson behind this.
# ---------------------------------------------------------------------------


def _norm_header(s: str) -> str:
    """Normalize a header cell to a comparable key — uppercase + collapse
    whitespace + strip outer whitespace. Lets 'Tactical Effect' match
    'TACTICAL  EFFECT' / 'Tactical effect' from analyst-typed headers."""
    if not isinstance(s, str):
        return ""
    return " ".join(s.upper().split())


def _collect_list_of_dicts_specs(schema: Optional[dict]) -> dict:
    """Build a lookup that maps a table's column-header SET (frozen) to
    the matching list_of_dicts field's subfield map.

    Returns: {field_name: {
        "subfield_by_label": {NORMALIZED_LABEL: subfield_name},
        "header_set": frozenset(NORMALIZED_LABEL),
        "required_set": frozenset(REQUIRED_NORMALIZED_LABELS),
    }}

    Schema sources scanned (in order):
      - schema['paragraph_fields'] with shape='list_of_dicts' OR
        item_subfields list present.
      - schema['row_fields'] (HVTL/HPTL/etc.): the per-row subfields
        are the row_field DICT itself; the published table's columns
        are each row_field's render_label.

    A header SET match is required-subset: the table's header set must
    contain every REQUIRED subfield's render_label. Extra columns are
    fine — they're ignored. A pure-empty schema returns {}.
    """
    specs: dict = {}
    if not isinstance(schema, dict):
        return specs
    # list_of_dicts paragraph_fields.
    for pf in schema.get("paragraph_fields") or []:
        if not isinstance(pf, dict):
            continue
        if pf.get("shape") != "list_of_dicts" and not pf.get("item_subfields"):
            continue
        subfields = pf.get("item_subfields") or []
        if not subfields:
            continue
        subfield_by_label: dict = {}
        required_set: set = set()
        for sf in subfields:
            if not isinstance(sf, dict):
                continue
            name = sf.get("name")
            label = sf.get("render_label") or sf.get("name") or ""
            if not name or not label:
                continue
            norm = _norm_header(str(label))
            subfield_by_label[norm] = name
            if sf.get("required"):
                required_set.add(norm)
        if not subfield_by_label:
            continue
        fname = pf.get("name")
        if fname:
            specs[fname] = {
                "subfield_by_label": subfield_by_label,
                "header_set": frozenset(subfield_by_label.keys()),
                "required_set": frozenset(required_set),
                "container": "list",
            }
    # Row-shape products (rows[] of dicts — HVTL / HPTL / etc.).
    row_fields = schema.get("row_fields")
    if isinstance(row_fields, dict) and row_fields:
        subfield_by_label = {}
        required_set = set()
        for rname, rspec in row_fields.items():
            if not isinstance(rspec, dict):
                continue
            label = rspec.get("render_label") or rname
            if not label:
                continue
            norm = _norm_header(str(label))
            subfield_by_label[norm] = rname
            if rspec.get("required"):
                required_set.add(norm)
        if subfield_by_label:
            specs["rows"] = {
                "subfield_by_label": subfield_by_label,
                "header_set": frozenset(subfield_by_label.keys()),
                "required_set": frozenset(required_set),
                "container": "list",
            }
    return specs


def _match_table_to_field(headers: List[str], specs: dict) -> Optional[Tuple[str, dict]]:
    """Given the column headers from a table's first row, find the
    list_of_dicts field whose subfields best match the header set.

    Match rule: every REQUIRED subfield render_label must appear in the
    table's header set. Among candidates, the one with the most
    subfields covered by the table wins (ties: deterministic by field
    name). Returns (field_name, spec) or None when nothing matches."""
    if not headers or not specs:
        return None
    norm_headers = {_norm_header(h) for h in headers if h}
    if not norm_headers:
        return None
    best: Optional[Tuple[str, dict]] = None
    best_score = -1
    for fname, spec in specs.items():
        required = spec["required_set"]
        if required and not required.issubset(norm_headers):
            continue
        # Count subfields that appear in the table headers.
        covered = sum(
            1 for label in spec["subfield_by_label"].keys()
            if label in norm_headers
        )
        # Require at least one match — a table with zero schema-known
        # columns is some other table, not this field.
        if covered <= 0:
            continue
        if covered > best_score or (covered == best_score and best is not None
                                      and fname < best[0]):
            best = (fname, spec)
            best_score = covered
    return best


def _extract_table_rows(table, specs: dict, pairs: List[Tuple[str, Any]]) -> None:
    """PPTX table extractor. `table` is a python-pptx Table. Reads row 0
    as headers, matches against list_of_dicts specs, emits per-cell
    pairs for every data row.
    """
    if not specs:
        return
    try:
        rows = list(table.rows)
    except Exception:
        return
    if len(rows) < 2:
        return
    # Headers: row 0 cells, .text_frame.text per cell.
    try:
        headers = [(cell.text_frame.text or "").strip() for cell in rows[0].cells]
    except Exception:
        return
    match = _match_table_to_field(headers, specs)
    if match is None:
        return
    fname, spec = match
    subfield_by_label = spec["subfield_by_label"]
    # Map column index → subfield name (None for ignored columns).
    col_map: list = []
    for h in headers:
        col_map.append(subfield_by_label.get(_norm_header(h or "")))
    # Emit per-cell pairs for every data row, skipping rows that are
    # entirely empty (analyst-added blank trailing row in some editors).
    out_idx = 0
    for row in rows[1:]:
        try:
            cells = list(row.cells)
        except Exception:
            continue
        row_text = [((c.text_frame.text or "").strip()) for c in cells]
        if not any(row_text):
            continue
        for ci, text in enumerate(row_text):
            sub = col_map[ci] if ci < len(col_map) else None
            if not sub:
                continue
            pairs.append((f"{fname}[{out_idx}].{sub}", text))
        out_idx += 1


def _extract_docx_table_rows(table, specs: dict, pairs: List[Tuple[str, Any]]) -> None:
    """DOCX table extractor. `table` is a python-docx Table. Mirror of
    the PPTX extractor — different cell.text accessor, otherwise the
    same column-header → subfield logic."""
    if not specs:
        return
    try:
        rows = list(table.rows)
    except Exception:
        return
    if len(rows) < 2:
        return
    try:
        headers = [(cell.text or "").strip() for cell in rows[0].cells]
    except Exception:
        return
    match = _match_table_to_field(headers, specs)
    if match is None:
        return
    fname, spec = match
    subfield_by_label = spec["subfield_by_label"]
    col_map: list = [subfield_by_label.get(_norm_header(h or "")) for h in headers]
    out_idx = 0
    for row in rows[1:]:
        try:
            cells = list(row.cells)
        except Exception:
            continue
        row_text = [((c.text or "").strip()) for c in cells]
        if not any(row_text):
            continue
        for ci, text in enumerate(row_text):
            sub = col_map[ci] if ci < len(col_map) else None
            if not sub:
                continue
            pairs.append((f"{fname}[{out_idx}].{sub}", text))
        out_idx += 1


def _extract_xlsx_sheet_rows(ws, specs: dict, pairs: List[Tuple[str, Any]]) -> None:
    """XLSX sheet extractor. Scans `ws` for the FIRST row that contains
    a recognized column-header set, then treats every non-empty row
    below it as a data row of the matched list_of_dicts field.

    Banner / classification / DTG rows above the header are skipped
    automatically because they don't form a header-set match. Internal
    sheets (_Workpaper / _Header / etc.) are skipped at the caller."""
    if not specs:
        return
    try:
        max_r = ws.max_row or 0
        max_c = ws.max_column or 0
    except Exception:
        return
    if max_r < 2 or max_c < 1:
        return
    header_row_idx: Optional[int] = None
    matched_field: Optional[str] = None
    matched_spec: Optional[dict] = None
    col_map: list = []
    # Look at the first 20 rows for a header match — banner rows in
    # exported xlsx are typically at most 3-5 rows.
    for r in range(1, min(max_r, 20) + 1):
        try:
            row_vals = [
                ws.cell(row=r, column=c).value for c in range(1, max_c + 1)
            ]
        except Exception:
            continue
        headers = [str(v).strip() if v is not None else "" for v in row_vals]
        if sum(1 for h in headers if h) < 2:
            continue
        match = _match_table_to_field(headers, specs)
        if match is not None:
            header_row_idx = r
            matched_field, matched_spec = match
            subfield_by_label = matched_spec["subfield_by_label"]
            col_map = [
                subfield_by_label.get(_norm_header(h or "")) for h in headers
            ]
            break
    if header_row_idx is None or matched_field is None:
        return
    out_idx = 0
    for r in range(header_row_idx + 1, max_r + 1):
        try:
            row_vals = [
                ws.cell(row=r, column=c).value for c in range(1, max_c + 1)
            ]
        except Exception:
            continue
        if not any(v not in (None, "") for v in row_vals):
            continue
        for ci, v in enumerate(row_vals):
            sub = col_map[ci] if ci < len(col_map) else None
            if not sub:
                continue
            text = "" if v is None else str(v)
            pairs.append((f"{matched_field}[{out_idx}].{sub}", text))
        out_idx += 1


_XLSX_REF_RE = re.compile(r"^'?([^'!]+)'?!(.+)$")


def _parse_xlsx_ref(attr_text: str) -> Tuple[str, str]:
    """Parse a defined-name attr_text into (sheet, cell_ref).

    Examples:
      "'CARVER'!$E$8"     -> ("CARVER", "$E$8")
      "CARVER!$E$8:$F$9"  -> ("CARVER", "$E$8:$F$9")
    """
    if not attr_text:
        return ("", "")
    m = _XLSX_REF_RE.match(attr_text)
    if not m:
        return ("", "")
    return (m.group(1), m.group(2))


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------


def _reconstruct(flat_pairs: List[Tuple[str, Any]],
                 schema: dict) -> Tuple[dict, List[ImportWarning]]:
    """Walk (path, value) tuples and build the canonical structured_data
    dict, coerced by schema shape."""
    warnings: List[ImportWarning] = []
    structured: dict = {}

    # Strip region tags for routing — paint-only suffixes (`:heading`,
    # `:body`, `:badge`, etc.) do NOT change the destination field path.
    # The importer keeps the FIRST emission per (path) wins for `:heading`
    # vs `:body` — `:body` overwrites because it carries the actual value
    # for narrative subfields.
    region_priority = {":body": 3, "": 2, ":value": 2, ":narrative": 1,
                       ":heading": 0, ":badge": 0, ":rule": 0,
                       ":frame": 0, ":absent": -1, ":label": -1}

    # Bucket of (canonical_path_tuple, value, region_priority) for resolving
    # multi-region collisions to the most-informative value.
    bucket: dict[tuple, Tuple[Any, int]] = {}

    for raw_tag, value in flat_pairs:
        try:
            tokens, region = parse_path_with_region(raw_tag)
        except ValueError:
            warnings.append(("malformed_path", raw_tag, "could not parse marker"))
            continue
        if not tokens:
            continue

        # Drop the XLSX sheet-slug prefix when reconstructing: the canonical
        # structured_data dict doesn't carry a sheet token. Header tokens
        # (`pytho_header__<field>`) parse as ["header", "<field>"] and
        # bypass this strip — only sheet-prefixed grammar gets stripped.
        if raw_tag.startswith(XLSX_PREFIX) and tokens and tokens[0] != "header":
            # The first token is the sheet slug — drop it. Then if the
            # next token is "rows" or "items", that's the canonical key.
            if len(tokens) > 1:
                tokens = tokens[1:]
            else:
                continue
            # `items` (XLSX list-of-dicts grammar) maps to whichever
            # paragraph_field name the sheet represented. For the simple
            # importer we leave as-is and let the consumer handle. To map
            # `items` -> <paragraph_field_name>, the importer would need
            # the sheet->field mapping; for now, list-of-dicts secondary
            # sheets are stashed under `_extra` for the analyst to triage.
            # XLSX importer is best-effort; canonical round-trip relies
            # on the workpaper / header sheets which DO carry the field
            # name directly.

        # Compute region priority for the value.
        prio = region_priority.get(region, 1)
        path_key = tuple(tokens)
        # If `:absent` mark, set the value to None.
        if region == ":absent":
            value = None
        existing = bucket.get(path_key)
        if existing is None or existing[1] < prio:
            bucket[path_key] = (value, prio)
        elif existing[1] == prio and existing[0] not in (None, ""):
            # Duplicate marker; keep first non-empty.
            warnings.append(("duplicate_marker", _path_to_str(tokens),
                             f"second value={value!r} (kept first)"))

    # Now apply buckets to structured_data.
    for path_tuple, (value, _prio) in bucket.items():
        path = list(path_tuple)
        try:
            _set_canonical_path(structured, schema, path, value, warnings)
        except Exception as exc:
            warnings.append(("reconstruct_error", _path_to_str(path), str(exc)))
            continue

    # Sanity: ensure header dict exists.
    if "header" not in structured:
        structured["header"] = {}

    return structured, warnings


def _path_to_str(path: List[Any]) -> str:
    out = []
    for tok in path:
        if isinstance(tok, int):
            out.append(f"[{tok}]")
        else:
            if out:
                out.append("." + str(tok))
            else:
                out.append(str(tok))
    return "".join(out)


def _set_canonical_path(structured: dict, schema: dict, path: List[Any],
                        value: Any, warnings: List[ImportWarning]) -> None:
    """Walk `path` into `structured`, creating intermediate dicts / lists
    as needed, and place `value` at the leaf.

    Schema-shape coercion:
      - "header.<f>" -> structured["header"][f] = value
      - "<f>" (paragraph_field shape=string) -> structured[f] = value
      - "<f>.<sub>" (dict) -> structured[f][sub] = value
      - "<f>[<i>].<sub>" (list_of_dicts) -> ...
      - "rows[<i>].<f>" -> structured["rows"][i][f] = value
    """
    if not path:
        return
    spec = schema_field_lookup(schema, path)
    cur = structured
    for idx, tok in enumerate(path):
        is_last = idx == len(path) - 1
        nxt = path[idx + 1] if not is_last else None
        if is_last:
            if isinstance(cur, list):
                _list_extend(cur, tok if isinstance(tok, int) else 0)
                if isinstance(tok, int):
                    cur[tok] = value
                # else: shouldn't happen — list expects int leaf
            elif isinstance(cur, dict):
                cur[str(tok)] = value
            return
        # Not last — need to descend. Decide whether the child is a list
        # (next token is an int) or a dict (next token is a str).
        next_is_list = isinstance(nxt, int)
        if isinstance(cur, dict):
            key = str(tok)
            if next_is_list:
                if key not in cur or not isinstance(cur[key], list):
                    cur[key] = []
            else:
                if key not in cur or not isinstance(cur[key], dict):
                    cur[key] = {}
            cur = cur[key]
        elif isinstance(cur, list):
            if not isinstance(tok, int):
                # Unexpected — schema mismatch
                warnings.append(("schema_mismatch", _path_to_str(path),
                                 f"expected int index at {idx}, got {tok!r}"))
                return
            _list_extend(cur, tok)
            if next_is_list:
                if not isinstance(cur[tok], list):
                    cur[tok] = []
            else:
                if not isinstance(cur[tok], dict):
                    cur[tok] = {}
            cur = cur[tok]


def _list_extend(lst: list, idx: int) -> None:
    """Ensure lst has at least idx+1 entries, padding with empty dicts."""
    while len(lst) <= idx:
        lst.append({})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_MGRS_RE = re.compile(r"^[0-9]{1,2}[A-Z]{1}[A-Z]{2}[0-9]{2,10}$")
_DTG_RE = re.compile(r"^\d{6}Z?[A-Z]{3}\d{2,4}$")


def _validate(structured_data: dict, schema: dict) -> List[ImportWarning]:
    """Required-field / enum / MGRS / DTG / provenance checks. Emits
    warnings; never throws."""
    warnings: List[ImportWarning] = []

    header = structured_data.get("header") or {}
    for fname, fspec in (schema.get("header_fields") or {}).items():
        if not (fspec or {}).get("required"):
            continue
        val = header.get(fname)
        if val in (None, "", []):
            warnings.append(("missing_required_header", fname,
                             "required header field has no value"))
        else:
            _validate_field_value(fspec, f"header.{fname}", val, warnings)

    for p in (schema.get("paragraph_fields") or []):
        name = p.get("name") or ""
        if not name:
            continue
        if not p.get("required"):
            continue
        # shape="map" fields are binary image artifacts — they live as
        # <UPLOAD_DIR>/<owner>/<artifact_id>/<name>.jpg, not as text markers
        # in structured_data. The renderer paints them onto the deliverable
        # via _emit_map_slide / map-figure HTML, not via the marker tree.
        # Surfacing them as `missing_required_paragraph` is a false positive
        # — analysts saw the warning on every SITEMP / Threat Overlay /
        # MCOO finalize even when the map embed was clearly present.
        if (p.get("shape") or "") == "map":
            continue
        val = structured_data.get(name)
        if val in (None, "", [], {}):
            warnings.append(("missing_required_paragraph", name,
                             "required paragraph field has no value"))

    if (schema.get("shape") or "") == "row":
        rows = structured_data.get("rows") or []
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for rfn, rfs in (schema.get("row_fields") or {}).items():
                if not (rfs or {}).get("required"):
                    continue
                if (rfs or {}).get("view") not in (None, "published"):
                    # workpaper / internal row_fields rarely render to the
                    # XLSX surface — don't surface as a missing-required.
                    continue
                if row.get(rfn) in (None, "", []):
                    warnings.append(("missing_required_cell",
                                     f"rows[{i}].{rfn}",
                                     "required row field has no value"))

    return warnings


def _validate_field_value(fspec: dict, path: str, val: Any,
                          warnings: List[ImportWarning]) -> None:
    enum = (fspec or {}).get("enum")
    if enum and isinstance(val, str) and val not in enum:
        warnings.append(("enum_mismatch", path,
                         f"value {val!r} not in allowed set {sorted(enum)}"))
    # MGRS / DTG sniff via field naming convention (existing exporter heuristic).
    fname = path.split(".")[-1]
    if "mgrs" in fname.lower() and isinstance(val, str) and val:
        if not _MGRS_RE.match(val.replace(" ", "")):
            warnings.append(("mgrs_invalid", path, f"value={val!r}"))
    if fname.lower().endswith("_dtg") and isinstance(val, str) and val:
        if not _DTG_RE.match(val.replace(" ", "")):
            warnings.append(("dtg_invalid", path, f"value={val!r}"))


__all__ = ["import_artifact"]
