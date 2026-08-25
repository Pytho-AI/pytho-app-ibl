"""Gemma4 OPORD/FRAGO scenario ingestion.

Reads the latest OPORD + every FRAGO (plus optional ANNEX_L_INTEL and
RoadToWar narrative) from the project doc catalog, feeds them to vLLM
Gemma4 with a structured-output spec, and applies the parsed result to:

  - geo-agent master map  — operations overlay (AO polygon, phase lines,
                            division/brigade boundaries, axes of advance,
                            NAIs / TAIs, engagement areas) written via the
                            `/api/v1/maps/{id}/sync?source=opord` endpoint
                            so re-runs UPDATE rather than duplicate.
  - in-memory `scenario`   — civil considerations (`KG.opord_derived_civil`),
                            Donovian disposition (`KG.opord_derived_threat`),
                            phase concept timeline (`KG.opord_derived_phases`),
                            and the operations overlay mirror
                            (`scenario.OPORD_OVERLAY`).

The `scenario.py` source file is NEVER modified. Mutations happen in the
imported module's in-memory dicts. A process restart wipes them — that is
the intended behavior: this is a derived view, not authoritative state.

Public entry point:
    `parse_and_apply_opord(state) -> dict`

The state argument is the intel-agent RunState (so list_project_docs can
see analyst-published artifacts as well as the static catalog); pass None
to use only the static PROJECT_DOCS catalog.

The Gemma4 output JSON is validated against a strict schema before any
writes happen. Malformed output raises ScenarioIngestError — no silent
graceful handling. Each field carries provenance back to a source
OPORD/FRAGO fragment (`source_doc_tag` + `source_fragment`) so analysts
can audit the parse.
"""
from __future__ import annotations

import json
import logging
import re
import time
import asyncio
import os
from typing import Any

import requests

from utils import scenario
from utils.llm import invoke_vllm_chat_async
from handlers.intel_agent import (
    tool_list_project_docs,
    tool_get_project_doc,
    _project_map_uuid,
    _GEO_AGENT_API_BASE,
)

log = logging.getLogger(__name__)


class ScenarioIngestError(Exception):
    """Gemma4 output failed schema validation, or geo-agent write failed.
    Always carries the offending payload (or a truncated view of it) so
    the analyst sees why the parse was rejected."""

    def __init__(self, message: str, *, payload: Any = None):
        self.payload = payload
        super().__init__(message)


# Document types fed to Gemma4. Order matters — OPORD first establishes
# mission/concept, FRAGOs apply deltas in order, ANNEX L Intel and
# RoadToWar give analyst-facing context. The parser instructs Gemma4 to
# treat later docs as authoritative overrides of earlier ones (FRAGO
# overrides OPORD for any conflicting field).
_INGEST_DOC_TYPES = {"OPORD", "FRAGO", "ANNEX", "ANNEX_L_INTEL", "RoadToWar"}


def _collect_source_docs(state) -> list[dict]:
    """Pull every doc whose type is in _INGEST_DOC_TYPES from the
    project-docs catalog (static + analyst-published). Sort:
      1. OPORD first (base order)
      2. ANNEX_L_INTEL / RoadToWar (narrative context)
      3. FRAGOs sorted by tag (FRAGO_26-04-A, FRAGO_26-04-B, ...)
    """
    listing = tool_list_project_docs({"filter": {}}, state)
    docs_meta = listing.get("docs") or []
    collected: list[dict] = []
    for meta in docs_meta:
        dtype = meta.get("type") or ""
        if dtype not in _INGEST_DOC_TYPES:
            continue
        tag = meta.get("tag") or ""
        full = tool_get_project_doc({"tag": tag}, state)
        if not isinstance(full, dict) or full.get("error"):
            log.warning("scenario_ingest: skipped tag=%s (fetch error)", tag)
            continue
        collected.append({
            "tag": tag,
            "type": dtype,
            "title": full.get("title") or meta.get("title") or tag,
            "body": full.get("body") or "",
            "dtg": full.get("dtg") or meta.get("dtg"),
        })

    def _sort_key(d):
        dtype = d.get("type") or ""
        tag = d.get("tag") or ""
        # OPORD before ANNEX/RoadToWar before FRAGO; within FRAGO sort by tag
        primary = {"OPORD": 0, "ANNEX_L_INTEL": 1, "ANNEX": 1,
                   "RoadToWar": 2, "FRAGO": 3}.get(dtype, 9)
        return (primary, tag)

    collected.sort(key=_sort_key)
    return collected


# Schema enforced on Gemma4's structured-output JSON. Each top-level key is
# required; per-item provenance (`source_doc_tag` + `source_fragment`) is
# required so analysts can audit which doc fed which field.
_REQUIRED_TOP_LEVEL_KEYS = (
    "operations_overlay",
    "civil",
    "threat_disposition",
    "phases",
)


