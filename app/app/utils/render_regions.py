"""Canonical-on-disk marker helpers — shared by DOCX, PPTX, and XLSX exporters.

The rendered DOCX / PPTX / XLSX is the canonical record of an artifact's
analyst-visible state. The renderer emits regions — one per addressable
schema field — and each region carries a format-native marker that encodes
the field's path. There are no orphan paragraphs, shapes, or cells anywhere
outside a marker region (the gap-free invariant). The importer walks every
marker in the file, rebuilds `structured_data`, and overwrites
`schema_state.json` next to the deliverable.

This module hosts:
  - `parse_path` / `emit_path` — the dotted-path tokenizer/emitter shared by
    all three formats. Format-specific prefixes (`pytho:` for DOCX,
    `pytho.` for PPTX, `pytho_` for XLSX) are applied at the call site;
    the BODY grammar is identical.
  - DOCX HTML carrier wrappers: `sdt_wrap_html` (block-level <div data-pytho-tag>),
    `sdt_inline_wrap_html` (inline <span data-pytho-tag>). `convert.js`
    recognizes these attributes and emits w:sdt block / inline-level wrappers.
  - PPTX shape-name helper: `shape_name_for(field_path)` -> str. The PPTX
    renderer sets `shape.name = shape_name_for(...)` directly; the per-paragraph
    hidden-run marker uses `paragraph_marker_text(field_path)`.
  - XLSX defined-name helper: `xlsx_token(sheet_slug, field_path) -> str`,
    `register_defined_name(workbook, sheet, range_ref, token)`. Tokens
    follow `[A-Za-z_][A-Za-z0-9_]*` to satisfy Excel's defined-name rules.
  - Empty-region emitters per format.

`parse(emit(x)) == x` for every legal field path. The shared grammar is the
contract every walker / renderer reads.
"""

from __future__ import annotations

import html as _html
import re
from typing import List, Union, Iterator, Tuple

# Marker format version. Importer reads this from a sibling tag; renderer
# stamps it on every region. Bump to 1.x for additive changes; 2.0 forces
# a full refire (no auto-migration).
MARKER_FORMAT_VERSION = "1.0"

# Format-specific namespace prefixes for the same dotted body grammar.
DOCX_PREFIX = "pytho:"
PPTX_PREFIX = "pytho."
XLSX_PREFIX = "pytho_"

# ASCII-only path characters. Brackets carry list indices; `.` separates
# identifier segments; `:` appears only in the format prefix (DOCX) and in
# region-tag suffixes (`:heading`, `:body`, `:absent`, `:badge`, `:rule`,
# `:frame`, `:narrative`). No spaces, no quotes, no XML entities.
_PATH_BODY_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Region-tag suffixes (paint-only sub-regions). Importers strip these before
# reading values.
REGION_SUFFIXES = (":heading", ":body", ":bullet", ":badge", ":rule",
                   ":frame", ":absent", ":narrative", ":label", ":value")


PathToken = Union[str, int]


