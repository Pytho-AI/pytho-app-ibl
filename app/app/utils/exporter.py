"""Schema-driven HTML/DOCX/PPTX exporter for the 15 doctrinal artifact types.

Everything that varies per product lives in `schemas.PRODUCT_SCHEMAS`. This
module contains zero product-specific branches: it reads `shape`,
`page_orientation`, `header_fields`, `row_fields`, `paragraph_fields`,
`matrix_spec`, `published_section_title`, `group_by`, and renders generically.

Visual style follows real Army doctrine examples (FM 3-60 App C, FM 6-99 App A):
- Classification banner is the DOCX/PPTX page header AND footer (centered,
  bold, all-caps), not a colored bar in the body.
- Body is black-on-white with light-gray (#d9d9d9) table-header shading and
  thin black borders. No navy/red color overload.
- H1 = title, bold sans-serif with thin bottom-rule, black.
- Tables compact, single-row light-gray header, alternating bodies optional.
- Caption below each table: "Table N. <description>" centered italic.

CLI:
    python exporter.py <session_state.json> [<output_dir>]
"""
import json, logging, os, re, sys, html, subprocess
from pathlib import Path
from typing import Optional, Tuple
from templates import PRODUCT_SCHEMAS
# Canonical-on-disk marker helpers (render_regions module). Imported lazily
# below the module init so the import has no chance of cycling with anything
# that imports exporter. The helpers are pure-functional and side-effect free.
from utils.render_regions import (
    sdt_wrap_html as _sdt_wrap_html,
    sdt_inline_wrap_html as _sdt_inline_wrap_html,
    sdt_empty_region_html as _sdt_empty_region_html,
    pptx_shape_name as _pptx_shape_name,
    pptx_paragraph_marker as _pptx_paragraph_marker,
)

log = logging.getLogger(__name__)

# Resolve convert.js relative to this file (app/app/utils/ → ../docx-converter/).
DOCX_CONVERTER = str(Path(__file__).resolve().parent.parent / "docx-converter" / "convert.js")

# Classification banner colors per DoDM 5200.01 (Volume 2, Enclosure 3).
# Banner = filled strip, white bold text, full page width. Marking string is
# normalized (uppercase, strip CUI/portion-mark slashes) before lookup so any
# of UNCLASSIFIED / CUI / SECRET / TOP SECRET hits the right swatch.
CLASSIFICATION_COLORS = {
    "UNCLASSIFIED":      {"fill": "#007a33", "text": "#FFFFFF"},  # green
    "CUI":               {"fill": "#522D80", "text": "#FFFFFF"},  # purple
    "CONFIDENTIAL":      {"fill": "#0033A0", "text": "#FFFFFF"},  # blue
    "SECRET":            {"fill": "#C8102E", "text": "#FFFFFF"},  # red
    "TOP SECRET":        {"fill": "#FF8C00", "text": "#000000"},  # orange
    "TOP SECRET//SCI":   {"fill": "#FFD700", "text": "#000000"},  # yellow
}
def artifact_filename_base(artifact_type: str, schema: dict,
                             structured_data: dict, artifact_id: str) -> str:
    """Resolve a human-readable filename base for an artifact's deliverable
    files (docx / pptx / xlsx). Returns the schema's `title_field` resolved
    value when available (e.g. INTSUM's 'intsum_number' -> 'INTSUM-291800ZJUN26-300600ZJUN26'),
    falling back to the autodraft `artifact_id` when no title_field is
    declared or the resolved value is half-baked (e.g. 'INTSUM--' with
    empty period DTGs). Sanitizes for filesystem use — filesystem-illegal
    characters replaced with `_`.

    Mirrors the same title resolution render_artifact_html uses to set the
    HTML <title>; lifted out so the file-writer can name `INTSUM-...docx`
    instead of `artifact.docx`."""
    header = (structured_data or {}).get("header") or {}
    title_field = (schema or {}).get("title_field")
    candidate = ""
    if title_field:
        hspecs = (schema.get("header_fields") or {})
        # Prefer the live default-template recipe over the stored value
        # (same as render_artifact_html's logic) so the filename reflects
        # the freshest period DTGs / canonical-now.
        tdef = ((hspecs.get(title_field) or {}).get("default") or "")
        if isinstance(tdef, str) and tdef:
            try:
                class _SafeDict(dict):
                    def __missing__(self, key):
                        return "{" + key + "}"
                _candidate = tdef.format_map(_SafeDict(
                    {k: ("" if v is None else v) for k, v in header.items()}))
            except (ValueError, IndexError):
                _candidate = tdef
            if not re.search(r"[-_/]{2,}|^[-_/ ]+|[-_/ ]+$|^\s*$", _candidate):
                candidate = _candidate
        if not candidate:
            _stored = (header.get(title_field) or "").strip()
            if _stored and not re.search(r"[-_/]{2,}|^[-_/ ]+|[-_/ ]+$", _stored):
                candidate = _stored
    if not candidate:
        candidate = artifact_id or "artifact"
    # Filesystem hygiene — replace path separators + control chars + things
    # that browsers / shells / Excel choke on in download UX.
    cleaned = re.sub(r'[\\/:"*?<>|\x00-\x1f]', "_", candidate)
    cleaned = cleaned.strip(" .")  # trailing dot/space breaks Windows
    return cleaned or "artifact"


def _classification_colors(marking: str) -> dict:
    """Return {fill, text} for a marking. Falls back to UNCLASSIFIED green if
    no swatch matches. Normalizes the marking by uppercasing and stripping
    common portion-mark variants."""
    if not marking: return CLASSIFICATION_COLORS["UNCLASSIFIED"]
    m = str(marking).strip().upper()
    if m in CLASSIFICATION_COLORS: return CLASSIFICATION_COLORS[m]
    # Try the base marking (before any //CAVEAT or //REL TO)
    base = m.split("//", 1)[0].strip()
    if base in CLASSIFICATION_COLORS: return CLASSIFICATION_COLORS[base]
    return CLASSIFICATION_COLORS["UNCLASSIFIED"]


PRODUCT_LONG_NAMES = {
    # ATP 2-01.3 Ch. 3 — Step 1 IPOE. ATP 2-01.3 publishes no acronym; the
    # registry key IS the full doctrinal name (identity-mapped).
    "Operational Environment Definition": "Operational Environment Definition",
    "HVTL": "High-Value Target List",
    "HPTL": "High-Payoff Target List",
    # FM 3-60 (Aug 2023) Decide-phase targeting trio — App. D Tables D-2 / D-3 / D-4.
    "TSS": "Target Selection Standards",
    "AGM": "Attack Guidance Matrix",
    "TSM": "Target Synchronization Matrix",
    "CARVER": "CARVER Target Value Analysis",
    "BDA": "Battle Damage Assessment",
    "TMP": "Target Material Production",
    "TIDAT": "Target Intelligence Data Report",
    "SITREP": "Situation Report",
    "INTSUM": "Intelligence Summary",
    "GRINTSUM": "Graphical Intelligence Summary",
    # ATP 2-01.3 para 4-6 / Fig 4-2 — no doctrinal acronym; key is full name.
    "Threat Overlay": "Threat Overlay",
    "SITEMP": "Situation Template",
    # Renamed from "Enemy Course of Action" on 2026-05-30 — see
    # enemy_course_of_action.py module docstring. PRODUCT_SCHEMAS key
    # stays Threat COA Statement (registry / depends_on / on-disk filenames depend on
    # it); only the human-facing long name flips.
    "Threat COA Statement": "Threat COA Statement",
    "WEM": "Weather Effects Matrix",
    "TEM": "Terrain Effects Matrix",
    "MCOO": "Modified Combined Obstacle Overlay",
    "CCA": "Civil Considerations Assessment",
    # ATP 2-01.3 Step-3 threat product family — no doctrinal acronyms. Threat
    # Template is a Threat Model sub-section per para 5-45 (not a standalone
    # product).
    "Threat Model":                "Threat Model",
    "Threat Capability Statement": "Threat Capability Statement",
    # ATP 2-01.3 Step-4 IC trio (Ch. 6 paras 6-64 – 6-72) — no doctrinal acronyms.
    "Event Template":                "Event Template",
    "Decision Support Template":     "Decision Support Template",
    # Red (threat-perspective) DST — full doctrinal name is the registry key
    # (identity-mapped, no acronym).
    "Threat Decision Support Template": "Threat Decision Support Template",
    "Information Collection Matrix": "Information Collection Matrix",
    # ATP 2-33.4 App. E intel estimates — no doctrinal acronyms.
    "Intelligence Estimate":         "Intelligence Estimate",
    "Intelligence Running Estimate": "Intelligence Running Estimate",
    # FM 3-60 Aug 2023 Ch. 2 + JP 3-60 App. C Assess-phase closure. MEA findings
    # and Reattack Recommendation are CA sections per para 2-70, not separate
    # products.
    "CA":  "Combat Assessment",
    # JP 3-60 Ch. III §3.b.(4) + Fig III-3/III-4 system-level target development.
    # TSA label retained for the rare case an old artifact lingers on disk
    # (a published TSA from before the 2026-06-08 archive — see
    # templates/products/_archived/README.md). The rail registration was
    # dropped at the same date; nothing in the active set produces TSA now.
    "TSA": "Target System Analysis",
    # JP 3-60 + CJCSI 3122.06 / 3160.01D legal/ROE pre-strike gates.
    "NSL": "No-Strike List",
    "RTL": "Restricted Target List",
    "CDE": "Collateral Damage Estimate",
}

PRODUCT_CATEGORIES = {
    # Running Estimates & Reports — INTSUM/SITREP family, intelligence
    # estimates. Companions to the IPOE chain (staff products an analyst
    # produces on a recurring cadence).
    # Report family first (INTSUM → GRINTSUM → SITREP), then the estimate
    # family (Intelligence Estimate → Intelligence Running Estimate). The
    # Running Estimate is the continuously-updated rollup — it consumes both
    # the Intelligence Estimate and SITREP, so it sits last per the DAG.
    "INTSUM":                        ("Running Estimates & Reports", 1, None),
    "GRINTSUM":                      ("Running Estimates & Reports", 2, None),
    "SITREP":                        ("Running Estimates & Reports", 3, None),
    "Intelligence Estimate":         ("Running Estimates & Reports", 4, None),
    "Intelligence Running Estimate": ("Running Estimates & Reports", 5, None),

    # IPOE — ATP 2-01.3 four-step process. The step prefix lives in the rail
    # label so analysts can scan by step number.
    "Operational Environment Definition": ("IPOE",  1, "Step 1"),
    "WEM":                                 ("IPOE",  2, "Step 2"),
    "TEM":                                 ("IPOE",  3, "Step 2"),
    "MCOO":                                ("IPOE",  4, "Step 2"),
    "CCA":                                 ("IPOE",  5, "Step 2"),
    # CCA-Matrix is the XLSX sibling of CCA — sits directly beside it.
    "CCA-Matrix":                          ("IPOE",  5.5, "Step 2"),
    # Threat Overlay belongs to IPB Step 3 — Evaluate the Threat per
    # ATP 2-01.3 ¶4-6 / Fig 4-2. It's the rendered overlay portion of
    # the threat template family (Threat Template → Threat Model →
    # Threat Capability Statement → Threat Overlay), all Step 3.
    "Threat Overlay":                      ("IPOE",  6, "Step 3"),
    "Threat Model":                        ("IPOE",  7, "Step 3"),
    # Threat-Description-Table is the XLSX sibling in the threat family;
    # placed beside Threat Model per the categorization spec.
    "Threat-Description-Table":            ("IPOE",  7.5, "Step 3"),
    "Threat Capability Statement":         ("IPOE",  8, "Step 3"),
    "Threat COA Statement":                                ("IPOE",  9, "Step 4"),
    "SITEMP":                              ("IPOE", 10, "Step 4"),
    # Threat COAs — commander-facing PPTX. Consumes Threat COA Statement +
    # SITEMP (see consumes_artifacts), so it follows SITEMP in the rail
    # rather than preceding it.
    "Threat COAs":                         ("IPOE", 10.5, "Step 4"),
    "Event Template":                      ("IPOE", 11, "Step 4"),
    # Event-Matrix is the XLSX sibling of Event Template.
    "Event-Matrix":                        ("IPOE", 11.5, "Step 4"),
    "Decision Support Template":           ("IPOE", 12, "Step 4"),
    # DST-Matrix is the XLSX sibling of the Decision Support Template.
    "DST-Matrix":                          ("IPOE", 12.5, "Step 4"),
    # Threat (red) DST — threat-commander mirror of the blue DST; sits beside
    # it in the rail.
    "Threat Decision Support Template":    ("IPOE", 12.7, "Step 4"),
    "Information Collection Matrix":       ("IPOE", 13, "Step 4"),
    "HVTL":                                ("IPOE", 14, "Step 4"),

    # Collection — PIR is the parent; ICSM is the synchronization matrix;
    # DSM (Decision Support Matrix) is the Tier-2 fused collection product.
    "PIR":  ("Collection", 1, None),
    "ICSM": ("Collection", 2, None),
    "DSM":  ("Collection", 3, None),

    # Targeting & Fires — FM 3-60 / JP 3-60 D3A (Decide → Deliver → Assess)
    # chain. Ordered to follow the DAG dependency edges (each product's
    # consumes_artifacts) so nothing precedes its upstream:
    #   Decide  — TSA (system target development) → CARVER (target-value
    #             analysis) → HPTL → TSS → AGM → TSM (targeting working-group
    #             matrices, FM 3-60 App. D), then the NSL/RTL vetting lists.
    #   Deliver — TMP → TIDAT (per-target packets) → CDE (collateral damage
    #             estimate; consumes TMP + NSL + RTL, so it sits after them).
    #   Assess  — BDA → CA (CA = BDA + MEA + RR per FM 3-60 para 2-70).
    # TSA ARCHIVED 2026-06-08 — theater/J-2 product, not corps-authored.
    # Rail position dropped; sidebar starts at HVTL (to be moved here
    # from IPOE Step 4 in the per-COA refactor — see
    # scratch/docs/ibl/per-coa-product-scope.md §8.1).
    "CARVER": ("Targeting & Fires",  2, None),
    "HPTL":   ("Targeting & Fires",  3, None),
    "TSS":    ("Targeting & Fires",  4, None),
    "AGM":    ("Targeting & Fires",  5, None),
    "TSM":    ("Targeting & Fires",  6, None),
    "NSL":    ("Targeting & Fires",  7, None),
    "RTL":    ("Targeting & Fires",  8, None),
    "TMP":    ("Targeting & Fires",  9, None),
    "TIDAT":  ("Targeting & Fires", 10, None),
    "CDE":    ("Targeting & Fires", 11, None),
    "BDA":    ("Targeting & Fires", 12, None),
    "CA":     ("Targeting & Fires", 13, None),
    # CA-Engagements is the XLSX sibling of Combat Assessment.
    "CA-Engagements": ("Targeting & Fires", 13.5, None),

    # Single-INT — Tier 1 analytical products per discipline cell.
    "CCM":       ("Single-INT", 1, None),
    "HUMINTSUM": ("Single-INT", 2, None),
    "KLIEGLIGHT":("Single-INT", 3, None),
    "LOS":       ("Single-INT", 4, None),
    "OSIR":      ("Single-INT", 5, None),
    "SIGSUM":    ("Single-INT", 6, None),
}

# Canonical category render order for the rail. Categories not in this list
# (e.g. "Custom" for analyst-uploaded product types) are appended at the
# bottom in insertion order.
PRODUCT_CATEGORY_ORDER = (
    "Running Estimates & Reports",
    "IPOE",
    "Collection",
    "Targeting & Fires",
    "Single-INT",
)


# Map-bearing products are declared per-schema via paragraph_fields with
# shape="map" — no central allow-list. The single-map and multi-map cases
# share the same schema convention: each map paragraph_field's name is the
# PNG filename basename under /files/<uid>/<aid>/<field_name>.png.

# Header keys that are stored on the artifact for plumbing (audit / ownership /
# session correlation) but must NOT render into the visible header chrome.
# These are filtered out of both the metadata ribbon and the key/value table.
_HEADER_EXCLUDE_KEYS = {
    "classification",
    "owner_user_id",
    "user_id",
    "session_id",
    "project_id",
    "artifact_id",
    "turn_id",
    "trace_id",
    # `status` carries lifecycle state (draft/published) for the project tree
    # view but should not render visibly — the DRAFT watermark already conveys
    # the lifecycle state in the rendered artifact.
    "status",
}

# ---------------------------------------------------------------------------
# Style tokens (pure inline — Word ignores <style> blocks)
# Cell padding + line-height tuned for narrative cells (FM 6-99 / FM 3-60 use
# generous spacing; previous compact form crammed multi-line cells).
# ---------------------------------------------------------------------------
S_BODY = "font-family:'Times New Roman',serif;font-size:12pt;color:#000000;line-height:1.15"
# Heading vertical-space declared via explicit margin-top + margin-bottom
# (convert.js parses these explicitly; the CSS `margin:` shorthand is not
# unpacked, so a shorthand declaration silently produced 0 / default spacing
# in the DOCX output and ran each heading flush against its successor).
# AR 25-50 §1-19a/b: Times New Roman 12pt body, headings differ by weight not
# size — no point-size jump for H1/H2/H3 in Army correspondence / analytical
# products. Only weight (bold) and case (uppercase) change.
S_H1 = ("font-family:'Times New Roman',serif;font-size:12pt;"
        "font-weight:bold;color:#000000;"
        "text-transform:uppercase;letter-spacing:0.5pt;"
        "border-bottom:1pt solid #000000;padding-bottom:4pt;"
        "margin-top:0pt;margin-bottom:14pt")
S_H2 = ("font-family:'Times New Roman',serif;font-size:12pt;"
        "font-weight:bold;color:#000000;"
        "text-transform:uppercase;letter-spacing:0.4pt;"
        "margin-top:18pt;margin-bottom:8pt")
# Filled-bar section heading — doctrinal-style (FM 6-99 / FM 3-60 use a dark
# fill with white text for major section breaks inside a product). Use this
# for top-level paragraph_field sections so they are visually distinct from
# subheadings (H3) underneath.
S_H2_FILLED = ("font-size:12pt;font-weight:bold;color:#FFFFFF;"
                "background-color:#2c3e50;"
                "text-transform:uppercase;letter-spacing:0.5pt;"
                "padding:6pt 10pt;margin-top:20pt;margin-bottom:10pt;"
                "border:0.5pt solid #1a252f")
# Sub-section heading inside a sectioned paragraph (e.g., per-operation block
# in WEM). Heavier than S_H3 so the per-operation subheader visually anchors
# its parameter table.
S_H3_BOLD = ("font-size:10.5pt;font-weight:bold;color:#000000;"
              "text-transform:uppercase;letter-spacing:0.3pt;"
              "background-color:#e8eaed;padding:4pt 8pt;"
              "border-left:3pt solid #2c3e50;"
              "margin-top:14pt;margin-bottom:6pt")
S_H3 = ("font-size:10pt;font-weight:bold;color:#000000;"
        "margin-top:14pt;margin-bottom:6pt")
S_TABLE = ("border-collapse:collapse;width:100%;table-layout:fixed;"
           "margin-top:8pt;margin-bottom:16pt;page-break-inside:avoid")
# Cell padding: 6pt vertical + 10pt horizontal across all table cells. The
# old 7pt × 8pt spec produced visually-tight cells (content sitting on the
# borders) that analysts flagged during WEM review. Convert.js maps `padding`
# on TD/TH to OOXML tableCellMargins, so the bump lands in DOCX as well.
# convert.js parses `Xpt` as raw DXA. Vertical 6 / horizontal 10 sits inside
# the "readable but not bloated" band recommended in the spec.
S_TH_HEADER_ROW = ("background-color:#d9d9d9;color:#000000;font-weight:bold;"
                    "font-size:9pt;padding:6pt 10pt;text-align:left;"
                    "border:1pt solid #000000;line-height:1.3")
S_TH_LABEL = ("background-color:#f2f2f2;color:#000000;font-weight:bold;"
               "font-size:9pt;padding:6pt 10pt;text-align:left;"
               "border:1pt solid #000000;width:3060pt;line-height:1.3")
S_TD_VALUE = ("font-size:9pt;padding:6pt 10pt;border:1pt solid #000000;"
               "vertical-align:top;width:6300pt;line-height:1.45")
S_TD = ("font-size:9pt;padding:6pt 10pt;border:1pt solid #000000;"
        "vertical-align:top;line-height:1.45;"
        "word-wrap:break-word;overflow-wrap:break-word;word-break:normal")
S_GAP = "color:#666666;font-style:italic"
S_CAPTION = ("font-size:8.5pt;font-style:italic;color:#000000;"
              "text-align:center;margin-top:2pt;margin-bottom:14pt")
S_FOOTER_TEXT = "font-size:8pt;color:#000000;margin-top:18pt"
S_FOOTNOTE_HR = "border:0;border-top:0.75pt solid #000000;margin-top:24pt;margin-bottom:6pt"
S_FOOTNOTE_HEAD = "font-size:9pt;font-weight:bold;margin-top:6pt;margin-bottom:4pt;text-transform:uppercase;letter-spacing:0.3pt"
S_FOOTNOTE_ITEM = "font-size:8pt;margin-top:1pt;margin-bottom:1pt;line-height:1.4"

# Unicode superscript characters — Word renders these at proper superscript
# size without depending on `<sup>` / `vertical-align:super` (which convert.js
# does not honor). 1 → "¹", 23 → "²³", etc.
_SUP_DIGITS = {"0":"⁰","1":"¹","2":"²","3":"³","4":"⁴",
                "5":"⁵","6":"⁶","7":"⁷","8":"⁸","9":"⁹"}
def _to_superscript(n: int) -> str:
    return "".join(_SUP_DIGITS[c] for c in str(n))

# ---------------------------------------------------------------------------
# Footnote registry — every cell value with provenance gets a superscript
# footnote marker (real unicode superscript, not <sup>); the numbered Sources
# section renders at the end. Citations dedupe (same source → same number).
# ---------------------------------------------------------------------------
import re as _fn_re
# Citations that are pure doctrine references (methodology, not data) — these
# are the artifact's implicit context, not per-claim sources, so we drop them.
# Pattern matches: "ATP 2-01.3 Fig 4-17", "FM 3-60 §2.5", "JP 3-60 App. C",
# "CJCSI 3505.01E para 4-71", "Table 4-3", and similar pure-doctrine refs.
_DOCTRINE_ONLY_PAT = _fn_re.compile(
    r"^(?:ATP|FM|JP|CJCSI|TC)\s*\d|^(?:Fig(?:ure)?|Table|Para|App(?:endix)?\.?)\s",
    _fn_re.IGNORECASE,
)

def _is_doctrine_only_citation(c: str) -> bool:
    """True when the citation is a doctrinal-publication reference with no
    data identifier (no imagery_id, no report_id, no KG entity, no DTG).
    These references belong to the artifact's doctrinal frame, not to a
    per-claim data source."""
    if not c: return False
    s = str(c).strip()
    if not _DOCTRINE_ONLY_PAT.match(s): return False
    if _fn_re.search(r"\b(?:T-|U-|M-|I-|E-|W-|A-|SPOT-|INTREP|INTSUM|kg:|get_)\S*", s):
        return False
    if _fn_re.search(r"\b\d{6}Z\b", s): return False
    return True

# Non-citation provenance strings writers sometimes drop into _provenance when
# there's no real data source: operator directives, DTG-relative expressions,
# "no significant activity" stubs, bare tool-name handles. These shouldn't
# appear in the numbered Sources block — only real report ids / target ids /
# imagery ids / KG entities / DTG-stamped reports.
_NONCITE_NORMALIZED = {
    "operations expert directive",
    "operator directive",
    "no significant activity reported.",
    "no significant activity reported",
    "no reporting this period.",
    "no reporting this period",
    "no update this period.",
    "no update this period",
    "no_activity",
    "no activity",
    "n/a",
    "none",
    "(none)",
    "dtg",
    "nstr",
}
_DTG_ARITHMETIC_PAT = _fn_re.compile(r"^DTG\s*[-+]\s*\d+\s*[hms]?$", _fn_re.IGNORECASE)
_PURE_TOOLNAME_PAT = _fn_re.compile(r"^(?:get_|list_|render_)\w+$", _fn_re.IGNORECASE)

def _is_noncitation_provenance(c: str) -> bool:
    """Filter provenance values that aren't real data citations."""
    if not c: return True
    s = str(c).strip()
    if not s: return True
    if s.lower() in _NONCITE_NORMALIZED: return True
    if _DTG_ARITHMETIC_PAT.match(s): return True
    if _PURE_TOOLNAME_PAT.match(s): return True
    return False

_JSON_FENCE_PAT = _fn_re.compile(
    r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$",
    _fn_re.DOTALL,
)


