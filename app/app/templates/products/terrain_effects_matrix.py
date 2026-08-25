"""TEM — Terrain Effects Matrix — ATP 2-01.3 Table 4-5 (OAKOC).

See templates/__init__.py for the assembled PRODUCT_SCHEMAS map.
"""
from ..common import (
    HEADER_AO,
    HEADER_CLASSIFICATION,
    HEADER_DTG,
    HEADER_PHASE,
    HEADER_PREPARED_BY,
    OAKOC_ENUM,
    OAKOC_LABELS,
    PROVENANCE_DIRECTIVE,
    PROVENANCE_HARD_RULE,
)

SPEC = {
    "name": "TEM",
    "output_format": "docx",
    "shape": "row",
    "doctrine": {
        "purpose": (
            "Step-2 IPOE work aid describing terrain effects on friendly + threat operations "
            "per OAKOC. Built using the MCOO as a guide. Mandatory pair with MCOO. "
            "2-column × 5-OAKOC-row narrative.\n\n"
            "PER-ROW PROVENANCE (MANDATORY): each OAKOC row's `_provenance.narrative` MUST "
            "cite the source terrain feature(s) by `get_terrain.<layer>.<feature_name_or_id>`. "
            "Examples:\n"
            "  - mobility classification row → "
            "`_provenance.narrative = ['get_terrain.mobility_classifications.northern_open_ground', "
            "'get_terrain.mobility_classifications.pikasilla_forest_belt']`\n"
            "  - key terrain row → "
            "`_provenance.narrative = ['get_terrain.key_terrain.Hill_220', "
            "'get_terrain.key_terrain.Voru_river_crossing_site']`\n"
            "  - obstacles row → "
            "`_provenance.narrative = ['get_terrain.obstacles.Voru_river_wetland', "
            "'get_terrain.obstacles.Donovian_minefield_grid_HAWK']`\n"
            "Where geo-agent overlays were returned (look for "
            "`geo_agent_<layer>` siblings in get_terrain output), prefer those over the "
            "scenario fallback and cite them as `get_terrain.geo_agent_<layer>.<id_or_name>`. "
            "Weather-dependent claims (visibility, icing, soil saturation, illumination) "
            "ADDITIONALLY cite the WEM artifact_id."
            + PROVENANCE_DIRECTIVE
        ),
        "ordering_rule": "Rows in fixed OAKOC order: observation/fields-of-fire, avenues-of-approach, key-terrain, obstacles, cover-and-concealment.",
    },
    "row_source": "factor_list",
    "row_factors": OAKOC_ENUM,
    # Per ATP 2-01.3, MCOO INCORPORATES TEM as one of its input layers — so
    # the flow is TEM → MCOO, NEVER MCOO → TEM. Removing MCOO from this list
    # eliminated a real schema-level cycle. WEM legitimately feeds TEM (weather
    # modifies OAKOC trafficability and observation).
    "consumes_artifacts": ["WEM"],
    "published_section_title": "OAKOC Effects",
    "header_fields": {
        "phase":              HEADER_PHASE,
        "dtg":                HEADER_DTG,
        "area_of_operations": HEADER_AO,
        "force_type":         {"source": "session", "view": "header", "required": True,
                                "render_label": "Force Type",
                                "guidance": "mech / armor / dismount / aviation / mixed (mobility classes are force-relative)"},
        "weather_assumption_wem_ref": {"source": "ref:WEM", "view": "header",
                                        "required": False,
                                        "render_label": "WEM Reference",
                                        "depends_on": ["artifact.WEM"],
                                        "guidance": "WEM artifact_id supplying weather state"},
        "mcoo_artifact_id":   {"source": "ref:MCOO", "view": "internal",
                                "required": False,
                                "render_label": "MCOO Reference",
                                "guidance": "MCOO artifact_id used as base"},
        "prepared_by":     HEADER_PREPARED_BY,
        "classification":     HEADER_CLASSIFICATION,
    },
    "row_fields": {
        "factor": {"source": "analytical", "view": "published", "required": True,
                    "enum": OAKOC_ENUM,
                    "enum_labels": OAKOC_LABELS,
                    "render_label": "OAKOC Factor",
                    "guidance": "fixed: observation_and_fields_of_fire | avenues_of_approach | key_terrain | obstacles | cover_and_concealment"},
        "narrative": {"source": "analytical", "view": "published", "required": True,
                        "provenance_required": True,
                        "render_label": "Terrain Effects",
                        "depends_on": ["cougaar.get_terrain", "artifact.WEM"],
                        "guidance": (
                            "Bulleted narrative cell describing the OAKOC factor's effect on friendly + threat "
                            "operations. Mix measurements (km/m/ft), named features (MGRS where possible), and "
                            "operational judgment. NO friendly/threat split — both collapse into one narrative "
                            "column. Per-factor content expected:\n"
                            "  - observation_and_fields_of_fire: visibility ranges per terrain class; "
                            "intervisibility lines + their distances; air observation limits; small-arms / "
                            "direct-fire ranges; likely engagement areas (examples: '3 to 5 km observation', "
                            "'10 km between intervisibility lines', '300 to 500 m direct fire small arms').\n"
                            "  - avenues_of_approach: primary + secondary AAs by name/axis, supportable echelon "
                            "size, mobility-corridor characterization (severely_restricted / restricted / "
                            "unrestricted), recommended AA + why (example: 'AA2 is the recommended AA as it "
                            "enables placement of organic weapon systems...').\n"
                            "  - key_terrain: named features whose seizure/control gives marked advantage to "
                            "either side; tie each to which AA/EA it dominates (examples: 'Airfield used as "
                            "resupply', 'Dam controls water flow on the river and is the primary objective of "
                            "the threat').\n"
                            "  - obstacles: natural + reinforcing obstacles; tactical-obstacle effect "
                            "(disrupt | turn | fix | block); dimensions (depth/width/length); whether "
                            "bypassable (example: 'Restrictive runoffs … average depth of 5 to 10 feet … width "
                            "of 20 feet').\n"
                            "  - cover_and_concealment: cover for direct-fire systems (intervisibility lines, "
                            "sunken ground, walls); concealment from visual + thermal observation; "
                            "degraded-by-weather notes citing the WEM artifact (example: 'Cover by direct fire "
                            "systems is provided by intervisibility lines. Concealment is limited by the open "
                            "terrain and sparse vegetation').\n"
                            "Mobility classifications and corridor widths are FORCE-RELATIVE — use "
                            "header.force_type to scope (mech/armor vs dismount).\n\n"
                            "PROVENANCE REQUIREMENT (per row, mandatory): set "
                            "`_provenance.narrative` to a list of `get_terrain.<layer>.<feature>` references — "
                            "one per named feature mentioned in the cell. Layer is one of "
                            "{observation_and_fields_of_fire, avenues_of_approach, key_terrain, obstacles, "
                            "mobility_classifications, cover_and_concealment}. Feature is the named "
                            "feature (region/name/id) from the get_terrain payload. When geo-agent overlays "
                            "are present for that layer (look for `geo_agent_<layer>` keys returned by "
                            "get_terrain), prefer `get_terrain.geo_agent_<layer>.<id_or_name>` over the "
                            "scenario fallback. ADDITIONALLY cite the WEM artifact_id for weather-dependent "
                            "claims."
                        )},
    },
    "subtleties": [
        "TEM is built USING THE MCOO as a guide. The MCOO supplies the named features, AAs, mobility classifications, and obstacles that TEM narrates as effects. Cite mcoo_artifact_id in header.",
        "Cells embed measurements + named features + operational recommendations + cross-links to MCOO mobility-classification vocabulary (severely_restricted / restricted / unrestricted).",
        "No friendly-vs-threat split — both collapse into a single narrative column per row. Do NOT add separate friendly/threat sub-columns.",
        "Force-relative — mobility-corridor widths and severely_restricted/restricted/unrestricted classifications change with force type (mech/armor vs dismounted vs aviation). header.force_type scopes the analysis.",
        "Tactical obstacle effects use the controlled vocabulary disrupt / turn / fix / block — use these exact terms in the obstacles cell.",
        "Mobility corridor widths: when characterizing an AA, name the supportable echelon (e.g., 3 km for an armored brigade corridor).",
        "Weather-state assumption is inherited from the WEM artifact. Weather-dependent observations (e.g., 'icing degrades aviation observation') must cite weather_assumption_wem_ref.",
        "TEM and WEM are SEPARATE Step-2 products that both feed the MCOO and Step-4 Threat COA Statement/SITEMP — TEM does not subsume WEM and must not re-do weather analysis.",
        "Every cell's measurements, named features, and operational judgments must carry source citation in _provenance (GEOINT layer, get_terrain output, map sheet, MCOO row).",
    ],
    "hard_rules": [
        "Never invent terrain features — every named feature MUST appear in the get_terrain response (scenario.TERRAIN keys OR geo_agent_<layer> overlays).",
        "All five OAKOC rows required, in fixed order: observation_and_fields_of_fire → avenues_of_approach → key_terrain → obstacles → cover_and_concealment.",
        "Cite mcoo_artifact_id in header (TEM is built FROM MCOO). If MCOO not yet available, set mcoo_artifact_id='pending' and note the assumption in _provenance — do not silently omit.",
        "Cite weather_assumption_wem_ref when any cell narrative depends on weather state (visibility, icing, soil saturation, illumination). If no weather dependency in this TEM, the field may be blank.",
        "Use the controlled vocabulary: mobility = severely_restricted | restricted | unrestricted; tactical obstacle effect = disrupt | turn | fix | block.",
        "Each narrative cell must include at least one measurement (km/m/ft/MGRS) AND at least one named feature — pure prose without geographic anchoring is invalid output.",
        "Each row's `_provenance.narrative` MUST be a list of `get_terrain.<layer>.<feature>` references (one per named feature in the cell). When `get_terrain` returns `geo_agent_<layer>` overlays, prefer those over the scenario fallback. A row whose narrative names a feature without a corresponding `get_terrain` provenance entry is invalid output.",
        PROVENANCE_HARD_RULE,
    ],
    "data_sources": [
        "Terrain data: get_terrain() (returns all OAKOC layers)",
        "MCOO upstream: list_project_docs(filter={type:'MCOO'}) then get_project_doc(tag=<latest>)",
        "WEM weather assumption: list_project_docs(filter={type:'WEM'}) then get_project_doc(tag=<latest>)",
        "Per-layer if needed: get_terrain(layer='observation_and_fields_of_fire'), etc.",
    ],
    "post_emit_summary": "artifact_id, OAKOC summary, key factor with greatest operational impact",
}
