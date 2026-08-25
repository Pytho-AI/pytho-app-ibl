"""V Corps MITS 26 scenario fixture data — OPORD 26-001 (EASTERN FLANK LAND CAMPAIGN).

Substitute for app/app/utils/scenario.py used inside the V Corps parallel
docker-compose stack (bind-mounted over scenario.py at container start;
see docker-compose.vcorps.yml). Module SURFACE is identical to scenario.py
so every import site (`from utils import scenario`) gets V Corps data
unchanged when this file is the active one.

Scenario: V Corps (LTG Richardson) conducts area defense at Bemowo Piskie
Training Area (BPTA), NE Poland, against the OCCASUS 7th Combined Arms
Army (Maj. Gen. Churin) assault on OBJ JACKSON (34U EE 64392 54490 =
53.735N 21.976E). Threat axis is from the EAST through the Elk corridor
(Narew/Biebrza/Pisa river canalization), NOT from the north.

H-Hour: 11 0000L AUG 2026. Planning DTG: 170600L JUL 2026.

Friendly: 2CR (SOUTH Sector main effort) at OBJ JACKSON; 210th FA Brigade
(DIVARTY) at PAA 34U EE 58250 56900; V Corps CAB ISR; 21st TSC Fwd with
RSS Nodes X, Y, Z; Italian WGX Battle Group (OPCON 2CR) at IVO PL YELLOW;
ROC-T at ATK PSN VENUS (out of sector, deep). 1ABCT fixes CENTER; 1CD is
theater reserve at Grafenwohr; 173d ABCT in Finland under FINDEFCOM.

Threat: 7th CAA two-echelon assault — 72nd MRD (assault echelon, FAA
URAL-CENTRAL main effort + URAL-NORTH sub-axis) attacks west through the
Elk corridor against OBJ JACKSON; 71st MID (second echelon, FAA SOKOL)
exploits success; 75th Missile Brigade Iskander-M strikes DIVARTY PAAs +
RSS Nodes; 76th AD Brigade SAM suppresses V Corps aviation; 7th Combat
Helicopter Brigade attack + air assault; 750th SPF interdicts TRM corridor;
Cinereus EW Group jams from H-Hour.

Source materials (committed in scratch/ibl/joe-test/):
  - Exercise - DRAFT V Corps OPORD v2.docx (full FM 5-0 OPORD)
  - Joe's V Corps OPORD Map Prompt exchange (1).pdf (8 iteration rounds of
    pinned grids — every coordinate verified via mgrs lib, see
    scratch/ibl/joe-test/vcorps-compaction-log.md)
  - Joe's V corps map iteration map graphics.pdf (Google Earth screenshots)

All MGRS strings have been verified by mgrs.MGRS().toLatLon() against the
prompt-PDF's stated lat/lons. Discrepancies > 100 m are flagged in rationale.
"""

from __future__ import annotations
import datetime as _dt