def _maybe_parse_fenced_json(value: str):
    """The LLM writer for grid_table fields sometimes emits the matrix as
    a ```json-fenced string (or just a raw JSON object as a string)
    instead of a structured dict. When the renderer sees a str where it
    expects a dict, the grid drops to empty / quoted blob and analysts
    lose the centerpiece deliverable (CCA ASCOPE × PMESII-PT, TCS 7×4
    capabilities matrix). This helper strips ``` fences and json.loads
    the inside; returns the parsed dict if successful, else returns the
    original string unchanged so the caller's fallback path runs."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value
    # Try ```json … ``` or ``` … ``` first.
    m = _JSON_FENCE_PAT.match(s)
    inner = m.group(1).strip() if m else s
    # Only attempt parse on bracketed content.
    if not (inner.startswith("{") or inner.startswith("[")):
        return value
    try:
        import json as _json
        parsed = _json.loads(inner)
    except Exception:
        return value
    return parsed


def _sanitize_provenance_citation(c: str) -> str:
    """Map internal Pytho/Cougaar/Geo-Agent provenance tokens to clean
    analyst-facing source labels that reference the ORIGINAL data origin
    (Cougaar vertex, OPORD parse, scenario-authored fixture, upstream
    product) — not the in-process plumbing.

    Returns:
      ""           if the citation is grep canary / dev sentinel garbage
                   that should be dropped from the Sources block entirely
      transformed  if the citation can be mapped to a clean label
      c (unchanged) for citations that look analyst-ready already (raw
                    report ids, real product references, doctrinal refs)

    Per user direction 2026-06-03: scenario.py is a fixture standing in
    for Cougaar / OPORD / simulator output; the rendered label should
    name the ORIGINAL source, not "Scenario fixture …".
    """
    if not c:
        return ""
    s = str(c).strip()

    # --- Drop entirely: grep canary / dev sentinel garbage ---------------
    if "_legacy_polylines_removed_kept_for_grep_fail_fast" in s:
        return ""
    if s.startswith("DTG scenario-static"):
        # Bare "DTG scenario-static" with no underlying source.
        return ""
    if _PURE_TOOLNAME_PAT.match(s):
        # Bare tool name handles ("get_oob" / "list_targets") with no
        # context — drop. Real tool-tagged citations come through with
        # the "kg:" prefix and are mapped below.
        return ""

    # --- "Pytho autodraft pool: <PT> (<ARTIFACT_ID>) - DTG <dtg>" --------
    # Upstream-product citation. Keep just the product type.
    m = _fn_re.match(
        r"^Pytho\s+autodraft\s+pool:\s*([A-Za-z0-9_ \-]+?)\s*"
        r"(?:\(([^)]+)\))?\s*(?:[-—]\s*DTG\s+\S+)?\s*$",
        s,
    )
    if m:
        pt = (m.group(1) or "").strip()
        # Strip the artifact_id suffix; keep just the product type as the
        # upstream-product reference.
        return f"{pt} draft" if pt else "upstream product"

    # --- "Pytho artifact: <name>(<rest>)" --------------------------------
    m = _fn_re.match(r"^Pytho\s+artifact:\s*(.+)$", s)
    if m:
        rest = m.group(1).strip()
        # Strip "(PROJECT_DOC:<name>)" → just the friendly name
        rest = _fn_re.sub(r"\s*\(\s*PROJECT_DOC:\s*[^)]+\)\s*$", "", rest)
        return rest.strip()

    # --- "PROJECT_DOC:<name>" — bare doc handle --------------------------
    m = _fn_re.match(r"^PROJECT_DOC:\s*(.+)$", s)
    if m:
        return f"OPORD reference ({m.group(1).strip()})"

    # --- "kg:<tool_name>" — Cougaar tool handle --------------------------
    m = _fn_re.match(r"^kg:(\w+)$", s)
    if m:
        tool = m.group(1)
        # Map common Cougaar tool tags to analyst-doctrinal labels.
        kg_label = {
            "get_oob": "Cougaar OOB",
            "list_targets": "Cougaar targets",
            "get_spatial_overlay": "OPORD operations overlay",
            "get_terrain": "Terrain analysis",
            "get_civil": "Civil reporting",
            "get_environmental": "Environmental data",
            "get_recent_intel": "SIGINT collection",
            "list_project_docs": "Project documents",
        }.get(tool)
        if kg_label:
            return kg_label
        # Unknown tool — keep the tool name visible but drop the prefix.
        return tool.replace("_", " ")

    # --- "Scenario fixture KG.<layer>: <list>" / variants ----------------
    # scenario.py is a fixture standing in for Cougaar / OPORD parses; the
    # rendered citation should name where the data ORIGINALLY came from.
    m = _fn_re.match(
        r"^Scenario\s+fixture\s+KG\.(\w+)(?:\s*[:\-—]\s*(.*))?$",
        s,
    )
    if m:
        layer = m.group(1).lower()
        kg_origin = {
            "units": "Cougaar OOB",
            "unit_red": "Cougaar OOB (threat)",
            "unit_blue": "Cougaar OOB (friendly)",
            "target": "Cougaar targets",
            "targets": "Cougaar targets",
            "civil_entities": "Civil reporting",
            "civil_orgs": "Civil reporting",
            "weather": "Weather product",
            "events": "Event matrix",
        }.get(layer)
        if kg_origin:
            return kg_origin
        # Unknown KG layer — keep the layer name but strip the "Scenario
        # fixture" prefix and the underscore noise.
        return layer.replace("_", " ").title()

    # --- "Scenario fixture TERRAIN.<layer>" ------------------------------
    m = _fn_re.match(r"^Scenario\s+fixture\s+TERRAIN\.(\w+)", s)
    if m:
        layer = m.group(1).lower()
        if layer.startswith("_"):
            # Private fixture entry (e.g. legacy stubs). Drop.
            return ""
        return f"Terrain analysis ({layer.replace('_', ' ')})"

    # --- "Scenario fixture (via <tool>)" ---------------------------------
    m = _fn_re.match(r"^Scenario\s+fixture\s*\(\s*via\s+(\w+)\s*\)\s*$", s)
    if m:
        tool = m.group(1)
        kg_label = {
            "get_environmental": "Environmental data",
            "get_terrain": "Terrain analysis",
            "get_civil": "Civil reporting",
            "get_spatial_overlay": "OPORD operations overlay",
            "get_oob": "Cougaar OOB",
        }.get(tool)
        if kg_label:
            return kg_label

    # --- Bare "Scenario fixture …" with no recognizable structure --------
    # Drop the prefix; if nothing meaningful remains, drop entirely.
    if s.startswith("Scenario fixture"):
        tail = s[len("Scenario fixture"):].strip(" :-—,")
        if tail:
            return tail
        return ""

    # --- "Geo-Agent overlay map <uuid> layer <kind> …" -------------------
    m = _fn_re.match(
        r"^Geo[\- ]Agent\s+overlay(?:\s+map\s+[a-f0-9\-]+)?"
        r"(?:\s+layer\s+(\w+))?",
        s,
        _fn_re.IGNORECASE,
    )
    if m:
        layer = (m.group(1) or "").strip()
        if layer.startswith("geo_agent_"):
            layer = layer[len("geo_agent_"):]
        return (f"OPORD operations overlay ({layer.replace('_', ' ')})"
                if layer else "OPORD operations overlay")

    # --- Raw "<PT>_<TIMESTAMP>Z" artifact_id with no surrounding context -
    # e.g. "MCOO_20260602T051804_046Z" — collapse to "<PT> draft".
    m = _fn_re.match(r"^([A-Z][A-Za-z0-9 \-]+)_\d{8}T\d{6}_?\d*Z?$", s)
    if m:
        pt = m.group(1).strip()
        return f"{pt} draft"

    return s


def _normalize_citation(c: str) -> str:
    """Canonicalize a citation so near-duplicates dedupe in the registry.
    Common case: PW emits the same product with different `@ <DTG>` tails per
    parameter row (e.g., 'W-281200 @ 281800Z' vs 'W-281200 @ 281200Z'). The
    underlying source is one product; the DTG is metadata that belongs in the
    cell content, not the Sources section. Strip a trailing `@ DTG` / `(DTG)`
    if present and collapse whitespace."""
    if not c: return ""
    s = str(c).strip()
    # Sanitize Pytho/Cougaar/Geo-Agent internal tokens FIRST so the
    # normalized output references the original data origin (Cougaar
    # vertex, OPORD parse, upstream product) and not in-process plumbing
    # (autodraft pool, project_doc handle, kg:get_oob, scenario fixture,
    # geo-agent overlay uuid, legacy grep canary).
    s = _sanitize_provenance_citation(s)
    if not s:
        return ""
    # Strip trailing "@ <DTG>" / "@ <ts>" patterns
    s = _fn_re.sub(r"\s*@\s*\d{6,}Z?\b.*$", "", s)
    # Strip trailing "(DTG ...)" parens
    s = _fn_re.sub(r"\s*\(\s*\d{6,}Z?\b[^)]*\)\s*$", "", s)
    # Collapse internal whitespace
    s = _fn_re.sub(r"\s+", " ", s).strip()
    return s


class FootnoteRegistry:
    def __init__(self):
        self.footnotes = []  # ordered (number, citation)
        self.seen = {}       # citation → number (dedupe)

    def _register(self, citation: str) -> int:
        """Intern a citation, returning its 1-indexed number. Returns 0
        when the citation is dropped (doctrinal-only / noncitation /
        empty)."""
        if not citation: return 0
        c = _normalize_citation(citation)
        if not c: return 0
        if _is_doctrine_only_citation(c): return 0
        if _is_noncitation_provenance(c): return 0
        if c in self.seen:
            return self.seen[c]
        n = len(self.footnotes) + 1
        self.seen[c] = n
        self.footnotes.append((n, c))
        return n

    def add(self, citation: str) -> str:
        """Register a citation and return a superscript-styled HTML marker.

        Matches the military/government writing convention: footnote
        markers placed after punctuation (before em dashes), rendered as
        a small superscript number, not a bracketed numeral. The caller
        appends the returned HTML AFTER the prose's terminal punctuation:
        'enemy concentration at 35VLF.<sup>1</sup>'.

        Pure doctrinal references are dropped (the artifact's doctrinal
        frame is implicit in the product type — analyst reading a WEM
        already knows WEM doctrine). Near-duplicate citations (same
        product / tool, different `@ DTG` tails) collapse to one Sources
        entry via _normalize_citation.
        """
        n = self._register(citation)
        if not n: return ""
        return (f'<sup style="font-size:7.5pt;color:#0033aa;'
                f'vertical-align:super;line-height:0;'
                f'margin-left:1pt">{n}</sup>')

    def add_plain(self, citation: str) -> str:
        """Register a citation and return the bare marker number as text
        (e.g. "1", "12"). Used by the PPTX renderer which sets the
        superscript via python-pptx's font.superscript API rather than an
        HTML <sup> tag."""
        n = self._register(citation)
        return str(n) if n else ""

    def render_section(self) -> str:
        """Render the numbered Sources section at the end of the doc."""
        if not self.footnotes: return ""
        items = "".join(
            f'<p style="{S_FOOTNOTE_ITEM}"><b>{n}.</b> {_esc(c)}</p>'
            for n, c in self.footnotes
        )
        return (f'<hr style="{S_FOOTNOTE_HR}">'
                f'<p style="{S_FOOTNOTE_HEAD}">Sources</p>'
                f'{items}')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Inline citation markers written by the product writer into structured fields
# (e.g. "1500 ft AGL[3]", "Marginal[4]"). These leak into every cell when the
# writer drops a citation index alongside the value instead of attaching it as
# `_provenance[<field>]`. The renderer-driven path emits its own bracketed
# footnote markers via FootnoteRegistry (rendered in FOOTNOTE_BLUE outside of
# _esc); the inline writer-emitted markers must be scrubbed before HTML
# escaping so they never reach the analyst. Pattern: optional whitespace
# before, single `[<digits>]` or `[<digits>, <digits>, ...]`, end of string
# or immediately followed by punctuation/whitespace. Anchored to the END of
# a text node (the leak is always trailing on the value) to avoid stripping
# legitimate bracketed numbers mid-value (data identifiers like "T-DA0601[A]").
_CITATION_BRACKET_RE = _fn_re.compile(
    r"\s*\[\d+(?:\s*,\s*\d+)*\](?=[\s.,;:)\]’”'\"]|$)"
)

def _strip_inline_citations(s: str) -> str:
    """Remove writer-emitted inline `[N]` / `[N,M]` markers from a text value.
    The renderer's own footnote markers are inserted OUTSIDE _esc via
    _gap_or(value, marker), so they survive this scrub. Applied universally
    inside _esc so every text path (cells, paragraphs, labels, captions)
    benefits without touching each emission site."""
    if not s: return s
    # Iterate to handle adjacent markers like "value[3][4]" — single pass
    # leaves the second marker because the lookahead requires a non-`[` after.
    prev = None
    while prev != s:
        prev = s
        s = _CITATION_BRACKET_RE.sub("", s)
    return s

def _esc(v) -> str:
    if v is None or v == "": return ""
    if isinstance(v, bool): return "true" if v else "false"
    # Unwrap single-element list / tuple — schemas occasionally emit enum
    # values as one-item lists (e.g. `["munition_type_restriction"]`); the
    # bare `str([...])` would otherwise produce the literal `['foo']` repr
    # leaking brackets and quotes into the cell. Multi-element lists join
    # with comma-space so they read as a value list, not a Python repr.
    if isinstance(v, (list, tuple)):
        if len(v) == 0: return ""
        if len(v) == 1: return _esc(v[0])
        return ", ".join(_esc(x) for x in v)
    return html.escape(_strip_inline_citations(str(v)))

def _gap_or(v, footnote_html: str = "") -> str:
    # Required-but-missing analyst-input placeholder bypasses HTML escaping so
    # the red [ANALYST INPUT REQUIRED] badge renders correctly. Sentinel value
    # comes from _render_header_block when a required header field is empty.
    if _is_analyst_placeholder(v):
        return _ANALYST_PLACEHOLDER_HTML
    s = _esc(v)
    if not s:
        return f'<span style="{S_GAP}">—</span>'
    return s + (footnote_html or "")

def _provenance_for(parent: Optional[dict], field: str) -> str:
    """Pull row/dict-level _provenance[field] when present.

    Two shapes accepted:
    - dict: `_provenance: {"<field>": "<citation>", ...}` (canonical, per-field).
    - str:  `_provenance: "<row-level citation>"` (string fallback — applies to
            every field in the row; registry dedupes so all markers point to
            one footnote number).
    """
    if not isinstance(parent, dict): return ""
    prov = parent.get("_provenance")
    if isinstance(prov, dict):
        # Per-field shape only. A single-entry dict like `{"assessment": "..."}`
        # cites assessment, not every cell — the writer chose that field
        # deliberately and the renderer must respect it.
        val = prov.get(field)
        return val or ""
    if isinstance(prov, str):
        return prov
    return ""

def _cell_html(value, parent: Optional[dict], field: str,
                footnotes: Optional['FootnoteRegistry']) -> str:
    """Cell-value HTML with optional provenance footnote marker."""
    marker = ""
    if footnotes is not None and parent is not None:
        cite = _provenance_for(parent, field)
        if cite: marker = footnotes.add(cite)
    return _gap_or(value, marker)

_ACRONYMS = {
    # Generic IPB / targeting
    "mgrs", "dtg", "coa", "ao", "aoi", "aor", "bda", "fda", "pda", "tsda", "mea",
    "cda", "hpt", "hvt", "hvi", "tle", "ada", "c2", "fs", "cema", "ew", "uas",
    "isr", "cas", "nvg", "oakoc", "pir", "tva", "fmcoa", "mlcoa", "mdcoa",
    "nstr", "rtl", "nsl", "tst", "agm", "tsm", "icsm", "Threat COA Statement",
    "ascope", "pmesii", "mcoo", "wem", "tem", "cca", "tmp", "intsum",
    "grintsum", "sitemp", "sitrep", "carver", "hptl", "hvtl", "frago", "opord",
    "pdpa", "fdpa", "tsdpa", "id", "swo", "nai", "tai", "loc", "los", "qrf",
    "mez", "fez", "fscm", "fscl", "rfl", "nfa", "fom", "sam", "io", "ecm",
    "emcon", "joac", "itoa", "oob", "cbrn", "jdpi", "lrm", "scada", "atp",
    "fm", "jp", "atgm", "ifv", "apc", "mbt", "rpg", "ied", "dpe",
    # Intelligence disciplines
    "sigint", "humint", "geoint", "imint", "masint", "osint", "techint",
    "medint", "cyint", "acint", "finint", "elint", "comint",
    # Org / location shorthands
    "ngo", "un", "ihl", "loac", "unesco", "icrc", "nato", "eu",
    # Echelons
    "hq", "bn", "bde", "regt", "div", "corps", "bty", "plt", "co", "det",
    "btr",
    # Staff cell shorthands
    "g2", "g3", "g4", "g5", "g6", "g7", "g8", "g9",
    "s2", "s3", "s4", "s6",
}

# Stop words: keep lowercase mid-phrase (still capitalized when first word).
_STOP_WORDS = {
    "and", "or", "of", "the", "to", "in", "on", "for", "with", "by", "at",
    "a", "an", "vs", "via", "per", "but", "as",
}

def _humanize(field: str) -> str:
    """snake_case / kebab-with-acronym-prefix → display form.

    Rules:
      - Split on `_` then whitespace; within each token, also process
        hyphen-glued segments (so `NGO-facility` runs through token logic).
      - Already-uppercase ≥2-letter alpha tokens preserved (UNESCO, ICRC, NGO).
      - Tokens whose lowercase form is in `_ACRONYMS` uppercased (coa → COA).
      - Stop words (and, or, of, for, …) stay lowercase mid-phrase but are
        title-cased when they are the first word of the output.
      - Mixed alphanumeric tokens like `c2`, `c130` get a digit-letter
        boundary split: `c2` → `C2`, `c130` → `C-130`.
      - Pure-numeric and pure-alphabetic tokens fall through to title-case.

    Examples:
      `coa_id` → "COA ID"
      `command_and_control` → "Command and Control"
      `damage_category_for_armored_vehicles` → "Damage Category for Armored Vehicles"
      `c2_node` → "C2 Node"
      `airborne_operations_c130_c17` → "Airborne Operations C-130 C-17"
      `NGO-facility` → "NGO Facility"
      `cultural-heritage` → "Cultural Heritage"
      `UNESCO` → "UNESCO"
      `IHL_LOAC` → "IHL LOAC"
    """
    import re

    def _split_digit_letter(s: str) -> list:
        """Split where a digit-run meets a letter-run (in either direction).
        'c130' → ['c', '130']; 't72b' → ['t', '72', 'b']; 'c2' → ['c', '2'].
        Pure-alpha / pure-digit returned as single-element list."""
        if s.isalpha() or s.isdigit():
            return [s]
        return [p for p in re.split(r'(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)', s) if p]

    def _format_mixed(s: str) -> str:
        """Format hyphen-free mixed alphanumeric token (e.g., 'c130' → 'C-130',
        'c2' → 'C2', '155mm' → '155mm'). Identifier-shaped tokens where the
        leading alpha is a known weapon-system class letter (1-2 letters
        followed by digits) get dash-joined; other shapes keep the original
        casing with first-letter uppercased."""
        parts = _split_digit_letter(s)
        if len(parts) == 1:
            return s
        # If the token starts with alpha and the first alpha run is 1-2 chars,
        # treat as weapon-system identifier (T-72, C-130, AH-64). Otherwise
        # keep compact (C2, MQ1, 155mm).
        if parts[0].isalpha() and len(parts[0]) <= 2 and len(parts) >= 2 and parts[1].isdigit():
            # Weapon-system shape — dash-join. Uppercase alpha runs.
            return "-".join(p.upper() if p.isalpha() else p for p in parts)
        # Compact identifier (C2, MQ1, 155mm). Uppercase leading alpha if it
        # is the very first run; leave trailing alpha lowercase ('155mm'
        # keeps 'mm' lower; 'c2' → 'C2').
        out_parts = []
        for i, p in enumerate(parts):
            if p.isalpha():
                # Leading alpha → uppercase; trailing alpha after digits keeps lowercase
                if i == 0:
                    out_parts.append(p.upper())
                else:
                    out_parts.append(p.lower())
            else:
                out_parts.append(p)
        return "".join(out_parts)

    def _token(s: str, *, is_first: bool) -> str:
        if not s:
            return s
        # Already-uppercase ≥2-char alpha acronym (UNESCO, ICRC, NGO): preserve.
        if len(s) >= 2 and s.isalpha() and s.isupper():
            return s
        # Already-formatted identifier: if the token already contains an
        # uppercase letter and a digit, treat as data (KG id, weapon
        # designator, MGRS suffix) and preserve verbatim. Catches `T-DA0601`,
        # `SA-15b`, `I-EW-281430Z`, etc. that flow through _humanize from
        # data paths and must not be re-cased.
        if any(c.isupper() for c in s) and any(c.isdigit() for c in s):
            return s
        low = s.lower()
        # Acronym table — uppercase
        if low in _ACRONYMS:
            return s.upper()
        # Stop word — lowercase mid-phrase, title-case if first
        if low in _STOP_WORDS:
            return s.title() if is_first else low
        # Mixed alphanumeric like 'c2', 'c130' (lowercase incoming)
        if not s.isalpha() and not s.isdigit():
            return _format_mixed(s)
        # Pure alpha — title case
        if s.isalpha():
            return s.title()
        # Pure digit / other
        return s

    # Split on underscore + whitespace into top-level tokens.
    parts = field.replace("_", " ").split()
    if not parts:
        return field
    out = []
    first_emitted = False
    for p in parts:
        if "-" in p:
            # Determine: identifier-like (any segment contains digit) vs
            # kebab-phrase (all alpha). Identifier-like keeps hyphens
            # ('SA-15b' / 'COA-1'); kebab-phrase becomes space-joined
            # ('NGO-facility' → 'NGO Facility').
            segments = [s for s in p.split("-") if s != ""]
            any_digit = any(any(c.isdigit() for c in seg) for seg in segments)
            rendered = []
            for i, seg in enumerate(segments):
                rendered.append(_token(seg, is_first=(not first_emitted and i == 0)))
                if not first_emitted:
                    first_emitted = True
            out.append(("-" if any_digit else " ").join(rendered))
        else:
            out.append(_token(p, is_first=not first_emitted))
            first_emitted = True
    return " ".join(out)

def _label_for_field(field: str, spec: dict) -> str:
    """Render label override > auto humanize."""
    if spec and isinstance(spec, dict) and spec.get("render_label"):
        return spec["render_label"]
    return _humanize(field)

# ---------------------------------------------------------------------------
# Fragment renderers
# ---------------------------------------------------------------------------
def _kv_table(items: list, parent: Optional[dict] = None,
               footnotes: Optional['FootnoteRegistry'] = None) -> str:
    """2-col label/value table. When `parent` + `footnotes` are passed and an
    item triple (label, value, field_name) is supplied, provenance is looked up
    and rendered as a superscript footnote. Two-tuple items render without
    footnoting (legacy callers + simple kv blocks)."""
    rows = []
    for it in items:
        if len(it) == 3:
            label, value, field = it
            cell = _cell_html(value, parent, field, footnotes)
        else:
            label, value = it
            cell = _gap_or(value)
        # KV labels run through _strip_doctrine_refs so schema render_labels
        # like "Preparing Unit (G-9 lead per para 1-31)" don't leak doctrine
        # refs into header rows. Section-bar paths apply the same scrub.
        rows.append(f'<tr><th style="{S_TH_LABEL}">{_esc(_strip_doctrine_refs(str(label)))}</th>'
                    f'<td style="{S_TD_VALUE}">{cell}</td></tr>')
    return f'<table style="{S_TABLE}">{"".join(rows)}</table>'

# Trailing doctrinal parentheticals that schemas / writers sometimes bake into
# `render_label` or section titles. The doctrinal basis is conveyed by the
# product type itself; carrying "(Per Fig 4-17)" / "(ATP 2-01.3 …)" in every
# visible header reads as redundant chrome. Pattern: an opening paren followed
# by one of the doctrinal lead-ins, captured up to the matching close-paren at
# the end of the heading. Match is case-insensitive (CSS uppercases the bar).
_DOCTRINE_PAREN_RE = _fn_re.compile(
    r"\s*\(\s*(?:per\s+)?"  # optional "Per " lead-in
    r"(?:"
    r"fig(?:ure)?\b|"        # "Fig", "Figure"
    r"tbl|table\b|"          # tables
    r"atp\b|fm\b|jp\b|"      # publication identifiers
    r"adp\b|tc\b|tm\b|ar\b|"  # more pubs
    r"ch\b|chap\b|chapter\b|" # chapter refs
    r"para\b|paragraph\b|"   # paragraph refs
    r"app\b|appendix\b"      # appendix
    r")[^()]*\)\s*$",
    _fn_re.IGNORECASE,
)
# Suffix doctrinal refs without parens (e.g. "Operations — Per Fig 4-17").
_DOCTRINE_SUFFIX_RE = _fn_re.compile(
    r"\s*[–—\-:;,]\s*(?:per\s+)?"
    r"(?:fig(?:ure)?|tbl|table|atp|fm|jp|adp|tc|tm|ar|ch|chap|chapter|"
    r"para|paragraph|app|appendix)\b[^()]*$",
    _fn_re.IGNORECASE,
)

def _strip_doctrine_refs(s: str) -> str:
    """Remove trailing doctrinal-ref parentheticals/suffixes from a heading.
    Operates idempotently on the visible heading text. Applied universally
    inside _filled_bar_html (which renders every top-level section bar) so
    per-product schemas don't have to repeat the scrub."""
    if not s: return s
    prev = None
    while prev != s:
        prev = s
        s = _DOCTRINE_PAREN_RE.sub("", s)
        s = _DOCTRINE_SUFFIX_RE.sub("", s)
    return s.rstrip()

def _filled_bar_html(text: str, fill: str = "#2c3e50", text_color: str = "#FFFFFF",
                       font_size: str = "12pt", border_left: str = "",
                       inner_html: Optional[str] = None) -> str:
    """Render a filled section bar as a single-cell, single-row table. We use
    a table (not an H2 with paragraph shading) because LibreOffice / soffice
    headless conversion renders paragraph shading inconsistently once the
    document already contains multiple body tables — table cell shading
    renders reliably in both soffice and Word. Word renders both the same.

    The cell carries the full fill + bold uppercase white text + optional
    left accent bar; sits flush against the body content above/below.

    `inner_html` overrides the auto-escaped text when the caller needs to
    embed pre-built HTML (e.g. inline COA ML/MD badges). The escaping path
    via `text` is the default and safe path.
    """
    bl_style = (f"border-left:{border_left};" if border_left else "")
    # NOTE: do NOT put `width:100%` on the TD. convert.js parses CSS percent
    # widths through docx-npm's CT_TblWidth which then formats them with a
    # leading multiplier (size × 100). HTML 100% → TC width 10000 = 200% per
    # OOXML scale, which LibreOffice headless treats as out-of-range and
    # drops the cell shading. The parent <table style="width:100%"> already
    # stretches the cell; cell width must stay auto.
    cell_style = (
        f"background-color:{fill};color:{text_color};font-weight:bold;"
        f"font-size:{font_size};text-transform:uppercase;letter-spacing:0.5pt;"
        f"padding:6pt 10pt;border:0.5pt solid #1a252f;{bl_style}"
    )
    # `text` path: route through _strip_doctrine_refs so schemas / writers
    # that bake doctrinal refs into render_label don't leak them onto the
    # visible bar. `inner_html` is pre-built (already includes badges, COA
    # ids, etc.) — caller is responsible for keeping that path clean.
    content = inner_html if inner_html is not None else _esc(_strip_doctrine_refs(text))
    return (
        f'<table style="border-collapse:collapse;width:100%;'
        f'margin-top:20pt;margin-bottom:10pt;page-break-inside:avoid">'
        f'<tr><td style="{cell_style}">{content}</td></tr></table>'
    )

def _section_heading(text: str) -> str:
    """Top-level section heading inside a product body — HTML/DOCX path.

    Per AR 25-50 / AR 25-30 backbone (filtered for analytical products),
    section headings are plain ALL CAPS bold body-size text, NOT filled
    color bars. Army doctrine uses weight + case to convey hierarchy, not
    color fills or size jumps. PPTX still uses `_filled_bar_html` for slide
    chrome — only the HTML/DOCX render path emits inline <h2>.

    Format: ALL CAPS bold, label as-is. INTSUM paragraph labels already
    carry the leading number ("1. WEATHER", "2. SITUATION HIGHLIGHTS").
    Bare labels (e.g. "Analyst Workpaper", "Report Lines") render unnumbered.
    No trailing period — these are standalone headings, not sentences."""
    raw = _strip_doctrine_refs(text or "")
    label = raw.strip().upper()
    # NOTE: hex colors MUST be 6-digit (#000000, not #000). convert.js's
    # color parser rejects 3-digit shorthand with "Invalid hex value '000'.
    # Expected 6 digit hex value" and the whole DOCX conversion fails.
    return (
        f'<h2 style="font-family:\'Times New Roman\',serif;font-size:12pt;'
        f'font-weight:bold;color:#000000;text-transform:uppercase;'
        f'background:none;border:0;margin-top:18pt;margin-bottom:8pt">'
        f'{_esc(label)}</h2>'
    )


# ---------------------------------------------------------------------------
# Page-break marker honored by DocEditor.tsx::updatePageLayout.
# Splitter recognizes elements with class `pytho-page-break` and expands
# their height to fill the rest of the current page so the next real block
# starts at the next page-height boundary. Aspect-locked to the slide /
# Letter geometry — never a fractional page split.
# ---------------------------------------------------------------------------
def _page_break_html() -> str:
    return ('<div class="pytho-page-break" aria-hidden="true" '
            'contenteditable="false" '
            'style="display:block;height:1px;margin:0;padding:0;border:0;'
            'background:transparent"></div>')


# ---------------------------------------------------------------------------
# Classification banner — top + bottom of the rendered artifact. Filled
# color strip (DoDM 5200.01 palette: UNCLASSIFIED green, CUI purple,
# SECRET red, ...). Mirrors render_artifact_pptx::_classification_strip,
# but emitted as bare HTML inside the body so DocEditor + DOCX share the
# same source-of-truth styling.
# ---------------------------------------------------------------------------
def _classification_banner_html(classification: str) -> str:
    swatch = _classification_colors(classification)
    label = (classification or "UNCLASSIFIED").upper()
    # The class `ibl-classification-banner` is the contract with convert.js
    # — the DOCX converter strips any element carrying it so the body
    # banner stops duplicating the page-header banner Word stamps on every
    # page. The HTML preview in DocEditor (TinyMCE) keeps the banner so
    # the analyst sees classification chrome above the artifact body.
    return (
        f'<table class="ibl-classification-banner" '
        f'style="border-collapse:collapse;width:100%;'
        f'margin:0 0 14pt 0;page-break-inside:avoid">'
        f'<tr><td style="background-color:{swatch["fill"]};'
        f'color:{swatch["text"]};font-weight:bold;font-size:13pt;'
        f'letter-spacing:0.6pt;text-align:center;padding:5pt 8pt;'
        f'border:0">{_esc(label)}</td></tr></table>'
    )


# ---------------------------------------------------------------------------
# Compact metadata band — horizontal strip below the top classification
# banner with the salient header values (DTG, Phase, AOR, Threat COA,
# preparing unit, etc.). Mirrors render_artifact_pptx::_meta_band but
# emitted as a single-row HTML table so the DocEditor preview, DOCX, and
# PPTX-via-HTML all consume the same markup. Skipped (returns "") when
# fewer than two substantive entries would render — a band carrying only
# one field is visual noise.
# ---------------------------------------------------------------------------
def _meta_band_emitted_keys(header: dict, schema: dict) -> set:
    """Return the set of header keys the meta band ribbon emits, so the full
    kv table below can skip them and avoid double-rendering. The set is the
    fields actually displayed (skipped-blanks + ao-in-coa dedupe applied)."""
    band = _meta_band_html(header, schema, _capture_keys=True)
    if isinstance(band, tuple):
        return band[1]
    return set()


def _meta_band_html(header: dict, schema: dict, _capture_keys: bool = False):
    if not isinstance(header, dict):
        return ("", set()) if _capture_keys else ""
    header_specs = schema.get("header_fields") or {}
    order_keys = list(header_specs.keys()) + [
        k for k in header.keys() if k not in header_specs
    ]
    preferred = [
        "dtg", "engagement_dtg", "reporting_dtg",
        "phase", "phase_or_critical_event", "area_of_operations",
        "threat_coa", "target_number", "target_description",
        "target_location_mgrs", "preparing_unit",
        "approving_commander", "reporting_agency",
        "sitemp_artifact_id", "threat_overlay_artifact_id",
        "threat_coa_statement_artifact_id",
        "hvtl_artifact_id", "carver_artifact_id", "hptl_artifact_id",
        "tmp_artifact_id",
    ]
    ordered = [k for k in preferred if k in order_keys]
    ordered += [k for k in order_keys if k not in ordered]
    # TODO(exp-08): retire compact_labels entirely. Export-time fuzzy-string
    # renaming is fragile (any new label needs another entry; misses are
    # silent). The right fix is to bake analyst-facing labels into each
    # template's schema (render_label) so the exporter never has to rewrite.
    # Also: the "Pre-strike JDPI Reference (TMP)" key bakes a parenthetical
    # product-acronym into the comparison key — schema cleanup eliminates
    # that class of leak. Tracking: deferred until the schema-label cleanup
    # wave per user direction ("too big of a refactor [right now]").
    compact_labels = {
        "Phase or Critical Event": "Phase",
        "Area of Operations": "AOR",
        "Preparing Unit": "Prep",
        "Approving Commander": "Approver",
        "Threat COA": "COA",
        "SITEMP Reference": "SITEMP",
        "Threat Overlay Reference": "Threat Overlay",
        "Threat COA Statement Reference": "Threat COA Statement",
        "HVTL Reference": "HVTL",
        "CARVER Reference": "CARVER",
        "HPTL Reference": "HPTL",
        "Pre-strike JDPI Reference (TMP)": "TMP",
        "Mensuration DTG": "DTG",
        "Engagement DTG (when struck)": "Engaged",
        "Acquisition DTG": "Acq",
        "Reporting DTG": "DTG",
        "Target Number": "TGT",
        "Target Description": "TGT",
        "Target Location (MGRS)": "MGRS",
        "Reporting Agency": "Rep",
        "Assessing Analyst": "Analyst",
    }
    coa_text = str(header.get("threat_coa") or "").lower()
    ao_text = str(header.get("area_of_operations") or "").strip().lower()
    ao_in_coa = bool(ao_text) and ao_text in coa_text
    parts = []
    emitted_keys = set()
    seen = set()
    for k in ordered:
        if k in _HEADER_EXCLUDE_KEYS or (k or "").startswith("_"):
            continue
        if k == "area_of_operations" and ao_in_coa:
            continue
        if (k or "").startswith("spatial_overlay"):
            continue
        v = header.get(k)
        if v in (None, ""):
            continue
        spec = header_specs.get(k) or {}
        lbl = (spec.get("render_label") or _humanize(k)).strip()
        lbl_compact = compact_labels.get(lbl, lbl)
        key = (lbl_compact.lower(), str(v).strip())
        if key in seen:
            continue
        seen.add(key)
        emitted_keys.add(k)
        parts.append((lbl_compact, str(v)))
    if len(parts) < 2:
        return ("", set()) if _capture_keys else ""
    inner = []
    for i, (lbl, val) in enumerate(parts):
        if i:
            inner.append('<span style="color:#888888;padding:0 8pt">|</span>')
        inner.append(
            f'<span style="font-weight:bold;color:#000000">{_esc(lbl)}: </span>'
            f'<span style="color:#000000">{_esc(val)}</span>'
        )
    # Class `ibl-meta-band` is the contract with convert.js — the DOCX
    # converter strips elements carrying it so the compact pipe-joined
    # strip does not flatten into an unreadable wall of text in Word.
    # The HTML preview in DocEditor keeps it as the compact at-a-glance
    # header chrome; DOCX renders the full kv table below for legibility.
    html_out = (
        f'<table class="ibl-meta-band" '
        f'style="border-collapse:collapse;width:100%;'
        f'margin:0 0 14pt 0;page-break-inside:avoid">'
        f'<tr><td style="background-color:#eeeeee;color:#000000;'
        f'font-size:10pt;text-align:center;padding:5pt 10pt;'
        f'border:0">{"".join(inner)}</td></tr></table>'
    )
    return (html_out, emitted_keys) if _capture_keys else html_out

_ANALYST_PLACEHOLDER_SENTINEL = "​__ANALYST_INPUT__​"
_ANALYST_PLACEHOLDER_HTML = (
    '<span style="color:#b00020;font-style:italic;font-weight:bold;'
    'background-color:#fdecea;padding:1pt 4pt">'
    '[ANALYST INPUT REQUIRED]</span>'
)
def _is_analyst_placeholder(v) -> bool:
    return isinstance(v, str) and v == _ANALYST_PLACEHOLDER_SENTINEL

