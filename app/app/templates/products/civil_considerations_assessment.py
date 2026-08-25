"""CCA — Civil Considerations Assessment — ATP 2-01.3 §4-96 ASCOPE×PMESII-PT.

See templates/__init__.py for the assembled PRODUCT_SCHEMAS map.
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
    "name": "CCA",
    "output_format": "pptx",
    "shape": "matrix",
    # Landscape (pptx) — the ASCOPE × PMESII-PT matrix is 6-col × 8-row of
    # bullet-heavy civil-entity cells. Portrait gives ~0.8" per data column
    # which truncates entity names like "Tartu municipality (C-0101)";
    # landscape gives ~1.4" which fits. output_format implies orientation:
    # pptx → landscape 16:9 slide.
    "doctrine": {
        "purpose": (
            "Step-2 IPOE 3-part product family (data file + overlays + assessment) "
            "describing how civil considerations affect friendly + threat operations. "
            "ASCOPE × PMESII-PT 8-row × 6-col matrix (8 PMESII-PT lenses × 6 ASCOPE "
            "characteristics). Physical Environment + Time are the two lenses that "
            "extend PMESII to PMESII-PT; the unit defines what those rows contain."
            + PROVENANCE_DIRECTIVE
        ),
        "ordering_rule": "Matrix axes fixed: rows = PMESII-PT (8 — political, military, economic, social, information, infrastructure, physical_environment, time); columns = ASCOPE (6 — areas, structures, capabilities, organizations, people, events).",
    },
    # ASCOPE × PMESII-PT. Rollups (HUMINTSUM / OSIR) carry the cumulative
    # civic baseline; atomic HUMINT IIRs + Tactical OSINT Tippers carry
    # current-cycle civic events the rollup doesn't have yet.
    "consumes_artifacts": ["MCOO",
                            "HUMINTSUM", "OSIR"],
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
        "mcoo_artifact_id":   {"source": "ref:MCOO", "view": "internal",
                                "required": False,
                                "render_label": "MCOO Reference",
                                "depends_on": ["artifact.MCOO"],
                                "guidance": "for urban infrastructure overlap"},
        "classification":     HEADER_CLASSIFICATION,
    },
    "paragraph_fields": [
        {"name": "ascope_pmesii_pt_matrix",
         "view": "published",
         "shape": "grid_table",
         "render_label": "ASCOPE × PMESII-PT Matrix",
         "required": True,
         "depends_on": [
             "cougaar.get_civil",
         ],
         "guidance": (
             "8×6 grid. Rows = PMESII-PT operational variables (political, military, "
             "economic, social, information, infrastructure, physical_environment, "
             "time). Cols = ASCOPE characteristics (areas, structures, capabilities, "
             "organizations, people, events). "
             "Each cell = bullet list of concrete civil-data entries observed in the "
             "AO, NOT generic examples. Patterns to use as a guide:\n"
             "  - POLITICAL × Areas: enclaves; municipalities; provinces; districts; "
             "political-district / voting / party-affiliation areas; shadow-government "
             "influence areas.\n"
             "  - POLITICAL × Structures: courts; mobile courts; government centers; "
             "provincial/district centers; meeting halls; polling sites; police stations; "
             "prisons.\n"
             "  - POLITICAL × Capabilities: public-admin / judicial / law-enforcement "
             "capacities; civil + criminal codes; corrections.\n"
             "  - POLITICAL × Organizations: NGOs; cooperatives; guilds; labor unions; "
             "volunteer groups.\n"
             "  - POLITICAL × People: governors; legislative members; judges; prosecutors; "
             "law-enforcement officers; corrections officers; key elders.\n"
             "  - POLITICAL × Events: elections; council meetings; significant trials; "
             "treaties; speeches.\n"
             "  - MILITARY × Areas: host-nation force AO; areas of influence/interest; "
             "safe havens / sanctuaries; insurgent base areas.\n"
             "  - MILITARY × Structures: bases; headquarters; police stations; multinational "
             "facilities.\n"
             "  - MILITARY × Capabilities: doctrine; training; materiel; leadership; "
             "personnel; resource constraints; local security forces; QRFs.\n"
             "  - MILITARY × Organizations: host-nation forces; insurgent groups; "
             "paramilitary orgs; combat networks; historical militancy networks.\n"
             "  - MILITARY × People: key leaders; multinational/insurgent/paramilitary "
             "personnel.\n"
             "  - MILITARY × Events: kinetic events; combat; loss of leadership; unit "
             "reliefs.\n"
             "  - ECONOMIC × Areas: commercial / industrial / mining / market / trade-route / "
             "smuggling areas; black-market areas.\n"
             "  - ECONOMIC × Structures: banks; industrial plants; manufacturing; "
             "warehouses; silos; farms/ranches.\n"
             "  - ECONOMIC × Capabilities: monetary policy; banking; fuel + food + "
             "imports/exports; raw materials.\n"
             "  - ECONOMIC × Organizations: NGOs / cooperatives / guilds / labor unions / "
             "major illicit industries.\n"
             "  - ECONOMIC × People: bankers; employers; landholders; merchants; money "
             "lenders; smugglers; gang members.\n"
             "  - ECONOMIC × Events: drought/harvest cycles; livestock-market cycles; "
             "job-line gathering; paydays; business openings.\n"
             "  - SOCIAL × Areas: ethnic/religious/tribal enclaves; neighborhoods; school "
             "districts; cemeteries; social-gathering / market places.\n"
             "  - SOCIAL × Structures: refugee camps; jails; historical buildings; "
             "libraries; religious buildings; schools/universities; stadiums.\n"
             "  - SOCIAL × Capabilities: education; medical (traditional/modern); "
             "academic; patriotic / religious / police capabilities.\n"
             "  - SOCIAL × Organizations: community councils; school councils; tribal/"
             "clan structures; police orgs.\n"
             "  - SOCIAL × People: community leaders; clan/family elders; key criminals "
             "and entertainers; chiefs/elders.\n"
             "  - SOCIAL × Events: civil disturbances; national/religious holidays; clan "
             "events; food lines; weddings; funerals.\n"
             "  - INFORMATION × Areas: media broadcast coverage; word-of-mouth gathering "
             "points; graffiti / poster areas.\n"
             "  - INFORMATION × Structures: communications lines/towers; internet cafes; "
             "newspapers; postal/print/radio/TV stations.\n"
             "  - INFORMATION × Capabilities: local communications networks; intelligence "
             "services; printed material; propaganda capability; literacy rate.\n"
             "  - INFORMATION × Organizations: media groups; religious-info groups; "
             "insurgent inform-and-influence activity; government information arms.\n"
             "  - INFORMATION × People: decision-makers; media personalities; religious + "
             "community leaders; heads of families.\n"
             "  - INFORMATION × Events: censorship events; religious-observance days; "
             "publishing dates; inform-and-influence campaigns.\n"
             "  - INFRASTRUCTURE × Areas: residential/rural/urban; road systems; power "
             "grids; irrigation networks; water tables.\n"
             "  - INFRASTRUCTURE × Structures: shelters; energy distribution; clean-water; "
             "communications; medical (hospitals/clinics); transportation; sanitation; "
             "waste treatment.\n"
             "  - INFRASTRUCTURE × Capabilities: construction; medical service; "
             "firefighting; road / well-digging / maintenance.\n"
             "  - INFRASTRUCTURE × Organizations: builders; road contractors; "
             "construction companies; development councils.\n"
             "  - INFRASTRUCTURE × People: civil engineers; utility workers; medical staff.\n"
             "  - INFRASTRUCTURE × Events: scheduled maintenance; construction; natural / "
             "man-made disasters; well-digging completion.\n"
             "  - PHYSICAL_ENVIRONMENT × all six: AO-specific terrain, hydrology, "
             "climate, and seasonal phenomena affecting civil life (8-lens extension; "
             "the unit defines contents).\n"
             "  - TIME × all six: timing-sensitive civil patterns — diurnal market cycles, "
             "harvest seasons, election windows, ramadan / religious-observance windows, "
             "school calendars (8-lens extension).\n"
             "Each cell entry = real entity observed in the civil-considerations "
             "payload, NOT a paraphrase of generic boilerplate. "
             "IDENTIFIER DISCIPLINE: every civil-entity ID referenced in a matrix cell "
             "(e.g. 'C-001', 'C-101', 'PT-...') MUST come VERBATIM from the upstream "
             "civil-considerations payload supplied in this prompt. NEVER invent "
             "civil-entity IDs of the form C-001 / C-002 / C-### that are not "
             "literally present in the civil payload — that is a fabrication error "
             "and the runner will reject the artifact. If no qualifying entity is "
             "in the payload for a given cell, describe the entity in free text "
             "without a fake C-### id, or leave the cell empty (an empty cell is "
             "valid output). "
             "Every cell entry's source citation lives in _provenance citing the "
             "specific civil vertex — e.g. <layer>.<kbID> or the civil vertex "
             "kbID + objectType when the payload surfaces it (cite civil layer / "
             "civil-affairs SITREP / sociocultural DB / OPORD)."
         )},
        {"name": "displaced_persons_overlay",
         "view": "published",
         "shape": "dict",
         "render_label": "Displaced Persons Overlay",
         "required": False,
         "depends_on": [
             "cougaar.get_civil",
         ],
         "guidance": (
             "Civil-considerations overlay highlighting refugee / IDP flows that "
             "could affect operations. Populate when the AO has active or anticipated "
             "displacement; leave empty otherwise. Identifying flows permits "
             "prepositioning of capabilities, materials, and humanitarian assistance. "
             "Pull displacement entities directly from the civil-considerations "
             "payload. Cite the source vertex(s) in _provenance "
             "(civil <layer>.<kbID> or civil-affairs SITREP)."
         ),
         "subfields": ["camps", "movement_vectors", "expected_volume"],
         "subfield_guidance": {
             "camps": "Locations of refugee / IDP camps and temporary settlements (MGRS or named place; cite civil-affairs source).",
             "movement_vectors": "Anticipated displacement routes — origin → destination axes, road networks used, choke points.",
             "expected_volume": "Order-of-magnitude estimate (hundreds / thousands / tens-of-thousands) with the basis (e.g., 'population of affected districts ~50k per UN OCHA report').",
         }},
        {"name": "protected_targets_list",
         "view": "published",
         "shape": "list_of_dicts",
         "render_label": "Protected Targets List",
         "required": False,
         "depends_on": [
             "cougaar.get_civil",
         ],
         "guidance": (
             "G-9/S-9 (civil affairs) + chief of fires identify protected entities so "
             "they can be DECONFLICTED out of the HPTL during targeting. Examples "
             "include hospitals, religious sites, UNESCO World Heritage sites, ICRC "
             "facilities, water and food infrastructure. Cultural sites are generally "
             "protected by international law. Pull protected-entity records from the "
             "civil-considerations payload's relevant layers (structures / "
             "organizations / people). "
             "IDENTIFIER DISCIPLINE: PT-### ids may be locally minted by the writer "
             "(one per row, monotonic), but the underlying entity reference MUST be "
             "a real entity from the civil-considerations payload. Cite the source "
             "vertex in _provenance (civil <layer>.<kbID>). Leave empty if the AO "
             "genuinely has no such entities; do not fabricate."
         ),
         "item_subfields": [
             {"name": "id", "render_label": "ID",
              "guidance": "Stable identifier (e.g., 'PT-001')."},
             {"name": "name", "render_label": "Name",
              "guidance": "Specific named entity — 'Tartu University Hospital', 'St. Mary's Cathedral', 'Voru UNESCO archeological site'."},
             {"name": "type", "render_label": "Type",
              "enum": ["hospital", "religious-site", "cultural-heritage", "school",
                        "water-treatment", "NGO-facility", "ICRC-facility",
                        "UN-facility"],
              "enum_labels": {
                  "hospital":          "Hospital",
                  "religious-site":    "Religious Site",
                  "cultural-heritage": "Cultural Heritage",
                  "school":            "School",
                  "water-treatment":   "Water Treatment",
                  "NGO-facility":      "NGO Facility",
                  "ICRC-facility":     "ICRC Facility",
                  "UN-facility":       "UN Facility",
              },
              "guidance": "Category — hospital / religious-site / cultural-heritage / school / water-treatment / NGO-facility / ICRC-facility / UN-facility."},
             {"name": "protection_basis", "render_label": "Protection Basis",
              "enum": ["IHL_LOAC", "UNESCO", "religious", "ICRC"],
              "guidance": "IHL_LOAC for general LOAC-protected categories (medical / cultural / religious); UNESCO for designated World Heritage; religious for site protected by religious significance only; ICRC for International Committee of the Red Cross facilities."},
             {"name": "mgrs", "render_label": "Location",
              "guidance": "MGRS to 6- or 8-digit precision."}]},
        {"name": "narrative_assessment",
         "view": "published",
         "shape": "dict",
         "render_label": "Civil Considerations Assessment (Narrative)",
         "required": True,
         "provenance_required": True,
         "depends_on": [
             "cougaar.get_civil",
             "cougaar.list_project_docs",
             "artifact.HUMINTSUM",
             "artifact.OSIR",
         ],
         "guidance": (
             "Civil-considerations ASSESSMENT proper. Draws on the ASCOPE × PMESII-PT "
             "matrix + overlays to synthesize: (1) social and political pattern map, "
             "(2) formal + informal leadership, (3) key societal friction points, "
             "(4) effects of friendly operations on the population (and expected "
             "population reactions), and (5) influence strategies (non-lethal "
             "engagement). For stability tasks, weight civil considerations heavily "
             "— a greater emphasis may need to be placed on civil considerations "
             "than on the enemy during stability tasks. Draw on the "
             "civil-considerations payload for the civil baseline + the project "
             "doc catalog (HUMINTSUM / OSIR) for cumulative civic context. "
             "When naming leaders, friction points, or specific entities, cite the "
             "source vertex (civil <layer>.<kbID>) in _provenance — narrative "
             "claims about named real-world entities require a source vertex.\n\n"
             "EMIT FORMAT — CRITICAL: Emit a dict with EXACTLY these five keys, "
             "EACH ON ITS OWN LINE in the JSON object, EACH carrying one paragraph:\n"
             "  - social_political_pattern_map\n"
             "  - formal_informal_leadership\n"
             "  - key_societal_friction_points\n"
             "  - friendly_operations_effects_on_population\n"
             "  - influence_strategies\n"
             "All five subfields are REQUIRED. Do NOT concatenate two subfield "
             "values into a single oversized key (e.g., never emit a key like "
             "`\"...prose-text-here,influence_strategies\"` which fuses the "
             "formal_informal_leadership value with the influence_strategies key "
             "name — that is a known writer-side JSON-emission bug). Keep each "
             "key on its own line; close every value string with `\"` BEFORE "
             "starting the next key."
         ),
         "subfields": ["social_political_pattern_map", "formal_informal_leadership",
                        "key_societal_friction_points", "friendly_operations_effects_on_population",
                        "influence_strategies"],
         "subfield_guidance": {
             "social_political_pattern_map": "Mapping of social + political patterns across the AO — ethnic/sectarian distribution, party-affiliation areas, shadow-government zones, tribal/clan dominance. Tie to the matrix POLITICAL + SOCIAL rows.",
             "formal_informal_leadership": "Both formal (elected officials, judges, military commanders) AND informal (elders, religious leaders, criminal-network leaders) authority figures — name them where possible.",
             "key_societal_friction_points": "Inter-ethnic / inter-sectarian / economic / generational fault lines and their flashpoints (markets, holidays, anniversaries).",
             "friendly_operations_effects_on_population": "Anticipated population reactions to planned friendly operations — both negative (displacement, infrastructure damage, civilian casualties risk) and positive (security, economic stimulus, governance restoration).",
             "influence_strategies": "Non-lethal engagement opportunities — key communicators, MISO themes, civil-affairs project priorities, partnered local leadership.",
         }}],
    "subtleties": [
        "CCA is a 3-part product family: (1) civil considerations DATA FILE organizing raw entities by ASCOPE; (2) OVERLAYS that depict the data file graphically (incl. displaced-persons overlay); (3) ASSESSMENT narrative synthesizing patterns and influence opportunities. This schema renders the matrix (which surfaces the data file), the displaced-persons overlay, the protected-targets list, and the narrative_assessment.",
        "ASCOPE × PMESII crosswalk is the analytical frame (6-row variant). PMESII-PT extension to 8 lenses adds Physical Environment + Time; the unit defines what goes in those two extension rows.",
        "G-9/S-9 leads CCA; G-2/S-2 collaborates — especially on threat-enabling civil networks (insurgent inform-and-influence, criminal organizations, shadow-government influence areas).",
        "Stability tasks weight civil considerations heavily — a greater emphasis may need to be placed on civil considerations than on the enemy during stability tasks.",
        "Protected-targets sub-list feeds HPTL deconfliction — G-9/S-9 + chief of fires identify protected; cultural sites are protected by international law.",
        "Displaced-persons overlay is CONDITIONAL — populate only when AO has active or anticipated refugee/IDP flows.",
        "Narrative_assessment is the synthesis layer covering: (a) social + political pattern mapping, (b) formal + informal leadership, (c) key societal friction points, (d) effects of friendly operations on the population, (e) influence strategies. Matrix supports it; assessment is the operationally useful output.",
        "Every matrix cell entry + every protected-target row + every narrative_assessment claim cites source in _provenance (get_civil layer / civil-affairs SITREP / sociocultural DB / OPORD)."],
    "hard_rules": [
        "Never invent civil entities. Every matrix cell entry, every protected-target row, every overlay element cites a real source in _provenance (get_civil layer / civil-affairs SITREP / sociocultural DB / OPORD).",
        "Matrix axes are fixed and complete: 8 PMESII-PT rows × 6 ASCOPE columns = 48 cells. A cell may be empty if the AO has nothing in that intersection (e.g., MILITARY × Events may be empty during pure stability) — but every row and every column must appear in the rendered matrix.",
        "Matrix cells contain AO-specific real entries, NOT paraphrased generic boilerplate. 'enclaves; municipalities; provinces' is example text — replace with named municipalities/enclaves observed in this AO.",
        "Protected entities MUST set protection_basis (IHL_LOAC / UNESCO / religious / ICRC). A protected target without protection_basis is invalid output.",
        "Narrative_assessment must cover ALL FIVE investigation areas (social_political_pattern_map, formal_informal_leadership, key_societal_friction_points, friendly_operations_effects_on_population, influence_strategies). Emitting only 1–2 subfields is incomplete.",
        "Every analytical record carries _provenance.",
        PROVENANCE_HARD_RULE],
    "data_sources": [
        "Civil baseline (now merges OPORD-derived ASCOPE/PMESII rows extracted from scenario_ingest with the static fixture): get_civil()",
        "Per ASCOPE category: get_civil(layer='areas'), get_civil(layer='structures'), etc.",
        "Operations overlay control measures + sustainment kinds (civil annotations cross-ref — AO / boundaries / NAIs / NOGO/SLOWGO + MTF/CCP/Class VIII sustainment proximity): get_spatial_overlay()",
        "OPORD context: get_project_doc(tag='OPORD')",
        "MCOO upstream urban content: list_project_docs(filter={type:'MCOO'})"],
    "post_emit_summary": "artifact_id, populated cell count, key civil leaders / protected targets identified",
}