def _build_prompt(docs: list[dict]) -> list[dict]:
    """Assemble messages for the Gemma4 structured-output parse.

    System message defines the JSON schema, the provenance requirement, the
    expected coverage (minimum counts per category), worked examples, and
    the Estonia coordinate cheat-sheet. User message concatenates each
    source doc with a clear header so the model knows which fragment to
    cite.

    NOTE: Provenance fields (`source_doc_tag`, `source_fragment`) MUST be
    emitted at the TOP LEVEL of every list item — NOT nested inside
    `attrs`. The validator's fallback that promotes from attrs is a
    backstop; it should not be exercised in steady state.
    """
    system = (
        "You are an intelligence-doctrine extraction engine for the IDX 26 "
        "exercise (AO ALPHA-NORTH, Estonia/Latvia border). Read the OPORD, "
        "FRAGOs, Annex L (Intel), and Road-to-War narrative, then emit a "
        "single JSON object with EXACTLY these top-level keys:\n"
        "  operations_overlay (object with `control_measures` list)\n"
        "  civil (list)\n"
        "  threat_disposition (list)\n"
        "  phases (list)\n"
        "\n"
        "==============================================================\n"
        "ABSOLUTE PROVENANCE RULE\n"
        "==============================================================\n"
        "EVERY item in EVERY list (control_measures, civil, "
        "threat_disposition, phases) MUST carry `source_doc_tag` and "
        "`source_fragment` at the TOP LEVEL of the item — NEVER nested "
        "inside `attrs`. Example, correct:\n"
        "  {\"kind\":\"objective\",\"name\":\"OBJ TARTU\",\n"
        "   \"geom\":{...},\n"
        "   \"source_doc_tag\":\"OPORD\",\n"
        "   \"source_fragment\":\"...verbatim 1-2 sentence excerpt...\",\n"
        "   \"attrs\":{\"phase\":\"III\",\"echelon\":\"corps\"}}\n"
        "INCORRECT (do NOT do this): putting source_doc_tag inside attrs.\n"
        "If a later FRAGO supersedes an OPORD field, cite the FRAGO tag.\n"
        "\n"
        "==============================================================\n"
        "COVERAGE EXPECTATIONS — be EXHAUSTIVE, not minimal\n"
        "==============================================================\n"
        "An OPORD + ANNEX_L + FRAGO + Road-to-War corpus of this scope "
        "supports:\n"
        "  - 15 to 25 control measures (NOT 5 — be thorough)\n"
        "  - 10 or more civil-consideration entries (cover all six ASCOPE "
        "buckets: areas, structures, capabilities, organizations, people, "
        "events)\n"
        "  - 10 or more threat_disposition entries — one PER DATED "
        "movement in Road-to-War, never merged\n"
        "  - exactly 4 phases (doctrinal: I Penetration, II Advance, III "
        "Decisive, IV Stabilization)\n"
        "If your draft has fewer than these counts, RE-READ the source "
        "docs and add the missing items. Do not stop at the obvious ones.\n"
        "\n"
        "==============================================================\n"
        "operations_overlay.control_measures — what to extract\n"
        "==============================================================\n"
        "Allowed `kind` values:\n"
        "  Maneuver / IPB: ao_polygon, phase_line, boundary, "
        "axis_of_advance, named_area_of_interest, "
        "targeted_area_of_interest, engagement_area, objective, "
        "nogo_area, slowgo_area.\n"
        "\n"
        "  CRITICAL — nogo_area / slowgo_area extraction rule:\n"
        "  The standing geo-agent overlay already classifies NATURAL "
        "  terrain (forest, wetland, water, urban, slope) into the "
        "  doctrinal restricted / severely-restricted layers from "
        "  worldcover + DEM data. Do NOT re-emit natural terrain. "
        "  Emit nogo_area / slowgo_area ONLY for CUSTOM, "
        "  OPORD-DECLARED control measures — areas that exist "
        "  because the OPORD says so, not because the ground is "
        "  naturally that way:\n"
        "    - Templated minefields (\"Donovian minefield grid HAWK\" "
        "      at a specific MGRS) — nogo_area\n"
        "    - Named obstacle belts / wire / tank-ditch lines from "
        "      the OPORD obstacle plan — nogo_area or slowgo_area\n"
        "    - Contamination / CBRN restricted area — nogo_area\n"
        "    - No-fly box / no-strike-list polygon tied to a specific "
        "      protected feature — nogo_area\n"
        "    - Friendly-imposed slowgo from prepared obstacles "
        "      (rubble, abatis, demolitions) — slowgo_area\n"
        "  REJECT (do not emit) if:\n"
        "    - source_fragment is generic prose like \"forested\", "
        "      \"low-lying\", \"off-road mobility\", \"weather degrades "
        "      trafficability\" — that's narrative, not a control "
        "      measure; worldcover handles it.\n"
        "    - geom is an axis-aligned bounding box invented from "
        "      vague text instead of a feature the OPORD names.\n"
        "    - the OPORD doesn't tie the area to a specific MGRS or "
        "      a specific named feature.\n"
        "  Each emitted nogo / slowgo MUST carry the source MGRS or "
        "  named OPORD feature in attrs (e.g. "
        "  attrs.mgrs=\"35VLF47000600\", "
        "  name=\"Donovian minefield grid HAWK\") sourced verbatim "
        "  from the OPORD — never invented.\n"
        "  Sustainment (FM 4-0 ch. 6 / ATP 4-90 / JP 4-0 annex E — "
        "use these doctrinal kinds INSTEAD OF a generic 'logistics_node':\n"
        "    Lines: main_supply_route, alternate_supply_route\n"
        "    Areas: brigade_support_area, division_support_area, "
        "logistics_support_area, combat_support_area, "
        "forward_logistics_element, aerial_port_of_debarkation, "
        "seaport_of_debarkation\n"
        "    Points: logistics_release_point, ammunition_transfer_point, "
        "refuel_on_the_move, class_iii_supply_point, class_v_supply_point, "
        "class_viii_supply_point, medical_treatment_facility, "
        "casualty_collection_point, maintenance_collection_point, "
        "supply_point.\n"
        "When the OPORD's sustainment paragraph names 'logistics nodes' "
        "or 'forward support' or 'MEDEVAC', extract those as the SPECIFIC "
        "doctrinal kind (e.g. LSA in Latvia rear, BSA forward in Estonia, "
        "MTF + CCP for the casevac infrastructure, MSR connecting the LSA "
        "to forward units). NEVER emit a generic 'logistics_node' kind.\n"
        "\n"
        "Each control measure: `kind`, `name` (short label), `geom` "
        "(GeoJSON: Polygon for areas, LineString for lines, Point for "
        "points — coordinates in [lon, lat] decimal-degree order, WGS84), "
        "`attrs` (free-form dict — include phase, echelon, owning unit), "
        "`source_doc_tag`, `source_fragment`. MGRS may be supplied as "
        "attrs.mgrs in addition to geom.\n"
        "\n"
        "Specifically expect to extract (this is the MINIMUM checklist — "
        "add more if the docs support them):\n"
        "  1. ao_polygon \"AO ALPHA-NORTH\" — polygon covering southern + "
        "     central Estonia from Estonia-Latvia border north to "
        "     Donovian-Estonian border. Approx bounds: south ~57.6N "
        "     (Latvia border), north ~58.5N (Tartu region), west ~25.5E, "
        "     east ~27.7E.\n"
        "  2. boundary \"1ID Center (Main Effort)\" — LineString or "
        "     Polygon along the Valga-Tartu axis (center of AO). Derive "
        "     even when not explicitly MGRS-bounded.\n"
        "  3. boundary \"2AD West Flank\" — west of Valga (lon < 26.0E).\n"
        "  4. boundary \"3ID East (Reserve/Security)\" — east of Valga "
        "     extending toward Tartu (lon > 26.2E).\n"
        "  5. axis_of_advance \"Axis Valga-Tartu\" — LineString from "
        "     Valga (26.05, 57.78) to Tartu (26.73, 58.38). MAIN EFFORT.\n"
        "  6. axis_of_advance west-flank axis (2AD) — supporting effort, "
        "     LineString west of Valga.\n"
        "  7. axis_of_advance east axis (3AD/3ID) — supporting / "
        "     reserve, LineString east of Valga.\n"
        "  8. objective \"OBJ VALGA\" — Point at Valga crossing site "
        "     (26.05, 57.78). Phase I decisive point.\n"
        "  9. objective \"OBJ PIKASILLA\" — Point at Pikasilla "
        "     (26.05, 57.95). Intermediate objective, Phase II→III "
        "     transition trigger.\n"
        " 10. objective \"OBJ TARTU\" — Point at Tartu (26.73, 58.38). "
        "     Phase III central-Estonia decisive objective.\n"
        " 11. named_area_of_interest \"NAI MISSO-SAATSE\" — Polygon "
        "     covering Donovian penetration corridor (Misso 57.60N "
        "     27.23E to Saatse 57.91N 27.71E). Enemy main breach zone.\n"
        " 12. named_area_of_interest \"NAI PIKASILLA\" — Point or small "
        "     polygon at Pikasilla for SA-15b #2 silent location (PIR "
        "     trigger from FRAGO).\n"
        " 13. targeted_area_of_interest \"TAI VALGA CROSSING\" — Point "
        "     at Valga crossing site, priority for fires.\n"
        " 14. engagement_area for the Donovian Corps fix in Phase II/III "
        "     (along the Valga-Tartu axis where main body is to be "
        "     destroyed).\n"
        " 15. phase_line \"PL FALCON\" — LineString near Pikasilla "
        "     (lat ~57.95N), where Donovian 2nd Tank Coy counter-attack "
        "     is assessed. Draw it close to Pikasilla, not as a generic "
        "     full-AO horizontal line.\n"
        "  + additional phase lines for D-Day / D+3 / D+10 / D+20 "
        "     transitions if extractable.\n"
        "  + CUSTOM slowgo / nogo control measures the OPORD names "
        "     directly: templated minefields (e.g. \"Donovian "
        "     minefield grid HAWK\" at the MGRS the OPORD prints), "
        "     friendly obstacle belts, CBRN restricted areas, "
        "     no-fly / restricted-fire boxes. NOT generic \"forest "
        "     degrades mobility\" prose — that's narrative; "
        "     worldcover already paints the forest layer.\n"
        "  + logistics-node markers in Latvia (rear support area).\n"
        "\n"
        "==============================================================\n"
        "WORKED CONTROL-MEASURE EXAMPLES (copy this shape)\n"
        "==============================================================\n"
        "Axis of advance (LineString):\n"
        "  {\"kind\":\"axis_of_advance\",\n"
        "   \"name\":\"Axis Valga-Tartu (1ID Main Effort)\",\n"
        "   \"geom\":{\"type\":\"LineString\",\n"
        "     \"coordinates\":[[26.05,57.78],[26.30,58.05],"
        "[26.55,58.22],[26.73,58.38]]},\n"
        "   \"attrs\":{\"phase\":\"I-III\",\"echelon\":\"division\","
        "\"owning_unit\":\"1ID\",\"effort\":\"main\"},\n"
        "   \"source_doc_tag\":\"OPORD\",\n"
        "   \"source_fragment\":\"main effort crossing at Valga ... "
        "Advance north along the Valga axis to locate and fix enemy "
        "main body.\"}\n"
        "\n"
        "AO polygon (closed ring, last point == first):\n"
        "  {\"kind\":\"ao_polygon\",\n"
        "   \"name\":\"AO ALPHA-NORTH\",\n"
        "   \"geom\":{\"type\":\"Polygon\",\n"
        "     \"coordinates\":[[[25.50,57.55],[27.80,57.55],"
        "[27.80,58.55],[25.50,58.55],[25.50,57.55]]]},\n"
        "   \"attrs\":{\"phase\":\"all\",\"echelon\":\"corps\"},\n"
        "   \"source_doc_tag\":\"OPORD\",\n"
        "   \"source_fragment\":\"Southern and central Estonia, from "
        "the current Estonia-Latvia border north to the Donovian-"
        "Estonian border.\"}\n"
        "\n"
        "Phase line near a named feature (NOT a generic horizontal "
        "line — anchor it to the named place):\n"
        "  {\"kind\":\"phase_line\",\n"
        "   \"name\":\"PL FALCON\",\n"
        "   \"geom\":{\"type\":\"LineString\",\n"
        "     \"coordinates\":[[25.80,57.95],[26.30,57.95]]},\n"
        "   \"attrs\":{\"phase\":\"II/III boundary\","
        "\"anchor\":\"Pikasilla\"},\n"
        "   \"source_doc_tag\":\"FRAGO_26-04-A\",\n"
        "   \"source_fragment\":\"Donovian counter-attack from 2nd Tank "
        "Coy assessed MOST LIKELY at PL FALCON.\"}\n"
        "\n"
        "==============================================================\n"
        "civil — ASCOPE coverage\n"
        "==============================================================\n"
        "Each civil entry: `id`, `ascope` (areas|structures|capabilities|"
        "organizations|people|events), `pmesii` (political|military|"
        "economic|social|information|infrastructure), `name`, `type`, "
        "`mgrs` (if stated), `status`, `details`, `source_doc_tag`, "
        "`source_fragment`.\n"
        "\n"
        "Populate ALL SIX ASCOPE buckets. Minimum expected entries (add "
        "more if docs support):\n"
        "  - areas: Estonia-Latvia border region; southern Estonian "
        "    occupied zone; Tartu metropolitan area; logistics nodes in "
        "    Latvia (rear support).\n"
        "  - structures: Valga rail crossing; Tartu power substation; "
        "    Tartu university; hospitals (LOAC-protected); the Misso-"
        "    Saatse road network.\n"
        "  - capabilities: humanitarian-assistance corridors; medical "
        "    evacuation infrastructure; civil-comms networks.\n"
        "  - organizations: Estonian government / civil authorities; "
        "    NATO host-nation support in Latvia.\n"
        "  - people: Estonian civilian population (displaced/under "
        "    occupation in south); refugees along border.\n"
        "  - events: staged 'refugee incidents' (cited in Road-to-War); "
        "    cyber operations attributed to 'Estonian extremists'; "
        "    political demands cycle.\n"
        "\n"
        "==============================================================\n"
        "threat_disposition — extract EVERY dated Road-to-War movement\n"
        "==============================================================\n"
        "Each entry: `id`, `name`, `echelon`, `class` (mech_inf/armor/"
        "sam/arty/recon/ew/hq/ada/avn/eng/sig/at/fa/recon_bde), `mgrs`, "
        "`loc` (lat/lon string), `strength`, `activity`, `parent`, "
        "`origin_mgrs`, `origin_loc`, `destination_mgrs`, "
        "`destination_loc`, `dtg`, `source_doc_tag`, `source_fragment`.\n"
        "\n"
        "ABSOLUTE RULE: NEVER merge two dated events into one entry. If "
        "Road-to-War says '05 FEB: 64 MTZD INF DIV and 66 MTZD INF DIV "
        "move to assembly areas near Misso-Saatse' — that is TWO "
        "entries (one for 64 MTZD, one for 66 MTZD), each with "
        "dtg=05FEB42 and the same destination. If a brigade has "
        "multiple dated events (e.g. 600 CBT ENG BDE on 18 JAN, 18 "
        "FEB, 14 MAR, 05 APR), emit a SEPARATE entry for EACH date.\n"
        "\n"
        "Minimum expected threat-disposition entries from Road-to-War "
        "(all dates 2042; do not skip any):\n"
        "  03 JAN — HQ Western Army Warning Order\n"
        "  05 JAN — 6 IFC Army staff 100% manning\n"
        "  08 JAN — 60/64/66 division HQ partial mobilization\n"
        "  12 JAN — 644 RECON BDE → border observation posts\n"
        "  15 JAN — 676 SIG BDE C2 networks\n"
        "  18 JAN — 600 CBT ENG BDE pre-positioning bridging\n"
        "  22 JAN — 62 CBT AVN BDE → forward operating bases\n"
        "  25 JAN — 166 AT BDE + 167 ADA BDE initial positions\n"
        "  02 FEB — 60 MECH IFV DIV live-fire exercises\n"
        "  05 FEB — 64 MTZD INF DIV → Misso-Saatse assembly area "
        "(separate entry)\n"
        "  05 FEB — 66 MTZD INF DIV → Misso-Saatse assembly area "
        "(separate entry)\n"
        "  08 FEB — 644 RECON BDE intensified cross-border patrols\n"
        "  12 FEB — 362 + 364 FA BDE training fires\n"
        "  15 FEB — 62 CBT AVN BDE surge readiness\n"
        "  18 FEB — 600 CBT ENG BDE river-crossing rehearsals\n"
        "  22 FEB — 166 AT BDE + 167 ADA BDE emplacement complete\n"
        "  07 MAR — 644 RECON BDE forward OPs INSIDE Estonia\n"
        "  10 MAR — 60/64/66 corps-level maneuver rehearsal\n"
        "  14 MAR — 600 CBT ENG BDE GMZ-3 mine corridors\n"
        "  21 MAR — 62 CBT AVN BDE deep-strike rehearsal\n"
        "  02 APR — ALL formations → final assembly 30-50km Misso-"
        "Saatse\n"
        "  05 APR — 600 CBT ENG BDE final breaching rehearsals\n"
        "  15 APR — 62 CBT AVN BDE attack-helo squadrons reposition\n"
        "  18 APR — 362 + 364 FA BDE final registration fires\n"
        "  01 MAY (D-Day) 0300Z — coordinated corps attack across "
        "Misso-Saatse sector (lead: 60 MECH IFV + 64/66 MTZD INF)\n"
        "Extract every dated entry you see — the list above is illustrative; "
        "if Road-to-War contains additional dated movements, emit those too.\n"
        "\n"
        "==============================================================\n"
        "phases — exactly 4 doctrinal phases\n"
        "==============================================================\n"
        "Each phase entry: `id` (e.g. PHASE_1), `name` (e.g. "
        "'Penetration'), `start_dtg` (DDHHMMZ or DDHHMMZmonYY), "
        "`end_dtg`, `concept`, `decisive_point`, `phase_objectives` "
        "(list of strings), `source_doc_tag`, `source_fragment`.\n"
        "  PHASE_1 Penetration   D-Day=090400Z APR 26 to D+3\n"
        "  PHASE_2 Advance to Contact   D+4 to D+10\n"
        "  PHASE_3 Decisive Operations  D+11 to D+20\n"
        "  PHASE_4 Stabilization        D+21 onward\n"
        "\n"
        "==============================================================\n"
        "ESTONIA COORDINATE CHEAT-SHEET (use EXACTLY these — do NOT "
        "round to 25E, 27E, etc.)\n"
        "==============================================================\n"
        "  Valga          26.05 E, 57.78 N\n"
        "  Pikasilla      26.05 E, 57.95 N\n"
        "  Tartu          26.73 E, 58.38 N\n"
        "  Misso          27.23 E, 57.60 N\n"
        "  Saatse         27.71 E, 57.91 N\n"
        "AO ALPHA-NORTH spans roughly 25.5 E to 27.8 E, 57.55 N to "
        "58.55 N. Do not place Valga at 25 E, Tartu at 27 E, or any "
        "named feature outside its coordinates above. If the source "
        "supplies an MGRS string, convert to WGS84 lon/lat for `geom` "
        "and preserve the MGRS in attrs.mgrs.\n"
        "\n"
        "==============================================================\n"
        "OUTPUT FORMAT\n"
        "==============================================================\n"
        "Emit ONLY the JSON object. No markdown fences, no prose, no "
        "preamble, no commentary."
    )
    parts = []
    for d in docs:
        header = f"=== {d['tag']} ({d['type']}) — {d['title']} ===\n"
        if d.get("dtg"):
            header += f"DTG: {d['dtg']}\n"
        parts.append(header + (d.get("body") or ""))
    user = (
        "Parse the following IDX 26 documents into the JSON object specified. "
        "Apply FRAGOs as deltas on top of the OPORD; later FRAGOs supersede "
        "earlier ones. The Road-to-War narrative is HISTORICAL context — use "
        "it to populate threat_disposition movement vectors (origin → "
        "destination) for the Donovian build-up; do NOT promote Road-to-War "
        "scheduled rehearsals into operations_overlay control measures (the "
        "overlay tracks the CURRENT operation, not the build-up).\n"
        "\n"
        "REMINDERS before you emit:\n"
        "  1. control_measures should number 15 to 25 — re-check the "
        "checklist (AO polygon, 3 division boundaries, 3+ axes, OBJ "
        "VALGA, OBJ PIKASILLA, OBJ TARTU, NAI MISSO-SAATSE, NAI "
        "PIKASILLA, TAI VALGA CROSSING, engagement area, PL FALCON near "
        "Pikasilla, additional phase lines, slowgo/nogo, logistics).\n"
        "  2. civil should number 10 or more — cover ALL SIX ASCOPE "
        "buckets, not just `people`.\n"
        "  3. threat_disposition should have 10+ entries — ONE per "
        "DATED Road-to-War movement, NEVER merged. The 05 FEB entry "
        "for '64 MTZD and 66 MTZD' is TWO separate entries.\n"
        "  4. `source_doc_tag` and `source_fragment` go at the TOP "
        "LEVEL of every item, NOT inside `attrs`.\n"
        "  5. Use the coordinate cheat-sheet. Do not round Valga to "
        "25 E or Tartu to 27 E. Use the exact lon/lat given.\n"
        "  6. PL FALCON is at Pikasilla latitude (~57.95 N), not a "
        "generic east-west line.\n"
        "\n"
        + "\n\n".join(parts)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(raw: str) -> dict:
    """Strip optional markdown fences (Gemma4 occasionally wraps despite the
    system prompt forbidding it), then json.loads. No graceful catch — a
    malformed parse raises ScenarioIngestError to the caller."""
    s = (raw or "").strip()
    s = _FENCE_RE.sub("", s).strip()
    # Find first '{' and last '}' to recover from minor preamble/postamble.
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise ScenarioIngestError(
            "Gemma4 response is not a JSON object",
            payload=s[:500],
        )
    try:
        return json.loads(s[first:last + 1])
    except json.JSONDecodeError as e:
        raise ScenarioIngestError(
            f"Gemma4 response is not valid JSON: {e}",
            payload=s[:500],
        ) from e


def _validate(parsed: dict) -> None:
    """Check required top-level keys + per-item provenance."""
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in parsed:
            raise ScenarioIngestError(
                f"missing required top-level key: {key}",
                payload=list(parsed.keys()),
            )
    # operations_overlay carries a `control_measures` list; the other three
    # are lists directly.
    if not isinstance(parsed["operations_overlay"], dict):
        raise ScenarioIngestError(
            "operations_overlay must be an object with `control_measures`",
            payload=type(parsed["operations_overlay"]).__name__,
        )
    cms = parsed["operations_overlay"].get("control_measures")
    if not isinstance(cms, list):
        raise ScenarioIngestError(
            "operations_overlay.control_measures must be a list",
            payload=type(cms).__name__,
        )
    for key in ("civil", "threat_disposition", "phases"):
        if not isinstance(parsed[key], list):
            raise ScenarioIngestError(
                f"{key} must be a list",
                payload=type(parsed[key]).__name__,
            )
    # Provenance check: every item in every list needs source_doc_tag.
    for key in ("civil", "threat_disposition", "phases"):
        for i, item in enumerate(parsed[key] or []):
            if not isinstance(item, dict):
                raise ScenarioIngestError(
                    f"{key}[{i}] is not an object", payload=item)
            if not item.get("source_doc_tag"):
                raise ScenarioIngestError(
                    f"{key}[{i}] missing source_doc_tag",
                    payload=item.get("id") or item.get("name") or i)
    for i, cm in enumerate(cms or []):
        if not isinstance(cm, dict):
            raise ScenarioIngestError(
                f"operations_overlay.control_measures[{i}] is not an object",
                payload=cm)
        # Accept source_doc_tag at top level OR inside attrs (Gemma4
        # tends to wrap provenance into attrs unless told otherwise).
        # Promote attrs.source_doc_tag / attrs.source_fragment up so
        # downstream geo-agent payload builder sees them consistently.
        if not cm.get("source_doc_tag"):
            attrs = cm.get("attrs") or {}
            if attrs.get("source_doc_tag"):
                cm["source_doc_tag"] = attrs["source_doc_tag"]
                if attrs.get("source_fragment") and not cm.get("source_fragment"):
                    cm["source_fragment"] = attrs["source_fragment"]
            else:
                raise ScenarioIngestError(
                    f"operations_overlay.control_measures[{i}] missing source_doc_tag",
                    payload=cm.get("name") or i)

    # Drop hallucinated nogo/slowgo polygons — the standing worldcover
    # overlay already classifies natural terrain into the doctrinal
    # restricted layers. Gemma4 reads OPORD prose like "may degrade
    # off-road mobility in forested areas" and emits a 1°×0.2° axis-
    # aligned rectangle labelled FOREST/LOW-LYING TERRAIN, which then
    # renders as a giant green-hatched rectangle on the MCOO — exactly
    # the visual the analyst complained about. Keep CUSTOM OPORD-
    # declared slowgo/nogo (templated minefields, friendly obstacle
    # belts, CBRN restricted areas) which name a specific feature with
    # a specific MGRS / coord; drop everything else.
    cms_clean = []
    for cm in cms or []:
        kind = (cm.get("kind") or "").lower()
        if kind in ("nogo_area", "slowgo_area"):
            attrs = cm.get("attrs") or {}
            mgrs_anchor = (attrs.get("mgrs") or "").strip()
            fragment = (cm.get("source_fragment") or "").lower()
            generic_terrain = any(t in fragment for t in (
                "forest", "low-lying", "low lying", "wetland",
                "off-road mobility", "off road mobility",
                "weather degrad", "trafficability",
                "vegetation", "swamp", "marsh",
            ))
            if not mgrs_anchor or generic_terrain:
                log.warning(
                    "scenario_ingest: dropped hallucinated %s control "
                    "measure name=%r — no MGRS anchor / generic-terrain "
                    "source_fragment %r. Natural terrain comes from "
                    "worldcover overlay, not from the OPORD parser.",
                    kind, cm.get("name"), fragment[:120],
                )
                continue
            # Geometry shape check: if the polygon is an axis-aligned
            # rectangle (4 unique corners on 2 lon values × 2 lat values)
            # AND not anchored to the MGRS we just verified — that's a
            # bounding-box hallucination. Reject.
            geom = cm.get("geom") or {}
            if geom.get("type") == "Polygon":
                coords = (geom.get("coordinates") or [[]])[0] or []
                lons = sorted({round(float(c[0]), 5) for c in coords
                                if isinstance(c, (list, tuple)) and len(c) >= 2})
                lats = sorted({round(float(c[1]), 5) for c in coords
                                if isinstance(c, (list, tuple)) and len(c) >= 2})
                if len(lons) == 2 and len(lats) == 2 and len(coords) <= 6:
                    # 4 corners on 2 distinct lons × 2 distinct lats =
                    # axis-aligned rectangle; with a leading closing
                    # vertex you get 5 entries (last == first).
                    log.warning(
                        "scenario_ingest: dropped axis-aligned-rectangle "
                        "%s control measure name=%r — bounding-box "
                        "shape suggests Gemma4 invented coordinates "
                        "from prose rather than reading them from the "
                        "OPORD.", kind, cm.get("name"),
                    )
                    continue
        cms_clean.append(cm)
    parsed["operations_overlay"]["control_measures"] = cms_clean
    cms = cms_clean


# Mapping from the Gemma4 `kind` enum to the geo-agent paint-layer `kind`
# values consumed by ui/src/geo-agent/utils/tacticalLayers.maplibre.js.
# Mirrors utils.oob._SPATIAL_CONTROL_KIND_MAP — extending one
# requires extending both.
_OPORD_KIND_TO_GEO_AGENT: dict[str, tuple[str, str | None]] = {
    # geo-agent has no dedicated "area_of_operations" polygon kind; the AO
    # is doctrinally defined by its boundaries (lateral/forward/rear). We
    # map ao_polygon to `restricted_op_zone` so it renders as a polygon
    # outline + tag the attrs with opord_kind=ao_polygon so downstream
    # consumers know the doctrinal intent.
    "ao_polygon":               ("restricted_op_zone",          None),
    "phase_line":               ("phase_line",                  None),
    "boundary":                 ("boundary",                    None),
    "axis_of_advance":          ("direction_of_attack",         "main"),
    "named_area_of_interest":   ("named_area_of_interest",      None),
    "targeted_area_of_interest":("targeted_area_of_interest",   None),
    "engagement_area":          ("engagement_area",             None),
    "objective":                ("objective",                   None),
    "nogo_area":                ("severely_restricted_terrain", None),
    "slowgo_area":              ("restricted_terrain",          None),
    # Sustainment overlay (FM 4-0 ch. 6 / ATP 4-90 / JP 4-0 annex E).
    # Lines:
    "main_supply_route":        ("main_supply_route",           None),
    "alternate_supply_route":   ("alternate_supply_route",      None),
    # Areas:
    "brigade_support_area":     ("brigade_support_area",        None),
    "division_support_area":    ("division_support_area",       None),
    "logistics_support_area":   ("logistics_support_area",      None),
    "combat_support_area":      ("combat_support_area",         None),
    "forward_logistics_element":("forward_logistics_element",   None),
    "aerial_port_of_debarkation":("aerial_port_of_debarkation", None),
    "seaport_of_debarkation":   ("seaport_of_debarkation",      None),
    # Points:
    "logistics_release_point":  ("logistics_release_point",     None),
    "ammunition_transfer_point":("ammunition_transfer_point",   None),
    "refuel_on_the_move":       ("refuel_on_the_move",          None),
    "class_iii_supply_point":   ("class_iii_supply_point",      None),
    "class_v_supply_point":     ("class_v_supply_point",        None),
    "class_viii_supply_point":  ("class_viii_supply_point",     None),
    "medical_treatment_facility":("medical_treatment_facility", None),
    "casualty_collection_point":("casualty_collection_point",   None),
    "maintenance_collection_point":("maintenance_collection_point", None),
    "supply_point":             ("supply_point",                None),
    # Generic fallback the prompt is now told NOT to use — kept here so an
    # un-updated upstream that emits "logistics_node" still routes somewhere
    # plausible (LSA is the corps-level sustainment area, which is the
    # IDX 26 scenario's Latvia-rear logistics hub).
    "logistics_node":           ("logistics_support_area",      None),
}


def _build_geo_agent_payload(control_measures: list[dict]) -> dict:
    """Translate Gemma4-emitted control measures into the geo-agent
    `{overlays, entities}` shape. Mirrors
    utils.oob.build_geo_agent_sync_payload structure — every overlay
    carries `attrs.source = 'opord'` so the master-map sync transaction
    only replaces OPORD-derived rows.

    Skips entries with no `geom` field — the writer needs at least
    coordinates to plot. Provenance fields (source_doc_tag,
    source_fragment) flow through to attrs so geo-agent tooltips can show
    them.
    """
    overlays: list[dict] = []
    for cm in control_measures:
        mapped = _OPORD_KIND_TO_GEO_AGENT.get(cm.get("kind") or "")
        if not mapped:
            log.info("scenario_ingest: dropping CM unknown kind=%s name=%s",
                      cm.get("kind"), cm.get("name"))
            continue
        geom = cm.get("geom")
        if not geom:
            log.info("scenario_ingest: dropping CM no geom name=%s", cm.get("name"))
            continue
        kind, subkind = mapped
        attrs = dict(cm.get("attrs") or {})
        attrs["source"] = "opord"
        attrs["source_doc_tag"] = cm.get("source_doc_tag")
        attrs["source_fragment"] = cm.get("source_fragment")
        attrs["opord_kind"] = cm.get("kind")
        if subkind:
            attrs["subkind"] = subkind
        overlays.append({
            "kind": kind,
            "name": cm.get("name") or (attrs.get("source_doc_tag") or "OPORD-CM"),
            "geom": geom,
            "attrs": {k: v for k, v in attrs.items() if v is not None},
        })
    return {"overlays": overlays, "entities": []}


def _push_to_geo_agent(map_id: str, payload: dict) -> dict:
    """Transactional sync to geo-agent. Same endpoint shape as
    `intel_agent._push_cougaar_to_geo_agent` but with `source=opord` so
    Cougaar-source rows are NOT touched. Re-running the OPORD parse
    REPLACES every opord-source row on the master map.

    Raises ScenarioIngestError on HTTP failure — no silent graceful return.
    The caller is the analyst clicking the green refresh icon; they need
    to see the failure surface.
    """
    base = _GEO_AGENT_API_BASE
    url = f"{base}/maps/{map_id}/sync"
    try:
        r = requests.post(url, params={"source": "opord"},
                          json=payload, timeout=30)
    except Exception as exc:
        raise ScenarioIngestError(
            f"geo-agent /sync transport error: {type(exc).__name__}: {exc}",
            payload={"url": url, "overlays": len(payload.get("overlays") or [])},
        ) from exc
    if r.status_code >= 400:
        raise ScenarioIngestError(
            f"geo-agent /sync HTTP {r.status_code}",
            payload={"url": url, "body": r.text[:400]},
        )
    return r.json()


def _apply_in_memory(parsed: dict, overlay_payload: dict) -> None:
    """Write derived dicts into the imported `scenario` module's namespace.

    `scenario.py` source file is NOT modified — this attaches/replaces
    attributes on the imported module object, which next-restart will
    discard.

    Sibling keys (KG.opord_derived_civil, KG.opord_derived_threat,
    KG.opord_derived_phases) avoid colliding with any concurrent enrichment
    of the existing KG.civil / KG.unit_red performed by sibling subagents.
    """
    kg = scenario.KG
    kg["opord_derived_civil"] = list(parsed.get("civil") or [])
    kg["opord_derived_threat"] = list(parsed.get("threat_disposition") or [])
    kg["opord_derived_phases"] = list(parsed.get("phases") or [])
    # Overlay mirror — read by tool_get_spatial_overlay so the intel-agent
    # doesn't have to re-fetch from geo-agent every call.
    overlay = dict(parsed.get("operations_overlay") or {})
    overlay["_geo_agent_payload"] = overlay_payload
    overlay["_ingested_at_monotonic"] = time.monotonic()
    setattr(scenario, "OPORD_OVERLAY", overlay)


async def parse_and_apply_opord(state) -> dict:
    """Public entry point. Synchronous from the caller's perspective —
    blocks for 30-90s while Gemma4 chews through the docs.

    Pipeline:
      1. Collect every OPORD/FRAGO/ANNEX_L_INTEL/RoadToWar doc.
      2. Build the Gemma4 prompt + structured-output schema.
      3. Invoke vLLM Gemma4 (max_tokens=8000 — the response is long
         structured JSON; thinking enabled so the model can reason about
         FRAGO-over-OPORD overrides).
      4. Strip markdown fences (if any), parse JSON, validate schema.
      5. Build the geo-agent payload from operations_overlay.
      6. POST to geo-agent /sync?source=opord on the master map UUID. Re-
         runs UPDATE because the endpoint transactionally replaces every
         opord-source row.
      7. Apply civil/threat/phases/overlay to the in-memory scenario
         module dicts.
      8. Return a summary dict for the UI.

    Returns:
        {status: "ok",
         overlay_summary: {kinds: {kind: count}, total: N},
         civil_count: N, threat_count: N,
         phases: [phase_names],
         source_docs: [tag, ...],
         duration_sec: float}

    Raises ScenarioIngestError on any failure — no silent graceful return.
    """
    t0 = time.monotonic()
    docs = _collect_source_docs(state)
    if not docs:
        raise ScenarioIngestError(
            "no OPORD/FRAGO/ANNEX/RoadToWar docs available to parse",
            payload=None,
        )
    log.info("scenario_ingest: parsing %d source docs", len(docs))

    messages = _build_prompt(docs)
    # Generous ceiling — the exhaustive-coverage prompt drives Gemma4 to
    # emit ~20 control measures + 12 civil + 22 threat entries each with
    # source_fragment quotes. Observed truncation at max_tokens=16000 and
    # 24000 in live runs; thinking-trace consumes a large share of the
    # budget. Disable thinking for the JSON pass (the prompt is already
    # rigorous enough to drive the structure without trace reasoning),
    # which gives Gemma4 the full token budget for the actual output.
    resp = await invoke_vllm_chat_async(
        messages, thinking=False, timeout=360, max_tokens=24000, temperature=0.1,
    )
    raw_content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    parsed = _extract_json(raw_content)
    _validate(parsed)

    cms = parsed["operations_overlay"].get("control_measures") or []
    overlay_payload = _build_geo_agent_payload(cms)
    # Target the PROJECT's master map (map uuid == projectId) — the
    # OPORD-derived overlay belongs to the event whose docs were parsed.
    sync_response = _push_to_geo_agent(
        _project_map_uuid(getattr(state, "project_id", "")), overlay_payload)
    _apply_in_memory(parsed, overlay_payload)

    duration_sec = round(time.monotonic() - t0, 2)
    overlay_kinds: dict[str, int] = {}
    for cm in cms:
        k = cm.get("kind") or "unknown"
        overlay_kinds[k] = overlay_kinds.get(k, 0) + 1
    return {
        "status": "ok",
        "overlay_summary": {
            "kinds": overlay_kinds,
            "total": len(cms),
            "geo_agent_response": sync_response,
        },
        "civil_count": len(parsed.get("civil") or []),
        "threat_count": len(parsed.get("threat_disposition") or []),
        "phases": [p.get("name") or p.get("id") for p in (parsed.get("phases") or [])],
        "source_docs": [d["tag"] for d in docs],
        "duration_sec": duration_sec,
    }


def parse_and_apply_opord_sync(state) -> dict:
    """Sync wrapper for `parse_and_apply_opord` so the FastAPI endpoint can
    call it from a thread without juggling event loops. `asyncio.run`
    builds a fresh event loop per call — fine because Gemma4 is only one
    HTTP round-trip and the geo-agent sync is plain `requests.post`."""
    return asyncio.run(parse_and_apply_opord(state))