def _render_header_block(header: dict, schema: dict,
                           footnotes: Optional['FootnoteRegistry'] = None,
                           artifact_id: str = "",
                           user_id: str = "",
                           skip_keys: Optional[set] = None) -> str:
    """Use header_fields metadata for ordering + label override; never include
    classification (page header carries it). Skip keys starting with `_` so
    `_provenance` does not leak into the rendered header. When footnotes are
    passed, header field provenance threads through as superscript markers.

    Required header fields that are missing render as a visible
    [ANALYST INPUT REQUIRED] placeholder — silently dropping required-but-empty
    rows hid scope gaps from the analyst review. Optional fields stay omitted.

    When the header carries a spatial overlay (`spatial_overlay_url` or
    `spatial_overlay_map_id` for the mock path), append a TinyMCE-friendly
    map figure with an Edit map button. The DocEditor wraps a click handler
    around `.ibl-edit-map` and deep-links into the OperationsMap page."""
    header_specs = schema.get("header_fields") or {}
    skip_keys = skip_keys or set()
    items = []
    # Combine paired DTG fields into a single row when the schema declares
    # both — e.g. INTSUM's `period_start_dtg` + `period_end_dtg` render as
    # "Reporting Period: 180600Z – 181800Z" instead of two separate rows.
    # Keyed by the FIRST field of the pair so iteration order is preserved.
    _PAIRED_DTG_RANGES = {
        "period_start_dtg":     ("period_end_dtg",     "Reporting Period"),
        "valid_window_start_dtg": ("valid_window_end_dtg", "Valid Window"),
        "reporting_period_start_z": ("reporting_period_end_z", "Reporting Period"),
        "reporting_period_start": ("reporting_period_end", "Reporting Period"),
        "engagement_window_start": ("engagement_window_end", "Engagement Window"),
        # CDE per-target engagement window — drives time-of-day / pattern-of-life
        # collateral-environment inputs (JP 3-60 Ch. III §C para 3.c(6)).
        "engagement_window_start_dtg": ("engagement_window_end_dtg", "Engagement Window"),
    }
    _pair_seen: set = set()

    if header_specs:
        for f, spec in header_specs.items():
            if f in _HEADER_EXCLUDE_KEYS or f.startswith("_"): continue
            # view:"internal" — field exists for cross-product wiring (e.g.
            # WEM/SITEMP/Threat Overlay cross-refs) but should NOT surface in the
            # visible header. Default treats missing view as "header" so
            # existing fields without the key keep rendering.
            if (spec or {}).get("view") not in ("header", None): continue
            # spatial_overlay_* are surfaced as the map figure below, not as
            # plain key/value rows in the header table.
            if f.startswith("spatial_overlay"): continue
            # The compact meta-band ribbon already emitted these fields; skip
            # them here so the same DTG/Phase/AOR/COA don't render twice.
            if f in skip_keys: continue
            # Already-consumed end-of-pair — skip; the start key emitted
            # the combined row.
            if f in _pair_seen: continue
            # Start-of-pair: emit one combined row covering both fields.
            if f in _PAIRED_DTG_RANGES:
                end_key, combined_label = _PAIRED_DTG_RANGES[f]
                if end_key in header_specs:
                    v_start = header.get(f) or ""
                    v_end = header.get(end_key) or ""
                    if v_start or v_end:
                        combined = (
                            f"{v_start} – {v_end}"
                            if (v_start and v_end)
                            else (v_start or v_end)
                        )
                        items.append((combined_label, combined, f))
                        _pair_seen.add(end_key)
                        continue
                    # Both empty + at least one required → single placeholder row.
                    if ((spec or {}).get("required")
                        or (header_specs.get(end_key) or {}).get("required")):
                        items.append((combined_label,
                                       _ANALYST_PLACEHOLDER_SENTINEL, f))
                        _pair_seen.add(end_key)
                        continue
                    _pair_seen.add(end_key)
                    continue
            v = header.get(f)
            if v in (None, ""):
                # Schema default fallback. `default` strings may contain
                # `{header_field}` placeholders (e.g. INTSUM
                # intsum_number default = "INTSUM-{period_start_dtg}-{period_end_dtg}")
                # — substitute against the live header dict. format_map with a guard
                # dict so a missing placeholder leaves the literal in place
                # rather than raising KeyError. If substitution yields a
                # value that's clearly a half-resolved template (e.g. two
                # adjacent hyphens because both period DTGs are empty),
                # fall back to the [ANALYST INPUT REQUIRED] sentinel rather
                # than rendering "INTSUM--".
                spec_default = (spec or {}).get("default")
                if isinstance(spec_default, str) and spec_default:
                    try:
                        class _SafeDict(dict):
                            def __missing__(self, key):
                                return "{" + key + "}"
                        substituted = spec_default.format_map(
                            _SafeDict({k: ("" if val is None else val)
                                       for k, val in header.items()})
                        )
                    except (ValueError, IndexError):
                        substituted = spec_default
                    # Heuristic for half-resolved template: any sequence of
                    # 2+ adjacent separators (e.g. "INTSUM--", "FOO-/-")
                    # means at least one placeholder resolved to empty.
                    looks_unresolved = bool(re.search(r"[-_/]{2,}|^[-_/ ]+|[-_/ ]+$|^\s*$", substituted))
                    if substituted and not looks_unresolved:
                        items.append((_label_for_field(f, spec), substituted, f))
                        continue
                if (spec or {}).get("required"):
                    items.append((_label_for_field(f, spec),
                                   _ANALYST_PLACEHOLDER_SENTINEL, f))
                continue
            items.append((_label_for_field(f, spec), v, f))
        for k, v in header.items():
            if k in _HEADER_EXCLUDE_KEYS or k.startswith("_"): continue
            if k.startswith("spatial_overlay"): continue
            if k in header_specs: continue
            if k in skip_keys: continue
            if v in (None, ""): continue
            items.append((_humanize(k), v, k))
    else:
        items = [(_humanize(k), v, k) for k, v in header.items()
                  if k not in _HEADER_EXCLUDE_KEYS and not k.startswith("_")
                  and not k.startswith("spatial_overlay")
                  and k not in skip_keys
                  and v not in (None, "")]
    # AR 25-50 Figures 2-15/2-16 — header is a stacked label:value block
    # top-left, NOT a 2-col kv table. Each header field renders as its own
    # short paragraph with bold label and inline value. Doctrinal refs in
    # the render_label are scrubbed (matches the previous kv-table behavior).
    # Provenance / footnote markers thread through via _cell_html so header
    # fields with `_provenance` still emit numbered superscript markers.
    #
    # The stacked <p> elements are emitted as flat siblings — NOT inside a
    # <div class="ibl-header-block"> wrapper. convert.js currently has a
    # temporal-dead-zone bug in its <div> branch that references `styleAttr`
    # before it's declared (convert.js:590 inside the `if (hasBlockChild)`
    # path), so a header-wrapping <div> with <p> children explodes the
    # DOCX conversion. Stacked siblings render identically in the browser
    # and round-trip cleanly through convert.js.
    if not items:
        block = ""
    else:
        rows = []
        # First paragraph carries the block's top margin reset; the last
        # paragraph carries the spacing to the first section heading.
        last_idx = len(items) - 1
        for idx, it in enumerate(items):
            if len(it) == 3:
                label, value, field = it
                cell = _cell_html(value, header, field, footnotes)
            else:
                label, value = it
                cell = _gap_or(value)
            scrubbed = _strip_doctrine_refs(str(label))
            bottom_margin = "14pt" if idx == last_idx else "4pt"
            rows.append(
                f'<p class="ibl-header-line" '
                f'style="margin:0 0 {bottom_margin} 0;text-align:left">'
                f'<strong>{_esc(scrubbed)}:</strong> {cell}'
                f'</p>'
            )
        block = "".join(rows)
    # Map is rendered as a SEPARATE top-level block (see render_artifact_html),
    # not inside the header section. The header is the stacked summary for
    # slide 1; the map gets its own slide via the page-split push. Earlier
    # wiring concatenated `kv + map_embed` here, forcing the map onto the
    # same slide as the header table.
    return block

def _published_field_list(schema: dict) -> list:
    """Schema-tagged published row fields, in row_fields key order."""
    out = []
    for f, spec in (schema.get("row_fields") or {}).items():
        if (spec or {}).get("view", "published") == "published":
            out.append(f)
    return out

def _workpaper_field_list(schema: dict) -> list:
    return [f for f, spec in (schema.get("row_fields") or {}).items()
             if (spec or {}).get("view") == "workpaper"]

def _render_published_table(rows: list, schema: dict,
                              footnotes: Optional['FootnoteRegistry'] = None,
                              orientation: str = "portrait") -> str:
    """Wide table: schema's published row_fields → columns. Group banding by
    row_fields[group_by] when schema declares it. Uses render_label / enum
    metadata for column headers. Each cell with row._provenance[field] gets a
    superscript footnote marker keyed to the doc's footnote registry."""
    if not rows:
        return f'<p style="{S_GAP}">No rows.</p>'
    fields = _published_field_list(schema)
    if not fields:
        return _render_rows_as_kv_blocks(rows, schema, footnotes=footnotes)
    row_specs = schema.get("row_fields") or {}
    keep = [f for f in fields
             if any(r.get(f) not in (None, "", False) for r in rows)]
    if not keep: keep = fields

    def _row_cells(r):
        cells = []
        for f in keep:
            raw = r.get(f)
            val = _humanize_enum_value(raw, row_specs.get(f, {}))
            fill = _cell_fill_for(raw, row_specs.get(f, {}))
            cells.append(f'<td style="{S_TD}{fill}">{_cell_html(val, r, f, footnotes)}</td>')
        return cells

    col_tuples = [(f, _label_for_field(f, row_specs.get(f, {})), row_specs.get(f, {}))
                   for f in keep]
    dxa = _column_width_dxa(col_tuples, orientation)
    head = "".join(
        f'<th style="{S_TH_HEADER_ROW};width:{w}pt">{_esc(_strip_doctrine_refs(lbl))}</th>'
        for (_, lbl, _), w in zip(col_tuples, dxa)
    )
    group_by = schema.get("group_by")
    body_rows = []
    if group_by and group_by in row_specs:
        group_spec = row_specs.get(group_by) or {}
        # Optional schema directives for the published-table grouping. HVTL
        # uses these to collapse the FM 3-60 App. C target categories into
        # ADP 3-0 warfighting functions per ATP 2-01.3 Fig 6-9 — the row
        # data still carries the canonical FM 3-60 enum, but the published
        # group label and bucket assignment use the warfighting-function
        # roll-up. Other products without these directives keep their
        # category-level grouping unchanged.
        group_collapse_map = schema.get("group_collapse_map") or {}
        group_collapse_order = schema.get("group_collapse_order") or []
        group_collapse_labels = schema.get("group_collapse_labels") or {}
        group_collapse_label_override = schema.get("group_label_override")

        def _group_key(category_value):
            if not category_value:
                return None
            if group_collapse_map:
                return group_collapse_map.get(category_value, None)
            return category_value

        def _group_label(key):
            if group_collapse_labels and key in group_collapse_labels:
                return group_collapse_labels[key]
            if key == "other":
                return "Other"
            if key == "(unspecified)":
                return "(Unspecified)"
            humanized = _humanize_enum_value(str(key), group_spec)
            if humanized == str(key):
                humanized = _humanize(str(key))
            return humanized

        buckets = {}
        for r in rows:
            cat = r.get(group_by)
            gk = _group_key(cat)
            if gk is None and cat:
                # Category present but didn't match the collapse map —
                # surface it under an "Other" bucket so the row isn't
                # silently dropped. Renders an Other group at the bottom.
                gk = "other"
            if gk is None:
                gk = "(unspecified)"
            buckets.setdefault(gk, []).append(r)

        if group_collapse_order:
            order = [k for k in group_collapse_order if k in buckets]
            for k in buckets.keys():
                if k not in order:
                    order.append(k)
        else:
            order = list(buckets.keys())

        # Doctrinal 2-col grouped-bullet layout (ATP 2-01.3 Fig 6-9):
        # when group_by is declared AND there's exactly ONE published
        # row_field, render as a 2-col table — group label in column 1,
        # bulleted list of the published-field values in column 2. Each
        # group is ONE row, not a section banner over per-HVT rows.
        if len(keep) == 1:
            value_field = keep[0]
            value_spec = row_specs.get(value_field, {})
            value_label = _label_for_field(value_field, value_spec)
            group_label_text = group_collapse_label_override or \
                _label_for_field(group_by, group_spec)
            head = (
                f'<th style="{S_TH_HEADER_ROW};width:25%">{_esc(_strip_doctrine_refs(group_label_text))}</th>'
                f'<th style="{S_TH_HEADER_ROW};width:75%">{_esc(_strip_doctrine_refs(value_label))}</th>'
            )
            # ATP 2-01.3 Fig 6-9 published HVTL has no inline footnote
            # markers on the HVT name bullets — provenance still tracks
            # via the schema_state but doesn't decorate the doctrinal
            # 2-col layout. Pass footnotes=None for the bullet cells.
            for key in order:
                label = _group_label(key)
                items_html = "".join(
                    f"<li>{_cell_html(_humanize_enum_value(r.get(value_field), value_spec), r, value_field, None)}</li>"
                    for r in buckets[key]
                    if r.get(value_field) not in (None, "", False)
                )
                if not items_html:
                    continue
                body_rows.append(
                    f'<tr>'
                    f'<td style="{S_TD};vertical-align:top;font-weight:bold">'
                    f'{_esc(_strip_doctrine_refs(label))}</td>'
                    f'<td style="{S_TD};vertical-align:top">'
                    f'<ul style="margin:0;padding-left:18pt">{items_html}</ul></td>'
                    f'</tr>'
                )
        else:
            # Multi-published-column case keeps the section-banner layout:
            # group header row spans all columns, then one HVT row per
            # entry beneath it. Used by products with wider published
            # views (HPTL priority cols, AGM matrix, etc.).
            for key in order:
                label = _humanize_enum_value(str(key), group_spec)
                if label == str(key):
                    label = _humanize(str(key))
                body_rows.append(
                    f'<tr><th colspan="{len(keep)}" style="{S_TH_HEADER_ROW};text-transform:uppercase;letter-spacing:0.3pt">{_esc(_strip_doctrine_refs(label))}</th></tr>'
                )
                for r in buckets[key]:
                    body_rows.append("<tr>" + "".join(_row_cells(r)) + "</tr>")
    else:
        for r in rows:
            body_rows.append("<tr>" + "".join(_row_cells(r)) + "</tr>")
    return (f'<table style="{S_TABLE}">'
            f'<thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>')

def _render_rows_as_kv_blocks(rows: list, schema: dict,
                                footnotes: Optional['FootnoteRegistry'] = None) -> str:
    """Card-per-row when no published row_fields exist (or fallback)."""
    if not rows: return f'<p style="{S_GAP}">No rows.</p>'
    row_specs = schema.get("row_fields") or {}
    out = []
    for r in rows:
        rid = r.get("id") or r.get("factor") or ""
        name = r.get("name") or r.get("identity") or ""
        title = rid + (f" — {name}" if name and name != rid else "")
        if title:
            out.append(f'<h3 style="{S_H3}">{_esc(title)}</h3>')
        items = []
        for f, spec in row_specs.items():
            if f in ("id", "name", "identity", "factor"): continue
            v = r.get(f)
            if v in (None, "", False): continue
            items.append((_label_for_field(f, spec), v, f))
        if not items:
            for k, v in r.items():
                if k.startswith("_") or k in ("id", "name", "identity", "factor"):
                    continue
                if v in (None, "", False): continue
                items.append((_humanize(k), v, k))
        if items:
            out.append(_kv_table(items, parent=r, footnotes=footnotes))
    return "".join(out)

def _render_workpaper_appendix(rows: list, schema: dict,
                                 footnotes: Optional['FootnoteRegistry'] = None) -> str:
    """Workpaper view appendix only (analyst working notes that didn't make
    the published view). Provenance is now rendered via inline footnote markers
    + a numbered Sources section, NOT as a separate appendix table."""
    if not rows: return ""
    workpaper = _workpaper_field_list(schema)
    if not workpaper: return ""
    row_specs = schema.get("row_fields") or {}
    out = [_section_heading("Analyst Workpaper")]
    rendered_any = False
    for r in rows:
        rid = r.get("id") or r.get("factor") or ""
        name = r.get("name") or r.get("identity") or ""
        title = rid + (f" — {name}" if name and name != rid else "")
        items = []
        for f in workpaper:
            v = r.get(f)
            if v in (None, "", False): continue
            items.append((_label_for_field(f, row_specs.get(f, {})), v, f))
        if not items: continue
        out.append(f'<h3 style="{S_H3}">{_esc(title)}</h3>')
        out.append(_kv_table(items, parent=r, footnotes=footnotes))
        rendered_any = True
    return "".join(out) if rendered_any else ""

# ---------------------------------------------------------------------------
# Paragraph rendering — driven by paragraph_fields entries
# ---------------------------------------------------------------------------
def _list_of_dicts_has_nested_list(item_subfields: list) -> bool:
    """Detect the 'sectioned' pattern: any item subfield is itself shape='list_of_dicts'
    or shape='dict' (has a sub structure that doesn't fit in a cell)."""
    if not item_subfields: return False
    for f in item_subfields:
        if isinstance(f, dict):
            if f.get("shape") in ("list_of_dicts", "dict"):
                return True
    return False

def _list_of_dicts_sectioned(value: list, item_subfields: list,
                                footnotes: Optional['FootnoteRegistry'] = None,
                                orientation: str = "portrait",
                                level: int = 3) -> str:
    """Hierarchical render: each item gets its own h3 (titled by the first scalar
    subfield's value), with scalar fields as a kv table + each list_of_dicts /
    dict subfield rendered as a sibling table or sub-paragraph. Used when an
    item has a nested list_of_dicts subfield (e.g., WEM operations[*].parameters)."""
    if not value: return f'<p style="{S_GAP}">—</p>'
    # Sectioned items render as a single-cell filled-bar table. At level >= 3
    # use a light-gray fill with a left accent bar (subsection styling); at
    # level <= 2 use the dark fill (top-level styling).
    def _make_section_bar(inner_html: str) -> str:
        if level >= 3:
            return _filled_bar_html("", fill="#e8eaed",
                                      text_color="#000000", font_size="10.5pt",
                                      border_left="3pt solid #2c3e50",
                                      inner_html=inner_html)
        return _filled_bar_html("", fill="#2c3e50",
                                  text_color="#FFFFFF", font_size="12pt",
                                  inner_html=inner_html)
    out = []
    title_field = None
    body_subfields = []
    scalar_subfields = []
    for f in item_subfields:
        if not isinstance(f, dict): continue
        shape = f.get("shape")
        if shape in ("list_of_dicts", "dict"):
            body_subfields.append(f)
        else:
            scalar_subfields.append(f)
            if title_field is None:
                title_field = f
    # Items that carry body subfields (list_of_dicts / dict children) are
    # major sections — separate consecutive sections with a thin horizontal
    # rule so COA-1 vs COA-2, or operation-A vs operation-B, read as
    # distinct blocks.
    major_section = bool(body_subfields)
    for idx, it in enumerate(value):
        if not isinstance(it, dict):
            out.append(f"<p>{_esc(str(it))}</p>")
            continue
        if major_section and idx > 0:
            # Heavy-block items (per-COA, per-major-operation) get a
            # page-break-before so their heading doesn't dangle at the bottom
            # of the prior page. Light-block items (a short series of grouped
            # rows) keep a simple horizontal rule so they flow together.
            heavy = isinstance(it, dict) and any(
                k in it for k in ("coa_id", "most_dangerous", "rank", "label")
            )
            sep_style = (
                'border:0;border-top:0.75pt solid #000000;'
                'margin-top:20pt;margin-bottom:0pt'
            )
            if heavy:
                sep_style += ';page-break-before:always'
            out.append(f'<hr style="{sep_style}">')
        title_val = it.get(title_field["name"]) if title_field else None
        title_lbl = (title_field or {}).get("render_label", "") if title_field else ""
        # Role badges for Threat COA Statement and other rank-bearing
        # COA-like blocks. Surface the doctrinal role in the heading
        # rather than burying it as a kv row. Plain bracketed text, no
        # color coding — the badge text carries the semantic, color
        # adds visual noise (analyst feedback).
        #
        # Badges:
        #   rank=1                    → [MOST LIKELY]
        #   most_dangerous=True (rk3) → [MOST DANGEROUS]
        #   most_deceptive=True (rk4) → [MOST DECEPTIVE]
        #   rank=2 / no flag          → unbadged (intermediate slot,
        #                                no doctrinal name)
        #
        # Gated by the SHOW_COA_ROLE_BADGES_OVERRIDE contextvar from
        # equipment_doctrine. Default is False — analyst sees bare COA-1
        # / COA-2 / COA-3 / COA-4 headings without editorial pre-labels.
        # Toggle on via the gear button next to Generate on the dashboard
        # (config_fields entry on the Threat COA Statement + Threat COAs
        # specs). Same mechanism as the HVT_WORTHY_CATEGORIES_OVERRIDE
        # used by HVTL.
        coa_badge = ""
        _show_badges = False
        try:
            from utils import equipment_doctrine as _ed
            _show_badges = bool(_ed.SHOW_COA_ROLE_BADGES_OVERRIDE.get())
        except Exception:
            _show_badges = False
        if _show_badges and isinstance(it, dict):
            badges = []
            rk = it.get("rank")
            if isinstance(rk, int) and rk == 1:
                badges.append("MOST LIKELY")
            if it.get("most_dangerous") is True:
                badges.append("MOST DANGEROUS")
            if it.get("most_deceptive") is True:
                badges.append("MOST DECEPTIVE")
            if badges:
                coa_badge = (
                    "  "
                    + " ".join(f"[{b}]" for b in badges)
                )
        if title_val:
            enum_labels = (title_field or {}).get("enum_labels") or {}
            display = enum_labels.get(str(title_val)) or _humanize(str(title_val))
            if isinstance(it, dict):
                coa_id = it.get("coa_id") or it.get("id")
                lbl = it.get("label")
                if coa_id and lbl and title_field and title_field.get("name") != "coa_id":
                    display = f"{_humanize(str(coa_id))} — {_humanize(str(lbl))}"
                elif coa_id and lbl is None and title_field and title_field.get("name") != "coa_id":
                    display = _humanize(str(coa_id))
            out.append(_make_section_bar(_esc(_strip_doctrine_refs(display)) + coa_badge))
        elif title_lbl:
            out.append(_make_section_bar(_esc(_strip_doctrine_refs(title_lbl)) + coa_badge))
        # Scalar fields → kv table (with provenance lookup against `it`)
        kv_items = []
        for sf in scalar_subfields:
            if title_field and sf is title_field: continue
            v = it.get(sf["name"])
            if v in (None, "", False): continue
            kv_items.append((sf.get("render_label") or _humanize(sf["name"]),
                              _humanize_enum_value(_coerce_mgrs_value(v, sf), sf), sf["name"]))
        if kv_items:
            out.append(_kv_table(kv_items, parent=it, footnotes=footnotes))
        # Body subfields (list_of_dicts or dict) → sibling tables
        for sf in body_subfields:
            v = it.get(sf["name"])
            if v in (None, "", [], {}): continue
            sub_label = sf.get("render_label") or _humanize(sf["name"])
            if sf.get("shape") == "list_of_dicts" and isinstance(v, list):
                if len(body_subfields) > 1:
                    out.append(f'<h4 style="font-size:9.5pt;font-weight:bold;margin-top:6pt;margin-bottom:3pt">{_esc(sub_label)}</h4>')
                out.append(_list_of_dicts_table(v, sf.get("item_subfields") or [],
                                                  footnotes=footnotes,
                                                  orientation=orientation))
            elif sf.get("shape") == "dict" and isinstance(v, dict):
                if len(body_subfields) > 1:
                    out.append(f'<h4 style="font-size:9.5pt;font-weight:bold;margin-top:6pt;margin-bottom:3pt">{_esc(sub_label)}</h4>')
                # When strict_subfields is False, the writer emits only
                # the doctrinally-applicable subset of the declared key
                # vocabulary (e.g. TCOAS functional_allocation picks the
                # 3-4 role-bins that apply to the COA's operation type
                # from the 7-key union). Iterating over the full schema
                # union in that case renders `—` rows for
                # doctrinally-inapplicable role-bins, which reads to the
                # analyst as "expected value, missing" when doctrine
                # actually says "role does not apply here". Pass the
                # emitted keys through by passing subfields=None so
                # `_dict_inline` iterates value.keys() and emits exactly
                # what the writer produced.
                _sf_subfields = None if sf.get("strict_subfields") is False else sf.get("subfields")
                out.append(_dict_inline(v, _sf_subfields, footnotes=footnotes))
    return "".join(out)

# Page-usable width (DXA = 20ths of a pt) per orientation. 8.5" portrait −
# 0.75" margins (set via pytho-doc-settings) — 8.5" portrait − 1.5" margin →
# 7" usable = 10080 DXA. 11" landscape − 1.5" margin → 9.5" usable = 13680 DXA.
# Matches the convert.js page margins applied in render_artifact_html.
_PAGE_USABLE_DXA = {"portrait": 10080, "landscape": 13680}

def _column_width_dxa(cols: list, orientation: str = "portrait") -> list:
    """Compute per-column widths in DXA, summing to exactly PAGE_USABLE_DXA.
    The heuristic weights columns by typical content category:
    - identifier-ish (id, code, mgrs, dtg, doi, score): narrow (1.0)
    - default scalar columns: medium (1.6)
    - narrative / rationale / threshold / justification: wide (3.0)
    Same input always yields the same widths so identical-shape tables look
    identical across multiple renders in one document.

    Returns DXA integers summing to exactly PAGE_USABLE_DXA. We emit these as
    `width:Npt` in cell styles (convert.js parses `pt` as DXA directly), which
    avoids the percentage→OOXML conversion ambiguity in docx-npm.
    """
    if not cols: return []
    # Width buckets — column gets weight by name match.
    # ULTRA_NARROW: 1-3 char content (single-digit scores) → 0.5
    # NARROW: short identifiers, codes, MGRS, DTG → 1.0
    # WIDE: multi-line narrative, rationale, justification → 3.0
    # DEFAULT: everything else (Target, Category, Activity Type) → 1.7
    ULTRA_NARROW = {"score", "priority", "rank", "pri", "criticality_score",
                      "accessibility_score", "recuperability_score",
                      "vulnerability_score", "effect_score",
                      "recognizability_score"}
    NARROW = {"uic", "code", "echelon", "size", "side", "type",
                "hvi_id", "target_number", "answered", "precedence"}
    # ID is short (≤7 chars) but doesn't compress further; put in MEDIUM_NARROW
    # so it stays on one line in 15-col landscape tables like CARVER.
    MEDIUM_NARROW = {"id", "doi", "dtg", "mgrs", "location_mgrs", "location",
                      "total", "composite_total_score"}
    # MEDIUM tier — enum / labeled-value columns whose values are short words
    # but the column header is ≥10 chars and would wrap mid-word at
    # MEDIUM_NARROW width (e.g. WEM `parameter`/`assessment`, TEM `factor`,
    # CCA `protection_basis`).
    MEDIUM = {"parameter", "assessment", "factor", "protection_basis"}
    WIDE = {"narrative", "rationale", "justification", "activity",
             "activity_description", "statement", "description", "summary",
             "threshold", "favorable_threshold", "unfavorable_threshold",
             "current_forecast_value", "explanation", "guidance",
             "function_primary", "function_secondary",
             "requirement_text", "explanation_if_unanswered"}
    weights = []
    for name, _, _ in cols:
        n = (name or "").lower()
        if any(k in n for k in WIDE): w = 3.0
        elif n in MEDIUM or any(n.endswith("_"+k) for k in MEDIUM):
            w = 2.1
        # MEDIUM_NARROW must beat ULTRA_NARROW: `composite_total_score` matches
        # both `score` (ULTRA_NARROW suffix) and `composite_total_score` (full
        # name); the wider bucket wins so the "Total" header doesn't wrap.
        elif n in MEDIUM_NARROW or any(n.endswith("_"+k) for k in MEDIUM_NARROW):
            w = 1.5
        elif any(n == k or n.endswith("_"+k) for k in ULTRA_NARROW): w = 0.55
        elif any(n == k or n.startswith(k+"_") or n.endswith("_"+k) for k in NARROW):
            w = 1.1
        else: w = 1.9
        weights.append(w)
    total = sum(weights) or 1.0
    page = _PAGE_USABLE_DXA.get(orientation, 9360)
    raw = [w / total * page for w in weights]
    # Round + correct the last column so the sum equals page exactly
    out = [int(round(x)) for x in raw[:-1]]
    out.append(page - sum(out))
    return out


def _list_of_dicts_table(value: list, item_subfields: list,
                          footnotes: Optional['FootnoteRegistry'] = None,
                          orientation: str = "portrait") -> str:
    """Render a list_of_dicts. item_subfields drives column ordering + labels.
    Emits explicit per-column DXA widths on the first row's `<th>` cells
    (convert.js reads widths from there and emits a tblGrid that fills the
    page-usable width). Identical schemas render at identical column widths
    across multiple instances. If items contain nested list_of_dicts / dict
    subfields, switch to sectioned rendering."""
    if not value: return f'<p style="{S_GAP}">—</p>'
    if _list_of_dicts_has_nested_list(item_subfields):
        return _list_of_dicts_sectioned(value, item_subfields,
                                          footnotes=footnotes, orientation=orientation)
    cols = []
    if item_subfields:
        for f in item_subfields:
            if isinstance(f, dict):
                # view:"internal" / "workpaper" item_subfields exist for
                # cross-row linking (e.g. SITEMP shaping_operations.unit_id
                # back-references templated_units[].id) but should never
                # surface as a column on the published table. Default missing
                # view to "published" so existing schemas keep rendering.
                if (f.get("view") or "published") not in ("published", None):
                    continue
                cols.append((f["name"], f.get("render_label") or _humanize(f["name"]), f))
            else:
                cols.append((f, _humanize(f), {}))
    else:
        seen = []
        for it in value:
            if isinstance(it, dict):
                for k in it.keys():
                    if k not in seen and not k.startswith("_"):
                        seen.append(k)
        cols = [(k, _humanize(k), {}) for k in seen]
    dxa = _column_width_dxa(cols, orientation)
    head = "".join(
        f'<th style="{S_TH_HEADER_ROW};width:{w}pt">{_esc(_strip_doctrine_refs(lbl))}</th>'
        for (_, lbl, _), w in zip(cols, dxa)
    )
    body = []
    for it in value:
        if not isinstance(it, dict):
            body.append(f'<tr><td style="{S_TD}" colspan="{max(1,len(cols))}">{_esc(str(it))}</td></tr>')
            continue
        cells = []
        for k, _, sub in cols:
            v = it.get(k)
            sub_shape = (sub or {}).get("shape")
            sub_subfields = (sub or {}).get("subfields")
            if sub_shape == "dict" and isinstance(v, dict):
                cells.append(f'<td style="{S_TD}">{_dict_inline(v, sub_subfields, footnotes=footnotes)}</td>')
            elif isinstance(v, dict):
                cells.append(f'<td style="{S_TD}">{_dict_inline(v, None, footnotes=footnotes)}</td>')
            elif isinstance(v, list):
                if v and isinstance(v[0], dict):
                    cells.append(f'<td style="{S_TD}">{_list_of_dicts_table(v, [], footnotes=footnotes, orientation=orientation)}</td>')
                else:
                    cells.append(f'<td style="{S_TD}">{"; ".join(_esc(x) for x in v)}</td>')
            else:
                v = _coerce_mgrs_value(v, sub)
                val = _humanize_enum_value(v, sub)
                fill = _cell_fill_for(v, sub)
                cells.append(f'<td style="{S_TD}{fill}">{_cell_html(val, it, k, footnotes)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<table style="{S_TABLE}">'
            f'<thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')

_SNAKE_CASE_VALUE_RE = _fn_re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")


def _is_mgrs_column(spec) -> bool:
    """A subfield is an MGRS column when its render_label or name indicates
    MGRS. Used at the cell-rendering layer to coerce writer-emitted lat/lon
    strings into doctrinally-correct MGRS. SITEMP templated_units.location_mgrs,
    MCOO key_terrain.mgrs, MCOO obstacles.mgrs all match."""
    if not isinstance(spec, dict):
        return False
    label = (spec.get("render_label") or "")
    if "MGRS" in label:
        return True
    name = (spec.get("name") or "")
    return name == "mgrs" or name.endswith("_mgrs") or name == "location_mgrs"


