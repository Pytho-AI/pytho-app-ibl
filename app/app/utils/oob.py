"""Resolved order-of-battle + geo-agent OOB sync payloads — LOCAL sources only.

The Cougaar feed is DEAD and fully disconnected (2026-07-09): no
GraphQL client, no poller, no remote round-trips anywhere in this
module. The authoritative RED resolved OOB is the on-disk snapshot the
last live fusion pass persisted (oob_resolved_latest.json — the same
file product_autodraft._resolve_kg_record_inline and the autofire
target resolvers already read); BLUE units, spatial overlays, and
terrain come from scenario data. When per-project scenario data lands,
the snapshot becomes part of the project's scenario document.

Everything here was extracted verbatim from utils/cougaar_adapters.py
(the dead module keeps its git history; nothing imports it anymore):
LIVE_OOB_SOURCE, _SPATIAL_CONTROL_KIND_MAP, and
build_geo_agent_sync_payload are byte-identical moves.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger(__name__)

# Source token every OOB sync push tags its rows with — the geo-agent
# /sync endpoint transactionally REPLACEs by this token, and the
# Tactical Overlays sidebar's Units row toggles it.
LIVE_OOB_SOURCE = "live_oob"

def get_red_oob_resolved_sync() -> dict:
    """The ACTIVE project's resolved RED OOB payload (named_units with
    composition rollups, kg ids, categories, attributed positions) —
    the RESOLVED_OOB key of the project's scenario document. Raises
    KeyError when the project's dataset carries none (events without a
    fused-OOB snapshot run on the scenario KG floor — tool_get_oob's
    fixture path handles that loudly)."""
    from utils.project_context import active_project
    from utils.project_scenario import scenario_for
    ns = scenario_for(active_project())
    payload = getattr(ns, "RESOLVED_OOB", None)
    if not isinstance(payload, dict):
        raise KeyError(
            f"project {active_project()!r} scenario document has no "
            "RESOLVED_OOB payload")
    # Normalize: the on-disk snapshot wraps the resolved payload
    # ({"resolved": {...}}); consumers read named_units at the top
    # level (the shape the live fusion pass used to RETURN).
    if "named_units" not in payload and isinstance(payload.get("resolved"), dict):
        return payload["resolved"]
    return payload


async def get_red_oob_resolved() -> dict:
    return get_red_oob_resolved_sync()


def resolved_named_units() -> list:
    """named_units from the active project's RESOLVED_OOB — the shared
    read the target/HVT resolvers join against. Empty list when the
    project has no resolved snapshot (resolvers fall through to their
    scenario-KG tiers)."""
    try:
        payload = get_red_oob_resolved_sync()
    except Exception:
        return []
    return list((payload.get("resolved") or payload).get("named_units")
                 or payload.get("named_units") or [])


# entry per actionType in _CONSUMED_ACTION_TYPES — extending either requires
# extending both. The paint-layer kind values match what
# ui/src/geo-agent/utils/tacticalLayers.maplibre.js dispatches into the
# map.__mcoo* stashes consumed by the SVG drawers in tacticalGraphics.svg.js.
_SPATIAL_CONTROL_KIND_MAP: dict[str, tuple[str, str | None]] = {
    "FORWARD_LINE_OF_OWN_TROOPS":  ("boundary",                    "flot"),
    "NAMED_AREA_OF_INTEREST":      ("named_area_of_interest",      None),
    "TARGETED_AREA_OF_INTEREST":   ("targeted_area_of_interest",   None),
    "PHASE_LINE":                  ("phase_line",                  None),
    "OBJECTIVE":                   ("objective",                   None),
    "NO_GO_AREA":                  ("severely_restricted_terrain", None),
    "SLOW_GO_AREA":                ("restricted_terrain",          None),
    "MAIN_AXIS_OF_ADVANCE":        ("direction_of_attack",         "main"),
    "SUPPORTING_AXIS_OF_ADVANCE":  ("direction_of_attack",         "supporting"),
}


def build_geo_agent_sync_payload(
    *,
    control_measures: list,
    units: list,
    targets: list,
    unidentified_formations: list | None = None,
) -> dict:
    """Translate already-transformed Cougaar dicts (output of _transform()
    via the adapter functions get_spatial_overlay / get_oob / get_target_set)
    into the geo-agent /api/v1/maps/{id}/sync request body.

    This lives next to `_transform` because it's the same conceptual layer:
    Cougaar↔external-format translation. The intermediate Pydantic-validated
    dict shape isn't an end consumer in itself — the writer LLM and the
    geo-agent paint layer both want different projections of it. _transform
    deserializes Cougaar GraphQL; this function emits the geo-agent overlay/
    entity shape. Both knowledge sets (Cougaar action_type values, geo-agent
    kind values) sit in one module so a Cougaar-side schema change touches
    exactly one file.

    Returns: {"overlays": [...], "entities": [...]} matching the request
    body documented at backend/app/api/v1/maps.py sync_map_layer.
    The endpoint will REPLACE every row on the target map whose
    attrs.source equals the ?source= query param with this batch."""
    # BDA status overlay — read once per sync push. Maps unit kg_id ->
    # disposition string (DESTROYED / DEGRADED / DAMAGED / …) that the
    # frontend SIDC operational-condition amplifier renders. Written by
    # handlers/bda_autofire.py on every successful BDA fire that came
    # from a RECCEXREP carrying a non-blank target_status.
    _BDA_STATUS_OVERLAY: dict[str, str] = {}
    try:
        import os as _os, json as _json
        _OVERLAY_PATH = "/app/logs/bda_status_overlay.json"
        if _os.path.exists(_OVERLAY_PATH):
            with open(_OVERLAY_PATH, "r") as _f:
                _raw = _json.load(_f) or {}
            for _k, _v in _raw.items():
                if isinstance(_v, dict):
                    _d = (_v.get("disposition") or "").strip()
                    if _d:
                        _BDA_STATUS_OVERLAY[_k] = _d
    except Exception as _e:
        log.debug("BDA status overlay read failed: %s", _e)

    overlays: list[dict] = []
    for cm in (control_measures or []):
        kbid = cm.get("uuid")
        mapped = _SPATIAL_CONTROL_KIND_MAP.get(cm.get("action_type"))
        geom = cm.get("coordinates")
        if not (kbid and mapped and geom):
            continue
        kind, subkind = mapped
        attrs = {
            "source": LIVE_OOB_SOURCE,
            "source_kbid": kbid,
            "action_type": cm.get("action_type"),
            "description": cm.get("description"),
            "color": cm.get("color"),
            "region_status": cm.get("region_status"),
        }
        if subkind:
            attrs["subkind"] = subkind
        overlays.append({
            "kind": kind,
            "name": cm.get("name") or cm.get("description") or kbid[:8],
            "geom": geom,
            "attrs": {k: v for k, v in attrs.items() if v is not None},
        })

    # Lazy MGRS dep — only needed for the scenario-fixture T1 path.
    _mgrs_to_latlon = None
    entities: list[dict] = []
    for u in (units or []):
        # Two input shapes converge here:
        #   - Cougaar IDXRedUnit transform: {uuid, location (GeoJSON Point),
        #     sidc, unit_short_name, unit_long_name, force_type, echelon, ...}
        #   - Scenario-fixture T1 shape (from _t1_unit / fallback path):
        #     {id, name, category, echelon, location_mgrs, parent, ...}
        # The converter previously hard-required uuid+location, which
        # silently dropped every scenario-fixture unit when Cougaar was
        # empty — the entire reason the master map rendered with zero
        # mil_unit entities through May 21.
        kbid = u.get("uuid") or u.get("id")
        geom = u.get("location")
        if not geom and u.get("location_mgrs"):
            if _mgrs_to_latlon is None:
                from utils.coords import mgrs_to_latlon as _mgrs_to_latlon
            ll = _mgrs_to_latlon(u.get("location_mgrs"))
            if ll:
                geom = {"type": "Point", "coordinates": [ll[1], ll[0]]}
        if not (kbid and geom):
            continue
        # Derive UI-form-canonical fields (name / affiliation / echelon /
        # status). The geo-agent Entity row has no `name` column — the sync
        # endpoint stores only kind/geom/attrs, and map_state.py returns
        # `properties = {kind, confidence, **attrs}`. So the EntityPanel's
        # editForm only sees fields that are inside attrs. Surfacing the
        # raw Cougaar names (unit_short_name / force_type / unit_status)
        # alone leaves the Edit Military Unit form blank.
        _display_name = (u.get("unit_short_name") or u.get("unit_long_name")
                         or u.get("name") or str(kbid)[:8])
        _sidc = u.get("sidc") or ""
        _sidc_aff = _sidc[1] if len(_sidc) > 1 else ""
        _force = (u.get("force_type") or "").upper()
        if _sidc_aff in ("3", "F"):   _affiliation = "Friend"
        elif _sidc_aff in ("6", "H"): _affiliation = "Hostile"
        elif _sidc_aff in ("4", "N"): _affiliation = "Neutral"
        elif _sidc_aff in ("1", "U"): _affiliation = "Unknown"
        elif _force == "BLUE":        _affiliation = "Friend"
        elif _force == "RED":         _affiliation = "Hostile"
        else:                         _affiliation = "Unknown"
        # SIDC positions 8-9 (0-indexed) encode echelon; mirror SIDC_ECHELON
        # in geo-agent/backend/app/constants/types.py so the dropdown
        # matches an option value.
        _sidc_ech = _sidc[8:10] if len(_sidc) >= 10 else ""
        _ECHELON_FROM_SIDC = {
            "11": "Team", "12": "Squad", "13": "Section", "14": "Platoon",
            "15": "Company", "16": "Battalion", "17": "Regiment",
            "18": "Brigade", "21": "Division", "22": "Corps", "23": "Army",
        }
        _raw_ech = str(u.get("echelon") or "").strip()
        _echelon = (_ECHELON_FROM_SIDC.get(_raw_ech)
                    or _ECHELON_FROM_SIDC.get(_sidc_ech)
                    or _raw_ech
                    or "Battalion")
        # TIDAT-facing intel-state fields. Two cougaar shapes hit this
        # path: the IDX-26 IDXRedUnit vertex (per the 2026-06 schema
        # carries affiliation / disposition / currentActivity /
        # lastUpdated / composition / strengthPctManned /
        # symbolicIdentifier directly — no ENSIT join needed); and the
        # legacy World.Organizations.Unit vertex (NTC fixture, no
        # intel-state fields). We project from whichever fields are
        # present and let the `if v is not None` filter at the end drop
        # empties — scenario-fixture units pushed via this same function
        # also follow the same pattern.
        # SIDC fallback: legacy Unit emits `data__sidc`; new IDXRedUnit
        # emits `data__symbolicIdentifier`. Both are 2525C strings.
        # Detection-source is intentionally NOT pulled from the unit
        # vertex — the unit is the OOB record itself; detection_source
        # belongs to the SENSOR/OBSERVATION vertex that established the
        # contact (IDXSensorContactReport, IDXImageIirReport, etc.).
        # TIDAT writer joins by kg_id at emission time.
        _sidc_val = u.get("sidc") or u.get("symbolic_identifier")
        # BDA back-propagation: if a RECCEXREP target_status has been
        # written for this unit's kg_id (by bda_autofire), use it as
        # the disposition. Cougaar's IDXRedUnit.data__disposition is
        # null on all 225 live units today; the BDA status overlay is
        # the only source of damage state for the live-map SIDC
        # operational-condition amplifier. Read once per
        # _cougaar_to_mil_units call (cached by closure below).
        _bda_disp = _BDA_STATUS_OVERLAY.get(kbid) or ""
        _posture = (_bda_disp
                       or u.get("disposition")
                       or u.get("current_activity")
                       or u.get("unit_status"))
        _doi = u.get("last_updated") or u.get("observation_time")
        attrs = {
            "source": LIVE_OOB_SOURCE,
            "source_kbid": kbid,
            "sidc": _sidc_val,
            "force_type": u.get("force_type"),
            "name": _display_name,
            "affiliation": _affiliation,
            "echelon": _echelon,
            "status": "Present",
            "unit_short_name": u.get("unit_short_name") or u.get("name"),
            "unit_long_name": u.get("unit_long_name"),
            "unit_status": u.get("unit_status"),
            "personnel_status": u.get("personnel_status") or u.get("strength_pct_manned"),
            "description": u.get("description") or u.get("description_narrative"),
            "category": u.get("category"),
            "parent": u.get("parent"),
            "location_mgrs": u.get("location_mgrs"),
            # Position provenance — surfaces in the UnitMetadataPanel
            # so analysts see when a position is INFERRED via the
            # doctrinal-frontage derivation rather than reported
            # directly by cougaar. Values: 'raw', 'attributed',
            # 'children_avg', 'frontage:<role>@<deg>'.
            "position_source": u.get("position_source") or "raw",
            "posture": _posture,
            "doi": _doi,
            "target_category": u.get("organization_type"),
            "composition": u.get("composition"),
            "last_intel_stage": u.get("last_intel_stage"),
            "engagement_range_m": u.get("engagement_range"),
            "detection_range_m": u.get("detection_range"),
        }
        entities.append({
            "kind": "mil_unit",
            "name": _display_name,
            "geom": geom,
            "attrs": {k: v for k, v in attrs.items() if v is not None},
        })

        # Per-equipment range fans, derived from the unit's
        # composition_rollup nomenclatures against the doctrinal
        # EQ_ENGAGEMENT_RANGE_M table. ONE fan per UNIQUE nomenclature
        # per unit, anchored at the unit's position.
        #
        # The previous implementation read `u.engagement_range` +
        # `u.detection_range` directly off the unit, which made sense
        # when cougaar GraphQL returned those fields. Live cougaar
        # OOB no longer carries them (verified 2026-06-11 — 0 of 225
        # units have engagement_range or detection_range populated).
        # Result: zero range fans on the live operations map. Same
        # outage as the SITEMP / Threat Overlay / Threat Model side
        # which used to gate `_extract_writer_range_fans` to SITEMP
        # only. Both sides are now wired to the same doctrinal
        # composition_rollup → EQ_ENGAGEMENT_RANGE_M lookup.
        from utils import equipment_doctrine as _eq_doctrine
        _rollup = u.get("composition_rollup") or {}
        if not isinstance(_rollup, dict):
            _rollup = {}
        # Legacy nested shape fallback — keep the import resilient if
        # the adapter shape rotates back.
        if not _rollup:
            _legacy_comp = u.get("composition") or {}
            if isinstance(_legacy_comp, dict):
                for _er in (_legacy_comp.get("equipment_list_with_counts") or []):
                    if isinstance(_er, dict):
                        _nm = (_er.get("nomenclature") or "").strip()
                        if _nm:
                            _rollup[_nm] = _rollup.get(_nm, 0) + int(_er.get("count") or 1)
        _seen_noms: set = set()
        _unit_echelon = _echelon
        for _nom_raw in _rollup.keys():
            _nom = (_nom_raw or "").strip()
            if not _nom or _nom in _seen_noms:
                continue
            _range_m = _eq_doctrine.equipment_engagement_range_m(_nom)
            if not _range_m:
                continue
            _seen_noms.add(_nom)
            _subkind = _eq_doctrine.equipment_range_subkind(_nom)
            overlays.append({
                "kind": "range_fan",
                "name": f"{_display_name} {_nom}",
                "geom": geom,
                "attrs": {
                    k: v for k, v in {
                        "source": LIVE_OOB_SOURCE,
                        "source_kbid": f"{kbid}#rf:{_nom}",
                        "bearer_unit_kg_id": kbid,
                        "bearer_unit_name": _display_name,
                        "equipment_nomenclature": _nom,
                        "bands": [{
                            "radius_m": int(_range_m),
                            "label": f"{_nom} {int(_range_m / 1000)}km",
                        }],
                        "radius_m": int(_range_m),
                        "subkind": _subkind,
                        "category": u.get("category"),
                        "echelon": _unit_echelon,
                        "force_type": u.get("force_type"),
                    }.items() if v is not None
                },
            })

        # § 12 multi-position rendering — when the resolved RED OOB
        # carries attributed_positions[] (orphan-observation clusters
        # the pytho-side fusion mapped to this named bearer unit), emit
        # one ADDITIONAL mil_unit entity per attributed position. Each
        # sub-position uses the same SIDC + name but is tagged
        # `is_sub_position=true` + `attribution_confidence=HIGH|MED` so
        # the renderer can paint them with a distinct outline (e.g.,
        # dashed) and the analyst can see the unit's dispersed
        # firing/observation footprint, not just its primary vertex
        # location. Each sub-position is its own selectable mil_unit
        # with the bearer's full provenance.
        for ap_ix, ap in enumerate(u.get("attributed_positions") or []):
            if not isinstance(ap, dict):
                continue
            ap_lat = ap.get("lat")
            ap_lon = ap.get("lon")
            if ap_lat is None or ap_lon is None:
                continue
            ap_geom = {"type": "Point", "coordinates": [ap_lon, ap_lat]}
            sub_attrs = dict(attrs)
            sub_attrs["source_kbid"] = f"{kbid}#sub{ap_ix}"
            sub_attrs["is_sub_position"] = True
            sub_attrs["bearer_unit_kg_id"] = kbid
            sub_attrs["bearer_unit_name"] = _display_name
            sub_attrs["attribution_confidence"] = ap.get("confidence")
            sub_attrs["attribution_score"] = ap.get("score")
            sub_attrs["attribution_distance_km"] = ap.get("distance_km")
            sub_attrs["attributed_n_observations"] = ap.get("n_observations")
            sub_attrs["attributed_inferred_echelon"] = ap.get("inferred_echelon")
            sub_attrs["attributed_last_seen"] = ap.get("last_seen")
            sub_attrs["name"] = f"{_display_name} (pos #{ap_ix + 1})"
            entities.append({
                "kind": "mil_unit",
                "name": sub_attrs["name"],
                "geom": ap_geom,
                "attrs": {k: v for k, v in sub_attrs.items() if v is not None},
            })

    for t in (targets or []):
        kbid = t.get("observation_id") or (t.get("source_kbids") or [None])[0]
        raw = (t.get("raw_records") or [{}])[0] if isinstance(t.get("raw_records"), list) else {}
        geom = raw.get("location") or t.get("location")
        if not (kbid and geom):
            continue
        attrs = {
            "source": LIVE_OOB_SOURCE,
            "source_kbid": kbid,
            "source_int": t.get("source_int"),
            "be_number": raw.get("be_number") or t.get("entity_key"),
            "target_name": raw.get("target_name") or t.get("entity_name"),
            "country": raw.get("country"),
            "item_number": raw.get("item_number"),
        }
        entities.append({
            "kind": "target",
            "name": raw.get("target_name") or t.get("entity_name") or t.get("name") or kbid[:8],
            "geom": geom,
            "attrs": {k: v for k, v in attrs.items() if v is not None},
        })

    # § 12 unidentified-formation pins. Each orphan cluster that the
    # resolved-OOB adapter could NOT attribute to a named bearer unit
    # is surfaced here as a separate mil_unit entity with a hostile-
    # unknown-affiliation marker + collection-priority badge so the
    # analyst sees it on the live map alongside named units. These are
    # the PIR candidates for the next intel cycle.
    for uf_ix, uf in enumerate(unidentified_formations or []):
        if not isinstance(uf, dict):
            continue
        uf_lat = uf.get("centroid_lat")
        uf_lon = uf.get("centroid_lon")
        if uf_lat is None or uf_lon is None:
            continue
        uf_geom = {"type": "Point", "coordinates": [uf_lon, uf_lat]}
        # Synthetic kg-id-style token so the entity has a stable
        # source_kbid for sync REPLACE semantics. Prefix `UFP-` =
        # "unidentified formation pin" so future debugging can tell
        # these apart from real cougaar kbIDs at a glance.
        uf_kbid = f"UFP-{uf_ix:03d}-{int(uf_lat * 1000)}_{int(uf_lon * 1000)}"
        uf_inferred_ech = uf.get("inferred_echelon") or "Unknown"
        uf_n_obs = uf.get("n_observations") or 0
        uf_hvt_count = uf.get("hvt_worthy_count") or 0
        # Hostile-unknown SIDC — frame open at top so renderer paints
        # as a dashed-frame variant. echelon position fills in from
        # the cluster's inferred echelon.
        _UF_SIDC_ECH = {
            "BDE": "18", "BN": "16", "CO": "15", "BTRY": "15",
            "DIV": "21",
        }.get(uf_inferred_ech, "16")
        uf_attrs = {
            "source": LIVE_OOB_SOURCE,
            "source_kbid": uf_kbid,
            "name": f"Unidentified {uf_inferred_ech} formation",
            "affiliation": "Hostile",
            "echelon": {"18": "Brigade", "16": "Battalion", "15": "Company",
                        "21": "Division"}.get(_UF_SIDC_ECH, "Battalion"),
            "status": "Suspect",
            "force_type": "RED",
            "is_unidentified_formation": True,
            "inferred_echelon": uf_inferred_ech,
            "n_observations": uf_n_obs,
            "hvt_worthy_count": uf_hvt_count,
            "composition": uf.get("composition"),
            "collection_recommendation": uf.get("collection_recommendation"),
            "top_candidates": uf.get("top_candidates"),
            "last_seen": uf.get("last_seen"),
            # Suspect-class SIDC (position 2 = 'S' suspect) so the
            # renderer paints it with dashed framing per APP-6 standard.
            "sidc": "SSGPUC" + ("D" if uf_inferred_ech == "BDE" else "D")
                    + "C---" + _UF_SIDC_ECH + "FG",
        }
        entities.append({
            "kind": "mil_unit",
            "name": uf_attrs["name"],
            "geom": uf_geom,
            "attrs": {k: v for k, v in uf_attrs.items() if v is not None},
        })

    # Decluster coincident mil_unit positions. Cougaar IDXRedUnit
    # reports a brigade and its HHC + sibling brigades at one DIV-HHC
    # lat/lon (verified on the resolved-OOB snapshot: 7 BRIGADEs at
    # 58.3269, 25.3117; 179 BATTALIONs at 0.0, 0.0). They paint at one
    # pixel and the topmost intercepts every right-click — analyst
    # can't open per-target products on subordinates. Spread coincident
    # mil_units in a tight ring around their shared centroid so each
    # has its own hit-test footprint AND its own visible glyph at all
    # zoom levels. Deterministic ordering (by source_kbid then name)
    # means a unit lands at the SAME offset each render, so the
    # analyst's cursor stays on the same brigade across map refreshes.
    import math as _math
    DECLUSTER_RADIUS_DEG = 0.0035   # ≈ 350-400m at IDX 26 latitudes;
                                     # one milsymbol icon width at z=12
    SHARED_POS_BUCKET = 5            # rounding precision for "shared"
    pos_buckets: dict = {}
    for ent in entities:
        if ent.get("kind") != "mil_unit":
            continue
        geom = ent.get("geom") or {}
        coords = geom.get("coordinates")
        if not (isinstance(coords, list) and len(coords) >= 2):
            continue
        lon, lat = coords[0], coords[1]
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            continue
        # Skip the (0,0) placeholder bucket — those battalions have NO
        # reported position. Use the SAME precision as the bucket key
        # below (5 decimals = ~1m at the equator); a tighter check
        # (1e-6) let units at 1.5e-6 through that still rounded to 0.0
        # at 5-decimal precision, collapsing them into a giant
        # 179-member ring around (0,0) at the AO's south-west corner.
        bucket = (round(lat, SHARED_POS_BUCKET), round(lon, SHARED_POS_BUCKET))
        if bucket == (0.0, 0.0) or bucket == (-0.0, -0.0):
            continue
        pos_buckets.setdefault(bucket, []).append(ent)
    for bucket, members in pos_buckets.items():
        if len(members) < 2:
            continue
        # Deterministic ring order: kbid first (stable across refreshes),
        # then name as a tiebreak for synthetic entities without kbid.
        members.sort(key=lambda e: (
            (e.get("attrs") or {}).get("source_kbid") or "",
            e.get("name") or "",
        ))
        cx_lat, cx_lon = bucket
        # Latitude scaling factor — 1 deg lat ≈ 1 deg lon at the equator
        # but lon shrinks by cos(lat) toward the poles. IDX 26 sits near
        # 58°N so the unscaled offset would oval; scale the lon
        # component to keep the ring circular on the rendered map.
        lon_scale = 1.0 / max(0.01, _math.cos(_math.radians(cx_lat)))
        n = len(members)
        for ix, ent in enumerate(members):
            theta = 2 * _math.pi * ix / n
            dlat = DECLUSTER_RADIUS_DEG * _math.sin(theta)
            dlon = DECLUSTER_RADIUS_DEG * _math.cos(theta) * lon_scale
            new_lon = cx_lon + dlon
            new_lat = cx_lat + dlat
            ent["geom"] = {"type": "Point", "coordinates": [new_lon, new_lat]}
            # Record the original shared position on attrs so the panel
            # can show "reported at <orig MGRS>, declustered for display"
            # if we ever surface it.
            (ent.setdefault("attrs", {}))["declustered_from"] = {
                "lat": cx_lat, "lon": cx_lon, "ring_index": ix, "ring_size": n,
            }

    return {"overlays": overlays, "entities": entities}


# ---------------------------------------------------------------------------
# Within-INT fusion (group-by-natural-key)
# ---------------------------------------------------------------------------