def parse_path(tag: str) -> List[PathToken]:
    """Parse a marker tag into a list of path tokens.

    Accepts any of the three format prefixes (pytho:, pytho., pytho_) and
    decodes the body. Returns identifiers as strs and list indices as ints.

    Examples:
      parse_path("pytho:coas[2].statement.what")
        -> ["coas", 2, "statement", "what"]
      parse_path("pytho.coas[0].title")
        -> ["coas", 0, "title"]
      parse_path("pytho_CARVER__row_3__criticality_score")
        -> ["CARVER", "row_3", "criticality_score"]  # XLSX grammar uses
                                                       # double-underscore
                                                       # separators

    Region-tag suffixes (e.g. ":heading") are stripped from the final token
    and re-attached as `(token, region_suffix)` tuple via the caller when
    needed; for the basic walker contract, `parse_path` discards the suffix.
    """
    if not isinstance(tag, str) or not tag:
        raise ValueError(f"empty marker tag: {tag!r}")

    # Strip trailing region tag (`:heading`, `:absent`, etc.) — keep just
    # the field path for reconstruction. The walker can recover the suffix
    # via parse_path_with_region.
    for suffix in REGION_SUFFIXES:
        if tag.endswith(suffix):
            tag = tag[: -len(suffix)]
            break

    # Strip the format prefix.
    if tag.startswith(DOCX_PREFIX):
        body = tag[len(DOCX_PREFIX):]
        is_xlsx = False
    elif tag.startswith(PPTX_PREFIX):
        body = tag[len(PPTX_PREFIX):]
        is_xlsx = False
    elif tag.startswith(XLSX_PREFIX):
        body = tag[len(XLSX_PREFIX):]
        is_xlsx = True
    else:
        raise ValueError(f"not a Pytho marker (missing prefix): {tag!r}")

    if is_xlsx:
        # XLSX defined-name grammar: tokens separated by `__`, list indices
        # encoded as `idx_<n>`, sheet slug carried as first token.
        # Example: pytho_CARVER__row_3__criticality_score
        # The leading `header` sentinel (pytho_header__classification) is
        # also expressed via this grammar; "header" is just the first path
        # token.
        # For matrix / item grammar:
        #   pytho_<sheet>__row_<i>__<field>
        #   pytho_<sheet>__item_<i>__<field>
        #   pytho_<sheet>__matrix__<row_slug>__<col_slug>
        #   pytho_<sheet>__section_<key>
        tokens: List[PathToken] = []
        for seg in body.split("__"):
            if not seg:
                continue
            # row_<i> / item_<i> -> ("rows" or "items", int)
            m = re.match(r"^(row|item)_(\d+)$", seg)
            if m:
                tokens.append("rows" if m.group(1) == "row" else "items")
                tokens.append(int(m.group(2)))
                continue
            tokens.append(seg)
        return tokens
    else:
        # DOCX / PPTX dotted-bracket grammar.
        tokens = []
        i = 0
        while i < len(body):
            j = i
            while j < len(body) and body[j] not in ".[":
                j += 1
            if j > i:
                tokens.append(body[i:j])
            i = j
            if i < len(body) and body[i] == "[":
                close = body.index("]", i)
                tokens.append(int(body[i + 1:close]))
                i = close + 1
            if i < len(body) and body[i] == ".":
                i += 1
        return tokens


def parse_path_with_region(tag: str) -> Tuple[List[PathToken], str]:
    """Return (path_tokens, region_suffix). region_suffix is "" when absent."""
    if not isinstance(tag, str) or not tag:
        return ([], "")
    suffix = ""
    for s in REGION_SUFFIXES:
        if tag.endswith(s):
            suffix = s
            break
    return (parse_path(tag), suffix)


def emit_path(path: List[PathToken], *, prefix: str = DOCX_PREFIX) -> str:
    """Emit a marker tag string for a path.

    DOCX / PPTX use dotted-bracket grammar; XLSX uses double-underscore
    grammar. Set `prefix` to one of `DOCX_PREFIX`, `PPTX_PREFIX`,
    `XLSX_PREFIX`. Round-trip identity: `parse_path(emit_path(p)) == p`
    for every legal path.
    """
    if prefix == XLSX_PREFIX:
        # XLSX: sheet slug + path joined by `__`. List indices encoded as
        # the leading token's index_<i> partner. For example:
        #   ["CARVER", "rows", 3, "criticality_score"]
        #     -> "pytho_CARVER__row_3__criticality_score"
        out = [XLSX_PREFIX]
        i = 0
        first = True
        while i < len(path):
            tok = path[i]
            if isinstance(tok, str) and tok in ("rows", "items") and i + 1 < len(path) and isinstance(path[i + 1], int):
                sep = "" if first else "__"
                singular = "row" if tok == "rows" else "item"
                out.append(f"{sep}{singular}_{path[i+1]}")
                i += 2
                first = False
                continue
            if isinstance(tok, int):
                # Bare integer (rare in XLSX grammar) -> idx_<n>.
                sep = "" if first else "__"
                out.append(f"{sep}idx_{tok}")
                i += 1
                first = False
                continue
            sep = "" if first else "__"
            out.append(f"{sep}{tok}")
            i += 1
            first = False
        return "".join(out)
    else:
        # DOCX / PPTX dotted-bracket grammar.
        out = [prefix]
        for k, tok in enumerate(path):
            if isinstance(tok, int):
                out.append(f"[{tok}]")
            else:
                if k > 0 and not out[-1].endswith("]"):
                    out.append(".")
                elif k > 0:
                    out.append(".")
                out.append(str(tok))
        return "".join(out)


# ---------------------------------------------------------------------------
# DOCX HTML carriers — the convert.js side reads data-pytho-tag and emits
# w:sdt block / inline-level wrappers.
# ---------------------------------------------------------------------------


def _esc_attr(s: str) -> str:
    """Escape an attribute value for HTML emission."""
    if s is None:
        return ""
    return _html.escape(str(s), quote=True)


def docx_tag(path: List[PathToken]) -> str:
    """Emit the DOCX `w:tag/@w:val` value for a path."""
    return emit_path(path, prefix=DOCX_PREFIX)