def _coerce_mgrs_value(v, spec):
    """If `spec` declares an MGRS column and `v` looks like a 'lat,lon'
    decimal pair, convert to MGRS. Render-layer safety net: even when an
    upstream writer or LLM emits lat/lon, the published table shows MGRS.
    Pure pass-through for non-MGRS columns and for values already in MGRS
    form (no comma between two decimal numbers)."""
    if not _is_mgrs_column(spec):
        return v
    if not isinstance(v, str):
        return v
    from utils.coords import coerce_to_mgrs
    return coerce_to_mgrs(v)

def _humanize_enum_value(v, spec: dict):
    """When the field spec has an `enum` and the value matches one, look up
    `enum_labels[v]` for an explicit display string, falling back to
    snake_case → Title Case humanize. Lets schemas override the default
    casing for cases like 'fire_support_155mm' → 'Fire Support (155mm)'.

    Final-mile fallback: when the value is snake_case-shaped (lowercase
    letters/digits joined by underscores) but the field spec doesn't declare
    an enum — or the writer emitted an out-of-enum value — pass it through
    `_humanize` so `light_rain` reads as "Light rain" rather than leaking raw
    snake_case into the rendered body. Per-product enum_labels override is
    honored first; the smart humanizer is the catch-all."""
    if not isinstance(v, str): return v
    # Strip writer-emitted inline `[N]` citation markers BEFORE the enum /
    # snake_case checks so the visible label is "Light rain" (humanized),
    # not "light_rain[3]" (raw). The final HTML escape in _esc will also
    # strip these, but doing it here lets the snake_case regex match the
    # underlying token and route it through the smart humanizer.
    v_stripped = _strip_inline_citations(v).strip()
    if not v_stripped:
        return v_stripped
    # Per-spec enum_labels lookup always wins, even when the value isn't in
    # the declared enum list (writer may emit a near-enum string the schema
    # hasn't fully enumerated; if a label exists, use it).
    if spec and isinstance(spec, dict):
        labels = spec.get("enum_labels") or {}
        if v_stripped in labels:
            return labels[v_stripped]
        enum = spec.get("enum")
        if enum and v_stripped in enum:
            return labels.get(v_stripped) or _humanize(v_stripped)
    # No enum match (or no spec): if the value is snake_case-shaped, run it
    # through the smart humanizer so out-of-enum writer leaks ("light_rain")
    # render cleanly. Non-snake-case values pass through unchanged (except
    # for the inline-citation strip applied above).
    if _SNAKE_CASE_VALUE_RE.match(v_stripped):
        return _humanize(v_stripped)
    return v_stripped


# Doctrinal color triplets for assessment / status enums. Applied to the
# rendering cell background when the column value maps to one of the
# canonical triplet enums.
# WEM Fig 4-17: favorable=green, marginal=yellow, unfavorable=red.
# TEM/MCOO obstacle ratings: unrestricted=green, restricted=yellow,
# severely_restricted=red. Trafficability GO/SLOW-GO/NO-GO same triplet.
_ENUM_CELL_COLORS = {
    "favorable":           "#d8efd0",
    "marginal":            "#fdf3c4",
    "unfavorable":         "#f6cfcb",
    "unrestricted":        "#d8efd0",
    "restricted":          "#fdf3c4",
    "severely_restricted": "#f6cfcb",
    "go":                  "#d8efd0",
    "slow_go":             "#fdf3c4",
    "slow-go":             "#fdf3c4",
    "no_go":               "#f6cfcb",
    "no-go":               "#f6cfcb",
}

def _cell_fill_for(raw_value, spec: Optional[dict]) -> str:
    """Return ';background-color:#...' when the raw enum value maps to a
    doctrinal color triplet. Honors per-spec `enum_colors` override when the
    schema declares one."""
    if not isinstance(raw_value, str): return ""
    key = raw_value.strip().lower()
    if not key: return ""
    if spec and isinstance(spec, dict):
        custom = spec.get("enum_colors") or {}
        c = custom.get(raw_value) or custom.get(key)
        if c: return f";background-color:{c}"
    c = _ENUM_CELL_COLORS.get(key)
    return f";background-color:{c}" if c else ""


def _dict_inline(value: dict, subfields: Optional[list],
                  footnotes: Optional['FootnoteRegistry'] = None) -> str:
    """Render dict as compact 2-col kv (used inside cells / under paragraphs).
    Each item is (label, value, field_name) so provenance can be looked up
    against `value._provenance`."""
    if not value: return f'<span style="{S_GAP}">—</span>'
    if subfields:
        items = [(_humanize(k), value.get(k), k) for k in subfields]
    else:
        items = [(_humanize(k), v, k) for k, v in value.items() if not k.startswith("_")]
    return _kv_table(items, parent=value, footnotes=footnotes)

def _render_paragraph_field(spec: dict, value,
                              footnotes: Optional['FootnoteRegistry'] = None,
                              parent: Optional[dict] = None,
                              orientation: str = "portrait",
                              level: int = 2,
                              artifact_id: str = "",
                              user_id: str = "") -> str:
    """Render one schema-declared paragraph_fields entry. `parent` is the
    enclosing dict (used for provenance lookup). `orientation` determines
    page-usable width for column-width computation in nested tables.
    `artifact_id`/`user_id` are needed by shape="map" paragraph_fields to
    resolve the per-field PNG path under /files/<uid>/<aid>/<name>.png.

    Indentation follows AR 25-50 paragraph hierarchy: each nesting level
    (a. → (1) → (a)) shifts left margin by 18pt. Level 2 (top-level
    paragraphs 1./2./3./…) sits at the left margin; level 3 (a./b./c./…)
    indents 18pt; level 4 nests at 36pt, etc. Applied to BOTH the heading
    and the content block so each sub-paragraph reads as one indented
    unit, not a flush heading with shoved-back content."""
    name = spec.get("name", "")
    label = spec.get("render_label") or _humanize(name)
    shape = spec.get("shape", "")
    # AR 25-50 indent ladder. Top-level paragraph headings sit at the
    # left margin (level 2); each deeper level adds 18pt. Applied via
    # inline style on every emitted <p>/<h3>/<table>/<ul> for this
    # paragraph_field so the doc editor preview and DOCX export both
    # honor the hierarchy. <div> wrappers can't be used — convert.js
    # double-renders their children.
    _indent_pt = max(0, (level - 2) * 18)
    _indent_style = f"margin-left:{_indent_pt}pt" if _indent_pt else ""
    # Top-level paragraph_field headings emit a plain ALL CAPS bold inline
    # <h2> — Army doctrine uses weight + case for hierarchy, not color fills.
    # PPTX still uses `_filled_bar_html` for slide chrome. INTSUM paragraph
    # labels already carry the leading number ("1. WEATHER",
    # "2. SITUATION HIGHLIGHTS"). Sub-paragraph headings (level >= 3) stay
    # as bordered H3.
    def _make_head(lbl: str) -> str:
        if level <= 2:
            return _section_heading(lbl)
        h3_style = S_H3 + (";" + _indent_style if _indent_style else "")
        return f'<h3 style="{h3_style}">{_esc(_strip_doctrine_refs(lbl))}</h3>'

    def _indented_p(inner_html: str, extra_style: str = "") -> str:
        """Helper: emit a <p> with the AR 25-50 indent applied (no-op at
        top level). Used so sub-paragraph content lines up with its
        heading. extra_style appends to the inline style."""
        style_parts = [s for s in (_indent_style, extra_style) if s]
        style_attr = (' style="' + ";".join(style_parts) + '"') if style_parts else ""
        return f"<p{style_attr}>{inner_html}</p>"
    # Map figure embedding (GRINTSUM): when the field name is map_artifact_id
    # and the value looks like an M-#### id, emit an embedded image figure +
    # caption instead of plain text. convert.js resolves /mock/maps/M-####.png
    # against /tmp/mock_maps/ during visual walks. Width sized to land on the
    # same page as its heading on landscape pages without page-breaking.
    # Substitute the literal `M-####` placeholder in the schema render_label
    # with the actual artifact id BEFORE we build the heading so the heading
    # and caption show one consistent id (no double "M-####" / "M-0001").
    if name == "map_artifact_id" and isinstance(value, str) and value.strip():
        v = value.strip()
        # Skip placeholder/gap values like "NSTR", "(not available)", "[gap...]"
        # and writer-emitted scaffold tokens like "MAP-PENDING-RENDER".
        v_upper = v.upper()
        if v_upper.startswith("M-") and "PENDING" not in v_upper and "####" not in v:
            substituted_label = label.replace("M-####", v).replace("M-XXXX", v)
            head = _make_head(substituted_label)
            # Resolve image src against the same /files/M-####.png convention
            # _render_map_embed uses. Earlier wiring emitted /mock/maps/ but
            # nginx doesn't serve that path and there's no static fallback —
            # the result was a broken-image icon for every map artifact.
            # Now we (a) point at the unified /files/M-####.png path, and
            # (b) emit a styled inline-SVG placeholder behind the <img> via
            # the alt-text fallback styling so a missing PNG still looks
            # intentional (gray panel with the map id) rather than the
            # broken-image triangle.
            img_src = f"/files/{v}.png"
            caption = f"Figure 1. {substituted_label}"
            w_pt = 470 if orientation == "landscape" else 360
            h_pt = 264 if orientation == "landscape" else 252  # 16:9-ish on landscape
            return (
                f"{head}"
                f'<img src="{_esc(img_src)}" alt="{_esc(v)}" '
                f'style="display:block;margin:0 auto;width:{w_pt}pt;height:{h_pt}pt;'
                f'background:#eef2f7;border:1px solid #c8d2dd;object-fit:contain"/>'
                f'<p style="{S_CAPTION}">{_esc(caption)}</p>'
            )
    # Strip writer-scaffold placeholder tokens from the label before render.
    # Schema render_labels like "Rendered SITEMP (M-####)" leak the literal
    # placeholder when no map_id resolved. Same for `MAP-PENDING-RENDER`,
    # which writers occasionally pass as a value but it's a scaffold token.
    label = label.replace(" (M-####)", "").replace("(M-####)", "")
    label = label.replace(" (M-XXXX)", "").replace("(M-XXXX)", "")
    label = label.replace(" [M-####]", "").replace("[M-####]", "")
    # Map paragraphs render as full-bleed figures with the doctrinal
    # legend in-frame — WYSIWYG with the chromeless PPTX map slide,
    # which has no title. An H2 section heading above the figure ate
    # vertical space and pushed the aspect-locked figure off-page in
    # the HTML preview. Skip the heading for map shapes.
    head = "" if spec.get("shape") == "map" else _make_head(label)
    # Paragraph-level provenance marker on the section HEADING. Catches
    # the list_of_dicts / dict / map shapes that don't have one bottom
    # line of prose to attach the marker to — without this, MCOO's
    # mobility/AAs/key-terrain/obstacles tables emit zero footnote
    # markers and the Sources section comes out empty. Shape="string"
    # paragraphs have their own marker handler below (attached after the
    # terminal punctuation per military convention); we only emit at the
    # heading when the paragraph isn't a string and parent-level
    # _provenance[name] is set.
    if (footnotes is not None and parent is not None
            and spec.get("shape") not in ("string", "map")):
        _head_cite = _provenance_for(parent, name)
        if _head_cite:
            _head_marker = footnotes.add(_head_cite)
            if _head_marker:
                # Splice the marker inside the closing tag of the heading
                # so it reads as part of the heading text (e.g.
                # "TERRAIN MOBILITY CLASSIFICATIONS¹"). Works for both
                # h2 and h3 outputs of _make_head.
                for _close in ("</h2>", "</h3>"):
                    if head.endswith(_close):
                        head = head[: -len(_close)] + _head_marker + _close
                        break
    # INTSUM-style provenance wrapper: schema declares shape="string" but the
    # writer emits {"_provenance": {<field>: <cite>}, "text": "..."} so the
    # provenance can ride with the prose. Unwrap so we render the text and
    # pull the citation from the wrapper's _provenance (single-entry — the
    # writer chose one analytical sub-name; we just take its citation).
    # Compose paragraph-style declarations: indent (level-derived) + left
    # alignment. Used by every <p> emission below so heading and content
    # share the same left margin.
    _p_style = "text-align:left" + (";" + _indent_style if _indent_style else "")
    _ul_style = _indent_style if _indent_style else ""
    if shape in ("string", "") and isinstance(value, dict) and "text" in value:
        text_val = value.get("text", "")
        marker = ""
        if footnotes is not None:
            cite = ""
            prov = value.get("_provenance")
            if isinstance(prov, dict) and prov:
                for v in prov.values():
                    if v:
                        cite = v
                        break
            elif isinstance(prov, str):
                cite = prov
            if cite: marker = footnotes.add(cite)
        return f'{head}<p style="{_p_style}">{_esc(text_val)}{marker}</p>'
    if shape == "map":
        # Schema-declared map figure. Image path is convention-based:
        # /files/<uid>/<aid>/<field_name>.jpg. tool_render_map writes there;
        # exporter reads it back. No header dict pollution.
        #
        # Per-field map_id + bbox (for DocEditor's Edit-map deep link) live
        # on the parent structured_data at `map_overlays.<field_name>`, also
        # keyed by the field name. Missing image → empty string (no figure,
        # no break, no placeholder); the caller skips empty paragraph
        # fields so an unrendered map slot leaves no gap on the page.
        #
        # `.jpg` not `.png` — the headless renderer switched encoders to
        # avoid a 30s PNG-encode timeout at 2560x1440; the file extension
        # must match what tool_render_map actually wrote.
        if not artifact_id:
            return ""
        _uid = (user_id or "").strip() or "anonymous"
        _src = f"/files/{_uid}/{artifact_id}/{name}.jpg"
        _overlays = (parent or {}).get("map_overlays") if isinstance(parent, dict) else None
        _meta = (_overlays or {}).get(name) or {}
        _map_id = (_meta.get("map_id") or "").strip() if isinstance(_meta, dict) else ""
        _bbox_val = _meta.get("bbox") if isinstance(_meta, dict) else None
        _bbox = ""
        if isinstance(_bbox_val, (list, tuple)) and len(_bbox_val) == 4:
            _bbox = ",".join(str(x) for x in _bbox_val)
        elif isinstance(_bbox_val, str):
            _bbox = _bbox_val.strip()
        _alt = label or f"Map for {artifact_id}"
        # Edit-map button is enabled when the writer/renderer stamped a
        # map_id under map_overlays.<field_name> (so the DocEditor's
        # deep-link click handler has a target). Without one, render a
        # static figure.
        _edit_btn = ""
        if _map_id:
            _edit_btn = (
                f'<figcaption>'
                f'<button class="ibl-edit-map" type="button" '
                f'style="font-size:9pt;padding:2pt 8pt;cursor:pointer;'
                f'border:1pt solid #2c3e50;background:#f2f2f2;color:#2c3e50">'
                f'Edit map</button>'
                f'</figcaption>'
            )
        return (
            f'{head}'
            f'<figure class="ibl-map-embed" '
            f'data-map-id="{_esc(_map_id)}" '
            f'data-bbox="{_esc(_bbox)}" '
            f'data-artifact-id="{_esc(artifact_id)}" '
            f'data-map-field="{_esc(name)}">'
            f'<img src="{_esc(_src)}" alt="{_esc(_alt)}"/>'
            f'{_edit_btn}'
            f'</figure>'
        )
    if value in (None, "", [], {}):
        gap_style = S_GAP + (";" + _indent_style if _indent_style else "")
        return f'{head}<p style="{gap_style}">—</p>'
    if shape == "string":
        if isinstance(value, (list, tuple)):
            li_items = "".join(f"<li>{_esc(x)}</li>" for x in value)
            ul_attr = f' style="{_ul_style}"' if _ul_style else ""
            return f"{head}<ul{ul_attr}>{li_items}</ul>"
        marker = ""
        if footnotes is not None and parent is not None:
            cite = _provenance_for(parent, name)
            if cite: marker = footnotes.add(cite)
        # Narrative paragraphs are left-aligned. Justified text reads as
        # ragged gaps in narrow column widths; analysts pushed back.
        return f'{head}<p style="{_p_style}">{_esc(value)}{marker}</p>'
    if shape == "list_of_dicts":
        item_subfields = spec.get("item_subfields") or []
        _list_value = value if isinstance(value, list) else []
        _table_html = _list_of_dicts_table(_list_value, item_subfields,
                                            footnotes=footnotes,
                                            orientation=orientation)
        # Emit per-item / per-subfield markers as a hidden block AFTER the
        # visible table. Each item gets a nested block-level marker carrier
        # `pytho:<name>[<i>]` containing one inline marker per item_subfield.
        # The visible content is duplicated inside each inline marker so the
        # importer can recover scalar values per (item, subfield) tuple.
        # The block is styled invisible (1pt font, white text, no spacing)
        # so the analyst-visible DOCX is unchanged but the importer has
        # the structured value tree to walk.
        if _list_value and item_subfields:
            marker_parts = []
            for i, item in enumerate(_list_value):
                if not isinstance(item, dict):
                    continue
                inner = []
                for sf in item_subfields:
                    sf_name = sf.get("name") or ""
                    if not sf_name:
                        continue
                    sub_shape = sf.get("shape", "string")
                    raw = item.get(sf_name)
                    if sub_shape == "list_of_dicts" and isinstance(raw, list):
                        # Nested list — emit one marker per (i, sf_name, j, sub_sf).
                        nested_subfields = sf.get("item_subfields") or []
                        for j, sub_item in enumerate(raw):
                            if not isinstance(sub_item, dict):
                                continue
                            for sub_sf in nested_subfields:
                                sub_sf_name = sub_sf.get("name") or ""
                                if not sub_sf_name:
                                    continue
                                sub_val = sub_item.get(sub_sf_name)
                                if isinstance(sub_val, (list, dict)):
                                    sub_val = json.dumps(sub_val, default=str)
                                inner.append(_sdt_inline_wrap_html(
                                    [name, i, sf_name, j, sub_sf_name],
                                    sub_sf.get("render_label") or sub_sf_name,
                                    _esc(str(sub_val or "")),
                                ))
                    elif sub_shape == "dict" and isinstance(raw, dict):
                        for k, vv in raw.items():
                            if isinstance(vv, (list, dict)):
                                vv = json.dumps(vv, default=str)
                            inner.append(_sdt_inline_wrap_html(
                                [name, i, sf_name, k],
                                f"{sf.get('render_label') or sf_name} / {k}",
                                _esc(str(vv or "")),
                            ))
                    else:
                        val_str = raw
                        if isinstance(val_str, (list, dict)):
                            val_str = json.dumps(val_str, default=str)
                        inner.append(_sdt_inline_wrap_html(
                            [name, i, sf_name],
                            sf.get("render_label") or sf_name,
                            _esc(str(val_str or "")),
                        ))
                if inner:
                    marker_parts.append(_sdt_wrap_html(
                        [name, i],
                        f"{spec.get('render_label') or name} #{i + 1}",
                        f"<p style=\"display:none;color:#ffffff;font-size:1pt;"
                        f"line-height:1pt;margin:0;padding:0\">{''.join(inner)}</p>",
                    ))
            if marker_parts:
                _table_html = _table_html + "".join(marker_parts)
        return f"{head}{_table_html}"
    if shape == "dict":
        sub = spec.get("subfields")
        # Per-subfield specs let schemas attach `enum` / `enum_labels` to
        # dict subfields without changing the bare-string `subfields` order
        # list (backward compatible). Lookup is keyed by subfield name.
        subfield_specs = spec.get("subfield_specs") or {}
        if isinstance(value, dict):
            # Doctrinal sub-paragraph render: when the schema declares
            # `subfields`, each subfield is a doctrinal lettered subparagraph
            # (FM 6-99 / ATP 2-33.4 — INTSUM Para 2/3/4, GRINTSUM
            # multi-discipline rollup). Render as flat h3 siblings with a./b./
            # c./… lettering rather than a compact kv table so the doctrinal
            # paragraph hierarchy is visible. The nested-dict fallback covers
            # the case where some subfield value is itself a dict/list and
            # needs further structured rendering.
            if sub or any(isinstance(v, (dict, list)) for v in value.values()):
                # Nested-dict path: sub-fields are emitted as flat siblings of
                # the paragraph heading so each child h3/p/table flows as its
                # own paragraph in the DOCX. Earlier versions wrapped them in
                # `<div style="margin-left:18pt">`, but convert.js's element
                # walker treats `div` as a paragraph AND recurses into its
                # children, producing a duplicate inline-joined render plus
                # the proper render. Visual indentation is recovered through
                # the heading styling rather than a container wrapper.
                out = [head]
                keys = sub if sub else list(value.keys())
                # Doctrinal lettering: FM 6-99 / ATP 2-33.4 treat dict subfields
                # under a numbered paragraph as lettered subparagraphs (a. / b.
                # / c. / ...). Prefix the render_label so the visual hierarchy
                # is unmistakable in the DOCX. convert.js can't honor a
                # <div margin-left:Xpt> indent wrapper, so we encode the
                # subordination in the label rather than an indent container.
                # Skip subfields with empty/null/false values to avoid
                # "Camps —" orphan headings (CCA displaced_persons_overlay
                # with no camp data). The dict heading itself stays; only
                # subfields that have content are emitted.
                visible_keys = [k for k in keys
                                if k in value
                                and not (isinstance(k, str) and k.startswith("_"))
                                and value[k] not in (None, "", [], {}, False)]
                for idx, k in enumerate(visible_keys):
                    letter = chr(ord('a') + idx) if idx < 26 else f"{idx+1}"
                    sub_label = f"{letter}. {_humanize(k)}"
                    raw_v = value[k]
                    # If the parent paragraph declares per-subfield enum
                    # metadata, run scalar values through the enum-label
                    # lookup BEFORE recursing into the per-subfield render.
                    # Only do this for string scalars; dict/list values
                    # recurse into structural rendering.
                    sf_meta = subfield_specs.get(k) or {}
                    if isinstance(raw_v, str) and sf_meta.get("enum"):
                        raw_v = _humanize_enum_value(raw_v, sf_meta)
                    sub_spec = {"name": k, "shape": _infer_shape(value[k]),
                                "render_label": sub_label}
                    out.append(_render_paragraph_field(sub_spec, raw_v,
                                                         footnotes=footnotes,
                                                         parent=value,
                                                         orientation=orientation,
                                                         level=level + 1))
                return "".join(out)
            items = []
            for k in (sub or list(value.keys())):
                raw = value.get(k)
                # Apply per-subfield enum_labels / smart humanize when the
                # schema declares a subfield_specs entry for k. Falls back
                # to the raw value untouched (correct for free-text fields).
                sf_spec = subfield_specs.get(k) or {}
                if sf_spec.get("enum"):
                    rendered_val = _humanize_enum_value(raw, sf_spec)
                else:
                    rendered_val = raw
                items.append((_humanize(k), rendered_val, k))
            return f"{head}{_kv_table(items, parent=value, footnotes=footnotes)}"
        return f"{head}<p>{_esc(value)}</p>"
    if shape == "grid_table":
        # The LLM writer sometimes emits the matrix as a ```json-fenced
        # string instead of a structured dict — when that happens the
        # rendered HTML drops to "<p>{value}</p>" and the analyst sees
        # an empty / quoted blob instead of the matrix. Parse the fence
        # off and json.loads to recover the dict. Audit caught this on
        # CCA ASCOPE × PMESII-PT (the centerpiece) and TCS capabilities
        # matrix (7×4 grid) rendering as empty containers.
        if isinstance(value, str):
            value = _maybe_parse_fenced_json(value)
        if isinstance(value, dict):
            return f"{head}{_render_grid_inline(value, footnotes=footnotes, orientation=orientation)}"
        return f"{head}<p>{_esc(value)}</p>"
    return f"{head}{_render_inferred(value, level, orientation=orientation)}"

def _infer_shape(value) -> str:
    if isinstance(value, list):
        return "list_of_dicts" if (value and isinstance(value[0], dict)) else "string"
    if isinstance(value, dict): return "dict"
    return "string"

def _render_inferred(value, level: int, orientation: str = "portrait") -> str:
    if isinstance(value, str): return f"<p>{_esc(value)}</p>"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return _list_of_dicts_table(value, [], orientation=orientation)
        return f"<ul>{''.join(f'<li>{_esc(x)}</li>' for x in value)}</ul>"
    if isinstance(value, dict):
        return _kv_table([(_humanize(k), v) for k, v in value.items() if not k.startswith("_")])
    return f"<p>{_esc(value)}</p>"

def _render_grid_inline(matrix: dict, row_axis: Optional[list] = None,
                          col_axis: Optional[list] = None,
                          row_label: str = "", col_label: str = "",
                          footnotes: Optional['FootnoteRegistry'] = None,
                          orientation: str = "portrait") -> str:
    """ASCOPE×PMESII-PT-style grid. Cell provenance comes from
    matrix[row]._provenance[col] when present (per-cell citation).

    Column widths are computed explicitly from the page-usable DXA so the
    row-header column doesn't steal width from the data columns. The header
    text receives word-width minimums: 'Organizations' must fit on one line.
    """
    if not matrix: return f'<p style="{S_GAP}">—</p>'
    if not row_axis: row_axis = list(matrix.keys())
    if not col_axis:
        seen = []
        for v in matrix.values():
            if isinstance(v, dict):
                for k in v.keys():
                    if k not in seen and not k.startswith("_"): seen.append(k)
        col_axis = seen
    # Width allocation: row-header column gets fixed ~12% (enough for PMESII
    # labels like 'Infrastructure'); data columns split the rest evenly. This
    # avoids the row-header swallowing space the data columns need.
    page = _PAGE_USABLE_DXA.get(orientation, 9360)
    header_col_dxa = max(1200, int(page * 0.13))
    remaining = page - header_col_dxa
    n_data = max(len(col_axis), 1)
    per_data = remaining // n_data
    # Distribute rounding into the last column
    data_widths = [per_data] * (n_data - 1) + [remaining - per_data * (n_data - 1)]
    # DXA → pt for inline width style
    def _dxa_to_pt(d): return d / 20.0
    header_col_pt = _dxa_to_pt(header_col_dxa)
    data_pt = [_dxa_to_pt(w) for w in data_widths]

    # Python 3.11 (container runtime) rejects backslashes inside f-string
    # expressions; precompute the axis-legend snippet so the f-string only
    # interpolates plain values.
    _axis_legend = (
        '<br><span style="font-weight:normal;font-style:italic">'
        '↓ row · col →</span>'
    ) if row_label and col_label else ""
    head_cells = [
        f'<th style="{S_TH_HEADER_ROW};text-align:center;width:{header_col_pt}pt">'
        f'{_esc(row_label) if row_label else "&nbsp;"}'
        f'{_axis_legend}'
        f'</th>'
    ]
    for c, w_pt in zip(col_axis, data_pt):
        head_cells.append(
            f'<th style="{S_TH_HEADER_ROW};width:{w_pt}pt">{_esc(_humanize(c))}</th>'
        )
    body_rows = []
    for rname in row_axis:
        rval = matrix.get(rname) or {}
        cells = [
            f'<th style="{S_TH_LABEL};width:{header_col_pt}pt">{_esc(_humanize(rname))}</th>'
        ]
        for c, w_pt in zip(col_axis, data_pt):
            cv = rval.get(c) if isinstance(rval, dict) else ""
            cells.append(
                f'<td style="{S_TD};width:{w_pt}pt">{_cell_html(cv, rval, c, footnotes)}</td>'
            )
        body_rows.append(f'<tr>{"".join(cells)}</tr>')
    return (f'<table style="{S_TABLE}">'
            f'<thead><tr>{"".join(head_cells)}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>')

