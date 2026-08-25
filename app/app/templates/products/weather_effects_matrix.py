"""WEM — Weather Effects Matrix — ATP 2-01.3 Fig 4-17.

See templates/__init__.py for the assembled PRODUCT_SCHEMAS map.
"""
from ..common import (
    HEADER_CLASSIFICATION,
    HEADER_DTG,
    HEADER_PHASE,
    HEADER_PREPARED_BY,
    WEM_OPERATIONS_FIG_4_17,
    WEM_RATING_3TIER_ENUM,
    PROVENANCE_DIRECTIVE,
    PROVENANCE_HARD_RULE,
)

SPEC = {
    "name": "WEM",
    "output_format": "docx",
    # XLSX-canonical per EXCEL_DOCTRINAL_ALIGNMENT.md §2. ATP 2-01.3
    # Fig 4-17: operation × parameter × {favorable, unfavorable, current,
    # assessment} — updated each SWO forecast cycle.
    "output_formats": ["xlsx"],
    "legacy_formats": ["docx"],
    "shape": "structured",
    # SWO forecast validity window default. ATP 3-04.94 / ATP 2-01.3 cycle —
    # tactical weather effects matrices typically valid 24h. Analyst can
    # override per-cycle via session context.
    "cadence_hours": 24,
    "doctrine": {
        "purpose": (
            "Step-2 IPOE work aid translating SWO forecast values into operational "
            "impact per operation/system. Hierarchical: operation group → weather-parameter "
            "rows with FAVORABLE / UNFAVORABLE thresholds. A 3-tier alternate form exists.\n\n"
            "WEATHER DATA PROVENANCE (mandatory): the underlying weather values in this "
            "scenario are CLIMATOLOGY-MODELED, NOT a live SWO forecast. The writer MUST "
            "cite the climatology source (from `get_environmental.data_source` / "
            "`get_environmental.weather.source`) in the artifact-level `_provenance` under "
            "key `swo_attribution`. The SWO product id (`weather.id` e.g. 'W-281200') is "
            "the FIXTURE handle; the climatology citation is the SOURCE OF TRUTH for the "
            "values. Both belong in provenance — product id in `header.swo_attribution`, "
            "climatology source in `_provenance.swo_attribution`."
            + PROVENANCE_DIRECTIVE
        ),
        "ordering_rule": "Operation groups in canonical order; parameter rows per operation in canonical order.",
    },
    "consumes_artifacts": [],
    "header_fields": {
        # Phase is required for context (which OPORD phase this WEM cycle
        # supports). Default to the session phase token; if absent the writer
        # MUST populate from session.phase / OPORD §3.b — never leave empty.
        "phase":              {**HEADER_PHASE, "default": "{phase}",
                                "render_label": "Phase"},
        "dtg":                HEADER_DTG,
        "area_of_interest":   {"source": "session", "view": "header", "required": True,
                                "default": "{area_of_operations}",
                                "render_label": "Area of Interest"},
        # SWO forecast validity window — runner-computed (end = scenario
        # canonical-now, start = end - cadence_hours). Writer never fills.
        "valid_window_start_dtg": {"source": "computed", "view": "header", "required": True,
                                     "render_label": "Valid Window Start"},
        "valid_window_end_dtg":   {"source": "computed", "view": "header", "required": True,
                                     "render_label": "Valid Window End"},
        "prepared_by":        HEADER_PREPARED_BY,
        "swo_attribution":    {"source": "analytical", "view": "header", "required": True,
                                 "provenance_required": True,
                                 "render_label": "SWO Source",
                                 "depends_on": ["cougaar.get_environmental"],
                                 # Climatology provenance is MANDATORY — every WEM cycle in
                                 # this scenario is climatology-modeled, not a live forecast.
                                 # Default cite shape if the writer fails to assemble it from
                                 # get_environmental.data_source: at minimum the climatology
                                 # source string must appear in _provenance.swo_attribution.
                                 "default_provenance": "{get_environmental.weather.id} / {get_environmental.data_source}",
                                 "guidance": (
                                     "SWO product ID + DTG of forecast (mandatory). "
                                     "Read VERBATIM from the get_environmental response — copy "
                                     "the value of `weather.id` exactly (e.g., 'W-281200'), then "
                                     "append ' @ <weather.current_dtg>' (e.g., ' @ 281200Z'). "
                                     "Do not invent a new product id by mixing the analyst's "
                                     "window DTG into the id (e.g., 'W-291800' is wrong if the "
                                     "weather.id is 'W-281200'). The product id identifies the "
                                     "SWO product; the DTG identifies when it was issued.\n\n"
                                     "`_provenance` for this field MUST cite BOTH the SWO "
                                     "product id AND the underlying climatology source. The "
                                     "scenario weather is climatology-modeled (not a live "
                                     "forecast), so the cite-by-source is the climatology "
                                     "dataset, not just the synthetic product id. The "
                                     "climatology source string is MANDATORY — pull it from "
                                     "`get_environmental.data_source` (or "
                                     "`get_environmental.weather.source` if data_source is "
                                     "absent) and include it verbatim. Format:\n"
                                     "  `get_environmental.weather.id` + ' / ' + "
                                     "`get_environmental.data_source`\n"
                                     "Example: 'W-281200 / NOAA Estonia regional climatology "
                                     "(Jun 2026 averages); scenario-authored from Baltic "
                                     "regional historical seasonal data'.\n\n"
                                     "If `get_environmental.data_source` is missing from the "
                                     "tool response, the writer MUST still emit a climatology "
                                     "attribution string ('climatology-modeled; source: "
                                     "<dataset name from get_environmental.weather.source>') — "
                                     "an empty / product-id-only swo_attribution is invalid."
                                 )},
        "classification":     HEADER_CLASSIFICATION,
    },
    "paragraph_fields": [
        {
            "name": "operations",
            "view": "published",
            "shape": "list_of_dicts",
            "render_label": "Operations / Systems",
            "required": True,
            "guidance": "List per-operation groups with weather-parameter rows. "
                        "Each operation = {operation, parameters: [{parameter, "
                        "favorable_threshold, unfavorable_threshold, current_forecast_value, "
                        "assessment, _provenance}]}",
            "depends_on": [
                "cougaar.get_environmental",
            ],
            "item_subfields": [
                {"name": "operation",
                 "render_label": "Operation",
                 "enum": WEM_OPERATIONS_FIG_4_17,
                 "enum_labels": {
                     "maneuver_tracked": "Maneuver (Tracked)",
                     "maneuver_with_night_vision_goggles_tracked": "Maneuver with NVG (Tracked)",
                     "dismounted_infantry": "Dismounted Infantry",
                     "weapon_positioning": "Weapon Positioning",
                     "fire_support_155mm": "Fire Support (155mm)",
                     "close_air_support_a10": "Close Air Support (A-10)",
                     "air_defense_artillery_vulcan_chaparral_stinger": "Air Defense Artillery (Vulcan/Chaparral/Stinger)",
                     "battlefield_obscuration": "Battlefield Obscuration",
                     "airborne_operations_c130_c17": "Airborne Operations (C-130/C-17)",
                     "aviation_rotary": "Aviation (Rotary)",
                     "medical_evacuation_plus_above_criteria": "MEDEVAC (plus all aviation criteria)",
                     "gray_eagle": "Gray Eagle",
                     "shadow_unmanned_aircraft_system": "Shadow UAS",
                     "manned_isr_rc12_mc12": "Manned ISR (RC-12 / MC-12)",
                     "visual_sensors": "Visual Sensors",
                     "infrared_sensors": "Infrared Sensors",
                     "engineer_operations": "Engineer Operations",
                     "nuclear_biological_chemical": "NBC",
                 },
                 "guidance": (
                     "MUST be one of the canonical WEM operation enum keys "
                     "below (ATP 2-01.3 Fig 4-17). Emit the snake_case enum "
                     "KEY verbatim — the XLSX exporter renders the "
                     "human-readable label from enum_labels at render time. "
                     "Allowed values: maneuver_tracked, "
                     "maneuver_with_night_vision_goggles_tracked, "
                     "dismounted_infantry, weapon_positioning, "
                     "fire_support_155mm, close_air_support_a10, "
                     "air_defense_artillery_vulcan_chaparral_stinger, "
                     "battlefield_obscuration, airborne_operations_c130_c17, "
                     "aviation_rotary, medical_evacuation_plus_above_criteria, "
                     "gray_eagle, shadow_unmanned_aircraft_system, "
                     "manned_isr_rc12_mc12, visual_sensors, infrared_sensors, "
                     "engineer_operations, nuclear_biological_chemical.\n\n"
                     "DO NOT emit OPORD phase labels like 'Phase I "
                     "(Penetration)' / 'Phase II (Maneuver)' / 'Phase III "
                     "(Consolidation)'. Phases are unit operational phases "
                     "(OPORD §3.b — Concept of the Operation); WEM rows are "
                     "doctrinal OPERATION TYPES (ATP 2-01.3 Fig 4-17). A "
                     "single WEM covers the operations relevant to the "
                     "current phase — one row per (operation × parameter) — "
                     "not the phases themselves."
                 )},
                {"name": "parameters",
                 "render_label": "Parameter Rows",
                 "shape": "list_of_dicts",
                 "item_subfields": [
                     {"name": "parameter",
                      "render_label": "Parameter",
                      "guidance": "weather parameter (Visibility, Ceiling, Rainfall, Surface Wind, etc.)"},
                     {"name": "favorable_threshold",
                      "render_label": "Favorable (No Degradation)",
                      "guidance": "canonical threshold for this operation. Constant — _provenance is NOT required (the basis is implicit in the WEM product type, not a per-claim source)."},
                     {"name": "unfavorable_threshold",
                      "render_label": "Unfavorable (Significant Degradation)",
                      "guidance": "canonical threshold for this operation. Constant — _provenance NOT required."},
                     {"name": "current_forecast_value",
                      "render_label": "Current Forecast",
                      "guidance": (
                          "SWO forecast value at valid_window. Read from the SWO environmental "
                          "forecast list — pick the entry whose `valid_dtg` matches "
                          "valid_window_end_dtg (or the closest entry on/before it). Map every "
                          "parameter to the corresponding structured field on that entry; do not "
                          "leave any forecast cell blank when the data exists:\n"
                          "  - Visibility ← `vis_km` (e.g., '5 km')\n"
                          "  - Ceiling ← `ceiling_ft` (e.g., '2500 ft')\n"
                          "  - Surface Wind ← `wind_kt` + `wind_dir_true` (e.g., '18 kt @ 270°')\n"
                          "  - Wind (3000 ft AGL) ← surface wind +5–10 kt unless aloft data is "
                          "in the response (mark _provenance as 'derived from surface wind' if "
                          "extrapolated)\n"
                          "  - Cloud Cover ← `cloud_cover` (e.g., 'broken')\n"
                          "  - Precipitation ← `precip` (e.g., 'light_rain')\n"
                          "  - Temperature ← `temp_c` (e.g., '9°C')\n"
                          "  - Atmospheric Pressure ← `pressure_inhg`\n"
                          "  - Humidity ← `humidity_pct`\n"
                          "  - Illumination ← `light_illumination[<dtg>]` block\n"
                          "  - Thermal Crossover ← `thermal_crossover` block\n"
                          "Express in the same units doctrine uses. NEVER paraphrase from the "
                          "operations expert's prose summary — read the structured field. "
                          "_provenance cites the weather product id only (e.g., 'W-281200'); "
                          "the DTG context is already in the cell value."
                      )},
                     {"name": "assessment",
                      "render_label": "Assessment",
                      "enum": WEM_RATING_3TIER_ENUM,
                      "guidance": (
                          "favorable | marginal | unfavorable. THREE-BUCKET DECISION RULE "
                          "(apply literally — do NOT collapse the middle band):\n"
                          "  1. If `current_forecast_value` is on the no-degradation side of "
                          "`favorable_threshold` (i.e., better than or equal to the favorable "
                          "limit) → FAVORABLE.\n"
                          "  2. ELSE IF `current_forecast_value` is on the significant-degradation "
                          "side of `unfavorable_threshold` (i.e., worse than or equal to the "
                          "unfavorable limit) → UNFAVORABLE.\n"
                          "  3. ELSE (the value sits BETWEEN the two thresholds) → MARGINAL. "
                          "This is the most common case for real forecasts and the most commonly "
                          "miscoded — any value strictly between the two limits is MARGINAL, not "
                          "UNFAVORABLE.\n\n"
                          "DIRECTION OF 'WORSE' is parameter-specific:\n"
                          "  - Ceiling / Visibility / Illumination: LOWER is worse. Favorable is "
                          "the HIGHER number; unfavorable is the LOWER number. Forecast BETWEEN "
                          "(higher than unfavorable, lower than favorable) = MARGINAL.\n"
                          "  - Wind / Precipitation / Temperature-extreme: HIGHER is worse. "
                          "Favorable is the LOWER number; unfavorable is the HIGHER number. "
                          "Forecast BETWEEN = MARGINAL.\n\n"
                          "WORKED EXAMPLE (Ceiling, aviation_rotary): favorable_threshold='1500 "
                          "ft AGL', unfavorable_threshold='500 ft AGL', current_forecast_value="
                          "'1200 ft'. Step 1: is 1200 ft >= 1500 ft? NO — not FAVORABLE. Step "
                          "2: is 1200 ft <= 500 ft? NO — not UNFAVORABLE. Step 3: 1200 ft sits "
                          "between 500 and 1500 → MARGINAL. (Common bug: writers see 1200 < "
                          "1500 and label UNFAVORABLE — wrong, that skips the MARGINAL band.)\n\n"
                          "WORKED EXAMPLE (Surface Wind, fire_support_155mm): favorable_threshold="
                          "'15 kt', unfavorable_threshold='30 kt', current_forecast_value='22 "
                          "kt'. Step 1: is 22 kt <= 15 kt? NO — not FAVORABLE. Step 2: is 22 "
                          "kt >= 30 kt? NO — not UNFAVORABLE. Step 3: 22 kt sits between 15 "
                          "and 30 → MARGINAL.\n\n"
                          "The artifact-level swo_attribution covers provenance for every "
                          "assessment — no per-row _provenance needed."
                      )},
                 ]},
            ],
        },
    ],
    "subtleties": [
        "Operation coverage: a typical mission-driven WEM covers 4–6 operations relevant to the mission. For a combined-arms posture pick from: maneuver_tracked, dismounted_infantry, fire_support_155mm, air_defense_artillery_vulcan_chaparral_stinger, aviation_rotary, medical_evacuation_plus_above_criteria. Add ISR systems only if explicitly in scope. Keep the operation list focused — a WEM with 10+ operations bloats the artifact without adding analytical value.",
        "Worked example of one well-formed operation entry. Read the IA's get_environmental.forecast list, pick the entry whose valid_dtg matches valid_window_end_dtg (291800Z in this scenario), then map each parameter to its structured field. NO per-row _provenance — the artifact-level swo_attribution covers it. Every operation I emit follows this pattern:\n\n"
        "```json\n"
        '{\n'
        '  "operation": "aviation_rotary",\n'
        '  "parameters": [\n'
        '    {"parameter": "Ceiling", "favorable_threshold": "1500 ft AGL", "unfavorable_threshold": "500 ft AGL", "current_forecast_value": "1800 ft", "assessment": "marginal"},\n'
        '    {"parameter": "Visibility", "favorable_threshold": "5 km", "unfavorable_threshold": "1.5 km", "current_forecast_value": "3 km", "assessment": "marginal"},\n'
        '    {"parameter": "Surface Wind", "favorable_threshold": "20 kt", "unfavorable_threshold": "35 kt", "current_forecast_value": "22 kt", "assessment": "marginal"},\n'
        '    {"parameter": "Cloud Cover", "favorable_threshold": "scattered", "unfavorable_threshold": "overcast", "current_forecast_value": "overcast", "assessment": "unfavorable"},\n'
        '    {"parameter": "Precipitation", "favorable_threshold": "none", "unfavorable_threshold": "moderate+", "current_forecast_value": "moderate_rain", "assessment": "unfavorable"}\n'
        '  ]\n'
        '}\n'
        "```\n"
        "Each parameter row is one compact dict on one line. Forecast values come from the IA's structured forecast entry (not prose). Assessments compare forecast vs threshold.",
        "WEM is an EFFECTS product, not raw observations. Translate SWO forecast values into operational impact per operation.",
        "SWO co-author is mandatory — cite SWO product ID in header.swo_attribution.",
        "Atmospheric pressure is conditional (only when aviation in scope).",
        "Multi-altitude wind required when aviation in scope.",
        "Thresholds are canonical for each operation; the analytical layer is the assessment of which side of threshold the SWO forecast falls.",
        "Every parametric threshold and every assessment must cite source in _provenance.",
        "Each operation in `operations` carries a populated `parameters` table. The parameter list is operation-specific AND MANDATORY — every row below must be emitted for the operations selected. Row counts in [brackets] are non-negotiable; under-emission is invalid output:",
        "- `maneuver_tracked`, `maneuver_with_night_vision_goggles_tracked`, `dismounted_infantry` [5 rows MANDATORY]: Visibility, Surface Wind, Precipitation, Temperature, Cloud Cover.",
        "- `weapon_positioning`, `fire_support_155mm`, `air_defense_artillery_vulcan_chaparral_stinger` [5 rows MANDATORY]: Visibility, Surface Wind (sustained + gust), Precipitation, Temperature, Cloud Cover.",
        "- `close_air_support_a10`, `airborne_operations_c130_c17`, `aviation_rotary`, `medical_evacuation_plus_above_criteria` [8 rows MANDATORY]: Ceiling, Visibility, Surface Wind, Wind (3000 ft AGL), Cloud Cover, Precipitation, Icing, Atmospheric Pressure. Emitting 3 of 8 is BROKEN OUTPUT — emit ALL EIGHT.",
        "- `gray_eagle`, `shadow_unmanned_aircraft_system`, `manned_isr_rc12_mc12` [6 rows MANDATORY]: Ceiling, Visibility, Surface Wind, Wind (operating altitude), Icing, Cloud Cover.",
        "- `visual_sensors` [4 rows MANDATORY]: Visibility, Cloud Cover, Illumination (BMNT/EENT/moon phase), Precipitation.",
        "- `infrared_sensors` [5 rows MANDATORY]: Visibility, Cloud Cover, Thermal Crossover Window, Precipitation, Humidity.",
        "- `engineer_operations` [4 rows MANDATORY]: Precipitation (24h), Soil Saturation, Temperature, Surface Wind.",
        "- `nuclear_biological_chemical` [4 rows MANDATORY]: Surface Wind, Atmospheric Stability, Precipitation, Temperature.",
        "- `battlefield_obscuration` [4 rows MANDATORY]: Surface Wind, Precipitation, Cloud Cover, Atmospheric Stability.",
    ],
    "hard_rules": [
        (
            "The `operation` field on each operations[] item MUST be one of "
            "the canonical WEM_OPERATIONS_FIG_4_17 enum keys (maneuver_tracked, "
            "dismounted_infantry, fire_support_155mm, aviation_rotary, etc. "
            "— see the operation field guidance for the full list). "
            "NEVER emit OPORD phase labels like 'Phase I (Penetration)', "
            "'Phase II (Maneuver)', 'Phase III'. Phases live in the header "
            "(header.phase); WEM operation rows are doctrinal operation "
            "TYPES per ATP 2-01.3 Fig 4-17. An operation with a non-enum "
            "key is invalid output and the XLSX exporter cannot render the "
            "human-readable label for it."
        ),
        "Never invent rating values without weather data basis.",
        "Cite the get_environmental DTG / measurement that drove each impact rating in row._provenance.",
        "Atmospheric pressure included only when aviation in scope.",
        "Every operation entry MUST carry a populated parameters list. An operation with `parameters: []` is invalid output and must not be emitted — either populate the canonical parameter rows for this operation or drop the operation entry entirely.",
        "Each parameter row is ONE emitted dict containing parameter, favorable_threshold, unfavorable_threshold, current_forecast_value, assessment, and _provenance — together. Do not emit two half-rows (one with the parameter name and thresholds, another with the forecast and assessment). The renderer treats each emitted dict as one table row.",
        "swo_attribution is COPIED VERBATIM from get_environmental.weather.id — never synthesized, never modified. If the IA response shows weather.id='W-281200', emit swo_attribution='W-281200 @ 281200Z'. Do NOT construct an id like 'W-291800' to match the analyst's window — that's invention. The id identifies the SWO product (issued at one point in time); the @ DTG suffix identifies its issue time.",
        "swo_attribution._provenance MUST include the climatology source string (pulled from get_environmental.data_source or get_environmental.weather.source). The scenario weather is climatology-modeled, not a live forecast, so the cite-by-source is the climatology dataset — a _provenance that lists only the SWO product id is incomplete and invalid. If data_source is missing from the tool response, emit a climatology attribution string ('climatology-modeled; source: <dataset name>') — never an empty climatology cite.",
        "Three-bucket assessment arithmetic is MANDATORY. A `current_forecast_value` that sits strictly BETWEEN `favorable_threshold` and `unfavorable_threshold` is MARGINAL — never UNFAVORABLE. Collapsing the middle band (skipping straight from FAVORABLE to UNFAVORABLE) is a numeric bug and produces wrong assessments. See the assessment field guidance for the literal decision rule and worked examples.",
        "Required header fields phase + area_of_interest are NEVER empty. phase pulls from session.phase (OPORD §3.b — Concept of the Operation); area_of_interest pulls from session.area_of_operations. If the session token is missing, the writer MUST emit the current OPORD phase label (e.g., 'Phase II') and the AOI name (e.g., 'Voru–Tartu corridor') verbatim from the OPORD project doc — never an empty string.",
        (
            "Per-operation parameter completeness — MANDATORY ROW COUNTS. Each "
            "operation's parameters list must include EVERY parameter specified for "
            "that operation type (see the operation-specific parameter list in "
            "subtleties). Under-emission is invalid output:\n"
            "  - maneuver_tracked / maneuver_with_night_vision_goggles_tracked / "
            "dismounted_infantry → exactly 5 rows.\n"
            "  - weapon_positioning / fire_support_155mm / "
            "air_defense_artillery_vulcan_chaparral_stinger → exactly 5 rows.\n"
            "  - close_air_support_a10 / airborne_operations_c130_c17 / "
            "aviation_rotary / medical_evacuation_plus_above_criteria → exactly "
            "8 rows (Ceiling, Visibility, Surface Wind, Wind 3000 ft AGL, Cloud "
            "Cover, Precipitation, Icing, Atmospheric Pressure). Emitting 3 of 8 "
            "is BROKEN.\n"
            "  - gray_eagle / shadow_unmanned_aircraft_system / "
            "manned_isr_rc12_mc12 → exactly 6 rows.\n"
            "  - visual_sensors → exactly 4 rows. infrared_sensors → exactly 5 "
            "rows. engineer_operations → exactly 4 rows. "
            "nuclear_biological_chemical → exactly 4 rows. "
            "battlefield_obscuration → exactly 4 rows.\n"
            "Count the parameters specified for the operation, then emit that "
            "many rows. If a forecast value is missing for a parameter, emit the "
            "row anyway with current_forecast_value='unavailable' — do not drop "
            "the row."
        ),
        PROVENANCE_HARD_RULE,
    ],
    "data_sources": [
        "Weather + astronomical for the window: get_environmental(date=<window_end_dtg>)",
        "OPORD AOI: get_project_doc(tag='OPORD')",
    ],
    "post_emit_summary": "artifact_id, operations covered, worst-impact parameter identified",
}
