"""KLIEGLIGHT auto-fire trigger — event-driven SIGINT atomic generation.

Doctrinal anchor: KLIEGLIGHT is the time-perishable SIGINT atomic (FM 2-0
§1-86; USSID SE 4001; ATP 2-22.6). It fires on detection, not on a cadence.
The on-demand generation path (operator clicks Generate -> tool_generate_product)
remains intact; this module bolts an EVENT-DRIVEN path next to it so a new
qualifying signal in the Cougaar KG auto-fires a draft, lands it in temp
storage, and emits a notification the analyst can promote to a real session.

Trigger policy
--------------
On each recent-intel poll, the cougaar adapter computes the kbID set delta
(this_poll - last_poll). For every NEW kbID in that delta this module
inspects the raw record and decides whether to fire.

HVT determination comes from Pytho's HVTL product (FM 3-60 App. C /
ATP 2-01.3 Fig 6-9), NOT from a cougaar-side annotation. The HVTL is
the intel-staff analytical product that names which threat assets are
high-value for this phase × COA; cougaar is the sensor feed. The gate
loads the most-recent HVTL artifact's rows (id + name) and checks
whether the SIGINT/ELINT/COMMS signal's owning entity is on that list.
Without an HVTL artifact, NO autofires happen — that is the correct
default state (no analytical HVT product, no automatic atomic).

Doctrinal vertex gate (signal class):
  - IDXTacElintReport, IDXSignalOfInterestReport, IDXCommunicationsData
    are candidate trigger sources (SIGINT / ELINT / COMINT classes).
  - HUMINT / IMINT / Tactical / OOB never fire — KLIEGLIGHT is SIGINT-only.

HVTL membership gate (importance):
  - Direct kg_id match: if the cougaar record carries parent_kg_id /
    unit_kg_id / entity_id / kg_id, that id must appear in the HVTL row
    ids. This is the canonical match path once cougaar populates the
    upstream entity reference.
  - Name substring fallback: HVT designations ("6 IFC ARMY", "676 SIG
    BDE", ...) may appear in emitter_name / parent_threat_system /
    unit_designation / ew_callsign / platform_or_sensor_id when the
    cougaar feed labels signals by owning unit. Case-insensitive
    substring match in either direction.
  - Minimum signal data: record must carry at least one of frequency /
    MGRS / lat / intercept-time / PRF / bandwidth / modulation. Sparse
    rows with only an emitter_name produce empty KLIEGLIGHTs and are
    skipped here.

Storage + ownership
-------------------
The DAG runner persists the generated artifact via save_turn() into a NEW
SessionDetail row keyed by a synthesized session_id ("autodraft-<uuid>"),
with owner_user_id="_autodraft" and header.status="draft". The
artifact's HTML/DOCX render lands under uploads/_autodraft/<artifact_id>/
following handlers.product.render_artifact_files' existing convention.

Drafts in the _autodraft namespace are invisible to every analyst's
_project_docs_index until the open_draft endpoint promotes them — at which
point the artifact is rewritten to the requesting analyst's user_id +
project_id and a fresh session_id is minted that the UI can open in
DocEditorPage.

Why a separate "_autodraft" owner instead of stamping the artifact directly
on an analyst: there is no single analyst the auto-fire knows about. The
KG poll runs at the process level, not per-analyst. Tagging _autodraft
preserves the existing draft-visibility rule (drafts visible only to
owner_user_id) without leaking auto-fired drafts into analyst views.

Concurrency
-----------
The poller's callback await is sequential — a slow auto-fire pass delays
the NEXT callback in this poll, but not the NEXT poll tick (the poll loop
awaits all callbacks BEFORE sleeping). Multiple new kbIDs in one poll fire
sequentially; that's intentional. Running them in asyncio.gather() would
push 4+ concurrent DAG runs at gemma4 vLLM and saturate.

Error policy
------------
Per CLAUDE.md "no graceful error handling": failure to fire a single
KLIEGLIGHT is logged at WARNING with full traceback and the loop continues.
Failures don't poison the cache or stop the poller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


# Persistent fired-kbid log. One kbID per line; appended on every
# successful _fire_one_registered (see _record_fired_kbid). Lives in
# the mounted /app/logs volume so it survives container restart —
# without it the poller's _LAST_SEEN_KBIDS reset would re-fire every
# KL in cougaar on the next poll.
_FIRED_KBIDS_LOG_PATH = "/app/logs/klieglight_fired_kbids.log"


def _load_fired_kbids() -> set:
    """Read the persistent fired-kbid log into a set. O(N) on file size
    once per poll pass; O(1) membership during the loop. Resilient to
    missing files (first run) and read errors (we'd rather re-fire than
    silently fail open)."""
    try:
        with open(_FIRED_KBIDS_LOG_PATH, "r") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning("KLIEGLIGHT fired-kbid log read failed: %s", e)
        return set()


def _record_fired_kbid(kbid: str) -> None:
    """Append one kbID to the persistent fired-kbid log. Atomic line-
    append (single fwrite under POSIX append-mode semantics). Failures
    log + continue — the autofire path stays alive even if the disk is
    full; worst case we re-fire on restart."""
    if not kbid:
        return
    try:
        # Create the parent dir on the first record so deployments
        # without the /app/logs mount don't kill the autofire.
        os.makedirs(os.path.dirname(_FIRED_KBIDS_LOG_PATH), exist_ok=True)
        with open(_FIRED_KBIDS_LOG_PATH, "a") as f:
            f.write(f"{kbid}\n")
    except Exception as e:
        log.warning("KLIEGLIGHT fired-kbid log append failed (key=%s): %s",
                      kbid, e)


def _fired_key(project_id: str, kbid: str) -> str:
    """Per-project dedup key for the fired log:
    '<project_id>:<kbid>' — the same signal fires once PER PROJECT
    (each event owns its own artifact pool). Pre-unification bare-kbid
    log lines are rewritten by the box migration."""
    return f"{project_id}:{kbid}"


# Persistent file logging — survives container restart so a SCIF poll's
# gate-rejection counters can be inspected AFTER the spark is brought
# back. Writes to /app/logs/klieglight_autofire.log (mounted in
# docker-compose at ./logs:/app/logs on docker-api). The host path
# /home/pytho/pytho-app/logs/ persists across rebuilds. Rotated at 50MB
# x 5 keeps disk bounded. log level INFO so the per-poll auto-fire-pass
# counter lines + HVTL gate cache loads are captured.
try:
    import logging.handlers as _lh
    _LOG_DIR = "/app/logs"
    os.makedirs(_LOG_DIR, exist_ok=True)
    _fh = _lh.RotatingFileHandler(
        os.path.join(_LOG_DIR, "klieglight_autofire.log"),
        maxBytes=50 * 1024 * 1024, backupCount=5,
    )
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(funcName)s L%(lineno)d %(message)s"
    ))
    log.addHandler(_fh)
    log.setLevel(logging.INFO)
    # Avoid duplicate file rows when root also has a handler.
    log.propagate = True
except Exception as _e:
    # Don't take down the autofire path if logging setup fails — fall
    # back to root-handler-only logging.
    log.warning("KLIEGLIGHT autofire: persistent file logging setup failed: %s", _e)


# Filesystem location for product artifacts. Mirrors handlers.product.UPLOAD_DIR
# (kept independent here to avoid a circular import — autofire runs at module
# import time and product.py drags in the writer / DAG stack).
_UPLOAD_DIR = Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))

# HVTL cache — re-scanned on a TTL because filesystem walk across all owner
# dirs is cheap but not free. 60s is short enough that a freshly-emitted HVTL
# starts gating within a minute. Cleared on process restart (correct: new
# HVTLs get picked up on the next scan).
_HVT_CACHE: dict = {
    "loaded_at": 0.0,
    "hvt_type_substrings": set(),
    "hvt_equipment_names": set(),
    "hvt_bearer_units": set(),
    "hvt_bearer_kg_ids": set(),
    # List of (lat, lon, bearer_unit_name, kg_id) tuples — the spatial
    # anchor set used by tier 4 (proximity match). Live cougaar IDX
    # SIGINT/ELINT/COMINT vertices ship `location` (GeoPoint) but no
    # kg_id linkage to a bearer unit, so spatial join is the only
    # match path against the real schema (verified against the IDX 26
    # schema at cougaar_adapters.py:441-575 — IDXTacElintReport carries
    # only event_description / force_threat_designator / location;
    # IDXSignalOfInterest carries only signal_id / emitter_name /
    # platform_or_sensor_id / location; IDXCommunicationsData carries
    # bandwidth / ew_callsign / frequency_raw only).
    "hvt_bearer_positions": [],
}

# Spatial-proximity threshold (km) for tier-4 HVT match. Signals from a
# Pantsir radar typically locate within 1-3 km of the launcher under
# multi-collect geolocation; 5 km is the conservative outer envelope
# for sensors with elliptical-error-meters in the 500-3000m range.
# Override via env so the SCIF can tune without redeploy.
import os as _os
HVT_SPATIAL_THRESHOLD_KM = float(_os.environ.get("KLIEGLIGHT_HVT_SPATIAL_KM", "25"))
_HVT_CACHE_TTL_SEC = 60.0


def _load_current_hvt_set() -> tuple[set[str], set[str], set[str], set[str], list]:
    """Walk the uploads tree for HVTL_* artifact directories, read the most
    recent schema_state.json, and return three lowercased sets used by the
    autofire gate:

      - hvt_type_substrings — the writer's selected_hvt_kg_ids picks per
        row (`row.matched_type`), e.g. {"sa 11", "pantsir s1", "2s19 msta",
        "bm-30 smerch"}. The writer chose these to be distinctive
        substrings that case-insensitive-contains-match the cougaar
        composition_rollup keys; the autofire gate uses the same shape
        to match against a SIGINT signal's emitter_name / nomenclature /
        equipment_name / parent_threat_system / signal_id text fields.
      - hvt_equipment_names — the full cougaar equipment nomenclature
        each HVT row resolved to (`row.name`, e.g. "LCHR 9A310M1 SA 11
        GADFLY TELAR TRKD"). Matched the same way as substrings.
      - hvt_bearer_units — the set of bearer unit DESIGNATIONS holding
        any HVT-type equipment (parsed from the multi-line
        `row.bearer_unit` cell, each line shaped "<unit name> (×N)").
        Lowercased; used to gate signals that carry an owning-unit
        designator but no equipment nomenclature.

    HVTL is now equipment-type shaped (see per-coa-product-scope.md
    §11.1a + handlers/intel_agent.py _build_skeleton's
    expand_to_equipment fan-out). The prior gate matched on
    structured_data.rows[*].id which used to be a cougaar unit kg_id;
    after the refactor row.id is a synthetic "hvt::<type>" composite,
    so the old kg_id equality match silently returned False on every
    signal and KLIEGLIGHT autofire stopped firing. This gate matches
    on the doctrinally-meaningful surface (equipment type substring
    against the signal's emitter text), restoring autofire under the
    new HVTL shape.

    Returns (empty, empty, empty) when no HVTL exists yet — autofire
    is gated off in that state by design (no analytical HVT product,
    no automatic KLIEGLIGHT). Cached for `_HVT_CACHE_TTL_SEC`.
    """
    now = time.monotonic()
    if (now - _HVT_CACHE["loaded_at"] < _HVT_CACHE_TTL_SEC
            and _HVT_CACHE["loaded_at"] > 0):
        return (
            _HVT_CACHE["hvt_type_substrings"],
            _HVT_CACHE["hvt_equipment_names"],
            _HVT_CACHE["hvt_bearer_units"],
            _HVT_CACHE["hvt_bearer_kg_ids"],
            _HVT_CACHE["hvt_bearer_positions"],
        )

    hvt_type_substrings: set[str] = set()
    hvt_equipment_names: set[str] = set()
    hvt_bearer_units: set[str] = set()
    hvt_bearer_kg_ids: set[str] = set()
    hvt_bearer_positions: list = []
    try:
        # Use ONLY the most-recent HVTL artifact — not the union across
        # every stale autodraft on disk. Unioning 50+ stale HVTLs creates
        # a giant superset and explodes the autofire rate to ~12/min
        # (the rate observed in production 2026-06-09). HVTL is authored
        # by analyst IPOE Step 4; the live HVT set is whatever the latest
        # finalized/autodrafted HVTL says, not a historical OR.
        candidate_dirs = []
        if _UPLOAD_DIR.exists():
            for owner_dir in _UPLOAD_DIR.iterdir():
                if not owner_dir.is_dir():
                    continue
                for hvtl_dir in owner_dir.glob("HVTL_*"):
                    schema_state = hvtl_dir / "schema_state.json"
                    if schema_state.is_file():
                        candidate_dirs.append((hvtl_dir.name, schema_state))
        # HVTL_<YYYYMMDDTHHMMSS>_<msZ> — lexicographic sort = chronological
        candidate_dirs.sort(reverse=True)
        latest = candidate_dirs[0] if candidate_dirs else None
        if latest:
            try:
                s = json.loads(latest[1].read_text())
                sd = s.get("structured_data") or {}
                import re as _re
                for r in sd.get("rows") or []:
                    matched_type = (r.get("matched_type") or "").strip()
                    eq_name = (r.get("name") or "").strip()
                    bearer_cell = r.get("bearer_unit") or ""
                    # The writer's distinctive equipment substring. This
                    # is the analyst's "what kind of equipment are we
                    # hunting" — match SIGINT emitters against THIS.
                    if matched_type and len(matched_type) >= 3:
                        hvt_type_substrings.add(matched_type.lower())
                    # The cougaar full equipment nomenclature the type
                    # resolved to — also matched against emitter text.
                    if eq_name and len(eq_name) >= 3:
                        hvt_equipment_names.add(eq_name.lower())
                    # Bearer-unit designators (multi-line cell, one
                    # entry per "<unit> (×N)" line). Lowercased for
                    # case-insensitive equality match against an
                    # owning-unit designator on the signal.
                    if isinstance(bearer_cell, str) and bearer_cell:
                        for line in bearer_cell.split("\n"):
                            # Strip "(×N)" suffix; keep just the unit
                            # designation.
                            stripped = _re.sub(r"\s*\(×\d+\)\s*$", "", line).strip()
                            if stripped and len(stripped) >= 4:
                                hvt_bearer_units.add(stripped.lower())
            except Exception:
                log.exception("KLIEGLIGHT autofire: HVTL parse failed for %s",
                                latest[0])
    except Exception:
        log.exception("KLIEGLIGHT autofire: HVTL set load failed")

    # Resolve bearer-unit DESIGNATIONS into bearer-unit KG_IDS via the
    # resolved-OOB snapshot. The HVTL row's bearer_unit cell stores
    # designations like "626 ADA BDE (×12)" (analyst-readable) but live
    # cougaar SIGINT/COMMS records carry parent_kg_id / unit_kg_id as
    # UUIDs — the path the OLD kg_id-equality gate matched on, the path
    # that fired 400+ KLIEGLIGHTs before the equipment-type refactor.
    # Restoring that match against the new HVTL's bearer-unit set.
    try:
        from utils import oob as _oob
        doc = {"resolved": {"named_units": _oob.resolved_named_units()}}
        if True:
            # Note: the snapshot uses `kg_id` (not `id`) and
            # `primary_position` (not `location_mgrs`) — verified
            # against the live snapshot field set (cougaar_adapters
            # _write_oob_snapshot writes the resolved RedUnit shape).
            for u in ((doc.get("resolved") or {}).get("named_units") or []):
                nm = (u.get("name") or "").strip().lower()
                kgid = (u.get("kg_id") or u.get("id") or "").strip()
                if kgid and nm in hvt_bearer_units:
                    hvt_bearer_kg_ids.add(kgid)
                    pos = u.get("primary_position") or {}
                    lat = pos.get("lat")
                    lon = pos.get("lon")
                    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                        hvt_bearer_positions.append(
                            (float(lat), float(lon), u.get("name") or "", kgid))
    except Exception:
        log.debug("KLIEGLIGHT autofire: bearer-unit resolve failed",
                    exc_info=True)

    _HVT_CACHE["loaded_at"] = now
    _HVT_CACHE["hvt_type_substrings"] = hvt_type_substrings
    _HVT_CACHE["hvt_equipment_names"] = hvt_equipment_names
    _HVT_CACHE["hvt_bearer_units"] = hvt_bearer_units
    _HVT_CACHE["hvt_bearer_kg_ids"] = hvt_bearer_kg_ids
    _HVT_CACHE["hvt_bearer_positions"] = hvt_bearer_positions
    log.info(
        "KLIEGLIGHT HVTL gate loaded: %d type-substrings, %d equipment-names, "
        "%d bearer-units, %d bearer-kg_ids, %d bearer-positions "
        "(threshold=%.1fkm, from %s)",
        len(hvt_type_substrings), len(hvt_equipment_names),
        len(hvt_bearer_units), len(hvt_bearer_kg_ids),
        len(hvt_bearer_positions), HVT_SPATIAL_THRESHOLD_KM,
        latest[0] if latest else "no HVTL on disk",
    )
    return (hvt_type_substrings, hvt_equipment_names,
              hvt_bearer_units, hvt_bearer_kg_ids, hvt_bearer_positions)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Earth radius 6371 km. Pure python so
    no numpy import in the hot path."""
    import math
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


# Signal-record fields that may carry equipment / emitter nomenclature
# text. Matched case-insensitive contains against the HVTL type-substring
# and equipment-name sets — a signal from a Pantsir radar shows up as
# emitter_name="LCHR PANTSIR S1 ..." or parent_threat_system="Pantsir-S1"
# depending on the report subtype.
_SIGNAL_EMITTER_TEXT_FIELDS = (
    "emitter_name",
    "equipment_name",
    "nomenclature",
    "parent_threat_system",
    "elnot_designator",
    "force_threat_designator",
    "signal_id",
)

# Signal-record fields that may carry a bearer-unit designator (the
# operating unit). Matched against the HVTL bearer-unit set.
_SIGNAL_BEARER_UNIT_TEXT_FIELDS = (
    "owning_unit",
    "owning_unit_designator",
    "parent_unit",
    "unit_designation",
    "associated_unit",
)

# Signal-record fields that may carry the bearer-unit kg_id (UUID).
# Matched by EQUALITY against the HVTL-derived bearer-unit kg_id set.
# Live cougaar IDX SIGINT/COMMS vertices typically carry these UUIDs
# rather than human-readable designations, so this is the dominant
# real-world match path on the live feed.
_SIGNAL_BEARER_KG_ID_FIELDS = (
    "parent_kg_id",
    "unit_kg_id",
    "entity_id",
    "kg_id",
    "owning_unit_kg_id",
    "source_kbid",
    "associated_unit_kg_id",
)


def _signal_resolves_to_hvt(raw_record: dict) -> bool:
    """Cross-reference a SIGINT/ELINT/COMMS record against the latest HVTL.

    The match is three-tier (any one alone is sufficient):

      1. Equipment-text match — case-insensitive contains-check between
         any of _SIGNAL_EMITTER_TEXT_FIELDS on the record (emitter_name,
         equipment_name, nomenclature, parent_threat_system, ...) and
         the writer's hvt_type_substrings + hvt_equipment_names. A
         Pantsir-S1 emitter showing up as `emitter_name="LCHR PANTSIR
         S1 SA 22 GREYHOUND TELAR WHL"` fires against the writer's
         "PANTSIR S1" pick.
      2. Bearer-unit text match — case-insensitive equality between
         any of _SIGNAL_BEARER_UNIT_TEXT_FIELDS on the record
         (owning_unit, parent_unit, unit_designation, ...) and the
         HVTL bearer-unit designation set.
      3. Bearer-unit kg_id match — equality between any of
         _SIGNAL_BEARER_KG_ID_FIELDS (parent_kg_id, unit_kg_id, ...)
         and the HVTL bearer-unit kg_id set (derived by resolving each
         HVTL bearer designation against oob_resolved_latest.json's
         named_units). Live cougaar IDX SIGINT vertices ship parent_kg_id
         (UUID) rather than designation strings, so this is the dominant
         real-feed match path. False positives self-limit because the
         kg_id set is narrow (one kg_id per HVT bearer brigade, not the
         whole OOB).

    All tiers self-limit by HVTL membership — a wrong tier is bounded
    by the writer's HVT picks. The 2026-06-09 12/min stampede came
    from substring-matching unit designators like \"601\" — gone now,
    type substrings are analyst-curated and bearer matches require
    either full-designation equality (tier 2) or kg_id equality
    (tier 3).

    Returns False when no HVTL exists yet OR when no field on the
    signal resolves under any tier.
    """
    type_subs, eq_names, bearer_units, bearer_kg_ids, bearer_positions = _load_current_hvt_set()
    if not (type_subs or eq_names or bearer_units or bearer_kg_ids
              or bearer_positions):
        return False
    # Tier 1: equipment / emitter text on the signal contains any HVT
    # type-substring or equipment-nomenclature key.
    for key in _SIGNAL_EMITTER_TEXT_FIELDS:
        v = raw_record.get(key)
        if not v:
            continue
        v_lower = str(v).lower()
        for sub in type_subs:
            if sub in v_lower:
                return True
        for sub in eq_names:
            if sub in v_lower:
                return True
    # Tier 2: bearer-unit designator on the signal equals any unit
    # that holds an HVT-type item.
    for key in _SIGNAL_BEARER_UNIT_TEXT_FIELDS:
        v = raw_record.get(key)
        if not v:
            continue
        if str(v).strip().lower() in bearer_units:
            return True
    # Tier 3: bearer-unit kg_id (UUID) on the signal equals any HVT
    # bearer's kg_id. Forward-compat path — the verified IDX 26 schema
    # (IDXTacElintReport / IDXSignalOfInterest / IDXCommunicationsData)
    # does NOT today carry parent_kg_id, so this tier reserves the path
    # for when cougaar adds a unit-link field.
    if bearer_kg_ids:
        for key in _SIGNAL_BEARER_KG_ID_FIELDS:
            v = raw_record.get(key)
            if not v:
                continue
            if str(v).strip() in bearer_kg_ids:
                return True
    # Tier 4: spatial proximity. Live IDX SIGINT/ELINT/COMINT vertices
    # carry only `location` (lat/lon) — no kg_id link to bearer units —
    # so spatial join against the HVT-bearer primary_position set is
    # the DOMINANT match path on the real feed. Signal fires when its
    # location is within HVT_SPATIAL_THRESHOLD_KM of any HVT-holder's
    # primary position.
    if bearer_positions:
        lat = raw_record.get("lat")
        lon = raw_record.get("lon")
        # IDX vertex `location` arrives as a GeoJSON Point per
        # cougaar_adapters._to_geojson_point:
        #   {"type": "Point", "coordinates": [lon, lat, alt]}
        # Extract lon/lat from the coordinates array (NOTE: lon is index
        # 0 per GeoJSON spec, lat is index 1) when top-level lat/lon
        # aren't already populated by a richer fixture path.
        if (lat is None or lon is None) and isinstance(raw_record.get("location"), dict):
            loc = raw_record["location"]
            coords = loc.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                if lon is None:
                    lon = coords[0]
                if lat is None:
                    lat = coords[1]
            # Older / fixture path may use {lat, lon} keys instead.
            if lat is None:
                lat = loc.get("lat") or loc.get("latitude")
            if lon is None:
                lon = loc.get("lon") or loc.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            try:
                lat_f = float(lat); lon_f = float(lon)
                for blat, blon, _bname, _bkg in bearer_positions:
                    if _haversine_km(lat_f, lon_f, blat, blon) <= HVT_SPATIAL_THRESHOLD_KM:
                        return True
            except (TypeError, ValueError):
                pass
    return False


_FIRE_REQUIRES_AT_LEAST_ONE_OF = (
    # Rich-signal fields (event_data fixture path + future cougaar populated state)
    "frequency_mhz", "mgrs", "lat", "intercept_start", "start_time", "location",
    "prf_pps", "bandwidth", "modulation",
    "intercept_end", "end_time", "reported_time", "report_time",
    "elliptical_error_meters", "line_of_bearing_1", "line_of_bearing_2",
    "pulse_duration",
    # Sparse-live-IDX fields. Per the 2026-06-10 per-poll counter capture
    # (13747 records, no_data=13747, 641 IDXTacElintReport + 641
    # IDXSignalOfInterestReport per poll), live cougaar SIGINT vertices
    # arrive with NO frequency/MGRS/intercept-time AT ALL — only
    # emitter_name / signal_id / force_threat_designator. The old gate
    # required rich fields and killed 100% of live candidates. Bearer-
    # MGRS seed (_bearer_mgrs_for_signal in _fire_one) backfills
    # paragraph_4 from the resolved-OOB so an emitter-only signal still
    # produces a useful KLIEGLIGHT.
    "emitter_name", "signal_id", "force_threat_designator",
    "elnot_designator", "parent_threat_system", "platform_or_sensor_id",
    "ref_id", "uuid",
    # COMINT (IDXCommunicationsData) carries these and only these.
    "ew_callsign", "frequency_raw",
)


def _has_minimal_signal_data(raw_record: dict) -> bool:
    """Accept any signal-identifying field. The live IDX schema today
    ships only emitter_name + signal_id + force_threat_designator on
    SIGINT/ELINT/COMINT vertices (verified against the 2026-06-10 SCIF
    counter capture); requiring frequency / MGRS / intercept-time killed
    100% of live candidates. The bearer-MGRS pre-fire seed
    (_bearer_mgrs_for_signal called from _fire_one) backfills the
    geolocation paragraph from the resolved-OOB snapshot when the signal
    lacks coords, so an emitter-only signal still produces a useful
    KLIEGLIGHT. Writer fills absent paragraphs with 'Not reported'."""
    for key in _FIRE_REQUIRES_AT_LEAST_ONE_OF:
        val = raw_record.get(key)
        if val not in (None, "", [], {}):
            return True
    return False


def _should_fire(raw_record: dict) -> bool:
    """Decide whether a single newly-arrived KG record should auto-fire a
    KLIEGLIGHT.

    Receives the dict shape produced by cougaar_adapters._transform —
    flattened model_dump() of the per-INT pydantic model with an extra
    `_objectType` key stamped by _fetch_recent_intel_raw.

    Three gates (all must pass):
      1. SIGINT/ELINT/COMINT vertex class — IDXTacElintReport /
         IDXSignalOfInterestReport / IDXCommunicationsData.
      2. Minimum signal data — at least one of frequency / MGRS / lat /
         intercept-time / PRF / bandwidth / modulation. Sparse rows
         produce empty artifacts and are skipped.
      3. HVTL membership — the signal's owning entity must resolve to a
         row on the most-recent HVTL artifact (see _signal_resolves_to_hvt
         and module docstring "Trigger policy"). With no HVTL on disk
         this gate fails closed — no autofires until an HVTL exists.
    """
    obj_type = raw_record.get("_objectType") or ""
    # SIGINT-class allow-list — AUTHORITATIVE source→vertex map from
    # the cougaar simulation team 2026-06-10:
    #   WRANGLER  → IDXWranglerReport
    #   CMF       → IDXTacElintReport
    #   TACELINT  → IDXTacElintReport
    # Only these two vertex types are source reports KL must fire on.
    # IDXSignalOfInterestReport is the emitter-side companion of
    # IDXTacElintReport (verified via the schema's
    # HAS_TAC_ELINT_EQUIPMENT_…IDXSignalOfInterestReport relationship)
    # — firing on it double-counts. IDXCommunicationsData isn't on the
    # source map.
    if obj_type not in ("IDXTacElintReport", "IDXWranglerReport"):
        return False
    if not _has_minimal_signal_data(raw_record):
        return False
    # HVT gate disabled per user directive 2026-06-10 night: the live IDX
    # SIGINT/Wrangler vertices ship without emitter nomenclature, bearer
    # kg_id, or usable location — every match path was dying at 100%
    # (counter capture 03:41 UTC: hvt_miss=12/12 wrangler). KLIEGLIGHT is
    # a perishable SIGINT report at its core; HVT relevance is a downstream
    # fusion concern. Fire on every gate-1+2 pass. _signal_resolves_to_hvt
    # is preserved for the day cougaar adds the link fields.
    return True


def _signal_class(raw_record: dict) -> str:
    """Classify the source so the writer fills gap fields with a doctrinally
    correct N/A rather than 'Not reported'. Three classes:
      - radar    : pulsed radar emitter (TacElintReport with PRF/PD/modulation).
                   PRF / pulse_duration / modulation should all be populated
                   from intercept_record; gap = "Not in intercept record."
      - voice_fm : voice/FM-tagged comms intercept (SignalOfInterest with
                   modulation=FM, no PRF). PRF / pulse_duration are
                   doctrinally N/A; modulation populated from source.
      - comms    : COMINT bandwidth intercept (CommunicationsData). PRF /
                   pulse_duration N/A; modulation/frequency populated.
    """
    obj = raw_record.get("_objectType") or ""
    mod = (raw_record.get("modulation") or "").upper()
    if obj == "IDXTacElintReport":
        # EMTREW (modulation='EW') is still ELINT-class but lacks PRF — treat
        # as radar with intercept-record gaps rather than voice.
        return "radar"
    if obj == "IDXSignalOfInterestReport":
        if mod in ("FM", "AM", "SSB", "USB", "LSB", "CW"):
            return "voice_fm"
        return "voice_fm"  # default for SIGINT signals is voice
    if obj == "IDXCommunicationsData":
        return "comms"
    return "radar"  # conservative default


def _gap_token_for_field(field: str, signal_class: str) -> str:
    """Doctrinally-aware default for a KLIEGLIGHT paragraph 3/4 subfield when
    the source record doesn't carry it. Tells the writer what to emit so the
    rendered KLIEGLIGHT reads as a DELIBERATE gap with reason, not a generic
    'Not reported' that looks like a system failure."""
    radar_only = field in ("prf_pps", "pulse_duration")
    triangulation_only = field in ("elliptical_error_meters",
                                     "line_of_bearing_1", "line_of_bearing_2")
    if radar_only and signal_class != "radar":
        return f"'N/A — {signal_class.replace('_', '/')} intercept (no pulsed radar emission)'"
    if triangulation_only:
        return "'N/A — single-sensor intercept (no multi-bearing triangulation in this collect)'"
    if radar_only and signal_class == "radar":
        return "'Not in intercept record (radar collect missing this parameter)'"
    if field == "modulation" and signal_class == "radar":
        # TACELINT fixture rows from the IDX 26 event_data carry freq+PRF
        # but no modulation; that's the source format limitation, not a
        # missing field on the doctrinal record.
        return "'Not in intercept record (radar collect missing this parameter)'"
    if field == "frequency_mhz" and signal_class == "radar":
        return "'Not in intercept record (radar collect missing this parameter)'"
    return "'Not reported'"


def _build_autofire_context(raw_record: dict) -> str:
    """Build the prose context string passed to tool_generate_product's
    `context` arg. The DAG runner forwards this verbatim to every per-field
    LLM prompt under "ANALYST CONTEXT" — so this string is what the writer
    sees when deciding what to put in paragraph_1_emitter_target_identification,
    paragraph_3_frequency_and_parameters, etc.

    The raw_record may come from either:
      - cougaar live IDX vertex (sparse — uuid + ref_id + force_threat_designator)
      - event_data fixture (rich — emitter_name + frequency_mhz + lat/lon
        + intercept window etc.) per utils/event_data_fixtures.py

    We surface labeled, sectioned context so the writer fills each KLIEGLIGHT
    paragraph verbatim from the matching fields without having to guess
    which line is what.
    """
    obj_type = raw_record.get("_objectType") or "(unknown)"
    kbid = raw_record.get("uuid") or "(no kbID)"
    src = raw_record.get("_source") or "cougaar_live"
    source_serial = raw_record.get("_source_msg_serial") or raw_record.get("_source_report_id") or ""
    signal_class = _signal_class(raw_record)
    lines = [
        f"EVENT-DRIVEN KLIEGLIGHT auto-fire on newly-arrived Cougaar record.",
        f"Trigger source: {obj_type} kbID={kbid} (origin: {src}"
        + (f"; report_id={source_serial}" if source_serial else "")
        + ").",
        f"Signal class: {signal_class}.",
        "",
        "PROVENANCE TAG (cite verbatim in every paragraph's _provenance):",
        f"  {obj_type}.kbID={kbid}",
        "",
    ]

    # Section the record into the doctrinal KLIEGLIGHT paragraph buckets
    # so the writer's per-field prompt sees the relevant fields up top.
    def _emit(label: str, value):
        if value in (None, "", [], {}):
            return
        if isinstance(value, (dict, list)):
            try:
                value = json.dumps(value, default=str)
            except Exception:
                value = str(value)
        lines.append(f"  {label}: {value}")

    lines.append("Paragraph 1 — Emitter / Target Identification:")
    _emit("emitter_name", raw_record.get("emitter_name"))
    _emit("elnot_designator", raw_record.get("elnot_designator"))
    _emit("parent_threat_system", raw_record.get("parent_threat_system"))
    _emit("signal_id", raw_record.get("signal_id"))
    _emit("force_threat_designator", raw_record.get("force_threat_designator"))
    lines.append("")
    lines.append("Paragraph 2 — Time of Intercept:")
    _emit("intercept_start", raw_record.get("intercept_start"))
    _emit("intercept_end", raw_record.get("intercept_end"))
    _emit("start_time", raw_record.get("start_time"))
    _emit("end_time", raw_record.get("end_time"))
    _emit("reported_time", raw_record.get("reported_time"))
    lines.append("")
    lines.append("Paragraph 3 — Frequency & Parameters:")
    _emit("frequency_mhz", raw_record.get("frequency_mhz"))
    _emit("prf_pps", raw_record.get("prf_pps"))
    _emit("pulse_duration", raw_record.get("pulse_duration"))
    _emit("modulation", raw_record.get("modulation"))
    _emit("bandwidth", raw_record.get("bandwidth"))
    lines.append(
        f"  GAP RULES for paragraph 3: if a subfield is absent from the data "
        f"above, emit the EXACT string {_gap_token_for_field('prf_pps', signal_class)} "
        f"for prf_pps / pulse_duration; emit {_gap_token_for_field('modulation', signal_class)} "
        f"for modulation only when source genuinely lacks it."
    )
    lines.append("")
    lines.append("Paragraph 4 — Geolocation:")
    _emit("mgrs", raw_record.get("mgrs"))
    _emit("lat", raw_record.get("lat"))
    _emit("lon", raw_record.get("lon"))
    _emit("location", raw_record.get("location"))
    _emit("elliptical_error_meters", raw_record.get("elliptical_error_meters"))
    _emit("line_of_bearing_1", raw_record.get("line_of_bearing_1"))
    _emit("line_of_bearing_2", raw_record.get("line_of_bearing_2"))
    lines.append(
        f"  GAP RULES for paragraph 4: elliptical_error_meters / "
        f"line_of_bearing_1 / line_of_bearing_2 require multi-sensor "
        f"triangulation that isn't captured in single-collect intercept "
        f"records — when absent, emit "
        f"{_gap_token_for_field('line_of_bearing_1', signal_class)}."
    )
    lines.append("")
    lines.append("Paragraph 6 — Action Taken (GAP RULE):")
    lines.append(
        "  This is an event-driven autofire; no SIGINT cell action history "
        "is captured at intercept time. Emit "
        "'Pending SIGINT cell action — KLIEGLIGHT generated automatically "
        "from intercept event; analyst-driven actions (AFATDS coordinate "
        "push, EOB update, aviation rerouting) to be appended on review.' "
        "Do NOT invent specific actions that did not occur."
    )
    lines.append("")
    lines.append("Other raw fields (verbatim from source):")
    for k, v in raw_record.items():
        if k.startswith("_") or v in (None, "", [], {}):
            continue
        if k in {
            "uuid", "ref_id", "emitter_name", "elnot_designator",
            "parent_threat_system", "signal_id", "force_threat_designator",
            "intercept_start", "intercept_end", "start_time", "end_time",
            "reported_time", "frequency_mhz", "prf_pps", "pulse_duration",
            "modulation", "bandwidth", "mgrs", "lat", "lon", "location",
            "elliptical_error_meters", "line_of_bearing_1", "line_of_bearing_2",
        }:
            continue
        _emit(k, v)

    lines.extend([
        "",
        "Rules:",
        "  - Fill each paragraph verbatim from the matching fields above.",
        "  - For missing subfields use the GAP RULE strings above — do NOT "
        "default to 'Not reported' on doctrinally-N/A fields.",
        "  - Every paragraph's _provenance MUST cite the PROVENANCE TAG above.",
        "  - Do NOT aggregate across other signals; this fires on THIS detection only.",
    ])
    return "\n".join(lines)


_AUTODRAFT_USER_ID = "_autodraft"


# Hook for the notification module. The KLIEGLIGHT auto-fire calls this
# AFTER a successful artifact emission so the notification module can
# persist + broadcast a {"type": "notification", ...} frame. We don't
# import the notifications module at module level to avoid a circular
# import — main.py wires the hook at startup.
_notification_emitter = None


def register_notification_emitter(emitter) -> None:
    """Wire an async callable that receives the post-fire notification payload.

        async def emitter(payload: dict) -> None

    Payload shape:
        {
          "type": "klieglight_autofire",
          "artifact_id": str,
          "autodraft_session_id": str,
          "kbid": str,
          "emitter_name": str | None,
          "mgrs": str | None,
          "location": dict | None,
          "fired_at_ms": int,
        }
    """
    global _notification_emitter
    _notification_emitter = emitter


def _bearer_mgrs_for_signal(raw_record: dict) -> tuple[str, str]:
    """When the SIGINT/COMMS record lacks its own coordinates (sparse
    live-cougaar IDX vertices today ship only uuid + ref_id +
    force_threat_designator), fall back to the bearer unit's MGRS so
    the writer fills paragraph_4_geolocation_mgrs with a real coord
    instead of the '[ANALYST INPUT REQUIRED]' placeholder and the
    operations-map signal-report layer can drop an icon at the
    correct point.

    Walks the same parent-id keys _signal_resolves_to_hvt uses
    (parent_kg_id / unit_kg_id / entity_id / kg_id / owning_unit_kg_id
    / source_kbid) against the resolved RED OOB snapshot to find the
    holding unit, then returns (bearer_kg_id, bearer_mgrs). Returns
    ("", "") when no parent id resolves (signal was admitted by the
    HVTL gate's tier-1 text-substring match, not a kg_id link) — the
    writer's placeholder stands and the signal-report layer skips
    plotting until a future autofire carries a real coord.
    """
    from pathlib import Path as _P
    try:
        from utils import oob as _oob
        units = _oob.resolved_named_units()
        if not units:
            return ("", "")
        by_kg = {}
        for u in units:
            kgid = (u.get("id") or "").strip()
            if kgid:
                by_kg[kgid] = u
        for key in ("parent_kg_id", "unit_kg_id", "entity_id",
                      "kg_id", "owning_unit_kg_id", "source_kbid"):
            v = raw_record.get(key)
            if v and str(v).strip() in by_kg:
                u = by_kg[str(v).strip()]
                return (u.get("id") or "", (u.get("location_mgrs") or "").strip())
    except Exception:
        log.debug("KLIEGLIGHT bearer-MGRS lookup failed", exc_info=True)
    return ("", "")


async def _fire_one(raw_record: dict, project_id: str = "") -> dict:
    """Generate a single KLIEGLIGHT draft for one triggering record,
    into `project_id`'s artifact pool.

    Returns a dict describing the fire result. Raises only on programming
    errors (bad imports etc.); operational failures (DAG validation, vLLM
    timeout) are captured into the returned dict.
    """
    # Imports inside the function so that import-time ordering with main.py
    # / handlers/intel_agent.py stays clean (avoiding any circulars when
    # the poller fires before all handlers have been imported).
    from handlers import intel_agent as ia
    from handlers.intel_agent import State, tool_generate_product
    from utils import coords

    kbid = raw_record.get("uuid") or ""
    obj_type = raw_record.get("_objectType") or "(unknown)"

    # Sparse-signal MGRS seeding. When the raw record has no mgrs/lat/lon
    # of its own (live cougaar IDX SIGINT vertices today), seed a coordinate
    # into raw_record so the writer fills paragraph_4 with a real coord
    # instead of the placeholder. Same record is forwarded to
    # _build_autofire_context below, which surfaces 'mgrs' in the Paragraph 4
    # section the writer reads. No-op when the record already has its own mgrs
    # (rich event_data fixture path).
    if not raw_record.get("mgrs") and not raw_record.get("lat"):
        # Tier 1 — the signal's own emitter geolocation, resolved by
        # cougaar_adapters from the AT_EMITTER_LOCATION / AT_WRANGLER_LOCATION
        # edge and stamped onto raw_record['location'] as a GeoJSON Point.
        # This is the actual emitter fix, so it beats the owning-unit MGRS.
        loc = raw_record.get("location")
        xy = loc.get("coordinates") if isinstance(loc, dict) else None
        if xy and len(xy) >= 2:
            lon, lat = xy[0], xy[1]
            try:
                mgrs = coords.latlon_to_mgrs(lat, lon)
                raw_record["lat"], raw_record["lon"] = lat, lon
                raw_record["mgrs"] = mgrs
                raw_record["_geo_source"] = "emitter_location_edge"
                log.info(
                    "KLIEGLIGHT auto-fire emitter-edge geo: kbid=%s mgrs=%s "
                    "(%.5f, %.5f)", kbid, mgrs, lat, lon,
                )
            except Exception:
                log.warning(
                    "KLIEGLIGHT emitter-edge MGRS convert failed: kbid=%s",
                    kbid, exc_info=True,
                )
        # Tier 2 — fall back to the bearer (owning) unit's MGRS when the
        # signal has no emitter-location edge (about half the TacElints).
        if not raw_record.get("mgrs"):
            bearer_kg, bearer_mgrs = _bearer_mgrs_for_signal(raw_record)
            if bearer_mgrs:
                raw_record["mgrs"] = bearer_mgrs
                raw_record["_geo_source"] = f"bearer_unit:{bearer_kg or 'unknown'}"
                log.info(
                    "KLIEGLIGHT auto-fire MGRS seed: kbid=%s bearer_kg=%s mgrs=%s",
                    kbid, bearer_kg, bearer_mgrs,
                )
    autodraft_session_id = f"autodraft-{uuid.uuid4()}"
    fired_at_ms = int(time.time() * 1000)

    log.info(
        "KLIEGLIGHT auto-fire START: kbid=%s obj_type=%s session=%s",
        kbid, obj_type, autodraft_session_id,
    )

    # Fresh State pre-seeded for manual KLIEGLIGHT generation. Mirrors the
    # /api/products/generate-async path so the DAG runner sees the same
    # state shape it expects from the analyst Generate-button flow. No DB
    # load — this session has no prior history.
    from utils.project_context import set_active_project
    set_active_project(project_id)
    state = State.fresh(
        autodraft_session_id,
        user_id=_AUTODRAFT_USER_ID,
        manual_product_type="KLIEGLIGHT",
        project_id=project_id,
    )

    args = {
        "product_type": "KLIEGLIGHT",
        "session_id": autodraft_session_id,
        "user_id": _AUTODRAFT_USER_ID,
        "project_id": project_id,
        "context": _build_autofire_context(raw_record),
    }

    try:
        result = await tool_generate_product(args, state, on_event=None)
    except Exception as e:
        log.exception("KLIEGLIGHT auto-fire failed: kbid=%s", kbid)
        entry = {
            "kbid": kbid,
            "obj_type": obj_type,
            "artifact_id": None,
            "autodraft_session_id": autodraft_session_id,
            "fired_at_ms": fired_at_ms,
            "error": f"{type(e).__name__}: {e}",
        }
        return entry

    if isinstance(result, dict) and result.get("error"):
        log.warning(
            "KLIEGLIGHT auto-fire DAG returned error: kbid=%s err=%s detail=%s",
            kbid, result.get("error"), result.get("detail"),
        )
        entry = {
            "kbid": kbid,
            "obj_type": obj_type,
            "artifact_id": None,
            "autodraft_session_id": autodraft_session_id,
            "fired_at_ms": fired_at_ms,
            "error": f"{result.get('error')}: {result.get('detail') or ''}",
        }
        return entry

    artifact_id = (result or {}).get("artifact_id") or ""
    structured = (result or {}).get("structured_data") or {}
    header = structured.get("header") or {}
    # Pull MGRS out of paragraph_4 if present — the schema declares it as
    # a dict subfield. Fall back to the raw_record.location lat/lon dump.
    mgrs = ""
    p4 = structured.get("paragraph_4_geolocation_mgrs") or {}
    if isinstance(p4, dict):
        mgrs = (p4.get("mgrs") or "") or ""

    log.info(
        "KLIEGLIGHT auto-fire DONE: kbid=%s artifact_id=%s mgrs=%s",
        kbid, artifact_id, mgrs or "(none)",
    )

    entry = {
        "kbid": kbid,
        "obj_type": obj_type,
        "artifact_id": artifact_id,
        "autodraft_session_id": autodraft_session_id,
        "kliegl_number": header.get("kliegl_number") or "",
        "emitter_name": raw_record.get("emitter_name") or raw_record.get("name") or "",
        "mgrs": mgrs,
        "location": raw_record.get("location"),
        "fired_at_ms": fired_at_ms,
        "error": None,
    }

    # Emit the notification via the wired emitter (if any). Failures here
    # are LOGGED but don't roll back the fire — the draft still exists on
    # disk + in the DB even if the alert pipe is broken.
    if _notification_emitter is not None:
        try:
            await _notification_emitter({
                "type": "klieglight_autofire",
                "artifact_id": artifact_id,
                "autodraft_session_id": autodraft_session_id,
                "kbid": kbid,
                "kliegl_number": header.get("kliegl_number") or "",
                "emitter_name": entry["emitter_name"],
                "mgrs": mgrs,
                "location": raw_record.get("location"),
                "fired_at_ms": fired_at_ms,
            })
        except Exception:
            log.exception("KLIEGLIGHT notification emitter raised")

    return entry


async def _fire_one_registered(raw_record: dict, project_id: str = "") -> dict:
    """Run _fire_one through the unified product_records registry so
    the unified UI sees KLIEGLIGHT autofires alongside autodraft sweep
    fires. Returns the legacy entry dict for back-compat.

    The KLIEGLIGHT-specific notification emission inside _fire_one
    stays in place — the unified record_upsert is purely additive."""
    from handlers import product_records as _pr

    captured: dict[str, dict] = {}

    async def _coro(record):
        # No phase=writer transition here — _fire_one wraps a single
        # tool_generate_product call. The unified row stays at
        # phase=awaiting_gate→writer (transition on terminal) since the
        # autofire is short (~5-15s) and intermediate phases would be
        # noisy.
        entry = await _fire_one(raw_record, project_id=project_id)
        captured["entry"] = entry
        # Stamp artifact_id on success so the chain row shows it.
        if entry.get("artifact_id") and not entry.get("error"):
            await _pr.record_transition(
                record.record_id,
                event_type="phase",
                phase="completed",
                artifact_id=entry["artifact_id"],
                autodraft_session_id=entry.get("autodraft_session_id"),
            )
            # Idempotence: record this kbid on the persistent fired log
            # so on_poll_callback never re-fires the same signal — the
            # cougaar poller's in-memory seen-set resets on container
            # restart, so without this every KL would re-fire on the
            # next poll.
            _record_fired_kbid(
                _fired_key(project_id, raw_record.get("uuid") or ""))
        elif entry.get("error"):
            # Surface DAG / writer errors so register_and_run's
            # except-arm transitions to failed.
            raise RuntimeError(str(entry["error"]))
        return entry

    record_id = await _pr.register_and_run(
        source="klieglight_autofire",
        product_type="KLIEGLIGHT",
        request_payload={
            "kbid": raw_record.get("uuid") or "",
            "obj_type": raw_record.get("_objectType") or "",
            # Persist the full raw_record so the retry dispatcher in
            # main.py can re-fire _fire_one(raw_record). Without this,
            # every manual retry of a failed KLIEGLIGHT chain fires
            # _fire_one({}) and either rejects (no _objectType) or
            # produces a vacuous artifact.
            "raw_record": dict(raw_record),
        },
        coro_factory=_coro,
        source_metadata={
            "kbid": raw_record.get("uuid") or "",
            "obj_type": raw_record.get("_objectType") or "",
            "project_id": project_id,
        },
    )
    task = _pr._RUNNING_TASKS.get(record_id)  # type: ignore[attr-defined]
    if task is not None:
        try:
            await task
        except (Exception, asyncio.CancelledError):
            pass
    if "entry" not in captured:
        captured["entry"] = {
            "kbid": raw_record.get("uuid") or "",
            "obj_type": raw_record.get("_objectType") or "",
            "artifact_id": None,
            "autodraft_session_id": None,
            "fired_at_ms": int(time.time() * 1000),
            "error": "cancelled or interrupted before completion",
        }
    return captured["entry"]


async def sweep_for_project(project_id: str) -> dict:
    """Fire a KLIEGLIGHT into `project_id`'s pool for every qualifying
    signal row in the scenario data. THE fire path since the Cougaar
    poll plane was disconnected (2026-07-09): scenario data is static,
    so one sweep at PROJECT CREATION covers every signal the event
    carries. Idempotent via the persistent fired-kbid log (keys
    '<project_id>:<kbid>'), so operator re-runs and bootstrap retries
    never duplicate artifacts.

    Sequential fires — parallel would saturate the writer LLM.
    """
    from utils import scenario
    from utils.event_data_fixtures import build_fixture_raw_records

    raw_by_kbid = build_fixture_raw_records(scenario.KG.get("report") or [])
    already = _load_fired_kbids()
    candidates = [(k, r) for k, r in raw_by_kbid.items() if _should_fire(r)]

    fired = 0
    errors = 0
    already_fired = 0
    for kbid, raw in candidates:
        if _fired_key(project_id, kbid) in already:
            already_fired += 1
            continue
        try:
            entry = await _fire_one_registered(raw, project_id=project_id)
            if entry and entry.get("error"):
                errors += 1
            elif entry and entry.get("artifact_id"):
                fired += 1
        except Exception:
            log.exception("KLIEGLIGHT project sweep: _fire_one raised for "
                          "kbid=%s project=%s", kbid, project_id)
            errors += 1

    log.info(
        "KLIEGLIGHT project sweep complete: project=%s candidates=%d "
        "already_fired=%d fired=%d errors=%d",
        project_id, len(candidates), already_fired, fired, errors,
    )
    return {
        "project_id": project_id,
        "candidates": len(candidates),
        "already_fired": already_fired,
        "fired": fired,
        "errors": errors,
    }


async def force_refire(kbids: list[str] | None = None) -> dict:
    """Operator-triggered force refire — fires KLIEGLIGHTs for the given
    kbIDs (or every qualifying scenario signal row when kbids is None)
    into EVERY active project's pool, BYPASSING the fired-kbid dedup.
    Use after a schema / writer-prompt / signal-class fix when you want
    fresh KLIEGLIGHTs even though the prior ones already landed.

    `_should_fire` is still consulted so only records passing the
    doctrinal vertex-type + minimum-signal-data gates fire; the bypass
    is the dedup gate only.
    """
    from utils import scenario
    from utils.event_data_fixtures import build_fixture_raw_records
    from handlers.intel_agent import active_project_ids

    raw_by_kbid = build_fixture_raw_records(scenario.KG.get("report") or [])
    kbid_filter: set[str] | None = set(kbids) if kbids else None

    candidates: list[tuple[str, dict]] = []
    for kbid, raw in raw_by_kbid.items():
        if not _should_fire(raw):
            continue
        if kbid_filter is not None and kbid not in kbid_filter:
            continue
        candidates.append((kbid, raw))

    fired = 0
    errors = 0
    for kbid, raw in candidates:
        for fire_project_id in active_project_ids():
            try:
                entry = await _fire_one_registered(raw, project_id=fire_project_id)
                if entry and entry.get("error"):
                    errors += 1
                elif entry and entry.get("artifact_id"):
                    fired += 1
            except Exception:
                log.exception("KLIEGLIGHT force_refire: _fire_one raised for kbid=%s", kbid)
                errors += 1

    log.info(
        "KLIEGLIGHT force_refire complete: candidates=%d fired=%d errors=%d",
        len(candidates), fired, errors,
    )
    return {
        "candidates": len(candidates),
        "fired": fired,
        "errors": errors,
    }