def _normalize_matrix(matrix, row_axis: Optional[list] = None,
                        col_axis: Optional[list] = None) -> dict:
    """Coerce any of the common PW-emitted matrix shapes into the canonical
    `{<row>: {<col>: <scalar>}}` form the renderer expects.

    Accepted inputs:
    - `{<row>: {<col>: <scalar>}}` — pass through.
    - `{"cells": [{"row": ..., "col": ..., "value": ...}]}` — flat cell list.
    - `{"cells": {<row>: {<col>: <scalar>}}}` — wrapped dict-of-dicts.
    - `{"rows": [{"row_id": ..., "cols": [v0, v1, ...]}]}` — row/col-array
      form (positional cols indexed by col_axis). PW-emitted ASCOPE×PMESII
      uses this shape; we pair each value with the corresponding col_axis
      key. Per-cell provenance lives on a sibling `_provenance` dict keyed
      by `"<row>_<col>"`.

    Cell `value` of `[{"text": "..."}]` is flattened to "..." (joined by "; ").
    """
    # List-of-dicts with row-id field — many writers emit
    # `[{"id": "ada_assets", "accuracy_required_tle": ..., ...}, ...]` or
    # equivalent per-product row-naming convention. Convert to the
    # canonical `{<row_id>: {<col>: <val>}}` form. Row-id field names cover
    # the per-product conventions: `id` (TSS/AGM/TSM generic), `pir` (ICM),
    # `function` (Threat Capability Statement capabilities_matrix),
    # `hpt`/`hpt_id`/`target_id` (HPTL-derived matrices). First non-empty
    # of these wins; the matching key is stripped from the row body.
    _ROW_ID_FIELDS = ("id", "row_id", "row", "name", "pir",
                       "function", "warfighting_function", "wff",
                       "hpt", "hpt_id", "target", "target_id", "unit")
    # Writer LLMs sometimes wrap a structurally-correct JSON object in
    # ```json ... ``` markdown fences (e.g. ICSM_20260611T084239_088Z's
    # icsm_matrix). Strip fences + re-parse so the canonical form below
    # picks it up. Defensive: any parse failure leaves the original
    # string untouched and the renderer's empty-fallback fires (same as
    # before this strip).
    if isinstance(matrix, str):
        import re as _re, json as _json
        s = matrix.strip()
        m = _re.match(r"^```(?:json)?\s*(.*?)```\s*$", s,
                        flags=_re.DOTALL | _re.IGNORECASE)
        if m:
            s = m.group(1).strip()
        # Bare-JSON case (no fences but starts with { or [).
        if s.startswith("{") or s.startswith("["):
            try:
                matrix = _json.loads(s)
            except Exception:
                pass
    if isinstance(matrix, list):
        grid: dict = {}
        for r in matrix:
            if not isinstance(r, dict): continue
            rid = None
            rid_field = None
            for f in _ROW_ID_FIELDS:
                v = r.get(f)
                if v not in (None, "", [], {}):
                    rid = v
                    rid_field = f
                    break
            if not rid: continue
            row_dict = {k: v for k, v in r.items() if k != rid_field}
            grid[str(rid)] = row_dict
        return grid
    if not isinstance(matrix, dict): return {}
    # rows[]/cols[] form — positional cols indexed by col_axis.
    rows_field = matrix.get("rows")
    if isinstance(rows_field, list) and col_axis:
        grid: dict = {}
        prov_flat = matrix.get("_provenance") if isinstance(matrix.get("_provenance"), dict) else None
        for r in rows_field:
            if not isinstance(r, dict): continue
            rid = r.get("row_id") or r.get("row") or r.get("name")
            if not rid: continue
            cols_val = r.get("cols") or r.get("values") or []
            row_dict: dict = {}
            for idx, col_name in enumerate(col_axis):
                if idx < len(cols_val):
                    row_dict[col_name] = cols_val[idx]
                else:
                    row_dict[col_name] = None
            # Pull per-cell provenance from the flat `<row>_<col>` keys when
            # present so footnote markers attach to the right cell.
            if prov_flat is not None:
                inner_prov = {}
                for col_name in col_axis:
                    key = f"{rid}_{col_name}"
                    if key in prov_flat and prov_flat[key]:
                        v = prov_flat[key]
                        inner_prov[col_name] = ", ".join(v) if isinstance(v, list) else v
                if inner_prov:
                    row_dict["_provenance"] = inner_prov
            grid[rid] = row_dict
        if grid:
            return grid
    # Already canonical?
    if matrix and not any(k in ("cells",) for k in matrix.keys()):
        # Writers occasionally emit row/col keys in human-readable / prose
        # form ("Command and Control", "Effects on Friendly") instead of the
        # snake_case axis tokens the renderer expects ("command_and_control",
        # "effects_on_friendly"). Normalize each row/col key against the
        # provided axes so the grid aligns even when the writer drifts from
        # the schema's literal token. Without this, _render_grid_inline's
        # `matrix.get("command_and_control")` returns None and the grid
        # renders all-empty cells — the marquee artifact field silently
        # disappears even though structured_data carries it in full.
        def _snake_fold(k: str) -> str:
            if not isinstance(k, str): return ""
            s = k.strip().lower()
            s = re.sub(r"[\s\-/]+", "_", s)
            s = re.sub(r"[^a-z0-9_]+", "", s)
            s = re.sub(r"_+", "_", s).strip("_")
            return s
        def _resolve_against_axis(k, axis):
            if not axis or k in axis: return k
            folded = _snake_fold(k)
            for tok in axis:
                if _snake_fold(tok) == folded:
                    return tok
            return k
        if row_axis or col_axis:
            normalized: dict = {}
            for rk, rv in matrix.items():
                if rk == "_provenance":
                    normalized[rk] = rv
                    continue
                new_rk = _resolve_against_axis(rk, row_axis)
                if isinstance(rv, dict) and col_axis:
                    new_rv: dict = {}
                    for ck, cv in rv.items():
                        if ck == "_provenance" and isinstance(cv, dict):
                            inner_prov: dict = {}
                            for pk, pv in cv.items():
                                inner_prov[_resolve_against_axis(pk, col_axis)] = pv
                            new_rv[ck] = inner_prov
                        else:
                            new_rv[_resolve_against_axis(ck, col_axis)] = cv
                    normalized[new_rk] = new_rv
                else:
                    normalized[new_rk] = rv
            return normalized
        return matrix
    cells = matrix.get("cells")
    if isinstance(cells, dict):
        # The wrapped dict may be either canonical nested `{<row>: {<col>: ...}}`
        # OR the flat `<row>_<col>` form the CCA writer emits, e.g.
        # `{"infrastructure_structures": ["Valga rail station"], "social_areas": [...]}`.
        # Detect: if any value is a list/scalar (not a dict), and any key
        # contains an underscore matching <row_axis>_<col_axis>, expand into
        # the canonical nested form. Provenance lives in the parent
        # `_provenance: {<row>_<col>: <cite>}` sibling.
        # Helper to extract a display string from a cell value of any shape.
        # Cells may be: scalar, list-of-scalars, list-of-dicts, single dict.
        # Writers often emit dicts like {"details": "...", "name": "...",
        # "id": "C-001", "provenance": "..."} per entity — we extract the
        # display-relevant fields (name + details) and skip plumbing fields
        # (id, provenance, source) so the cell reads as "Name (details)"
        # instead of the raw Python dict repr.
        def _cell_to_str(val):
            if val in (None, "", [], {}):
                return ""
            if isinstance(val, (int, float, bool)):
                return str(val)
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                name = val.get("name") or val.get("title") or val.get("label") or val.get("id")
                details = val.get("details") or val.get("description") or val.get("text") or val.get("value")
                if name and details:
                    return f"{name} ({details})"
                return str(name or details or "")
            if isinstance(val, list):
                parts = [_cell_to_str(it) for it in val]
                return "; ".join(p for p in parts if p)
            return str(val)

        if row_axis and col_axis:
            grid: dict = {r: {} for r in row_axis}
            prov_flat = matrix.get("_provenance") if isinstance(matrix.get("_provenance"), dict) else None
            for flat_key, val in cells.items():
                # Match the LONGEST row_axis prefix so two-token row names
                # like `physical_environment` win over `physical`.
                for r in sorted(row_axis, key=len, reverse=True):
                    if flat_key == r or flat_key.startswith(r + "_"):
                        rest = flat_key[len(r):].lstrip("_")
                        if rest in col_axis:
                            cell_val = _cell_to_str(val)
                            if cell_val:
                                grid[r][rest] = cell_val
                            # Per-cell provenance.
                            if prov_flat and flat_key in prov_flat and prov_flat[flat_key]:
                                grid[r].setdefault("_provenance", {})[rest] = prov_flat[flat_key]
                        break
            # Drop rows with no cells AND no provenance — keep the structural
            # skeleton via row_axis so the renderer can still draw the grid.
            return grid
        return cells
    if not isinstance(cells, list): return {}
    grid: dict = {}
    def _flatten_value(v):
        if isinstance(v, str): return v
        if isinstance(v, (int, float, bool)): return v
        if isinstance(v, list):
            parts = []
            for it in v:
                if isinstance(it, dict):
                    parts.append(it.get("text") or it.get("value") or
                                  next(iter(it.values()), ""))
                else:
                    parts.append(str(it))
            return "; ".join(str(x) for x in parts if x)
        if isinstance(v, dict):
            return v.get("text") or v.get("value") or json.dumps(v, default=str)
        return ""
    for c in cells:
        if not isinstance(c, dict): continue
        r = c.get("row"); col = c.get("col")
        if r is None or col is None: continue
        v = _flatten_value(c.get("value"))
        if r not in grid: grid[r] = {}
        existing = grid[r].get(col)
        if existing:
            grid[r][col] = f"{existing}; {v}" if v else existing
        else:
            grid[r][col] = v
    return grid

def _render_matrix_from_spec(structured_data: dict, schema: dict,
                                footnotes: Optional['FootnoteRegistry'] = None,
                                orientation: str = "portrait") -> str:
    spec = schema.get("matrix_spec") or {}
    field = spec.get("field")
    if not field: return ""
    raw = structured_data.get(field)
    matrix = _normalize_matrix(raw, spec.get("row_axis"), spec.get("col_axis"))
    if not isinstance(matrix, dict) or not matrix: return ""
    label = ""
    for p in (schema.get("paragraph_fields") or []):
        if p.get("name") == field:
            label = p.get("render_label") or _humanize(field)
            break
    if not label: label = _humanize(field)
    grid = _render_grid_inline(
        matrix,
        row_axis=spec.get("row_axis"),
        col_axis=spec.get("col_axis"),
        row_label=spec.get("row_label", ""),
        col_label=spec.get("col_label", ""),
        footnotes=footnotes,
        orientation=orientation,
    )
    return f'{_filled_bar_html(label)}{grid}'

# ---------------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------------
def _line_field_keys(structured_data: dict, schema: dict) -> list:
    """Return ordered line_<N>_<...> keys from schema if available, else from data."""
    pfs = schema.get("paragraph_fields") or []
    if pfs and all(p.get("name", "").startswith("line_") for p in pfs):
        return [p["name"] for p in pfs]
    return sorted(
        [k for k in structured_data if k.startswith("line_")],
        key=lambda x: int(x.split("_")[1]) if x.split("_")[1].isdigit() else 0,
    )

def _line_label(field: str, schema: dict) -> str:
    for p in (schema.get("paragraph_fields") or []):
        if p.get("name") == field:
            return p.get("render_label") or _humanize(field)
    return _humanize(field)

def render_artifact_html(artifact_type: str, structured_data: dict,
                          gaps: Optional[list] = None,
                          artifact_id: str = "",
                          user_id: str = "") -> str:
    """Top-level renderer. `gaps` is accepted for backwards-compat with callers
    but is NOT rendered into the artifact body — gaps are runner-internal
    completeness metadata and stay in `art.gaps`. The published artifact is
    the analytical product the analyst publishes; runner-flagged missing
    fields belong on the OE's next-turn brief, not on the published page.

    `artifact_id` (optional) flows into the map-embed figure's
    `data-artifact-id` attr so DocEditor's edit-button hook can identify which
    artifact's map to open in OperationsMap. Callers that already track the
    id (`pytho-app/main.py` and the harness loop in `walk_artifacts`) pass it
    through; older callers (test code) can omit it and the figure simply
    carries an empty `data-artifact-id`."""
    # Allow caller to pre-fetch artifact_id from the wrapping entry, but also
    # fall back to structured_data.artifact_id if present.
    if not artifact_id:
        artifact_id = str(structured_data.get("artifact_id") or "")
    schema = PRODUCT_SCHEMAS.get(artifact_type, {})
    shape = schema.get("shape", "")
    header = structured_data.get("header") or {}
    # `doctrinal_basis_citation` is still read by the writer/agent for grounding
    # but is NOT rendered as a visible body element or page footer — kept off
    # the rendered artifact per analyst feedback (reads as redundant chrome).
    long_name = PRODUCT_LONG_NAMES.get(artifact_type, schema.get('name', artifact_type))
    # Title is the doctrinal long-name uppercased. Earlier wiring appended
    # `[<artifact_type>]` for products where long-name != registry key
    # (e.g. `WEATHER EFFECTS MATRIX [WEM]`), but this produced ugly
    # duplication for the 14 products whose long-name IS the registry key
    # (`THREAT MODEL [THREAT MODEL]`, `CARVER [CARVER]`, …). Drop the
    # bracket entirely — the registry-key acronym lives in the metadata
    # band / file naming, not the rendered title.
    title = long_name.upper()
    # Schemas may name a header field whose resolved value IS the title
    # (e.g. INTSUM uses `intsum_number` = "INTSUM-<start>-<end>" as the
    # document title). When set, replace the doctrinal long-name title
    # with the resolved field value and skip the field from the header
    # table so it doesn't render twice.
    _title_field = schema.get("title_field")
    _header_skip: set = set()
    if _title_field:
        _hspecs = schema.get("header_fields") or {}
        # Prefer the live default-template recipe over the stored value:
        # _build_skeleton bakes the default at skeleton time when downstream
        # fields (e.g. period DTGs) are still empty, freezing "INTSUM--" into
        # the header. Re-resolve at render time against the current header dict.
        _tval = ""
        _tdef = ((_hspecs.get(_title_field) or {}).get("default") or "")
        if isinstance(_tdef, str) and _tdef:
            try:
                class _SafeDict(dict):
                    def __missing__(self, key):
                        return "{" + key + "}"
                _candidate = _tdef.format_map(_SafeDict(
                    {k: ("" if v is None else v) for k, v in header.items()}))
            except (ValueError, IndexError):
                _candidate = _tdef
            if not re.search(r"[-_/]{2,}|^[-_/ ]+|[-_/ ]+$|^\s*$", _candidate):
                _tval = _candidate
        if not _tval:
            # No default, or default still half-resolved — fall back to the
            # stored value if it isn't itself half-resolved.
            _stored = (header.get(_title_field) or "").strip()
            if _stored and not re.search(r"[-_/]{2,}|^[-_/ ]+|[-_/ ]+$", _stored):
                _tval = _stored
        if _tval:
            title = _tval.upper()
            _header_skip.add(_title_field)
    # Output format implies orientation — docx → portrait (8.5×11 Letter),
    # pptx → landscape (16:9 slide). Schemas declare only `output_format`;
    # page_orientation is derived, not stored.
    orientation = "landscape" if schema.get("output_format") == "pptx" else "portrait"
    footnotes = FootnoteRegistry()

    # Tighter page margins so densely-tabled portrait products (WEM 3-table
    # operation, Threat COA Statement COA blocks, CCA matrix) get more usable width and don't
    # cram into 6.5" portrait. 0.75" all sides instead of the convert.js
    # default 1". Convert.js reads `pageMargins` and `pageOrientation` from a
    # script#pytho-doc-settings element in the HTML; margin values are in inches.
    # `pageOrientation` is the schema's `page_orientation` field — the DocEditor
    # splitter (DocEditor.tsx:updatePageLayout) reads it to flip portrait/landscape
    # page constants, and convert.js reads it (alongside `pageMargins`) so the
    # save-back DOCX round-trips with the right orientation.
    _doc_settings_payload = {
        "pageMargins": {"top": 0.75, "right": 0.75, "bottom": 0.75, "left": 0.75},
        "pageOrientation": orientation,
    }
    # PPTX-target products (HVTL, HPTL, AGM, CARVER scoring, INTSUM short
    # form, etc.) carry classification on the section-bar header or the
    # PPTX-page chrome itself — the DocEditor body banner is a duplicate
    # and ATP 2-01.3 Fig 6-9 / FM 3-60 App. C show the published artifacts
    # without a body-top banner. Suppress it explicitly by emitting an
    # empty header in the doc-settings block; DocEditor.tsx applies the
    # override via `'header' in savedSettings` so empty string clears the
    # default 'UNCLASSIFIED\nFOR EXERCISE USE ONLY' state.
    if schema.get("output_format") == "pptx":
        _doc_settings_payload["header"] = ""
    DOC_SETTINGS = ('<script id="pytho-doc-settings" type="application/json">'
                     f'{json.dumps(_doc_settings_payload)}'
                     '</script>')
    classification = header.get("classification") or ""
    parts = [
        "<!DOCTYPE html><html><head>",
        f"<title>{_esc(title)}</title>",
        '<meta charset="utf-8">',
        DOC_SETTINGS,
        f'</head><body style="{S_BODY}">',
        # Top + bottom classification banner strips dropped — TinyMCE /
        # DocEditor renders classification at the page-layout chrome level,
        # and convert.js stamps it in the DOCX page header. The body
        # banner was IBL-era duplication; the helper function
        # `_classification_banner_html` is still defined for the PPTX
        # top/bottom strip but is not called from the HTML/DOCX body.
        #
        # Meta-band ribbon also dropped — AR 25-50 Figures 2-15/2-16 show a
        # stacked label:value header block top-left as the only header
        # chrome (no horizontal pipe-joined strip). The helper
        # `_meta_band_html` is kept for PPTX slide chrome but no longer
        # called from the HTML/DOCX body.
    ]
    # Section break policy. PPTX-output products are slide-aspect-locked —
    # each section must start on a fresh slide because python-pptx doesn't
    # auto-paginate content that overflows a slide (text gets clipped, tables
    # bleed off the bottom). For these products the DocEditor preview's
    # splitter expands `.pytho-page-break` markers to fill the rest of the
    # current page, forcing the next section onto the next slide-aspect page.
    # DOCX-only products (INTSUM, intelligence_estimate, intelligence_running_
    # estimate, SITREP, etc.) flow continuously — Word handles pagination
    # naturally, and forced breaks make a 7-paragraph analytical narrative
    # look like 7 single-section pages.
    _emit_slide_breaks = schema.get("output_format") == "pptx"
    def _maybe_break():
        return _page_break_html() if _emit_slide_breaks else ""

    # PPTX products center the title on the slide; DOCX flush-left like an
    # AR 25-50 memo head. Header (key/value) block renders below as-is in
    # both cases — no subtitle concatenation, no cover-slide rebuild.
    _h1_style = (S_H1 + ";text-align:center") if _emit_slide_breaks else S_H1
    parts.extend([
        f'<h1 style="{_h1_style}">{_esc(title)}</h1>',
        # Header block is wrapped as a SINGLE block-level marker region
        # tagged `pytho:_header`. The importer reads this as header chrome
        # — individual header-field values within are recovered from
        # inline-SDT spans (when the analyst edits a value, the field
        # value rides in its own inline marker — see _render_header_block).
        _sdt_wrap_html(
            ["_header"],
            "Header",
            _render_header_block(header, schema, footnotes=footnotes,
                                  artifact_id=artifact_id, user_id=user_id,
                                  skip_keys=_header_skip),
        ),
    ])
    # Emit one inline-SDT wrapped <span> per header field at the END of the
    # header block. The header table itself is laid out by
    # _render_header_block; we attach inline markers as a hidden sibling
    # so the importer can recover individual field values without
    # re-parsing the table. The hidden markers render as zero-height spans
    # but Collabora preserves them on round-trip.
    _header_marker_parts = []
    for _hf_name, _hf_spec in (schema.get("header_fields") or {}).items():
        if (_hf_spec or {}).get("view") == "internal":
            # Internal-view fields (artifact-id references) still get a
            # marker so cross-product references round-trip.
            pass
        _hf_label = (_hf_spec or {}).get("render_label") or _hf_name
        _hf_val = header.get(_hf_name) or ""
        if isinstance(_hf_val, (list, dict)):
            _hf_val = json.dumps(_hf_val, default=str)
        _header_marker_parts.append(
            _sdt_inline_wrap_html(["header", _hf_name], _hf_label, _esc(str(_hf_val)))
        )
    if _header_marker_parts:
        parts.append(
            f'<p style="display:none;color:#ffffff;font-size:1pt;line-height:1pt;'
            f'margin:0;padding:0">{"".join(_header_marker_parts)}</p>'
        )
    # Map figures are NOT hardcoded here — they are paragraph_fields with
    # shape="map" placed at their doctrinal position by the schema.
    # Multi-map products declare multiple map fields; products without maps
    # declare none. No more blank-slide-from-empty-map bug.

    paragraph_fields = schema.get("paragraph_fields") or []
    matrix_spec = schema.get("matrix_spec")

    if shape == "row":
        rows = structured_data.get("rows") or []
        section_title = schema.get("published_section_title") or "Entries"
        parts.append(_maybe_break())
        # CRITICAL: do NOT wrap the published table in an outer
        # `<div data-pytho-tag="pytho.rows">`. convert.js's marker
        # injection walks `<w:p>` open/close offsets across the whole
        # document, including the `<w:p>` elements inside `<w:tc>` table
        # cells. A wrapper around a `<table>` therefore stitches a
        # `<w:sdt>` open inside the first cell and the close inside the
        # last cell — producing OOXML where `<w:sdt>` spans `<w:tc>`
        # boundaries (`Opening and ending tag mismatch: tc line 1 and
        # sdtContent`). Word/python-docx parsers reject the file as
        # malformed; the finalize importer returns `parse_failure` and
        # (until B3 lands the fatal-warning fix) silently overwrites
        # schema_state.json with an empty new_sd.
        #
        # The fix is the established list_of_dicts pattern (see
        # `shape == "list_of_dicts"` below at ~line 2034): emit the
        # heading + table UNWRAPPED, then append per-row markers as a
        # hidden block AFTER the table. Each row gets one block-level
        # `pytho.rows[i]` marker carrying inline-wrapped subfield
        # markers — same structure the importer's `rows[<i>].<f>` path
        # grammar (utils/importer.py:484) reads. SDTs live OUTSIDE the
        # table so they never cross `<w:tc>` boundaries.
        parts.append(
            _section_heading(section_title)
            + _render_published_table(rows, schema, footnotes=footnotes,
                                       orientation=orientation)
        )
        # Per-row hidden marker block. Only emit when there are rows AND
        # the schema declares row_fields (otherwise nothing to mark).
        row_specs = schema.get("row_fields") or {}
        if rows and row_specs:
            marker_parts = []
            # All declared row_fields participate in the marker stream —
            # both published and workpaper views. The importer needs to
            # round-trip every field the schema declares, not only the
            # visible columns. Matches the list_of_dicts marker pass
            # which iterates every `item_subfield` regardless of view.
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                inner = []
                for rfn, rspec in row_specs.items():
                    if not rfn:
                        continue
                    sub_shape = (rspec or {}).get("shape", "string")
                    raw = row.get(rfn)
                    if sub_shape == "list_of_dicts" and isinstance(raw, list):
                        nested_subfields = (rspec or {}).get("item_subfields") or []
                        for j, sub_item in enumerate(raw):
                            if not isinstance(sub_item, dict):
                                continue
                            for sub_sf in nested_subfields:
                                sub_sf_name = sub_sf.get("name") or ""
                                if not sub_sf_name:
                                    continue
                                sub_val = sub_item.get(sub_sf_name)
                                if isinstance(sub_val, (list, dict)):
                                    sub_val = json.dumps(sub_val, default=str)
                                inner.append(_sdt_inline_wrap_html(
                                    ["rows", i, rfn, j, sub_sf_name],
                                    sub_sf.get("render_label") or sub_sf_name,
                                    _esc(str(sub_val or "")),
                                ))
                    elif sub_shape == "dict" and isinstance(raw, dict):
                        for k, vv in raw.items():
                            if isinstance(vv, (list, dict)):
                                vv = json.dumps(vv, default=str)
                            inner.append(_sdt_inline_wrap_html(
                                ["rows", i, rfn, k],
                                f"{(rspec or {}).get('render_label') or rfn} / {k}",
                                _esc(str(vv or "")),
                            ))
                    else:
                        val_str = raw
                        if isinstance(val_str, (list, dict)):
                            val_str = json.dumps(val_str, default=str)
                        inner.append(_sdt_inline_wrap_html(
                            ["rows", i, rfn],
                            (rspec or {}).get("render_label") or rfn,
                            _esc(str(val_str or "")),
                        ))
                if inner:
                    marker_parts.append(_sdt_wrap_html(
                        ["rows", i],
                        f"{section_title} #{i + 1}",
                        f"<p style=\"display:none;color:#ffffff;font-size:1pt;"
                        f"line-height:1pt;margin:0;padding:0\">{''.join(inner)}</p>",
                    ))
            if marker_parts:
                parts.append("".join(marker_parts))
        for p in paragraph_fields:
            if (p.get("view", "published")) != "published": continue
            _is_map = p.get("shape") == "map"
            v = structured_data.get(p.get("name"))
            _name = p.get("name") or ""
            _label = p.get("render_label") or _name
            # Map fields render from the per-field PNG path, not from an
            # analytical value — let them through even when value is empty.
            if not _is_map and v in (None, "", [], {}):
                # Emit empty-region marker so the field exists in the marker
                # tree even when absent. Importer reads as field-absent.
                parts.append(_maybe_break())
                parts.append(_sdt_empty_region_html([_name], _label))
                continue
            _rendered = _render_paragraph_field(p, v, footnotes=footnotes,
                                                  parent=structured_data,
                                                  orientation=orientation,
                                                  artifact_id=artifact_id,
                                                  user_id=user_id)
            # Empty render output (e.g. missing map PNG) → emit empty region.
            if not _rendered:
                parts.append(_maybe_break())
                parts.append(_sdt_empty_region_html([_name], _label))
                continue
            parts.append(_maybe_break())
            parts.append(_sdt_wrap_html([_name], _label, _rendered))
        # Workpaper-view row_fields (function/impact/critical_elements/etc.)
        # are doctrinally INTERNAL — they feed the published Justification
        # field via explicit DAG depends_on, but do NOT render in the
        # artifact. FM 3-60 App C: published HVTL is Cat | HVT | Justification.

    elif shape == "lines":
        line_keys = _line_field_keys(structured_data, schema)
        parts.append(_maybe_break())
        parts.append(_section_heading("Report Lines"))
        items = [(_line_label(k, schema), structured_data.get(k), k) for k in line_keys]
        parts.append(_kv_table(items, parent=structured_data, footnotes=footnotes))

    elif shape == "matrix":
        if matrix_spec:
            parts.append(_maybe_break())
            _matrix_field_name = matrix_spec.get("field") or "matrix"
            parts.append(_sdt_wrap_html(
                [_matrix_field_name],
                matrix_spec.get("render_label") or _matrix_field_name,
                _render_matrix_from_spec(structured_data, schema,
                                          footnotes=footnotes,
                                          orientation=orientation),
            ))
        for p in paragraph_fields:
            name = p.get("name")
            if matrix_spec and name == matrix_spec.get("field"): continue
            if (p.get("view", "published")) != "published": continue
            _is_map = p.get("shape") == "map"
            v = structured_data.get(name)
            _label = p.get("render_label") or name
            if not _is_map and v in (None, "", [], {}):
                parts.append(_maybe_break())
                parts.append(_sdt_empty_region_html([name], _label))
                continue
            _rendered = _render_paragraph_field(p, v, footnotes=footnotes,
                                                  parent=structured_data,
                                                  orientation=orientation,
                                                  artifact_id=artifact_id,
                                                  user_id=user_id)
            if not _rendered:
                parts.append(_maybe_break())
                parts.append(_sdt_empty_region_html([name], _label))
                continue
            parts.append(_maybe_break())
            # Mirror of the shape == "paragraph" branch above — see its
            # block-comment for the convert.js / table-spanning-sdt bug.
            if p.get("shape") == "list_of_dicts":
                parts.append(_rendered)
            else:
                parts.append(_sdt_wrap_html([name], _label, _rendered))

    elif shape in ("paragraph", "structured", "header_only"):
        # header_only products (TIDAT, TMP, CDE, BDA-stub, CA-stub) have
        # NO paragraph_fields — the document content IS the header_fields.
        # The header renderer above only emits view="header" fields, so
        # the view="published" header_fields (target_id, target_name,
        # target_category, target_location_mgrs, method_of_engagement,
        # etc.) wouldn't render anywhere without this branch. Iterate the
        # published header_fields here and render each as a "Label: Value"
        # body block so the analyst sees the target identity + engagement
        # decision + validity flags + handling block in the DOCX, not
        # just the session-context header.
        if shape == "header_only" and not paragraph_fields:
            header_specs = schema.get("header_fields") or {}
            header_block = (structured_data or {}).get("header") or {}
            for f, spec in header_specs.items():
                if (spec or {}).get("view") != "published":
                    continue
                v = header_block.get(f)
                # Field absent OR explicitly empty — show the empty region
                # so the analyst knows this slot exists and is gap-noted.
                _label = (spec or {}).get("render_label") or _humanize(f)
                val = v.get("value") if isinstance(v, dict) and "value" in v else v
                val_str = "" if val is None else str(val)
                parts.append(_maybe_break())
                if not val_str:
                    parts.append(_sdt_empty_region_html([f], _label))
                else:
                    # Render as visible "Label: Value" lines (Sdt-wrapped so
                    # the analyst's Collabora edits round-trip back into
                    # structured_data on save). The label inside the Sdt
                    # is the analyst-readable form; without this, only the
                    # raw value renders and the analyst can't tell what
                    # any string is for.
                    parts.append(_sdt_wrap_html(
                        [f], _label,
                        f'<p><strong>{_esc(_label)}:</strong> {_esc(val_str)}</p>'
                    ))
        elif paragraph_fields:
            for p in paragraph_fields:
                if (p.get("view", "published")) != "published": continue
                _is_map = p.get("shape") == "map"
                _name = p.get("name") or ""
                _label = p.get("render_label") or _name
                v = structured_data.get(_name)
                if not _is_map and v in (None, "", [], {}):
                    parts.append(_maybe_break())
                    parts.append(_sdt_empty_region_html([_name], _label))
                    continue
                _rendered = _render_paragraph_field(p, v, footnotes=footnotes,
                                                      parent=structured_data,
                                                      orientation=orientation,
                                                      artifact_id=artifact_id,
                                                      user_id=user_id)
                if not _rendered:
                    parts.append(_maybe_break())
                    parts.append(_sdt_empty_region_html([_name], _label))
                    continue
                parts.append(_maybe_break())
                # Table-producing paragraph_field shapes (list_of_dicts)
                # must NOT be wrapped in an outer pytho-tag div: convert.js's
                # `<w:p>`-offset marker injection would stitch a `<w:sdt>`
                # open inside the first `<w:tc>` cell and close inside the
                # last, producing OOXML where `<w:sdt>` spans `<w:tc>`
                # boundaries (`Opening and ending tag mismatch: tc line 1
                # and sdtContent`). Word / python-docx / Collabora reject
                # the file as corrupt; the finalize importer's parse fails.
                # Same fix as the shape == "row" path above:
                # _render_paragraph_field for list_of_dicts already emits
                # per-item hidden marker blocks AFTER the table carrying
                # the field name + index + per-subfield inline markers, so
                # the importer round-trips the values without the outer
                # `pytho:<name>` wrapper. Other shapes (string/dict/map/
                # range/etc.) emit flow content with no internal table and
                # are safe to wrap.
                if p.get("shape") == "list_of_dicts":
                    parts.append(_rendered)
                else:
                    parts.append(_sdt_wrap_html([_name], _label, _rendered))
        else:
            skip = {"header", "rows", "doctrinal_basis_citation"}
            for k, v in structured_data.items():
                if k in skip or k.startswith("_"): continue
                parts.append(_maybe_break())
                if isinstance(v, str):
                    parts.append(f'<h2 style="{S_H2}">{_esc(_humanize(k))}</h2>'
                                  f'<p>{_esc(v)}</p>')
                else:
                    parts.append(f'<h2 style="{S_H2}">{_esc(_humanize(k))}</h2>'
                                  f'{_render_inferred(v, 2, orientation=orientation)}')

    else:
        skip = {"header", "rows", "doctrinal_basis_citation"}
        for k, v in structured_data.items():
            if k in skip or k.startswith("_"): continue
            parts.append(_page_break_html())
            if isinstance(v, str):
                parts.append(f'<h2 style="{S_H2}">{_esc(_humanize(k))}</h2>'
                              f'<p>{_esc(v)}</p>')
            else:
                parts.append(f'<h2 style="{S_H2}">{_esc(_humanize(k))}</h2>'
                              f'{_render_inferred(v, 2, orientation=orientation)}')

    # Sources section (numbered footnotes) — per-claim data citations.
    fn_section = footnotes.render_section()
    if fn_section:
        parts.append(_maybe_break())
        parts.append(fn_section)

    # Bottom body banner dropped — see top of body for rationale.

    # NOTE: the doctrinal-basis citation is intentionally not rendered. The
    # doctrinal frame is implicit in the product type (a WEM is a WEM); the
    # writer still uses `doctrine.citation` in schemas for prompt grounding,
    # but the analyst never sees that line in the rendered output.

    parts.append("</body></html>")
    return "".join(parts)

