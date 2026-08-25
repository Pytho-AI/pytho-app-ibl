"""CCA-Matrix — ASCOPE × PMESII-PT spreadsheet sibling of CCA.

Per scratch/ibl/reports/EXCEL_DOCTRINAL_ALIGNMENT.md §3 (canonical hybrid):
the 48-cell ASCOPE × PMESII-PT grid is a spreadsheet primitive; the
parent CCA's narrative_assessment + protected-targets list stay in DOCX.
This product is the matrix half ONLY — analysts generate both the
parent CCA (full hybrid) and CCA-Matrix (XLSX) independently and
neither propagates edits to the other.

Schema reuses the same matrix_spec axes as the parent CCA so a regenerate
on either side produces the same 8 PMESII-PT × 6 ASCOPE grid; only the
output format differs.
"""
from ..common import (
    HEADER_AO,
    HEADER_CLASSIFICATION,
    HEADER_DTG,
    HEADER_PREPARED_BY,
    PROVENANCE_DIRECTIVE,
    PROVENANCE_HARD_RULE,
)

SPEC = {
    "name": "CCA-Matrix",
    "output_format": "xlsx",
    "output_formats": ["xlsx"],
    # No legacy_formats — CCA-Matrix is XLSX-only by design. The parent
    # CCA carries the DOCX rendition for analysts who want the hybrid.
    "shape": "matrix",
    "doctrine": {
        "citation": "ATP 2-01.3 §4-96 ASCOPE × PMESII-PT",
        "purpose": (
            "XLSX surface for the 48-cell ASCOPE × PMESII-PT civil-considerations "
            "matrix. Spreadsheet-canonical per EXCEL_DOCTRINAL_ALIGNMENT.md §3 — "
            "analysts sort, filter, and edit cell-by-cell during civic-baseline "
            "evolution; narrative synthesis lives in the parent CCA artifact."
            + PROVENANCE_DIRECTIVE
        ),
        "ordering_rule": "Matrix axes fixed: rows = PMESII-PT (8); cols = ASCOPE (6).",
    },
    "consumes_artifacts": ["CCA", "MCOO", "HUMINTSUM", "OSIR"],
    "matrix_spec": {
        "field": "ascope_pmesii_pt_matrix",
        "row_axis": ["political", "military", "economic", "social",
                      "information", "infrastructure",
                      "physical_environment", "time"],
        "col_axis": ["areas", "structures", "capabilities",
                      "organizations", "people", "events"],
        "row_label": "PMESII-PT Lens",
        "col_label": "ASCOPE Characteristic",
    },
    "header_fields": {
        "dtg":                HEADER_DTG,
        "area_of_operations": HEADER_AO,
        "area_of_interest":   {"source": "session", "view": "header",
                                "required": True,
                                "render_label": "Area of Interest",
                                "default": "{area_of_operations}"},
        "prepared_by":        HEADER_PREPARED_BY,
        "parent_cca_artifact_id": {"source": "ref:CCA", "view": "internal",
                                     "required": False,
                                     "render_label": "Parent CCA",
                                     "depends_on": ["artifact.CCA"],
                                     "guidance": "Companion CCA artifact_id when both exist; not required."},
        "classification":     HEADER_CLASSIFICATION,
    },
    "paragraph_fields": [
        {"name": "ascope_pmesii_pt_matrix",
         "view": "published",
         "shape": "grid_table",
         "render_label": "ASCOPE × PMESII-PT Matrix",
         "required": True,
         "guidance": (
             "48-cell grid. Rows = PMESII-PT operational variables (political, "
             "military, economic, social, information, infrastructure, "
             "physical_environment, time). Cols = ASCOPE characteristics (areas, "
             "structures, capabilities, organizations, people, events). Cell "
             "entries cite the upstream civil-considerations vertex VERBATIM (kbID). "
             "Empty cell is valid output when nothing in the AO occupies that "
             "intersection. "
             "NO MARKDOWN IN CELL VALUES — never wrap row labels, column headers, "
             "or cell entries in **double-asterisks**, *single-asterisks*, "
             "`backticks`, _underscores_, or any other markdown syntax. The XLSX "
             "renderer applies bold/italic via cell formatting; markdown characters "
             "surface as literal asterisks in the rendered sheet. Emit plain text "
             "for every row label, column header, and cell value. "
             "Per-cell provenance: emit a structured_data entry for each populated "
             "cell carrying _provenance = {kbID, source_tool, acquisition_dtg} so "
             "downstream consumers can trace the source. "
             "TODO(exporter): the current XLSX exporter does NOT yet render "
             "per-cell _provenance as XLSX cell comments — writer still emits "
             "_provenance in structured_data so a future exporter upgrade can "
             "surface them on hover without requiring a writer regeneration."
         ),
         "depends_on": [
             "cougaar.get_civil",
         ]},
    ],
    "hard_rules": [
        "Matrix axes are fixed: 8 PMESII-PT rows × 6 ASCOPE cols, no row/col added or removed.",
        "Every cell entry cites the upstream get_civil kbID (provenance per cell, rendered as XLSX comment).",
        "Never invent civil-entity IDs (C-001, C-101, etc.) — only IDs literally present in get_civil output.",
        PROVENANCE_HARD_RULE,
    ],
    "data_sources": [
        "Civil baseline: get_civil()",
        "Per ASCOPE category: get_civil(layer='areas'), get_civil(layer='structures'), etc.",
        "Civic SITREPs (rollups): list_project_docs(filter={type:'HUMINTSUM'}) + list_project_docs(filter={type:'OSIR'})",
        "MCOO for urban infrastructure overlap: get_project_doc(tag=<latest MCOO>)",
        "OPORD §5 (CMO annex when present): get_project_doc(tag='OPORD')",
        "Parent CCA artifact (when present): list_project_docs(filter={type:'CCA'}) — share narrative anchor",
    ],
    "post_emit_summary": "artifact_id, filled cell count out of 48, top 3 row/col intersections by entry count",
}