# Scenario DTG anchor — OPORD publication 170600L JUL 2026, expressed as
# UTC for the epoch-ms field. Local CEST = UTC+02 in July, so 170600L =
# 170400Z. tool_get_oob compares this against any live cougaar snapshot;
# whichever is fresher wins. There is no live cougaar for the V Corps
# scenario, so this fixture is authoritative.
SCENARIO_MONTH_YEAR = "JUL26"
KG_OOB_AUTHORED_AT_MS = int(
    _dt.datetime(2026, 7, 17, 4, 0, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000
)

# Per-product map RENDER fallback bbox — encompasses BPTA + Elk corridor +
# FAA depth east + sufficient overscan for label/legend space. Order:
# [west, south, east, north] (lon/lat WGS84). Single source of truth.
#
# Composition of bounds:
#   west  21.5  — west of OBJ JACKSON (21.976) by ~0.5° (~33 km) — covers TAA
#                 BRAVO, Italian WGX, RSS rear, BPTA western edge
#   south 53.5  — south of BPTA SW corner (53.66) and Pisz town anchor (53.63)
#   east  23.0  — east of FAA SOKOL (22.72) — covers full 7th CAA assembly
#                 belt + Polish-Lithuanian border, with margin
#   north 54.1  — north of all FAAs (the URAL belt sits at ~53.92, SOKOL ~53.83)
AO_BBOX = [21.5, 53.5, 23.0, 54.1]


# ============================================================================
# KG — Knowledge Graph (units, targets, civil, reports, events, imagery)
# ============================================================================
# Same shape as scenario.py KG. unit_blue + unit_red are the OOB; everything
# else is empty for Phase 1 (no live SIGINT/HUMINT feed in the V Corps stack
# until Joe wires one).
KG = {
    # Threat actor — per V Corps OPORD v2 §1.a.(1), the threat is OCCASUS
    # Alliance, with 7th CAA as the assault formation at BPTA. Picked up by
    # intel_agent's session header resolver (`source: "session"` +
    # `f == "threat_actor"`) and overrides the schema default
    # "Donovian Armed Forces" hard-wired in threat_model.py /
    # threat_capability_statement.py. The IDX 26 scenario doesn't expose
    # this key (its threat IS Donovian) so the resolver falls through to
    # the schema default for IDX 26 cleanly.
    "threat_actor": "7th CAA (OCCASUS Alliance)",

    # -------------------------------------------------------------------------
    # RED — OCCASUS 7th CAA (assault echelon at BPTA + supporting elements)
    # -------------------------------------------------------------------------
    # Task org per OPORD §1.c.(1):
    #   7th CAA (Maj. Gen. Churin) — Forward Assembly Areas east of Poland:
    #     72nd MRD (Col. Voronov) — ASSAULT ECHELON, FAA URAL-CENTRAL + URAL-NORTH
    #       721 MRB, 722 MRB, 723 MRB (motorized rifle brigades)
    #       72nd Tank Battalion (organic)
    #       72nd FA Regiment (2S19 152mm) — direct support
    #       72nd ADA Battalion (2S6 + Pantsir)
    #     71st MID (Col. Kuzmin) — 2ND ECHELON exploitation, FAA SOKOL
    #       711 MIB, 712 MIB, 713 MIB
    #     74th SP Artillery Brigade — GS counterfire (2S19, BM-27)
    #     75th Missile Brigade — Iskander-M SRBM TELs, Grisian Enclave
    #     76th AD Brigade — Medium-range SAM (SA-11/SA-12)
    #     7th Combat Helicopter Brigade — Mi-35/Mi-24/Ka-52
    #     7th Engineer Brigade — Narew/Biebrza bridging
    #     7th Logistics Brigade — 3-day combat load
    #     750th Separate SPF Brigade — TRM corridor interdiction
    #
    # Composition_rollup uses EXACT equipment nomenclatures from
    # equipment_doctrine.EQ_ENGAGEMENT_RANGE_M so the SITEMP range-fan
    # extractor (_extract_writer_range_fans) emits one fan per piece of
    # range-fan-eligible organic equipment per templated unit.
    "unit_red": [
        # ===== 7th CAA HQ =====
        {
            "id": "U-OCC-7CAA-HQ", "name": "7TH CAA HQ", "echelon": "ARMY",
            "class": "army_hq", "class_template": "OPFOR CAA",
            "loc": "Grisian Enclave (Suwalki Gap, east of Polish-Lithuanian border)",
            "mgrs": "34UFE3000080000",  # ~54.07N 23.00E — east of FAA SOKOL
            "strength": "full",
            "activity": "directing 7th CAA two-echelon assault toward OBJ JACKSON",
            "last_obs": "171200Z", "parent": None,
            "sidc": "SNGPUCII---KXOC", "uic": "7CAA-HQ",
            "composition_rollup": {
                "URAL 375D C3 VAN": 8, "GAZ 66 SIGNAL VAN": 6,
                "GAZ 66 SATCOM VEH": 4, "GAZ 66 RADIO RELAY": 4,
                "GAZ 2330 TIGR": 12, "TRK CGO URAL 375 WHL 4000KG": 20,
            },
        },
        # ===== 72nd MRD (assault echelon) =====
        {
            "id": "U-OCC-72MRD-HQ", "name": "72ND MRD HQ", "echelon": "DIV",
            "class": "div_hq", "class_template": "OPFOR MTZD INF DIV",
            "loc": "vic FAA URAL-CENTRAL",
            "mgrs": "34UFE0203564609",  # FAA URAL-CENTRAL: 53.82N 22.55E
            "strength": "full",
            "activity": "directing 72nd MRD assault through Elk corridor toward OBJ JACKSON",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCII---IXOC", "uic": "72MRD-HQ",
            "composition_rollup": {
                "URAL 375D C3 VAN": 4, "GAZ 66 SIGNAL VAN": 4,
                "MTLB 1V14 TRKD CP": 3, "MTLB 1V13 FDC": 2,
                "GAZ 2330 TIGR": 8,
            },
        },
        {
            "id": "U-OCC-721-MRB", "name": "721 MRB",
            "echelon": "BDE", "class": "mech_inf",
            "class_template": "OPFOR MTZD RIFLE BDE",
            "loc": "FAA URAL-CENTRAL — main effort, Elk-corridor axis",
            "mgrs": "34UFE0150064500",  # ~1km W of FAA URAL-CENTRAL center
            "strength": "full",
            "activity": "main effort assault echelon — attack west via DK16 toward OBJ JACKSON",
            "last_obs": "171200Z", "parent": "U-OCC-72MRD-HQ",
            "sidc": "SNGPUCIM---HXOC", "uic": "721-MRB",
            # 3 BTGs each w/ ~30 BMP-2, ~10 T-90, ~6 ATGM jeeps;
            # plus brigade FA bn (18× 2S19), MLRS battery (6× BM-21),
            # ADA battery (4× 2S6), recon company (6× BRM-3K)
            "composition_rollup": {
                "BMP 2 IFV": 90, "T 90A": 30, "T 90A ATGM": 6,
                "TIGR GAZ 2330 WITH AT14 ATGM": 18,
                "HOW SP 2S19 TRKD 152MM": 18,
                "BM-21 GRAD": 6,
                "LCHR 2S6 30MM SA 19 TRKD": 4,
                "BRM 3K RECON VEHICLE": 6,
                "URAL 375D C3 VAN": 3, "GAZ 66 SIGNAL VAN": 3,
                "MTLB 1V14 TRKD CP": 4,
            },
        },
        {
            "id": "U-OCC-722-MRB", "name": "722 MRB",
            "echelon": "BDE", "class": "mech_inf",
            "class_template": "OPFOR MTZD RIFLE BDE",
            "loc": "FAA URAL-NORTH — northern sub-axis",
            "mgrs": "34UEE9982275691",  # FAA URAL-NORTH: 53.92N 22.52E
            "strength": "full",
            "activity": "northern sub-axis — supports 721 MRB main effort by isolating OBJ JACKSON from north",
            "last_obs": "171200Z", "parent": "U-OCC-72MRD-HQ",
            "sidc": "SNGPUCIM---HXOC", "uic": "722-MRB",
            "composition_rollup": {
                "BMP 2 IFV": 90, "T 90A": 30, "T 90A ATGM": 6,
                "BRDM 2 HOT 3": 18,
                "HOW SP 2S19 TRKD 152MM": 18,
                "BM-21 GRAD": 6,
                "LCHR 2S6 30MM SA 19 TRKD": 4,
                "BRM 3K RECON VEHICLE": 6,
            },
        },
        {
            "id": "U-OCC-723-MRB", "name": "723 MRB",
            "echelon": "BDE", "class": "mech_inf",
            "class_template": "OPFOR MTZD RIFLE BDE",
            "loc": "FAA URAL-CENTRAL — southern flank protection",
            "mgrs": "34UFE0250064000",  # near FAA URAL-CENTRAL south
            "strength": "full",
            "activity": "southern flank protection — Drygały/Nitki rail-corridor approach",
            "last_obs": "171200Z", "parent": "U-OCC-72MRD-HQ",
            "sidc": "SNGPUCIM---HXOC", "uic": "723-MRB",
            "composition_rollup": {
                "BMP 2 IFV": 90, "T 90A": 30, "T 90A ATGM": 6,
                "ATGM BMP 3 TRKD AT 15": 12,
                "HOW SP 2S19 TRKD 152MM": 18,
                "BM-21 GRAD": 6,
                "ADA,MIXED,TRACKED,PANTSIRS1": 4,
                "BRM 3K RECON VEHICLE": 6,
            },
        },
        {
            "id": "U-OCC-72FA-REGT", "name": "72ND FA REGIMENT",
            "echelon": "BDE", "class": "arty",
            "class_template": "OPFOR DIV ARTY (2S19)",
            "loc": "behind URAL-CENTRAL, mass fires support to 72nd MRD",
            "mgrs": "34UFE0350066000",  # ~2km N of URAL-CENTRAL
            "strength": "full",
            "activity": "general-support fires for 72nd MRD assault; mass on EAs at Drygały / Nitki / Klusy",
            "last_obs": "171200Z", "parent": "U-OCC-72MRD-HQ",
            "sidc": "SNGPUCFHE--HXOC", "uic": "72FA-REGT",
            "composition_rollup": {
                "HOW SP 2S19 TRKD 152MM": 36,
                "120MM SP MORTAR 2S9": 18,
                "MTLB 1V13 FDC": 4, "MTLB 1V16 TRKD BN FDC": 2,
                "URAL 375D C3 VAN": 4,
            },
        },
        {
            "id": "U-OCC-72ADA-BN", "name": "72ND ADA BN",
            "echelon": "BN", "class": "ada",
            "class_template": "OPFOR DIV ADA (PANTSIR/SA-19)",
            "loc": "umbrella over 72nd MRD assault depth",
            "mgrs": "34UFE0200066500",  # ~2.5km N of URAL-CENTRAL
            "strength": "full",
            "activity": "air defense umbrella over 72nd MRD; suppresses V Corps CAB aviation",
            "last_obs": "171200Z", "parent": "U-OCC-72MRD-HQ",
            "sidc": "SNGPUCDS---HXOC", "uic": "72ADA-BN",
            "composition_rollup": {
                "LCHR PANTSIR S1 SA 22 GREYHOUND TELAR WHL": 6,
                "ADA,MIXED,TRACKED,PANTSIRS1": 4,
                "LCHR 2S6 30MM SA 19 TRKD": 12, "2S6 TRANSLOADER": 4,
                "DOG EAR RADAR TORRIKE": 2,
            },
        },
        # ===== 71st MID (2nd-echelon exploitation force) =====
        {
            "id": "U-OCC-71MID-HQ", "name": "71ST MID HQ", "echelon": "DIV",
            "class": "div_hq", "class_template": "OPFOR MECH INF DIV",
            "loc": "FAA SOKOL — 2nd-echelon exploitation",
            "mgrs": "34UFE1319865979",  # FAA SOKOL: 53.83N 22.72E
            "strength": "full",
            "activity": "follow-on exploitation force; commits west through Elk on order — departure is CCIR 3 / DP-4",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCII---IXOC", "uic": "71MID-HQ",
            "composition_rollup": {
                "URAL 375D C3 VAN": 4, "GAZ 66 SIGNAL VAN": 4,
                "MTLB 1V14 TRKD CP": 3,
            },
        },
        {
            "id": "U-OCC-711-MIB", "name": "711 MIB",
            "echelon": "BDE", "class": "mech_inf",
            "class_template": "OPFOR MECH INF BDE",
            "loc": "FAA SOKOL",
            "mgrs": "34UFE1300065800",
            "strength": "full",
            "activity": "2nd-echelon BDE awaiting 72nd MRD culmination",
            "last_obs": "171200Z", "parent": "U-OCC-71MID-HQ",
            "sidc": "SNGPUCIM---HXOC", "uic": "711-MIB",
            "composition_rollup": {
                "BMP 2 IFV": 90, "T 90A": 30, "T 90A ATGM": 6,
                "BRDM 2 HOT 3": 12,
                "HOW SP 2S19 TRKD 152MM": 18,
                "BM-21 GRAD": 6,
                "LCHR 2S6 30MM SA 19 TRKD": 4,
            },
        },
        {
            "id": "U-OCC-712-MIB", "name": "712 MIB",
            "echelon": "BDE", "class": "mech_inf",
            "class_template": "OPFOR MECH INF BDE",
            "loc": "FAA SOKOL",
            "mgrs": "34UFE1350066200",
            "strength": "full",
            "activity": "2nd-echelon BDE awaiting commit",
            "last_obs": "171200Z", "parent": "U-OCC-71MID-HQ",
            "sidc": "SNGPUCIM---HXOC", "uic": "712-MIB",
            "composition_rollup": {
                "BMP 2 IFV": 90, "T 90A": 30, "T 90A ATGM": 6,
                "BRDM 2 HOT 3": 12,
                "HOW SP 2S19 TRKD 152MM": 18,
                "ADA,MIXED,TRACKED,PANTSIRS1": 4,
            },
        },
        {
            "id": "U-OCC-713-MIB", "name": "713 MIB",
            "echelon": "BDE", "class": "mech_inf",
            "class_template": "OPFOR MECH INF BDE",
            "loc": "FAA SOKOL deep",
            "mgrs": "34UFE1400066500",
            "strength": "full",
            "activity": "deep 2nd-echelon — last commitment from SOKOL",
            "last_obs": "171200Z", "parent": "U-OCC-71MID-HQ",
            "sidc": "SNGPUCIM---HXOC", "uic": "713-MIB",
            "composition_rollup": {
                "BMP 2 IFV": 90, "T 90A": 30, "T 90A ATGM": 6,
                "TIGR GAZ 2330 WITH AT14 ATGM": 12,
                "HOW SP 2S19 TRKD 152MM": 18,
                "LCHR 2S6 30MM SA 19 TRKD": 4,
            },
        },
        # ===== Corps-level reinforcing fires + ADA + aviation =====
        {
            "id": "U-OCC-74-SPFA-BDE", "name": "74TH SP ARTILLERY BDE",
            "echelon": "BDE", "class": "arty",
            "class_template": "OPFOR CAA SP ARTY BDE",
            "loc": "between URAL belt and SOKOL — GS counterfire reinforcement",
            "mgrs": "34UFE0700066000",
            "strength": "full",
            "activity": "general support reinforcing fires; mass to 72nd MRD; counterbattery primary",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCFHE--HXOC", "uic": "74-SPFA-BDE",
            "composition_rollup": {
                "HOW SP 2S19 TRKD 152MM": 54,
                "BM-27 URAGAN": 18,
                "RDR CB GM 5951 IL220 TRKD": 4,
                "MTLB 1V13 FDC": 6,
            },
        },
        {
            "id": "U-OCC-75-MSL-BDE", "name": "75TH MISSILE BRIGADE",
            "echelon": "BDE", "class": "ssm",
            "class_template": "OPFOR ISKANDER-M BDE",
            "loc": "Grisian Enclave — Iskander-M TEL forward firing positions",
            "mgrs": "34UFE3500085000",  # ~54.16N 23.07E — deep east
            "strength": "full",
            "activity": "Iskander-M strikes on DIVARTY PAAs + RSS Nodes; CCIR 2 (TEL positioning)",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCFR---HXOC", "uic": "75-MSL-BDE",
            # Iskander-M isn't in EQ_ENGAGEMENT_RANGE_M (would need add for 500km
            # range). For Phase 1 range fans don't derive; SITEMP still shows the
            # unit symbol.
            "composition_rollup": {
                "VEH,MSL TRNSLDR,SP,CSA12": 12,
                "URAL 375D C3 VAN": 4,
                "DOG EAR RADAR TORRIKE": 2,
            },
        },
        {
            "id": "U-OCC-76-ADA-BDE", "name": "76TH AD BRIGADE",
            "echelon": "BDE", "class": "ada",
            "class_template": "OPFOR CAA SAM BDE",
            "loc": "umbrella over 7th CAA assembly + assault depth",
            "mgrs": "34UFE0800065500",  # between URAL and SOKOL
            "strength": "full",
            "activity": "medium-range SAM umbrella; suppresses V Corps aviation; CCIR/DP-3 target",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCDS---HXOC", "uic": "76-ADA-BDE",
            "composition_rollup": {
                "LCHR 9A310M1 SA 11 GADFLY TELAR TRKD": 12,
                "VEH,MSL TRNSLDR,SP,CSA12": 8,
                "LCHR PANTSIR S1 SA 22 GREYHOUND TELAR WHL": 6,
                "GIRAFFE 40 TA RADAR": 2,
            },
        },
        {
            "id": "U-OCC-7CHB", "name": "7TH COMBAT HELICOPTER BDE",
            "echelon": "BDE", "class": "avn",
            "class_template": "OPFOR ARMY AVN BDE",
            "loc": "rear airfield east of FAA SOKOL",
            "mgrs": "34UFE2000080000",
            "strength": "full",
            "activity": "attack aviation + 47th Abn lift; MDCOA: Pabrade Training Area air assault on 2CR rear",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCVRA--HXOC", "uic": "7CHB",
            "composition_rollup": {
                "MI35 EXPORT HIND E": 12,
                "MI24RSH HIND G1 NBC ARMORED": 8,
                "KA 52 HOKUM": 6,
                "MI 8 HIP": 18,
            },
        },
        {
            "id": "U-OCC-7-ENG-BDE", "name": "7TH ENGINEER BDE",
            "echelon": "BDE", "class": "engineer",
            "class_template": "OPFOR CORPS ENG BDE",
            "loc": "Narew/Biebrza bridging stage",
            "mgrs": "34UFE0500067000",
            "strength": "full",
            "activity": "combat bridging at Narew/Biebrza crossings; opens routes for 72nd MRD west",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCEN---HXOC", "uic": "7-ENG-BDE",
            "composition_rollup": {
                "TMM BRIDGING VEH": 12, "PMP BRIDGE": 8,
                "MT 55A AVLB": 4, "AEV IMR 2 COMBAT TRKD": 6,
                "UR 77 MINEFIELD BREACHING VEH": 4,
            },
        },
        {
            "id": "U-OCC-7-LOG-BDE", "name": "7TH LOGISTICS BDE",
            "echelon": "BDE", "class": "sustainment",
            "class_template": "OPFOR CAA LOG BDE",
            "loc": "deep rear — sustains 72nd MRD + 71st MID",
            "mgrs": "34UFE2500075000",
            "strength": "full",
            "activity": "3-day combat load forward; Class V push to firing units",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCS----HXOC", "uic": "7-LOG-BDE",
            "composition_rollup": {
                "TRK CGO URAL 5323 8X8 9000KG WHL": 60,
                "TRK FUEL URAL 375 WHL": 30,
                "TRK FUEL KRAZ 260 9000L WHL": 20,
                "TRK CRANE KRAZ 255 WHL": 6,
            },
        },
        {
            "id": "U-OCC-750-SPF-BDE", "name": "750TH SEPARATE SPF BDE",
            "echelon": "BDE", "class": "recon",
            "class_template": "OPFOR SPF BDE",
            "loc": "infiltrated TRM corridor — interdiction",
            "mgrs": "34UEE8500050000",  # ~along DK16 east of OBJ
            "strength": "full",
            "activity": "interdict V Corps TRM corridor (Route 58); ambush sustainment convoys",
            "last_obs": "171200Z", "parent": "U-OCC-7CAA-HQ",
            "sidc": "SNGPUCRR---HXOC", "uic": "750-SPF-BDE",
            "composition_rollup": {
                "GAZ 2330 TIGR DSHK": 12,
                "GAZ 2330 TIGR AGL": 8,
                "BRM 3K RECON VEHICLE": 6,
                "ZALA 421 08M UAV": 12,
            },
        },
    ],

    # -------------------------------------------------------------------------
    # BLUE — V Corps task org per OPORD §3.c
    # -------------------------------------------------------------------------
    # 2CR is SOUTH Sector main effort (defend OBJ JACKSON); 1ABCT fixes
    # CENTER (Silmaris Gap); 1CD = NORTH cavalry + theater reserve; 173d ABCT
    # OPCON FINDEFCOM (Finland delay); 210th FA Brigade = V Corps DIVARTY;
    # 11th CAB = V Corps aviation; 21st TSC Fwd = sustainment.
    "unit_blue": [
        {
            "id": "U-BL-VCORPS-HQ", "name": "V CORPS HQ", "echelon": "CORPS",
            "class": "corps_hq", "loc": "Wiesbaden, Germany (Main CP)",
            "mgrs": "32UNB8480053200",  # rough Wiesbaden
            "strength": "full",
            "activity": "V Corps Main CP — directing simultaneous multi-sector area defense",
            "last_obs": "171200Z", "parent": None,
            "sidc": "SFGPUCIZ---KXCG",
        },
        {
            "id": "U-BL-VCORPS-TAC-FWD", "name": "V CORPS TAC FWD",
            "echelon": "CORPS", "class": "corps_hq",
            "loc": "Elblag, Poland (TAC Fwd, co-located MND-NE) NLT 15 AUG 2026",
            "mgrs": "34UCE5000060000",  # rough Elblag
            "strength": "full",
            "activity": "V Corps TAC Forward — CDR forward C2 from 15 AUG",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCIZ---HXCG",
        },
        # ===== 2CR — SOUTH Sector main effort =====
        {
            "id": "U-BL-2CR-HQ", "name": "2CR HQ", "echelon": "BDE",
            "class": "cav", "loc": "OBJ JACKSON forward defensive sector",
            "mgrs": "34UEE6250052500",  # CP TRIX
            "strength": "full",
            "activity": "directing 2CR area defense at OBJ JACKSON in depth",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCRVA--HXCG",
        },
        {
            "id": "U-BL-2CR-1SQDN", "name": "1ST SQDN, 2CR", "echelon": "BN",
            "class": "cav", "loc": "OBJ JACKSON forward — eastern EAs",
            "mgrs": "34UEE6800054800",  # forward east of OBJ
            "strength": "full",
            "activity": "forward defense; EA-3 (Klusy) primary; counter-recon",
            "last_obs": "171200Z", "parent": "U-BL-2CR-HQ",
            "sidc": "SFGPUCRVA--FXCG",
        },
        {
            "id": "U-BL-2CR-2SQDN", "name": "2ND SQDN, 2CR", "echelon": "BN",
            "class": "cav", "loc": "OBJ JACKSON forward — central EAs",
            "mgrs": "34UEE6700053500",
            "strength": "full",
            "activity": "forward defense; EA-2 (Nitki) primary",
            "last_obs": "171200Z", "parent": "U-BL-2CR-HQ",
            "sidc": "SFGPUCRVA--FXCG",
        },
        {
            "id": "U-BL-2CR-3SQDN", "name": "3RD SQDN, 2CR", "echelon": "BN",
            "class": "cav", "loc": "OBJ JACKSON forward — southern EAs",
            "mgrs": "34UEE6700051500",
            "strength": "full",
            "activity": "forward defense; EA-1 (Drygały) primary; southern flank",
            "last_obs": "171200Z", "parent": "U-BL-2CR-HQ",
            "sidc": "SFGPUCRVA--FXCG",
        },
        {
            "id": "U-BL-2CR-FAS", "name": "FAS / TF HELL",
            "echelon": "BN", "class": "arty",
            "loc": "PAA 34U EE 58250 56900 (DIVARTY co-located)",
            "mgrs": "34UEE5825056900",
            "strength": "full",
            "activity": "2CR organic fires + ROC-T deep counterfire",
            "last_obs": "171200Z", "parent": "U-BL-2CR-HQ",
            "sidc": "SFGPUCFHE--FXCG",
        },
        {
            "id": "U-BL-2CR-ROC-T", "name": "ROC-T (ATK PSN VENUS)",
            "echelon": "BN", "class": "arty",
            "loc": "ATK PSN VENUS — Lithuania (out of sector, deep)",
            "mgrs": "35UMA2242298926",
            "strength": "full",
            "activity": "deep counterfire / strategic strike — ATACMS DP-2 trigger",
            "last_obs": "171200Z", "parent": "U-BL-2CR-HQ",
            "sidc": "SFGPUCFR---FXCG",
        },
        {
            "id": "U-BL-ITALIAN-WGX", "name": "ITALIAN WGX BG (OPCON 2CR)",
            "echelon": "BN", "class": "mech_inf",
            "loc": "IVO PL YELLOW — Italian WGX Area",
            "mgrs": "34UEE6200048500",
            "strength": "full",
            "activity": "coalition mechanized BG integrated under 2CR; PL YELLOW frontage",
            "last_obs": "171200Z", "parent": "U-BL-2CR-HQ",
            "sidc": "SFGPUCIM---FXIT",
        },
        # ===== 210th FA Brigade (V Corps DIVARTY) =====
        {
            "id": "U-BL-210FA-HQ", "name": "210TH FA BRIGADE",
            "echelon": "BDE", "class": "arty",
            "loc": "PAA 34U EE 58250 56900",
            "mgrs": "34UEE5825056900",
            "strength": "full",
            "activity": "GS reinforcing fires; priority: 75th MB TELs, 7th CAA artillery, 76th ADA",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCFHE--HXCG",
        },
        # ===== 11th CAB =====
        {
            "id": "U-BL-11CAB-HQ", "name": "11TH CAB", "echelon": "BDE",
            "class": "avn", "loc": "Poland (V Corps CAB forward)",
            "mgrs": "34UEE5500055000",
            "strength": "full",
            "activity": "ISR + JAAT (DP-3); MEDEVAC OPCON 2CR",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCVRA--HXCG",
        },
        # ===== 21st TSC Fwd + RSS Nodes =====
        {
            "id": "U-BL-21TSC-FWD", "name": "21ST TSC FWD", "echelon": "BDE",
            "class": "sustainment", "loc": "BPTA sustainment area",
            "mgrs": "34UEE6000053500",  # RSS Node X
            "strength": "full",
            "activity": "theater sustainment to SOUTH Sector via RSS Nodes X/Y/Z",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCS----HXCG",
        },
        {
            "id": "U-BL-RSS-X", "name": "RSS NODE X (C2 / CLASS IX)",
            "echelon": "BN", "class": "sustainment",
            "loc": "BPTA rear, mid-depth", "mgrs": "34UEE6000053500",
            "strength": "full",
            "activity": "Class IX repair parts; central C2 of RSS triplet",
            "last_obs": "171200Z", "parent": "U-BL-21TSC-FWD",
            "sidc": "SFGPUCS----FXCG",
        },
        {
            "id": "U-BL-RSS-Y", "name": "RSS NODE Y (ROLE II / MEDEVAC)",
            "echelon": "BN", "class": "sustainment",
            "loc": "BPTA SW — biased east toward Elk District Hospital",
            "mgrs": "34UEE6000051000",
            "strength": "full",
            "activity": "Role II medical; MEDEVAC routing to Elk District Hospital (Role 3)",
            "last_obs": "171200Z", "parent": "U-BL-21TSC-FWD",
            "sidc": "SFGPUCSM---FXCG",
        },
        {
            "id": "U-BL-RSS-Z", "name": "RSS NODE Z (CLASS V / AMMO)",
            "echelon": "BN", "class": "sustainment",
            "loc": "BPTA NW — paired with DIVARTY for ammo push",
            "mgrs": "34UEE5880056200",
            "strength": "full",
            "activity": "Class V ammunition forward push; co-located with DIVARTY for short LOC",
            "last_obs": "171200Z", "parent": "U-BL-21TSC-FWD",
            "sidc": "SFGPUCS----FXCG",
        },
        # ===== Other sectors (per OPORD §3.c.(2)/(3)/(4) — minimal stubs) =====
        {
            "id": "U-BL-1ABCT-HQ", "name": "1ST ABCT",
            "echelon": "BDE", "class": "mech",
            # CENTER Sector mid-corridor per OPORD §3.c.(2): "Defend
            # CENTER Sector and deny OCCASUS seizure of the Silmaris Gap
            # and Augustów-Silmaris-Białystok corridor." Sokółka area
            # (53.40°N 23.51°E) sits mid-corridor between Augustów (NE)
            # and Białystok (SW) where the supporting effort can fix
            # OCCASUS lateral pressure into the V Corps CENTER without
            # forfeiting the BPTA / 2CR seam. Was placed wrongly at
            # 35UMA3000050000 (~55°N 25.9°E, deep Lithuania near the
            # Belarus border — ~200 km east of the corridor Joe named).
            "loc": "Sokółka area, mid Augustów-Silmaris-Białystok corridor (CENTER Sector)",
            "mgrs": "34UFE6687019703",
            "strength": "full",
            "activity": "CENTER fix; Silmaris Gap defense; flank coord w/ Polish 18th MD",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCIM---HXCG",
        },
        {
            "id": "U-BL-1CD-HQ", "name": "1ST CD (THEATER RESERVE)",
            "echelon": "DIV", "class": "div_hq",
            "loc": "Grafenwohr Training Area, Germany",
            "mgrs": "33UUQ5000040000",  # rough Grafenwohr
            "strength": "full",
            "activity": "theater reserve — uncommitted, V Corps CDR release only",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCIZ---IXCG",
        },
        {
            "id": "U-BL-173ABCT-HQ", "name": "173D ABCT (OPCON FINDEFCOM)",
            "echelon": "BDE", "class": "airborne",
            "loc": "Kajaani axis, Finland",
            "mgrs": "35VPK0000040000",  # rough Kajaani
            "strength": "full",
            "activity": "Finland delay under FINDEFCOM; reports 4th AC indicators",
            "last_obs": "171200Z", "parent": "U-BL-VCORPS-HQ",
            "sidc": "SFGPUCIA---HXCG",
        },
    ],

    # -------------------------------------------------------------------------
    # Empty / stub collections — Phase 1 baseline. Wired through the pipeline
    # so DAG executor doesn't crash on missing keys; populated later if Joe
    # wires SIGINT / civilian / target-grid feeds.
    # -------------------------------------------------------------------------
    "target": [],
    "civil": [
        {
            "id": "CV-ELK", "ascope": "structures", "pmesii": "infrastructure",
            "name": "Elk District Hospital", "type": "hospital",
            "mgrs": "34UEE8200068000", "population": 61000,
            "details": "Polish Role 2 facility; CASEVAC routing via MEDEVAC chain; designated NFA",
        },
        {
            "id": "CV-PISZ", "ascope": "structures", "pmesii": "infrastructure",
            "name": "Pisz Fuel Depot", "type": "industrial",
            "mgrs": "34UEE5500040000", "population": 19000,
            "details": "Civilian POL depot; designated NFA",
        },
        {
            "id": "CV-ORZYSZ", "ascope": "structures", "pmesii": "infrastructure",
            "name": "Orzysz Water Treatment", "type": "utility",
            "mgrs": "34UEE6100049000", "population": 5500,
            "details": "BPTA garrison town water treatment; designated NFA",
        },
    ],
    "engagement": [],
    "event": [],
    "hvt_candidate": [],
    "imagery": [],
    "report": [],
}


# ============================================================================
# TERRAIN — physical + tactical terrain analysis
# ============================================================================
TERRAIN = {
    "aoi": "BPTA / V Corps SOUTH Sector (MITS 26)",
    "summary": (
        "AO encompasses Bemowo Piskie Training Area (BPTA), NE Poland — mixed "
        "forest and agricultural terrain with Narew/Biebrza/Pisa river corridors "
        "canalizing 7th CAA movement onto predictable axes toward OBJ JACKSON. "
        "Sandy loam soil firms dry, degrades after rain. Threat axis EAST through "
        "Elk corridor. OBJ JACKSON is decisive terrain; Elk District Hospital, "
        "Pisz Fuel Depot, Orzysz Water Treatment are protected NFAs."
    ),

    # ---- Avenues of approach (threat side, west-into-OBJ JACKSON) -----------
    "avenues_of_approach": [
        {
            "id": "AA-72MRD-DK16",
            "side": "threat",
            "echelon": "brigade",
            "origin_name": "FAA URAL-CENTRAL (72nd MRD assembly)",
            "objective_name": "OBJ JACKSON via DK16 / Klusy corridor (main effort)",
            "central_axis_polyline": [
                [22.5500, 53.8200],   # FAA URAL-CENTRAL
                [22.4000, 53.8100],   # midway
                [22.2500, 53.8000],
                [22.0950, 53.7940],   # vic Klusy / EA-3
                [21.9762, 53.7351],   # OBJ JACKSON
            ],
            "envelope_polygon": [
                [22.5500, 53.8400], [22.4000, 53.8300], [22.2500, 53.8200],
                [22.0950, 53.8140], [21.9762, 53.7551],
                [21.9762, 53.7151], [22.0950, 53.7740], [22.2500, 53.7800],
                [22.4000, 53.7900], [22.5500, 53.8000], [22.5500, 53.8400],
            ],
            "bundle_halfwidth_m": 4000.0,
            "rationale": "72nd MRD main effort via DK16 / Klusy axis. OPORD §1.c.(1) "
                          "names this as the canalized 72nd MRD attack route. EA-3 is sited astride this approach.",
        },
        {
            "id": "AA-72MRD-NITKI-RAIL",
            "side": "threat",
            "echelon": "brigade",
            "origin_name": "FAA URAL-CENTRAL (72nd MRD southern sub-axis)",
            "objective_name": "OBJ JACKSON via Nitki / DW667 rail corridor",
            "central_axis_polyline": [
                [22.5500, 53.8200], [22.4000, 53.7800], [22.2500, 53.7400],
                [22.1206, 53.7175], [21.9762, 53.7351],
            ],
            "envelope_polygon": [
                [22.5500, 53.8350], [22.4000, 53.7950], [22.2500, 53.7550],
                [22.1206, 53.7325], [21.9762, 53.7501],
                [21.9762, 53.7201], [22.1206, 53.7025], [22.2500, 53.7250],
                [22.4000, 53.7650], [22.5500, 53.8050], [22.5500, 53.8350],
            ],
            "bundle_halfwidth_m": 3000.0,
            "rationale": "72nd MRD southern sub-axis via Nitki + DW667 / rail. EA-2 sited astride.",
        },
        {
            "id": "AA-72MRD-DRYGALY",
            "side": "threat",
            "echelon": "brigade",
            "origin_name": "FAA URAL-CENTRAL (72nd MRD southern flank)",
            "objective_name": "OBJ JACKSON via Drygały axis",
            "central_axis_polyline": [
                [22.5500, 53.8200], [22.4000, 53.7600], [22.2500, 53.7100],
                [22.1067, 53.6861], [21.9762, 53.7351],
            ],
            "envelope_polygon": [
                [22.5500, 53.8350], [22.4000, 53.7750], [22.2500, 53.7250],
                [22.1067, 53.7011], [21.9762, 53.7501],
                [21.9762, 53.7201], [22.1067, 53.6711], [22.2500, 53.6950],
                [22.4000, 53.7450], [22.5500, 53.8050], [22.5500, 53.8350],
            ],
            "bundle_halfwidth_m": 3000.0,
            "rationale": "72nd MRD flank protection axis via Drygały. EA-1 sited astride.",
        },
        {
            "id": "AA-71MID-EXPLOIT",
            "side": "threat",
            "echelon": "division",
            "origin_name": "FAA SOKOL (71st MID 2nd echelon)",
            "objective_name": "OBJ JACKSON exploitation (post-72nd-MRD-culmination)",
            "central_axis_polyline": [
                [22.7200, 53.8300],   # FAA SOKOL
                [22.5500, 53.8200],   # passes through 72nd MRD AA
                [22.2500, 53.8000],
                [22.0000, 53.7600],
                [21.9762, 53.7351],
            ],
            "envelope_polygon": [
                [22.7200, 53.8500], [22.5500, 53.8400], [22.2500, 53.8200],
                [22.0000, 53.7800], [21.9762, 53.7551],
                [21.9762, 53.7151], [22.0000, 53.7400], [22.2500, 53.7800],
                [22.5500, 53.8000], [22.7200, 53.8100], [22.7200, 53.8500],
            ],
            "bundle_halfwidth_m": 5000.0,
            "rationale": "71st MID 2nd-echelon exploitation follows the 72nd MRD axis west; CCIR 3 / DP-4 trigger.",
        },
    ],

    # ---- Engagement areas (BLUE kill zones astride threat AAs) -------------
    "engagement_areas_catalog": [
        {
            "id": "EA-1-DRYGALY",
            "name": "EA-1 (Drygały axis)",
            "subkind": "POLY", "side": "friendly",
            "mgrs": "34UEE7308649166",
            "polygon": [
                [22.0900, 53.6750], [22.1250, 53.6750],
                [22.1250, 53.6970], [22.0900, 53.6970], [22.0900, 53.6750],
            ],
            "linked_avenue_id": "AA-72MRD-DRYGALY",
            "linked_unit": "U-OCC-723-MRB",
            "firing_formation": "3rd SQDN, 2CR + 210th FA",
            "rationale": "Kill area astride 72nd MRD southern flank approach via Drygały (DW667 / rail corridor)."
                          " Per Joe iteration #7 + OPORD ¶3.c.(1).c.",
        },
        {
            "id": "EA-2-NITKI",
            "name": "EA-2 (Nitki axis)",
            "subkind": "POLY", "side": "friendly",
            "mgrs": "34UEE7394952674",
            "polygon": [
                [22.1050, 53.7080], [22.1380, 53.7080],
                [22.1380, 53.7280], [22.1050, 53.7280], [22.1050, 53.7080],
            ],
            "linked_avenue_id": "AA-72MRD-NITKI-RAIL",
            "linked_unit": "U-OCC-721-MRB",
            "firing_formation": "2nd SQDN, 2CR + 210th FA",
            "rationale": "Kill area on rail/DW667 corridor — Nitki SE of Bemowo Piskie. Per Joe iteration #7.",
        },
        {
            "id": "EA-3-KLUSY",
            "name": "EA-3 (Klusy / DK16 main effort)",
            "subkind": "POLY", "side": "friendly",
            "mgrs": "34UEE7212961158",
            "polygon": [
                [22.0700, 53.7800], [22.1200, 53.7800],
                [22.1200, 53.8080], [22.0700, 53.8080], [22.0700, 53.7800],
            ],
            "linked_avenue_id": "AA-72MRD-DK16",
            "linked_unit": "U-OCC-721-MRB",
            "firing_formation": "1st SQDN, 2CR + 210th FA (mass)",
            "rationale": "Main-effort kill area astride DK16 / Klusy. EA-2 sized larger (2.8 × 2 km) than "
                          "EA-1/EA-3 per Joe iteration #4 — 72nd MRD main-effort axis weight.",
        },
    ],

    # ---- Threat objectives (RED-side) --------------------------------------
    "threat_objectives": [
        {
            "id": "THR-OBJ-JACKSON",
            "name": "OBJ JACKSON (seizure target)",
            "mgrs": "34UEE6439254490",
            "default_intent": "seize",
            "rationale": "72nd MRD assault objective — OPORD §1.c.(1). Seizure denies V Corps SOUTH Sector decisive terrain.",
        },
        {
            "id": "THR-OBJ-DIVARTY",
            "name": "210th FA Brigade PAA (Iskander-M target)",
            "mgrs": "34UEE5825056900",
            "default_intent": "destroy",
            "rationale": "75th Missile Brigade Iskander-M priority target per OPORD §1.c.(1). DP-2 / CCIR 2 trigger.",
        },
        {
            "id": "THR-OBJ-RSS",
            "name": "RSS Nodes X/Y/Z (Iskander-M target)",
            "mgrs": "34UEE6000053500",
            "default_intent": "destroy",
            "rationale": "75th Missile Brigade Iskander-M priority target — V Corps logistics nodes. EEFI 3.",
        },
    ],

    # ---- NAIs (Named Areas of Interest) ------------------------------------
    "nai_catalog": [
        {
            "id": "NAI-201-URAL-CENTRAL",
            "name": "FAA URAL-CENTRAL surveillance",
            "mgrs": "34UFE0203564609",
            "tied_to_aa": "AA-72MRD-DK16",
            "threat_branch": "main_effort",
            "watching_for": "72nd MRD assault echelon departure indicators — CCIR 1 main effort axis confirmation",
        },
        {
            "id": "NAI-202-URAL-NORTH",
            "name": "FAA URAL-NORTH surveillance",
            "mgrs": "34UEE9982275691",
            "tied_to_aa": "AA-72MRD-DK16",
            "threat_branch": "main_effort",
            "watching_for": "722 MRB departure (northern sub-axis); CCIR 1",
        },
        {
            "id": "NAI-203-SOKOL",
            "name": "FAA SOKOL surveillance — 71st MID departure",
            "mgrs": "34UFE1319865979",
            "tied_to_aa": "AA-71MID-EXPLOIT",
            "threat_branch": "exploit",
            "watching_for": "71st MID departure from SOKOL — CCIR 3 / DP-4 CDR counterattack trigger",
        },
        {
            "id": "NAI-204-75MB-TELS",
            "name": "75th Missile Brigade Iskander-M TEL forward firing positions",
            "mgrs": "34UFE3500085000",
            "tied_to_aa": "AA-72MRD-DK16",
            "threat_branch": "main_effort",
            "watching_for": "Iskander-M TEL repositioning to forward firing positions — CCIR 2 / DP-2 ATACMS trigger",
        },
        {
            "id": "NAI-205-DK16-KLUSY",
            "name": "DK16 vic Klusy — main-effort lead element",
            "mgrs": "34UEE7212961158",
            "tied_to_aa": "AA-72MRD-DK16",
            "threat_branch": "main_effort",
            "watching_for": "72nd MRD lead element crossing into EA-3 — close fight trigger",
        },
    ],

    # ---- Decision points ----------------------------------------------------
    "dp_catalog": [
        {
            "id": "DP-1",
            "name": "2CR authorizes forward engagement at EAs",
            "friendly_decision": "Authorize forward engagement at designated EAs (EA-1/2/3)",
            "owning_commander": "2CR CDR",
            "phase": "Phase IV (H+0 to H+12)",
            "ltiov": "H+12",
            "triggering_indicator": "72nd MRD main effort axis confirmed (CCIR 1)",
            "linked_nais": ["NAI-201-URAL-CENTRAL", "NAI-202-URAL-NORTH", "NAI-205-DK16-KLUSY"],
        },
        {
            "id": "DP-2",
            "name": "ATACMS authorized against 75th Missile Brigade TELs",
            "friendly_decision": "Authorize ATACMS fires against 75th MB Iskander-M TEL forward firing positions",
            "owning_commander": "V Corps CDR + USAREUR-AF G3 DOCC",
            "phase": "Phase IV (H+4 to H+16)",
            "ltiov": "H+16",
            "triggering_indicator": "75th MB TELs at forward firing positions confirmed (CCIR 2)",
            "linked_nais": ["NAI-204-75MB-TELS"],
        },
        {
            "id": "DP-3",
            "name": "V Corps CAB AH-64 JAAT committed",
            "friendly_decision": "Commit AH-64 JAAT against 7th CAA 2nd-echelon assembly areas",
            "owning_commander": "V Corps CDR",
            "phase": "Phase IV (H+18 to H+30)",
            "ltiov": "H+30",
            "triggering_indicator": "76th AD Brigade SAM suppressed",
            "linked_nais": ["NAI-203-SOKOL"],
        },
        {
            "id": "DP-4",
            "name": "V Corps CDR evaluates 1CD theater reserve counterattack",
            "friendly_decision": "Commit (or hold) 1CD theater reserve for counterattack to SOUTH Sector",
            "owning_commander": "V Corps CDR (EXPLICIT order only)",
            "phase": "Phase IV (H+24 to H+48)",
            "ltiov": "H+48",
            "triggering_indicator": "71st MID departs FAA SOKOL (CCIR 3) — CDR's counterattack decision trigger",
            "linked_nais": ["NAI-203-SOKOL"],
        },
    ],

    # ---- Key terrain --------------------------------------------------------
    "key_terrain": [
        {"name": "OBJ JACKSON", "mgrs": "34UEE6439254490", "decisive": True,
         "side": "friendly",
         "value": "Decisive terrain SOUTH Sector — 2CR area defense centerpiece; loss collapses V Corps "
                  "SOUTH Sector defense per OPORD ¶3.a.(1) Commander's Intent"},
        {"name": "Elk corridor (Narew/Biebrza/Pisa)", "mgrs": "34UEE7500060000", "decisive": True,
         "side": "neutral",
         "value": "River-corridor terrain canalizes 7th CAA movement onto predictable axes — OPORD ¶1.b.(1)"},
        {"name": "Elk (city + Role 2 hospital + rail logistics hub)", "mgrs": "34UEE8200068000",
         "decisive": False, "side": "neutral",
         "value": "Polish HNS rail logistics hub; CASEVAC Role 2; population 61,000; protected NFA"},
        {"name": "Orzysz (BPTA garrison town)", "mgrs": "34UEE6100049000",
         "decisive": False, "side": "neutral",
         "value": "BPTA garrison town; water treatment NFA; population 5,500"},
        {"name": "Pisz (fuel depot)", "mgrs": "34UEE5500040000",
         "decisive": False, "side": "neutral",
         "value": "Civilian POL depot NFA; population 19,000"},
        {"name": "Silmaris Gap (CENTER Sector)", "mgrs": "35UMA3000050000",
         "decisive": True, "side": "neutral",
         "value": "Open terrain favorable to OCCASUS combined arms maneuver; loss isolates Baltic from NATO "
                  "reinforcement per OPORD ¶1.b.(1)"},
    ],

    # ---- Range fans — empty for Phase 1; populated at SITEMP fanout time
    # via _extract_writer_range_fans (which reads each templated unit's
    # composition_rollup and emits one range fan per range-fan-eligible
    # piece of organic equipment). The same path the IDX 26 stack uses.
    "range_fans": [],

    # ---- Battle positions (BLUE) -------------------------------------------
    "battle_positions": [
        {
            "id": "BP-2CR-1SQDN-EAST",
            "name": "BP East (1st SQDN, 2CR — EA-3 orientation)",
            "parent_unit_id": "U-BL-2CR-1SQDN",
            "bp_type": "complex",
            "centerline": [[22.000, 53.740], [22.060, 53.760]],
            "envelope_polygon": [
                [21.990, 53.730], [22.070, 53.730],
                [22.070, 53.770], [21.990, 53.770], [21.990, 53.730],
            ],
            "default_occupancy": "occupied",
            "orientation_deg": 90,  # facing east toward Klusy
            "rationale": "Forward defense BP oriented EAST toward EA-3 / Klusy / DK16 main-effort approach",
        },
        {
            "id": "BP-2CR-2SQDN-CENTER",
            "name": "BP Center (2nd SQDN, 2CR — EA-2 orientation)",
            "parent_unit_id": "U-BL-2CR-2SQDN",
            "bp_type": "complex",
            "centerline": [[22.010, 53.720], [22.060, 53.730]],
            "envelope_polygon": [
                [22.000, 53.715], [22.070, 53.715],
                [22.070, 53.740], [22.000, 53.740], [22.000, 53.715],
            ],
            "default_occupancy": "occupied",
            "orientation_deg": 105,
            "rationale": "Forward defense BP oriented EAST/SE toward EA-2 / Nitki rail corridor",
        },
        {
            "id": "BP-2CR-3SQDN-SOUTH",
            "name": "BP South (3rd SQDN, 2CR — EA-1 orientation)",
            "parent_unit_id": "U-BL-2CR-3SQDN",
            "bp_type": "complex",
            "centerline": [[22.000, 53.700], [22.060, 53.710]],
            "envelope_polygon": [
                [21.990, 53.690], [22.070, 53.690],
                [22.070, 53.720], [21.990, 53.720], [21.990, 53.690],
            ],
            "default_occupancy": "occupied",
            "orientation_deg": 110,
            "rationale": "Forward defense BP oriented SE toward EA-1 / Drygały axis (DW667 / rail)",
        },
    ],

    "forward_bp_belt_centerlines": [
        {
            "id": "FBPBC-2CR-FORWARD",
            "division_id": "U-BL-2CR-HQ",
            "centerline": [
                [21.990, 53.690], [22.005, 53.715], [22.020, 53.740],
                [22.040, 53.770], [22.060, 53.800],
            ],
            "rationale": "2CR forward BP belt — three squadrons disposed in depth astride OBJ JACKSON",
        },
    ],

    # ---- Defensible terrain / obstacles / mobility -------------------------
    "defensible_terrain": [
        {
            "id": "DT-OBJ-JACKSON-RIDGE",
            "name": "OBJ JACKSON defensive ridge complex",
            "mgrs": "34UEE6439254490",
            "note": "BPTA defensive ground; mixed forest + agricultural — concealment for prepared positions",
            "polygon": [
                [21.960, 53.720], [22.000, 53.720],
                [22.000, 53.760], [21.960, 53.760], [21.960, 53.720],
            ],
        },
    ],

    "obstacles": [
        {
            "name": "BPTA obstacle belt (eastern frontage)",
            "type": "man_made",
            "mgrs": "34UEE7000054000",
            "tactical_effect": "turn",
            "mobility_class": "severely_restricted",
            "details": "2CR-constructed obstacle belt forward of OBJ JACKSON, turning 72nd MRD into the three EAs",
        },
    ],

    # ---- Mobility + observation/cover stubs (no per-tile data for Phase 1) -
    "mobility_classifications": [],
    "observation_and_fields_of_fire": {},
    "cover_and_concealment": {},

    # ---- Legacy field — kept as empty list to satisfy fail-fast grep --------
    "_legacy_polylines_removed_kept_for_grep_fail_fast": [],
}


# ============================================================================
# OPERATIONS — friendly control measures (phase lines, axes, boundaries)
# ============================================================================
# Per OPORD §3.b — V Corps area defense. Phase lines reoriented N-S per Joe
# iteration #6 (threat axis is EAST through Elk corridor, so successive lines
# the enemy crosses moving WEST: PL YELLOW LD/LC east of FAA URAL belt, then
# PL GOLD at OBJ JACKSON, then PL BLUE rear; FSCL well east of the FAA belt).
OPERATIONS = {
    "operation_name": "V CORPS DEFENSE OF OBJ JACKSON (MITS 26)",
    "ld": "PL YELLOW (LD/LC)",
    "phase_lines": [
        {
            "id": "PL-YELLOW",
            "name": "PL YELLOW (LD/LC)",
            "role": "line_of_departure",
            "geom": [[22.22, 53.50], [22.22, 54.10]],
            "note": "Line of departure / Line of contact — east of FAA URAL belt; enemy crosses moving west. "
                     "Italian WGX Battle Group integrates at IVO PL YELLOW per OPORD ¶3.c.(1).f.",
        },
        {
            "id": "PL-GOLD",
            "name": "PL GOLD",
            "role": "security_area",
            "geom": [[22.05, 53.50], [22.05, 54.10]],
            "note": "Security area phase line just east of OBJ JACKSON; EAs sit between PL YELLOW and PL GOLD.",
        },
        {
            "id": "PL-BLUE",
            "name": "PL BLUE",
            "role": "battle_handover",
            "geom": [[21.88, 53.50], [21.88, 54.10]],
            "note": "Battle handover line rear of OBJ JACKSON; RSS nodes sit behind PL BLUE.",
        },
        {
            "id": "FSCL-VCORPS",
            "name": "FSCL (V Corps)",
            "role": "fire_support_coord",
            "geom": [[22.30, 53.50], [22.30, 54.10]],
            "note": "Fire Support Coordination Line — well east of FAA belt. Counterfire against URAL/SOKOL FAAs "
                     "falls beyond FSCL and triggers V Corps CDR + DOCC approval per OPORD ¶3.d.(2).b.",
        },
    ],
    "axes_of_advance": [],  # V Corps area defense — no friendly offensive axes
    "boundaries": [
        {
            "id": "BND-BPTA",
            "name": "BPTA Training Area Boundary",
            "subkind": "training_area",
            "geom": [
                [21.86256, 53.65964],  # SW corner (34UEE5700046000)
                [22.18432, 53.65964],
                [22.18432, 53.80071],  # NE corner (34UEE7800062000)
                [21.86256, 53.80071],
                [21.86256, 53.65964],
            ],
            "note": "Bemowo Piskie Training Area — 2CR area defense centered inside; OBJ JACKSON well inside the box",
        },
        {
            "id": "BND-VCORPS-SOUTH-SECTOR",
            "name": "V Corps SOUTH Sector boundary",
            "subkind": "sector",
            "geom": [
                [21.50, 53.50], [23.00, 53.50],
                [23.00, 54.10], [21.50, 54.10],
                [21.50, 53.50],
            ],
            "note": "V Corps SOUTH Sector AOR — encompasses BPTA + Elk corridor + FAA depth east + town anchors",
        },
    ],
}


# ============================================================================
# WEATHER — per OPORD §1.b.(2) summer conditions, 53°N latitude
# ============================================================================
# Summer (JUL-AUG): 16-17h daylight; afternoon thunderstorms 15-25mm 3-5x/wk;
# morning fog in river valley bottoms (Pisa, Narew, Biebrza) 0500-0800;
# reduced thermal contrast limits FLIR.
WEATHER = {
    "id": "W-171200",
    "current_dtg": "171200ZJUL26",
    "current": {
        "temp_c": 22, "wind_kt": 6, "wind_dir_true": 240,
        "vis_km": 14, "ceiling_ft": 6000, "cloud_cover": "scattered",
        "precip": "none", "humidity_pct": 68, "pressure_inhg": 29.94,
        "rotary_ok": True, "fixed_ok": True,
    },
    "forecast": [
        {"valid_dtg": "171800Z", "temp_c": 26, "wind_kt": 8, "wind_dir_true": 250,
         "vis_km": 12, "ceiling_ft": 4500, "cloud_cover": "broken",
         "precip": "afternoon thunderstorms 15mm", "humidity_pct": 72, "pressure_inhg": 29.90,
         "rotary_ok": True, "fixed_ok": True},
        {"valid_dtg": "180600Z", "temp_c": 17, "wind_kt": 4, "wind_dir_true": 220,
         "vis_km": 3, "ceiling_ft": 800, "cloud_cover": "overcast",
         "precip": "morning fog Pisa/Narew valleys", "humidity_pct": 96, "pressure_inhg": 29.96,
         "rotary_ok": False, "fixed_ok": False},
        {"valid_dtg": "181200Z", "temp_c": 23, "wind_kt": 6, "wind_dir_true": 240,
         "vis_km": 13, "ceiling_ft": 5500, "cloud_cover": "scattered",
         "precip": "none", "humidity_pct": 66, "pressure_inhg": 29.98,
         "rotary_ok": True, "fixed_ok": True},
    ],
    "doctrinal_notes": (
        "Sustained 16-17h daylight at 53°N compresses OCCASUS night movement windows. "
        "Afternoon thunderstorms (15-25mm, 3-5/wk) create soft ground reducing off-road "
        "trafficability + degrade UAS. Morning fog in river valleys (Pisa/Narew/Biebrza) "
        "0500-0800 degrades ISR. Reduced thermal contrast limits FLIR effectiveness."
    ),
}


# ============================================================================
# PROJECT_DOCS — text blobs used by the agent LLM as scenario context
# ============================================================================
# Mirror the IDX 26 keyset so any consumer (manual_injects loader, intel_agent
# context-building, etc.) finds the expected names. Bodies are short pointers
# to the canonical V Corps source materials; the full OPORD docx is in
# scratch/ibl/joe-test/.
# PROJECT_DOCS item shape (matches scenario.PROJECT_DOCS):
#   {"title": str, "body": str, "type": str, "source_path": str?}
# dag_executor._scenario_fixture_block reads `.get("OPORD", {}).get("body", "")`
# — values MUST be dicts with a "body" key, NOT bare strings.
def _doc(title: str, type_: str, body: str, source_path: str = "") -> dict:
    d = {"title": title, "type": type_, "body": body.strip()}
    if source_path:
        d["source_path"] = source_path
    return d


PROJECT_DOCS = {
    "OPORD": _doc(
        "V Corps OPORD 26-001 (MITS 26 — EASTERN FLANK LAND CAMPAIGN)",
        "opord",
        """V Corps OPORD 26-001 (MITS 26 — EASTERN FLANK LAND CAMPAIGN).
H-Hour 11 0000L AUG 2026. V Corps (LTG Richardson) conducts simultaneous
multi-sector area defense — SOUTH Sector main effort 2CR at OBJ JACKSON
(34U EE 64392 54490) defeats OCCASUS 7th CAA assault through Elk corridor;
210th FA Brigade GS reinforcing fires; 11th CAB ISR + JAAT; 21st TSC Fwd
RSS Nodes X/Y/Z. CENTER (1ABCT) fixes Silmaris Gap; NORTH (1CD) secures
Baltic LOCs + theater reserve; FINLAND (173d ABCT OPCON FINDEFCOM) delay.
Threat: 7th CAA two-echelon — 72nd MRD assault from FAA URAL-CENTRAL via
DK16/Elk corridor; 71st MID exploitation from FAA SOKOL; 75th MB Iskander-M
TEL strikes on DIVARTY + RSS; 76th ADA SAM umbrella; 7th CHB attack avn.
Decision points DP-1 (2CR forward engagement), DP-2 (ATACMS authorization),
DP-3 (CAB JAAT), DP-4 (1CD theater reserve counterattack).
See scratch/ibl/joe-test/Exercise - DRAFT V Corps OPORD v2.docx for full text.""",
        source_path="scratch/ibl/joe-test/Exercise - DRAFT V Corps OPORD v2.docx",
    ),
    "OOB": _doc(
        "OCCASUS 7th CAA OOB",
        "oob",
        """OCCASUS 7th CAA OOB per OPORD §1.c.(1) and Annex B (Intelligence).
7th CAA HQ (Maj. Gen. Churin) directing two-echelon assault; assault echelon
72nd MRD (Col. Voronov, FAA URAL-CENTRAL + URAL-NORTH) with 721/722/723 MRB,
72nd FA Regiment (2S19), 72nd ADA Bn (Pantsir/2S6); 2nd echelon 71st MID
(Col. Kuzmin, FAA SOKOL) with 711/712/713 MIB. Corps-level reinforcement:
74th SP FA Bde (counterfire), 75th MB Iskander-M (Grisian Enclave), 76th AD Bde
(SA-11/SA-12 medium SAM), 7th CHB (Mi-35/Mi-24/Ka-52), 7th Eng Bde (Narew/Biebrza
bridging), 7th Log Bde, 750th SPF Bde (TRM interdiction), Cinereus EW Group
(R-330Zh Zhitel + Krasukha-4).""",
    ),
    "DonovianDoctrine": _doc(
        "OCCASUS 7th CAA — Doctrine Reference (Russian-pattern offensive, OPFOR / TC 7-100 model)",
        "doctrine",
        """# DonovianDoctrine — OCCASUS 7th CAA Offensive Reference

## §1. Scope and Doctrinal Anchor

This reference describes the OFFENSIVE doctrine of OCCASUS 7th CAA as a Russian-pattern combined-arms army (CAA). It is the doctrinal grounding for the downstream threat artifacts in the V Corps MITS 26 stack: Threat Overlay (the threat template + collection NAIs), Threat Model (organizational structure, tactics, warfighting functions), Threat Capability Statement (WfF capability statements + zone distribution), Threat-Description-Table (per-unit doctrinal description), Threat COA Statement (per-COA narrative + decisive engagement + branches), Situation Template (per-COA SITEMP graphic), Threat COAs PPTX (brief), Event Template (NAIs × indicators × DPs), HVTL (high-value-target derivation), and the per-COA SITEMP fanout. This reference does NOT pre-select which COA the writer should pick, does NOT pre-allocate specific OCCASUS formations against specific COA tasks, and does NOT name HPTs or NAIs — those are runtime decisions the writer derives in the Threat COA Statement / SITEMP from the OOB (cougaar.get_oob() / scenario.KG.unit_red) and the OPORD-declared scheme.

The tag is "DonovianDoctrine" because the schema-level dependency citations in app/app/templates/products/{threat_capability_statement,threat_model,threat_overlay,high_value_target_list,threat_description_table,situation_template}.py reference `get_project_doc(tag='DonovianDoctrine')`. The body content here is OCCASUS-7th-CAA-offensive doctrine for the V Corps scenario, NOT the Donovian-Estonia-defensive content baked into the IDX 26 scenario.py. The IDX 26 reference is DEFENSIVE (Donovian Corps holding Kõpu-Rõngu-Osula); this reference is OFFENSIVE (OCCASUS 7th CAA assaulting through the Elk corridor toward OBJ JACKSON). Same doctrinal lineage, opposite operational posture, different geographic anchor.

Doctrinal lineage is the union of TC 7-100.2 Chapter 3 (OPFOR offensive operations) and ATP 7-100.1 Chapter 5 (Russian Federation Ground Forces — Sukhoputnyye Voyska or SV — offensive operations), with TC 7-100.2 Chapter 2 (zone construct) and Chapter 4 (defensive operations) cited in cross-reference where the offensive operates against an enemy defense. The two publications use parallel but not identical vocabulary; this reference standardizes on the TC 7-100.2 zone construct (DISRUPTION / BATTLE / SUPPORT — applied from the attacker's perspective, i.e. the disruption zone is on the FRIENDLY side of the line of contact and the support zone is in OCCASUS rear) and uses the ATP 7-100.1 SV offensive vocabulary (ATTACK ZONE, IMMEDIATE OBJECTIVE, SUBSEQUENT OBJECTIVE) inline where the distinction matters. Mixing the two vocabularies in a single product is the signature of a non-doctrinal artifact — choose one and cross-reference the other (TC 7-100.2 ¶3-1 to ¶3-50; ATP 7-100.1 ¶5-1 to ¶5-12).

Application to this scenario: OCCASUS 7th CAA (Maj. Gen. Aleksei Churin) is deployed in the Forward Assembly Areas east of the Polish border — FAA URAL-CENTRAL (34UFE02035 64609, 53.82°N 22.55°E), FAA URAL-NORTH (34UEE99822 75691, 53.92°N 22.52°E), FAA SOKOL (34UFE13198 65979, 53.83°N 22.72°E) — preparing a two-echelon mechanized assault west toward OBJ JACKSON at BPTA (34UEE64392 54490, 53.735°N 21.976°E). The 7th CAA's IMMEDIATE OBJECTIVE is to penetrate the V Corps 2CR area defense in the SOUTH Sector via the Elk corridor (Narew / Biebrza / Pisa river network through DK16, the Drygały rail corridor, and the Nitki rail corridor) and seize OBJ JACKSON; the SUBSEQUENT OBJECTIVE is to exploit through the captured corridor with the 2nd-echelon 71st MID (Col. Kuzmin) committed from FAA SOKOL on order. The disruption zone (from OCCASUS's perspective) is the FRIENDLY V Corps SOUTH Sector and its forward security elements east of PL YELLOW; the battle zone is the OBJ JACKSON defensive band held by 1/2/3 SQDN 2CR + FAS/TF HELL + ROC-T + Italian WGX BG; the support zone is the V Corps rear (210th FA DIVARTY at 34UEE58250 56900; RSS Nodes X/Y/Z; 21st TSC FWD; 11th CAB; 1ABCT in the CENTER Sector covering Silmaris Gap; 1CD theater reserve at Grafenwöhr). This geography anchors every reference to "attack zone," "disruption zone," and "support zone" in the rest of this document, but does NOT pre-determine which OCCASUS subordinates execute which doctrinal role — that allocation is made in the Threat COA Statement and SITEMP from the OOB at runtime.

## §2. Offensive Forms Available to an OCCASUS Army Commander

A Russian-pattern combined-arms army commander chooses from a doctrinal menu of offensive forms. TC 7-100.2 ¶3-3 and ATP 7-100.1 ¶5-3 are explicit: a tactical group commander may employ multiple offensive forms simultaneously across the AOR (different forms on different axes within the same operation), and a single offensive plan may include subordinate units conducting fixing actions, attack actions, and exploitation in parallel. The Threat COA Statement writer selects from this menu based on scenario conditions; this section is the MENU, not the selection.

### §2.a Penetration (Frontal Attack with Concentrated Effort on a Narrow Sector)

Penetration (прорыв, ATP 7-100.1 ¶5-8; "attack against a defending enemy" / "penetration" in TC 7-100.2 ¶3-22 to ¶3-31) is an offensive form in which the attacker concentrates combat power, fires, and engineer effort on a narrow sector of the defender's main defensive zone to rupture it, then exploits the rupture by passing 2nd-echelon forces through to the depth. It is selected when:

- The defender has prepared positions that cannot be bypassed (a coherent area defense, deep obstacle belt, no exposed flank).
- The attacker enjoys local combat-power superiority (doctrinal SV preference: 5:1 or better in the breach sector) and can mass fires + engineer effort at the chosen breach point.
- The terrain canalizes movement into a small number of usable corridors (e.g. the Elk corridor / Narew-Biebrza-Pisa river system, the DK16 main road, the Drygały and Nitki rail corridors), forcing the attacker to break through one of them rather than maneuver around.
- The defender has committed his reserves elsewhere or is not yet positioned to counter the breach.
- A subsequent exploitation force (2nd echelon) is positioned and prepared to pass through the breach on order.

Key attributes (ATP 7-100.1 ¶5-8, ¶5-13 to ¶5-22; TC 7-100.2 ¶3-22 to ¶3-31): main-effort BDE on the breach axis at brigade frontages of 4-6 km in attack (narrower than defense — concentrated effort); supporting attacks at 6-10 km frontages to fix adjacent defenders and prevent lateral reinforcement; massed fires preparation (the FIRES PREPARATION period) for 30-60 minutes against the breach sector before H-Hour, suppressing enemy fires, ADA, C2, and obstacle-belt overwatch; engineer breach of obstacle belts in the lead BDE's attack zone; 2nd-echelon (exploitation force) held 15-30 km behind the line of departure for commitment on the order of the army commander once the breach is confirmed (4-8 hours of attack-time typical); recon-fires complex (ROK / RUK) continuously detecting and engaging enemy reserves attempting to reinforce the breach; air-assault force (if available) used to seize a key terrain feature in the defender's rear to fix the defense's withdrawal and isolate the breach sector.

The first echelon RUPTURES the defender's main defensive position. The second echelon EXPLOITS through the rupture and drives to the depth (ATP 7-100.1 ¶5-8, ¶5-22).

### §2.b Envelopment (Flank Attack with Maneuver Around a Defender's Position)

Envelopment (охват, ATP 7-100.1 ¶5-9; "envelopment" in TC 7-100.2 ¶3-32 to ¶3-39) is an offensive form in which the attacker fixes the defender's front and maneuvers around one or both flanks to attack the defender's flank or rear, destroying him in place or forcing him to fight in two directions. It is selected when:

- The defender's flanks are open or weakly held (a doctrinal Russian assumption is OPEN FLANKS — the defender's BANDS of strongpoints have gaps that the attacker can exploit, ATP 7-100.1 ¶6-1).
- The terrain on a flank offers a maneuver corridor the defender has not adequately covered (e.g. the Drygały rail corridor as a southern envelopment of 2CR's main defense at OBJ JACKSON; the Nitki rail corridor as a northern envelopment; a combination of both as a double envelopment).
- The attacker possesses a mobility advantage that enables the flanking force to reach the flank before the defender can reposition reserves.
- A fixing force can pin the defender's front long enough for the flanking force to complete the maneuver.
- Air assault or vertical envelopment is available to seize key terrain in the defender's depth that the flanking force will then link up with.

Key attributes (ATP 7-100.1 ¶5-9, ¶5-23 to ¶5-32; TC 7-100.2 ¶3-32 to ¶3-39): a FIXING FORCE (typically 1/3 of attacking strength) holds the defender's front on a wide frontage with fires and limited assault to prevent withdrawal or lateral reinforcement; an ENVELOPING FORCE (typically 2/3 of strength) maneuvers around one or both flanks via the chosen maneuver corridor; the EXPLOITATION FORCE (2nd echelon, if separate from the enveloping force) passes through the envelopment to deepen the penetration; the recon-fires complex prioritizes detection of defender reserves repositioning to counter the envelopment so the BrAG and corps long-range fires can interdict them en route.

### §2.c Turning Movement (Maneuver to Force the Defender to Abandon Position)

Turning movement (TC 7-100.2 ¶3-40 to ¶3-46) is an offensive form in which the attacker passes around the defender's main position to attack a key location in his rear — a critical LOC, a sustainment base, a higher CP — forcing the defender to abandon his prepared position and fight on terms favorable to the attacker (a meeting engagement in the open, defender's prepared works left behind unused). It is selected when:

- A geographically decisive target in the defender's rear can be reached without fighting through his main defensive position (e.g. interdiction of the V Corps TRM corridor / Route 58 via SPF + 750th SPF Bde infiltration; threat to RSS Node X / Y / Z sustainment triplet; threat to the 21st TSC FWD at Elblag).
- The defender's main position is too strong to penetrate or envelop within the available time.
- The attacker possesses operational mobility (organic air, long-range fires, SPF) to reach the rear target.
- The defender's withdrawal route options can be controlled or interdicted to ensure he is forced into the attacker's chosen ground.

Key attributes: a holding force (sometimes only fires and reconnaissance) maintains contact with the defender's front to fix his attention; the main effort moves on the rear target via the chosen corridor (often SPF + air-assault + long-range fires combination); the OBJECTIVE in a turning movement is NOT to destroy the defender at his prepared position but to force him to fight elsewhere.

### §2.d Frontal Attack (Distributed Pressure Across a Wide Frontage)

Frontal attack (фронтальный удар, ATP 7-100.1 ¶5-10; TC 7-100.2 ¶3-47 to ¶3-50) is an offensive form in which the attacker assaults the defender across his full frontage simultaneously to overwhelm by mass or to fix the defender while a decisive effort is conducted elsewhere. It is the LEAST PREFERRED form (Russian doctrine considers it the most costly and least effective — direct casualty exchange, no maneuver advantage) and is selected only when:

- All flanking options are foreclosed (defender's flanks anchored on terrain, friendly forces, or international borders the attacker will not cross).
- The attacker possesses overwhelming combat-power superiority that makes frontal mass viable.
- Speed is more important than economy of force (e.g. a spoiling attack to disrupt enemy preparations before he can complete a counterattack).
- The frontal attack is a DECEPTION serving a decisive effort elsewhere (commonly the fixing force in an envelopment).

Frontal attack is the form to use as a DECEPTION ELEMENT in a multi-axis scheme — it ties the defender to his prepared position while the actual decisive effort develops elsewhere (TC 7-100.2 ¶3-50; ATP 7-100.1 ¶5-10).

### §2.e Spoiling Attack (Offensive Action During Friendly Defensive Operations)

Spoiling attack (TC 7-100.2 ¶3-7) is an offensive action conducted during what is doctrinally a defensive operation, with the aim of disrupting the enemy's attack preparations before he can launch. It is selected when:

- Reliable indicators of enemy attack preparation exist (forward assembly, fires registration, fires-preparation displacement, recon focus).
- The OPFOR is otherwise on the operational defensive but has uncommitted forces (SPF, long-range fires, attack aviation, available BDE) that can be committed to the spoiling effect.
- The window before enemy launch is sufficient to plan and execute (typically 24-72 hours).

For the V Corps scenario this form is RELEVANT IN REVERSE — the threat analyst must consider whether V Corps might conduct a spoiling attack against OCCASUS preparations in the FAA URAL-CENTRAL / URAL-NORTH / SOKOL belt; the Threat COA Statement writer evaluates whether OCCASUS will pre-empt by accelerating H-Hour or by repositioning forces to absorb a friendly spoiling attack.

### §2.f Exploitation and Pursuit (Post-Breakthrough Phases)

Exploitation (TC 7-100.2 ¶3-51 to ¶3-56; ATP 7-100.1 ¶5-11) is the offensive phase following a successful penetration or envelopment, in which the attacker pushes the 2nd-echelon force (often an OPERATIONAL MANEUVER GROUP — OMG; in 7th CAA the analogue is the 71st MID exploitation force) through the breach to attack the defender's depth, destroy his reserves before they can reset, disrupt his C2 and logistics, and seize operational-depth objectives. Pursuit (TC 7-100.2 ¶3-57 to ¶3-62; ATP 7-100.1 ¶5-12) is the destruction of a defender attempting to withdraw — direct pressure on the retreating force plus encircling-force maneuver to cut him off.

Key attributes of exploitation (Forward Detachment Concept, ATP 7-100.1 ¶5-50 to ¶5-54): the EXPLOITATION FORCE moves rapidly and avoids decisive engagement with bypassed pockets — strength preserved for the depth objective. The exploitation force commander is given mission-type orders (objective, intent, end state) and operates with mission command latitude beyond the army commander's direct control during execution. A FORWARD DETACHMENT (FED — a reinforced battalion or BDE-minus, typically tank-heavy, with organic engineer, AD, and fires) moves AHEAD of the main exploitation force to seize critical objectives — bridges, defiles, road junctions, depots — before the defender can react. Bypass criteria are explicit: pockets smaller than a defending company are bypassed; larger pockets are reported and either fixed by follow-on forces or engaged by the next echelon.

## §3. Zone Construct (TC 7-100.2 Vocabulary, Applied from the Attacker's Perspective)

The OPFOR zone construct organizes the AOR into three named zones separated by named lines. For the OFFENSIVE the construct is applied from the attacker's perspective — the DISRUPTION ZONE is the enemy's near rear that the attacker disaggregates before his main forces close on the defender; the BATTLE ZONE is where the decisive engagement occurs (penetration of the defender's main defensive position); the SUPPORT ZONE is the attacker's own rear — assembly areas, sustainment, long-range fires, reserves, C2 nodes (TC 7-100.2 ¶2-36 to ¶2-46, ¶3-4). The construct is conceptual and may be linear or non-linear, contiguous or non-contiguous, layered between echelons.

### §3.a Disruption Zone (Attacker's Forward Effort Against the Defender's Combat System)

Purpose: The portion of the defender's AOR where the attacker conducts DISRUPTION OPERATIONS to begin DISAGGREGATION of the defender's combat system. The disruption zone is FORWARD of the OPFOR line of departure and extends through the defender's security zone into his battle zone. Successful disruption-zone actions create the WINDOW OF OPPORTUNITY exploited in the battle zone (TC 7-100.2 ¶2-38, ¶3-4, ¶3-7).

Depth: From the line of departure forward to the limit of attacker reconnaissance / long-range fires (LOR for the attacker). May be layered between echelons — the army's disruption zone extends to corps long-range fires range; the division and brigade have shallower disruption zones.

Functional forces (templated by capability, not by name): SPF (Special Purpose Forces) infiltrated into the defender's rear BEFORE H-Hour to attack C2, ISR, logistics, and fires (in V Corps scenario the 750th SEPARATE SPF BDE is positioned for this — interdicting the V Corps TRM corridor / Route 58 and ambushing sustainment convoys); long-range reconnaissance patrols and UAV company forward to find the defender's reserves, fires, and CP locations for targeting by the recon-fires complex; long-range fires (75th MISSILE BRIGADE Iskander-M; 74th SP FA BDE; corps MRL formations) striking C2, ISR, ADA, fires, and sustainment in the defender's depth; attack aviation (7th COMBAT HELICOPTER BDE Mi-35 / Mi-24 / Ka-52) hunting reserves and key C2 in the disruption zone before they can reposition; EW arrays (Cinereus EW Group: R-330Zh Zhitel + Krasukha-4) suppressing the defender's C2 nets, jamming his fires-direction radios, and degrading his RISTA (TC 7-100.2 ¶3-7, ¶3-9, ¶3-11; ATP 7-100.1 ¶5-4, ¶5-5, ¶5-6; FMSO Russian Recon-Fire-Strike 2021).

Functions (TC 7-100.2 ¶3-7 to ¶3-11): attack lucrative targets in the defender's depth — fires complex, C2, ISR, sustainment, reserves; deceive the defender as to the location and timing of the main attack (deception positions, false fires-registration, decoy radio traffic via Cinereus EW Group); detect defender main groupings and key elements; force defender to reveal reserves and timing; canalize the defender into anticipated kill zones for fixing fires and supporting attacks; maintain contact with the defender to enable continuous recon-fires and set conditions for the main attack.

For THIS scenario, the OCCASUS disruption zone reaches west from the line of departure (the OCCASUS-friendly side of the Elk corridor) into the V Corps SOUTH Sector — through 2CR's forward security elements at EA-1 (Drygały), EA-2 (Nitki), EA-3 (Klusy / DK16), and into the V Corps depth (210th FA DIVARTY at 34UEE58250 56900; RSS Nodes X / Y / Z; 11th CAB; 21st TSC FWD; and as far as the 1ABCT CENTER Sector covering Silmaris Gap and the 1CD theater reserve at Grafenwöhr). The specific OCCASUS units allocated to disruption-zone roles are derived in the Threat COA Statement from the OOB (75th MISSILE BRIGADE, 74th SP FA BDE, 7th CHB, 750th SPF BDE, 71st MID recon assets if committed forward) — this reference describes the role types, not the unit ids.

### §3.b Battle Zone (Where Decisive Engagement Occurs)

Purpose: The portion of the AOR where the OPFOR expects to conduct DECISIVE OFFENSIVE ACTION — typically the breach of the defender's main defensive position, the destruction of the defender's main force, and the link-up of the exploitation force to drive into the defender's depth (TC 7-100.2 ¶2-42, ¶3-4).

Depth: Between the line of departure (rear) and the limit of advance for the first echelon (forward). May shift forward as the attack succeeds.

Functional forces (templated by capability): first-echelon ATTACK FORMATIONS (the assault BDEs — in the V Corps scenario the 721 MRB, 722 MRB, 723 MRB of the 72nd MRD) breaking through the defender's main defensive position via the chosen corridor; supporting fires (72nd FA REGIMENT, 74th SP FA BDE) massing on the breach sector during the fires preparation and on the defender's reserves during the assault; engineer breach assets (7th ENGINEER BDE with TMM bridging and UR-77 minefield breaching) clearing obstacle belts and bridging the Narew / Biebrza / Pisa river crossings; 2nd-echelon EXPLOITATION FORCE (71st MID at FAA SOKOL, with 711 MIB / 712 MIB / 713 MIB) held 15-30 km behind the line of departure for commitment on order once the breach is confirmed; AD coverage over the assault formations and over the breach corridor (72nd ADA BN Pantsir / 2S6 forward; 76th AD BRIGADE SA-11 / SA-12 mid-range SAM umbrella overhead). FORWARD DETACHMENTS (tank-heavy reinforced battalions, typically detached from the assault BDEs) move ahead of the main effort to seize key terrain in the defender's near depth — bridges, defiles, road junctions, CPs — before the defender can react (ATP 7-100.1 ¶5-50 to ¶5-54).

Functions (TC 7-100.2 ¶2-42, ¶3-4): destroy or break through the defender's main defensive position; pass the exploitation force through the breach; destroy the defender's reserves attempting to seal the breach; seize the breach-corridor depth necessary for the exploitation force's launch.

For THIS scenario, the battle zone is the 2CR area defense at OBJ JACKSON (34UEE64392 54490) — the 1st SQDN at EA-3 (Klusy) forward of DK16, the 2nd SQDN at EA-2 (Nitki), the 3rd SQDN at EA-1 (Drygały), with FAS / TF HELL providing organic fires and ROC-T providing deep counterfire from ATK PSN VENUS in Lithuania. The Italian WGX BG (OPCON 2CR) holds the southern frontage along PL YELLOW. The battle zone extends west to PL GOLD (the security area) and PL BLUE (battle handover) — once the breach reaches PL BLUE, the 2nd-echelon 71st MID is committed from FAA SOKOL for exploitation. Specific OCCASUS brigade assignments to attack roles are derived in the Threat COA Statement at runtime.

### §3.c Support Zone (Attacker's Own Rear — Assembly, Sustainment, Reserves, Fires, C2)

Purpose: The attacker's own rear, designed to be free of significant enemy action — for ASSEMBLY of the attack formations, SUSTAINMENT (POL, ammo, maintenance, medical), reserves staging, indirect-fire weapons positioning, and C2 (TC 7-100.2 ¶2-46, ¶3-4).

Depth: Behind the line of departure, extending to the operational rear. May be layered between echelons.

Functional forces (templated by capability): Corps HQ (7TH CAA HQ at 34UFE30000 80000 east of FAA SOKOL); the corps INTEGRATED FIRES COMMAND (IFC) C2 node for corps long-range fires (TC 7-100.2 ¶4-100; tied to 75th MISSILE BRIGADE Iskander-M and 74th SP FA BDE counterfire); corps signal formations; corps ADA formations (76th AD BRIGADE) providing rear-area SAM umbrella over the FAAs and the C2 nodes; corps combat aviation (7th COMBAT HELICOPTER BDE FARP); sustainment brigades (7th LOGISTICS BDE with 3-day combat load forward, plus the 7th ENGINEER BDE staging bridging assets); the EXPLOITATION FORCE 71st MID HQ at 34UFE13198 65979 with its 711 / 712 / 713 MIBs in dispersed AAs ringing FAA SOKOL. The IFC CP and corps main CP are heavily camouflaged and protected by C3D against V Corps standoff RISTA and ATACMS precision attack (TC 7-100.2 ¶2-46; ATP 7-100.1 ¶5-53).

Functions (TC 7-100.2 ¶2-46): assemble the attack formations in the FAAs before H-Hour; sustain the attack with continuous POL / ammo / maintenance / casualty evacuation push forward; provide C2 from the main CP and the IFC CP; mass corps long-range fires on demand against deep targets in the V Corps SUPPORT ZONE (210th FA, RSS Nodes, 11th CAB FARP, 1CD reserve assembly); stage and commit reserves on the army commander's order; protect against V Corps deep ATACMS strikes (76th AD BRIGADE umbrella + dispersion + C3D); protect against V Corps SOF and air-assault threat (anti-air-assault planning and corps SPF screen).

For THIS scenario, the OCCASUS support zone extends east of the line of departure (the Polish-eastern-border region between Elk and FAA SOKOL) through FAA URAL-CENTRAL, FAA URAL-NORTH, and FAA SOKOL into the corps rear east of 22.7°E. The corps HQ at 34UFE30000 80000 sits ~30 km east of FAA SOKOL; the 75th MISSILE BRIGADE TEL forward firing positions are at 34UFE35000 85000 (~50 km east of FAA SOKOL, within Iskander-M range of every V Corps SOUTH Sector target including ATK PSN VENUS in Lithuania). Specific OCCASUS support-zone OOB ids are derived at runtime.

### §3.d Cross-Reference to ATP 7-100.1 SV Offensive Vocabulary

For analysts working from ATP 7-100.1 (Russian SV) offensive terminology rather than TC 7-100.2 (OPFOR) terminology, the mapping is:

- TC disruption zone ≈ SV ATTACK ZONE (FORWARD portion — the zone forward of the line of departure into which the attacker's recon and long-range fires reach to attack defender depth before H-Hour) plus the SV objective area (defender main defensive position) once the assault begins. ATP 7-100.1 ¶5-13 to ¶5-15.
- TC battle zone ≈ SV IMMEDIATE OBJECTIVE area (the defender's main defensive position the assault formations must penetrate) plus the breach-corridor approaches. ATP 7-100.1 ¶5-16, ¶5-22.
- TC support zone ≈ SV ATTACK ZONE (REAR portion — the attacker's own assembly area, sustainment, fires positions, reserves AA) plus the SV depth-area where the SUBSEQUENT OBJECTIVE (exploitation-phase objective) is referenced from. ATP 7-100.1 ¶5-12, ¶5-50 to ¶5-54.

Either vocabulary is doctrinally valid. Pick ONE and cross-reference the other inline only where the distinction adds analytical clarity.

## §4. Offensive Tenets

Russian offensive doctrine is built on a small number of tenets that constrain every COA and SITEMP. Each is universal — it applies in every form of offense — and each carries observable signatures the analyst can use to identify it in the OE. Fourteen tenets follow.

### §4.a Concentration of Effort on a Narrow Front

The attacker MASSES combat power on a narrow sector at the chosen breach point — 5:1 doctrinal ratio in the breach BDE's attack zone is the SV planning factor (ATP 7-100.1 ¶5-13). Supporting attacks on adjacent sectors fix the defender and prevent lateral reinforcement at 1:1 or worse — they are not intended to penetrate; they are intended to PIN. The Western pattern of equal distribution across a wide frontage is NOT Russian offensive doctrine.

### §4.b Two-Echelon Scheme of Maneuver

Offensive operations are two-echelon at every echelon from BDE upward. First echelon (typically 2/3 of combat power for an army-level offensive) executes the assault and penetration. Second echelon (typically 1/3) is held back for EXPLOITATION through the breach once the first echelon has ruptured the defender's main defensive position. The 2nd-echelon commitment is the ARMY COMMANDER's decision — it is a major DP. Echelons have explicit missions distinguishing them from reserves (ATP 7-100.1 ¶5-7, ¶5-18 to ¶5-21).

The COMBINED-ARMS RESERVE, ANTITANK RESERVE, ANTI-AIR-ASSAULT RESERVE, MOBILE OBSTACLE DETACHMENT (POZ), ENGINEER RESERVE, and (when needed) CHEMICAL-DEFENSE RESERVE are SEPARATE from the 2nd echelon. The reserves are positioned to react to specific contingencies (counterattack against an exposed flank, defender counterattack into the breach, defender air-assault into the support zone); the 2nd echelon is positioned to EXPLOIT a planned success.

### §4.c Recon-Fires Complex Driving the Attack (ROK / RUK)

The sensor-to-shooter loop is institutionalized AND IS THE ATTACK'S PRIMARY MECHANISM in modern Russian offensive doctrine. The Reconnaissance Strike Complex (RUK) at operational-strategic level integrates long-range precision weapons (Iskander-M, Tornado-S MRL, ground-launched cruise missiles) with real-time intel via a fused fire-direction centre (FMSO Russian Recon-Fire-Strike 2021). The Reconnaissance Fire Complex (ROK) is the tactical analogue at brigade and below. Strelets KRUS and Bylina automated C2 tie the UAV company, artillery recon, EW recon, and human OPs to the BrAG and battalion mortars. Detection-to-engagement cycle is 8-12 minutes per Shoigu, with kill-chain-level cycles claimed at 10 seconds at the lowest levels.

The attack ENABLES the recon-fires complex (forward movement places sensors in range of more targets); the recon-fires complex ENABLES the attack (continuously stripping defender fires, ADA, C2, and reserves before they can affect the assault). The two are INSEPARABLE — Russian offensive operations without a functioning recon-fires complex revert to attritional WW1-style frontal assault, which doctrine explicitly rejects (TC 7-100.2 ¶3-9; ATP 7-100.1 ¶5-4; FMSO 2021).

### §4.d Massed Indirect Fires (Two Periods in Offense)

Massed indirect fires (artillery, MRL, mortar) are the principal destruction mechanism in offense as in defense. Two doctrinal periods (ATP 7-100.1 ¶5-15, ¶5-19):

- FIRES PREPARATION (огневая подготовка) — typically 30-60 minutes before H-Hour, against the defender's forward positions, fires, ADA, C2, obstacle-belt overwatch, and identified reserves. Includes COUNTER-PREPARATION FIRES against any defender preparations the recon-fires complex detected. The fires-preparation density is doctrinal — full BrAG and corps-FA-BDE contribution on the breach sector, with thermobaric munitions (TOS-1 / TOS-2) against fortified positions and CBPs.
- FIRES SUPPORT OF THE ATTACK (огневая поддержка наступления) — continuous from H-Hour through the assault, supporting the maneuver formations as they close, breach, and exploit. Subdivides into fires support of the breach (suppressing defender reaction to the lead BDE's assault) and fires support of the exploitation (interdicting defender reserves attempting to reinforce against the 2nd-echelon commitment).

When the attack is followed by a defender counterattack into the breach, two additional periods open: fires preparation for repulsing the counterattack and fires support of the counterattack-repulse. SV artillery types of fire include fire on individual target, concentrated fire, standing barrage, rolling barrage (offensive variant — leads the assault formations forward), successive fire concentration, and massed fire (ATP 7-100.1 ¶6-65 — same fire types apply in offense and defense).

### §4.e Engineer Effort to Enable the Breach

Engineer effort enables the offensive at the same scale as it enables the defense. The 7th ENGINEER BDE conducts forward obstacle effort in the OCCASUS approach march (route clearance, bridging at the Narew / Biebrza / Pisa crossings, repair of the DK16 main road and the Drygały / Nitki rail corridors), then conducts ASSAULT BREACH of the V Corps obstacle belts at the chosen breach sector (UR-77 minefield breaching, TMM bridging across anti-tank ditches, mechanical breach of dragon's teeth and concrete obstacles). The POZ (Mobile Obstacle Detachment) is retained for the OFFENSIVE COUNTERMINELAYING role — laying mines on defender counterattack approaches and on flanks of the breach corridor to canalize defender reaction (ATP 7-100.1 ¶5-29, ¶5-34). The engineer reserve is held to strengthen the breach when defender obstacle effort proves heavier than expected.

For THIS scenario the 7th ENGINEER BDE is the corps-level engineer formation; it includes combat bridging at the Narew / Biebrza crossings (PMP pontoon bridge segments, TMM scissor bridges) and minefield breach (UR-77). The 72nd MRD has organic engineer assets for tactical-level obstacle clearance within the BDE attack zone.

### §4.f Integrated Air Defense Forward With the Attack

The attacker MUST cover the assault formations + the breach corridor + the corps support zone with continuous AD. Failure to do so concedes the breach to enemy attack aviation and to enemy CAS, both of which historically devastate Russian assault formations in a Western-RISTA-rich AOR (ATP 7-100.1 ¶5-25, ¶6-25; Watling 2024). The 72nd ADA BN provides organic AD (Tunguska AAA, Strela-10, Igla MANPADS) covering the lead BDEs as they assault; the 76th AD BRIGADE provides mid-range SA-11 / SA-12 SAM umbrella over the FAAs, the corps HQ, the 75th MISSILE BRIGADE forward firing positions, and the breach corridor.

The V Corps 11th CAB attack aviation and the V Corps F-16 / F-35 CAS sortie generation are the primary V Corps AD-suppression targets in the OCCASUS scheme — the recon-fires complex prioritizes detection of CAB FARPs and identification of V Corps fixed-wing CAS push timing; the 75th MISSILE BRIGADE and 7th COMBAT HELICOPTER BDE prioritize attacking those C2 / FARP / IADS targets.

### §4.g Forward Detachment Concept (Tactical Depth Seizure)

The Forward Detachment (FED — передовой отряд, ATP 7-100.1 ¶5-50 to ¶5-54) is a reinforced tank battalion or BDE-minus (organic tank or motor rifle plus organic engineer, AD, fires, and recon) detached from the lead BDE and given a MISSION TO MOVE AHEAD OF THE MAIN FORCE to seize critical objectives — bridges, defiles, road junctions, depots, key terrain — before the defender can react or reinforce. The FED operates with mission command latitude; the army commander gives it an objective, an intent, and an end state, and lets the FED commander execute. The FED is sized to FIGHT for the objective if necessary but to MOVE if the defender is absent or weak.

In THIS scenario, the doctrinal FED roles are: (a) seize and hold the Narew / Biebrza / Pisa bridge crossings in the Elk corridor before V Corps engineer demolition; (b) seize the DK16 main road junctions east of OBJ JACKSON; (c) seize the Klusy rail station and the DK16/rail intersection at EA-3; (d) penetrate to OBJ JACKSON's eastern outskirts in advance of the main 721 MRB body. The specific OCCASUS FED detachment is derived in the Threat COA Statement — typically a tank-heavy company team or battalion task force from the lead BDE, with organic engineer, AD, and fires attachments.

### §4.h Operational Maneuver Group (OMG) Equivalent — Exploitation Force

The Operational Maneuver Group (OMG — оперативная маневренная группа) is the army-level analogue of the FED at the operational scale. The OMG is a reinforced division or BDE+ detached from the army's 2nd echelon, given a deep operational objective in the defender's rear (a sustainment hub, a higher CP, a piece of operationally decisive terrain), and committed through the breach to OPERATE INDEPENDENTLY in the defender's depth — bypassing pockets, disrupting C2, attacking sustainment, seizing depth objectives, fixing defender reserves so the main 2nd echelon can exploit (Grau/Bartles 2021; FMSO 2018 OMG monograph; TC 7-100.2 ¶3-51 to ¶3-56).

In THIS scenario, the OMG analogue is the 71st MID (Col. Kuzmin) at FAA SOKOL with the 711 MIB, 712 MIB, and 713 MIB. The 71st MID is committed on the army commander's order once the 72nd MRD has confirmed breach of the 2CR main defensive position (typically 4-8 hours of attack time). The 71st MID's doctrinal deep objective is the seizure of the V Corps TRM corridor (Route 58) and the interdiction of the V Corps 1ABCT CENTER Sector lateral reinforcement of 2CR — a deep operational effect that prevents V Corps from sealing the breach with the CENTER Sector reserve.

### §4.i Maskirovka Throughout the Offense

Maskirovka is mandatory at every level of the offense and is operationally co-equal with fires and maneuver. Four principles: activity (continuous), conviction or plausibility (must be credible), continuity, variety. Offensive maskirovka includes pre-H-Hour deception (decoy assemblies in alternate FAAs, false fires-registration on non-breach sectors, dummy obstacle-clearing operations on non-breach axes, simulated radio traffic on inactive nets), H-Hour deception (frontal-attack supporting effort that masks the actual envelopment direction, false air-assault feints, decoy electronic emissions to draw V Corps recon away from the actual breach), and post-breach deception (decoy 2nd-echelon commitment timing, false withdrawal preparations to draw a V Corps counterattack into a fires sack). Russia treats maskirovka as a permanent qualitative advantage (TC 7-100.2 ¶3-12, ¶4-36, ¶4-45, ¶4-130).

For THIS scenario, the COA-4 ("Air-Assault Diversionary Scheme — Most Deceptive") is the canonical maskirovka-heavy COA pattern: feigned air assault on the Silmaris Gap (CENTER Sector) to draw the 1CD theater reserve and the 1ABCT covering force out of position, while the actual main effort develops in the Elk corridor against 2CR.

### §4.j Electronic Warfare Integration Forward of the Attack

EW is co-equal with fires and ISR in offense. The Cinereus EW Group (R-330Zh Zhitel + Krasukha-4) suppresses V Corps tactical and operational C2 from H-Hour — degrades the 2CR S-2 to V Corps G-2 reporting chain, jams the 11th CAB attack-aviation tactical net, blinds V Corps counter-battery radars at 210th FA, and protects OCCASUS assault formations from V Corps proximity-fused and signal-detonated munitions. EW arrays are integrated FORWARD with the lead BDEs; the EW battalion organic to the 72nd MRD provides BDE-level EW; corps EW assets (75th MISSILE BRIGADE C2 EW protection, Cinereus EW Group depth jamming) provide army-level coverage.

EW aims (ATP 7-100.1 ¶5-31, ¶6-31; Jamestown EW monograph 2024): degrade V Corps C2 of forces and weapons; reduce V Corps ISR and weapons effectiveness; protect OCCASUS C2 and protect assault formations from precision attack; defeat V Corps precision-strike (jam GPS guidance on JDAM / Excalibur / GMLRS; spoof Link-16 / SADL / JREAP).

### §4.k Anti-Airborne and Anti-Air-Assault Defense in the Support Zone

The OCCASUS support zone is a high-value target for V Corps air-assault and SOF action — the 11th CAB Black Hawk lift capability, the 173d ABCT (OPCON FINDEFCOM) Finland-axis airborne option, and the V Corps RANGER capability all threaten OCCASUS C2, fires, and sustainment in the support zone. OCCASUS doctrine fields ANTI-AIR-ASSAULT RESERVES at army and corps level (typically a motorized rifle company from the 2nd echelon, positioned with rapid-access routes to likely DZs and LZs around FAA SOKOL, the 75th MISSILE BRIGADE TEL positions, and the corps HQ) with integrated AD overhead and integrated obstacles at likely landing zones (ATP 7-100.1 ¶5-26, ¶6-28, ¶6-60).

### §4.l Systems Warfare — Disaggregate the Defender's Combat System Forward of the Assault

The offense is built on systems warfare: attacking specific COMPONENTS or SUBSYSTEMS of the defender's combat system FORWARD OF H-HOUR in order to disaggregate the "system of systems" before the assault formations close on the defender's main defensive position. The disruption-zone effort begins the disaggregation (SPF + long-range fires + recon-fires complex strip V Corps C2, ISR, fires, ADA, sustainment); when the lead BDEs enter the battle zone, the defender is no longer synchronized — V Corps's recon-fires loop is broken, V Corps's fires lack mass because counter-battery radars are blinded, V Corps's CAS is delayed because the 11th CAB FARP is suppressed, V Corps's 2CR forward defense is fighting unsupported. The assault then exploits the disaggregated defense rather than fighting through a coherent one (TC 7-100.2 ¶3-7, ¶3-9, ¶4-21 to ¶4-23, ¶4-44; ATP 7-100.1 ¶5-4).

### §4.m Lateral Reinforcement and Multi-Axis Pressure

OCCASUS doctrine assumes the defender will attempt LATERAL REINFORCEMENT of the threatened axis (V Corps shifts 1ABCT CENTER Sector reserve to reinforce 2CR; 1CD theater reserve commits to seal the breach). Russian offensive doctrine PRE-EMPTS LATERAL REINFORCEMENT by (a) supporting attacks on adjacent sectors that fix lateral defenders so they cannot shift, (b) long-range fires interdicting movement on lateral routes (75th MB Iskander-M against 1ABCT assembly areas; 74th SP FA BDE counterfire interdicting 11th CAB FARP relocation; corps MRL against road and rail movement), (c) air-assault or SPF action in the defender's depth to fix higher reserves (the 7th CHB air-assault feint against the Silmaris Gap in COA-4 is doctrinal lateral-reinforcement-suppression), and (d) deception (the OCCASUS scheme makes V Corps uncertain which axis is the main effort, delaying his reserve-commitment decision).

For THIS scenario, the OCCASUS planning factor is that V Corps has roughly 90 minutes of warning to commit the 1ABCT reserve and 4-6 hours to commit 1CD; the offensive scheme must close on the breach faster than that or else fix those reserves with supporting effort.

### §4.n Transition From Offense to Hasty Defense at Culmination

Every offensive operation culminates — when the attacker's combat power has been expended sufficient that further attack risks decisive defeat. Russian doctrine treats CULMINATION as a positive decision, not a failure: at culmination the army commander TRANSITIONS the attack formations to a hasty defense on the seized objective, consolidates gains, accepts logistics push, and prepares for either (a) defender counterattack repulse, (b) resumption of the offense from the new line, or (c) operational pause for further build-up (TC 7-100.2 ¶3-63 to ¶3-65; ATP 7-100.1 ¶5-12, ¶6-1).

In THIS scenario, the OCCASUS culmination threshold is the seizure of OBJ JACKSON plus 40-50% attrition of the 72nd MRD assault echelon — at that point the army commander either commits the 71st MID for continued exploitation OR transitions to hasty defense on OBJ JACKSON to await reinforcement. The V Corps counterattack scheme (DP-4 1CD theater reserve commit) is designed to engage OCCASUS at culmination.

## §5. Capability Descriptions by Functional Bucket (SV Vocabulary, Cross-walked to US WFFs)

The seven US warfighting functions do not map one-to-one onto Russian doctrine. The SV organizes around combat support (fires, AD, AT, engineer, EW, ISR), troop control (C2 plus reconnaissance), logistics rear support, and morale-psychological work. The descriptions below are the operational-tactical behaviors of OCCASUS 7th CAA in each functional bucket during the offensive phase.

### §5.a Command and Control (Troop Control)

Centralized planning, centralized intent, mission command latitude within the FED and OMG roles, more decentralized execution at lower echelons than the Cold War pattern. Strelets KRUS and Bylina automated C2 tie the brigade UAV company to the BrAG and to battalion mortars in a sensor-to-shooter loop. The standing combat order is the primary product. Brigade fields primary plus reserve control points throughout the depth; the CP locates out of enemy direct-fire range but with view of main attacker axes (own forward) and main defender reserve approaches (ATP 7-100.1 ¶5-53, ¶6-53). Russian commanders retain stronger top-down control than Western counterparts at the higher echelons; FED and OMG commanders operate with explicit mission command latitude during execution.

Signature observables: wired comms in the FAAs and rear support zone; radio plus cellular when assault is joined; automated message traffic via Strelets terminals; multiple CP nodes (primary plus reserve); FED detachment radio nets active forward of the main BDE bodies; corps HQ relocation prior to H-Hour to a forward TAC CP for the duration of the assault.

### §5.b Movement and Maneuver

Two-echelon attack scheme is universal. First echelon (the 72nd MRD assault echelon with 721 / 722 / 723 MRB) executes the penetration and seizes the immediate objective. Second echelon (the 71st MID exploitation force with 711 / 712 / 713 MIB) is held back and committed by the army commander on order for exploitation to the depth. Bronegruppa (armored group) of dismounted IFV / APC vehicles forms a direct-fire maneuver reserve under deputy commander. FORWARD DETACHMENTS (FEDs) move ahead of the main attack to seize critical objectives. The COMBINED-ARMS RESERVE is held at army level for unexpected requirements (defender counterattack against a flank, sudden opportunity to commit on an unanticipated axis).

In multi-axis schemes (COA-2 — Multi-Axis Breakthrough; COA-3 — Deep Strike and Massed Exploitation; COA-4 — Air-Assault Diversionary Scheme), the supporting axes pin defender forces on adjacent sectors while the main axis penetrates. Blocking (blokirovanie) is used to fix the defender for fires destruction — maneuver fixes, fires destroy, the recon-fires complex targets (RAND PE231 2017; TC 7-100.2 ¶3-7, ¶3-9).

Signature observables: assembly in dispersed AAs around the FAAs (FAA URAL-CENTRAL, URAL-NORTH, SOKOL) with sub-units dispersed by 1-3 km against precision fires; movement to the line of departure in BDE columns on multiple parallel routes; FED detachments forward of the main BDE bodies in the line-of-departure crossing; 71st MID 2nd echelon held in dispersed AAs around FAA SOKOL; bronegruppa with the BDE deputy commander in the immediate rear of the assault echelon.

### §5.c Intelligence

Reconnaissance is doctrinally inseparable from fires — the ROK at brigade integrates organic UAV company, Strelets-linked artillery recon, EW collection, engineer recon, signals recon, and COMINT / SIGINT. The SV expects detection-to-engagement under 10 minutes. Recon priority targets in offense are: defender RESERVES (V Corps 1ABCT CENTER reserve assembly, 1CD theater reserve movement indicators from Grafenwöhr); defender FIRES (210th FA DIVARTY PAA at 34UEE58250 56900, FAS / TF HELL organic 2CR fires, ROC-T ATACMS positions); defender ADA (V Corps Patriot / NASAMS / SHORAD positions); defender C2 (V Corps Main CP at Wiesbaden, V Corps TAC FWD at Elblag from 15 AUG, 2CR HQ at 34UEE62500 52500, MND-NE CP); defender LOGISTICS (RSS Nodes X / Y / Z at 34UEE60000 53500 / 34UEE60000 51000 / 34UEE58800 56200, 21st TSC FWD); defender AIR (11th CAB FARP, V Corps F-16 / F-35 CAS basing). Counterreconnaissance is its own line of effort — corps SPF (750th SEPARATE SPF BDE) and long-range recon patrols against V Corps forward recon, counter-UAV at brigade, EW protection of OCCASUS movement (FMSO Russian Recon-Fire-Strike 2021; ATP 7-100.1 ¶5-31, ¶6-66).

Signature observables: UAVs flying over and forward of the FAAs; OCCASUS counter-battery radars positioned in the support zone covering V Corps 210th FA PAA; SIGINT and EW collection arrays at the FAAs targeting V Corps tactical nets; SPF teams operating against V Corps ISR (750th SPF BDE in the TRM corridor); intensified recon-fires complex activity in the 24-72 hours before H-Hour as targets are confirmed and engagement plans built.

### §5.d Fires

The Brigade Artillery Group (BrAG, brigade level — for the 72nd MRD this is the 72nd FA REGIMENT) and the Integrated Fires Command (IFC, corps level — for the 7th CAA this is centered on the 74th SP FA BDE counterfire plus the 75th MISSILE BRIGADE Iskander-M plus the corps MRL formations) are the doctrinal fires organizations. Both disperse for survivability and converge via automated C2 to mass on designated targets. Direct-lay against armor remains a SV mission — artillery is not just an indirect-fire asset.

Two fire-destruction periods in offense (ATP 7-100.1 ¶5-15, ¶5-19): fires preparation (30-60 minutes before H-Hour) and fires support of the attack (continuous from H-Hour). Fires preparation focuses on the breach sector (defender forward positions + fires + ADA + obstacle overwatch + identified reserves); fires support focuses on the assault BDEs as they close, the breach corridor as it is enlarged, and the defender's reserves attempting to reinforce. The 75th MISSILE BRIGADE Iskander-M provides DEEP STRIKE against operational-depth targets (210th FA DIVARTY, RSS Nodes, 11th CAB FARP, 1CD assembly area, ATK PSN VENUS ROC-T position in Lithuania) — these are the highest-value targets the army commander has and are released only against pre-validated CCIRs (CCIR 2 in the V Corps scenario is "75th MB TELs at forward firing positions confirmed").

Signature observables: BrAG positions in the OCCASUS support zone east of the line of departure; corps long-range fires dispersed forward (74th SP FA BDE in the 7th CAA rear; 75th MISSILE BRIGADE TEL positions at 34UFE35000 85000); pre-registered targets in the V Corps disruption zone (fires preparation) and on counterattack approaches (fires support against V Corps DP-1 / DP-4 reserve commitment); thermobaric munitions (TOS-1 / TOS-2) against fortified positions; UAV-spotted reconnaissance fires throughout.

### §5.e Sustainment (Logistics Rear Support)

Decentralized logistics with carried days of supply plus advanced caches to mitigate easily-disrupted LOCs (TC 7-100.2 ¶3-15, ¶4-50). Russian doctrine acknowledges sustainment can drive offensive culmination as much as enemy action — the 7th LOGISTICS BDE forward positioning ("3-day combat load forward, Class V push to firing units") is the V Corps scenario's doctrinal sustainment posture. The 7th LOGISTICS BDE locates with corps trains beyond V Corps ATACMS range; each company assault echelon stockpiles ammunition locally. Casualty evacuation routes back through the FAAs to corps Role 2 (MEDEVAC chain back to OCCASUS rear).

Signature observables: decentralized cache sites integrated into FAA dispersal areas; corps trains far rear (east of FAA SOKOL); multiple basic loads forward; pre-positioned Class V at the FAAs; reduced visible logistics convoys on identified routes (use of secondary roads and rail to avoid V Corps recon-fires complex).

### §5.f Protection

Integrated AD (Tunguska / Strela-10 / Igla forward with assault formations; SA-11 / SA-12 SAM umbrella over the FAAs and breach corridor); integrated antitank (organic tank fires + ATGM + 72nd FA REGIMENT direct-lay against V Corps M1A2 / M2 in defense; POZ for offensive counterminelaying on defender counterattack approaches); anti-air-assault (army-level anti-air-assault reserve plus integrated AD over the FAAs and corps HQ); counterreconnaissance via corps SPF + 750th SPF BDE; integrated engineer obstacles on breach-corridor flanks to canalize defender reaction; CBRN defense via organic CBRN battalion.

Signature observables: multiple AD layers from MANPADS forward to mid-range SAM in depth; POZ co-located with the engineer reserve at the line of departure; anti-air-assault reserve in dispersed AAs around FAA SOKOL and the corps HQ; deep minefields on breach-corridor flanks (NOT in the breach sector itself); CBRN decontamination sites in the rear support zone.

### §5.g Information and EW (Information Warfare)

Maskirovka is operationally co-equal with fires and maneuver — pre-H-Hour decoy assemblies in alternate FAAs, false fires-registration on non-breach sectors, dummy obstacle-clearing on non-breach axes, simulated radio traffic, deception forces replicating non-existent units, perception management of Polish civilian population and Western media via Cinereus IO campaign. EW subunits at brigade and the Cinereus EW Group at corps suppress V Corps C2, jam tactical nets, blind counter-battery radars, protect OCCASUS movement from V Corps proximity-fused munitions. Russian Ground Forces are the doctrinal leaders of EW in the Russian military (Jamestown EW monograph 2024).

Signature observables: decoy assemblies in dispersed locations beyond the actual FAAs; deception force units transmitting on replicated 71st MID command nets to misdirect V Corps intel on exploitation force location; EW arrays integrated forward with the lead BDEs; signals discipline plus wire comms in the FAAs pre-H-Hour; counter-UAV EW at brigade and battalion; Cinereus IO campaign in Polish-language and English-language social media (a 30-40 km civil-population displacement belt is reported in the V Corps OPORD §1.b.(3) and is exploited by Cinereus disinformation).

## §6. High-Value Target Categories (Systems-Warfare Methodology)

Russian-pattern HVT derivation in offense is NOT a fixed target list — it is a SYSTEMS-WARFARE methodology. The OCCASUS army commander identifies which components or subsystems of the V Corps combat system, if disaggregated FORWARD OF H-HOUR, would deny V Corps the ability to synchronize the defense. Specific HVTs flow from the operational scheme and the assessed V Corps reserve / counterattack posture.

Doctrinal HVT categories from the OFFENSIVE perspective (TC 7-100.2 ¶3-9, ¶4-100; ATP 7-100.1 ¶5-4 to ¶5-6; FMSO 2021):

- COMMAND AND CONTROL — V Corps Main CP (Wiesbaden), V Corps TAC FWD (Elblag from 15 AUG), MND-NE CP (Polish-led higher tactical HQ), 2CR HQ (34UEE62500 52500), squadron CPs of 1/2/3 SQDN 2CR, 210th FA CP, 11th CAB CP, 21st TSC FWD CP, comms relays, fires CP. Destruction effect: denies V Corps the synergy of its combat system; without C2 the recon-fires loop breaks, maneuver decentralizes into uncoordinated reactions, fires lose mass.
- RECONNAISSANCE, INTELLIGENCE, SURVEILLANCE, TARGET ACQUISITION (RISTA) — V Corps Gray Eagle / Reaper UAV bases, V Corps EW collection assets, V Corps counter-battery radars (Q-36 / Q-37 / TPQ-53), V Corps SOF teams in the disruption zone, V Corps JSTARS-equivalent (E-8C if available, Reaper-MX, etc.), V Corps ELINT and SIGINT collection. Destruction effect: blinds V Corps precision-strike system; OCCASUS movement to the FAAs and recon-fires-complex use of long-range fires is uncontested. Russian doctrine explicitly states the offense will focus significant effort on destroying ground reconnaissance by enemy SOF.
- PRECISION-FIRE SYSTEMS — V Corps 210th FA HIMARS / ATACMS / GMLRS (the DIVARTY PAA at 34UEE58250 56900); ROC-T at ATK PSN VENUS (35UMA22422 98926 in Lithuania) providing ATACMS deep counterfire; V Corps precision-guided artillery (Excalibur, M982); V Corps PrSM if available; corps cruise missiles if applicable. Destruction effect: removes V Corps's most dangerous standoff threat to OCCASUS depth (75th MISSILE BRIGADE TELs, 7th CAA HQ, BrAGs, reserves, logistics, CPs). CCIR 2 in the V Corps scenario maps to the OCCASUS side of this category — confirming when V Corps's ATACMS is ready to fire on 75th MB TEL positions.
- AVIATION (FIXED-WING + ROTARY-WING) — V Corps 11th CAB AH-64E (the primary CAS threat to OCCASUS assault formations), V Corps Black Hawk lift (the air-assault capability), V Corps F-16 / F-35 CAS basing (Powidz, Łask, Mihail Kogălniceanu), V Corps Reaper-MX armed UAVs, plus the FARPs and airfields that sustain them. Destruction effect: removes air-delivered precision and CAS; the 7th CHB attack-aviation feint into 2CR rear (COA-3 / COA-4) becomes uncontested. AD ambushes in the disruption zone are particularly effective.
- LOGISTICS AND SUSTAINMENT — V Corps RSS Nodes X / Y / Z (34UEE60000 53500 / 34UEE60000 51000 / 34UEE58800 56200 — the SOUTH Sector RSS triplet supporting 2CR), V Corps 21st TSC FWD (Elblag), V Corps BSA equivalents, fuel pumps, ATP and forward ammo points, distribution platoons, key bridges on V Corps LOCs (the TRM corridor / Route 58 plus the Polish HNS rail nodes at Elk). Destruction effect: brings V Corps's ability to sustain the defense to culmination by attrition of fuel and ammunition.
- GROUND LINES OF COMMUNICATION — V Corps TRM corridor (Route 58), Polish HNS rail (Elk rail hub, Białystok rail hub), road network from Elk to Suwałki to the Polish-Lithuanian border, bridges, defiles, river crossings. Destruction or interdiction effect: cuts V Corps from sustainment and reinforcement; forces canalization onto OPFOR-chosen axes; supports the 750th SPF BDE interdiction mission.
- MOBILITY AND COUNTERMOBILITY ASSETS — V Corps engineer breaching assets (MICLIC, plows, AVLBs), V Corps engineer bridging companies, V Corps bulldozers and route-clearance equipment. Destruction effect: prevents V Corps engineer effort on V Corps's own counterattack approaches; if V Corps cannot bridge the Narew / Biebrza / Pisa to counterattack into the breach, the OCCASUS lodgement is secure.
- CASUALTY EVACUATION ROUTES AND MEANS — V Corps MEDEVAC helicopters (11th CAB), Battalion Aid Station, ambulance exchange points, casualty collection points, V Corps Role 2 (likely co-located with RSS Node Y) and Role 3 (Elk District Hospital — but this is a PROTECTED FACILITY per IHL/LOAC). Listed explicitly as an OPFOR disruption-zone target.
- ENEMY AIR DEFENSE — V Corps Patriot batteries, V Corps NASAMS, V Corps SHORAD (Stinger, Avenger, IM-SHORAD Stryker variants), V Corps GBAD radars. Destruction effect: opens corridors for 7th COMBAT HELICOPTER BDE attack aviation and for the 75th MISSILE BRIGADE Iskander-M deep strikes.
- THEATER RESERVES — V Corps 1CD theater reserve at Grafenwöhr (the V Corps CDR-release-only reserve), 1ABCT in the CENTER Sector (Silmaris Gap defense — the V Corps CDR's first lateral-reinforcement option for 2CR), 173d ABCT OPCON FINDEFCOM (the Finland delay element). Destruction or fixing effect: prevents V Corps from sealing the breach with reinforcements. The COA-4 "Air-Assault Diversionary Scheme" is the OCCASUS doctrine for fixing the 1CD reserve and the 1ABCT CENTER lateral-reinforcement option.

For THIS scenario the per-COA HPT list is derived in the Threat COA Statement and HVTL, NOT here. The doctrine reference provides the categorical menu; the Threat COA Statement projects the menu onto the V Corps combat system, identifies which categories the selected COA targets, and produces the named HPT list. The HVTL writer derives the equipment-level HPT list from OCCASUS organic equipment (composition_rollup × EQ_HVT_VALUE) which is independent of the per-COA target selection. Do NOT invent categories beyond the menu — "long-range fires C2 node" is a real instantiation of CATEGORY: PRECISION-FIRE SYSTEMS; "enemy will to fight" is not a Russian-doctrinal HVT category.

## §7. Signature Behaviors

The behaviors below are what the analyst observes that signals each OFFENSIVE form. Each is annotated with the doctrinal pattern it indicates and the source.

- DISPERSED ASSEMBLY in multiple sub-AAs within each FAA, with sub-units spaced 1-3 km apart against precision fires. Indicates: OCCASUS pre-H-Hour assembly posture for any offensive form — universal. (ATP 7-100.1 ¶5-13; Watling 2024.)
- INTENSIFIED RECON-FIRES COMPLEX ACTIVITY in the 24-72 hours before H-Hour — UAV sorties forward, SIGINT / EW collection intensification, counter-battery radar emplacement, V Corps tactical-net jamming starts. Indicates: imminent offensive launch (the recon-fires complex is the attack's primary mechanism and must be loaded before H-Hour). (FMSO 2021; ATP 7-100.1 ¶5-4; ¶6-66.)
- CONCENTRATED ARTILLERY POSITIONS in OCCASUS support zone with overlapping range fans on a single breach sector, with adjacent sectors thinly covered. Indicates: penetration form (§2.a) — fires concentration matches assault concentration. (ATP 7-100.1 ¶5-13, ¶5-15.)
- DISPERSED ARTILLERY POSITIONS with overlapping range fans on TWO OR MORE sectors of equal weight. Indicates: envelopment (§2.b) or mixed-axis attack (multiple supporting attacks pinning the defender's lateral reinforcement). (ATP 7-100.1 ¶5-9; TC 7-100.2 ¶3-32.)
- 75th MISSILE BRIGADE TEL DISPLACEMENT to forward firing positions (34UFE35000 85000 or further forward) in the 4-12 hours before H-Hour. Indicates: CCIR 2 satisfied for V Corps — OCCASUS preparing to deliver deep strike on V Corps DIVARTY, RSS Nodes, and ROC-T. ATACMS authorization (V Corps DP-2) becomes urgent. (Watling 2024; Grau/Bartles 2021.)
- FED DETACHMENTS FORWARD of main BDE bodies on the chosen breach axis — tank-heavy task forces of company / battalion strength moving in advance of the assault echelon. Indicates: penetration form (§2.a) with classical FED employment to seize breach-corridor depth (bridges, defiles, road junctions). (ATP 7-100.1 ¶5-50 to ¶5-54.)
- 72nd MRD ASSAULT FORMATIONS in single-axis column with concentrated frontage of 4-6 km on a breach sector. Indicates: penetration form (§2.a) — concentrated effort at 5:1 doctrinal ratio. (ATP 7-100.1 ¶5-13.)
- 72nd MRD ASSAULT FORMATIONS distributed across multiple parallel axes (Drygały + Nitki + DK16) with each axis at lower concentration. Indicates: multi-axis breakthrough or envelopment form (§2.b) — pinning defender on multiple sectors to prevent lateral shift. (ATP 7-100.1 ¶5-9, ¶5-10.)
- 71st MID 2nd ECHELON HELD IN DISPERSED AAs around FAA SOKOL with NO movement to the line of departure in the early phase. Indicates: classical two-echelon scheme (§4.b) — exploitation force preserved for army commander's commitment decision. CCIR 3 / DP-4 V Corps trigger: detecting the 71st MID departure from SOKOL is the 1CD commit decision indicator. (ATP 7-100.1 ¶5-7, ¶5-18.)
- 71st MID 2nd ECHELON FORWARD MOVEMENT to the line of departure or beyond. Indicates: army commander has committed the exploitation force — the breach is confirmed and exploitation is underway. CCIR 3 satisfied; V Corps DP-4 1CD commit decision is forced. (ATP 7-100.1 ¶5-18 to ¶5-22.)
- 7th COMBAT HELICOPTER BDE AIR-ASSAULT lift-off from MDCOA staging (Pabrade Training Area or alternate) toward Silmaris Gap or 2CR rear. Indicates: COA-4 deception / fixing effort (§2.b envelopment via vertical envelopment) — drawing 1CD reserve and 1ABCT lateral reinforcement out of position. (TC 7-100.2 ¶3-32 to ¶3-39; FMSO Russian Airborne monograph.)
- 750th SEPARATE SPF BDE INFILTRATION INDICATIONS in V Corps TRM corridor (Route 58) — ambush activity, communications intercepts of SPF radio nets, V Corps sustainment convoy delays. Indicates: turning movement element (§2.c) — SPF interdicting V Corps LOCs to force V Corps to defend elsewhere; usually a precursor or accompaniment to a main effort on a different axis. (TC 7-100.2 ¶3-40.)
- CINEREUS EW GROUP ACTIVITY (R-330Zh Zhitel, Krasukha-4) jamming V Corps tactical nets, GPS spoofing in the breach corridor, Polish civilian-network suppression. Indicates: H-Hour minus 30 minutes to H-Hour — OCCASUS EW preparation phase. (Jamestown EW 2024; ATP 7-100.1 ¶5-31.)
- DECOY ASSEMBLIES detected in alternate FAAs east of OCCASUS rear, with deception-force radio nets active. Indicates: maskirovka — OCCASUS intends to misdirect V Corps recon on actual breach axis. (TC 7-100.2 ¶3-12.)
- COUNTER-PREPARATION FIRES LANDING ON OCCASUS PRE-ASSEMBLY POSITIONS. Indicates: V Corps has accurate recon on OCCASUS assembly and is launching a spoiling effort; OCCASUS will respond by accelerating H-Hour, by lateral movement of assault formations to non-targeted assembly, or by launching the offensive on shortened preparation. (TC 7-100.2 ¶3-7, ¶4-120 to ¶4-122; ATP 7-100.1 ¶5-15.)
- DEEP MINEFIELD EMPLACEMENT on the FLANKS of an identified breach corridor (NOT in the breach sector itself). Indicates: OCCASUS POZ employed offensively — canalizing V Corps counterattack reaction onto a flank that has been pre-targeted by 72nd FA REGIMENT direct-lay or 74th SP FA BDE counterfire. (ATP 7-100.1 ¶5-29.)
- INTEGRATED AD POSITIONING (76th AD BRIGADE forward to within SAM range of the breach corridor; 72nd ADA BN with the lead BDE). Indicates: OCCASUS preparing to absorb V Corps 11th CAB attack aviation and V Corps F-16 / F-35 CAS in the breach corridor — a doctrinal requirement of any Russian assault in a Western-RISTA-rich AOR. (ATP 7-100.1 ¶5-25.)
- THERMOBARIC FIRES (TOS-1 / TOS-2) landing on 2CR forward CBPs at OBJ JACKSON. Indicates: fires preparation against fortified positions — typical of fires-preparation phase against an area defense. (Watling 2024; ATP 7-100.1 ¶5-15.)
- ANTI-AIR-ASSAULT POSTURE around 75th MISSILE BRIGADE TEL positions, corps HQ, and FAA SOKOL — anti-air-assault reserves at rapid-access AAs, obstacles in likely DZs/LZs, organic AD overhead. Indicates: OCCASUS assesses V Corps air-assault or SOF action on key support-zone targets is a real threat (especially 173d ABCT OPCON FINDEFCOM Finland-axis airborne option). (ATP 7-100.1 ¶5-26, ¶6-28.)

## §8. COA Pattern Family (the Menu)

There is no doctrinally-fixed "most likely / intermediate / most dangerous / most deceptive" COA quartet for a Russian-pattern army commander in offense. The COA family is a MENU of doctrinal offensive patterns. Which is selected — and which is "most likely" versus "most dangerous" versus "most deceptive" — depends on (a) the army commander's mission (seize OBJ JACKSON; penetrate to specific operational depth; conduct economy-of-force tying), (b) the assessed V Corps defensive posture (where is 2CR's main defense actually positioned? are 1ABCT and 1CD committed elsewhere?), (c) terrain and seasonal trafficability (Elk corridor narrow river crossings; rail corridor utility), (d) OCCASUS force-ratio assessment (5:1 in breach sector achievable? where?), (e) the V Corps PRECISION-STRIKE and AVIATION threat (must 76th AD and EW cover the breach? FED commit conditional on suppression?), (f) the timeline (must seize OBJ JACKSON before 1CD theater reserve can commit — that's the 4-6 hour planning factor), and (g) political-strategic constraint (must OCCASUS avoid Polish civilian casualties in NFAs at Elk District Hospital / Pisz Fuel Depot / Orzysz Water Treatment? IHL/LOAC). The Threat COA Statement writer's job is to pick from the family based on scenario context, not to pre-rank pattern names.

The doctrinal minimum is ATP 2-01.3 ¶6-35 — develop AT LEAST a most-likely and a most-dangerous COA. The V Corps OPORD-driven minimum is FOUR COAs (most-likely / intermediate / most-dangerous / most-deceptive) to give the V Corps CDR the full COA panel. Each COA must differ from the others on at least one of the six basic questions in a way the V Corps ISR plan can detect (ATP 2-01.3 ¶6-34).

### Candidate patterns (offensive analogues; selection is runtime)

- TWO-ECHELON MECHANIZED PENETRATION (single-axis, classical SV form §2.a). 72nd MRD assault on a single breach corridor (most commonly DK16 / Klusy axis = EA-3); concentrated fires preparation; FED forward of assault echelon; 71st MID 2nd echelon held for exploitation. Doctrinal conditions favoring: terrain forces canalization onto a single usable corridor; OCCASUS achieves 5:1 in the breach sector; V Corps reserves are committable to the lateral but engagement timeline is short. (ATP 7-100.1 ¶5-8.)

- MULTI-AXIS BREAKTHROUGH AND FLANK ISOLATION (envelopment, §2.b). 72nd MRD splits across two-or-three parallel axes (Drygały + Nitki + DK16 — or some subset); supporting attacks fix V Corps on each axis and prevent lateral shift; 72nd FA REGIMENT shifts mass between axes; 71st MID exploits whichever axis breaks through first; deep strikes (75th MISSILE BRIGADE) interdict 1ABCT lateral movement from CENTER Sector. Doctrinal conditions favoring: multiple usable corridors exist; OCCASUS has the formations to mass on each at sub-5:1 but with sufficient pinning effect; V Corps lateral-reinforcement is the key vulnerability. (ATP 7-100.1 ¶5-9; TC 7-100.2 ¶3-32.)

- DEEP STRIKE AND MASSED EXPLOITATION (penetration §2.a augmented with operational deep strike, "Most Dangerous" pattern). 75th MISSILE BRIGADE Iskander-M deep strike at H-2 hours on 210th FA DIVARTY + RSS Nodes + ROC-T ATACMS — blinds V Corps fires and sustainment forward of H-Hour; 72nd MRD single-axis assault on DK16 with overwhelming fires; 71st MID committed early for rapid exploitation through OBJ JACKSON to deep operational objective (Silmaris Gap / Suwałki). Doctrinal conditions favoring: 75th MISSILE BRIGADE has confirmed forward firing positions; V Corps fires complex has not dispersed adequately; V Corps 1CD theater reserve commitment is slow (Grafenwöhr-to-BPTA is multi-day if not pre-positioned). Most dangerous because it removes V Corps fires before the assault, dramatically improving OCCASUS force ratio. (ATP 7-100.1 ¶5-4, ¶5-5; FMSO 2021.)

- AIR-ASSAULT DIVERSIONARY SCHEME (envelopment §2.b with vertical envelopment, "Most Deceptive" pattern). 7th COMBAT HELICOPTER BDE air-assault feint on the Silmaris Gap (CENTER Sector) at H-2 hours — draws 1CD theater reserve commit decision AWAY from SOUTH Sector and ties 1ABCT to CENTER; concurrent 72nd MRD genuine assault on Elk corridor with reduced V Corps reserve threat. Doctrinal conditions favoring: V Corps CDR has shown sensitivity to CENTER Sector vulnerability (Silmaris Gap is the V Corps political-strategic vulnerability per the OPORD); 7th CHB has uncommitted lift capability; OCCASUS can credibly stage the air-assault deception (replicated radio nets, decoy assemblies, false fires-registration at Silmaris Gap). Most deceptive because it weaponizes V Corps's own political-strategic priority against him. (TC 7-100.2 ¶3-32 to ¶3-39; ATP 7-100.1 ¶5-9; SunTzuOPFOR project_doc cross-reference for deception primitives.)

- TURNING MOVEMENT BY 750th SPF BDE INTERDICTION (§2.c). 750th SEPARATE SPF BDE interdicts V Corps TRM corridor (Route 58) and conducts SOF action against V Corps RSS Nodes and 21st TSC FWD — forces V Corps to defend his rear before he can fully commit to the forward defense at OBJ JACKSON; 72nd MRD assaults Elk corridor on a thinned defender. Doctrinal conditions favoring: SPF infiltration achieved without compromise; V Corps sustainment posture is fragile (RSS triplet is dispersed but constrained by single road network). Less likely as the SOLE main effort because SPF cannot seize and hold OBJ JACKSON; more likely as a TURNING-MOVEMENT SUPPORT to one of the other patterns. (TC 7-100.2 ¶3-40 to ¶3-46.)

- FRONTAL ATTACK ON A WIDE FRONTAGE (least-preferred §2.d). Distributed pressure across the full V Corps 2CR frontage. NOT a doctrinal preferred OCCASUS COA because it concedes force-ratio at every point. May appear as a DECEPTION ELEMENT supporting another pattern (e.g. supporting attacks pinning 1/2/3 SQDN 2CR while the actual penetration develops elsewhere). Doctrinal conditions favoring (rare): overwhelming combat-power superiority; V Corps flanks anchored on terrain or boundary that cannot be enveloped; time pressure forces non-optimal scheme.

- SPOILING ATTACK BY V CORPS (RECEIVED §2.e). V Corps may launch a spoiling attack against OCCASUS FAA positions before H-Hour — this is the V Corps side of the menu but is RECEIVED by OCCASUS. OCCASUS response: accelerate H-Hour to pre-empt; reposition forces to absorb; commit fires-preparation against the spoiling force. The Threat COA Statement writer evaluates whether V Corps is positioned to spoil and how OCCASUS would respond.

### How to pick in a scenario (writer's worksheet)

1. Identify the army commander's intent constraint: must he seize OBJ JACKSON in 12-24 hours (forces deep strike + accelerated 71st MID commit) or can he take 48-72 hours (enables multi-axis envelopment with longer breach effort)? Political deadline?
2. Identify the V Corps reserve posture: is 1CD positioned to commit (Grafenwöhr is multi-day from BPTA) or already forward (then deception is harder)? Is 1ABCT committed CENTER (then COA-4 deception is highly effective)?
3. Identify usable terrain: is only DK16 trafficable for armor (forces single-axis penetration) or are Drygały rail + Nitki rail also usable (enables multi-axis or envelopment)?
4. Assess relative mobility: does OCCASUS have local mobility advantage over 2CR (enables maneuver / envelopment) or is the force pinned by V Corps fires (forces penetration)?
5. Assess V Corps RISTA + precision-standoff balance: heavy V Corps advantage drives the OCCASUS scheme toward deep strike (§2.a + Iskander-M) and EW saturation, with greater reliance on dispersion and maskirovka.
6. Locate the OCCASUS operational reserve: 71st MID at FAA SOKOL; commit timing is the army commander's key DP. If 71st MID is committed early, OCCASUS is in the deep-strike / massed-exploitation COA; if held late, classical two-echelon.
7. Check the deception calculus: does V Corps's posture create an exploitable deception opportunity (e.g. 1ABCT committed CENTER to Silmaris Gap)? If yes, COA-4 maskirovka pattern becomes highly attractive.
8. Identify the MOST-LIKELY COA as the one where MOST conditions align doctrinally; identify MOST-DANGEROUS as the COA most threatening to V Corps's scheme (typically the deep-strike pattern); identify MOST-DECEPTIVE as the COA whose discovery cost is highest for V Corps (typically the air-assault deception). These three are NOT necessarily the same and are not pre-fixed.

### Application to this scenario

OCCASUS 7th CAA is in the Forward Assembly Areas east of Poland, preparing a two-echelon mechanized assault west toward OBJ JACKSON. The political constraint (must OCCASUS avoid Polish-civilian-NFA collateral?), the V Corps defensive posture (where exactly are 1ABCT and 1CD?), the OCCASUS readiness state (75th MB at forward firing positions yet?), and the timeline (12-24h vs 48-72h) are scenario-runtime inputs that the Threat COA Statement writer evaluates. Do NOT pre-declare which pattern applies — the writer selects from the menu above using the picker steps. Whatever patterns are selected, each must (a) cite a doctrinal pattern from this menu, (b) name the formations committed to each functional role using OOB ids derived from scenario.KG.unit_red and the OPORD, (c) be distinguishable from sibling COAs per ATP 2-01.3 ¶6-34, and (d) carry the recon-fires-complex, deep-strike, and 71st MID commitment plan as part of the scheme, not as contingencies.

## §9. Decision Triggers and Branches

There is no universal Russian-pattern offensive decision flowchart. Decisions are framed by the army commander's intent (operational mission — seize OBJ JACKSON), the prerequisite of concentration of effort plus recon-fires-complex success, and the imperative that any offensive must culminate in either continued exploitation OR transition to hasty defense on the seized objective. Decision logic is a FAMILY OF BRANCHES tied to observable defender conditions — not a script.

### §9.a Transition to Spoiling Defense (Triggers from Offense to Defense Within the Operation)

- Unexpected V Corps spoiling attack disrupts OCCASUS assembly before H-Hour.
- V Corps preempts with massed ATACMS strike on OCCASUS support zone (75th MISSILE BRIGADE TEL positions, corps HQ, 76th AD BRIGADE).
- V Corps gains unexpected air superiority over the FAAs (11th CAB + F-35 CAS push the OCCASUS AD umbrella back).
- 72nd MRD assault echelon takes catastrophic attrition (>50%) before reaching the immediate objective.
- OCCASUS culminates short of the immediate objective with the second echelon already committed.

(Source: TC 7-100.2 ¶3-63 to ¶3-65; ATP 7-100.1 ¶5-12.)

### §9.b Transition Between Offensive Forms (Within the Operation)

- From penetration to envelopment: lead BDE assault on the chosen breach sector fails to rupture defender main defensive position; army commander shifts main effort to a supporting axis that has shown weaker resistance — converts the supporting attack into the main effort.
- From envelopment to penetration: enveloping force is intercepted by V Corps reserves before reaching the flank; army commander concentrates the remaining effort on the original fixing force's sector and converts the fixing attack into a penetration attempt.
- Within a penetration: army commander commits the 2nd echelon (71st MID) earlier or later than planned based on (a) breach depth achieved, (b) defender reserve commitment indicators, (c) recon-fires complex assessment of defender residual combat power on the breach axis.

### §9.c Commitment of Reserve Branches

Each branch is pre-rehearsed with priority order; the army-level combined-arms reserve has a list of missions ranked by likelihood. Reserves are always combined-arms.

- COUNTERATTACK INTO DEFENDER COUNTERATTACK — engage and destroy a V Corps counterattack into the breach (V Corps DP-4 1CD theater reserve commit is the doctrinal trigger).
- BREACH WIDENING — commit reserve to widen the breach corridor for the 2nd echelon's passage; reduces shoulders of the breach against V Corps lateral fires.
- ANTI-AIR-ASSAULT REACTION — eliminate V Corps vertical envelopment by airborne or air-assault forces against OCCASUS support zone (75th MISSILE BRIGADE TELs, corps HQ).
- BRANCH EXPLOITATION — commit reserve to an unexpected breakthrough opportunity on a non-main-effort axis.
- TRANSITION-TO-DEFENSE COVER — cover the assault echelon transitioning to hasty defense on the seized objective.

### §9.d Second-Echelon (Exploitation Force) Commitment Triggers

The army commander's commit decision for the 71st MID is the operation's single most consequential DP. Doctrinal trigger conditions (ATP 7-100.1 ¶5-18 to ¶5-22; Grau/Bartles 2021):

- Breach of V Corps 2CR main defensive position is confirmed by recon (72nd MRD lead element reaches PL BLUE or beyond — V Corps NAI-205-DK16-KLUSY in the scenario).
- Recon-fires complex confirms V Corps reserves cannot interdict the exploitation force before it reaches the immediate objective.
- 72nd MRD remains capable of widening or holding the breach corridor for the 71st MID's passage (combat power above culmination threshold).
- 71st MID has completed pre-commit displacement to the line of departure (CCIR 3 / V Corps DP-4 indicator — detecting 71st MID departure from FAA SOKOL).

Commit ORDER timing is doctrinally 4-6 hours of attack time minimum (force the V Corps DP-1 decision before the 71st MID commits, so V Corps has expended forward engagement authorization before seeing the exploitation force); upper bound is 12 hours (beyond which the breach corridor risks being sealed by V Corps lateral reserves).

### §9.e Branches at Army and Brigade Commander Level

- ON FIRST CONTACT WITH V CORPS COVERING FORCE: lead BDE initiates assault on V Corps disruption-zone targets — branch (a) destroy lucrative target, (b) deceive on actual breach axis, (c) canalize V Corps reaction onto chosen ground, (d) delay V Corps response for OCCASUS lateral movement.
- AT V CORPS MAIN DEFENSE IDENTIFICATION: 72nd FA REGIMENT fires preparation against confirmed defender positions; 7th ENGINEER BDE moves to breach posture; FED detachment crosses line of departure.
- ON BREACH ACHIEVED: 71st MID commit signal sent (per §9.d); 76th AD BRIGADE shifts forward to cover breach corridor; 7th LOGISTICS BDE Class V push to firing units in the breach.
- ON V CORPS COUNTERATTACK INDICATIONS: anti-counterattack fires preparation; reserve commitment per §9.c; 75th MISSILE BRIGADE strike on the V Corps reserve assembly area en route.
- ON V CORPS AIR-ASSAULT INDICATIONS: anti-air-assault reserve repositions to threatened DZ/LZ; corps AD focuses on the air-assault lift; 76th AD BRIGADE engages the lift formations.
- ON CULMINATION INDICATORS: army commander either (a) commits the combined-arms reserve to complete the operation, (b) commits the 71st MID earlier than planned, or (c) transitions the 72nd MRD assault echelon to hasty defense on the seized ground.

### §9.f Decision Information Requirements

The army commander's decision support hinges on these IRs:

- Where is V Corps's reserve actually committed (1CD released from Grafenwöhr? 1ABCT committed CENTER or shifting to SOUTH? 11th CAB FARP location and sortie rate?)?
- Has V Corps revealed defensive scheme (which axis is the main defense actually on? has 2CR committed FAS / TF HELL forward or held it for counterattack? where is the V Corps DIVARTY actually positioned at H-Hour?)?
- Is the V Corps combat system aggregated or disaggregated (C2 active under EW pressure? recon-fires loop functioning under SPF + Iskander-M strike? logistics tail moving on schedule? AD active over forward defense?)?
- Are the breach-sector kill zones loaded as planned (72nd FA REGIMENT mass on breach? 7th ENGINEER BDE breach assets ready? 72nd ADA BN forward with assault echelon?)?
- Is the 71st MID exploitation force uncompromised by V Corps deep ATACMS strike, EW, or ISR detection of pre-commit displacement?
- Has the recon-fires complex achieved disaggregation of V Corps disruption-zone combat system before assault echelon crosses the line of departure?

The Threat COA Statement and SITEMP translate these IRs into named NAIs whose `watching_for` indicators map to friendly PIRs (V Corps CCIRs 1-3 in the scenario) and to OCCASUS commander's DPs. NAI-201-URAL-CENTRAL, NAI-202-URAL-NORTH, NAI-203-SOKOL, NAI-204-75MB-TELS, NAI-205-DK16-KLUSY in the scenario map to OCCASUS-side observables that ALSO serve as V Corps indicators — every V Corps PIR maps to an OCCASUS DP because both sides are watching the same battlefield events.

## §10. Common Misconceptions

These are widespread misreadings of Russian offensive doctrine. The writer must NOT reproduce them in any artifact.

- "Russian / OPFOR offense is a frontal mass attack across a wide frontage." WRONG. Modern SV doctrine concentrates effort on a NARROW breach sector at the 5:1 doctrinal ratio; supporting attacks pin defender on adjacent sectors. Frontal mass is the LEAST preferred offensive form (§2.d, used only as deception or when flanking is foreclosed). (ATP 7-100.1 ¶5-8, ¶5-10, ¶5-13; TC 7-100.2 ¶3-22, ¶3-47.)
- "The two-echelon scheme is Soviet legacy and obsolete." WRONG. Two-echelon is universal at every echelon from BDE up. The 2nd echelon is preserved for EXPLOITATION through the breach (§4.b). Commitment of the 2nd echelon is the army commander's single most consequential DP. (ATP 7-100.1 ¶5-7, ¶5-18.)
- "Russian fires are mass without precision." WRONG. The recon-fires complex (ROK at brigade, RUK at operational level) is the modern SV's primary offensive mechanism. Detection-to-engagement is sub-10 minutes via Strelets KRUS. Mass and precision are complementary. (FMSO Russian Recon-Fire-Strike 2021; ATP 7-100.1 ¶5-4; ¶6-66.)
- "The Forward Detachment is just an advance guard." WRONG. The FED is a reinforced battalion or BDE-minus with MISSION-COMMAND LATITUDE, given an objective and intent and trusted to seize it. The FED is sized to FIGHT for the objective if necessary. The advance guard is a smaller security element forward of the main body. (ATP 7-100.1 ¶5-50 to ¶5-54.)
- "The Operational Maneuver Group is the same thing as the second echelon." WRONG. The OMG is a 2nd-echelon detachment given a DEEP operational objective and committed to operate independently in the defender's depth. The OMG bypasses pockets, attacks sustainment, fixes reserves. The main 2nd echelon exploits the breach into the defender's main defensive depth. They are separate roles. (Grau/Bartles 2021; FMSO 2018 OMG monograph.)
- "Maskirovka means camouflage." WRONG. Maskirovka is the full doctrinal complex of camouflage plus concealment plus cover plus deception plus denial plus disinformation plus perception management. In offense it includes pre-H-Hour decoys, false fires-registration, false-axis assemblies, vertical-envelopment feints, and post-breach false withdrawal preparations. (TC 7-100.2 ¶3-12, ¶4-36, ¶4-45, ¶4-130.)
- "Russian doctrine requires equal force-ratio across the front." WRONG. Doctrine REQUIRES 5:1 in the breach sector and ACCEPTS 1:1 or worse on supporting attacks. The supporting attacks are not intended to penetrate; they are intended to PIN the defender's lateral reinforcement. Equal distribution is non-doctrinal. (ATP 7-100.1 ¶5-13.)
- "Recon-fires complex is just a sensor-to-shooter loop, not the attack's primary mechanism." WRONG. In modern Russian offensive doctrine the recon-fires complex DRIVES the attack — it disaggregates the defender FORWARD of H-Hour and continuously interdicts defender reserves during the assault. Without a functioning recon-fires complex the offensive reverts to attritional frontal assault, which doctrine explicitly rejects. (FMSO 2021; ATP 7-100.1 ¶5-4.)
- "Russian commanders don't allow subordinate initiative." WRONG. FED and OMG commanders have explicit mission command latitude. Lower-echelon initiative is constrained but battle-drill-based — and the FED / OMG roles are doctrinally designed to give a commander the latitude to seize objectives without waiting for orders. (ATP 7-100.1 ¶5-50.)
- "Russian air defense in offense is just dragging the defensive AD forward." WRONG. AD in offense is INTEGRATED with the assault formations from the brigade forward (Tunguska + Strela-10 + MANPADS with the lead BDE) and overlaid with corps SAM (SA-11 / SA-12) covering the breach corridor. Failure to do so concedes the breach to enemy attack aviation. (ATP 7-100.1 ¶5-25.)
- "OCCASUS / OPFOR will only attack on the obvious axis." WRONG. Doctrine REWARDS exploitation of multiple corridors, vertical envelopment, deep SPF action, and deception. The "obvious axis" is often the supporting effort intended to pin the defender while the actual main effort develops on a less-obvious axis. (TC 7-100.2 ¶3-32; ATP 7-100.1 ¶5-9.)
- "Culmination is a failure." WRONG. Culmination is a positive operational decision. At culmination the army commander transitions assault formations to hasty defense, consolidates gains, accepts logistics push, and prepares for the next phase. Russian doctrine does NOT treat culmination as failure to be avoided at all costs — sustainable offensive momentum is preserved by accepting culmination on a seized objective rather than over-extending into a counterattack. (TC 7-100.2 ¶3-63; ATP 7-100.1 ¶5-12.)
- "Russian Ground Forces are still Soviet — frontal massed armor on a single axis." WRONG. ATP 7-100.1 is explicit: the SV is not the ground force of WW2 or the Cold War; modern SV fights with the recon-fires complex driving the attack, two-echelon scheme with FED / OMG mission command latitude, integrated EW + AD + engineer effort, and multi-axis pressure. (ATP 7-100.1 ¶5-1, ¶6-1.)
- "OCCASUS Alliance (Murinus + Griseus + Plumbeus + Cinereus) is a fictional construct without doctrinal grounding." WRONG. OCCASUS is a fictional Alliance construct AUTHORED by V Corps OPORD v2 (J. McConnell) for the MITS 26 exercise. The Russian-pattern doctrine described here APPLIES TO 7th CAA as the assault formation regardless of the alliance fiction; the alliance composition affects strategic-level analysis (Murinus = primary land power; Cinereus = cyber/EW lead) but does not change the army-level doctrinal patterns. (V Corps OPORD v2 §1.a.(1); this reference §1, §3 application paragraph.)

## §11. Mapping Doctrinal Roles to OOB at Runtime

This reference NEVER names specific OCCASUS formations against specific doctrinal roles. The Threat COA Statement writer, the SITEMP writer, the Threat COAs PPTX writer, and the HVTL writer derive unit allocations from the OOB at scenario time. The doctrinal-role-to-OOB-template mapping below shows the role types each writer must fill from scenario.KG.unit_red — the IDs themselves are determined by the writer at runtime.

- Disruption-zone LONG-RANGE FIRES — the corps-level missile / MRL formation in the OOB (in V Corps scenario: 75th MISSILE BRIGADE Iskander-M and 74th SP FA BDE counterfire / MRL).
- Disruption-zone SPF — the corps-level SOF formation in the OOB (in V Corps scenario: 750th SEPARATE SPF BDE).
- Disruption-zone ATTACK AVIATION — the corps-level rotary attack formation in the OOB (in V Corps scenario: 7th COMBAT HELICOPTER BDE Mi-35 / Mi-24 / Ka-52).
- Disruption-zone EW — the corps-level EW formation in the OOB (in V Corps scenario: Cinereus EW Group at army level — see §1 application paragraph).
- Battle-zone FIRST-ECHELON ASSAULT BDEs — the division-organic forward-echelon BDEs in each first-echelon division's OOB (in V Corps scenario: 721 / 722 / 723 MRB of the 72nd MRD).
- Battle-zone DIVISIONAL FA — the division-organic FA formation, positioned in the support zone to support the assault BDEs (in V Corps scenario: 72nd FA REGIMENT 2S19 152mm).
- Battle-zone DIVISIONAL ADA — the division-organic AD formation, integrated forward with the assault formations (in V Corps scenario: 72nd ADA BN Pantsir / 2S6).
- Battle-zone ENGINEER BREACH ASSETS — the corps-level engineer formation conducting forward obstacle effort and assault breach (in V Corps scenario: 7th ENGINEER BDE TMM / PMP / UR-77).
- Battle-zone FORWARD DETACHMENT (FED) — a reinforced battalion or BDE-minus detached from the lead BDE for the deep-objective seizure mission (FED selection and composition is per-COA; typically a tank-heavy company / battalion task force from the lead BDE with organic engineer, AD, and fires attachments).
- Battle-zone SECOND-ECHELON EXPLOITATION FORCE — the army's 2nd echelon held back for commitment on order, committed through the breach (in V Corps scenario: 71st MID with 711 / 712 / 713 MIB, at FAA SOKOL).
- Battle-zone OMG ANALOGUE — the 2nd-echelon's deep operational mission (in V Corps scenario: the 71st MID's deep objective is interdiction of V Corps lateral reinforcement from CENTER Sector).
- Support-zone ARMY HQ — the corps headquarters formation in the OOB (in V Corps scenario: 7TH CAA HQ at 34UFE30000 80000).
- Support-zone IFC — the corps Integrated Fires Command for corps long-range fires (in V Corps scenario: IFC tied to 75th MISSILE BRIGADE + 74th SP FA BDE).
- Support-zone CORPS ADA — corps-level AD covering rear targets (in V Corps scenario: 76th AD BRIGADE SA-11 / SA-12).
- Support-zone CORPS SIGNAL — corps-level signal formation (in V Corps scenario: typically organic to 7TH CAA HQ).
- Support-zone CORPS SUSTAINMENT — corps-level logistics formation (in V Corps scenario: 7th LOGISTICS BDE).

The writer reads each slot from the OOB at scenario time, populates the SITEMP and COA Statement with the named ids, and surfaces the doctrinal-role-to-id mapping in the per-COA functional_allocation. If an OOB slot is empty (no formation exists for a given doctrinal role), the writer either (a) marks the gap as a planning consideration in the COA Statement, or (b) flags it as an OOB gap for fixture resolution — the writer never invents a formation that is not in the OOB.

## §12. Key Doctrinal Terms (Glossary)

Terms used throughout this reference and the downstream Threat COA Statement / SITEMP / Threat COAs / Threat Model / Threat Capability Statement / HVTL artifacts.

- FED (FORWARD DETACHMENT — передовой отряд) — a reinforced tank battalion or BDE-minus (organic tank or motor rifle plus organic engineer, AD, fires, and recon) detached from the lead BDE and tasked to move AHEAD of the main force to seize critical objectives (bridges, defiles, road junctions, depots, key terrain) before the defender can react. Operates with mission command latitude (ATP 7-100.1 ¶5-50 to ¶5-54).
- OMG (OPERATIONAL MANEUVER GROUP — оперативная маневренная группа) — the army-level analogue of the FED at operational scale. A reinforced division or BDE+ detached from the army's 2nd echelon, given a deep operational objective in the defender's rear, committed through the breach to operate independently in the defender's depth (Grau/Bartles 2021; FMSO 2018; TC 7-100.2 ¶3-51 to ¶3-56).
- BRONEGRUPPA — armored group; dismounted-IFV / APC vehicles re-formed into a direct-fire armored maneuver group, commanded by the deputy commander, used in offense to close gaps formed by enemy fires and to conduct local counterattacks against defender counterattacks (ATP 7-100.1 ¶5-32, ¶6-32, ¶6-77).
- BRAG — Brigade Artillery Group; multi-battalion organization defeating enemy on approach march, line of deployment, staging area, and during penetration from interim firing positions (in offense the BrAG supports the assault echelon from positions in the OCCASUS support zone). ATP 7-100.1 ¶5-15, ¶6-23.
- IFC — Integrated Fires Command; the corps fires C2 node controlling corps long-range artillery, MRLs, and ground-launched standoff systems (TC 7-100.2 ¶4-100).
- ROK — Reconnaissance Fire Complex; the tactical analogue of the RUK at brigade and below; ties UAV company, artillery recon, EW recon, and OPs to the BrAG and battalion mortars via Strelets KRUS (FMSO Russian Recon-Fire-Strike 2021).
- RUK — Reconnaissance Strike Complex; operational-strategic integration of long-range precision weapons with real-time intel via fused fire-direction centre (FMSO Russian Recon-Fire-Strike 2021).
- POZ — Mobile Obstacle Detachment; in OFFENSE used for offensive counterminelaying on defender counterattack approaches and on breach-corridor flanks to canalize defender reaction (ATP 7-100.1 ¶5-29).
- KRUS / STRELETS — the SV automated C2 / sensor-to-shooter terminal at brigade and below; modern cycle 8-12 minutes (Shoigu), kill-chain claims of 10 seconds at the lowest level (CCW Oxford 2018).
- BYLINA — the higher-echelon automated C2 system; ties RUK assets to operational-strategic fires (FMSO Russian Recon-Fire-Strike 2021).
- FAA — Forward Assembly Area; OCCASUS-specific terminology for the corps-rear assembly area from which assault formations launch the attack (in V Corps scenario: FAA URAL-CENTRAL, FAA URAL-NORTH, FAA SOKOL).
- LINE OF DEPARTURE (LD) — the trace at which the assault formations cross into the disruption zone toward the defender's main defensive position.
- IMMEDIATE OBJECTIVE — the first-echelon's depth objective; for the V Corps scenario this is the rupture of the 2CR area defense and seizure of OBJ JACKSON.
- SUBSEQUENT OBJECTIVE — the exploitation force's depth objective; for the V Corps scenario this is the operational-depth target reached by the 71st MID after committing through the breach (interdiction of V Corps lateral reinforcement; seizure of operationally decisive terrain in V Corps rear).
- FIRES PREPARATION (огневая подготовка) — the 30-60 minute fires period before H-Hour against defender forward positions, fires, ADA, C2, obstacle-belt overwatch, and identified reserves (ATP 7-100.1 ¶5-15).
- FIRES SUPPORT OF THE ATTACK (огневая поддержка наступления) — continuous fires from H-Hour through the assault, supporting the maneuver formations as they close, breach, and exploit (ATP 7-100.1 ¶5-19).
- COUNTER-PREPARATION FIRES — fires landed on the defender before friendly H-hour to disrupt his attack preparation (in V Corps scenario the OCCASUS-side concern is the inverse — V Corps counter-preparation against OCCASUS H-Hour). TC 7-100.2 ¶4-120 to ¶4-122.
- ON-CALL FIRES — pre-registered fires released against pre-planned targets on demand during the fight (ATP 7-100.1 ¶6-65).
- MASKIROVKA — the doctrinal complex of camouflage, concealment, cover, deception, denial, disinformation, and perception management; co-equal with fires and maneuver (TC 7-100.2 ¶4-36, ¶4-45, ¶4-130).
- BLOKIROVANIE — blocking tactic; boxes in an enemy force to facilitate destruction by massed artillery fire (RAND PE231 2017).
- SYSTEMS WARFARE — attacking specific components or subsystems of the enemy combat system in order to disaggregate the system of systems (TC 7-100.2 ¶3-7, ¶3-9, ¶4-21 to ¶4-23, ¶4-44).
- ATTACK FORCE / EXPLOITATION FORCE / FIXING FORCE / SECURITY FORCE / DECEPTION FORCE — TC 7-100.2 functional force terms applied to offense (TC 7-100.2 ¶3-7 to ¶3-11); identity may shift during the operation.
- ASSAULT ECHELON — the 1st echelon committed to the penetration / envelopment of the defender's main defensive position; in V Corps scenario the 72nd MRD with 721 / 722 / 723 MRB.
- EXPLOITATION ECHELON — the 2nd echelon held for commitment through the breach to attack the defender's depth; in V Corps scenario the 71st MID with 711 / 712 / 713 MIB at FAA SOKOL.
- ANTITANK RESERVE (AT RESERVE) — brigade's organic AT artillery battalion; in offense held to engage defender armored counterattacks against the breach (ATP 7-100.1 ¶5-26, ¶6-26).
- ANTI-AIR-ASSAULT RESERVE — motorized rifle company from second echelon, positioned with rapid-access routes to likely DZs and LZs around the corps support zone (75th MISSILE BRIGADE TEL positions, corps HQ, FAA SOKOL). ATP 7-100.1 ¶5-26, ¶6-28, ¶6-60.
- KILL ZONE / FIRE SACK — in offense, a planned area in the defender's depth where the recon-fires complex catches defender reserves attempting to reinforce against the breach (TC 7-100.2 ¶4-125).
- BREACH SECTOR — the narrow front (4-6 km at BDE) on which the assault echelon concentrates effort for the rupture (ATP 7-100.1 ¶5-13).
- BREACH CORRIDOR — the depth corridor through the defender's main defensive position that the assault echelon opens for the 2nd-echelon exploitation force's passage (ATP 7-100.1 ¶5-22).
- LATERAL REINFORCEMENT — defender movement of reserves from adjacent sectors to seal the breach; OCCASUS offensive doctrine pre-empts via supporting attacks, long-range fires interdiction, and deception (§4.m).
- CULMINATION — the operational moment when the attacker's combat power has been expended such that further attack risks decisive defeat. OCCASUS doctrine treats culmination as a positive decision to transition to hasty defense on the seized objective (§4.n; TC 7-100.2 ¶3-63 to ¶3-65; ATP 7-100.1 ¶5-12).
- TRANSITION TO HASTY DEFENSE — the operational action at culmination; assault formations dig in on the seized objective, accept logistics push, prepare to repulse defender counterattack, and await either continued exploitation order or operational pause for further build-up.""",
    ),
    "SunTzuOPFOR": _doc(
        "OPFOR Sun Tzu modeling",
        "doctrine",
        """OPFOR Sun Tzu modeling — OCCASUS commander prefers to engage where terrain
canalizes the enemy (Elk corridor narrow river crossings), commits 2nd echelon
only when 1st-echelon attrition justifies (71st MID departure from SOKOL is
the decisive 2nd-echelon commit; CCIR 3 reports it as a Flash). MDCOA variant:
8th CAA massed assault on Lithuania axis with 7th CHB air-assault on 2CR rear.""",
    ),
    "RoadToWar": _doc(
        "Road to War — pre-H-Hour intelligence build",
        "intelligence",
        """Pre-H-Hour intelligence build: H-30d OCCASUS deployment indicators (74th SP FA + 75th MB
movement to forward positions), H-14d 72nd MRD assembly at FAA URAL-CENTRAL/NORTH,
H-7d Iskander-M battery-displacement training, H-3d V Corps deployment EEFI compromise
via Cinereus disinformation. H-Hour: 11 0000L AUG 2026.""",
    ),
    "ANNEX_A_TASKORG": _doc(
        "Annex A — Task Organization",
        "annex",
        """Annex A Task Organization: V Corps (LTG Richardson) — SOUTH Main Effort 2CR
(3 SQDN + HHT + FAS/TF Hell + ROC-T at ATK PSN VENUS + Italian WGX BG OPCON),
CENTER Supporting 1ABCT, NORTH 1CD (3 CAV SQDN + main body theater reserve),
FINLAND 173d ABCT OPCON FINDEFCOM, GS 210th FA BDE (DIVARTY), Avn 11th CAB,
Sustainment 21st TSC FWD with RSS Nodes X/Y/Z.""",
    ),
    "ANNEX_B_INTEL": _doc(
        "Annex B — Intelligence",
        "annex",
        """Annex B Intelligence: Enemy MLCOA = simultaneous multi-front fixing attacks
(7th CAA two-echelon assault on OBJ JACKSON via Elk corridor; 8th CAA fixes
1ABCT in Silmaris Gap; 750th SPF interdicts TRM corridor; Cinereus EW jams
from H-Hour). Enemy MDCOA = 8th CAA massed assault Lithuania axis + 47th Abn
air assault to isolate 2CR. NAIs 201-215 cover 7th CAA assembly + FAA SOKOL.
CCIRs: CCIR 1 (72nd MRD main effort axis), CCIR 2 (75th MB TEL positioning),
CCIR 3 (71st MID departure from SOKOL).""",
    ),
    "ANNEX_C_OPS_OVERLAY": _doc(
        "Annex C — Operations Overlay",
        "annex",
        """Annex C Operations Overlay: OBJ JACKSON (34U EE 64392 54490, 2.5 km radius);
PL YELLOW (LD/LC) N-S at 22.22°E; PL GOLD security area N-S at 22.05°E;
PL BLUE battle-handover N-S at 21.88°E; FSCL N-S at 22.30°E;
EA-1 (Drygały axis 34U EE 73086 49166), EA-2 (Nitki axis 34U EE 73949 52674),
EA-3 (Klusy / DK16 main effort 34U EE 72129 61158); CP TRIX (2CR TAC)
34U EE 62500 52500; DIVARTY PAA 34U EE 58250 56900; ATK PSN VENUS 35U MA 22422 98926;
TAA BRAVO 34U EE 58000 50000; Italian WGX Area 34U EE 62000 48500;
RSS Nodes X/Y/Z (34U EE 60000 53500 / 34U EE 60000 51000 / 34U EE 58800 56200);
BPTA box 34U EE 57000 46000 -> 34U EE 78000 62000.
FAA URAL-NORTH 34U EE 99822 75691 (~41 km ENE), FAA URAL-CENTRAL 34U FE 02035 64609
(~39 km ENE), FAA SOKOL 34U FE 13198 65979 (~50 km ENE). All coords MGRS-verified
against the prompt PDF iteration #7; see scratch/ibl/joe-test/vcorps-compaction-log.md.""",
    ),
}