# ---------------------------------------------------------------------------
# DOCX (via convert.js)
# ---------------------------------------------------------------------------
def html_to_docx(html_str: str, out_path: str, classification: str = "UNCLASSIFIED",
                 orientation: str = "portrait", **_legacy_kwargs) -> str:
    # DOCX header/footer match DocEditor's `defaultHeaderStyle`: plain
    # 11pt Arial gray text, centered, no fill. The DocEditor has had this
    # header system since the editor-overhaul (commit 7edfdf4). A
    # filled-color banner (fillColor green) was added later in the export
    # path — it duplicated the existing system AND painted a green strip
    # over the clean header. Reverted: no fillColor, no bold, no
    # white-on-color, just the same text styling the analyst sees in the
    # editor.
    #
    # The per-page doctrinal-citation footer is also intentionally
    # absent — the doctrinal basis is conveyed by the product type itself.
    # `**_legacy_kwargs` keeps older callers (which still pass
    # `doctrinal_citation=`) from breaking until they are updated.
    # imageBaseDir is the root the converter joins against `<img src>` values.
    # The exporter writes map figures as `<img src="/files/<owner>/<aid>/x.jpg">`
    # (URL form, served by the SPA from the uploads dir at runtime). For DOCX
    # rendering the convert.js side must inline the bytes — passing the
    # uploads root as imageBaseDir lets `_loadImageBuffer` strip the /files/
    # prefix and read the JPG directly. Without this every map image in a
    # DOCX (Threat Model is the only schema with maps in DOCX today) lands
    # as a broken URL reference: convert.js skips the embed, the DOCX zip
    # has zero word/media/* parts, and Collabora renders the doc with the
    # map missing because it can't resolve /files/... from inside its
    # container. UPLOAD_DIR is resolved to absolute so the converter
    # (running in a different working directory) finds the right tree.
    from handlers.product import UPLOAD_DIR as _UPLOAD_DIR
    payload = {
        "html": html_str,
        "outputPath": os.path.abspath(out_path),
        "imageBaseDir": str(Path(_UPLOAD_DIR).resolve()),
        # Two-line header: classification token on line 1, exercise marking on
        # line 2. The `\n` is the DocEditor/convert.js convention (see line
        # ~2605 default state `'UNCLASSIFIED\nFOR EXERCISE USE ONLY'`) — the
        # ' // ' inline form was a render bug. Storage strings elsewhere
        # remain unchanged; this is the render-side fix only.
        "header": "UNCLASSIFIED\nFOR EXERCISE USE ONLY",
        "headerStyle": {
            "position": "header", "align": "center",
            "bold": False, "italic": False, "underline": False,
            "fontSize": "11pt", "fontFamily": "Arial, sans-serif",
            "textColor": "#808080",
        },
        "orientation": orientation,
        # Override Times New Roman default — body Arial matches the inline
        # font-family declared on every cell/paragraph in `_render_*` and avoids
        # Word falling back to Times for headings and cells with no inline font.
        "defaultFont": "Arial",
    }
    proc = subprocess.run(
        ["node", DOCX_CONVERTER],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
        cwd=os.path.dirname(DOCX_CONVERTER),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docx convert failed: {proc.stderr}")
    last = proc.stdout.strip().split("\n")[-1]
    response = json.loads(last)
    if not response.get("success"):
        raise RuntimeError(f"docx convert error: {response.get('error')}")
    return out_path

# ---------------------------------------------------------------------------
# PPTX canonical-on-disk marker tree.
#
# After all visible slides are emitted, render_artifact_pptx appends a
# single hidden marker slide whose shapes each carry one (field_path,
# value) pair from the structured_data dict — shape.name encodes the
# field path (`pytho.<dotted_path>`), the text frame carries the value.
#
# This is the gap-free invariant's PPTX implementation: every schema-
# declared field has at least one marker on disk that the importer can
# walk. Per-shape markers on visible slides (Threat COA bespoke slide,
# per-product render branches) handle analyst-edit round-trips; the
# hidden marker slide is the shadow registry the importer falls back on
# when a visible-slide marker has been edited away.
# ---------------------------------------------------------------------------
def _emit_pptx_marker_tree(slide, structured_data: dict, schema: dict) -> None:
    """Emit one TextBox per (path, value) leaf in structured_data, named
    `pytho.<path>` and containing the value as text. Tiny textboxes at
    (0, 0) sized 0.01×0.01 inch — visually inert, machine-readable."""
    from pptx.util import Inches, Pt, Emu  # type: ignore

    def _is_scalar(v):
        return v is None or isinstance(v, (str, int, float, bool))

    def _walk(prefix, node):
        if _is_scalar(node):
            _emit(prefix, node)
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.startswith("_"):
                    continue
                _walk(prefix + [k], v)
            return
        if isinstance(node, list):
            for i, v in enumerate(node):
                _walk(prefix + [i], v)
            return
        # Fallback: stringify.
        _emit(prefix, json.dumps(node, default=str))

    def _emit(prefix, value):
        try:
            name = _pptx_shape_name(prefix)
        except Exception:
            return
        # Cap to 250 chars — PPTX shape name has no hard limit but extremely
        # long names slow openxml validators. Long names truncated; the
        # actual VALUE goes in the text frame (no length issue).
        if len(name) > 250:
            return
        try:
            tx = slide.shapes.add_textbox(Emu(0), Emu(0), Emu(9525), Emu(9525))
            tx.name = name
            tf = tx.text_frame
            p = tf.paragraphs[0]
            r = p.add_run()
            r.text = "" if value is None else str(value)
            try:
                r.font.size = Pt(1)
            except Exception:
                pass
        except Exception:
            pass

    # Walk header + paragraph_fields tree.
    header = (structured_data or {}).get("header") or {}
    for k, v in header.items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        _walk(["header", k], v)
    for k, v in (structured_data or {}).items():
        if k in ("header", "_provenance", "_extra") or (isinstance(k, str) and k.startswith("_")):
            continue
        _walk([k], v)


# ---------------------------------------------------------------------------
# PPTX renderer (schema-driven). Produces:
#   - Title slide with header kv table
#   - Per-paragraph_field slide:
#       * string             → text block (with footnote marker)
#       * list_of_dicts      → proper M×N table (rows=items, cols=item_subfields,
#                              skipping _provenance), with footnote markers
#       * dict               → kv table (label, value), with footnote markers
#       * grid_table         → matrix grid
#   - Per-row slide for row-shape products (HVTL/HPTL/CARVER/Threat Overlay) — kv block
#   - Per-line slide for SITREP
#   - Sources slide (numbered footnotes)
#   - Doctrinal basis slide
# Cell values strip _provenance keys; provenance citations turn into superscript
# footnote markers ("¹") via the same FootnoteRegistry used by the HTML/DOCX
# path. Single-citation row-level provenance applies one marker per row.
# ---------------------------------------------------------------------------
def render_artifact_pptx(artifact_type: str, structured_data: dict, out_path: str,
                          gaps: Optional[list] = None,
                          artifact_id: str = "",
                          user_id: str = "") -> str:
    import time as _t_mod
    _pptx_t0 = _t_mod.monotonic()
    import logging as _logging
    _pptx_log = _logging.getLogger("utils.exporter")
    _pptx_log.info("PPTX START type=%s artifact_id=%s rows=%d",
                    artifact_type, artifact_id,
                    len((structured_data or {}).get("rows") or []))
    # `user_id` mirrors render_artifact_html and is required by shape="map"
    # paragraph_fields — _emit_map_slide builds /files/<uid>/<aid>/<name>.jpg
    # to locate the headless render output. Earlier this defaulted to
    # "anonymous" (the empty-string fallback), the path 404'd, and the map
    # slide was silently skipped from every PPTX export. Same pattern as
    # the HTML path.
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    schema = PRODUCT_SCHEMAS.get(artifact_type, {})
    shape = schema.get("shape", "")
    header = structured_data.get("header") or {}
    # Mirror the HTML path's artifact_id resolution: caller may pre-pass it
    # from the wrapping entry, otherwise fall back to structured_data.artifact_id
    # (set by the runner for map-bearing products).
    if not artifact_id:
        artifact_id = str(structured_data.get("artifact_id") or "")
    rows = structured_data.get("rows") or []
    # Map embed sidecar accumulator. Each shape="map" paragraph_field that
    # `_emit_map_slide` actually emits appends one entry here. After
    # `pres.save(out_path)` we write the list to <artifact_dir>/map_embeds.json
    # so the LiveEditor's map-button overlay subsystem can read it on doc
    # load. Empty list = no maps in this artifact (sidecar still written so
    # the frontend gets a deterministic 200 instead of 404). The key
    # fields: map_id, label, slide_index (0-based), twips bbox, bbox_lonlat,
    # image_part (zip-relative path inside the .pptx for the in-zip
    # replace done by handlers/artifact_map_refresh.refresh_map),
    # image_filename (alongside-deliverable PNG path for the dashboard
    # preview re-render path).
    _map_embeds: list[dict] = []
    # `doctrinal_basis_citation` is still on `structured_data` for writer
    # grounding but is NOT rendered as a slide footer band — see the doctrine
    # footer removal in render_artifact_html for the matching rationale.
    classification = header.get("classification") or ""
    long_name = PRODUCT_LONG_NAMES.get(artifact_type, artifact_type)
    paragraph_fields = schema.get("paragraph_fields") or []
    row_specs = schema.get("row_fields") or {}
    matrix_spec = schema.get("matrix_spec") or {}
    fn = FootnoteRegistry()

    pres = Presentation()
    pres.slide_width = Inches(13.33); pres.slide_height = Inches(7.5)
    SLIDE_W = pres.slide_width; SLIDE_H = pres.slide_height

    BLACK = RGBColor(0x00, 0x00, 0x00)
    GRAY_HEADER = RGBColor(0xD9, 0xD9, 0xD9)
    GRAY_LABEL = RGBColor(0xF2, 0xF2, 0xF2)
    DARK_BAR = RGBColor(0x40, 0x40, 0x40)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    FOOTNOTE_BLUE = RGBColor(0x00, 0x33, 0xAA)
    META_BAND_FILL = RGBColor(0xEE, 0xEE, 0xEE)
    # DoD / FM 6-99 classification color palette — filled banner strips, white
    # bold text. Per CDSE classification-marking standards.
    _CLASS_PALETTE = {
        "UNCLASSIFIED":   (RGBColor(0x00, 0x7A, 0x33), WHITE),
        "U":              (RGBColor(0x00, 0x7A, 0x33), WHITE),
        "CUI":            (RGBColor(0x50, 0x2B, 0x85), WHITE),
        "CONTROLLED":     (RGBColor(0x50, 0x2B, 0x85), WHITE),
        "CONFIDENTIAL":   (RGBColor(0x00, 0x33, 0xA0), WHITE),
        "C":              (RGBColor(0x00, 0x33, 0xA0), WHITE),
        "TOP SECRET/SCI": (RGBColor(0xFF, 0xC7, 0x2C), BLACK),
        "TS/SCI":         (RGBColor(0xFF, 0xC7, 0x2C), BLACK),
        "TOP SECRET":     (RGBColor(0xFF, 0x82, 0x00), WHITE),
        "TS":             (RGBColor(0xFF, 0x82, 0x00), WHITE),
        "SECRET":         (RGBColor(0xC8, 0x10, 0x2E), WHITE),
        "S":              (RGBColor(0xC8, 0x10, 0x2E), WHITE),
    }
    def _classification_palette(text: str):
        t = (text or "UNCLASSIFIED").upper().strip()
        # Sort keys longest-first so "TOP SECRET/SCI" matches before "TOP SECRET"
        for key in sorted(_CLASS_PALETTE.keys(), key=len, reverse=True):
            if t == key or t.startswith(key + "/") or t.startswith(key + " ") or t.startswith(key + "//"):
                return _CLASS_PALETTE[key]
        return _CLASS_PALETTE["UNCLASSIFIED"]
    CLASS_FILL, CLASS_TXT = _classification_palette(classification)

    def _add_marker_run(paragraph, citation_text: str):
        """Add a superscript footnote marker after a cell's main text.
        Rendered in FOOTNOTE_BLUE so it reads as a citation, not as data.
        Military writing convention places the marker after the cell's
        terminal punctuation. We use OOXML's `baseline` attribute on
        rPr — python-pptx's public Font API doesn't expose superscript."""
        marker = fn.add_plain(citation_text)
        if not marker: return
        # Superscript marker: thin hair-space prelude (em-space U+2003) so
        # the marker doesn't touch the prose's terminal punctuation.
        prelude = paragraph.add_run(); prelude.text = " "
        prelude.font.size = Pt(9); prelude.font.color.rgb = FOOTNOTE_BLUE
        prelude.font.name = "Arial"
        run = paragraph.add_run()
        run.text = marker
        run.font.size = Pt(7)
        run.font.color.rgb = FOOTNOTE_BLUE
        run.font.name = "Arial"
        # OOXML superscript baseline. python-pptx's Font API doesn't expose
        # superscript; mutate the rPr element directly. 30000 = 30% raise.
        try:
            rPr = run._r.get_or_add_rPr()
            rPr.set("baseline", "30000")
        except Exception:
            pass

    def _set_cell(cell, text, parent: Optional[dict] = None, field: str = "",
                    bold=False, font_size=10, fill=WHITE, font_name="Arial"):
        """Set a cell's text + optional footnote marker pulled from
        parent._provenance[field]. Markers are suppressed on absent values
        ("—", None, "", False) — citing a missing data point is noise.

        Cell margins: 6pt top/bottom × 10pt left/right to match the HTML/DOCX
        table cell padding (S_TD / S_TH_HEADER_ROW). Old 3pt × 5pt left content
        flush against borders on dense WEM/CCA tables.

        Borders: python-pptx's default table style paints WHITE 12240-EMU
        borders on every cell via <a:lnL/R/T/B>. They're invisible against
        the default white cell fill but each adds ~340 chars of XML; on a
        211-row × 10-col threat-dispositions table that's ~700KB of
        invisible markup that Collabora has to parse on every load (root
        cause of the 30-45s 'this product takes forever to open' bug).
        Override at the tcPr level to <a:lnL/R/T/B><a:noFill/>: same
        visual result, ~10x smaller XML."""
        cell.fill.solid(); cell.fill.fore_color.rgb = fill
        tf = cell.text_frame; tf.margin_top = Pt(6); tf.margin_bottom = Pt(6)
        tf.margin_left = Pt(10); tf.margin_right = Pt(10); tf.word_wrap = True
        p = tf.paragraphs[0]
        is_absent = text in (None, "", False)
        r = p.add_run()
        r.text = "—" if is_absent else str(text)
        r.font.size = Pt(font_size); r.font.bold = bold; r.font.color.rgb = BLACK
        r.font.name = font_name
        if parent is not None and field and not is_absent:
            cite = _provenance_for(parent, field)
            if cite: _add_marker_run(p, cite)
        # Replace python-pptx's default white-fill cell borders with noFill
        # on the tcPr element directly. python-pptx doesn't expose cell
        # borders through the public API, so we operate on the lxml tree.
        try:
            from pptx.oxml.ns import qn
            tcPr = cell._tc.get_or_add_tcPr()
            for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
                ln = tcPr.find(qn(tag))
                if ln is None:
                    ln = tcPr.makeelement(qn(tag), {})
                    tcPr.insert(0, ln)
                for child in list(ln):
                    ln.remove(child)
                ln.set("w", "0")
                noFill = ln.makeelement(qn("a:noFill"), {})
                ln.append(noFill)
        except Exception:
            # lxml/python-pptx internals can drift across versions; the
            # cell still renders correctly with python-pptx's default
            # white borders, just larger XML.
            pass

    # Vertical layout (in inches, top-to-bottom):
    #   0.00 - 0.32   : top classification banner (filled color)
    #   0.32 - 0.62   : header metadata band (compact strip)
    #   0.65 - 1.05   : section title bar (when title provided)
    #   1.12 - 7.15   : content area (extended now that doctrine footer band
    #                   was removed — recovers ~0.25" of usable slide height)
    #   7.18 - 7.50   : bottom classification banner (filled color)
    BANNER_H = Inches(0.32)
    META_BAND_H = Inches(0.30)
    SECTION_BAR_TOP = Inches(0.65)
    SECTION_BAR_H = Inches(0.40)

    def _classification_strip(slide):
        """Top + bottom filled-color banners. Color + text color come from
        the doctrine palette (UNCLASSIFIED = green, SECRET = red, etc.) per
        FM 6-99 / DoDM 5200.01 / CDSE marking guide."""
        for top in (Emu(0), SLIDE_H - BANNER_H):
            bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(0), top,
                                           SLIDE_W, BANNER_H)
            bar.line.fill.background()
            bar.fill.solid(); bar.fill.fore_color.rgb = CLASS_FILL
            bar.shadow.inherit = False
            tf = bar.text_frame
            tf.margin_top = Pt(2); tf.margin_bottom = Pt(2)
            tf.margin_left = Pt(4); tf.margin_right = Pt(4)
            p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
            r = p.add_run(); r.text = classification
            r.font.size = Pt(14); r.font.bold = True; r.font.color.rgb = CLASS_TXT
            r.font.name = "Arial"

    def _meta_band(slide):
        """Compact metadata strip below the top classification banner.

        Skipped when fewer than two substantive entries would render — a band
        showing only "Prep: G-2" is visual chrome with no information density.
        Doctrinal threshold: at least one of (DTG, Phase, AOR, Threat COA) +
        one supporting entry; otherwise the band sits dark and noisy."""
        header_specs = schema.get("header_fields") or {}
        # Pull from BOTH the schema-defined fields AND the actual state header
        # — state may carry common metadata (dtg/phase/aor) that the schema
        # doesn't declare per-artifact (BDA uses engagement_dtg, etc., but
        # state often also carries the canonical dtg/phase keys).
        order_keys = list(header_specs.keys()) + [k for k in header.keys()
                                                    if k not in header_specs]
        preferred = ["dtg", "engagement_dtg", "reporting_dtg",
                     "phase", "phase_or_critical_event", "area_of_operations",
                     "threat_coa", "target_number", "target_description",
                     "target_location_mgrs", "preparing_unit",
                     "approving_commander", "reporting_agency",
                     "sitemp_artifact_id", "threat_overlay_artifact_id",
                     "threat_coa_statement_artifact_id",
                     "hvtl_artifact_id", "carver_artifact_id", "hptl_artifact_id",
                     "tmp_artifact_id"]
        ordered = [k for k in preferred if k in order_keys]
        ordered += [k for k in order_keys if k not in ordered]
        parts = []
        # Pre-check: does Threat COA already contain the AOR string? If so,
        # we'll suppress AOR (it duplicates content in a compact strip).
        coa_text = str(header.get("threat_coa") or "").lower()
        ao_text = str(header.get("area_of_operations") or "").strip().lower()
        ao_in_coa = bool(ao_text) and ao_text in coa_text
        for k in ordered:
            if k in _HEADER_EXCLUDE_KEYS or (k or "").startswith("_"): continue
            if k == "area_of_operations" and ao_in_coa: continue
            # Spatial overlay fields are surfaced as the map figure, not as
            # values in the compact PPTX meta band — same skip rule as the
            # HTML header block in _render_header_block.
            if (k or "").startswith("spatial_overlay"): continue
            v = header.get(k)
            if v in (None, ""): continue
            spec = header_specs.get(k) or {}
            lbl = (spec.get("render_label") or _humanize(k)).strip()
            lbl_compact = {
                "Phase or Critical Event": "Phase",
                "Area of Operations": "AOR",
                "Preparing Unit": "Prep",
                "Approving Commander": "Approver",
                "Threat COA": "COA",
                "SITEMP Reference": "SITEMP",
                "Threat Overlay Reference": "Threat Overlay",
                "Threat COA Statement Reference": "Threat COA Statement",
                "HVTL Reference": "HVTL",
                "CARVER Reference": "CARVER",
                "HPTL Reference": "HPTL",
                "Pre-strike JDPI Reference (TMP)": "TMP",
                "Mensuration DTG": "DTG",
                "Engagement DTG (when struck)": "Engaged",
                "Acquisition DTG": "Acq",
                "Reporting DTG": "DTG",
                "Target Number": "TGT",
                "Target Description": "TGT",
                "Target Location (MGRS)": "MGRS",
                "Reporting Agency": "Rep",
                "Assessing Analyst": "Analyst",
            }.get(lbl, lbl)
            parts.append((lbl_compact, str(v)))
        # Dedupe by (compact_label, value) — when a state carries both `phase`
        # and `phase_or_critical_event` with identical values, only emit one
        # "Phase:" entry; same for any other label collision.
        seen_pairs = set()
        deduped = []
        for lbl, val in parts:
            key = (lbl.lower(), val.strip())
            if key in seen_pairs: continue
            seen_pairs.add(key)
            deduped.append((lbl, val))
        parts = deduped
        if len(parts) < 2: return  # not enough density — drop the band
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(0), BANNER_H,
                                       SLIDE_W, META_BAND_H)
        bar.line.fill.background()
        bar.fill.solid(); bar.fill.fore_color.rgb = META_BAND_FILL
        bar.shadow.inherit = False
        tf = bar.text_frame
        tf.margin_top = Pt(2); tf.margin_bottom = Pt(2)
        tf.margin_left = Pt(10); tf.margin_right = Pt(10)
        p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
        for i, (lbl, val) in enumerate(parts):
            if i:
                rsep = p.add_run(); rsep.text = "   |   "
                rsep.font.size = Pt(10); rsep.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
                rsep.font.name = "Arial"
            rl = p.add_run(); rl.text = f"{lbl}: "
            rl.font.size = Pt(10); rl.font.bold = True; rl.font.color.rgb = BLACK
            rl.font.name = "Arial"
            rv = p.add_run(); rv.text = val
            rv.font.size = Pt(10); rv.font.color.rgb = BLACK
            rv.font.name = "Arial"

    def _section_bar(slide, text, top=None):
        if top is None: top = SECTION_BAR_TOP
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.4), top,
                                       SLIDE_W - Inches(0.8), SECTION_BAR_H)
        bar.line.color.rgb = BLACK; bar.line.width = Pt(0.5)
        bar.fill.solid(); bar.fill.fore_color.rgb = DARK_BAR
        bar.shadow.inherit = False
        tf = bar.text_frame; tf.margin_top = Pt(3); tf.margin_bottom = Pt(3)
        p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
        r = p.add_run(); r.text = text
        r.font.size = Pt(15); r.font.bold = True; r.font.color.rgb = WHITE
        r.font.name = "Arial"

    def _new_slide(title: Optional[str] = None, with_meta_band: bool = True,
                    **_legacy_kwargs):
        # Body classification strips + meta-band ribbon dropped — chrome was
        # noisy and the pipe-joined ribbon read as one long unreadable string
        # in pptx. Classification is carried by the slide page-header (set
        # globally by python-pptx options below). Per-slide header context
        # lives in the cover slide's k/v block, not on every page.
        slide = pres.slides.add_slide(pres.slide_layouts[6])
        if title is not None:
            _section_bar(slide, title.upper())
        return slide

    # ---- Title slide -----------------------------------------------------
    def _title_slide():
        # Cover slide — centered long-name title, then the header k/v block
        # rendered as a small table below. No banners, no meta band, no
        # subtitle pipe-concatenation. Mirrors the DOCX cover sheet so
        # analysts see the same header chrome in both deliverables.
        slide = pres.slides.add_slide(pres.slide_layouts[6])
        tx = slide.shapes.add_textbox(Inches(0.5), Inches(2.4),
                                        Inches(12.3), Inches(1.3))
        tf = tx.text_frame; tf.word_wrap = True
        p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = long_name.upper()
        r.font.size = Pt(40); r.font.bold = True; r.font.color.rgb = BLACK
        r.font.name = "Arial"
        rule = slide.shapes.add_connector(1, Inches(2.0), Inches(3.75),
                                            Inches(11.33), Inches(3.75))
        rule.line.color.rgb = BLACK; rule.line.width = Pt(1.5)
        # Header k/v block below the title — bold label : value, one row
        # per header field, k/v table semantics. Same data as the DOCX
        # _render_header_block, no concatenation.
        header_specs = schema.get("header_fields") or {}
        kv_items = []
        for fname, spec in header_specs.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("view") not in ("header", None):
                continue
            if fname in _HEADER_EXCLUDE_KEYS or fname.startswith("spatial_overlay"):
                continue
            val = header.get(fname)
            if val in (None, "", False):
                continue
            kv_items.append((_label_for_field(fname, spec), str(val)))
        if kv_items:
            _render_kv(slide, kv_items, parent=header,
                        top=Inches(4.0), left=Inches(2.5),
                        width=Inches(8.3), max_height=Inches(2.8))

    # ---- Per-shape rendering ---------------------------------------------
    def _render_string(slide, value, parent: Optional[dict], field: str):
        tx = slide.shapes.add_textbox(Inches(0.5), Inches(1.15), Inches(12.3), Inches(5.65))
        tf = tx.text_frame; tf.word_wrap = True
        p = tf.paragraphs[0]
        r = p.add_run()
        r.text = "—" if value in (None, "", False) else str(value)
        r.font.size = Pt(13); r.font.color.rgb = BLACK
        if parent is not None and field:
            cite = _provenance_for(parent, field)
            if cite: _add_marker_run(p, cite)

    def _render_kv(slide, items, parent: Optional[dict] = None,
                    top=Inches(1.15), left=Inches(0.5),
                    width=Inches(12.3), max_height=Inches(5.65)):
        """items: list of (label, value, field) triples. Row heights are fixed
        per-row so a short list does not stretch into tall, empty rows
        (the most common visual eyesore on PPTX detail slides)."""
        if not items: return
        n = len(items)
        # Fixed row height, 0.42" minimum; expand up to fit max_height when
        # there are few items, but never grow beyond ~0.65" — past that the
        # rows look empty. Cast to int (EMU) because Inches/int → float and
        # python-pptx row heights are integral EMU values.
        row_h = int(min(Inches(0.65), max(Inches(0.42), max_height / n)))
        t = slide.shapes.add_table(n, 2, left, top, width, row_h * n).table
        t.columns[0].width = Inches(3.2); t.columns[1].width = width - Inches(3.2)
        for i, it in enumerate(items):
            t.rows[i].height = row_h
            if len(it) == 3:
                k, v, fname = it
            else:
                k, v = it; fname = ""
            _set_cell(t.cell(i, 0), k, bold=True, fill=GRAY_LABEL, font_size=12)
            _set_cell(t.cell(i, 1), v, parent=parent, field=fname, font_size=12)

    def _render_list_of_dicts(slide, value: list, item_subfields: list,
                                top=Inches(1.15), left=Inches(0.4),
                                width=Inches(12.5), max_height=Inches(5.65),
                                group_by: Optional[str] = None,
                                group_collapse_map: Optional[dict] = None,
                                group_collapse_order: Optional[list] = None,
                                group_collapse_labels: Optional[dict] = None,
                                group_label_override: Optional[str] = None):
        """Proper M×N table: M = items, N = visible item_subfields (skip _provenance).
        Columns are width-distributed via _column_width_dxa (same heuristic the
        DOCX path uses) so narrative cols get more space than score/id cols.
        Header row gets a fixed 0.42" height; body rows split the remainder so
        the header is never absurdly tall on short tables.

        Empty-column suppression: when a column's value is None / "" / "N/A"
        (case-insensitive) / False across EVERY row, the column is dropped.
        Doctrinally the HVI ID column should not render when no row is an
        HVI; this generalises the rule.

        Row grouping: when `group_by` is set, rows are sorted by the named
        field and a shaded "category" header row is inserted before each new
        group. The grouping column itself is then dropped from the body
        cells (the group header carries it)."""
        if not value: return
        cols = []
        if item_subfields:
            for f in item_subfields:
                if isinstance(f, dict):
                    if f.get("name") == "_provenance": continue
                    if f.get("shape") in ("list_of_dicts", "dict"): continue
                    # view:"internal" / "workpaper" item_subfields are
                    # cross-row linkage fields, not published columns.
                    # Mirror the DOCX/HTML _list_of_dicts_table filter.
                    if (f.get("view") or "published") not in ("published", None):
                        continue
                    cols.append((f["name"], f.get("render_label") or _humanize(f["name"]), f))
                else:
                    if f == "_provenance": continue
                    cols.append((f, _humanize(f), {}))
        else:
            seen = []
            for it in value:
                if isinstance(it, dict):
                    for k in it.keys():
                        if k not in seen and not k.startswith("_"):
                            seen.append(k)
            cols = [(k, _humanize(k), {}) for k in seen]
        if not cols: return
        # Empty-column suppression — drop columns whose every row value reads
        # as absent. "N/A" and "—" both count as absent. Bool False counts as
        # absent. Score columns are NEVER dropped (every CARVER row scores 0
        # but the column must show), neither are columns marked required:True.
        def _is_absent(v) -> bool:
            if v in (None, "", False): return True
            sv = str(v).strip().lower()
            return sv in ("", "n/a", "—", "-", "nstr", "tbd")
        keep_cols = []
        for name, lbl, spec in cols:
            spec = spec or {}
            # Never drop score / required-True columns; the table's purpose
            # is to show those even when sparse.
            if spec.get("required") is True or "score" in name.lower():
                keep_cols.append((name, lbl, spec)); continue
            if any(not _is_absent((it or {}).get(name)) for it in value if isinstance(it, dict)):
                keep_cols.append((name, lbl, spec))
        cols = keep_cols or cols  # fallback: keep all if everything looked empty
        n_rows = len(value) + 1
        n_cols = len(cols)
        # Resolve row sequence + optional grouping. When `group_by` is set,
        # rows are sorted by that field and a "category" header row is
        # inserted before each new group. The group_by column is then
        # dropped from the body (its info is in the group header).
        grouping_active = bool(group_by) and any(
            isinstance(it, dict) and (it or {}).get(group_by) for it in value
        )

        def _collapsed_key(cat_value):
            """Apply the optional group_collapse_map (FM 3-60 category →
            ADP 3-0 warfighting function, etc.). Returns the bucket key the
            row belongs to in the published view."""
            if not cat_value:
                return None
            if group_collapse_map:
                return group_collapse_map.get(cat_value)
            return cat_value

        def _bucket_label(key):
            if group_collapse_labels and key in group_collapse_labels:
                return group_collapse_labels[key]
            if key == "other":
                return "Other"
            return _humanize(str(key))

        if grouping_active:
            # When a collapse map is declared, bucket by warfighting function
            # (or whatever destination space the schema specifies) instead of
            # by the raw FM 3-60 category. Preserves doctrinal Fig 6-9 layout.
            seen_categories = []
            bucket_for_row: list = []
            for it in value:
                if not isinstance(it, dict):
                    bucket_for_row.append(None)
                    continue
                raw = it.get(group_by) or ""
                if group_collapse_map:
                    bk = _collapsed_key(raw)
                    if bk is None and raw:
                        bk = "other"
                else:
                    bk = raw or "—"
                bucket_for_row.append(bk)
                if bk and bk not in seen_categories:
                    seen_categories.append(bk)
            if group_collapse_order:
                ordered_keys = [k for k in group_collapse_order if k in seen_categories]
                for k in seen_categories:
                    if k not in ordered_keys:
                        ordered_keys.append(k)
                seen_categories = ordered_keys
            ordered_rows = []
            for cat in seen_categories:
                for idx, it in enumerate(value):
                    if not isinstance(it, dict):
                        continue
                    if bucket_for_row[idx] == cat:
                        ordered_rows.append(it)
            # Drop the group_by column from the visible columns; the category
            # header row carries the label so showing it again per row is
            # noise.
            cols = [(name, lbl, sub) for name, lbl, sub in cols if name != group_by]
            n_cols = len(cols)
        else:
            ordered_rows = list(value)
            seen_categories = []

        # ATP 2-01.3 Fig 6-9 published HVTL layout: when grouping is active
        # AND exactly one value column remains, render a 2-col bullet table
        # — group label in column 1, bulleted list of values in column 2 —
        # to mirror the doctrinal figure. The HTML preview already does this
        # via _render_published_table's 1-keep branch; the PPTX path now
        # matches so DOCX → preview side-by-side comparisons stay aligned.
        if grouping_active and len(cols) == 1:
            value_field, value_lbl, value_spec = cols[0]
            group_label_lbl = group_label_override or "Group"
            # Filter out groups that have no row with a non-empty value.
            groups_with_items = []
            for cat in seen_categories:
                rows_in_cat = []
                for idx, it in enumerate(value):
                    if not isinstance(it, dict):
                        continue
                    if bucket_for_row[idx] != cat:
                        continue
                    raw_v = it.get(value_field)
                    if raw_v in (None, "", False):
                        continue
                    rows_in_cat.append(it)
                if rows_in_cat:
                    groups_with_items.append((cat, rows_in_cat))
            n_group_rows = len(groups_with_items)
            n_rows = 1 + n_group_rows
            header_row_h = int(Inches(0.42))
            body_remaining = int(max_height) - header_row_h
            # Variable row height — taller groups get more space (proportional
            # to bullet count, clamped).
            total_items = sum(len(rs) for _, rs in groups_with_items) or 1
            avg_row_h = int(max(Inches(0.5),
                                  min(Inches(2.5), body_remaining / max(n_group_rows, 1))))
            total_h = header_row_h + avg_row_h * n_group_rows
            t = slide.shapes.add_table(n_rows, 2, left, top, width, total_h).table
            from pptx.util import Emu
            total_emu = int(width)
            t.columns[0].width = Emu(int(total_emu * 0.30))
            t.columns[1].width = Emu(int(total_emu * 0.70))
            t.rows[0].height = header_row_h
            _set_cell(t.cell(0, 0), group_label_lbl, bold=True,
                      fill=GRAY_HEADER, font_size=11)
            _set_cell(t.cell(0, 1), value_lbl, bold=True,
                      fill=GRAY_HEADER, font_size=11)
            row_idx = 1
            for cat, rows_in_cat in groups_with_items:
                # Variable height per group so bigger lists don't truncate.
                est_h = int(max(Inches(0.5), min(Inches(2.0),
                                                   Inches(0.32) * len(rows_in_cat) + Inches(0.18))))
                t.rows[row_idx].height = est_h
                _set_cell(t.cell(row_idx, 0), _bucket_label(cat), bold=True,
                          fill=RGBColor(0xF2, 0xF2, 0xF2), font_size=11)
                # Bullet list in column 2.
                bullet_lines = []
                for r in rows_in_cat:
                    v = _humanize_enum_value(r.get(value_field), value_spec)
                    if v in (None, ""):
                        continue
                    bullet_lines.append(f"• {v}")
                _set_cell(t.cell(row_idx, 1), "\n".join(bullet_lines), font_size=11)
                row_idx += 1
        else:
            # Build table rows: header + (optional group bands + body rows).
            # We expand n_rows / n_cols accordingly.
            n_data_rows = len(ordered_rows)
            n_group_rows = len(seen_categories) if grouping_active else 0
            n_rows = 1 + n_group_rows + n_data_rows
            # Header row: fixed height. Group rows: ~0.36" each. Body rows: fill
            # remaining max_height, clamped to a sensible minimum.
            header_row_h = int(Inches(0.42))
            group_row_h = int(Inches(0.36)) if grouping_active else 0
            body_remaining = int(max_height) - header_row_h - group_row_h * n_group_rows
            body_row_h = int(max(Inches(0.32), min(Inches(0.7), body_remaining / max(n_data_rows, 1))))
            total_h = header_row_h + group_row_h * n_group_rows + body_row_h * n_data_rows
            t = slide.shapes.add_table(n_rows, n_cols, left, top, width, total_h).table
            # Column widths — _column_width_dxa returns DXA (twentieths-of-point).
            col_tuples = [(name, lbl, sub) for name, lbl, sub in cols]
            from pptx.util import Emu
            total_emu = int(width)
            dxa_widths = _column_width_dxa(col_tuples, "landscape")
            dxa_total = sum(dxa_widths) or 1
            for j, dxa in enumerate(dxa_widths):
                t.columns[j].width = Emu(int(total_emu * dxa / dxa_total))
            # Header row — slightly larger font, bold, gray.
            t.rows[0].height = header_row_h
            for j, (_, lbl, _) in enumerate(cols):
                _set_cell(t.cell(0, j), lbl, bold=True, fill=GRAY_HEADER, font_size=11)
            # Body rows. Font sizes scale with column count — wide tables (CARVER)
            # must shrink type to fit; HVTL's 7-col stays at 10pt.
            body_font = 10 if n_cols <= 8 else (9 if n_cols <= 12 else 8)
            row_idx = 1
            if grouping_active:
                # Walk seen_categories in order; emit a category band row then
                # data rows. Use the collapse-aware bucket_for_row mapping so
                # rows land in the right warfighting-function bucket.
                for cat in seen_categories:
                    t.rows[row_idx].height = group_row_h
                    cat_text = _bucket_label(cat)
                    # Merge cells across the row in one call - python-pptx merges
                    # via opposite corners; iterative calls raise
                    # "range contains one or more merged cells".
                    first_cell = t.cell(row_idx, 0)
                    if n_cols > 1:
                        first_cell.merge(t.cell(row_idx, n_cols - 1))
                    _set_cell(first_cell, str(cat_text), bold=True,
                              fill=RGBColor(0xC8, 0xD8, 0xE8), font_size=11)
                    row_idx += 1
                    # Data rows for this category — use bucket_for_row.
                    for idx, it in enumerate(value):
                        if not isinstance(it, dict):
                            continue
                        if bucket_for_row[idx] != cat:
                            continue
                        t.rows[row_idx].height = body_row_h
                        for j, (k, _, sub) in enumerate(cols):
                            v = it.get(k)
                            v = _coerce_mgrs_value(v, sub)
                            v = _humanize_enum_value(v, sub)
                            _set_cell(t.cell(row_idx, j), v, parent=it, field=k, font_size=body_font)
                        row_idx += 1
            else:
                for it in ordered_rows:
                    t.rows[row_idx].height = body_row_h
                    if not isinstance(it, dict):
                        _set_cell(t.cell(row_idx, 0), str(it), font_size=body_font)
                        row_idx += 1
                        continue
                    for j, (k, _, sub) in enumerate(cols):
                        v = it.get(k)
                        v = _coerce_mgrs_value(v, sub)
                        v = _humanize_enum_value(v, sub)
                        _set_cell(t.cell(row_idx, j), v, parent=it, field=k, font_size=body_font)
                    row_idx += 1

    def _render_grid(slide, matrix: dict, row_axis: list, col_axis: list,
                       top=Inches(1.15), left=Inches(0.4),
                       width=Inches(12.5), max_height=Inches(5.65)):
        if not matrix: return
        n_rows = len(row_axis) + 1
        n_cols = len(col_axis) + 1
        row_h = int(max(Pt(20), max_height / max(n_rows, 1)))
        t = slide.shapes.add_table(n_rows, n_cols, left, top, width, row_h * n_rows).table
        col_w = int(width / n_cols)
        for j in range(n_cols):
            t.columns[j].width = col_w
        _set_cell(t.cell(0, 0), "", bold=True, fill=GRAY_HEADER, font_size=9)
        for j, c in enumerate(col_axis, 1):
            _set_cell(t.cell(0, j), _humanize(c), bold=True, fill=GRAY_HEADER, font_size=9)
        for i, rname in enumerate(row_axis, 1):
            rval = matrix.get(rname) or {}
            _set_cell(t.cell(i, 0), _humanize(rname), bold=True, fill=GRAY_LABEL, font_size=9)
            for j, c in enumerate(col_axis, 1):
                cv = rval.get(c) if isinstance(rval, dict) else ""
                _set_cell(t.cell(i, j), cv, parent=rval, field=c, font_size=8)

    def _render_item_slides(value: list, item_subfields: list,
                              section_prefix: str = ""):
        """When items in a list_of_dicts have nested list_of_dicts/dict subfields,
        emit one OVERVIEW slide per item plus one DETAIL slide per nested
        sub-block. Threat COA Statement example: COA-01 gets a slide for {label, rank, most_dangerous},
        plus a slide for the COA Statement (Six Basic Questions), plus one for
        Functional Allocation, plus one for the per-COA HVT list (Fig 6-9)."""
        if not value: return
        scalar_subfields = []
        body_subfields = []
        title_field = None
        for f in item_subfields:
            if not isinstance(f, dict): continue
            if f.get("name") == "_provenance": continue
            sh = f.get("shape")
            if sh in ("list_of_dicts", "dict"):
                body_subfields.append(f)
            else:
                scalar_subfields.append(f)
                if title_field is None:
                    title_field = f
        for it in value:
            if not isinstance(it, dict):
                continue
            title_val = it.get(title_field["name"]) if title_field else None
            ident = (it.get("id") or it.get("coa_id")
                       or (str(title_val) if title_val else "Item"))
            base_title = f"{section_prefix}{ident}".strip(" —")
            # Overview slide — scalar fields as kv block
            kv_items = []
            for sf in scalar_subfields:
                v = it.get(sf["name"])
                if v in (None, "", False): continue
                kv_items.append((sf.get("render_label") or _humanize(sf["name"]),
                                  _humanize_enum_value(v, sf), sf["name"]))
            if kv_items:
                slide = _new_slide(f"{base_title} — Overview" if body_subfields else base_title)
                _render_kv(slide, kv_items, parent=it)
            # One slide per body subfield (full slide bandwidth → no overlap)
            for sf in body_subfields:
                v = it.get(sf["name"])
                if v in (None, "", [], {}): continue
                sub_label = sf.get("render_label") or _humanize(sf["name"])
                slide = _new_slide(f"{base_title} — {sub_label}")
                if sf.get("shape") == "list_of_dicts" and isinstance(v, list):
                    _render_list_of_dicts(slide, v, sf.get("item_subfields") or [])
                elif sf.get("shape") == "dict" and isinstance(v, dict):
                    # `strict_subfields: False` on the item_subfield means
                    # the writer emits only doctrinally-applicable keys
                    # from the declared union; iterate emitted keys, not
                    # the schema union, or the PPTX will render `—` rows
                    # for inapplicable role-bins (same crime as the HTML
                    # path in `_render_paragraph_field`).
                    sub = None if sf.get("strict_subfields") is False else sf.get("subfields")
                    items = [(_humanize(k), v.get(k), k)
                              for k in (sub or list(v.keys()))
                              if not k.startswith("_")]
                    _render_kv(slide, items, parent=v)

    def _render_paragraph_slide(p: dict, value):
        name = p.get("name")
        title_label = p.get("render_label") or _humanize(name)
        # Strip writer-scaffold placeholder tokens from slide titles. Schema
        # render_labels like "Rendered MCOO (M-####)" leak the literal
        # placeholder when no map_id resolved. Same fix as the HTML path in
        # `_render_paragraph_field`.
        for tok in (" (M-####)", "(M-####)", " (M-XXXX)", "(M-XXXX)",
                     " [M-####]", "[M-####]"):
            title_label = title_label.replace(tok, "")
        # Register paragraph-level provenance with the footnote registry so
        # the Sources slide at the end of the deck lists the citation. The
        # HTML path attaches a superscript marker to the section heading;
        # PPTX slide titles don't render inline runs cleanly, so we just
        # log the citation here and let the Sources slide carry the
        # numbered list (analyst can correlate by section title).
        if name and isinstance(structured_data, dict):
            _para_cite = _provenance_for(structured_data, name)
            if _para_cite:
                fn.add_plain(_para_cite)
        sh = p.get("shape")
        if sh == "map":
            _emit_map_slide(name or "map", p)
            return
        if sh == "list_of_dicts":
            isf = p.get("item_subfields") or []
            # If items have nested list_of_dicts/dict subfields, emit one slide
            # per item rather than one summary slide with filtered columns.
            has_nested = any(
                isinstance(f, dict) and f.get("shape") in ("list_of_dicts", "dict")
                for f in isf
            )
            if has_nested and isinstance(value, list) and value:
                _render_item_slides(value, isf, section_prefix=f"{title_label} — ")
                return
            slide = _new_slide(title_label)
            _render_list_of_dicts(slide, value if isinstance(value, list) else [], isf)
            return
        if sh == "string":
            slide = _new_slide(title_label)
            _render_string(slide, value, structured_data, name)
        elif sh == "dict":
            sub = p.get("subfields")
            if isinstance(value, dict):
                # Drop subfields with no data — emitting "— —" rows on every
                # blank subfield turns a BDA phase slide into a wall of dashes.
                items = []
                for k in (sub or list(value.keys())):
                    if k.startswith("_"): continue
                    vv = value.get(k)
                    if vv in (None, "", False, [], {}): continue
                    sv = str(vv).strip().lower()
                    if sv in ("", "n/a", "—", "-", "nstr"): continue
                    items.append((_humanize(k), vv, k))
                if not items:
                    # Whole dict is empty / N/A — skip the slide; the
                    # paragraph_field is documented in the schema, not on the
                    # rendered deck. Doctrine footer + meta band already
                    # establish the artifact's frame.
                    return
                slide = _new_slide(title_label)
                _render_kv(slide, items, parent=value)
            else:
                slide = _new_slide(title_label)
                _render_string(slide, value, structured_data, name)
        elif sh == "grid_table":
            # Same ```json-fence recovery as the HTML render — see the
            # parallel comment near the `if shape == "grid_table"` branch
            # above.
            if isinstance(value, str):
                value = _maybe_parse_fenced_json(value)
            if isinstance(value, dict):
                row_axis = list(value.keys())
                col_axis = []
                for v in value.values():
                    if isinstance(v, dict):
                        for k in v.keys():
                            if k not in col_axis and not k.startswith("_"):
                                col_axis.append(k)
                slide = _new_slide(title_label)
                _render_grid(slide, value, row_axis, col_axis)
        else:
            if isinstance(value, str):
                slide = _new_slide(title_label)
                _render_string(slide, value, structured_data, name)
            elif isinstance(value, list):
                slide = _new_slide(title_label)
                _render_list_of_dicts(slide, value, [])
            elif isinstance(value, dict):
                items = [(_humanize(k), v, k) for k, v in value.items()
                          if not k.startswith("_")]
                if not items: return
                slide = _new_slide(title_label)
                _render_kv(slide, items, parent=value)

    _title_slide()

    # ---- Map slide (position 2) ------------------------------------------
    # When the artifact's header carries a spatial overlay (rendered map PNG
    # via tool_render_map), insert a dedicated map slide immediately after
    # the title. Leadership flipping through the deck scans the map first;
    # tucking it deeper in the deck buries the only visual product the
    # analyst actually owns.
    #
    # Skip silently when:
    #   - no spatial_overlay_url / spatial_overlay_map_id set, or
    #   - the resolved PNG path doesn't exist on disk (analyst hasn't run
    #     render_map yet — don't crash the export).
    def _resolve_map_png(src_url: str) -> Optional[str]:
        """Translate a /files/<name>.png URL into a filesystem path.
        The FastAPI static mount in main.py points /files/ at the runtime
        UPLOAD_DIR (default "uploads", overridable via PYTHO_UPLOADS_DIR).
        We try the env var first, then the canonical pytho-app/app/app/uploads/
        location (relative to this file) as a fallback for harness/CI runs
        that don't set the env var."""
        if not src_url:
            return None
        # Strip the /files/ prefix; tolerate either form.
        rel = src_url
        for prefix in ("/files/", "files/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        env_dir = os.environ.get("PYTHO_UPLOADS_DIR")
        candidates = []
        if env_dir:
            candidates.append(os.path.join(env_dir, rel))
        # Fallback 1: CWD-relative "uploads/" — matches FastAPI default.
        candidates.append(os.path.join("uploads", rel))
        # Fallback 2: pytho-app/app/app/uploads/ relative to this file.
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.normpath(os.path.join(here, "..", "uploads", rel)))
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    def _emit_map_slide(field_name: str, p_spec: dict):
        """Emit one PPTX slide for a paragraph_field with shape="map".

        Image path is convention-based: /files/<uid>/<aid>/<field_name>.jpg.
        Per-field map_id + bbox come from structured_data.map_overlays[<field_name>]
        for the Edit-map hyperlink. Missing image → slide skipped silently
        (no broken-image placeholder, no crash). `.jpg` matches what
        tool_render_map writes (PNG encoder was timing out at 2560x1440).
        """
        if not artifact_id:
            return
        _uid = (user_id or "").strip() or "anonymous"
        src = f"/files/{_uid}/{artifact_id}/{field_name}.jpg"
        png_path = _resolve_map_png(src)
        if not png_path:
            return
        overlays = structured_data.get("map_overlays") if isinstance(structured_data, dict) else None
        meta = (overlays or {}).get(field_name) or {}
        map_id = (meta.get("map_id") or "").strip() if isinstance(meta, dict) else ""
        bbox_val = meta.get("bbox") if isinstance(meta, dict) else None
        bbox = ""
        if isinstance(bbox_val, (list, tuple)) and len(bbox_val) == 4:
            bbox = ",".join(str(x) for x in bbox_val)
        elif isinstance(bbox_val, str):
            bbox = bbox_val.strip()

        slide_title = p_spec.get("render_label") or _humanize(field_name)
        # FULL-BLEED layout for map slides. The map is 16:9 (the geo-agent
        # render is 3840×2160) and the slide is 16:9 (13.33×7.5). Earlier
        # layout stuffed the map into a 12.53×5.25 box (aspect 2.39) which
        # both stretched the source AND left ~1" of whitespace top/bottom
        # + ~0.5" sides. Replace with a full-slide picture + title/caption
        # as text overlays at top/bottom.
        slide = pres.slides.add_slide(pres.slide_layouts[6])
        # Read the source dimensions so we can aspect-preserve. Sliding
        # the picture into the slide WITHOUT a height arg lets python-pptx
        # compute the height from the source aspect ratio (no stretch).
        try:
            from PIL import Image as _PILImage
            with _PILImage.open(png_path) as _img:
                _src_w, _src_h = _img.size
            _src_aspect = _src_w / max(_src_h, 1)
        except Exception:
            _src_aspect = 16 / 9  # safe fallback
        # Slide aspect — when source matches slide aspect we can cover edge-
        # to-edge with zero whitespace. When they diverge, fit to whichever
        # dimension constrains (preserving aspect, accepting the smaller
        # margin on the other axis).
        _slide_aspect = SLIDE_W / SLIDE_H
        if _src_aspect >= _slide_aspect:
            # source is at least as wide as the slide — fit to width
            pic_width = SLIDE_W
            pic_height = int(SLIDE_W / _src_aspect)
        else:
            # source is taller than the slide — fit to height
            pic_height = SLIDE_H
            pic_width = int(SLIDE_H * _src_aspect)
        pic_left = (SLIDE_W - pic_width) // 2
        pic_top = (SLIDE_H - pic_height) // 2
        try:
            pic = slide.shapes.add_picture(png_path, pic_left, pic_top,
                                            width=pic_width, height=pic_height)
        except Exception:
            # add_picture can fail on a corrupt/empty PNG; treat as missing.
            return
        # Stamp the picture's name with a stable join key so the
        # LiveEditor's UNO replace_picture script can match by Name
        # (shape.Name in UNO == cNvPr/@name in OOXML; python-pptx
        # exposes it as pic.name). Required for the in-place map-swap
        # path that skips the iframe reload on Done editing.
        try:
            if map_id:
                pic.name = f"pytho-map:{map_id}"
        except Exception:
            # Name setter is exposed by every supported python-pptx
            # version but a future regression here mustn't fail the
            # slide; the slide_index fallback in the UNO script covers
            # the case where Name isn't stamped.
            pass

        # Click-to-edit deep link: clicking the picture in PowerPoint (or
        # in a PPTX viewer that honors hyperlinks) jumps to the OperationsMap
        # editor with map_id + bbox + originating artifact pre-loaded.
        try:
            from urllib.parse import quote as _q
            deeplink = "/missioncommand/operations-map"
            params = []
            if map_id:
                params.append(f"map_id={_q(map_id)}")
            if bbox:
                params.append(f"bbox={_q(bbox)}")
            if artifact_id:
                params.append(f"from_artifact={_q(artifact_id)}")
            if params:
                deeplink = deeplink + "?" + "&".join(params)
            pic.click_action.hyperlink.address = deeplink
        except Exception:
            # Hyperlink wiring is a convenience; don't fail the slide on it.
            pass

        # Record this map in the LiveEditor sidecar. EMU → twips conversion:
        # 1 twip = 1/1440 inch, 1 EMU = 1/914400 inch → 1 twip = 635 EMU.
        # slide_index = 0-based; pres.slides is append-ordered.
        # image_part is resolved AFTER pres.save() by walking the zip
        # (python-pptx doesn't expose the future part-name reliably at
        # add_picture time — Picture.image carries blob/sha1/ext but no
        # zip path; pic.part.related_parts is empty until commit).
        try:
            _emu_per_twip = 635
            _slide_index = len(pres.slides) - 1
            _bbox_lonlat = list(bbox_val) if isinstance(bbox_val, (list, tuple)) else None
            _map_embeds.append({
                "map_id": map_id or field_name,
                "label": slide_title,
                "slide_index": _slide_index,
                "twips": [
                    pic_left // _emu_per_twip,
                    pic_top // _emu_per_twip,
                    pic_width // _emu_per_twip,
                    pic_height // _emu_per_twip,
                ],
                "emu": [pic_left, pic_top, pic_width, pic_height],
                "bbox_lonlat": _bbox_lonlat,
                "image_part": "",  # resolved post-save (zip walk)
                "image_filename": f"{field_name}.jpg",
                "artifact_id": artifact_id,
                "_image_sha1": pic.image.sha1,  # join key for post-save zip walk
                "_image_ext": pic.image.ext,
            })
        except Exception:
            # Sidecar emission is observability — don't fail the slide on it.
            pass

        # No slide-level metadata overlay or notes. The map render carries
        # its own in-frame title (MapCartographicChrome) + doctrinal legend
        # at the bottom of the JPG, and the picture's hyperlink already
        # encodes artifact_id (from_artifact), map_id, and bbox — every
        # piece of metadata that was previously painted on the slide or
        # written to notes_slide. Notes_slide was Keynote-incompatible
        # (Keynote: "The file format is invalid") because python-pptx's
        # generated notesSlide / notesMaster part lacked some structure
        # Keynote requires; leaving it out keeps the file portable.

    # Map slides are emitted in-order from paragraph_fields with shape="map"
    # via _render_paragraph_slide / the row-shape iteration below — no
    # standalone hardcoded slot.

    # ------------------------------------------------------------------
    # Threat COAs — bespoke per-COA slide layout per the IBL exhibit
    # (scratch/docs/ibl/event_samples/IPSR03_B_16_Exh_03D01_Threat_COAs_
    # OPORD_*.pptx). Each COA slide is a 3-column composition: task-org
    # tree on the left, per-COA SITEMP map in the center, per-COA narrative
    # (mission / phased actions / decisive engagement / branches and
    # sequels / continuing operations) on the right. Per-COA SITEMP map
    # is resolved at render time by matching coa_id to a SITEMP autodraft
    # artifact whose header.coa_id == this slide's coa_id — no back-fill.
    # ------------------------------------------------------------------
    def _resolve_per_coa_sitemp_map(coa_id_lookup: str) -> Optional[str]:
        """Scan the autodraft pool on disk for SITEMP artifacts and return
        the local filesystem path to the SITEMP's situation_overlay_map.jpg
        whose header.coa_id matches `coa_id_lookup`. Latest SITEMP wins
        when more than one matches (autodraft folder names are
        SITEMP_<UTC_TS>Z, lexical sort = chronological).
        Returns None if no matching SITEMP map is found on disk."""
        if not coa_id_lookup:
            return None
        # Resolve uploads root once, mirroring _resolve_map_png's logic.
        env_dir = os.environ.get("PYTHO_UPLOADS_DIR")
        roots = []
        if env_dir:
            roots.append(env_dir)
        roots.append("uploads")
        here = os.path.dirname(os.path.abspath(__file__))
        roots.append(os.path.normpath(os.path.join(here, "..", "uploads")))
        autodraft_dir = None
        for r in roots:
            cand = os.path.join(r, "_autodraft")
            if os.path.isdir(cand):
                autodraft_dir = cand
                break
        if autodraft_dir is None:
            return None
        # Walk SITEMP_* dirs, parse schema_state.json header.coa_id, keep
        # latest match. Single best-effort scan per slide; small N (3 in
        # typical fanouts), no need to cache.
        best_aid = None
        try:
            entries = sorted(os.listdir(autodraft_dir))
        except OSError:
            return None
        for ent in entries:
            if not ent.startswith("SITEMP_"):
                continue
            schema_state = os.path.join(autodraft_dir, ent, "schema_state.json")
            if not os.path.isfile(schema_state):
                continue
            try:
                with open(schema_state, "r") as f:
                    sd_data = json.load(f)
            except (OSError, ValueError):
                continue
            hdr = ((sd_data.get("structured_data") or {}).get("header") or {})
            if (hdr.get("coa_id") or "").strip() == coa_id_lookup.strip():
                # Lexical sort = chronological, so last-match wins (latest).
                best_aid = ent
        if not best_aid:
            return None
        jpg_path = os.path.join(autodraft_dir, best_aid, "situation_overlay_map.jpg")
        if not os.path.isfile(jpg_path):
            return None
        return jpg_path

    def _emit_threat_coa_slide(coa_item: dict, coa_index: int = 0) -> None:
        """Render one slide of the Threat COAs deck per the IBL exhibit
        layout. Called once per upstream Threat COA Statement COA. Slide composition:

          ┌─ title bar (full width, dark fill) ─────────────────────┐
          │  Threat COA <rank> — <title>      [<ROLE BADGE>]         │
          │  badge ∈ { MOST LIKELY (rank 1), MOST DANGEROUS (rank 3),│
          │            MOST DECEPTIVE (rank 4) }; rank 2 unbadged    │
          ├─ task org (left) ─┬─ map (center) ─┬─ narrative (right) ┤
          │  X (+)            │                │ Mission             │
          │  Army             │                │ <text>              │
          │  60 MECH DIV      │   per-COA      │ Phased Actions      │
          │  · 604 TANK BDE   │   SITEMP       │ · Phase 1: <text>   │
          │  62 MTZD DIV      │   map          │ · Phase 2: <text>   │
          │  · 623 TANK BDE   │   (image)      │ · Phase 3: <text>   │
          │  66 MTZD DIV      │                │ Decisive Engagement │
          │  · 663 TANK BDE   │                │ Branches & Sequels  │
          │  HQ WESTERN ARMY  │                │ Continuing Ops      │
          └───────────────────┴────────────────┴─────────────────────┘
        """
        slide = pres.slides.add_slide(pres.slide_layouts[6])
        # Font for every run on this slide. Latvian place names in the
        # IDX 26 scenario (Grundzāle, Smiltene, Rūjiena) use U+0101 / U+016B
        # / etc. from Latin Extended-A. python-pptx writes the codepoints
        # correctly into the XML, but at render time LibreOffice / PowerPoint
        # substitute when the named font lacks coverage — and the substitute
        # picker has been observed to swap U+0101 'ā' for U+0104 'Ą' (Polish
        # ogonek-A) when the host's Arial install lacks Latvian glyphs.
        # Liberation Sans ships with LibreOffice (the headless converter in
        # the IDX 26 stack), is metric-compatible with Arial (no layout
        # shift), and carries full Latvian coverage.
        _slide_font = "Liberation Sans"
        # Title bar — dark fill spanning the slide width, height ~0.5".
        title_text = (coa_item.get("title") or
                       f"Threat COA {coa_item.get('rank', '?')} — "
                       f"{coa_item.get('coa_id', '?')}")
        most_dangerous = bool(coa_item.get("most_dangerous"))
        most_deceptive = bool(coa_item.get("most_deceptive"))
        # rank=1 is the most-likely COA per the upstream Threat COA
        # Statement convention. Derived from rank rather than a
        # separate flag — the schema's coas[] ordering is rank-driven
        # and rank=1 carries the "most likely" doctrinal role per
        # ATP 2-01.3 §6.
        try:
            _rank = int(coa_item.get("rank") or 0)
        except (TypeError, ValueError):
            _rank = 0
        most_likely = (_rank == 1)
        title_box = slide.shapes.add_textbox(
            Inches(0.0), Inches(0.0), Inches(13.33), Inches(0.55)
        )
        title_box.fill.solid()
        title_box.fill.fore_color.rgb = DARK_BAR
        title_box.line.color.rgb = DARK_BAR
        tf = title_box.text_frame
        tf.word_wrap = True
        tf.margin_left = Inches(0.15)
        tf.margin_right = Inches(0.15)
        tf.margin_top = Inches(0.05)
        tf.margin_bottom = Inches(0.05)
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text = title_text
        r.font.size = Pt(22)
        r.font.bold = True
        r.font.color.rgb = WHITE
        r.font.name = _slide_font
        # Title-bar role badge — one of [MOST LIKELY] (rank=1),
        # [MOST DANGEROUS] (rank=3 / most_dangerous), [MOST DECEPTIVE]
        # (rank=4 / most_deceptive). rank=2 (intermediate / bracketing
        # residual possibility space) carries no badge — there is no
        # doctrinal name for that slot. Uniform white text on the dark
        # title bar; the badge text itself carries the semantic, no
        # per-badge color coding.
        #
        # Gated by SHOW_COA_ROLE_BADGES_OVERRIDE contextvar (default
        # False — bare COA-1..COA-4 title bars without editorial
        # pre-labels). Opt-in via the Threat COAs gear-button
        # config_fields entry.
        _badge = ""
        _show_badges = False
        try:
            from utils import equipment_doctrine as _ed
            _show_badges = bool(_ed.SHOW_COA_ROLE_BADGES_OVERRIDE.get())
        except Exception:
            _show_badges = False
        if _show_badges:
            if most_likely:
                _badge = "    [MOST LIKELY]"
            elif most_dangerous:
                _badge = "    [MOST DANGEROUS]"
            elif most_deceptive:
                _badge = "    [MOST DECEPTIVE]"
        if _badge:
            r2 = p.add_run()
            r2.text = _badge
            r2.font.size = Pt(13)
            r2.font.bold = True
            r2.font.color.rgb = WHITE
            r2.font.name = _slide_font

        # Column geometry — slide is 13.33" wide × 7.5" tall.
        # Title bar consumes y=[0.0, 0.55]; columns span y=[0.65, 7.30].
        # Left task-org: 0.15-2.55 (2.4" wide)
        # Center map:    2.65-8.65 (6.0" wide)
        # Right text:    8.75-13.18 (4.43" wide)
        COL_TOP = Inches(0.65)
        COL_BOT = Inches(7.30)
        COL_HEIGHT = COL_BOT - COL_TOP

        # --- LEFT COLUMN: task org tree --------------------------------
        # Writers sometimes emit task_org_units as a comma-separated
        # string ("60 MECH IFV DIV, 62 MTZD INF DIV, ...") instead of
        # the schema-prescribed list_of_dicts. Coerce both into the
        # same {echelon, designation, doctrinal_role} shape so the
        # sidebar renders either way.
        _raw_tou = coa_item.get("task_org_units")
        task_org_units: list = []
        if isinstance(_raw_tou, list):
            task_org_units = _raw_tou
        elif isinstance(_raw_tou, str) and _raw_tou.strip():
            for _tok in _raw_tou.split(","):
                _tok = _tok.strip()
                if not _tok:
                    continue
                # Heuristic echelon classification — drives indent
                # in the sidebar. Order matters: ARMY before DIV.
                _t_upper = _tok.upper()
                if "ARMY" in _t_upper or "CORPS" in _t_upper or "HQ" in _t_upper:
                    _ech = "CORPS"
                elif "DIV" in _t_upper:
                    _ech = "DIVISION"
                elif " BN" in _t_upper or "BATTALION" in _t_upper:
                    _ech = "BATTALION"
                else:
                    _ech = "BRIGADE"
                task_org_units.append({
                    "echelon": _ech,
                    "designation": _tok,
                    "doctrinal_role": "",
                })
        if task_org_units:
            org_box = slide.shapes.add_textbox(
                Inches(0.15), COL_TOP, Inches(2.40), COL_HEIGHT
            )
            org_box.fill.solid()
            org_box.fill.fore_color.rgb = RGBColor(0xF5, 0xF7, 0xFA)
            org_box.line.color.rgb = RGBColor(0xB5, 0xC0, 0xCC)
            otf = org_box.text_frame
            otf.word_wrap = True
            otf.margin_left = Inches(0.10)
            otf.margin_right = Inches(0.10)
            otf.margin_top = Inches(0.08)
            otf.margin_bottom = Inches(0.08)
            # Header line
            hp = otf.paragraphs[0]
            hp.alignment = PP_ALIGN.LEFT
            hr = hp.add_run()
            hr.text = "TASK ORGANIZATION"
            hr.font.size = Pt(10)
            hr.font.bold = True
            hr.font.color.rgb = BLACK
            hr.font.name = _slide_font
            for u in task_org_units:
                if not isinstance(u, dict):
                    continue
                designation = (u.get("designation") or "").strip()
                if not designation:
                    continue
                ech = (u.get("echelon") or "").upper().strip()
                role = (u.get("doctrinal_role") or "").strip()
                line_p = otf.add_paragraph()
                line_p.alignment = PP_ALIGN.LEFT
                # Indent BDE/BN under their parents — use ASCII bullet for
                # readability since python-pptx indents don't show cleanly
                # in all renderers.
                if ech in ("BRIGADE", "BDE", "BATTALION", "BN"):
                    prefix = "  · "
                    fsize = Pt(9)
                    bold = False
                elif ech in ("DIVISION", "DIV", "CORPS"):
                    prefix = ""
                    fsize = Pt(10)
                    bold = True
                else:
                    prefix = ""
                    fsize = Pt(10)
                    bold = True
                line_r = line_p.add_run()
                line_r.text = f"{prefix}{designation}"
                line_r.font.size = fsize
                line_r.font.bold = bold
                line_r.font.color.rgb = BLACK
                line_r.font.name = _slide_font
                if role:
                    role_p = otf.add_paragraph()
                    role_p.alignment = PP_ALIGN.LEFT
                    role_r = role_p.add_run()
                    role_r.text = f"     {role}"
                    role_r.font.size = Pt(8)
                    role_r.font.italic = True
                    role_r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
                    role_r.font.name = _slide_font

        # --- CENTER COLUMN: per-COA SITEMP map -------------------------
        coa_id_for_lookup = (coa_item.get("coa_id") or "").strip()
        sitemp_map_path = _resolve_per_coa_sitemp_map(coa_id_for_lookup)
        center_left = Inches(2.65)
        center_top = COL_TOP
        center_w = Inches(6.00)
        center_h = COL_HEIGHT
        if sitemp_map_path:
            try:
                # Aspect-preserve: pass width only, python-pptx computes
                # height from the JPG's native aspect.
                pic = slide.shapes.add_picture(
                    sitemp_map_path, center_left, center_top, width=center_w
                )
                # If aspect produced a taller-than-column image, shrink.
                if pic.height > center_h:
                    scale = center_h / pic.height
                    pic.height = int(pic.height * scale)
                    pic.width = int(pic.width * scale)
                pic.line.color.rgb = RGBColor(0x80, 0x80, 0x80)
                pic.line.width = Pt(0.5)
            except Exception:
                # Map missing → drop a placeholder box so the slot still
                # reads as the map's slot.
                box = slide.shapes.add_textbox(
                    center_left, center_top, center_w, center_h
                )
                box.line.color.rgb = RGBColor(0xB5, 0xC0, 0xCC)
                bf = box.text_frame
                bf.word_wrap = True
                bp = bf.paragraphs[0]
                bp.alignment = PP_ALIGN.CENTER
                br = bp.add_run()
                br.text = f"(SITEMP map unavailable for {coa_id_for_lookup})"
                br.font.size = Pt(11)
                br.font.italic = True
                br.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
                br.font.name = _slide_font
        else:
            box = slide.shapes.add_textbox(
                center_left, center_top, center_w, center_h
            )
            box.line.color.rgb = RGBColor(0xB5, 0xC0, 0xCC)
            bf = box.text_frame
            bf.word_wrap = True
            bp = bf.paragraphs[0]
            bp.alignment = PP_ALIGN.CENTER
            br = bp.add_run()
            br.text = f"(SITEMP map unavailable for {coa_id_for_lookup})"
            br.font.size = Pt(11)
            br.font.italic = True
            br.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
            br.font.name = _slide_font

        # --- RIGHT COLUMN: per-COA narrative ---------------------------
        text_box = slide.shapes.add_textbox(
            Inches(8.75), COL_TOP, Inches(4.43), COL_HEIGHT
        )
        # Name the shape with the pytho.* prefix so the importer enters
        # per-paragraph marker scan mode (utils/importer.py _pptx_walk
        # only enters per-paragraph mode for shapes whose name starts with
        # PPTX_PREFIX). The `:narrative` suffix is a paint-only region tag
        # the importer's reconstructor maps back to the shape-level emit
        # but the per-paragraph emits inside it carry the canonical
        # `pytho.coa_slides[<i>].<field_name>` tags directly.
        try:
            text_box.name = f"pytho.coa_slides[{coa_index}]:narrative"
        except Exception:
            pass
        text_box.fill.solid()
        text_box.fill.fore_color.rgb = RGBColor(0xFC, 0xFC, 0xFC)
        text_box.line.color.rgb = RGBColor(0xC8, 0xD2, 0xDD)
        ttf = text_box.text_frame
        ttf.word_wrap = True
        ttf.margin_left = Inches(0.12)
        ttf.margin_right = Inches(0.12)
        ttf.margin_top = Inches(0.10)
        ttf.margin_bottom = Inches(0.10)
        # Section emitter — bold heading line + small-text body paragraphs.
        # When `field_name` is provided, prepend a hidden zero-size leading
        # run carrying the canonical paragraph marker
        # `[[pytho.coa_slides[<coa_index>].<field_name>]]` BEFORE the
        # visible body run on every body paragraph. The importer reads the
        # leading `[[...]]` token to route the analyst-edited visible text
        # back to the correct structured_data slot. The marker run is
        # Pt(1) (smallest reliable PowerPoint render size) with WHITE fill
        # so it doesn't visually appear at the top of the narrative column.
        first_section = True
        def _emit_section(heading: str, body_lines: list[str],
                          field_name: str = "") -> None:
            nonlocal first_section
            non_empty = [ln for ln in body_lines if isinstance(ln, str) and ln.strip()]
            if not non_empty:
                return
            if first_section:
                hp = ttf.paragraphs[0]
                first_section = False
            else:
                hp = ttf.add_paragraph()
            hp.alignment = PP_ALIGN.LEFT
            hr = hp.add_run()
            hr.text = heading
            hr.font.size = Pt(11)
            hr.font.bold = True
            hr.font.color.rgb = BLACK
            hr.font.name = _slide_font
            # Build the marker once for the whole section (cheaper than
            # per-paragraph re-render).
            marker_token = ""
            if field_name:
                try:
                    marker_token = _pptx_paragraph_marker(
                        ["coa_slides", coa_index, field_name]
                    )
                except Exception:
                    marker_token = ""
            for ln in non_empty:
                bp_ = ttf.add_paragraph()
                bp_.alignment = PP_ALIGN.LEFT
                # Leading hidden marker run — Pt(1), white-on-white so it
                # doesn't bleed into the visible narrative. The importer
                # reads it to route analyst-edited prose back to the
                # canonical structured_data slot.
                if marker_token:
                    mr_ = bp_.add_run()
                    mr_.text = marker_token
                    mr_.font.size = Pt(1)
                    mr_.font.color.rgb = WHITE
                    mr_.font.name = _slide_font
                br_ = bp_.add_run()
                br_.text = ln
                br_.font.size = Pt(9)
                br_.font.color.rgb = BLACK
                br_.font.name = _slide_font

        # Coerce string-typed slide fields. The writer occasionally
        # emits scalar string fields as lists ('three indicators' read
        # as a JSON array). Accept either shape and feed _emit_section
        # the right number of body lines.
        def _coerce_text(val) -> list[str]:
            if val is None:
                return []
            if isinstance(val, list):
                return [str(x).strip() for x in val if x not in (None, "")]
            return [str(val).strip()]

        mission_lines = _coerce_text(coa_item.get("mission_statement"))
        _emit_section("Mission", mission_lines,
                       field_name="mission_statement")

        phased = coa_item.get("phased_actions") or []
        phased_lines: list[str] = []
        for pa in phased:
            if not isinstance(pa, dict):
                continue
            phase = (pa.get("phase") or "").strip()
            action = (pa.get("threat_action") or "").strip()
            timing = (pa.get("timing") or "").strip()
            if not (phase or action):
                continue
            if timing:
                phased_lines.append(f"• {phase} ({timing}): {action}")
            elif phase and action:
                phased_lines.append(f"• {phase}: {action}")
            elif action:
                phased_lines.append(f"• {action}")
        _emit_section("Phased Actions", phased_lines,
                       field_name="phased_actions")

        _emit_section("Decisive Engagement",
                       _coerce_text(coa_item.get("decisive_engagement")),
                       field_name="decisive_engagement")
        _emit_section("Branches and Sequels",
                       _coerce_text(coa_item.get("branches_and_sequels")),
                       field_name="branches_and_sequels")
        _emit_section("Continuing Operations",
                       _coerce_text(coa_item.get("continuing_operations")),
                       field_name="continuing_operations")
        _emit_section("Indicators",
                       _coerce_text(coa_item.get("distinguishing_signatures")),
                       field_name="distinguishing_signatures")

    # Threat COAs short-circuit — bypass the generic row/paragraph
    # dispatch entirely. The deck is the cover slide (already added by
    # the unconditional _title_slide() above) + one Threat-COA slide
    # per upstream Threat COA Statement COA, in rank order. No other paragraph fields
    # in this schema, so we save and return after the COA slides.
    if artifact_type == "Threat COAs":
        coa_slides = []
        for pf in paragraph_fields:
            if isinstance(pf, dict) and pf.get("name") == "coa_slides":
                coa_slides = structured_data.get("coa_slides") or []
                break
        if isinstance(coa_slides, list):
            # Sort by rank (ascending) — most-likely first, most-dangerous
            # last per IBL exhibit ordering.
            def _coa_sort_key(c):
                if not isinstance(c, dict):
                    return (99, "")
                rk = c.get("rank")
                try:
                    rk = int(rk)
                except (TypeError, ValueError):
                    rk = 99
                return (rk, str(c.get("coa_id") or ""))
            # Preserve the ORIGINAL coa_slides index when iterating sorted
            # — the per-paragraph hidden marker the importer reads carries
            # the canonical path `pytho.coa_slides[<index>].<field>`, which
            # must match the slot in structured_data["coa_slides"] (NOT
            # the rendered slide-rank order). Build (orig_index, coa) pairs
            # first, then sort by rank for visual presentation.
            _indexed_coa_slides = [
                (i, c) for i, c in enumerate(coa_slides) if isinstance(c, dict)
            ]
            _indexed_coa_slides.sort(key=lambda t: _coa_sort_key(t[1]))
            for _orig_idx, coa in _indexed_coa_slides:
                _emit_threat_coa_slide(coa, coa_index=_orig_idx)
        # Canonical-on-disk marker slide — same shadow-registry emit as
        # the generic per-product PPTX path below. The Threat COAs
        # branch short-circuits via `return out_path`, so the marker
        # emit at the end of the function never fires for this product
        # type. Emit it here so Threat COAs decks carry the marker tree
        # like every other PPTX.
        #
        # Mark the slide hidden via OOXML <p:sld show="0"> so PowerPoint /
        # Collabora skip it in normal presentation. The slide still
        # round-trips through the importer (which walks every slide
        # element regardless of show flag) but the analyst doesn't see
        # a blank "extra" slide at the end of the deck.
        try:
            _marker_slide = pres.slides.add_slide(pres.slide_layouts[6])
            _emit_pptx_marker_tree(_marker_slide, structured_data, schema)
            _marker_slide.element.set("show", "0")
        except Exception as _e:
            _pptx_log.warning(
                "PPTX canonical-on-disk marker slide emit failed "
                "(Threat COAs branch): %s", _e,
            )
        _pptx_log.info("PPTX pre-save type=%s dur_s=%.1f slides=%d",
                        artifact_type, _t_mod.monotonic() - _pptx_t0,
                        len(pres.slides))
        pres.save(out_path)
        _pptx_log.info("PPTX DONE type=%s dur_s=%.1f path=%s",
                        artifact_type, _t_mod.monotonic() - _pptx_t0, out_path)
        return out_path

    if shape == "row":
        # Detect "scored-matrix" products (CARVER signature): published row
        # fields contain paired `<criterion>_score` + `<criterion>_rationale`
        # entries. For these the doctrinal published view is JUST the scores
        # (one-letter columns under a "CARVER" supercolumn per FM 3-60
        # Fig G-1); rationales surface as per-row detail slides.
        published = _published_field_list(schema)
        score_fields = [f for f in published if f.endswith("_score")
                        and f != "composite_total_score"]
        rationale_fields = [f for f in published if f.endswith("_rationale")]
        is_scored_matrix = (len(score_fields) >= 3
                            and any(f.replace("_score", "_rationale") in rationale_fields
                                    for f in score_fields))

        if is_scored_matrix and rows and published:
            # Published summary: identity columns + score columns + total.
            # Drop all `_rationale` columns from the summary; they get a
            # dedicated workpaper slide per row below.
            summary_fields = [f for f in published if f not in rationale_fields]
            slide = _new_slide(schema.get("published_section_title") or "Entries")
            synth = [{"name": f, "render_label": _label_for_field(f, row_specs.get(f, {})),
                       **(row_specs.get(f) or {})} for f in summary_fields]
            _render_list_of_dicts(slide, rows, synth,
                                    group_by=schema.get("group_by"),
                                    group_collapse_map=schema.get("group_collapse_map"),
                                    group_collapse_order=schema.get("group_collapse_order"),
                                    group_collapse_labels=schema.get("group_collapse_labels"),
                                    group_label_override=schema.get("group_label_override"))
            # Per-target detail slide: identity row + score-rationale pairs.
            # Skip rows where every criterion is empty (no data, no slide).
            for r in rows:
                ident = r.get("id") or ""
                name = r.get("name") or ""
                title = ident + (f" — {name}" if name and name != ident else "")
                items = []
                for sf in score_fields:
                    base = sf[:-len("_score")]
                    rf = f"{base}_rationale"
                    score_lbl = _label_for_field(base, {"render_label": _humanize(base)})
                    score_val = r.get(sf)
                    rat_val = r.get(rf)
                    if score_val in (None, "", False) and rat_val in (None, "", False):
                        continue
                    score_disp = "—" if score_val in (None, "", False) else str(score_val)
                    items.append((f"{score_lbl} ({score_disp})", rat_val, rf))
                if "composite_total_score" in published:
                    cs = r.get("composite_total_score")
                    if cs not in (None, "", False):
                        items.append(("Total Composite Score", cs, "composite_total_score"))
                if not items:
                    # Whole row empty — skip the slide entirely.
                    continue
                slide = _new_slide(title or "Detail")
                _render_kv(slide, items, parent=r)
            # Skip the rest of the row-shape branch — done.
        elif rows and published:
            slide = _new_slide(schema.get("published_section_title") or "Entries")
            keep = [f for f in published
                     if any(r.get(f) not in (None, "", False) for r in rows)]
            if keep:
                synth = [{"name": f, "render_label": _label_for_field(f, row_specs.get(f, {})),
                           **(row_specs.get(f) or {})} for f in keep]
                _render_list_of_dicts(slide, rows, synth,
                                        group_by=schema.get("group_by"),
                                        group_collapse_map=schema.get("group_collapse_map"),
                                        group_collapse_order=schema.get("group_collapse_order"),
                                        group_collapse_labels=schema.get("group_collapse_labels"),
                                        group_label_override=schema.get("group_label_override"))

        # Workpaper-view row_fields are doctrinally INTERNAL — they feed the
        # published row body (e.g. HVTL Justification) via DAG depends_on
        # and never render as their own slide. FM 3-60 App C: published HVTL
        # is Category | HVT | Justification, period.

        # Any paragraph_fields tagged published
        for p in paragraph_fields:
            if (p.get("view", "published")) != "published": continue
            _is_map = p.get("shape") == "map"
            v = structured_data.get(p.get("name"))
            if not _is_map and v in (None, "", [], {}): continue
            _render_paragraph_slide(p, v)

    elif shape == "lines":
        slide = _new_slide("Report Lines")
        line_keys = _line_field_keys(structured_data, schema)
        items = [(_line_label(k, schema), structured_data.get(k), k)
                  for k in line_keys]
        _render_kv(slide, items, parent=structured_data)

    elif shape == "matrix":
        if matrix_spec.get("field"):
            raw = structured_data.get(matrix_spec["field"])
            mat = _normalize_matrix(raw, matrix_spec.get("row_axis"), matrix_spec.get("col_axis"))
            if isinstance(mat, dict) and mat:
                title_label = ""
                for p in paragraph_fields:
                    if p.get("name") == matrix_spec["field"]:
                        title_label = p.get("render_label") or _humanize(matrix_spec["field"])
                        break
                slide = _new_slide(title_label or _humanize(matrix_spec["field"]))
                row_axis = matrix_spec.get("row_axis") or list(mat.keys())
                col_axis = matrix_spec.get("col_axis")
                if not col_axis:
                    seen = []
                    for v in mat.values():
                        if isinstance(v, dict):
                            for k in v.keys():
                                if k not in seen and not k.startswith("_"): seen.append(k)
                    col_axis = seen
                col_axis = col_axis[:6]
                _render_grid(slide, mat, row_axis, col_axis)
        for p in paragraph_fields:
            name = p.get("name")
            if matrix_spec.get("field") == name: continue
            if (p.get("view", "published")) != "published": continue
            _is_map = p.get("shape") == "map"
            v = structured_data.get(name)
            if not _is_map and v in (None, "", [], {}): continue
            _render_paragraph_slide(p, v)

    else:
        # paragraph / structured / header_only — one slide per paragraph_field
        if paragraph_fields:
            for p in paragraph_fields:
                if (p.get("view", "published")) != "published": continue
                _is_map = p.get("shape") == "map"
                name = p.get("name")
                v = structured_data.get(name)
                if not _is_map and v in (None, "", [], {}): continue
                _render_paragraph_slide(p, v)
        else:
            skip = {"header", "rows", "doctrinal_basis_citation"}
            for k, v in structured_data.items():
                if k in skip or k.startswith("_"): continue
                if isinstance(v, str):
                    slide = _new_slide(_humanize(k))
                    _render_string(slide, v, structured_data, k)
                elif isinstance(v, dict):
                    slide = _new_slide(_humanize(k))
                    items = [(_humanize(kk), vv, kk) for kk, vv in v.items()
                              if not kk.startswith("_")]
                    _render_kv(slide, items, parent=v)
                elif isinstance(v, list):
                    slide = _new_slide(_humanize(k))
                    _render_list_of_dicts(slide, v, [])

    # Sources slide (numbered footnotes registered above) — citations are
    # data, not doctrine. No separate Doctrinal Basis slide.
    if fn.footnotes:
        slide = _new_slide("Sources")
        tx = slide.shapes.add_textbox(Inches(0.5), Inches(1.15), Inches(12.3), Inches(5.65))
        tf = tx.text_frame; tf.word_wrap = True
        for i, (num, cite) in enumerate(fn.footnotes):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.space_after = Pt(4)
            r1 = p.add_run(); r1.text = f"{num}. "
            r1.font.size = Pt(12); r1.font.bold = True; r1.font.color.rgb = BLACK
            r1.font.name = "Arial"
            r2 = p.add_run(); r2.text = cite
            r2.font.size = Pt(12); r2.font.color.rgb = BLACK
            r2.font.name = "Arial"

    # Canonical-on-disk marker slide. Emit a single hidden slide carrying
    # a structured_data shadow as a tree of textboxes whose shape names
    # encode the field path (`pytho.<path>`) and whose text frames carry
    # the field value. The importer walks this slide to recover the
    # canonical payload from the rendered file. The slide lives at the
    # end of the deck and is not navigated to in normal operation.
    #
    # This is the gap-free invariant's PPTX implementation: every schema-
    # declared field has at least one marker on disk. Per-shape markers on
    # the VISIBLE slides (see _emit_threat_coa_slide and the per-product
    # render branches) carry the analyst-edit round-trip when present; the
    # hidden marker slide is the authoritative shadow registry the importer
    # falls back on for fields whose visible-slide marker the analyst may
    # have inadvertently deleted.
    try:
        _marker_slide = pres.slides.add_slide(pres.slide_layouts[6])
        _emit_pptx_marker_tree(_marker_slide, structured_data, schema)
        # Hide the marker slide so PowerPoint / Collabora skip it in
        # normal presentation. Importer still walks every slide element
        # regardless of show flag, so round-trip recovery is unaffected.
        _marker_slide.element.set("show", "0")
    except Exception as _e:
        _pptx_log.warning("PPTX canonical-on-disk marker slide emit failed: %s", _e)

    # BDA-specific: append a "Post-strike imagery" slide near the end.
    # Header field iir_file_url comes from IDXImageIirReport.fileURL
    # (threaded by handlers/bda_autofire) — GRVTY-supplied imagery
    # location. utils.url_fetcher handles every scheme GRVTY may use
    # (http/https/file/ftp/s3/smb). Always emit the slide so the
    # analyst-facing PPTX surfaces the imagery slot — embed the
    # fetched bytes when the URL is populated + fetchable, otherwise
    # surface a clear placeholder explaining why.
    if artifact_type == "BDA":
        try:
            _hdr = structured_data.get("header") or {}
            _file_url = (_hdr.get("iir_file_url") or "").strip()
            _slide = pres.slides.add_slide(pres.slide_layouts[5])
            _title = _slide.shapes.title
            if _title is not None:
                _title.text = "Post-strike Imagery (IIR)"
            _embed_left, _embed_top = Inches(0.5), Inches(1.5)
            _embed_w, _embed_h = Inches(9.0), Inches(5.0)
            if _file_url:
                try:
                    from utils.url_fetcher import fetch_bytes, URLFetchError
                    _img_bytes = fetch_bytes(_file_url, timeout=20.0)
                    import io
                    _slide.shapes.add_picture(
                        io.BytesIO(_img_bytes),
                        _embed_left, _embed_top,
                        width=_embed_w,
                    )
                    # Caption below image with source URL + analysis.
                    _cap = _slide.shapes.add_textbox(
                        _embed_left, Inches(6.7), _embed_w, Inches(0.6)
                    )
                    _tf = _cap.text_frame
                    _tf.text = (
                        f"Source: {_file_url}  ·  "
                        f"Analysis: {_hdr.get('iir_observation_text') or '(no IIR observation text)'}"
                    )
                except URLFetchError as _ufe:
                    _ph = _slide.shapes.add_textbox(
                        _embed_left, _embed_top, _embed_w, _embed_h
                    )
                    _ph.text_frame.text = (
                        f"[IMAGERY FETCH FAILED]\n\n"
                        f"URL: {_file_url}\n"
                        f"Error: {_ufe}\n\n"
                        f"GRVTY-supplied IDXImageIirReport.fileURL was set, but "
                        f"the file could not be retrieved. Check that the URL "
                        f"scheme + credentials are reachable from this network."
                    )
                except Exception as _e:
                    _pptx_log.warning("BDA imagery embed failed: %s", _e)
                    _ph = _slide.shapes.add_textbox(
                        _embed_left, _embed_top, _embed_w, _embed_h
                    )
                    _ph.text_frame.text = f"[Imagery embed error: {_e}]"
            else:
                # No URL yet — surface the gap explicitly.
                _ph = _slide.shapes.add_textbox(
                    _embed_left, _embed_top, _embed_w, _embed_h
                )
                _obs = _hdr.get("iir_observation_text") or ""
                _change = _hdr.get("iir_change_significance") or ""
                _ph.text_frame.text = (
                    "[POST-STRIKE IMAGERY PENDING GRVTY UPLOAD]\n\n"
                    "IDXImageIirReport.fileURL is not yet populated for "
                    "this engagement. GRVTY ships the file URL out-of-band "
                    "(any of http/https/file/ftp/s3/smb — the BDA exporter "
                    "fetches via utils.url_fetcher when the URL lands).\n\n"
                    + (f"Analyst observation (additionalInfo):\n  {_obs}\n\n" if _obs else "")
                    + (f"Imagery change significance: {_change}\n" if _change else "")
                )
        except Exception as _e:
            _pptx_log.warning("BDA imagery slide emit failed: %s", _e)

    _pptx_log.info("PPTX pre-save type=%s dur_s=%.1f slides=%d",
                    artifact_type, _t_mod.monotonic() - _pptx_t0,
                    len(pres.slides))
    pres.save(out_path)
    _pptx_log.info("PPTX DONE type=%s dur_s=%.1f path=%s",
                    artifact_type, _t_mod.monotonic() - _pptx_t0, out_path)

    # Emit the LiveEditor map-embed sidecar alongside the deliverable.
    # Resolve each entry's image_part by walking the freshly-written
    # PPTX zip — every map shape we tracked carries its image's SHA1, and
    # the zip's `ppt/media/imageN.<ext>` entries match by content hash.
    # Falls back to nth-encounter pairing when SHA1 join misses (logo /
    # chart parts won't have matching hashes, so they sort out naturally).
    # Always write (empty array for no-maps artifacts) so the frontend
    # GET /api/artifacts/<aid>/map_embeds always returns a deterministic
    # 200. See handlers/wopi.py + handlers/artifact_map_refresh.py for
    # the consumers.
    try:
        import hashlib as _hashlib
        import zipfile as _zipfile
        # Build sha1 → zip path index over every ppt/media/* part.
        _sha_to_part: dict[str, str] = {}
        with _zipfile.ZipFile(out_path, "r") as _z:
            for n in _z.namelist():
                if not n.startswith("ppt/media/") or n.endswith("/"):
                    continue
                _sha_to_part[_hashlib.sha1(_z.read(n)).hexdigest()] = n
        # Join each embed entry with its zip part via SHA1.
        for _e in _map_embeds:
            _sha = _e.pop("_image_sha1", "")
            _e.pop("_image_ext", None)
            if _sha and _sha in _sha_to_part:
                _e["image_part"] = _sha_to_part[_sha]
        _sidecar = os.path.join(os.path.dirname(out_path), "map_embeds.json")
        with open(_sidecar, "w", encoding="utf-8") as _f:
            json.dump(_map_embeds, _f, indent=2)
        _pptx_log.info(
            "PPTX map_embeds.json written: %d embeds (parts joined: %d) at %s",
            len(_map_embeds),
            sum(1 for _e in _map_embeds if _e.get("image_part")),
            _sidecar,
        )
    except Exception as _e:
        _pptx_log.warning("PPTX map_embeds.json write failed: %s", _e)
    return out_path