def sdt_wrap_html(path: List[PathToken], alias: str, inner_html: str) -> str:
    """Wrap rendered HTML in a block-level marker carrier.

    `path` is the schema path tokens. `alias` is the human label
    (`render_label`) for editor display. `inner_html` is the already-
    rendered content.

    The `<div data-pytho-tag="..." data-pytho-alias="...">` element is a
    pure marker carrier — convert.js recognizes the data attributes and
    emits a block-level `w:sdt` wrapper around the inner content (no
    paragraph for the div itself; the inner block elements become the
    SDT's `w:sdtContent` children).
    """
    tag = docx_tag(path)
    return (
        f'<div data-pytho-tag="{_esc_attr(tag)}"'
        f' data-pytho-alias="{_esc_attr(alias)}">{inner_html or ""}</div>'
    )


def sdt_inline_wrap_html(path: List[PathToken], alias: str, inner_html: str) -> str:
    """Wrap rendered HTML in an inline-level marker carrier.

    `<span data-pytho-tag="...">` — convert.js emits an inline-level
    `w:sdt` wrapping the resulting run.
    """
    tag = docx_tag(path)
    return (
        f'<span data-pytho-tag="{_esc_attr(tag)}"'
        f' data-pytho-alias="{_esc_attr(alias)}">{inner_html or ""}</span>'
    )


def sdt_empty_region_html(path: List[PathToken], alias: str) -> str:
    """Emit a block-level empty-region marker for an absent / empty field."""
    return sdt_wrap_html(path, alias, "<p>&nbsp;</p>")


# ---------------------------------------------------------------------------
# PPTX shape-name helpers.
# ---------------------------------------------------------------------------


def pptx_shape_name(path: List[PathToken], *, region: str = "") -> str:
    """Build a PPTX shape name (`p:cNvPr/@name`) for a field path.

    `region` is one of the closed-set region-tag suffixes (":heading",
    ":body", ":badge", ":rule", ":frame", ":absent", ":narrative", "").
    """
    base = emit_path(path, prefix=PPTX_PREFIX)
    if region:
        if not region.startswith(":"):
            region = ":" + region
        return base + region
    return base


def pptx_paragraph_marker(path: List[PathToken], *, region: str = "") -> str:
    """Build a [[<marker>]] paragraph-marker token for shared text frames.

    The PPTX renderer prepends a 1pt-sized hidden run carrying this token
    ahead of every visible paragraph in shared text frames (the narrative
    column on Threat COA slides, task-org sidebar). The importer reads the
    leading `[[...]]` prefix and routes the visible runs to the named
    subfield.
    """
    name = pptx_shape_name(path, region=region)
    return f"[[{name}]]"


# ---------------------------------------------------------------------------
# XLSX defined-name helpers.
# ---------------------------------------------------------------------------


def xlsx_safe_token(name: str) -> str:
    """Sanitize a string into the XLSX defined-name token charset.

    Excel defined names: `[A-Za-z_][A-Za-z0-9_]{0,254}`. ASCII only;
    no `.`, no `-`, no space, no Unicode. First character is a letter
    or underscore. Length capped at 255.
    """
    if not name:
        return "x"
    clean = "".join(c if c.isalnum() or c == "_" else "_" for c in str(name))
    if not clean or clean[0].isdigit():
        clean = "_" + clean
    return clean[:255]


def xlsx_token(sheet_slug: str, field_path: List[PathToken]) -> str:
    """Build an XLSX defined-name token for a (sheet, field_path) pair.

    Grammar: `pytho_<sheet_slug>__<field_path>` where field_path follows
    the XLSX double-underscore convention (row_<i>, item_<i>, etc).
    """
    sheet = xlsx_safe_token(sheet_slug or "Sheet")
    body_path = [sheet] + list(field_path)
    return emit_path(body_path, prefix=XLSX_PREFIX)


def xlsx_header_token(field_name: str) -> str:
    """Build a `pytho_header__<field_name>` token."""
    return f"{XLSX_PREFIX}header__{xlsx_safe_token(field_name)}"


def xlsx_register_defined_name(workbook, token: str, sheet_name: str,
                               cell_ref: str) -> None:
    """Register a defined name on the workbook pointing at the given cell/range.

    `cell_ref` is a sheet-relative reference like `$A$3` or `$A$3:$F$10`.
    Sheet name is single-quoted (Excel requires it for names containing
    spaces / special chars; we always quote for consistency).
    """
    try:
        from openpyxl.workbook.defined_name import DefinedName
    except ImportError:  # pragma: no cover — openpyxl is a hard dep elsewhere
        return
    attr_text = f"'{sheet_name}'!{cell_ref}"
    try:
        workbook.defined_names[token] = DefinedName(name=token, attr_text=attr_text)
    except Exception:
        # Duplicate name / openpyxl rejection — log via caller side.
        pass


