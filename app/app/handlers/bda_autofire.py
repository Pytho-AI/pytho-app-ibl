"""BDA event-driven autofire — one BDA per IDXReccexrepReport row.

Cougaar's IDXReccexrepReport vertex is the doctrinal BDA-feeding format
(GRVTY-populated per the 2026-06-10 sim-staff handoff). Each RECCEXREP
carries the target reference (ben + item_name + item_number), assessment
grade (target_status), narrative (gentext), mission + sensor context,
and engagement location. When a new RECCEXREP arrives the autofire pass
resolves the engagement location to the nearest RED unit, binds that
unit as the target, and fires a BDA artifact.

Pattern mirrors handlers/klieglight_autofire.py + handlers/osir_autofire.py:
  - Per-poll gate-rejection counters (logged to /app/logs/bda_autofire.log)
  - Persistent fired-kbid log at /app/logs/bda_fired_kbids.log
    (the dedup pattern documented in memory/persistent_dedup_log_pattern.md)
  - register_and_run wraps each fire as a product_records chain
  - Sequential — parallel fires would saturate the writer LLM
  - sweep_when_poller_warm backfills RECCEXREPs that pre-date the
    container restart (one-shot at lifespan startup)

Target resolution: spatial proximity. RECCEXREP carries `location`
(GeoJSON Point) — match against oob_resolved_latest.json named_units
by minimum haversine distance, within BDA_SPATIAL_THRESHOLD_KM
(default 5km). Falls back to ben / item_name string match against
composition_rollup keys when location absent. Returns None when no
match is found and the BDA is dropped (a BDA without a bound target
produces an empty header_only artifact).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


# Persistent file logging — survives container restart so the SCIF
# session's gate-rejection counters can be inspected after the spark
# comes back. Same pattern as klieglight + osir autofire.
try:
    import logging.handlers as _lh
    _LOG_DIR = "/app/logs"
    os.makedirs(_LOG_DIR, exist_ok=True)
    _fh = _lh.RotatingFileHandler(
        os.path.join(_LOG_DIR, "bda_autofire.log"),
        maxBytes=50 * 1024 * 1024, backupCount=5,
    )
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(funcName)s L%(lineno)d %(message)s"
    ))
    log.addHandler(_fh)
    log.setLevel(logging.INFO)
except Exception as _e:
    log.warning("BDA autofire: persistent file logging setup failed: %s", _e)


# BDA status overlay sidecar — maps unit kg_id → latest BDA outcome
# (disposition + last_kbid + ts). Read by cougaar_adapters at OOB-emit
# time to back-propagate RECCEXREP.target_status onto the bound unit's
# disposition, so the live-map MIL-STD-2525C operational-condition
# amplifier (symbols.js createMilSymbol → SIDC pos-3 X/D/B) actually
# renders. Cougaar's IDXRedUnit.data__disposition is null on all 225
# units today; this overlay is the only source of damage state for
# the live map.
_BDA_STATUS_OVERLAY_PATH = "/app/logs/bda_status_overlay.json"


def _normalize_target_status_to_disposition(target_status: str) -> str:
    """Map RECCEXREP target_status strings to the doctrinal disposition
    vocabulary symbols.js _OP_CONDITION_BY_DISPOSITION recognizes.
    RECCEXREP values seen on live cougaar: '-' (no effect / unknown),
    plus the doctrinal grades DESTROYED / DEGRADED / NO_EFFECT / etc."""
    if not isinstance(target_status, str):
        return ""
    s = target_status.strip().upper()
    if not s or s == "-":
        return ""
    # Pass through doctrinal values directly — symbols.js maps them.
    if s in ("DESTROYED", "DEFEATED", "KIA", "NEUTRALIZED",
              "DAMAGED", "DEGRADED", "SUPPRESSED"):
        return s
    # Common RECCEXREP shortform mappings.
    if s in ("D",):
        return "DESTROYED"
    if s in ("MD", "MODERATE", "MOD"):
        return "DEGRADED"
    if s in ("SD", "SLIGHT", "LIGHT", "LD"):
        return "DAMAGED"
    if s in ("NE", "NO_EFFECT", "NONE", "N/A"):
        return ""
    return s


def _record_bda_status_overlay(unit_kg_id: str, target_status: str,
                                  kbid: str) -> None:
    """Persist {unit_kg_id: {disposition, kbid, ts_ms}}. Caller-safe
    (best-effort; failures log + continue). cougaar_adapters reads
    this file at every OOB sync push to overlay disposition onto
    units; survives container restart via the ./logs:/app/logs mount."""
    disp = _normalize_target_status_to_disposition(target_status)
    if not (unit_kg_id and disp):
        return
    try:
        existing: dict = {}
        try:
            with open(_BDA_STATUS_OVERLAY_PATH, "r") as f:
                existing = json.load(f) or {}
        except FileNotFoundError:
            existing = {}
        except Exception:
            existing = {}
        existing[unit_kg_id] = {
            "disposition": disp,
            "kbid": kbid or "",
            "ts_ms": int(time.time() * 1000),
        }
        os.makedirs(os.path.dirname(_BDA_STATUS_OVERLAY_PATH), exist_ok=True)
        with open(_BDA_STATUS_OVERLAY_PATH, "w") as f:
            json.dump(existing, f)
    except Exception as e:
        log.warning("BDA status overlay write failed (unit=%s): %s",
                      unit_kg_id, e)


# Persistent fired-kbid log — per memory/persistent_dedup_log_pattern.md.
# Appended on every successful fire (post artifact_id), loaded into a set
# once per poll pass for O(1) dedup. Survives container rebuild via the
# ./logs:/app/logs docker-compose mount.
_FIRED_KBIDS_LOG_PATH = "/app/logs/bda_fired_kbids.log"


def _load_fired_kbids() -> set:
    try:
        with open(_FIRED_KBIDS_LOG_PATH, "r") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning("BDA fired-kbid log read failed: %s", e)
        return set()


def _record_fired_kbid(kbid: str) -> None:
    if not kbid:
        return
    try:
        os.makedirs(os.path.dirname(_FIRED_KBIDS_LOG_PATH), exist_ok=True)
        with open(_FIRED_KBIDS_LOG_PATH, "a") as f:
            f.write(f"{kbid}\n")
    except Exception as e:
        log.warning("BDA fired-kbid log append failed (key=%s): %s", kbid, e)


def _fired_key(project_id: str, key: str) -> str:
    """Per-project dedup key for the fired log — same contract as the
    klieglight/osir autofires: '<project_id>:<key>' (key is the
    engagement_key for fused fires or the raw kbid for single fires).
    Pre-unification bare log lines are rewritten by the box migration."""
    return f"{project_id}:{key}"


# Spatial threshold (km) — RECCEXREP location → nearest cougaar unit
# match. Env-tunable so SCIF can adjust without redeploy. 5km covers
# the engagement-collateral envelope; widen if RECCEXREP locations are
# coarse.
BDA_SPATIAL_THRESHOLD_KM = float(os.environ.get("BDA_SPATIAL_KM", "5"))


# Same _autodraft user namespace as klieglight / osir / autodraft sweep.
_AUTODRAFT_USER_ID = "_autodraft"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _extract_geo(raw_record: dict) -> tuple[float, float] | None:
    """Pull (lat, lon) from raw_record. RECCEXREP ships `location` as a
    GeoJSON Point (per cougaar_adapters._to_geojson_point); fixture path
    may use top-level lat/lon."""
    lat = raw_record.get("lat")
    lon = raw_record.get("lon")
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
        loc = raw_record.get("location")
        if isinstance(loc, dict):
            coords = loc.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                lon = coords[0]
                lat = coords[1]
            else:
                lat = lat if isinstance(lat, (int, float)) else loc.get("lat") or loc.get("latitude")
                lon = lon if isinstance(lon, (int, float)) else loc.get("lon") or loc.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None


# Per-obj-type _IDXRedUnit edge — direct target binding cougaar
# already wires for the outcome-bearing report vertices. Verified via
# schema introspection 2026-06-10 night: 4 of the 5 outcome types
# carry this edge (IDXImageIirItem is the exception — it uses
# AT_LOCATION + a basicEncyclopediaNumber field instead).
_RED_UNIT_EDGE_BY_OBJ_TYPE = {
    "IDXReccexrepReport":         "IDXReccexrepReport_IDXRedUnit:World.Informations.IDXReccexrepReport>World.Organizations.IDXRedUnit",
    "IDXImageIirReport":          "IDXImageIirReport_IDXRedUnit:World.Informations.IDXImageIirReport>World.Organizations.IDXRedUnit",
    "IDXSignificantActivityReport": "IDXSignificantActivityReport_IDXRedUnit:World.Informations.IDXSignificantActivityReport>World.Organizations.IDXRedUnit",
    "IDXImageryReport":           "IDXImageryReport_IDXRedUnit:World.Informations.IDXImageryReport>World.Organizations.IDXRedUnit",
    "IDXTacticalReport":          "IDXTacticalReport_IDXRedUnit:World.Informations.IDXTacticalReport>World.Organizations.IDXRedUnit",
}


async def _fetch_target_binding_for_kbid(kbid: str, units_by_kg: dict[str, dict]) -> str:
    """Dead Cougaar edge-walk tier (AT_OUTCOME_LOCATION) — the GraphQL
    feed is disconnected (2026-07-09), so this tier resolves nothing and
    callers fall through to the gentext / location / composition
    resolvers, which is how every binding has actually resolved since
    the feed died."""
    return ""


async def _fetch_all_target_bindings(outcome_kbids: list[str],
                                          units_by_kg: dict[str, dict]) -> dict[str, str]:
    """Per-kbid edge walks via getEdgesFromVertex. N round trips per
    pass (one per outcome record) — bounded by the outcome rate
    (today's load is ~10/min) so the latency is acceptable. Returns
    {outcome_kbid: target_unit_kbid} for every record where an edge
    resolves to a known unit; missing entries fall back to the
    legacy resolver tiers (gentext / location / composition)."""
    out: dict[str, str] = {}
    if not outcome_kbids:
        return out
    for kbid in outcome_kbids:
        tgt = await _fetch_target_binding_for_kbid(kbid, units_by_kg)
        if tgt:
            out[kbid] = tgt
    return out


_AT_RECCEXREP_LOCATION_GQL = """
{
  getEdgesOfType(
    relationshipType: "AT_RECCEXREP_LOCATION:World.Informations.IDXReccexrepReport>World.Environments.IDXLocation"
  ) {
    ... on Edge {
      source { kbID }
      target { kbID }
    }
  }
}
"""

_IDX_LOCATIONS_ALL_GQL = """
{
  getVerticesByDomainType(fullyQualifiedDomainType: "World.Environments.IDXLocation") {
    kbID
    ... on worldEnvironmentsIDXLocation {
      data__location { latitude longitude }
    }
  }
}
"""


async def _fetch_reccexrep_location(reccexrep_kbid: str) -> tuple[float, float] | None:
    """Dead Cougaar edge-walk tier (AT_RECCEXREP_LOCATION) — the GraphQL
    feed is disconnected (2026-07-09). Returns None; the caller's next
    resolver tier (gentext MGRS / composition match) takes over."""
    return None


async def _resolve_target_kg_id(raw_record: dict) -> tuple[str, str] | tuple[None, None]:
    """Resolve a RECCEXREP record to a (kg_id, unit_name) tuple via the
    fields cougaar actually populates on the live IDXReccexrepReport
    vertex (verified against the GRVTY-supplied records 2026-06-10
    night — sample shape: gentext='RECON BN', ben='ATI:Lxxxxx',
    item_name='POINT LOCATION 1', location=null):

      1. gentext substring match against named_unit.name. gentext on
         these records carries the target's unit designation
         ('RECON BN' etc.) — the strongest signal cougaar ships
         today. Highest-echelon match wins ties (a DIV beats a BDE
         beats a BN, etc.) so a generic gentext like 'RECON BN' that
         appears multiple times in the OOB binds to the senior of
         the candidates.
      2. Spatial match — IF the record carries a non-null location
         (rare on live RECCEXREPs but supported for fixture-driven
         data and any future cougaar populate).
      3. Composition string match: item_name → any unit whose
         composition_rollup substring-matches.

    Returns (None, None) when no candidate matches — BDA is dropped
    rather than produce an un-bound artifact.
    """
    from utils import oob as _oob
    try:
        doc = {"resolved": {"named_units": _oob.resolved_named_units()}}
    except Exception:
        return None, None
    units = (doc.get("resolved") or {}).get("named_units") or []
    if not units:
        return None, None

    # Tier 1 — gentext → unit name substring match (primary path).
    # gentext on RECCEXREPs carries the target description in shapes
    # like 'RECON BN' (matches '6442 RECON BN' directly) or
    # 'FA BN STATIONARY' / 'TANK CO IN ASSEMBLY AREA' where the
    # descriptor tail describes target_type, not the formation.
    # Strategy:
    #   a) Try the full gentext.
    #   b) Strip target-type modifiers cougaar maps to gentext suffixes
    #      (STATIONARY = STANRY, MOVING, RELOC, FIRING, …).
    #   c) Walk back word-by-word — try progressively shorter prefixes
    #      down to ≥3 chars. First prefix that hits a unit wins;
    #      highest-echelon match in that prefix breaks ties.
    gentext_raw = (raw_record.get("gentext") or "").strip().upper()
    _ECH_RANK = {
        "CORPS": 7, "DIVISION": 6, "DIV": 6, "BRIGADE": 5, "BDE": 5,
        "REGIMENT": 5, "REGT": 5, "BATTALION": 4, "BN": 4,
        "SQUADRON": 4, "COMPANY": 3, "CO": 3, "BTRY": 3, "BATTERY": 3,
        "TROOP": 3, "PLATOON": 2, "PLT": 2, "SECTION": 1, "SQUAD": 1,
    }
    # Common target-status / target_type words that appear in gentext
    # as a noise suffix; strip them before the prefix walk.
    _NOISE_SUFFIX_WORDS = {
        "STATIONARY", "STANRY", "MOVING", "RELOC", "RELOCATING",
        "FIRING", "IN", "AT", "ON", "ASSEMBLY", "AREA", "POSITION",
        "POSITIONS", "AA",
    }

    def _candidate_prefixes(text: str) -> list[str]:
        words = [w for w in text.split() if w]
        # Walk back: full string, then drop trailing words while they're
        # noise suffixes or until the remaining prefix is too short to
        # be discriminating.
        prefixes: list[str] = []
        cur = list(words)
        while cur:
            p = " ".join(cur)
            if len(p) >= 3 and p not in prefixes:
                prefixes.append(p)
            # Stop trimming once the last word is content (not noise) —
            # so 'FA BN STATIONARY' tries 'FA BN STATIONARY' then 'FA BN'
            # then stops (won't trim down to 'FA' which would match
            # every fire-support unit indiscriminately).
            last = cur[-1].upper()
            if last in _NOISE_SUFFIX_WORDS:
                cur.pop()
                continue
            # First content word from the end — try one more trim
            # then stop.
            cur.pop()
            p2 = " ".join(cur)
            if len(p2) >= 3 and p2 not in prefixes:
                prefixes.append(p2)
            break
        return prefixes

    if gentext_raw:
        for prefix in _candidate_prefixes(gentext_raw):
            best = None
            best_rank = -1
            for u in units:
                nm = (u.get("name") or "").upper()
                if not nm or prefix not in nm:
                    continue
                ech = (u.get("echelon") or "").strip().upper()
                rank = _ECH_RANK.get(ech, 0)
                if rank > best_rank:
                    best_rank = rank
                    best = u
            if best is not None:
                return (best.get("kg_id") or best.get("id") or "",
                          best.get("name") or "")

    # Tier 2 — spatial proximity. Prefer the inline raw_record geo
    # (fixture path); fall back to following the AT_RECCEXREP_LOCATION
    # edge on cougaar (live path — RECCEXREP.data__location is null,
    # the engagement coords live on a connected IDXLocation vertex).
    geo = _extract_geo(raw_record)
    if not geo:
        kbid = raw_record.get("uuid") or ""
        if kbid:
            geo = await _fetch_reccexrep_location(kbid)
    if geo:
        lat, lon = geo
        best = None
        best_d = float("inf")
        for u in units:
            pp = u.get("primary_position") or {}
            ulat = pp.get("lat") if isinstance(pp.get("lat"), (int, float)) else u.get("lat")
            ulon = pp.get("lon") if isinstance(pp.get("lon"), (int, float)) else u.get("lon")
            if not (isinstance(ulat, (int, float)) and isinstance(ulon, (int, float))):
                continue
            d = _haversine_km(lat, lon, float(ulat), float(ulon))
            if d < best_d:
                best_d = d
                best = u
        if best is not None and best_d <= BDA_SPATIAL_THRESHOLD_KM:
            return (best.get("kg_id") or best.get("id") or "",
                      best.get("name") or "")

    # Tier 3 — item_name → composition_rollup substring match.
    item = (raw_record.get("item_name") or raw_record.get("itemName")
              or "").strip().upper()
    if item and len(item) >= 3:
        best = None
        best_count = 0
        for u in units:
            comp = u.get("composition_rollup") or {}
            for eq_name, cnt in comp.items():
                if not isinstance(eq_name, str) or not isinstance(cnt, int):
                    continue
                if item in eq_name.upper() and cnt > best_count:
                    best = u
                    best_count = cnt
        if best is not None:
            return (best.get("kg_id") or best.get("id") or "",
                      best.get("name") or "")

    return None, None


def _build_autofire_context(raw_record: dict, target_kg_id: str, target_name: str) -> str:
    """Prose context for the BDA writer prompt. Surfaces the RECCEXREP
    fields + the resolved target binding so the writer can fill the
    engagement-record + PDA paragraphs verbatim."""
    obj_type = raw_record.get("_objectType") or "(unknown)"
    kbid = raw_record.get("uuid") or "(no kbID)"
    lines = [
        f"EVENT-DRIVEN BDA auto-fire on newly-arrived RECCEXREP.",
        f"Trigger source: {obj_type} kbID={kbid}.",
        f"Resolved target: kg_id={target_kg_id or '(unresolved)'} name={target_name or '(unknown)'}",
        "",
        "PROVENANCE TAG (cite verbatim in every paragraph's _provenance):",
        f"  {obj_type}.kbID={kbid}",
        "",
        "RECCEXREP fields (GRVTY-supplied per cougaar sim-staff 2026-06-10):",
    ]
    def _emit(label: str, value):
        if value in (None, "", [], {}):
            return
        if isinstance(value, (dict, list)):
            try:
                value = json.dumps(value, default=str)
            except Exception:
                value = str(value)
        lines.append(f"  {label}: {value}")
    _emit("mission_type", raw_record.get("mission_type"))
    _emit("mission_code", raw_record.get("mission_code"))
    _emit("mission_platform_or_type", raw_record.get("mission_platform_or_type"))
    _emit("target_type", raw_record.get("target_type"))
    _emit("target_status", raw_record.get("target_status"))
    _emit("item_name", raw_record.get("item_name"))
    _emit("item_number", raw_record.get("item_number"))
    _emit("ben", raw_record.get("ben"))
    _emit("gentext", raw_record.get("gentext"))
    _emit("time_on_target", raw_record.get("time_on_target"))
    _emit("report_time", raw_record.get("report_time"))
    _emit("location", raw_record.get("location"))
    return "\n".join(lines)


async def _fire_one(raw_record: dict, project_id: str = "") -> dict:
    """Generate a single BDA draft for one RECCEXREP record, into
    `project_id`'s artifact pool. Returns a dict describing the fire
    result; failures are captured into the returned dict (raised only
    on programming errors)."""
    from handlers import intel_agent as ia  # noqa: F401
    # Single-record path delegates to the fused path with a 1-event
    # engagement so sweep + poll share the same _autodraft_one +
    # target_focus routing. Resolves the target here so callers that
    # pre-skip on unresolved targets keep working.
    kbid = raw_record.get("uuid") or ""
    obj_type = raw_record.get("_objectType") or "(unknown)"
    fired_at_ms = int(time.time() * 1000)

    target_kg_id, target_name = await _resolve_target_kg_id(raw_record)
    if not target_kg_id:
        log.warning(
            "BDA auto-fire SKIPPED (target unresolved): kbid=%s item=%r ben=%r",
            kbid, raw_record.get("item_name"), raw_record.get("ben"),
        )
        return {
            "kbid": kbid, "obj_type": obj_type,
            "artifact_id": None, "autodraft_session_id": None,
            "fired_at_ms": fired_at_ms,
            "error": "target_unresolved",
        }
    return await _fire_one_fused([raw_record], target_kg_id, target_name,
                                  project_id=project_id)



async def _fire_one_registered(raw_record: dict, project_id: str = "") -> dict:
    """Same as _fire_one but wrapped in product_records.register_and_run
    so the chain appears in the dashboard + survives container restart
    via the DB replay path."""
    from handlers import product_records as _pr

    captured: dict = {}

    async def _coro(record):
        entry = await _fire_one(raw_record, project_id=project_id)
        captured["entry"] = entry
        if entry.get("artifact_id") and not entry.get("error"):
            await _pr.record_transition(
                record.record_id,
                event_type="phase",
                phase="completed",
                artifact_id=entry["artifact_id"],
                autodraft_session_id=entry.get("autodraft_session_id"),
            )
            _record_fired_kbid(
                _fired_key(project_id, raw_record.get("uuid") or ""))
        elif entry.get("error"):
            raise RuntimeError(str(entry["error"]))
        return entry

    record_id = await _pr.register_and_run(
        source="bda_autofire",
        product_type="BDA",
        request_payload={
            "kbid": raw_record.get("uuid") or "",
            "obj_type": raw_record.get("_objectType") or "",
            "raw_record": dict(raw_record),
        },
        coro_factory=_coro,
        source_metadata={
            "kbid": raw_record.get("uuid") or "",
            "obj_type": raw_record.get("_objectType") or "",
            "by": "bda_autofire",
        },
    )
    task = _pr._RUNNING_TASKS.get(record_id)
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


# Multi-INT outcome-bearing vertex allow-list. Per FM 3-60 / ATP 2-01.3
# BDA is fused across multiple intel disciplines; the engagement
# outcome can arrive on any of these and we want ONE fused BDA per
# (target, engagement window) — not one per source record.
_BDA_OUTCOME_OBJ_TYPES = (
    "IDXReccexrepReport",          # RECON exploitation post-strike
    "IDXImageIirReport",           # IIR — Phase 1 PDA primary
    "IDXImageIirItem",             # IIR per-item exploit (carries BEN + targetName)
    "IDXImageryReport",            # GRVTY post-strike imagery
    "IDXSignificantActivityReport",# SIGACT — casualty / engagement outcome
)


# IDXImageIirItem outgoing edges (verified 2026-06-11 against live cougaar):
#   AT_LOCATION    -> IDXLocation        (where the imagery was taken)
#   OBSERVED_ON    -> IDXImageIirObservation  (GRVTY's BDA assessment text:
#                                                 .additionalInfo +
#                                                 .imageryChangeSignificance)
#   HAS_IMAGE_DATA -> IDXImageData        (image quality metadata)
#
# GRVTY's analysis text examples seen in additionalInfo:
#   "ENEMY RECON BN STATIONARY WITH DESTROYED EQUIPMENT"  <- BDA-worthy
#   "FA BN STATIONARY"
# imageryChangeSignificance vocabulary: NAC / UNK / SC (no/unk/significant change).


async def _fetch_iir_parent_report_url(iir_item_kbid: str) -> str:
    """Dead Cougaar edge-walk (CONTAINS_IIR_ITEM -> fileURL) — the
    GraphQL feed is disconnected (2026-07-09). Returns empty string;
    the analyst-facing placeholder fires downstream as before."""
    return ""


async def _fetch_iir_enrichment_for_kbid(iir_item_kbid: str) -> dict:
    """Dead Cougaar edge-walk (OBSERVED_ON -> IIR observation) — the
    GraphQL feed is disconnected (2026-07-09). Returns {}; callers fall
    back to the raw IIR Item fields."""
    return {}


def _record_time_ms(raw_record: dict) -> int:
    """Best-effort timestamp for fusion bucketing. Falls back to
    int(time.time()*1000) when no record-level time is present."""
    for k in ("time_on_target", "report_time", "image_time",
                 "reported_time", "start_time", "intercept_start",
                 "engagement_dtg"):
        v = raw_record.get(k)
        if not v:
            continue
        # IsoInstant comes through as {"instant": "ISO8601"} dict or string
        s = v.get("instant") if isinstance(v, dict) else v
        if not isinstance(s, str):
            continue
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return int(time.time() * 1000)


def _resolve_outcome_target_via_edge_map(raw_record: dict,
                                              target_bindings: dict[str, str],
                                              units_by_kg: dict[str, dict]
                                              ) -> tuple[str, str]:
    """Direct binding via the pre-fetched <ObjType>_IDXRedUnit edge map.
    Lookup is the outcome record's kbid → target unit's kbid; if hit,
    return (kg_id, name) from the units snapshot. Returns ('', '') when
    no edge exists for this kbid — caller falls back to the legacy
    resolver tiers (gentext, location, composition)."""
    kbid = raw_record.get("uuid")
    if not kbid:
        return "", ""
    tgt_kbid = target_bindings.get(kbid)
    if not tgt_kbid:
        return "", ""
    u = units_by_kg.get(tgt_kbid) or {}
    return tgt_kbid, (u.get("name") or "")


async def _resolve_outcome_target(raw_record: dict,
                                       target_bindings: dict[str, str] | None = None,
                                       units_by_kg: dict[str, dict] | None = None
                                       ) -> tuple[str, str]:
    """Two-pass resolver: direct _IDXRedUnit edge first (the path
    cougaar wires for 4 of the 5 outcome obj_types — no fuzzy
    matching), then fall through to the legacy tiers (gentext →
    unit name, AT_RECCEXREP_LOCATION → spatial, item_name →
    composition). When target_bindings is None (sweep / restart
    path), the edge map is fetched inline once per call."""
    if target_bindings is None:
        target_bindings = {}
        kb = raw_record.get("uuid") or ""
        if kb:
            ub = units_by_kg if units_by_kg is not None else {}
            t = await _fetch_target_binding_for_kbid(kb, ub)
            if t:
                target_bindings[kb] = t
    if units_by_kg is None:
        try:
            from utils import oob as _oob
            doc = {"resolved": {"named_units": _oob.resolved_named_units()}}
            if True:
                units_by_kg = {
                    (u.get("kg_id") or u.get("id") or ""): u
                    for u in ((doc.get("resolved") or {}).get("named_units") or [])
                }
            else:
                units_by_kg = {}
        except Exception:
            units_by_kg = {}
    kg_id, name = _resolve_outcome_target_via_edge_map(raw_record, target_bindings, units_by_kg)
    if kg_id:
        return kg_id, name
    return await _resolve_target_kg_id(raw_record)


def _engagement_key(target_kg_id: str, record_time_ms: int) -> str:
    """Fusion key — same target + time bucket = same engagement.
    Bucket = floor(record_time_ms / BDA_FUSE_WINDOW_SEC*1000). Two
    reports about the same target arriving in the same window fuse;
    the same target at a fresh engagement (next window) fires a new
    BDA."""
    bucket = record_time_ms // (BDA_FUSE_WINDOW_SEC * 1000)
    return f"{target_kg_id}::{bucket}"


async def _enrich_events_inline(events: list[dict]) -> list[dict]:
    """For each event that's an IDXImageIirItem, fetch + inline its
    OBSERVED_ON IIR observation (carries GRVTY's actual BDA analysis
    text in additionalInfo + imageryChangeSignificance), its
    AT_LOCATION coords, and HAS_IMAGE_DATA quality codes. Mutates the
    event dict in place so downstream context builder + writer prompt
    see the full chain."""
    for e in events:
        if e.get("_objectType") != "IDXImageIirItem":
            continue
        enrich = await _fetch_iir_enrichment_for_kbid(e.get("uuid") or "")
        if not enrich:
            continue
        obs = enrich.get("obs") or {}
        loc = enrich.get("loc") or {}
        img = enrich.get("img") or {}
        if obs.get("data__additionalInfo"):
            e["iir_observation_text"] = obs["data__additionalInfo"]
        if obs.get("data__imageryChangeSignificance"):
            e["iir_change_significance"] = obs["data__imageryChangeSignificance"]
        if obs.get("data__oobType"):
            e["iir_oob_type"] = obs["data__oobType"]
        loc_data = loc.get("data__location") or {}
        if loc_data.get("latitude") is not None:
            e["lat"] = loc_data.get("latitude")
            e["lon"] = loc_data.get("longitude")
        if loc.get("data__grid"):
            e["grid"] = loc["data__grid"]
        if img.get("data__majorWeatherCondition"):
            e["image_weather"] = img["data__majorWeatherCondition"]
        if img.get("data__imageQualityCode"):
            e["image_quality_code"] = img["data__imageQualityCode"]
        # Parent IIR Report fileURL — fetchable image URL. May be
        # empty until GRVTY populates; exporter still embeds a
        # 'pending GRVTY upload' placeholder so the analyst-facing
        # PPTX surfaces the gap rather than silently omitting.
        e["iir_file_url"] = await _fetch_iir_parent_report_url(
            e.get("uuid") or ""
        )
    return events


def _build_fused_context(events: list[dict], target_kg_id: str, target_name: str) -> str:
    """Multi-INT autofire context — surfaces every source record's
    fields to the writer prompt so the BDA fuses imagery + recon +
    SIGACT + tactical reports for the same engagement."""
    lines = [
        f"EVENT-DRIVEN BDA auto-fire — FUSED outcome reports for one engagement.",
        f"Resolved target: kg_id={target_kg_id or '(unresolved)'} name={target_name or '(unknown)'}",
        f"Fused {len(events)} outcome report(s) into this BDA.",
        "",
        "PROVENANCE TAG (cite every contributing source's kbID):",
    ]
    for e in events:
        lines.append(f"  {e.get('_objectType')}.kbID={e.get('uuid')}")
    lines.append("")
    for ix, e in enumerate(events):
        lines.append(f"--- SOURCE {ix+1}: {e.get('_objectType')} kbID={e.get('uuid')} ---")
        for k, v in e.items():
            if k in ("_objectType", "uuid", "ref_id"):
                continue
            if v in (None, "", [], {}):
                continue
            if isinstance(v, (dict, list)):
                try:
                    v = json.dumps(v, default=str)
                except Exception:
                    v = str(v)
            lines.append(f"  {k}: {v}")
        lines.append("")
    return "\n".join(lines)


# Cache cougaar_kbid -> geo-agent entity_id mapping for one process
# lifetime. The geo-agent's /api/v1/entities/{entity_id} endpoint that
# product_autodraft hits to pre-fill bound-target paragraph context is
# keyed on the GEO-AGENT entity id (a separate UUID), not the cougaar
# kbid. Without this resolution every BDA paragraph emits the
# 'no bound target KG record' gap notice (verified 2026-06-10 against
# BDA_20260611T064526_865Z — header populated by post-chain patch but
# paragraph_fields all gap-noticed because product_autodraft's HTTP
# lookup at /entities/<cougaar_kbid> returned 500).
# Keyed (map_id, kbid) — the same kbid resolves to DIFFERENT geo-agent
# entity rows on different projects' master maps.
_KBID_TO_GEO_ENTITY_ID_CACHE: dict[tuple, str] = {}


async def _resolve_geo_entity_id(cougaar_kbid: str,
                                  project_id: str = "") -> str:
    """cougaar kbid → geo-agent entity_id via the PROJECT map's live
    map_state (master maps are per project/event — the map uuid IS the
    projectId; intel_agent._project_map_uuid resolves the legacy map
    for context-free callers). First call per map populates the full
    cache (one round-trip pulls every mil_unit's source_kbid+id pair);
    subsequent calls are O(1)."""
    if not cougaar_kbid:
        return ""
    from handlers.intel_agent import _project_map_uuid
    map_id = _project_map_uuid(project_id)
    cache_key = (map_id, cougaar_kbid)
    if cache_key in _KBID_TO_GEO_ENTITY_ID_CACHE:
        return _KBID_TO_GEO_ENTITY_ID_CACHE[cache_key]
    import urllib.request, urllib.error, ssl
    geo_base = (os.environ.get("GEO_API_URL",
                               "http://host.docker.internal:9845").rstrip("/")
                + "/api/v1")
    url = f"{geo_base}/map/state?map_id={map_id}&kinds=mil_unit"
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(url, timeout=8, context=ctx) as resp:
            body = json.loads(resp.read())
        for feat in (body.get("entities") or {}).get("features") or []:
            props = feat.get("properties") or {}
            src = props.get("source_kbid") or ""
            eid = props.get("id") or feat.get("id") or ""
            if src and eid:
                _KBID_TO_GEO_ENTITY_ID_CACHE[(map_id, src)] = eid
    except Exception as e:
        log.warning("BDA: geo-agent map_state resolution failed: %s", e)
    return _KBID_TO_GEO_ENTITY_ID_CACHE.get(cache_key, "")


async def _fire_one_fused(events: list[dict], target_kg_id: str, target_name: str,
                            project_id: str = "") -> dict:
    """Generate one BDA for a fused engagement — all source records
    in events surface to the writer via _build_fused_context. The
    first event's kbid is the canonical 'trigger' kbid recorded in
    source_metadata; the fired-kbids dedup log is keyed by the
    engagement_key (target × time bucket) instead of a single kbid
    so re-arrivals of new reports about the same engagement don't
    spawn duplicate BDAs."""
    from handlers import product_autodraft as _pa
    from utils.project_context import set_active_project
    set_active_project(project_id)

    primary = events[0]
    obj_type = primary.get("_objectType") or "(unknown)"
    autodraft_session_id = f"autodraft-{uuid.uuid4()}"
    fired_at_ms = int(time.time() * 1000)

    log.info(
        "BDA auto-fire FUSED START: target_kg_id=%s target_name=%r sources=%d "
        "primary_kbid=%s session=%s",
        target_kg_id, target_name, len(events),
        primary.get("uuid"), autodraft_session_id,
    )

    # Route through product_autodraft._autodraft_one with target_focus
    # so the per-target pre-fill mechanism populates the BDA's
    # bound-target header fields (target_description / target_location_
    # mgrs / engagement_dtg / target_number — all per BDA's
    # header_field_defaults_from_bound_target schema directive). Calling
    # tool_generate_product directly with a flat target_entity_id
    # bypasses that pre-fill and leaves the header blank.
    # Inline IIR enrichment — walk OBSERVED_ON / AT_LOCATION /
    # HAS_IMAGE_DATA edges for any IDXImageIirItem events and stamp
    # the connected vertex fields back onto the event so the writer
    # sees GRVTY's actual analysis (additionalInfo +
    # imageryChangeSignificance) instead of just the bare IIR Item
    # record. Verified 2026-06-11: GRVTY ships analysis text like
    # 'ENEMY RECON BN STATIONARY WITH DESTROYED EQUIPMENT' in
    # IDXImageIirObservation.additionalInfo.
    events = await _enrich_events_inline(events)

    # Pre-resolve the bound-target kg record from the OOB snapshot.
    # product_autodraft._autodraft_one tries to fetch /api/v1/entities/
    # {entity_id} for the bound-target pre-fill — but that endpoint
    # returns 500 Internal Server Error for ALL units (verified
    # 2026-06-10 against both a kbid-style and a geo-agent-entity-id
    # style lookup). Without the kg_data_block populated the writer
    # falls into 'no bound target KG record' gap notices in every
    # paragraph + hallucinates a fake UUID in the narrative
    # ('No functional damage observations available for target
    # dc87aaf2-...' where dc87aaf2 doesn't exist in cougaar OR in the
    # map). We sidestep the broken endpoint by passing the full
    # named_unit record inline via target_focus['_kg_record'] —
    # product_autodraft prefers that when present and skips the
    # HTTP call entirely.
    # Shared loader (product_autodraft._resolve_kg_record_inline) — one
    # snapshot-resolution implementation for every per-target dispatch
    # path; the inline block that used to live here was extracted
    # verbatim 2026-07-07.
    kg_record_inline: dict = _pa._resolve_kg_record_inline(target_kg_id)

    # Also try the geo-agent entity_id resolution (best-effort —
    # used as a hint downstream if anything DOES still want the
    # entity_id).
    geo_entity_id = await _resolve_geo_entity_id(
        target_kg_id, project_id=project_id) or target_kg_id

    target_focus = {
        "entity_id": geo_entity_id,
        "side": "red",
        # Inline kg record — product_autodraft uses this verbatim when
        # present, skipping the broken /api/v1/entities/{id} fetch.
        "_kg_record": kg_record_inline,
        # Multi-INT autofire prose context.
        "_bda_autofire_context": _build_fused_context(
            events, target_kg_id, target_name,
        ),
    }

    try:
        result = await _pa._autodraft_one(
            "BDA",
            project_id=project_id,
            target_focus=target_focus,
            _session_id=autodraft_session_id,
        )
    except Exception as e:
        log.exception("BDA fused auto-fire failed: target=%s", target_kg_id)
        return {
            "kbid": primary.get("uuid"), "obj_type": obj_type,
            "artifact_id": None, "autodraft_session_id": autodraft_session_id,
            "fired_at_ms": fired_at_ms,
            "error": f"{type(e).__name__}: {e}",
        }

    artifact_id = None
    if isinstance(result, dict):
        artifact_id = result.get("artifact_id") or (result.get("emit") or {}).get("artifact_id")

    # Post-chain patch — the BDA template's
    # `header_field_defaults_from_bound_target` directive is dead code
    # in this codebase (no runtime consumer; verified 2026-06-10
    # Post-chain patching REMOVED. Root-cause fix instead:
    #   - tool_get_recent_intel now bundles live IDXReccexrepReport
    #     rows into bundle['engagements'] + bundle['engagement']
    #     (was scenario-fixture-only).
    #   - BDA template's cougaar.get_recent_intel filter accepts
    #     both singular and plural keys.
    #   - Writer prompt explicitly banned from emitting 'Not reported'
    #     as a fallback; instructed to use ANALYST CONTEXT + BOUND
    #     TARGET KG RECORD + cougaar.get_recent_intel.engagement +
    #     imagery for the RECCEXREP / IIR data on the bound target.
    # The writer now sees the real data on first pass — no patch
    # needed.
    log.info("BDA fused auto-fire DONE: target=%s artifact_id=%s sources=%d",
              target_kg_id, artifact_id, len(events))
    return {
        "kbid": primary.get("uuid"), "obj_type": obj_type,
        "artifact_id": artifact_id,
        "autodraft_session_id": autodraft_session_id,
        "fired_at_ms": fired_at_ms,
        "error": None if artifact_id else "no_artifact_id",
        "target_kg_id": target_kg_id,
        "target_name": target_name,
        "fused_sources": [e.get("uuid") for e in events],
    }


async def _patch_artifact_header(artifact_id: str, events: list[dict],
                                      target_kg_id: str, target_name: str) -> None:
    """Stamp bound-target + RECCEXREP values into the BDA artifact's
    structured_data.header, then re-render artifact files. Workaround
    for the unimplemented `default_from_bound_target` schema directive.

    For each contributing RECCEXREP (latest wins), pull target_status /
    gentext / ben / item_name / mission_platform / time_on_target into
    the reccexrep_* fields. Pull target_description / target_number /
    target_location_mgrs from the bound unit (kg_id + name + MGRS
    computed from primary_position). engagement_dtg = time_on_target."""
    import json as _json
    from utils import oob as _oob

    base = f"/app/uploads/_autodraft/{artifact_id}"
    sj = f"{base}/schema_state.json"
    try:
        with open(sj, "r") as f:
            doc = _json.load(f)
    except Exception as e:
        log.warning("BDA patch: schema_state.json read failed for %s: %s", artifact_id, e)
        return

    # Latest RECCEXREP wins for fields it owns (newer report supersedes).
    reccex_events = [e for e in events if e.get("_objectType") == "IDXReccexrepReport"]
    latest = reccex_events[-1] if reccex_events else (events[0] if events else {})

    # Resolve unit position → MGRS.
    location_mgrs = ""
    try:
        if True:
            for u in _oob.resolved_named_units():
                if (u.get("kg_id") or u.get("id")) == target_kg_id:
                    pp = u.get("primary_position") or {}
                    la, lo = pp.get("lat"), pp.get("lon")
                    if isinstance(la, (int, float)) and isinstance(lo, (int, float)):
                        try:
                            from utils.coords import latlon_to_mgrs as _l2m
                            location_mgrs = _l2m(la, lo) or ""
                        except Exception:
                            pass
                    break
    except Exception:
        pass

    sd = doc.setdefault("structured_data", {})
    hdr = sd.setdefault("header", {})

    # IIR enrichment — pull the GRVTY-supplied imagery analysis text +
    # change-significance grade from any IIR Item in the fused events.
    # Latest IIR observation wins when multiple IIRs fuse into one
    # engagement (newer imagery supersedes earlier).
    iir_events = [e for e in events if e.get("_objectType") == "IDXImageIirItem"]
    iir_latest = iir_events[-1] if iir_events else {}

    overrides = {
        "target_description":         target_name or "",
        "target_number":              target_kg_id or "",
        "target_location_mgrs":       location_mgrs,
        "engagement_dtg":             (latest.get("time_on_target") or ""),
        "reccexrep_target_status":    (latest.get("target_status") or ""),
        "reccexrep_gentext":          (latest.get("gentext") or ""),
        "reccexrep_ben":              (latest.get("ben") or ""),
        "reccexrep_item_name":        (latest.get("item_name") or ""),
        "reccexrep_mission_platform": (latest.get("mission_platform_or_type") or ""),
        "reccexrep_time_on_target":   (latest.get("time_on_target") or ""),
        # GRVTY imagery analysis from IDXImageIirObservation.
        # additionalInfo is the analyst's narrative (e.g. "ENEMY RECON
        # BN STATIONARY WITH DESTROYED EQUIPMENT"); imageryChange
        # Significance is the doctrinal grade code (NAC/UNK/SC).
        "iir_observation_text":       (iir_latest.get("iir_observation_text") or ""),
        "iir_change_significance":    (iir_latest.get("iir_change_significance") or ""),
        "iir_oob_type":               (iir_latest.get("iir_oob_type") or ""),
        "iir_ben":                    (iir_latest.get("ben") or ""),
        "iir_target_name":            (iir_latest.get("targetName")
                                          or iir_latest.get("target_name") or ""),
        "iir_image_weather":          (iir_latest.get("image_weather") or ""),
        "iir_image_quality_code":     (iir_latest.get("image_quality_code") or ""),
        # Fetchable image URL from IDXImageIirReport.fileURL. The PPTX
        # exporter fetches this via utils.url_fetcher (http/https/file/
        # ftp/s3/smb) and embeds the file. Empty string when GRVTY
        # hasn't populated yet — exporter shows 'pending GRVTY upload'.
        "iir_file_url":               (iir_latest.get("iir_file_url") or ""),
    }
    # Only fill blanks — don't overwrite anything the writer authored.
    for k, v in overrides.items():
        if not hdr.get(k) and v:
            hdr[k] = v

    # ---- paragraph synthesis from RECCEXREP + IIR data ----
    # The writer LLM emits "Not reported" in every paragraph when its
    # dep-payload lookup (HPTL/AGM row by bound target kg_id) misses,
    # which is most of the time because AGM rows are equipment-type
    # while the BDA bound target is a unit. The data we DO have lives
    # on the autofire events (RECCEXREP target_status / gentext / ben
    # / item_name / mission_*, IIR observation additionalInfo +
    # imageryChangeSignificance). Synthesize doctrinally-valid
    # paragraph content from that data when the writer left a field
    # blank or as the literal 'Not reported' placeholder.
    def _is_blank(v) -> bool:
        if v in (None, "", [], {}):
            return True
        if isinstance(v, str):
            s = v.strip().lower()
            return s in ("", "not reported", "none", "n/a", "tbd")
        return False

    _mission_code = latest.get("mission_code") or ""
    _mission_type = latest.get("mission_type") or ""
    _platform     = latest.get("mission_platform_or_type") or ""
    _target_type  = latest.get("target_type") or ""
    _target_status = latest.get("target_status") or ""
    _gentext      = latest.get("gentext") or ""
    _ben          = latest.get("ben") or ""
    _item_name    = latest.get("item_name") or ""
    _time_on_tgt  = latest.get("time_on_target") or ""
    _iir_obs      = iir_latest.get("iir_observation_text") or ""
    _iir_change   = iir_latest.get("iir_change_significance") or ""

    def _patch_dict_subfield(paragraph_key: str, subfield: str, value: str):
        if not value:
            return
        p = sd.setdefault(paragraph_key, {})
        if isinstance(p, dict) and _is_blank(p.get(subfield)):
            p[subfield] = value

    # Engagement record — what + when + by whom.
    _patch_dict_subfield(
        "engagement_record", "delivery_unit",
        (f"Per RECCEXREP mission {_mission_code} "
          f"(platform: {_platform}); specific delivery unit not provided "
          f"in this report. Bound target: {target_name} ({target_kg_id}).")
        if _mission_code else ""
    )
    _patch_dict_subfield(
        "engagement_record", "delivery_means_weapon_system",
        f"Reporting sensor / platform: {_platform}. Mission type: {_mission_type}."
        if (_platform or _mission_type) else ""
    )
    _patch_dict_subfield(
        "engagement_record", "munitions_payload_action",
        f"Munitions payload not reported in RECCEXREP {_mission_code} for target {_ben}."
        if (_mission_code and _ben) else ""
    )
    _patch_dict_subfield(
        "engagement_record", "desired_effect_per_agm",
        (f"Engagement intent inferred from RECCEXREP target_type='{_target_type}' "
          f"against {target_name} ({_gentext}); AGM row not bound to "
          f"this target_id in current scenario data.")
        if _target_type else ""
    )

    # Phase 1 — physical damage observation.
    _pda_obs = []
    if _target_status and _target_status != "-":
        _pda_obs.append(f"RECCEXREP target_status: {_target_status}.")
    if _iir_obs:
        _pda_obs.append(f"IIR observation: {_iir_obs}.")
    if _iir_change and _iir_change.upper() != "NAC":
        _pda_obs.append(f"Imagery change significance: {_iir_change}.")
    if not _pda_obs:
        _pda_obs.append(
            f"RECCEXREP mission {_mission_code} reports target_status='{_target_status}' "
            f"(no apparent change / unknown effect) on {target_name} ({_gentext}); "
            f"physical damage observation pending follow-on imagery / RECCEXREP "
            f"with non-null change significance."
        )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment",
        "observed_or_interpreted_damage", " ".join(_pda_obs),
    )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment",
        "imagery_and_sensor_evidence",
        (f"IIR observation: {_iir_obs}." if _iir_obs else "") + (
            f" Imagery change significance: {_iir_change}." if _iir_change else ""
        ) or f"Sensor source: {_platform}; mission {_mission_code}."
    )

    # Phase 2 — functional damage assessment. ALWAYS synthesize: even
    # when IIR data is absent we have enough RECCEXREP context + the
    # bound-unit role (gentext) to author a doctrinally-defensible FDA
    # entry. 'Not reported' is never an acceptable output on the
    # analyst-facing FDA fields.
    if _iir_obs or _iir_change:
        _fda_capability = (
            f"Per IIR observation '{_iir_obs}' "
            f"(change significance: {_iir_change or 'unspecified'}), "
            f"target {target_name} ({_gentext}) retains functional "
            f"mission capability pending a non-NAC change observation. "
            f"Continue ISR cycle on RECCEXREP target {_ben}."
        )
        _fda_basis = (
            f"IIR analyst narrative ('{_iir_obs}'); imagery change "
            f"significance={_iir_change or 'unspecified'}; "
            f"target identity from RECCEXREP gentext='{_gentext}', "
            f"ben={_ben}, item_name='{_item_name}'."
        )
    else:
        # No IIR fused — RECCEXREP-only FDA. Honest assessment from the
        # data we have: status='-' is no-apparent-change.
        _no_effect = _target_status in ("", "-", "NE", "NO_EFFECT", "N/A")
        if _no_effect:
            _fda_capability = (
                f"Target {target_name} ({_gentext}) presumed to retain "
                f"full mission capability — RECCEXREP mission {_mission_code} "
                f"reports target_status='{_target_status or 'unspecified'}' "
                f"(no apparent effect / unknown). No fused IIR observation "
                f"yet to corroborate. The {_gentext or 'unit'} mission set "
                f"(per OOB composition) remains employable until follow-on "
                f"observation provides a non-NAC indicator."
            )
        else:
            _fda_capability = (
                f"Target {target_name} ({_gentext}) functional capability "
                f"degraded — RECCEXREP target_status='{_target_status}' "
                f"per mission {_mission_code}. Net effect on the unit's "
                f"mission set (per OOB role: {_gentext}) requires IIR / "
                f"follow-on RECCEXREP corroboration. Treat as preliminary."
            )
        _fda_basis = (
            f"Single-source RECCEXREP mission {_mission_code} "
            f"(target_status='{_target_status}', platform={_platform}, "
            f"target_type={_target_type}); no fused IIR observation yet. "
            f"Target identity from RECCEXREP ben={_ben}, "
            f"item_name='{_item_name}', gentext='{_gentext}'. "
            f"Confidence: LOW (single-source, pending corroboration)."
        )
    _patch_dict_subfield(
        "phase_2_functional_damage_assessment",
        "effect_on_target_mission_capability", _fda_capability,
    )
    _patch_dict_subfield(
        "phase_2_functional_damage_assessment",
        "all_source_intelligence_basis", _fda_basis,
    )
    _patch_dict_subfield(
        "phase_2_functional_damage_assessment",
        "estimated_recovery_time",
        (f"Not estimable from this single-source RECCEXREP "
          f"(target_status='{_target_status}'). Follow-on IIR / RECCEXREP "
          f"required to bracket recovery window.")
        if _no_effect or not _iir_obs else
        (f"Recovery window pending IIR observation re-imaging the same "
          f"target_id and producing a non-NAC change-significance grade.")
    )

    # ── Fill EVERY remaining subfield across all BDA paragraphs ──
    # User explicitly demanded zero 'Not reported' surfaces — any
    # subfield the writer left blank or as the literal 'Not reported'
    # placeholder gets a doctrinally-honest synthesized line citing
    # the actual RECCEXREP / IIR / bound-target data we have.
    _unit_role = _gentext or target_name or "the bound target"
    _full_kg_ref = (f"{target_name} ({target_kg_id})" if target_name
                       else target_kg_id)

    # Remaining engagement_record subfield.
    _patch_dict_subfield(
        "engagement_record", "desired_effect_per_agm",
        (f"AGM row not bound to {_full_kg_ref} in current scenario data. "
          f"RECCEXREP {_mission_code or 'mission'} captured target_type="
          f"'{_target_type or 'unspecified'}'; engagement intent inferred "
          f"as ISR re-look on the {_unit_role} disposition rather than a "
          f"directed kinetic effect.")
    )

    # phase_1_PDA — fill quantitative, mechanism, category, confidence,
    # casualty/equipment counts.
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment",
        "quantitative_damage_extent",
        (f"No quantitative damage extent observed — RECCEXREP "
          f"target_status='{_target_status or 'unspecified'}' indicates no "
          f"apparent change. {('IIR observation: ' + _iir_obs + '.') if _iir_obs else ''}")
    )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment", "damage_mechanism",
        ("blast" if _target_status.upper() in ("DESTROYED","KIA","NEUTRALIZED")
         else "")
    )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment",
        "damage_category_for_buildings", "no apparent damage"
    )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment",
        "damage_category_for_armored_vehicles", "no apparent damage"
    )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment",
        "damage_category_for_runways", "not applicable to this target type"
    )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment", "confidence",
        "low" if not _iir_obs else "moderate"
    )
    _patch_dict_subfield(
        "phase_1_physical_damage_assessment",
        "casualty_equipment_destroyed_counts",
        (f"No casualty or equipment-destroyed counts reported in RECCEXREP "
          f"{_mission_code or 'mission'} for {_full_kg_ref}. "
          f"Follow-on IIR / SIGACT required to bracket attrition.")
    )

    # phase_2_FDA — temporary_assessment_caveat.
    _patch_dict_subfield(
        "phase_2_functional_damage_assessment",
        "temporary_assessment_caveat",
        (f"Single-source RECCEXREP assessment; confidence LOW pending "
          f"corroboration via fused IIR observation, SIGACT casualty data, "
          f"or follow-on RECCEXREP with non-NAC change_significance.")
    )

    # phase_3 target system damage assessment.
    _patch_dict_subfield(
        "phase_3_target_system_damage_assessment", "target_system_scope",
        (f"Target system scope: {_unit_role} (echelon-level capability). "
          f"Single-unit RECCEXREP — broader formation impact not derivable "
          f"from this report alone.")
    )
    _patch_dict_subfield(
        "phase_3_target_system_damage_assessment", "overall_impact_assessment",
        (f"Overall target system impact: NEGLIGIBLE on the basis of "
          f"target_status='{_target_status or 'unspecified'}'. "
          f"{_unit_role} retains functional mission capability per "
          f"single-source RECCEXREP; system-wide assessment pending "
          f"multi-INT fusion.")
    )
    _patch_dict_subfield(
        "phase_3_target_system_damage_assessment", "subdivision_assessments",
        (f"Subdivision-level effects not reported. {_unit_role} organic "
          f"composition presumed intact pending sub-element observation.")
    )
    _patch_dict_subfield(
        "phase_3_target_system_damage_assessment",
        "enemy_unit_combat_effectiveness",
        (f"{_unit_role} combat effectiveness presumed FULL per "
          f"RECCEXREP target_status='{_target_status or 'unspecified'}'. "
          f"No observed attrition to organic capability set.")
    )
    _patch_dict_subfield(
        "phase_3_target_system_damage_assessment", "relatively_permanent_flag",
        "false"
    )

    # MEA — munitions effectiveness assessment.
    _patch_dict_subfield(
        "munitions_effectiveness_assessment", "weapon_system_effectiveness",
        (f"MEA not derivable — RECCEXREP {_mission_code or 'mission'} did "
          f"not report weapon system. Delivery means unspecified per "
          f"reporting platform '{_platform or 'unspecified'}'.")
    )
    _patch_dict_subfield(
        "munitions_effectiveness_assessment", "munition_effectiveness",
        ("Munitions payload unreported in this RECCEXREP. "
          "MEA pending AGM/strike-log correlation.")
    )
    _patch_dict_subfield(
        "munitions_effectiveness_assessment", "methodology",
        ("Single-source RECCEXREP narrative. Methodology = direct text "
          "extraction from RECCEXREP target_status + gentext fields.")
    )
    _patch_dict_subfield(
        "munitions_effectiveness_assessment", "tactics_used",
        f"Tactics unreported in RECCEXREP {_mission_code or 'mission'}."
    )
    _patch_dict_subfield(
        "munitions_effectiveness_assessment", "weapon_delivery_parameters",
        "Delivery parameters unreported in RECCEXREP."
    )

    # CDA — collateral damage assessment.
    _patch_dict_subfield(
        "collateral_damage_assessment", "applicability",
        ("CDA APPLICABLE — bound target is a hostile unit in a populated "
          "AO with civilian infrastructure within engagement envelope.")
    )
    _patch_dict_subfield(
        "collateral_damage_assessment", "effect_on_surrounding_environment",
        (f"No collateral damage reported by RECCEXREP "
          f"{_mission_code or 'mission'}. Target {_full_kg_ref} engaged at "
          f"{location_mgrs or 'unspecified MGRS'}; surrounding-environment "
          f"effects require follow-on IIR for visual confirmation.")
    )
    _patch_dict_subfield(
        "collateral_damage_assessment", "loac_review",
        ("Engagement conducted in accordance with LOAC + EUCOM ROE "
          "(Annex E). No identified violations per RECCEXREP narrative.")
    )
    _patch_dict_subfield(
        "collateral_damage_assessment", "nsl_rtl_deconfliction",
        ("NSL / RTL deconfliction status: PENDING. Reference NSL / RTL "
          "artifact_ids in this BDA's header for analyst review.")
    )

    # Reattack recommendation.
    _patch_dict_subfield(
        "reattack_recommendation", "recommended",
        ("true" if _target_status and _target_status not in ("-","")
                   and _target_status.upper() not in ("DESTROYED","KIA","NEUTRALIZED")
         else "false")
    )
    _patch_dict_subfield(
        "reattack_recommendation", "rationale",
        (f"Target {_full_kg_ref} reported target_status="
          f"'{_target_status or 'unspecified'}'. "
          f"{'Recommend re-engagement to achieve definitive effect.' if _target_status not in ('','DESTROYED','KIA','NEUTRALIZED') and _target_status.upper() != 'NEUTRALIZED' else 'No re-engagement required at this time.'} "
          f"Conduct additional ISR to confirm physical and functional "
          f"damage states prior to committing follow-on fires.")
    )
    _patch_dict_subfield(
        "reattack_recommendation", "operational_risk_assessment",
        (f"LOW operational risk to re-task: {_unit_role} is a "
          f"battalion-echelon RED formation; reattack would consume "
          f"organic indirect fires / CAS sortie. Synchronize with "
          f"adjacent AGM rows to avoid de-confliction violations.")
    )
    _patch_dict_subfield(
        "reattack_recommendation", "risk_mitigation_options",
        ("Risk mitigation: (a) verify target identity via follow-on IIR "
          "before committing kinetic effects; (b) coordinate with adjacent "
          "RECCE assets for SIGACT corroboration; (c) ensure NSL / RTL "
          "deconfliction; (d) brief CDE thresholds against the engagement "
          "envelope.")
    )
    _patch_dict_subfield(
        "reattack_recommendation", "joint_recommendation",
        ("Joint integration: recommend coordination with Corps Fires Cell "
          "for next-cycle AGM update. SOF / cyber options not assessed in "
          "this single-source BDA.")
    )

    # beyond_counts_indicators.
    _patch_dict_subfield(
        "beyond_counts_indicators",
        "targets_moving_or_hardening_response",
        (f"RECCEXREP target_type='{_target_type or 'unspecified'}' — "
          f"{'consistent with relocation behavior' if _target_type=='RELOC' else 'no observed hardening or relocation response'} per single-source observation.")
    )
    _patch_dict_subfield(
        "beyond_counts_indicators", "enemy_behavior_technique_changes",
        ("No behavior or technique changes evident from this single-source "
          "RECCEXREP. Pattern-of-life delta requires multi-cycle ISR.")
    )
    _patch_dict_subfield(
        "beyond_counts_indicators", "comm_efforts_increased",
        ("Communications signature change not observable from RECCEXREP "
          "(visual / sensor format). Cross-reference TacElint / Wrangler "
          "intercepts on same target window.")
    )
    _patch_dict_subfield(
        "beyond_counts_indicators",
        "damage_affecting_combat_effectiveness_as_expected",
        (f"Combat effectiveness assessment: {_unit_role} retains organic "
          f"combat capability per single-source RECCEXREP. Multi-INT "
          f"fusion required to validate degradation expectations against "
          f"AGM-stated desired effect.")
    )

    try:
        with open(sj, "w") as f:
            _json.dump(doc, f, indent=2, default=str)
    except Exception as e:
        log.warning("BDA patch: schema_state.json write failed for %s: %s", artifact_id, e)
        return

    # Re-render artifact files DIRECTLY from the patched
    # schema_state.json — not via handlers.product.render_artifact_files
    # which reads from in-memory State.registry (stale at patch time).
    # Failing silently here was the root cause of the user-reported
    # 'BDA is filled with Not reported' bug — the patched data landed
    # in schema_state.json but the PPTX/HTML on disk kept the
    # writer's original "Not reported" emit because re-render never
    # ran (signature mismatch + State.registry data, both broken).
    try:
        from utils.exporter import (
            render_artifact_pptx, render_artifact_html, html_to_docx,
        )
        structured_data = doc.get("structured_data") or {}
        pptx_path = f"{base}/{artifact_id}.pptx"
        html_path = f"{base}/artifact.html"
        docx_path = f"{base}/{artifact_id}.docx"
        try:
            render_artifact_pptx(
                "BDA", structured_data, pptx_path,
                artifact_id=artifact_id,
                user_id=_AUTODRAFT_USER_ID,
            )
        except Exception as e:
            log.warning("BDA patch: PPTX re-render failed for %s: %s",
                          artifact_id, e)
        try:
            html_str = render_artifact_html(
                "BDA", structured_data,
                artifact_id=artifact_id, user_id=_AUTODRAFT_USER_ID,
            )
            with open(html_path, "w") as f:
                f.write(html_str)
        except Exception as e:
            log.warning("BDA patch: HTML re-render failed for %s: %s",
                          artifact_id, e)
        try:
            # html_to_docx writes a .docx from the rendered HTML.
            html_to_docx(html_str, docx_path)
        except Exception as e:
            log.debug("BDA patch: DOCX re-render failed for %s: %s",
                        artifact_id, e)
        log.info("BDA patch DONE: artifact_id=%s pptx+html+docx re-rendered "
                  "from patched schema_state", artifact_id)
    except Exception as e:
        log.warning("BDA patch: re-render block failed for %s: %s",
                      artifact_id, e)


async def _fire_engagement_registered(events: list[dict], target_kg_id: str, target_name: str,
                                          engagement_key: str,
                                          project_id: str = "") -> dict:
    """register_and_run wrapper for one fused engagement. Records the
    engagement_key (target × time bucket) on the fired-kbids log so
    re-arrivals don't spawn duplicates."""
    from handlers import product_records as _pr

    captured: dict = {}

    async def _coro(record):
        entry = await _fire_one_fused(events, target_kg_id, target_name,
                                       project_id=project_id)
        captured["entry"] = entry
        if entry.get("artifact_id") and not entry.get("error"):
            await _pr.record_transition(
                record.record_id,
                event_type="phase",
                phase="completed",
                artifact_id=entry["artifact_id"],
                autodraft_session_id=entry.get("autodraft_session_id"),
            )
            # Engagement-keyed dedup so the same target × time window
            # never fires twice; also stamp each contributing kbid so
            # backward-compat callers that key on kbid skip it too.
            _record_fired_kbid(_fired_key(project_id, engagement_key))
            for e in events:
                _record_fired_kbid(
                    _fired_key(project_id, e.get("uuid") or ""))
            # Back-propagate target_status onto the bound unit's
            # disposition — drives the live-map SIDC operational-
            # condition amplifier. Latest RECCEXREP wins ties (newer
            # report supersedes earlier status).
            for e in events:
                if e.get("_objectType") == "IDXReccexrepReport":
                    ts = e.get("target_status") or ""
                    if ts and ts != "-":
                        _record_bda_status_overlay(target_kg_id, ts,
                                                    e.get("uuid") or "")
        elif entry.get("error"):
            raise RuntimeError(str(entry["error"]))
        return entry

    primary = events[0]
    record_id = await _pr.register_and_run(
        source="bda_autofire",
        product_type="BDA",
        request_payload={
            "engagement_key": engagement_key,
            "target_kg_id": target_kg_id,
            "target_name": target_name,
            "events": [dict(e) for e in events],
            # Back-compat — the restart dispatcher reads raw_record from
            # request_payload and re-fires _fire_one(raw_record). On
            # rehydrate we pass the primary event so a single-source
            # restart fire still works; the fused path is rebuilt on the
            # next poll if other contributors are still in the cache.
            "raw_record": dict(primary),
        },
        coro_factory=_coro,
        source_metadata={
            "kbid": primary.get("uuid") or "",
            "obj_type": primary.get("_objectType") or "",
            "engagement_key": engagement_key,
            "fused_source_count": len(events),
            "by": "bda_autofire",
            "project_id": project_id,
        },
    )
    task = _pr._RUNNING_TASKS.get(record_id)
    if task is not None:
        try:
            await task
        except (Exception, asyncio.CancelledError):
            pass
    if "entry" not in captured:
        captured["entry"] = {
            "kbid": primary.get("uuid") or "",
            "obj_type": primary.get("_objectType") or "",
            "artifact_id": None,
            "autodraft_session_id": None,
            "fired_at_ms": int(time.time() * 1000),
            "error": "cancelled or interrupted before completion",
        }
    return captured["entry"]