# ---------------------------------------------------------------------------
# Session export
# ---------------------------------------------------------------------------
def export_session(session_state_path: str, output_dir: str):
    """Render each artifact in only the formats its schema declares
    (`output_formats`). HTML is always emitted (debugging mirror of the
    rendered DOCX/PPTX content). Schema-declared docx-only products do not
    get a PPTX and vice versa — the published format reflects doctrinal
    delivery medium (matrix/portrait narrative → docx; landscape table or
    map-dominant briefing → pptx)."""
    os.makedirs(output_dir, exist_ok=True)
    with open(session_state_path) as f:
        state = json.load(f)
    ar = state.get("artifact_registry", [])
    items_iter = list(ar.items()) if isinstance(ar, dict) else ar
    written = []
    for entry in items_iter:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2): continue
        artifact_type, items = entry
        schema = PRODUCT_SCHEMAS.get(artifact_type, {})
        output_format = schema.get("output_format") or "docx"
        orientation = "landscape" if output_format == "pptx" else "portrait"
        # Internal flag preserved for the docx/pptx branch checks below; the
        # schema now carries a singular output_format string, not a list.
        formats = [output_format]
        for art in items:
            artifact_id = art.get("artifact_id", "unknown")
            sd = art.get("structured_data") or {}
            gaps = art.get("gaps") or []
            classification = (sd.get("header", {}).get("classification") or "UNCLASSIFIED")
            html_str = render_artifact_html(artifact_type, sd, gaps=gaps,
                                              artifact_id=artifact_id)
            html_path = os.path.join(output_dir, f"{artifact_type}_{artifact_id}.html")
            with open(html_path, "w") as f: f.write(html_str)
            docx_path = None; pptx_path = None
            # Render errors propagate — register_and_run treats a raised
            # exception as chain-failed instead of swallowing it and reporting
            # a successful export with a half-rendered set. Catching here +
            # log.warning + None previously masked schema/template bugs that
            # left analysts staring at zero-page DOCX or empty PPTX without
            # any failure signal on the calling side.
            if "docx" in formats:
                docx_path = os.path.join(output_dir, f"{artifact_type}_{artifact_id}.docx")
                html_to_docx(html_str, docx_path, classification=classification,
                              orientation=orientation)
            if "pptx" in formats:
                pptx_path = os.path.join(output_dir, f"{artifact_type}_{artifact_id}.pptx")
                _owner_uid = ((sd.get("header") or {}).get("owner_user_id")
                               or art.get("owner_user_id") or "")
                render_artifact_pptx(artifact_type, sd, pptx_path, gaps=gaps,
                                      artifact_id=artifact_id,
                                      user_id=_owner_uid)
            written.append((artifact_type, artifact_id, html_path, docx_path, pptx_path))
            outs = [b for b in (html_path, docx_path, pptx_path) if b]
            log.info("%s %s (%s) -> %s", artifact_type, artifact_id, "/".join(formats),
                     ", ".join(os.path.basename(b).split(".")[-1] for b in outs))
    return written

