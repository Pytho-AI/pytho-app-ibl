"""Doctrinal equipment lookup tables — speed, category, role.

Used by ``cougaar_adapters.get_red_oob_resolved`` to:

  1. Derive a unit's rate-of-march from its organic composition (slowest
     self-propelled element governs the column — FM 4-01.30, TC 7-100.2).
  2. Map equipment names → FM 3-60 App. C target value categories (HVT
     classification for HVTL / CARVER / HPTL).
  3. Fingerprint clusters by composition (TF-IDF over equipment vectors).

This module is intentionally a flat lookup file the analyst can edit
without touching adapter code. Speeds are SUSTAINED road km/h per
doctrine; cross-country is applied by callers as ``0.5 * road``. Items
that don't move under their own power (trailers, towed pods, manportable
weapons) return ``None`` so they don't constrain the min-of-column.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Per-equipment SUSTAINED road speed (km/h).
# ---------------------------------------------------------------------------
# Citations: FM 4-01.30 Movement Operations; TC 7-100.2 OPFOR Tactics
# (Ch. 3 movement); FM 3-90 / FM 3-21.10 planning factors; Janes Land
# Warfare Platforms; NGIC datasheets for Donovian/OPFOR equipment.
#
# Names match the populated ``data__name`` (or ``data__equipmentName``)
# values observed in the IDX 26 cougaar Equipment vertex pool. New names
# can be added here without touching adapter logic.

EQ_SPEED: dict[str, int] = {
    # === Tracked main combat ===
    "T 90A":                                   30,
    "T 90A ATGM":                              30,
    "TANK T 90A PLOW":                         28,
    "T 72B":                                   30,
    "T 72 PLOW":                               28,
    "BMP 2 IFV":                               30,
    "BMP 3 IFV":                               30,
    "AIFV,TRACK,AMPHIB,BMP1":                  30,
    "BMP 1 KSH":                               30,
    "BMP 1 KSH RDR INTERCEPT DF":              30,
    "ATGM BMP 3 TRKD AT 15":                   30,
    "ARV BREM 2 TRKD":                         25,
    "ARV,TRACK,AMPHIB,BREM2":                  25,
    "ARV T 55T TRKD":                          25,
    "ARV,TRACK,NONAMPHIB,T55T":                25,

    # === SP artillery (tracked) ===
    "HOW SP 2S19 TRKD 152MM":                  30,
    "120MM SP MORTAR 2S9":                     35,
    "COMBO 2S9 TRKD 120MM":                    35,
    "120MM SP MORTAR 2S23":                    35,
    "2B16 120MM COMBO GUN":                    25,
    "125MM ATG 2A45M":                         25,  # towed; prime-mover limited

    # === MLRS ===
    "9A51 PRIMA MRL TORRIKE":                  60,
    "BM-30 9K58 SMERCH":                       40,
    "BM-21 GRAD":                              60,
    "BM-27 URAGAN":                            55,

    # === Air defense ===
    "LCHR PANTSIR S1 SA 22 GREYHOUND TELAR WHL": 40,
    "ADA,MIXED,TRACKED,PANTSIRS1":             35,
    "LCHR 2S6 30MM SA 19 TRKD":                35,
    "2S6 TRANSLOADER":                         35,
    "VEH,MSL TRNSLDR,SP,CSA12":                40,
    "MSL SYS,ATGM,HOT 3":                      45,
    "ADA,SAM SYS,CROTALE":                     40,
    "RDR CROTALE P4R RADAR SYSTEM":            40,
    "DOG EAR RADAR TORRIKE":                   35,
    "GIRAFFE 40 TA RADAR":                     35,
    "RADAR,SS,GIRAFFE 40":                     35,
    "RDR CB GM 5951 IL220 TRKD":               25,

    # === Recon / RISTA ===
    "BRM 3K RECON VEHICLE":                    50,
    "PRP 4":                                   50,
    "TRK UAZ 469B DIM MINE DETECTOR WHL":      60,
    "UAZ 452D AMBULANCE":                      70,
    "RQ7 SHADOW UAV":                          80,
    "UAV,RECON,ASN207":                        80,
    "UAV,NRIST S100":                          80,
    "TRANSP VEH,UAV,6X6,ASN207":               60,
    "ZALA 421 08M UAV":                        80,

    # === Aviation (flight speed; the unit-as-a-whole moves at ground tail rate) ===
    "MI35 EXPORT HIND E":                     270,
    "MI24RSH HIND G1 NBC ARMORED":            260,
    "MI 8 HIP":                               230,
    "KA 52 HOKUM":                            300,

    # === C2 / signal vans ===
    "URAL 375D C3 VAN":                        55,
    "GAZ 66 SIGNAL VAN":                       65,
    "GAZ 66 SATCOM VEH":                       65,
    "GAZ 66 RADIO RELAY":                      65,
    "GAZ 66 COMMO TRK":                        65,
    "ACV MTLB 1V14 TRKD BTY CP":               30,
    "MTLB 1V14 TRKD CP":                       30,
    "MTLB 1V13 TRKD BTY FDC":                  30,
    "ACV MTLB 1V15 TRKD BN CP":                30,
    "MTLB 1V13 FDC":                           30,
    "MTLBU":                                   30,
    "ACV MTLB 1V14 TRKD CP":                   30,
    "MTLB 1V16 TRKD BN FDC":                   30,
    "APC MTLB TRKD CLASS":                     30,
    "ACV BTR 80K M1981 WHL":                   40,
    "APC BTR 80 UNSH WHL":                     40,
    "ACV,8X8,AMPHIB,BTR80 SERIES":             40,
    "BTR 90":                                  45,
    "BTR 80 RKHM 4 CBRN":                      40,
    "ARC CBR BTR 80 RKHM 4 WHL":               40,

    # === Engineer ===
    "PMP BRIDGE":                              25,
    "TMM BRIDGING VEH":                        25,
    "MT 55A AVLB":                             25,
    "AVLB,SCISSOR,MT55A":                      25,
    "UR 77 MINEFIELD BREACHING VEH":           15,
    "MINE LAY,PLOW,TWD,PMZ4":                  15,
    "KMT 7 MINE CLEARING ROLLER":              15,
    "UMZ MINELAYER":                           30,
    "GMZ 3 MINE LAYER":                        30,
    "AEV IMR 2 COMBAT TRKD":                   25,
    "ARMD EN VEH IMR 2":                       25,
    "MTU 72":                                  30,

    # === Wheeled trucks ===
    "TRK CGO URAL 375 WHL 4000KG":             60,
    "URAL 375 PKM":                            60,
    "URAL 375 DSHK":                           60,
    "URAL 375D MAINTENANCE VAN":               55,
    "URAL 375D 6X6 WRECKER":                   50,
    "TRK CGO URAL 5323 8X8 9000KG WHL":        60,
    "TRK,CARGO,6X6,4 TON,URAL375DK1":          60,
    "TRK,CARGO,6X6,5 TON,ZIL131":              60,
    "TRK,CARGO,4X4,5 TON,STALLION M":          65,
    "TRK,CARGO,4X4,5 TON,STALLION MK3":        65,
    "TRK,CARGO,6X6,9 TON,KRAZ260V":            50,
    "TRK TRCT,6X6,9 TON,KRAZ260V":             50,
    "TRK CRANE KRAZ 255 WHL":                  45,
    "TRK FUEL URAL 375 WHL":                   60,
    "TRK FUEL KRAZ 260 9000L WHL":             50,
    "TRK TCTR MAZ 537 WHL":                    45,
    "TRK WATER KRAZ 255B WHL 9500L":           50,
    "GENERIC MESS TRK WHL":                    55,
    "GENERIC TRK FUEL WHL":                    55,
    "GENERIC TRK WATER WHL":                   55,
    "GENERIC TRK WATER PUR WHL":               55,
    "GAZ 66":                                  70,
    "GAZ 66 DSHK":                             70,
    "GAZ 66 PKM":                              70,
    "GAZ 2330 TIGR":                           80,
    "GAZ 2330 TIGR DSHK":                      80,
    "GAZ 2330 TIGR PKM":                       80,
    "GAZ 2330 TIGR AGL":                       80,
    "TRK UTIL GAZ 2330 TIGR WHL":              80,
    "MOTORCYCLE,3X1,URAL M72":                 80,
    "MOTORCYCLE IMZ 8 WHL PKM":                80,
    "AMBULANCE,0.25 TON,(US)":                 70,
    "BMM 1 AMBULANCE":                         65,
    "TRUCKMOUNTED FIELD KITCHEN,KSP":          50,
    "TRUCKMOUNTED FIELD KITCHEN,KSP200":       50,
    "FUEL,STORAGE TANK TRUCK":                 50,
    "VEH,NFI":                                 60,
    "BRDM 2 HOT 3":                            65,
    "AMPHIBIAN,TRACK,PTS":                     15,
}


# ---------------------------------------------------------------------------
# Per-equipment effective engagement range (meters).
# ---------------------------------------------------------------------------
# Doctrinal weapon-effects radius per major OPFOR equipment family. Values
# are the catalog effective range — what the SITEMP renderer draws as a
# concentric envelope around each templated unit per piece of organic
# equipment in its composition. Same key set as EQ_SPEED (nomenclature
# strings from the IDX 26 cougaar Equipment vertex pool).
#
# Citations: FM 3-09 Field Artillery Operations, FM 3-01 US Army Air and
# Missile Defense Operations, TC 7-100.2 OPFOR Tactics, NGIC datasheets,
# Jane's Land Warfare Platforms — Artillery & Air Defense. Where multiple
# variants exist (HE base vs extended-range RAP, gun vs missile range),
# the value chosen is the EFFECTIVE range against the typical target the
# system threatens at SITEMP scope, not the maximum publicly-reported
# range.
#
# Equipment NOT in this table emits NO range fan. Trucks, signal vans,
# engineer vehicles, manportable launchers, and short-range support items
# are intentionally absent — the SITEMP renders the THREAT'S stand-off
# envelopes, not the unit's perimeter security.
EQ_ENGAGEMENT_RANGE_M: dict[str, int] = {
    # === SP artillery ===
    "HOW SP 2S19 TRKD 152MM":                  24700,  # 2S19 Msta-S, OF-45 HE
    "120MM SP MORTAR 2S9":                     8855,   # 2S9 Nona-S
    "COMBO 2S9 TRKD 120MM":                    8855,
    "120MM SP MORTAR 2S23":                    8855,   # 2S23 Nona-SVK
    "2B16 120MM COMBO GUN":                    12800,  # 2B16 Nona-K (RAP)

    # === Anti-tank guns ===
    "125MM ATG 2A45M":                         4000,   # 2A45M Sprut-B, AP

    # === MLRS ===
    "9A51 PRIMA MRL TORRIKE":                  20500,  # Prima 50-tube 122mm
    "BM-21 GRAD":                              20400,  # M-21OF rocket
    "BM-27 URAGAN":                            35800,  # 9M27F
    "BM-30 9K58 SMERCH":                       70000,  # 9M55K

    # === Air defense — surface to air ===
    "LCHR PANTSIR S1 SA 22 GREYHOUND TELAR WHL": 20000,  # 57E6E SAM
    "ADA,MIXED,TRACKED,PANTSIRS1":             20000,
    "LCHR 2S6 30MM SA 19 TRKD":                8000,    # 9M311 (SA-19)
    "2S6 TRANSLOADER":                         8000,    # paired with 2S6
    "VEH,MSL TRNSLDR,SP,CSA12":                25000,   # carrier for SA-12
    "ADA,SAM SYS,CROTALE":                     11000,   # Crotale R440
    "LCHR 9A310M1 SA 11 GADFLY TELAR TRKD":    35000,   # 9M38 (SA-11 Buk)

    # === Tanks (gun-launched ATGM is the doctrinal SITEMP envelope —
    # main-gun direct fire is implied by the SIDC symbol itself). ===
    "T 90A":                                   5000,   # 9M119 Refleks
    "T 90A ATGM":                              5000,
    "TANK T 90A PLOW":                         5000,

    # === IFV-mounted ATGM ===
    "BMP 1 IFV":                               3000,   # 9M14 Malyutka
    "BMP 2 IFV":                               4000,   # 9M113 Konkurs
    "IFV BMP 2M TRKD":                         4000,   # modernized Konkurs
    "ATGM BMP 3 TRKD AT 15":                   6000,   # 9M123 Khrizantema-S

    # === Anti-tank guided missile platforms ===
    "MSL SYS,ATGM,HOT 3":                      4300,   # HOT 3
    "BRDM 2 HOT 3":                            4300,
    "TIGR GAZ 2330 WITH AT14 ATGM":            5500,   # 9M133 Kornet

    # === Aviation — air-to-ground ATGM ===
    "MI35 EXPORT HIND E":                      8000,   # 9M120 Ataka
    "MI24RSH HIND G1 NBC ARMORED":             8000,
    "KA 52 HOKUM":                             12000,  # 9K121 Vikhr-1
}

# Doctrinal subkind classification per equipment, mapping to the SITEMP
# sub-toggle "Range fan classes" weapon-family checkboxes (see
# OperationsMap.tsx SUB_TOGGLE_DEFS.SITEMP subkind dimension). Drives
# drawRangeFan styling and analyst filtering by weapon family.
EQ_RANGE_SUBKIND: dict[str, str] = {
    # Indirect fire (artillery / mortars)
    "HOW SP 2S19 TRKD 152MM":                  "indirect_fire",
    "120MM SP MORTAR 2S9":                     "indirect_fire",
    "COMBO 2S9 TRKD 120MM":                    "indirect_fire",
    "120MM SP MORTAR 2S23":                    "indirect_fire",
    "2B16 120MM COMBO GUN":                    "indirect_fire",
    "125MM ATG 2A45M":                         "antitank",

    # Multiple-rocket launchers
    "9A51 PRIMA MRL TORRIKE":                  "mlrs",
    "BM-21 GRAD":                              "mlrs",
    "BM-27 URAGAN":                            "mlrs",
    "BM-30 9K58 SMERCH":                       "long_range",

    # Surface to air
    "LCHR PANTSIR S1 SA 22 GREYHOUND TELAR WHL": "ada",
    "ADA,MIXED,TRACKED,PANTSIRS1":             "ada",
    "LCHR 2S6 30MM SA 19 TRKD":                "ada",
    "2S6 TRANSLOADER":                         "ada",
    "VEH,MSL TRNSLDR,SP,CSA12":                "ada",
    "ADA,SAM SYS,CROTALE":                     "ada",
    "LCHR 9A310M1 SA 11 GADFLY TELAR TRKD":    "ada",

    # Anti-tank — tanks (gun-ATGM), IFV-ATGM, dedicated ATGM platforms,
    # attack helo ATGM. SITEMP analyst will toggle these together when
    # studying the threat's anti-armor coverage.
    "T 90A":                                   "antitank",
    "T 90A ATGM":                              "antitank",
    "TANK T 90A PLOW":                         "antitank",
    "BMP 1 IFV":                               "antitank",
    "BMP 2 IFV":                               "antitank",
    "IFV BMP 2M TRKD":                         "antitank",
    "ATGM BMP 3 TRKD AT 15":                   "antitank",
    "MSL SYS,ATGM,HOT 3":                      "antitank",
    "BRDM 2 HOT 3":                            "antitank",
    "TIGR GAZ 2330 WITH AT14 ATGM":            "antitank",
    "MI35 EXPORT HIND E":                      "antitank",
    "MI24RSH HIND G1 NBC ARMORED":             "antitank",
    "KA 52 HOKUM":                             "antitank",
}


def equipment_engagement_range_m(name: str | None) -> int | None:
    """Return effective engagement range (m) for a piece of equipment, or
    None if the equipment doesn't get a range fan on the SITEMP.

    Names not in EQ_ENGAGEMENT_RANGE_M return None — caller skips emission
    rather than rendering a generic envelope. Per CLAUDE.md "no fabricated
    data": better to omit a fan than to paint a guessed radius.
    """
    if not name:
        return None
    return EQ_ENGAGEMENT_RANGE_M.get(name)


def equipment_range_subkind(name: str | None) -> str | None:
    """Return doctrinal weapon-family subkind for a piece of equipment.

    Drives drawRangeFan styling + analyst sub-toggle filtering by class
    (ada / indirect_fire / mlrs / long_range / antitank). Returns None if
    no classification — the fan renders in default styling.
    """
    if not name:
        return None
    return EQ_RANGE_SUBKIND.get(name)


# Trailers / towed pods / weapons / generators — no own propulsion. These
# return None from ``equipment_road_speed_kmh`` so they don't poison the
# slowest-element rule.
_NON_PROPELLED_PREFIXES = (
    "TRL ",            # generic trailer
    "TRLR",            # trailer alt spelling
    "POL POD",
    "POL CNTR",
    "GENERIC TRL",
    "TRL CGO",
    "TRL GEN",
    "TRL WATER",
    "TRL HET",
    "GENERATOR TRL",
    "TRLR,FLD",
    "PKP AMPHIBIOUS TRL",
    # Personal/crew-served weapons
    "GL ",             # grenade launcher
    "GL RPG",
    "GL FARA",
    "GL FO",
    "GL RTO",
    "GL PANZERFAUST",
    "GL SA",
    "GL ARMBRUST",
    "GL GP",
    "GL AT",
    "GL 35MM",
    "GL 60MM",
)


def equipment_road_speed_kmh(name: str | None) -> int | None:
    """Return SUSTAINED road km/h for a piece of equipment, or None if it
    doesn't move under its own power (trailers, towed pods, weapons).

    Returns ``None`` so the caller's ``min()`` over a unit's composition
    skips non-propelled items rather than zeroing the rate.
    """
    if not name:
        return None
    if name in EQ_SPEED:
        v = EQ_SPEED[name]
        return v if v and v > 0 else None
    upper = name.upper()
    for pref in _NON_PROPELLED_PREFIXES:
        if upper.startswith(pref) or pref in upper:
            return None
    # Heuristic fallback by keyword
    if any(k in upper for k in ("HELICOPTER", "HELO", "MI-", "MI ", "KA ", "HIND", "HOKUM")):
        return 250
    if any(k in upper for k in ("TANK ",)):
        return 30
    if "TRKD" in upper or "TRACK" in upper:
        return 30
    if "WHL" in upper or "WHEEL" in upper:
        return 55
    return 40  # generic vehicle default


def derive_unit_rate_of_march(composition_counts: dict[str, int]) -> int:
    """Doctrinal rate-of-march for a unit, derived from its organic equipment.

    Rule: a column moves at the speed of its slowest self-propelled organic
    element (FM 4-01.30 + TC 7-100.2). Non-propelled items (trailers, pods,
    weapons) are excluded because the tow vehicle's speed already governs
    them.

    Mixed ground+air formations: the GROUND TAIL governs because aviation
    units still have organic ground support (fuel trucks, command vehicles,
    maintenance vans) that have to move with the unit. Only when the entire
    composition is aviation-pure (no ground elements at all) does flight
    speed apply.
    """
    if not composition_counts:
        return 25  # planning-factor fallback
    speeds: list[int] = []
    for name in composition_counts:
        s = equipment_road_speed_kmh(name)
        if s is not None:
            speeds.append(s)
    if not speeds:
        return 25
    # Aviation-pure check: only when every contributing speed is >=200
    if all(s >= 200 for s in speeds):
        return max(speeds)
    return min(speeds)


# ---------------------------------------------------------------------------
# FM 3-60 App. C target value category mapping.
# ---------------------------------------------------------------------------
# Used to bucket each equipment item by doctrinal HVT category for HVTL
# rollup. Match is by keyword/substring against ``data__name``; the first
# matching category wins (table is ordered most-specific first).

_HVT_FAMILY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("AirDefense", (
        "SAM", "ADA", "PANTSIR", "CROTALE", "SA-19", "SA-22", "SA-",
        "MANPADS", "VEH,MSL TRNSLDR", "MSL SYS,ATGM", "2S6", "CSA12",
        "MIXED,TRACKED,PANTSIRS1", "LCHR",
    )),
    ("FireSupport", (
        "MLRS", "SMERCH", "URAGAN", "GRAD", "2S19", "2S3", "MSTA",
        "HOW SP", "HOW ", "MRL", "MORTAR", "ARTY", "BM-21", "BM-27",
        "BM-30", "TOS", "120MM SP", "152MM", "PRIMA", "9A51", "2B16",
        "2S9", "2S23", "125MM ATG",
    )),
    ("EW_REC", (
        "KRASUKHA", "LEER", "BORISOGLEBSK", "SHTORA", "MURMANSK",
        "RB-", "JAMMER", "BMP 1 KSH", "IL220", "IL-220", "RDR CB GM",
        "RDR CB", "EW ",
    )),
    ("Radar", (
        "DOG EAR", "GIRAFFE", "RDR CROTALE", "RADAR,SS",
    )),
    ("C2_Signal", (
        "SATCOM", "SIGNAL VAN", "C3 VAN", "C3D", "MTLB 1V14",
        "MTLB 1V13", "MTLB 1V15", "MTLB 1V16", "MTLBU", "BTY FDC",
        "BTY CP", "BN CP", "BN FDC", "ACV BTR 80K", "ACV MTLB",
        "RADIO RELAY", "COMMO TRK",
    )),
    ("Engineer", (
        "MINE LAY", "BRIDGE", "PONTOON", "MT55", "TMM", "AVLB",
        "BREACH", "UR-77", "UR 77", "PMP", "MTU", "IMR", "UMZ",
        "GMZ", "MINEFIELD", "MINELAYER", "MINE DETECTOR",
        "MINE CLEARING", "PMZ",
    )),
    ("RISTA_UAV", (
        "UAV", "SHADOW", "ZALA", "PRP 4", "BRM 3K", "BRM-3K",
        "RECON VEHICLE", "NRIST", "ASN207", "ORLAN",
    )),
    ("Lift", (
        "MI24", "MI 24", "MI35", "MI 35", "HIND", "MI 8", "MI8",
        "KA 52", "KA52", "HOKUM",
    )),
    ("Maneuver", (
        "T 72", "T 90", "T-72", "T-90", "T-80", "BMP 2", "BMP 3",
        "BMP-", "BMD-", "BTR 80", "BTR 90", "AIFV", "TANK T",
    )),
    ("Sustainment", (
        "AMBULANCE", "FIELD KITCHEN", "MAINTENANCE", "WRECKER",
        "SUPPLY",
    )),
    ("POL", (
        "TRK FUEL", "POL POD", "POL CNTR", "FUEL,STORAGE",
    )),
    ("Transport", (
        "TRK CGO", "TRK,CARGO", "TRK CRANE", "TRK UTIL", "TRK TCTR",
        "TRK TRCT", "TRK WATER", "TRK UAZ", "TRL CGO", "TRL GEN",
        "TRL WATER", "TRLR", "GAZ 66", "GAZ 2330", "URAL 375",
        "URAL 4320", "URAL 5323", "ZIL", "KRAZ", "STALLION",
        "MOTORCYCLE", "M103A3", "MAZ 537",
    )),
]


def fm360_category(equipment_name: str | None) -> str | None:
    """Map an equipment name to its FM 3-60 App. C target value category.

    Returns the first matching category, or None for unclassifiable items
    (personal weapons, generic vehicles without doctrinal HVT shape).
    """
    if not equipment_name:
        return None
    upper = equipment_name.upper()
    for cat, kws in _HVT_FAMILY_KEYWORDS:
        for kw in kws:
            if kw.upper() in upper:
                return cat
    return None


# ---------------------------------------------------------------------------
# HVT-worthiness predicate (subset of categories that are doctrinally HVT).
# ---------------------------------------------------------------------------
# Transport, Sustainment, POL, and unclassified items are NOT HVTs on
# their own. POL and Sustainment can become HVTs at the FACILITY level
# (fuel depot, ammo dump) but not as individual vehicles. Same with
# Transport — a truck isn't an HVT; a transportation chokepoint or rail
# yard is. The classifications stay in fm360_category so they show up in
# the composition rollup (analyst can see "664 trucks") but get filtered
# out of the HVT pool.

HVT_WORTHY_CATEGORIES: frozenset[str] = frozenset((
    "AirDefense", "FireSupport", "EW_REC", "C2_Signal", "Engineer",
    "RISTA_UAV", "Lift", "Radar", "Maneuver",
))


# ---------------------------------------------------------------------------
# FM 3-60 App. C target value category mapping.
# ---------------------------------------------------------------------------
# The fm360_category() classifier returns cougaar-internal capability buckets
# (AirDefense, FireSupport, EW_REC, ...). The published HVTL category column
# uses TARGET_CATEGORY_ENUM (templates/common.py) — the FM 3-60 App. C
# canonical categories an analyst recognizes. This mapping is the bridge.
#
# Used by intel_agent._build_skeleton when expanding the writer's per-unit
# HVT pick into per-equipment rows: each equipment item's FM 3-60 category
# is derived from its name via fm360_category(), then projected through
# this map onto the published enum.
FM360_TO_TARGET_CATEGORY: dict[str, str] = {
    "AirDefense":    "air_defense_artillery",
    "FireSupport":   "fire_support",
    "EW_REC":        "cyberspace_electromagnetic_activities",
    "C2_Signal":     "command_and_control",
    "Engineer":      "engineer",
    "RISTA_UAV":     "reconnaissance_and_surveillance",
    "Radar":         "reconnaissance_and_surveillance",
    "Lift":          "lift",
    "Maneuver":      "maneuver",
    "Sustainment":   "sustainment",
    "POL":           "sustainment",
    "Transport":     "line_of_communication",
}


# Per-request override for HVT_WORTHY_CATEGORIES — set via contextvar
# from /api/products/generate when the analyst supplies a config in the
# Generate gear (HVTL schema config_fields.hvt_worthy_categories).
# is_hvt_worthy() reads this when present, falls back to the module
# default. Lets the analyst opt POL / Sustainment / Transport into the
# HVT pool without a code change — the 'fuel depot is the HVT not the
# 600 tanks' use case.
from contextvars import ContextVar
HVT_WORTHY_CATEGORIES_OVERRIDE: ContextVar[frozenset[str] | None] = ContextVar(
    "HVT_WORTHY_CATEGORIES_OVERRIDE", default=None
)

# COA role-badge gate. Default False — Threat COA Statement docx + Threat
# COAs PPTX render COA-1 / COA-2 / COA-3 / COA-4 as bare numbered headings
# without the "[MOST LIKELY]" / "[MOST DANGEROUS]" / "[MOST DECEPTIVE]"
# inline pre-labels (analyst direction: badges are editorial overlay, not
# the four COAs themselves). The rank / most_dangerous / most_deceptive
# schema fields remain so probability_rationale narrative can reference
# them; only the heading badge is suppressed. Set via the gear-button
# config_fields entry on the Threat COA Statement + Threat COAs schemas,
# threaded through main._generate_with_config the same way the
# HVT_WORTHY_CATEGORIES_OVERRIDE is wired for HVTL. Read in exporter.py
# at the badge-emission sites (one for TCOAS docx headings, one for the
# Threat COAs PPTX title bar).
SHOW_COA_ROLE_BADGES_OVERRIDE: ContextVar[bool | None] = ContextVar(
    "SHOW_COA_ROLE_BADGES_OVERRIDE", default=False
)


def is_hvt_worthy(equipment_name: str | None) -> bool:
    """True when equipment falls into a doctrinal HVT capability category.

    Honors HVT_WORTHY_CATEGORIES_OVERRIDE when set in the current
    contextvar scope (per-request analyst config). Default behavior
    (module-level HVT_WORTHY_CATEGORIES) preserved when no override."""
    cat = fm360_category(equipment_name)
    if not cat:
        return False
    override = HVT_WORTHY_CATEGORIES_OVERRIDE.get()
    if override is not None:
        return cat in override
    return cat in HVT_WORTHY_CATEGORIES


# ---------------------------------------------------------------------------
# Echelon ordinal — for echelon-match scoring in attribution.
# ---------------------------------------------------------------------------

ECHELON_ORDINAL: dict[str, int] = {
    "DIV": 6, "DIVISION": 6,
    "BDE": 5, "BRIGADE": 5, "REGT": 5, "REGIMENT": 5,
    "BN": 4,  "BATTALION": 4, "SQUADRON": 4,
    "CO": 3,  "COY": 3, "COMPANY": 3, "TROOP": 3,
    "BTRY": 3, "BATTERY": 3,
    "PLT": 2, "PLATOON": 2,
    "SEC": 1, "SECTION": 1,
}


def echelon_ord(echelon: str | None) -> int:
    """Map an echelon string to its doctrinal ordinal (DIV=6 .. SEC=1).

    Unknown / empty echelons return 4 (battalion-equivalent) — the most
    common echelon in OPFOR OOB, used as the safer middle of the scoring
    distribution rather than the bottom.
    """
    if not echelon:
        return 4
    return ECHELON_ORDINAL.get(echelon.upper(), 4)


__all__ = [
    "EQ_SPEED",
    "EQ_ENGAGEMENT_RANGE_M",
    "EQ_RANGE_SUBKIND",
    "FM360_TO_TARGET_CATEGORY",
    "equipment_road_speed_kmh",
    "equipment_engagement_range_m",
    "equipment_range_subkind",
    "derive_unit_rate_of_march",
    "fm360_category",
    "is_hvt_worthy",
    "HVT_WORTHY_CATEGORIES",
    "ECHELON_ORDINAL",
    "echelon_ord",
]