# ---------------------------------------------------------------------------
# Schema introspection helpers (shared between exporter + importer).
# ---------------------------------------------------------------------------


def iter_schema_paths(schema: dict) -> Iterator[Tuple[List[PathToken], dict, str]]:
    """Yield (field_path, spec, shape_kind) for every addressable field in a
    schema.

    Walks header_fields, paragraph_fields (recursing into list_of_dicts
    item_subfields up to depth 6), row_fields, matrix_spec. Used by:
      - renderers to discover which paths to emit empty-region markers for.
      - importer to drive `_validate`'s required-field checks.
    """
    for field_name, spec in (schema.get("header_fields") or {}).items():
        yield (["header", field_name], spec or {}, "header")

    for p in (schema.get("paragraph_fields") or []):
        name = p.get("name")
        if not name:
            continue
        shape = p.get("shape", "string")
        yield ([name], p, f"paragraph:{shape}")
        if shape == "list_of_dicts":
            for sf in (p.get("item_subfields") or []):
                sf_name = sf.get("name")
                if not sf_name:
                    continue
                # Path includes a placeholder index '*' — callers expand
                # to actual i at emit time. We yield the abstract path so
                # required-field checks can see the schema's declared
                # subfields without knowing the runtime cardinality.
                yield ([name, "*", sf_name], sf, "item_subfield")

    for rf_name, rf_spec in (schema.get("row_fields") or {}).items():
        yield (["rows", "*", rf_name], rf_spec or {}, "row_field")


def schema_field_lookup(schema: dict, path: List[PathToken]) -> dict:
    """Look up a path's spec in the schema. Returns {} for unknown paths."""
    if not path:
        return {}
    head = path[0]
    if head == "header" and len(path) >= 2:
        return (schema.get("header_fields") or {}).get(path[1]) or {}
    # rows[<i>].<field> -> look up row_fields[<field>]. The legacy form of
    # this check baked an operator-precedence bug:
    #   head == "rows" and len(path) >= 3 and path[1] in ("*",) or (
    #       isinstance(path[1] if len(path) > 1 else None, int)
    #   )
    # evaluated as `(... and ... and ...) or (isinstance ...)`, so ANY path
    # whose second token was an int — `templated_units[0].id`,
    # `shaping_operations[2].unit_id`, etc. — fell into the rows branch and
    # tried to resolve subfields against `row_fields` instead of the
    # paragraph_field's `item_subfields`. Rewrite with explicit parens.
    if head == "rows" and len(path) >= 2 and (
        path[1] == "*" or isinstance(path[1], int)
    ):
        if len(path) >= 3:
            return (schema.get("row_fields") or {}).get(path[2]) or {}
        return {"shape": "row"}
    # paragraph_fields: head is the field name
    for p in (schema.get("paragraph_fields") or []):
        if p.get("name") == head:
            if len(path) == 1:
                return p
            # subfield walk
            shape = p.get("shape", "string")
            if shape == "list_of_dicts" and len(path) >= 3:
                # path[1] is index (int), path[2] is subfield name
                sub_name = path[2]
                for sf in (p.get("item_subfields") or []):
                    if sf.get("name") == sub_name:
                        if len(path) == 3:
                            return sf
                        # nested: list_of_dicts of list_of_dicts
                        sub_shape = sf.get("shape", "string")
                        if sub_shape == "list_of_dicts" and len(path) >= 5:
                            for sub_sf in (sf.get("item_subfields") or []):
                                if sub_sf.get("name") == path[4]:
                                    return sub_sf
                        return sf
                return {}
            if shape == "dict" and len(path) >= 2:
                # dict subkey
                sub_name = path[1]
                for sf in (p.get("item_subfields") or []):
                    if sf.get("name") == sub_name:
                        return sf
                return {}
            return p
    return {}


__all__ = [
    "MARKER_FORMAT_VERSION",
    "DOCX_PREFIX",
    "PPTX_PREFIX",
    "XLSX_PREFIX",
    "REGION_SUFFIXES",
    "parse_path",
    "parse_path_with_region",
    "emit_path",
    "docx_tag",
    "sdt_wrap_html",
    "sdt_inline_wrap_html",
    "sdt_empty_region_html",
    "pptx_shape_name",
    "pptx_paragraph_marker",
    "xlsx_safe_token",
    "xlsx_token",
    "xlsx_header_token",
    "xlsx_register_defined_name",
    "iter_schema_paths",
    "schema_field_lookup",
]