def write_index(root_dir: str, all_written: list) -> str:
    rows = []
    for entry in all_written:
        atype, aid, h, d, p = (entry + (None,)*5)[:5]
        long_name = PRODUCT_LONG_NAMES.get(atype, atype)
        def _rel(x): return os.path.relpath(x, root_dir) if x else ""
        # Python 3.11 rejects backslashes inside f-string expressions; build
        # the optional <a> snippets in plain strings first.
        _docx_cell = ('<a href="' + _rel(d) + '">docx</a>') if d else "—"
        _pptx_cell = ('<a href="' + _rel(p) + '">pptx</a>') if p else "—"
        rows.append(
            f'<tr><td style="{S_TD}"><b>{atype}</b><br>'
            f'<span style="{S_GAP}">{long_name}</span></td>'
            f'<td style="{S_TD}">{_esc(aid)}</td>'
            f'<td style="{S_TD}"><a href="{_rel(h)}">html</a></td>'
            f'<td style="{S_TD}">{_docx_cell}</td>'
            f'<td style="{S_TD}">{_pptx_cell}</td>'
            f'</tr>'
        )
    body = (f'<!DOCTYPE html><html><head><title>IBL Sample Exports</title></head>'
            f'<body style="{S_BODY}">'
            f'<h1 style="{S_H1}">IBL Sample Artifact Exports</h1>'
            f'<p>{len(all_written)} artifact(s).</p>'
            f'<table style="{S_TABLE}">'
            f'<thead><tr>'
            f'<th style="{S_TH_HEADER_ROW}">Product</th>'
            f'<th style="{S_TH_HEADER_ROW}">Artifact ID</th>'
            f'<th style="{S_TH_HEADER_ROW}">HTML</th>'
            f'<th style="{S_TH_HEADER_ROW}">DOCX</th>'
            f'<th style="{S_TH_HEADER_ROW}">PPTX</th>'
            f'</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            f'</table></body></html>')
    out = os.path.join(root_dir, "index.html")
    with open(out, "w") as f: f.write(body)
    return out

def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        print(__doc__); sys.exit(1)
    state_path = argv[1]
    out_dir = argv[2] if len(argv) > 2 else os.path.join(
        os.path.dirname(state_path), "exports")
    print(f"Reading {state_path}\nWriting to {out_dir}")
    written = export_session(state_path, out_dir)
    print(f"\n{len(written)} artifact(s) exported to {out_dir}")


if __name__ == "__main__":
    main()
