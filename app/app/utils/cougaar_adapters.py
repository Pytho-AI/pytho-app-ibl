"""
Cougaar GraphQL adapter — five assemblers feeding the IBL doctrine products.

Entry points:
    get_oob(force_type)            -> {"units": [...]}
    get_spatial_overlay()          -> {"control_measures": [...]}
    get_target_set(since_iso)      -> {"targets": [...]}
    get_target_by_id(target_uuid)  -> {"target": ...}
    get_recent_intel(since_iso)    -> {"recent_intel": [...]}

Products consuming these assemblers (per the 31-entry product registry in
templates/products/__init__.py — see scratch/ibl/product-audit-2026-05-13.md):
    get_oob('RED')           -> HVTL, CARVER, HPTL, SITEMP, Threat COA Statement, Threat
                                Model, Threat Overlay, INTSUM, GRINTSUM, TSA
    get_oob('BLUE')          -> WEM (column relevance), SITREP
    get_spatial_overlay()    -> MCOO, TEM, SITEMP, Event Template,
                                Decision Support Template, GRINTSUM
    get_target_set()         -> HPTL, CARVER, TSS, AGM, TSM, BDA, TMP
    get_target_by_id()       -> BDA, TMP, CA (stubs for now)
    get_recent_intel()       -> INTSUM, GRINTSUM, SITREP, CCA, MCOO, TEM

Data binding follows cougaar-schema-analysis.md v3.0. The IDX intel layer
(IDXTacticalReport, IDXEnemyOrderOfBattleReport, IDXImageIirReport / Item,
IDXTacElintReport, IDXSignalOfInterestReport, IDXHumintReport) is the live
source today; the TacLogSim baseline carriers (Target, SPOTReport, SitRep,
Observation, SensorReport, Image, Event) are schema-present but data-empty.

Endpoint: COUGAAR_BASE_URL points at the path up through `/rest/gql`; the
code appends `/graphql`. Authorization is the Pytho edge bearer token,
stripped by our Caddy edge before forwarding to Cougaar.

Within-INT dedup is done client-side via group-by-natural-key (BE number
for IMINT, signalId for SIGINT, unitKey for OOB). Output shape mirrors
Cougaar's IDXFusionObservation so swapping to Cougaar-side fusion is a
one-line dispatch change when their fusion layer populates.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import weakref
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from gql import Client, GraphQLRequest, gql
from gql.client import AsyncClientSession
from gql.transport.exceptions import (
    TransportClosed,
    TransportConnectionFailed,
    TransportQueryError,
)
from gql.transport.httpx import HTTPXAsyncTransport
from pydantic import AliasPath, BaseModel, BeforeValidator, ConfigDict, Field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outbound abstraction
# ---------------------------------------------------------------------------
# Source token stamped on entities + overlays this module pushes to the
# geo-agent master map and out to UI consumers. DELIBERATELY does not
# name the upstream system (cougaar) so consumers outside this adapter
# never bind to the implementation. When the upstream OOB feed changes
# (post-IBL cougaar replacement), the adapter is swapped but the outbound
# token stays — every consumer (sidebar toggle source token, map filter,
# product writers, sync transactional REPLACE keys) keeps working without
# code changes.
LIVE_OOB_SOURCE = "live_oob"


# ---------------------------------------------------------------------------
# Client + transport
# ---------------------------------------------------------------------------


class CougaarError(Exception):
    """Cougaar's GraphQL response contained errors[]. Hard failure."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(str(errors))


# Cougaar is an OPTIONAL integration. The presence/absence of
# COUGAAR_BASE_URL in the environment is the signal:
#   - unset / empty  → cougaar is not in use for this deployment; every
#                      query helper returns empty results without any
#                      network I/O. Product pipeline falls back to the
#                      preloaded scenario.py / KG fixtures.
#   - set to a URL   → cougaar is reachable; queries use it; if the
#                      endpoint is temporarily down the existing poll-
#                      loop retry + per-request reset path handles it.
#
# Same pattern for COUGAAR_PROXY_TOKEN: if set, sent as a Bearer
# header; if unset/empty, no auth header is sent (cougaar instances
# that don't require edge auth just work).
#
# No special airgap flag needed. The app must NOT hard-fail on a
# missing optional integration's env var.
_COUGAAR_BASE_URL = (os.environ.get("COUGAAR_BASE_URL") or "").strip()
_COUGAAR_PROXY_TOKEN = (os.environ.get("COUGAAR_PROXY_TOKEN") or "").strip()
if not _COUGAAR_BASE_URL:
    log.info(
        "COUGAAR_BASE_URL unset — Cougaar integration disabled; query "
        "helpers will return empty results. Set COUGAAR_BASE_URL in "
        "app/.env to a GraphQL endpoint to enable."
    )


def _build_client() -> Client | None:
    """Build the shared gql Client, or None if COUGAAR_BASE_URL is unset.

    Returns None when Cougaar isn't configured for this deployment;
    `_ensure_session` propagates that None through to all callers, which
    short-circuit to empty results.
    """
    if not _COUGAAR_BASE_URL:
        return None
    headers = {}
    if _COUGAAR_PROXY_TOKEN:
        headers["Authorization"] = f"Bearer {_COUGAAR_PROXY_TOKEN}"
    transport = HTTPXAsyncTransport(
        url=f"{_COUGAAR_BASE_URL}/graphql",
        headers=headers,
        timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
        verify=False,
    )
    # fetch_schema_from_transport=False: cougaar's live GraphQL server now
    # rejects schema introspection with BadFaithIntrospection — so the gql
    # client's auto-introspection at connect time fails with a JSONDecode
    # error on Tomcat's HTML 400 page and the whole session setup dies.
    # Queries don't need the schema to execute; they're sent verbatim to
    # the server. Client-side validation is lost (a query field typo now
    # errors at execute time instead of connect time), which is acceptable
    # because the queries here are static templates verified at boot via
    # the smoke tests in app/tests/cougaar_adapters_test.py.
    return Client(transport=transport, fetch_schema_from_transport=False)


# ---------------------------------------------------------------------------
# Shared client + session (per-event-loop singletons)
# ---------------------------------------------------------------------------
#
# Goal: hold ONE gql.Client and ONE long-lived AsyncClientSession across
# assembler calls so the schema introspection round trip (~1.1s) happens
# exactly once per (re)connect instead of once per assembler call.
# fetch_schema_from_transport=True only triggers when client.schema is None,
# so a reused Client.connect_async() amortizes the cost to a single hit.
#
# Why per-event-loop instead of strictly process-wide: handlers/intel_agent.py
# calls `asyncio.run(cougaar_adapters.X(...))` for each tool invocation, which
# spins a fresh event loop each time. httpx's AsyncClient and asyncio.Lock
# are bound to the loop that created them; reusing them across loops raises
# "got Future attached to a different loop". Keying by id(loop) gives the
# normal FastAPI/dag_executor path (one loop per request) full reuse, while
# the legacy asyncio.run() path simply builds its own short-lived singleton
# per loop (no slower than the old per-call behavior).
#
# Reconnect: only TransportConnectionFailed and TransportClosed are treated
# as transport-dead signals. TransportQueryError is a server-side query
# error — the connection is fine, the query was wrong; do NOT rebuild.

# Per-loop state. Keyed by id(loop); value is (Client, AsyncClientSession, Lock).
# The Lock is created lazily on the loop that owns it, so it never crosses
# loops. We hold a weak reference to the loop only for the cleanup callback
# that drops the dict entry when the loop closes.
_LOOP_STATE: dict[int, tuple[Client | None, AsyncClientSession | None, asyncio.Lock]] = {}


def _get_loop_slot() -> tuple[asyncio.AbstractEventLoop, asyncio.Lock]:
    """Return (loop, lock) for the current running loop, creating the slot
    if this is the first call from this loop. Registers a one-shot cleanup
    so _LOOP_STATE doesn't accumulate dead entries across asyncio.run() calls.
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    entry = _LOOP_STATE.get(key)
    if entry is None:
        lock = asyncio.Lock()
        _LOOP_STATE[key] = (None, None, lock)
        # asyncio.run() closes the loop right before returning. The weakref
        # finalizer fires when the loop is garbage-collected, dropping our
        # dict entry so _LOOP_STATE stays bounded across many asyncio.run()
        # calls (the legacy intel_agent.py path). The underlying httpx
        # client gets GC'd with the loop.
        weakref.finalize(loop, _LOOP_STATE.pop, key, None)
        return loop, lock
    return loop, entry[2]


async def _ensure_session() -> AsyncClientSession | None:
    """Return the shared session for the current event loop, building (and
    connecting) it if needed.

    Safe to call from many coroutines concurrently within one loop — the
    per-loop lock guarantees only one of them runs connect_async() + the
    one-time schema fetch; the rest wait and return the same session.

    Returns None when Cougaar is not configured for this deployment
    (COUGAAR_BASE_URL unset/empty). Every downstream `_execute*` helper
    checks for None session and returns empty results without network
    I/O, so callers degrade to scenario.py / KG fixtures instead of
    crashing.
    """
    loop, lock = _get_loop_slot()
    key = id(loop)
    entry = _LOOP_STATE.get(key)
    if entry is not None and entry[1] is not None:
        return entry[1]
    async with lock:
        entry = _LOOP_STATE.get(key)
        if entry is not None and entry[1] is not None:
            return entry[1]
        client = _build_client()
        if client is None:
            # COUGAAR_BASE_URL unset — Cougaar disabled. Record that we
            # tried (None client) so we don't repeatedly hit the build
            # path, and return None for callers to short-circuit.
            _LOOP_STATE[key] = (None, None, lock)
            return None
        # Client.__aenter__/connect_async() opens the httpx transport AND
        # fetches the schema (because client.schema is still None and
        # fetch_schema_from_transport=True). Both costs are paid once here.
        session = await client.connect_async()
        _LOOP_STATE[key] = (client, session, lock)
        return session


async def _reset_shared_session() -> None:
    """Tear down the current loop's client+session so the next
    _ensure_session() call rebuilds from scratch (which re-fetches the
    schema on the new connect).

    Called only when the underlying transport reports a connection-level
    failure — TransportConnectionFailed or TransportClosed.
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    entry = _LOOP_STATE.get(key)
    if entry is None:
        return
    client, _session_unused, lock = entry
    async with lock:
        # Re-read under the lock in case another coroutine already reset.
        entry = _LOOP_STATE.get(key)
        if entry is None:
            return
        client = entry[0]
        # Preserve the lock object so concurrent waiters keep coherent state.
        _LOOP_STATE[key] = (None, None, lock)
        if client is not None:
            try:
                await client.close_async()
            except Exception:
                # Connection is already known-bad; cleanup on a corpse is
                # not graceful error handling — the real error has already
                # been raised to the caller by _execute().
                log.debug("close_async on stale Cougaar client failed", exc_info=True)


@asynccontextmanager
async def _session():
    """Yield the shared gql session for the current event loop.

    Public contract unchanged for callers — assemblers still write
    `async with _session() as s: await _execute_list(s, _QUERY)`. The
    difference is that no new Client / transport / schema fetch happens
    here for any call after the first on this loop.
    """
    session = await _ensure_session()
    yield session


async def _execute(session, template: GraphQLRequest, variable_values: dict | None = None) -> Any:
    # COUGAAR_OFFLINE — session is None; return an empty response object
    # so `next(iter(response.values()))` in _execute_list yields []. The
    # GraphQL response shape is {<topkey>: <list-or-obj>}, so an empty
    # dict triggers the `or []` fallback below.
    if session is None:
        return {}
    request = template
    if variable_values is not None:
        request = GraphQLRequest(template, variable_values=variable_values)
    try:
        return await session.execute(request)
    except TransportQueryError as e:
        # Server-side query error. Connection is fine; do NOT rebuild.
        raise CougaarError(e.errors) from e
    except (TransportConnectionFailed, TransportClosed):
        # Transport is dead. Drop the current loop's client so the next
        # _session() call reconnects (re-fetching the schema on the fresh
        # connect). Re-raise the original exception — no graceful wrapping.
        await _reset_shared_session()
        raise


async def _execute_list(session, template: GraphQLRequest, variable_values: dict | None = None) -> list[dict]:
    # COUGAAR_OFFLINE shortcut — `_execute(None, ...)` returns {}; the
    # `next(iter({}.values()))` below would raise StopIteration, so
    # handle the empty-session case explicitly to return [].
    if session is None:
        return []
    response = await _execute(session, template, variable_values)
    return next(iter(response.values())) or []


# ---------------------------------------------------------------------------
# Reusable shape transforms
# ---------------------------------------------------------------------------


def _to_geojson_point(v):
    if not v:
        return None
    return {"type": "Point", "coordinates": [v["longitude"], v["latitude"], v.get("altitude", 0)]}


def _to_geojson_track(v):
    if not v:
        return None
    return {
        "type": "LineString",
        "coordinates": [[p["longitude"], p["latitude"], p.get("altitude", 0)] for p in v],
        "timestamps": [p.get("instant") for p in v],
    }


def _enum_literal(v):
    if isinstance(v, dict):
        return v.get("literal")
    return v


def _strip_force_suffix(v):
    v = _enum_literal(v)
    return v.replace("_FORCE", "") if v else None


def _iso_instant(v):
    if isinstance(v, dict):
        return v.get("instant")
    return v


GeoPoint = Annotated[dict | None, BeforeValidator(_to_geojson_point)]
GeoTrack = Annotated[dict | None, BeforeValidator(_to_geojson_track)]
ForceType = Annotated[str | None, BeforeValidator(_strip_force_suffix)]
EnumLit = Annotated[str | None, BeforeValidator(_enum_literal)]
IsoInstant = Annotated[str | None, BeforeValidator(_iso_instant)]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CougaarBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    uuid: str = Field(validation_alias="kbID")
    ref_id: int | None = Field(default=None, validation_alias="data__ID")
    name: str | None = Field(default=None, validation_alias="data__name")


class Unit(CougaarBase):
    force_type: ForceType = Field(default=None, validation_alias="data__forceType")
    echelon: EnumLit = Field(default=None, validation_alias="data__echelon")
    sidc: str | None = Field(default=None, validation_alias="data__sidc")
    unit_long_name: str | None = Field(default=None, validation_alias="data__unitLongName")
    unit_short_name: str | None = Field(default=None, validation_alias="data__unitShortName")
    superior_ref_id: int | None = Field(default=None, validation_alias="link__unitSuperiorID")
    location: GeoPoint = Field(default=None, validation_alias="data__location")
    historic_track: GeoTrack = Field(default=None, validation_alias="data__unitHistoricTrack")
    projected_track: GeoTrack = Field(default=None, validation_alias="data__unitProjectedTrack")
    description: str | None = Field(default=None, validation_alias="data__description")
    unit_status: EnumLit = Field(default=None, validation_alias="data__unitStatus")
    personnel_status: str | None = Field(default=None, validation_alias="data__personnelStatus")
    last_updated: IsoInstant = Field(default=None, validation_alias="data__lastUpdatedDateTime")
    composition: dict | None = None


class Equipment(CougaarBase):
    nsn: str | None = Field(default=None, validation_alias="data__nsn")
    nomenclature: str | None = Field(default=None, validation_alias="data__nomenclature")
    force_type: ForceType = Field(default=None, validation_alias="data__forceType")
    location: GeoPoint = Field(default=None, validation_alias="data__location")
    unit_ref_id: int | None = Field(default=None, validation_alias="link__unitID")
    last_updated: IsoInstant = Field(default=None, validation_alias="data__locationLastUpdate")


class SpatialControl(CougaarBase):
    subtype: str | None = Field(default=None, validation_alias="objectType")
    action_type: EnumLit = Field(default=None, validation_alias="data__actionType")
    description: str | None = Field(default=None, validation_alias="data__Description")
    color: str | None = Field(default=None, validation_alias="data__color")
    region_status: EnumLit = Field(default=None, validation_alias="data__regionStatus")
    effective_start: IsoInstant = Field(default=None, validation_alias="data__EffectiveStartDate")
    effective_end: IsoInstant = Field(default=None, validation_alias="data__EffectiveEndDate")
    source: str | None = Field(default=None, validation_alias="data__source")
    width: int | None = Field(default=None, validation_alias="data__width")
    coordinates: GeoPoint = Field(default=None, validation_alias="data__coordinates")


# --- IDX intel-report models ---


class IDXImageIirItem(CougaarBase):
    target_name: str | None = Field(default=None, validation_alias="data__targetName")
    be_number: str | None = Field(default=None, validation_alias="data__basicEncyclopediaNumber")
    country: str | None = Field(default=None, validation_alias="data__country")
    item_number: str | None = Field(default=None, validation_alias="data__itemNumber")
    target_function_code: str | None = Field(default=None, validation_alias="data__targetFunctionCode")
    status_activity: str | None = Field(default=None, validation_alias="data__statusActivitySummary")
    national_tasking_indicator: str | None = Field(default=None, validation_alias="data__nationalTaskingIndicator")
    erref: str | None = Field(default=None, validation_alias="data__erref")
    suffix: str | None = Field(default=None, validation_alias="data__suffix")


class IDXImageIirReport(CougaarBase):
    serial: str | None = Field(default=None, validation_alias="data__serial")
    item_type: str | None = Field(default=None, validation_alias="data__itemType")
    highlights: str | None = Field(default=None, validation_alias="data__highlights")
    imagery_mission_number: str | None = Field(default=None, validation_alias="data__imageryMissionNumber")
    project_identifier_code: str | None = Field(default=None, validation_alias="data__projectIdentifierCode")
    report_time: IsoInstant = Field(default=None, validation_alias="data__reportTime")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXReccexrepReport(CougaarBase):
    """RECONnaissance EXploitation REPort — doctrinal BDA-feeding format.
    GRVTY populates target_status (the BDA grade — e.g. DESTROYED /
    DEGRADED / NO_EFFECT) + gentext (narrative description) + ben
    (Basic Encyclopedia Number for the target reference) + mission /
    timing fields. Schema confirmed via __type introspection against
    live cougaar 2026-06-10. Joined to IDXImageryReport (which carries
    the imagery_url) via observation IDs or shared mission code."""
    mission_type: str | None = Field(default=None, validation_alias="data__missionType")
    mission_code: str | None = Field(default=None, validation_alias="data__missionCode")
    mission_platform_or_type: str | None = Field(default=None, validation_alias="data__missionPlatformOrType")
    target_type: str | None = Field(default=None, validation_alias="data__targetType")
    target_status: str | None = Field(default=None, validation_alias="data__targetStatus")
    item_name: str | None = Field(default=None, validation_alias="data__itemName")
    item_number: str | None = Field(default=None, validation_alias="data__itemNumber")
    ben: str | None = Field(default=None, validation_alias="data__ben")
    gentext: str | None = Field(default=None, validation_alias="data__gentext")
    time_on_target: str | None = Field(default=None, validation_alias="data__timeOnTarget")
    report_time: IsoInstant = Field(default=None, validation_alias="data__reportTime")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXImageryReport(CougaarBase):
    """Cougaar's imagery report vertex — carries the URL to the actual
    image file plus metadata. GRVTY (sim staff) populates imagery_url
    for BDA imagery; consumed downstream by BDA + per-target products.
    Schema confirmed via __type introspection against live cougaar
    2026-06-10."""
    imagery_url: str | None = Field(default=None, validation_alias="data__imageryUrl")
    imagery_type: str | None = Field(default=None, validation_alias="data__imageryType")
    image_number: str | None = Field(default=None, validation_alias="data__imageNumber")
    image_time: IsoInstant = Field(default=None, validation_alias="data__imageTime")
    imagery_mission_number: str | None = Field(default=None, validation_alias="data__imageryMissionNumber")
    sensor_position: str | None = Field(default=None, validation_alias="data__sensorPosition")
    project_identifier_code: str | None = Field(default=None, validation_alias="data__projectIdentifierCode")
    expansion_item: str | None = Field(default=None, validation_alias="data__expansionItem")
    universal_reference_grid: str | None = Field(default=None, validation_alias="data__universalReferenceGrid")
    index_number: str | None = Field(default=None, validation_alias="data__indexNumber")
    fusion_observation_ref_id: int | None = Field(default=None, validation_alias="data__fusionObservationRefID")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXTacticalReport(CougaarBase):
    force_threat_designator: str | None = Field(default=None, validation_alias="data__forceThreatDesignator")
    emitter_ew: str | None = Field(default=None, validation_alias="data__emitterEw")
    is_simulated: bool | None = Field(default=None, validation_alias="data__isSimulated")
    report_time: IsoInstant = Field(default=None, validation_alias="data__reportTime")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXEnemyOOBReport(CougaarBase):
    unit_key: str | None = Field(default=None, validation_alias="data__unitKey")
    enemy_unit: str | None = Field(default=None, validation_alias="data__enemyUnit")
    activity: str | None = Field(default=None, validation_alias="data__activity")
    event: str | None = Field(default=None, validation_alias="data__event")
    observation_time: IsoInstant = Field(default=None, validation_alias="data__observationTime")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXTacElintReport(CougaarBase):
    event_description: str | None = Field(default=None, validation_alias="data__eventDescription")
    event_entity_count: str | None = Field(default=None, validation_alias="data__eventEntityCount")
    force_threat_designator: str | None = Field(default=None, validation_alias="data__forceThreatDesignator")
    is_simulated: bool | None = Field(default=None, validation_alias="data__isSimulated")
    reported_time: IsoInstant = Field(default=None, validation_alias="data__reportedTime")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXSignalOfInterest(CougaarBase):
    signal_id: str | None = Field(default=None, validation_alias="data__signalId")
    emitter_name: str | None = Field(default=None, validation_alias="data__emitterName")
    platform_or_sensor_id: str | None = Field(default=None, validation_alias="data__platformOrSensorId")
    start_time: IsoInstant = Field(default=None, validation_alias="data__startTime")
    end_time: IsoInstant = Field(default=None, validation_alias="data__endTime")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXWranglerReport(CougaarBase):
    """WRANGLER source reports — second SIGINT-class vertex feeding the
    KLIEGLIGHT autofire. Per cougaar sim staff (2026-06-10): WRANGLER →
    IDXWranglerReport (its own vertex), CMF + TACELINT → IDXTacElintReport
    (shared). Schema-confirmed via __type introspection against the live
    cougaar at 10.10.23.134:8200."""
    intercept_case_number: str | None = Field(default=None, validation_alias="data__interceptCaseNumber")
    signal_case_number: str | None = Field(default=None, validation_alias="data__signalCaseNumber")
    emitter_function: str | None = Field(default=None, validation_alias="data__emitterFunction")
    modulation_type: str | None = Field(default=None, validation_alias="data__modulationType")
    scan_type: str | None = Field(default=None, validation_alias="data__scanType")
    fusion_ref_id: int | None = Field(default=None, validation_alias="data__fusionRefID")
    doi_time_up: IsoInstant = Field(default=None, validation_alias="data__doiTimeUp")
    doi_time_down: IsoInstant = Field(default=None, validation_alias="data__doiTimeDown")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


class IDXHumintReport(CougaarBase):
    report_type: str | None = Field(default=None, validation_alias="data__reportType")
    report_id: str | None = Field(default=None, validation_alias="data__reportId")
    summary: str | None = Field(default=None, validation_alias="data__summary")
    raw_text: str | None = Field(default=None, validation_alias="data__rawText")
    source_type: str | None = Field(default=None, validation_alias="data__sourceType")
    source_name: str | None = Field(default=None, validation_alias="data__sourceName")
    source_channel: str | None = Field(default=None, validation_alias="data__sourceChannel")
    source_account_or_org: str | None = Field(default=None, validation_alias="data__sourceAccountOrOrg")
    reliability_tier: str | None = Field(default=None, validation_alias="data__reliabilityTier")
    deception_risk: str | None = Field(default=None, validation_alias="data__deceptionRisk")
    bias_indicators: str | None = Field(default=None, validation_alias="data__biasIndicators")
    assessment_rationale: str | None = Field(default=None, validation_alias="data__assessmentRationale")
    report_time: IsoInstant = Field(default=None, validation_alias="data__reportTime")
    time_bucket: IsoInstant = Field(default=None, validation_alias="data__timeBucket")
    location: GeoPoint = Field(default=None, validation_alias="data__location")
    location_text: str | None = Field(default=None, validation_alias="data__locationText")


class IDXOpenSourceReport(CougaarBase):
    """OSINT per-event open-source report — FM 2-0 §1-82 OSIR source vertex.
    One row per discrete open-source observation (Telegram post, news article,
    broadcast intercept, academic publication, web post). The OSIR writer
    renders ONE published OSIR per row; this vertex IS the source, not a
    rollup of many.

    Field family aligns with the customer-provided OSIR example shape so the
    rendered doc maps 1:1 onto §1 Source / §2 Summary / §3 Details / §4
    Geospatial / §5 Collector's Comments. Live cougaar does not yet host this
    vertex; the scenario-fixture fallback in event_data_fixtures._parse_osint
    populates the same shape until cougaar adds it."""
    # Header / identity
    report_id: str | None = Field(default=None, validation_alias="data__reportId")
    subject: str | None = Field(default=None, validation_alias="data__subject")
    country_region: str | None = Field(default=None, validation_alias="data__countryRegion")
    publication_time: IsoInstant = Field(default=None, validation_alias="data__publicationTime")
    acquisition_time: IsoInstant = Field(default=None, validation_alias="data__acquisitionTime")
    capture_time: IsoInstant = Field(default=None, validation_alias="data__captureTime")
    # §1 Source description + reliability (ICD 203)
    source_type: str | None = Field(default=None, validation_alias="data__sourceType")
    source_name: str | None = Field(default=None, validation_alias="data__sourceName")
    source_channel: str | None = Field(default=None, validation_alias="data__sourceChannel")
    source_account_or_org: str | None = Field(default=None, validation_alias="data__sourceAccountOrOrg")
    source_url: str | None = Field(default=None, validation_alias="data__sourceUrl")
    source_reliability_icd203: str | None = Field(default=None, validation_alias="data__sourceReliabilityIcd203")
    source_reliability_rationale: str | None = Field(default=None, validation_alias="data__sourceReliabilityRationale")
    information_credibility: str | None = Field(default=None, validation_alias="data__informationCredibility")
    information_credibility_rationale: str | None = Field(default=None, validation_alias="data__informationCredibilityRationale")
    # §2 + §3 Body content (verbatim source text + media)
    summary: str | None = Field(default=None, validation_alias="data__summary")
    body_text: str | None = Field(default=None, validation_alias="data__bodyText")
    body_text_original_language: str | None = Field(default=None, validation_alias="data__bodyTextOriginalLanguage")
    translation_note: str | None = Field(default=None, validation_alias="data__translationNote")
    media_description: str | None = Field(default=None, validation_alias="data__mediaDescription")
    # §4 Geospatial + technical metadata
    location: GeoPoint = Field(default=None, validation_alias="data__location")
    location_mgrs: str | None = Field(default=None, validation_alias="data__locationMgrs")
    location_named_landmark: str | None = Field(default=None, validation_alias="data__locationNamedLandmark")
    tactical_markings: str | None = Field(default=None, validation_alias="data__tacticalMarkings")
    # §5 Collector's comments
    pir_alignment: str | None = Field(default=None, validation_alias="data__pirAlignment")
    contextual_note: str | None = Field(default=None, validation_alias="data__contextualNote")
    cross_cueing_recommendation: str | None = Field(default=None, validation_alias="data__crossCueingRecommendation")


class IDXSensorContactReport(CougaarBase):
    """SPOTREP / SIGACT-class generic sensor contact. FM 6-99 §A-174 SPOTREP +
    IEWTPT SIGACT message family. Carries who/where/when of a sensor-observed
    enemy unit + activity. Feeds INTSUM / SITREP / Threat Overlay directly."""
    observation_time: IsoInstant = Field(default=None, validation_alias="data__observationTime")
    location: GeoPoint = Field(default=None, validation_alias="data__location")
    enemy_unit: str | None = Field(default=None, validation_alias="data__enemyUnit")
    unit_key: str | None = Field(default=None, validation_alias="data__unitKey")
    activity: str | None = Field(default=None, validation_alias="data__activity")


class IDXEnemySituationalAwarenessReport(CougaarBase):
    """ENSIT-class enemy situation report. FM 6-99 ENSIT format + IEWTPT
    Enemy-Sit / ENSIT message family. Carries a structured snapshot of an
    enemy unit's disposition, strength, organization, and operational
    status. Feeds Threat Overlay / SITEMP / Threat Model directly."""
    location: GeoPoint = Field(default=None, validation_alias="data__location")
    enemy_observation_time: IsoInstant = Field(default=None, validation_alias="data__enemyObservationTime")
    unit_designation: str | None = Field(default=None, validation_alias="data__unitDesignation")
    extended_unit_number: int | None = Field(default=None, validation_alias="data__extendedUnitNumber")
    enemy_unit_master_key: str | None = Field(default=None, validation_alias="data__enemyUnitMasterKey")
    enemy_unit_classification: str | None = Field(default=None, validation_alias="data__enemyUnitClassification")
    enemy_parent_unit_designator: str | None = Field(default=None, validation_alias="data__enemyParentUnitDesignator")
    parent_master_key: str | None = Field(default=None, validation_alias="data__parentMasterKey")
    parent_extended_unit_number: str | None = Field(default=None, validation_alias="data__parentExtendedUnitNumber")
    parent_unit_echelon: str | None = Field(default=None, validation_alias="data__parentUnitEchelon")
    parent_unit_type: str | None = Field(default=None, validation_alias="data__parentUnitType")
    parent_honorific_name: str | None = Field(default=None, validation_alias="data__parentHonorificName")
    honorific_name: str | None = Field(default=None, validation_alias="data__honorificName")
    echelon: str | None = Field(default=None, validation_alias="data__echelon")
    organization_type: str | None = Field(default=None, validation_alias="data__organizationType")
    functional_role: str | None = Field(default=None, validation_alias="data__functionalRole")
    operational_status: str | None = Field(default=None, validation_alias="data__operationalStatus")
    enemy_intel_eval_stage: str | None = Field(default=None, validation_alias="data__enemyIntelEvalStage")
    activity_type: str | None = Field(default=None, validation_alias="data__activityType")
    condition: str | None = Field(default=None, validation_alias="data__condition")
    unit_strength: int | None = Field(default=None, validation_alias="data__unitStrength")
    equipment_percent: int | None = Field(default=None, validation_alias="data__equipmentPercent")
    nationality: str | None = Field(default=None, validation_alias="data__nationality")
    symbol_id: str | None = Field(default=None, validation_alias="data__symbolId")
    database_system_name: str | None = Field(default=None, validation_alias="data__databaseSystemName")
    database_key: str | None = Field(default=None, validation_alias="data__databaseKey")


class IDXCommunicationsData(CougaarBase):
    """COMINT-class comms data — TACREP COMEW (IEWTPT USMTF) component. Carries
    the technical parameters of an intercepted comms emission (bandwidth,
    frequency, EW callsign). Augments SIGSUM and feeds the COMINT lane of
    SIGINT-discipline analysis."""
    bandwidth: str | None = Field(default=None, validation_alias="data__bandwidth")
    ew_callsign: str | None = Field(default=None, validation_alias="data__ewCallsign")
    frequency_raw: str | None = Field(default=None, validation_alias="data__frequencyRaw")


class IDXRedUnit(CougaarBase):
    """IDX 26 Donovian (RED) OOB vertex.

    Schema-confirmed against live cougaar at 10.10.23.134:8200 (2026-06-07).
    The schema added direct intel-state fields on RedUnit rather than
    requiring an ENSIT join: `affiliation`, `disposition`, `currentActivity`,
    `lastUpdated`, `composition`, `strengthPctManned`, `engagementRange`,
    `detectionRange`, `lastIntelStage`, `descriptionNarrative`,
    `additionalInfo`, `symbolicIdentifier` (this is the new SIDC field name).
    Cougaar typically returns these as null for the bulk OOB feed today —
    fixture fallback still required — but when populated they flow straight
    into the TIDAT-facing attrs without a separate ENSIT merge step.

    Legacy `World.Organizations.Unit` vertex's currently-loaded rows are
    NTC (CONUS) and must NOT be treated as IDX 26 RED OOB; this vertex is
    the authoritative source once organizer ingestion populates it. When
    this returns 0 rows OR populated rows with no location, callers fall
    back to scenario-authored fixture (NEVER to Unit).
    """
    parent_organization_type: str | None = Field(default=None, validation_alias="data__parentOrganizationType")
    honorific_name: str | None = Field(default=None, validation_alias="data__honorificName")
    echelon: str | None = Field(default=None, validation_alias="data__echelon")
    parent_extended_unit_number: str | None = Field(default=None, validation_alias="data__parentExtendedUnitNumber")
    parent_honorific_name: str | None = Field(default=None, validation_alias="data__parentHonorificName")
    parent_echelon: str | None = Field(default=None, validation_alias="data__parentEchelon")
    parent_unit_designator: str | None = Field(default=None, validation_alias="data__parentUnitDesignator")
    extended_unit_number: str | None = Field(default=None, validation_alias="data__extendedUnitNumber")
    organization_type: str | None = Field(default=None, validation_alias="data__organizationType")
    unit_designator: str | None = Field(default=None, validation_alias="data__unitDesignator")
    # New IDX-26 RedUnit fields added to the schema in the 2026-06 cougaar
    # migration. Direct intel state — no ENSIT join required.
    affiliation: str | None = Field(default=None, validation_alias="data__affiliation")
    disposition: str | None = Field(default=None, validation_alias="data__disposition")
    current_activity: str | None = Field(default=None, validation_alias="data__currentActivity")
    composition: str | None = Field(default=None, validation_alias="data__composition")
    strength_pct_manned: str | None = Field(default=None, validation_alias="data__strengthPctManned")
    engagement_range: int | None = Field(default=None, validation_alias="data__engagementRange")
    detection_range: int | None = Field(default=None, validation_alias="data__detectionRange")
    last_intel_stage: str | None = Field(default=None, validation_alias="data__lastIntelStage")
    description_narrative: str | None = Field(default=None, validation_alias="data__descriptionNarrative")
    additional_info: str | None = Field(default=None, validation_alias="data__additionalInfo")
    symbolic_identifier: str | None = Field(default=None, validation_alias="data__symbolicIdentifier")
    last_updated: IsoInstant = Field(default=None, validation_alias="data__lastUpdated")
    location: GeoPoint = Field(default=None, validation_alias="data__location")


# --- Output: fused observation (shape-compatible with Cougaar's IDXFusionObservation) ---


class FusedObservation(BaseModel):
    """Adapter output for intel reports — same shape Cougaar's fusion would emit.

    `source_int` is the INT family (IMINT/SIGINT/ELINT/HUMINT/OOB/TACTICAL).
    `source_kbids` lists the underlying report vertices that backed this observation.
    `entity_key` is the natural key used for dedup (BE number, signalId, unitKey, etc.).
    """

    observation_id: str
    name: str | None = None
    source_int: str
    source_kbids: list[str]
    entity_key: str | None = None
    entity_name: str | None = None
    confidence: float | None = None
    evidence_text: str | None = None
    observed_time: str | None = None
    reported_time: str | None = None
    location: dict | None = None
    raw_records: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Transform dispatcher
# ---------------------------------------------------------------------------


_MODELS: dict[str, type[CougaarBase]] = {
    "World.Organizations.Unit": Unit,
    "World.Materiels.Equipment": Equipment,
    "World.Informations.RegionSpatialControl": SpatialControl,
    "World.Informations.LinearSpatialControl": SpatialControl,
    "World.Informations.BoundarySpatialControl": SpatialControl,
    "World.Informations.NOGOSpatialControl": SpatialControl,
    "World.Informations.SLOWGOSpatialControl": SpatialControl,
    "World.Informations.CorridorSpatialControl": SpatialControl,
    "World.Informations.NamedAreaOfInterest": SpatialControl,
    "World.Informations.IDXImageIirItem": IDXImageIirItem,
    "World.Informations.IDXImageIirReport": IDXImageIirReport,
    "World.Informations.IDXTacticalReport": IDXTacticalReport,
    "World.Informations.IDXEnemyOrderOfBattleReport": IDXEnemyOOBReport,
    "World.Informations.IDXTacElintReport": IDXTacElintReport,
    "World.Informations.IDXSignalOfInterestReport": IDXSignalOfInterest,
    "World.Informations.IDXWranglerReport": IDXWranglerReport,
    "World.Informations.IDXHumintReport": IDXHumintReport,
    "World.Informations.IDXOpenSourceReport": IDXOpenSourceReport,
    "World.Informations.IDXSensorContactReport": IDXSensorContactReport,
    "World.Informations.IDXEnemySituationalAwarenessReport": IDXEnemySituationalAwarenessReport,
    "World.Informations.IDXCommunicationsData": IDXCommunicationsData,
    "World.Informations.IDXImageryReport": IDXImageryReport,
    "World.Informations.IDXReccexrepReport": IDXReccexrepReport,
    "World.Organizations.IDXRedUnit": IDXRedUnit,
}


_CONSUMED_ACTION_TYPES: list[str] = [
    "FORWARD_LINE_OF_OWN_TROOPS",
    "NAMED_AREA_OF_INTEREST",
    "TARGETED_AREA_OF_INTEREST",
    "PHASE_LINE",
    "OBJECTIVE",
    "NO_GO_AREA",
    "SLOW_GO_AREA",
    "MAIN_AXIS_OF_ADVANCE",
    "SUPPORTING_AXIS_OF_ADVANCE",
]


# Cougaar SpatialControl.action_type → geo-agent overlay kind (+ optional
# subkind carried as attrs.subkind for variant-aware paint dispatch). One
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


def _transform(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows or []:
        object_type = r.get("objectType")
        if object_type not in _MODELS:
            continue
        is_spatial = (
            object_type.startswith("World.Informations.")
            and object_type.endswith("SpatialControl")
        ) or object_type == "World.Informations.NamedAreaOfInterest"
        if is_spatial:
            action_value = r.get("data__actionType")
            if isinstance(action_value, dict):
                action_value = action_value.get("literal")
            if action_value not in _CONSUMED_ACTION_TYPES:
                continue
        out.append(_MODELS[object_type].model_validate(r).model_dump())
    return out


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


def _filter_since(records: list[dict], time_field: str, since_iso: str | None) -> list[dict]:
    if not since_iso:
        return records
    return [r for r in records if (r.get(time_field) or "") >= since_iso]


def _fuse_imint_items(items: list[dict], reports: list[dict]) -> list[FusedObservation]:
    """Fuse IMINT items by basicEncyclopediaNumber.

    items: IDXImageIirItem records (carry the target identity)
    reports: IDXImageIirReport records (carry imagery acquisition metadata)
    """
    reports_by_uuid = {r["uuid"]: r for r in reports}
    by_be: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        be = it.get("be_number")
        if not be:
            continue
        by_be[be].append(it)

    out: list[FusedObservation] = []
    for be, group in by_be.items():
        target_name = group[0].get("target_name")
        country = group[0].get("country")
        kbids = [g["uuid"] for g in group]
        latest_report_time = max(
            (r.get("report_time") for r in reports_by_uuid.values() if r.get("report_time")),
            default=None,
        )
        out.append(
            FusedObservation(
                observation_id=f"imint:{be}",
                name=target_name,
                source_int="IMINT",
                source_kbids=kbids,
                entity_key=be,
                entity_name=target_name,
                evidence_text=f"{len(group)} IMINT report(s); country={country}",
                reported_time=latest_report_time,
                raw_records=group,
            )
        )
    return out


def _fuse_sigint(records: list[dict]) -> list[FusedObservation]:
    """Group SIGINT records by signalId."""
    by_signal: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        sig = r.get("signal_id")
        if not sig:
            continue
        by_signal[sig].append(r)
    out = []
    for sig, group in by_signal.items():
        emitters = sorted({r.get("emitter_name") for r in group if r.get("emitter_name")})
        kbids = [g["uuid"] for g in group]
        reported = max(
            (r.get("end_time") or r.get("start_time") for r in group if r.get("end_time") or r.get("start_time")),
            default=None,
        )
        out.append(
            FusedObservation(
                observation_id=f"sigint:{sig}",
                name=", ".join(emitters) or None,
                source_int="SIGINT",
                source_kbids=kbids,
                entity_key=sig,
                entity_name=", ".join(emitters) or None,
                evidence_text=f"{len(group)} SIGINT detection(s) of signal {sig}",
                reported_time=reported,
                raw_records=group,
            )
        )
    return out


def _fuse_oob(records: list[dict]) -> list[FusedObservation]:
    """Group OOB reports by unitKey. Drop scaffolds (records without a unitKey)."""
    by_unit: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        uk = r.get("unit_key")
        if not uk:
            continue
        by_unit[uk].append(r)
    out = []
    for unit_key, group in by_unit.items():
        kbids = [g["uuid"] for g in group]
        activities = sorted({r.get("activity") for r in group if r.get("activity")})
        reported = max(
            (r.get("observation_time") for r in group if r.get("observation_time")),
            default=None,
        )
        out.append(
            FusedObservation(
                observation_id=f"oob:{unit_key}",
                name=group[0].get("enemy_unit"),
                source_int="OOB",
                source_kbids=kbids,
                entity_key=unit_key,
                entity_name=group[0].get("enemy_unit"),
                evidence_text=f"{len(group)} OOB report(s); activities={','.join(activities) or '-'}",
                reported_time=reported,
                raw_records=group,
            )
        )
    return out


def _passthrough_fuse(records: list[dict], source_int: str, time_field: str, key_field: str | None = None) -> list[FusedObservation]:
    """No natural-key dedup; each record becomes its own observation."""
    out = []
    for r in records:
        key = r.get(key_field) if key_field else None
        out.append(
            FusedObservation(
                observation_id=f"{source_int.lower()}:{r['uuid']}",
                name=r.get("name"),
                source_int=source_int,
                source_kbids=[r["uuid"]],
                entity_key=key,
                entity_name=r.get("name"),
                reported_time=r.get(time_field),
                location=r.get("location"),
                raw_records=[r],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Assemblers
# ---------------------------------------------------------------------------


# Process-wide TTL cache for cougaar tool calls. Within a single autodraft
# run the same OOB / spatial overlay / target set is fetched many times
# (one per dependent paragraph_field). Audit found SITEMP made 106-116
# cougaar calls per COA — many were duplicates. 30-second TTL dedupes
# them without preventing live updates from landing.
_TOOL_CACHE: dict[tuple, dict] = {}


def _tool_cache_get(key: tuple) -> dict | None:
    entry = _TOOL_CACHE.get(key)
    if entry is None:
        return None
    if (time.monotonic() - entry["fetched_at"]) > _CACHE_TTL_SEC:
        _TOOL_CACHE.pop(key, None)
        return None
    return entry["value"]


def _tool_cache_put(key: tuple, value: dict) -> None:
    _TOOL_CACHE[key] = {"value": value, "fetched_at": time.monotonic()}


async def get_oob(force_type: str) -> dict:
    """Force-level OOB with equipment composition rolled up.

    Returns `{"units": [...]}`. Each unit's `composition.equipment_list_with_counts`
    is built by grouping the Equipment fetch under `unit_ref_id` and counting by
    `nomenclature`.

    Cached (30 s TTL, keyed by force_type) — see _TOOL_CACHE.
    """
    if force_type not in ("RED", "BLUE"):
        raise ValueError(f"force_type must be 'RED' or 'BLUE', got {force_type!r}")
    cached = _tool_cache_get(("get_oob", force_type))
    if cached is not None:
        return cached
    force_value = f"{force_type}_FORCE"

    async with _session() as s:
        units_rows, equipment_rows = await asyncio.gather(
            _execute_list(s, _units_query(force_value)),
            _execute_list(s, _equipment_query(force_value)),
        )
    # Cougaar's propertyKeyValuePairs filter doesn't actually filter on
    # data__forceType — it returns all rows regardless. Drop server-side filtering
    # and apply force_type filter client-side.
    units = [u for u in _transform(units_rows) if u.get("force_type") == force_type]
    equipment = [e for e in _transform(equipment_rows) if e.get("force_type") == force_type]

    rollup: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in equipment:
        unit_id = item.get("unit_ref_id")
        nomenclature = item.get("nomenclature")
        if unit_id is not None and nomenclature:
            rollup[unit_id][nomenclature] += 1

    for unit in units:
        unit_rollup = rollup.get(unit.get("ref_id"), {})
        unit["composition"] = {
            "personnel_count": None,
            "weapons_list_with_counts": [],
            "equipment_list_with_counts": [
                {"nomenclature": nomenclature, "count": count}
                for nomenclature, count in unit_rollup.items()
            ],
        }

    result = {"units": units}
    _tool_cache_put(("get_oob", force_type), result)
    return result


# Per-force snapshot timestamp for source-level OOB freshness comparison.
# Updated by get_red_oob_idx() when the IDXRedUnit query returns >0 rows
# (an empty IDXRedUnit means organizer hasn't ingested yet, so we leave the
# prior fetch ts in place — never "ages" the scenario fixture against an
# empty live source). intel_agent.tool_get_oob compares this against
# scenario.KG_OOB_AUTHORED_AT_MS and uses whichever source is fresher.
_OOB_LAST_FETCH_TS_MS: dict[str, int] = {}


def oob_last_fetch_ts_ms(force_type: str) -> int:
    """Last time IDXRedUnit (or equivalent live OOB source) returned a
    non-empty snapshot for the given force. Returns 0 if never fetched
    or never non-empty."""
    return _OOB_LAST_FETCH_TS_MS.get(force_type.upper(), 0)


async def get_red_oob_idx() -> dict:
    """IDX 26 RED (Donovian) OOB from the purpose-built IDXRedUnit vertex.

    Returns `{"units": [...]}` of transformed IDXRedUnit records. Empty list
    is the expected state until organizer ingestion (`scenario_ingest.
    parse_and_apply_opord`) populates the vertex from the OPORD parse.

    On non-empty result, stamps `_OOB_LAST_FETCH_TS_MS["RED"]` = current epoch
    ms. tool_get_oob compares this against scenario.KG_OOB_AUTHORED_AT_MS to
    pick the freshest source.

    This is NOT a substitute for `get_oob('RED')` (which queries the legacy
    World.Organizations.Unit vertex). The intel-agent tool layer prefers
    this assembler for RED OOB and falls through to scenario fixture on
    empty — it never falls through to `Unit` because that vertex's
    currently-loaded rows are a different scenario (NTC California)."""
    async with _session() as s:
        rows = await _execute_list(s, _IDX_RED_UNITS_ALL)
    units = _transform(rows)
    if units:
        _OOB_LAST_FETCH_TS_MS["RED"] = int(time.time() * 1000)
    return {"units": units}


# ---------------------------------------------------------------------------
# Resolved-OOB primitive (§ 12 of per-coa-product-scope.md).
# ---------------------------------------------------------------------------
# Fuses what cougaar provides but does NOT itself fuse: the equipment-to-bearer
# linkage is fully wired (95k+ Equipment_RedUnit edges) but the ~2,500 Unknown
# Red Unit stubs (one per orphan EOBSREP/TacElint observation) never get
# attributed to a named formation. This adapter does that fusion in pytho-app,
# stores the raw cougaar response for the analyst-facing GraphQL viewer, and
# exposes the resolved view to every downstream RED-OOB consumer (HVTL,
# CARVER, HPTL, SITEMP, Threat Overlay, live map, range fans, BDA, ...).
#
# Algorithm:
#   1. Fetch IDXRedUnit + Equipment + Equipment_RedUnit edges.
#   2. Derive per-unit rate_of_march_kmh from organic composition (slowest-
#      self-propelled-element rule per FM 4-01.30 + TC 7-100.2).
#   3. For each Unknown Red Unit stub: cluster at 1 km (battery dispersal
#      radius per FM 3-09 / OPFOR practice).
#   4. For each cluster: score against every named unit via
#      composition_tfidf × movement_feasibility × echelon_match × activity.
#   5. Attribute HIGH/MED-confidence clusters; flag the rest as
#      unidentified-formation candidates for the PIR loop.
#
# Raw cougaar response is persisted to disk on every successful refresh so
# the live-map GraphQL viewer can surface "what cougaar failed to fuse vs
# what pytho fused itself".

import json as _json
import math as _math
import os as _os
from datetime import datetime as _datetime

from utils.equipment_doctrine import (  # noqa: E402  (intentional late import)
    derive_unit_rate_of_march as _derive_unit_rate,
    fm360_category as _fm360_cat,
    is_hvt_worthy as _is_hvt_worthy,
    echelon_ord as _ech_ord,
)

# Where the raw cougaar snapshot for the resolved-OOB call is written. The
# GraphQL viewer surface (UI) reads from this directory to show "raw cougaar
# vs pytho-fused" deltas. One file per refresh keyed by epoch ms; latest
# symlink at oob_resolved_latest.json. Bounded retention: keep newest 30.
_OOB_SNAPSHOT_DIR = _os.environ.get(
    "PYTHO_OOB_SNAPSHOT_DIR", "/app/data/cougaar_raw_snapshots"
)


def _haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    R = 6371.0
    p1 = _math.radians(a_lat)
    p2 = _math.radians(b_lat)
    dp = _math.radians(b_lat - a_lat)
    dl = _math.radians(b_lon - a_lon)
    h = _math.sin(dp / 2) ** 2 + _math.cos(p1) * _math.cos(p2) * _math.sin(dl / 2) ** 2
    return 2 * R * _math.asin(_math.sqrt(h))


def _parse_iso_dt(s: str | None):
    if not s:
        return None
    try:
        s2 = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
        return _datetime.fromisoformat(s2)
    except Exception:
        return None


def _cluster_unionfind(points: list[dict], radius_km: float) -> dict[int, list[int]]:
    """O(n²) union-find clustering. For IDX 26's 600-ish HVT-bearing Unknown
    stubs this is well under a second; if the orphan pool grows past a few
    thousand swap for a grid-bucketed neighbor search."""
    n = len(points)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if _haversine_km(
                points[i]["lat"], points[i]["lon"],
                points[j]["lat"], points[j]["lon"],
            ) <= radius_km:
                union(i, j)
    out: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        out[find(i)].append(i)
    return out


def _cosine_tfidf(c1: dict[str, int], c2: dict[str, int], idf: dict[str, float]) -> float:
    v1 = {k: v * idf.get(k, 1.0) for k, v in c1.items()}
    v2 = {k: v * idf.get(k, 1.0) for k, v in c2.items()}
    keys = set(v1) | set(v2)
    num = sum(v1.get(k, 0) * v2.get(k, 0) for k in keys)
    a = _math.sqrt(sum(v * v for v in v1.values()))
    b = _math.sqrt(sum(v * v for v in v2.values()))
    return (num / (a * b)) if (a and b) else 0.0


# Confidence thresholds (per § 12.4 of the design doc, validated on IDX 26).
_CONF_HIGH_SCORE = 0.10
_CONF_HIGH_MARGIN = 0.03
_CONF_MED_SCORE = 0.04
# Cluster radius — battery dispersal per FM 3-09.
_CLUSTER_RADIUS_KM = 1.0
# Cross-country movement factor — half of road sustained per FM 4-01.30.
_CROSS_COUNTRY_FACTOR = 0.5
# Default time delta when one side lacks a timestamp.
_DEFAULT_DT_HOURS = 1.0


def _write_oob_snapshot(payload: dict, raw: dict) -> str | None:
    """Persist the raw cougaar response + the resolved payload for the
    GraphQL viewer surface. Best-effort; never raises into the caller."""
    try:
        _os.makedirs(_OOB_SNAPSHOT_DIR, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        path = _os.path.join(_OOB_SNAPSHOT_DIR, f"oob_resolved_{ts_ms}.json")
        with open(path, "w") as f:
            _json.dump({
                "snapshot_ts_ms": ts_ms,
                "raw": raw,            # what cougaar returned, verbatim
                "resolved": payload,   # what we fused on top
            }, f, default=str)
        # update latest symlink
        latest = _os.path.join(_OOB_SNAPSHOT_DIR, "oob_resolved_latest.json")
        try:
            if _os.path.islink(latest) or _os.path.exists(latest):
                _os.unlink(latest)
            _os.symlink(_os.path.basename(path), latest)
        except OSError:
            pass
        # bounded retention — keep newest 30
        try:
            files = sorted(
                (f for f in _os.listdir(_OOB_SNAPSHOT_DIR)
                 if f.startswith("oob_resolved_") and f.endswith(".json")),
                reverse=True,
            )
            for old in files[30:]:
                try:
                    _os.unlink(_os.path.join(_OOB_SNAPSHOT_DIR, old))
                except OSError:
                    pass
        except OSError:
            pass
        return path
    except Exception as e:
        log.warning("oob_snapshot write failed: %s", e)
        return None


# Process-wide "last good" cache of the resolved RED OOB. Survives the
# 60s _TOOL_CACHE eviction. Used when cougaar goes dark mid-exercise so
# the master map keeps the last-known-good unit snapshot instead of
# dropping to zero (which is what PYTHO_DISABLE_SCENARIO_FIXTURES=1
# would do otherwise — scenario fallback is intentionally off when that
# flag is on, but disappearing units is not acceptable). Replaced
# in-place on every successful impl call. No TTL — stale is better
# than empty.
_LAST_GOOD_RESOLVED_OOB: dict | None = None
# Optional disk-persist path so a docker-api restart re-warms the cache.
_LAST_GOOD_RESOLVED_OOB_PATH = _os.environ.get(
    "PYTHO_OOB_LAST_GOOD_PATH", "/app/data/cougaar_raw_snapshots/oob_last_good.json"
)


def _load_last_good_resolved_oob_from_disk() -> dict | None:
    """Boot-time hydration of the last-good resolved-OOB cache. Best-
    effort — returns None when the persist file doesn't exist or is
    unreadable. Caller stamps `_stale=True` so consumers know the data
    pre-dates the current process lifetime."""
    try:
        path = _LAST_GOOD_RESOLVED_OOB_PATH
        if not _os.path.exists(path):
            return None
        with open(path) as f:
            data = _json.load(f)
        if isinstance(data, dict):
            data["_stale"] = True
            data["_stale_reason"] = "loaded from disk on adapter init (cougaar may have been down at startup)"
            return data
        return None
    except Exception as e:
        log.warning("last-good OOB disk hydrate failed: %s", e)
        return None


_LAST_GOOD_RESOLVED_OOB = _load_last_good_resolved_oob_from_disk()


def _persist_last_good_resolved_oob(payload: dict) -> None:
    """Best-effort write of the last-good resolved-OOB to disk so docker-
    api restarts don't wipe the cache. Never raises into the caller."""
    try:
        path = _LAST_GOOD_RESOLVED_OOB_PATH
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            _json.dump(payload, f, default=str)
    except Exception as e:
        log.warning("last-good OOB disk persist failed: %s", e)


async def get_red_oob_resolved() -> dict:
    """Resolved RED OOB with last-known-good cache.

    Two layers of caching:
      1. 60s TTL via _tool_cache_get/_tool_cache_put — same as other
         expensive cougaar queries.
      2. Process-wide _LAST_GOOD_RESOLVED_OOB, persisted to disk —
         when cougaar throws (TransportConnectionFailed / timeout / etc),
         the last successful payload is returned with `_stale=True` so
         downstream consumers can choose to flag staleness in UI but
         keep showing the units.

    The 60s short cache is hit first; misses fall through to the impl.
    If impl raises, the last-good cache fills in. Result is ALWAYS
    written to both caches when fresh data lands."""
    global _LAST_GOOD_RESOLVED_OOB
    cached = _tool_cache_get(("get_red_oob_resolved",))
    if cached is not None:
        return cached
    try:
        result = await _get_red_oob_resolved_impl()
    except Exception as e:
        if _LAST_GOOD_RESOLVED_OOB is not None:
            log.warning(
                "get_red_oob_resolved: cougaar unreachable (%s) — "
                "serving last-good cache (snapshot_ts_ms=%s, %d named units)",
                e,
                _LAST_GOOD_RESOLVED_OOB.get("snapshot_ts_ms"),
                len(_LAST_GOOD_RESOLVED_OOB.get("named_units") or []),
            )
            # Don't put stale data into the 60s TTL cache — let the next
            # call retry cougaar live. Just return the stale snapshot.
            stale = dict(_LAST_GOOD_RESOLVED_OOB)
            stale["_stale"] = True
            stale["_stale_reason"] = f"cougaar unreachable: {type(e).__name__}: {e}"
            return stale
        raise
    _tool_cache_put(("get_red_oob_resolved",), result)
    _LAST_GOOD_RESOLVED_OOB = result
    _persist_last_good_resolved_oob(result)
    return result


async def _get_red_oob_resolved_impl() -> dict:
    """Resolved RED OOB — cougaar source-of-truth + pytho-side fusion.

    Returns:
        {
          "named_units": [
            {
              "kg_id":              "<IDXRedUnit kbID>",
              "name":               "6063 AD CO",
              "honorific_name":     "...",        # data__honorificName (null on live IDX-26)
              "unit_designator":    "...",        # data__unitDesignator (null on live IDX-26)
              "echelon":            "CO",
              "parent_unit":        "606 AD BDE",  # display label (null on live IDX-26)
              "parent_echelon":     "BDE",
              "parent_kg_id":       "<parent IDXRedUnit kbID>",  # data__unitSuperiorRefID -> data__ID -> kg_id; the live subordinate-tree join key
              "rate_of_march_kmh":  35,
              "primary_position":   {"lat": ..., "lon": ..., "last_seen": "ISO8601"},
              "attributed_positions": [
                {"lat": ..., "lon": ..., "n_observations": N,
                 "composition": {<eq>:cnt}, "confidence": "HIGH"|"MED",
                 "score": 0.354, "stub_kbids": [...]},
                ...
              ],
              "composition_rollup":           {<eq_name>: count, ...},
              "composition_by_fm360_category":{<category>: count, ...},
              "hvt_worthy_count":             N,
              "disposition":                  "...",
              "current_activity":             "...",
              "strength_pct_manned":          "...",
              "sidc":                         "...",
            },
            ...
          ],
          "unidentified_formations": [
            {"centroid_lat": ..., "centroid_lon": ..., "inferred_echelon": "BN",
             "n_observations": N, "composition": {...}, "hvt_worthy_count": N,
             "top_candidates": [{"name": "...", "score": 0.04}, ...],
             "collection_recommendation": "..."}
          ],
          "snapshot_ts_ms":   <epoch_ms>,
          "snapshot_path":    "<file path for GraphQL viewer>",
          "raw_summary": {
              "n_red_units":       2760,
              "n_named_units":     233,
              "n_unknown_stubs":   2527,
              "n_red_equipment":   83131,
              "n_equipment_edges": 95483,
          },
          "fusion_stats": {
              "n_orphan_clusters_total":          34,
              "n_orphan_clusters_high_conf":      20,
              "n_orphan_clusters_med_conf":       14,
              "n_orphan_clusters_unidentified":   0,
              "n_attributed_sub_positions":       N,
          },
        }
    """
    t0 = time.monotonic()
    async with _session() as s:
        units_rows, equipment_rows, edge_rows = await asyncio.gather(
            _execute_list(s, _IDX_RED_UNITS_ALL),
            _execute_list(s, _RED_EQUIPMENT_ALL),
            _execute_list(s, _EQUIPMENT_REDUNIT_EDGES),
        )

    # ---- normalize unit rows ----
    # Pre-pass: data__ID -> kbID. The subordinate hierarchy rides in on
    # data__unitSuperiorRefID (an integer ref to the parent's data__ID) —
    # the ONLY parent linkage cougaar populates on live IDX-26. The inline
    # parent fields (parentHonorificName / parentUnitDesignator / etc.) come
    # back null, so we resolve the superior ref to the parent's kg_id here,
    # the same way equipment rides in on the Equipment_RedUnit edge rather
    # than the (null) inline data__composition.
    id_to_kgid: dict = {}
    for r in units_rows or []:
        rid = r.get("data__ID")
        kb = r.get("kbID")
        if rid is not None and kb:
            id_to_kgid[rid] = kb
    units_by_kbid: dict[str, dict] = {}
    for r in units_rows or []:
        kb = r.get("kbID")
        if not kb:
            continue
        loc = r.get("data__location") or {}
        last_upd = (r.get("data__lastUpdated") or {}).get("instant") if r.get("data__lastUpdated") else None
        units_by_kbid[kb] = {
            "kg_id":             kb,
            "name":              r.get("data__name"),
            "echelon":           r.get("data__echelon"),
            "parent_unit":       r.get("data__parentHonorificName") or r.get("data__parentUnitDesignator"),
            "parent_echelon":    r.get("data__parentEchelon"),
            "parent_kg_id":      id_to_kgid.get(r.get("data__unitSuperiorRefID")),
            "lat":               loc.get("latitude"),
            "lon":               loc.get("longitude"),
            "last_seen":         last_upd,
            "disposition":       r.get("data__disposition"),
            "current_activity":  r.get("data__currentActivity"),
            "strength_pct":      r.get("data__strengthPctManned"),
            "sidc":              r.get("data__symbolicIdentifier"),
            "unit_designator":   r.get("data__unitDesignator"),
            "honorific_name":    r.get("data__honorificName"),
        }

    # ---- normalize equipment rows ----
    eq_by_kbid: dict[str, dict] = {}
    for r in equipment_rows or []:
        kb = r.get("kbID")
        if not kb:
            continue
        force = (r.get("data__forceType") or {}).get("literal") if r.get("data__forceType") else None
        if force != "RED_FORCE":
            continue
        loc = r.get("data__location") or {}
        last_upd = (r.get("data__locationLastUpdate") or {}).get("instant") if r.get("data__locationLastUpdate") else None
        eq_by_kbid[kb] = {
            "kg_id":      kb,
            "name":       r.get("data__name") or r.get("data__nomenclature") or r.get("data__description"),
            "lat":        loc.get("latitude"),
            "lon":        loc.get("longitude"),
            "last_seen":  last_upd,
        }

    # ---- build per-unit composition (via Equipment_RedUnit edges) ----
    unit_eq_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in edge_rows or []:
        src = (e.get("source") or {}).get("kbID")
        tgt = (e.get("target") or {}).get("kbID")
        if not (src and tgt):
            continue
        eq = eq_by_kbid.get(src)
        if not eq:
            continue
        nm = eq["name"]
        if not nm:
            continue
        unit_eq_counts[tgt][nm] += 1

    # ---- TF-IDF weights over equipment ----
    n_units_with_eq = sum(1 for c in unit_eq_counts.values() if c)
    eq_df: dict[str, int] = defaultdict(int)
    for c in unit_eq_counts.values():
        for nm in c:
            eq_df[nm] += 1
    idf = {
        nm: _math.log(max(2, n_units_with_eq) / (1 + df))
        for nm, df in eq_df.items()
    }

    # ---- separate named units from Unknown stubs ----
    UNKNOWN_NAME = "Unknown Red Unit"
    named: list[dict] = []
    stubs: list[dict] = []
    for kb, u in units_by_kbid.items():
        u_ext = dict(u)
        u_ext["comp"] = dict(unit_eq_counts.get(kb, {}))
        if u_ext["name"] == UNKNOWN_NAME:
            stubs.append(u_ext)
        else:
            named.append(u_ext)

    # ---- compute per-named-unit rate-of-march from composition ----
    for nu in named:
        nu["rate_of_march_kmh"] = _derive_unit_rate(nu["comp"])

    # ---- cluster Unknown stubs with location + HVT-worthy equipment ----
    cluster_points = [
        s for s in stubs
        if s.get("lat") is not None and s.get("lon") is not None
        and any(_is_hvt_worthy(nm) for nm in s["comp"])
    ]
    cluster_groups = _cluster_unionfind(cluster_points, _CLUSTER_RADIUS_KM)
    clusters: list[dict] = []
    for ck, member_idxs in cluster_groups.items():
        members = [cluster_points[i] for i in member_idxs]
        comp = defaultdict(int)
        latest_ts = None
        for m in members:
            for nm, cnt in m["comp"].items():
                comp[nm] += cnt
            ts = _parse_iso_dt(m.get("last_seen"))
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
        avg_lat = sum(m["lat"] for m in members) / len(members)
        avg_lon = sum(m["lon"] for m in members) / len(members)
        n_hvt = sum(cnt for nm, cnt in comp.items() if _is_hvt_worthy(nm))
        n_cats = len({_fm360_cat(nm) for nm in comp if _fm360_cat(nm) and _is_hvt_worthy(nm)})
        if n_cats >= 4 and n_hvt >= 40:
            inferred_ech = "BDE"
        elif n_cats >= 3 and n_hvt >= 20:
            inferred_ech = "BN"
        elif n_cats >= 2 and n_hvt >= 10:
            inferred_ech = "BN"
        else:
            inferred_ech = "CO"
        clusters.append({
            "members":         members,
            "stub_kbids":      [m["kg_id"] for m in members],
            "n_observations":  len(members),
            "centroid_lat":    avg_lat,
            "centroid_lon":    avg_lon,
            "composition":     dict(comp),
            "last_seen":       latest_ts,
            "inferred_echelon": inferred_ech,
            "hvt_worthy_count": n_hvt,
        })

    # ---- score each cluster against every named unit ----
    def score_pair(cluster: dict, nu: dict) -> dict:
        if nu.get("lat") is None or nu.get("lon") is None:
            return {"score": 0.0}
        d_km = _haversine_km(cluster["centroid_lat"], cluster["centroid_lon"],
                              nu["lat"], nu["lon"])
        nu_ts = _parse_iso_dt(nu.get("last_seen"))
        dt_h = abs((cluster["last_seen"] - nu_ts).total_seconds()) / 3600.0 \
            if (cluster["last_seen"] and nu_ts) else _DEFAULT_DT_HOURS
        rate = nu.get("rate_of_march_kmh") or 25
        budget = max(0.5, rate * _CROSS_COUNTRY_FACTOR * dt_h)
        feas = _math.exp(-max(0, d_km - budget) / max(2.0, budget))
        sim = _cosine_tfidf(cluster["composition"], nu["comp"], idf)
        u_ord = _ech_ord(nu.get("echelon"))
        c_ord = _ech_ord(cluster["inferred_echelon"])
        delta = abs(u_ord - c_ord)
        ech = 1.0 if delta == 0 else (0.6 if delta == 1 else (0.3 if delta == 2 else 0.1))
        disposition = (nu.get("disposition") or "").upper()
        act = 0.0 if disposition in ("DESTROYED", "DEFEATED", "KIA") else 1.0
        try:
            strength = int(str(nu.get("strength_pct") or "100").rstrip("%"))
            if strength < 25:
                act *= 0.5
        except (TypeError, ValueError):
            pass
        return {
            "score": sim * feas * ech * act,
            "comp_sim": sim,
            "d_km": d_km,
            "budget_km": budget,
            "feasibility": feas,
            "ech_score": ech,
            "act_factor": act,
            "dt_h": dt_h,
        }

    # ---- attribute each cluster ----
    cluster_attributions: list[dict] = []  # (cluster, best_nu, score_components, confidence)
    for cl in clusters:
        scored = []
        for nu in named:
            sc = score_pair(cl, nu)
            if sc["score"] > 0.001:
                scored.append((sc, nu))
        scored.sort(key=lambda x: -x[0]["score"])
        if not scored:
            cluster_attributions.append((cl, None, None, "UNIDENTIFIED"))
            continue
        best, best_nu = scored[0]
        second = scored[1] if len(scored) > 1 else (None, None)
        margin = best["score"] - (second[0]["score"] if second[0] else 0)
        if best["score"] >= _CONF_HIGH_SCORE and margin >= _CONF_HIGH_MARGIN:
            conf = "HIGH"
        elif best["score"] >= _CONF_MED_SCORE:
            conf = "MED"
        else:
            conf = "LOW"
        cluster_attributions.append((cl, best_nu, best, conf, scored[:5]))

    # ---- build named_units output with attributed_positions ----
    nu_by_kbid: dict[str, dict] = {}
    for nu in named:
        # base composition rollup is the unit's direct edges
        comp_by_cat: dict[str, int] = defaultdict(int)
        for nm, cnt in nu["comp"].items():
            cat = _fm360_cat(nm)
            if cat:
                comp_by_cat[cat] += cnt
        hvt_count = sum(c for nm, c in nu["comp"].items() if _is_hvt_worthy(nm))
        out = {
            "kg_id":              nu["kg_id"],
            "name":               nu["name"],
            "honorific_name":     nu.get("honorific_name"),
            "unit_designator":    nu.get("unit_designator"),
            "echelon":            nu["echelon"],
            "parent_unit":        nu.get("parent_unit"),
            "parent_echelon":    nu.get("parent_echelon"),
            "parent_kg_id":       nu.get("parent_kg_id"),
            "rate_of_march_kmh":  nu.get("rate_of_march_kmh"),
            "sidc":               nu.get("sidc"),
            "disposition":        nu.get("disposition"),
            "current_activity":   nu.get("current_activity"),
            "strength_pct_manned": nu.get("strength_pct"),
            "primary_position":   {
                "lat": nu.get("lat"),
                "lon": nu.get("lon"),
                "last_seen": nu.get("last_seen"),
            },
            "attributed_positions": [],
            "composition_rollup":         dict(nu["comp"]),
            "composition_by_fm360_category": dict(comp_by_cat),
            "hvt_worthy_count":           hvt_count,
        }
        nu_by_kbid[nu["kg_id"]] = out

    n_attrib = 0
    unidentified: list[dict] = []
    for attr in cluster_attributions:
        cl = attr[0]
        nu = attr[1]
        sc = attr[2]
        conf = attr[3]
        if conf in ("HIGH", "MED") and nu is not None:
            # Emit one attributed_position PER cluster member instead of one
            # per cluster centroid. Live cougaar IDXRedUnit observations for
            # subordinate units (BDE under DIV, BN under BDE) carry their
            # OWN lat/lon; collapsing them into the cluster centroid threw
            # those positions away, leaving the analyst unable to right-
            # click any individual subordinate on the live map (the parent
            # echelon's marker stole every hit). Per-member emit preserves
            # each subordinate's exact reported location AND lets each
            # render as its own clickable mil_unit downstream (see
            # _cougaar_to_mil_units sub-position emit at this file ~L942).
            # Cluster context still rides along on every member
            # (cluster_centroid_*, cluster_n_observations) so renderers can
            # group at low zoom if they want.
            cl_last_seen_iso = cl["last_seen"].isoformat() if cl["last_seen"] else None
            for member in cl["members"]:
                n_attrib += 1
                nu_by_kbid[nu["kg_id"]]["attributed_positions"].append({
                    "lat":              member.get("lat"),
                    "lon":              member.get("lon"),
                    "stub_kbid":        member.get("kg_id"),
                    "composition":      dict(member.get("comp") or {}),
                    "last_seen":        member.get("last_seen"),
                    "confidence":       conf,
                    "score":            round(sc["score"], 4),
                    "comp_sim":         round(sc["comp_sim"], 4),
                    "distance_km":      round(sc["d_km"], 2),
                    "movement_budget_km": round(sc["budget_km"], 2),
                    "inferred_echelon": cl["inferred_echelon"],
                    "cluster_centroid_lat":    cl["centroid_lat"],
                    "cluster_centroid_lon":    cl["centroid_lon"],
                    "cluster_n_observations":  cl["n_observations"],
                    "cluster_last_seen":       cl_last_seen_iso,
                    "cluster_stub_kbids":      cl["stub_kbids"],
                })
        else:
            top5 = attr[4] if len(attr) > 4 else []
            unidentified.append({
                "centroid_lat":    cl["centroid_lat"],
                "centroid_lon":    cl["centroid_lon"],
                "n_observations":  cl["n_observations"],
                "stub_kbids":      cl["stub_kbids"],
                "composition":     cl["composition"],
                "inferred_echelon": cl["inferred_echelon"],
                "hvt_worthy_count": cl["hvt_worthy_count"],
                "last_seen":       cl["last_seen"].isoformat() if cl["last_seen"] else None,
                "top_candidates":  [
                    {"name": n["name"], "score": round(s["score"], 4)}
                    for s, n in top5
                ],
                "collection_recommendation": (
                    f"Request HUMINT/SIGINT to identify formation near "
                    f"({cl['centroid_lat']:.4f}, {cl['centroid_lon']:.4f}) — "
                    f"observed {cl['hvt_worthy_count']} HVT-worthy items "
                    f"({cl['inferred_echelon']}-class composition)."
                ),
            })

    # ---- doctrinal-frontage role mapping for null-position units ----
    # Per FM 3-21.20 / ATP 2-01.3 / OPFOR templated frontages: BN
    # subordinates of a BDE deploy in known patterns relative to the
    # BDE FLOT — RECON forward of FLOT, maneuver BNs on the FLOT
    # laterally spread, FA behind, ADA near HQ, CSS in rear. Offsets
    # in (forward_km, lateral_spread_km, depth_km) relative to the
    # parent BDE centroid, where forward is along axis of advance
    # (270° west for IDX 26 OPFOR; env BDA_AXIS_AZIMUTH overrides).
    _DOCTRINAL_OFFSETS_KM = {
        # role:           (forward, lateral_spread, depth)
        "recon":          (12.0, 5.0, 3.0),
        "maneuver":       (3.0, 8.0, 2.0),
        "antitank":       (4.0, 8.0, 2.0),
        "fire_support":   (-4.0, 6.0, 3.0),
        "air_defense":    (-1.0, 4.0, 2.0),
        "engineer":       (1.0, 4.0, 2.0),
        "signals":        (-2.0, 2.0, 1.0),
        "support":        (-6.0, 4.0, 3.0),
        "medical":        (-7.0, 3.0, 2.0),
        "chemical":       (-9.0, 4.0, 2.0),
        "police":         (-5.0, 3.0, 2.0),
        "command":        (0.0, 0.0, 0.0),  # HQ sits on centroid
    }

    def _role_of_unit(u: dict) -> str:
        nm = (u.get("name") or "").upper()
        if "RECON" in nm or "RECCE" in nm or "SCOUT" in nm:
            return "recon"
        if " AT " in f" {nm} " or "ANTI-TANK" in nm or "ANTITANK" in nm:
            return "antitank"
        if "ADA" in nm or "AIR DEF" in nm:
            return "air_defense"
        if " FA " in f" {nm} " or "MRL" in nm or "ARTILLERY" in nm:
            return "fire_support"
        if "ENG" in nm:
            return "engineer"
        if "SIG" in nm:
            return "signals"
        if "MED" in nm:
            return "medical"
        if "CHEM" in nm:
            return "chemical"
        if "POL" in nm or "MP " in f" {nm} ":
            return "police"
        if ("SPT" in nm or "SUPPLY" in nm or "TRANS" in nm
                or "CSS" in nm or "MNT" in nm or "MAINT" in nm):
            return "support"
        if "HQ" in nm or "COMMAND" in nm or "HHC" in nm:
            return "command"
        if ("MTZD" in nm or "MECH" in nm or "INF" in nm or "TANK" in nm
                or "IFV" in nm or "MOTORIZED" in nm):
            return "maneuver"
        return "support"

    _KM_PER_DEG_LAT = 110.574

    # ---- threat avenue-of-approach axis lookup for per-BDE AOA ----
    # Per ATP 2-01.3 / FM 3-21.20: a unit's forward axis follows the
    # avenue of approach it's operating along, not a single global
    # compass bearing. scenario.KG.TERRAIN.avenues_of_approach holds
    # both friendly and threat AAs as central-axis polylines (origin
    # → objective). For OPFOR units we want the THREAT AA tangent
    # at the nearest point — that's the direction of intended attack
    # along that corridor.
    #
    # Fallback chain when AA data is unavailable:
    #   1. Bearing to nearest BLUE concentration (still per-BDE).
    #   2. BDA_AXIS_AZIMUTH env default (270° = west for IDX 26).
    _threat_aa_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    _blue_positions: list[tuple[float, float]] = []
    try:
        from utils import scenario as _scenario
        from utils.coords import mgrs_to_latlon as _mgrs_to_latlon_local
        terrain = (getattr(_scenario, "TERRAIN", None)
                     or _scenario.KG.get("terrain", {})) or {}
        for _aa in (terrain.get("avenues_of_approach") or []):
            if (_aa.get("side") or "").lower() != "threat":
                continue
            poly = _aa.get("central_axis_polyline") or []
            for _i in range(len(poly) - 1):
                p1, p2 = poly[_i], poly[_i + 1]
                # polyline entries are [lon, lat]
                if (isinstance(p1, list) and isinstance(p2, list)
                        and len(p1) >= 2 and len(p2) >= 2):
                    _threat_aa_segments.append(
                        ((float(p1[1]), float(p1[0])),
                         (float(p2[1]), float(p2[0])))
                    )
        for _bu in (_scenario.KG.get("unit_blue") or []):
            _m = _bu.get("mgrs") or ""
            if _m:
                _ll = _mgrs_to_latlon_local(_m)
                if _ll and len(_ll) == 2:
                    _blue_positions.append((float(_ll[0]), float(_ll[1])))
    except Exception:
        pass

    import os as _os_for_aoa
    _DEFAULT_AOA_DEG = float(_os_for_aoa.environ.get("BDA_AXIS_AZIMUTH", "270"))

    def _seg_midpoint_dist2(seg, lat, lon):
        # Squared (lat,lon) distance from query point to midpoint of
        # segment. Cheap; segment is short relative to AO scale so
        # midpoint is a good "nearest" approximation.
        (a_lat, a_lon), (b_lat, b_lon) = seg
        m_lat = 0.5 * (a_lat + b_lat)
        m_lon = 0.5 * (a_lon + b_lon)
        return (m_lat - lat) ** 2 + (m_lon - lon) ** 2

    def _seg_tangent_bearing(seg) -> float:
        # Compass bearing of segment direction (origin → objective).
        (a_lat, a_lon), (b_lat, b_lon) = seg
        dN = b_lat - a_lat
        dE = b_lon - a_lon
        rad = _math.atan2(dE, dN)
        return (_math.degrees(rad) + 360.0) % 360.0

    def _bearing_for_unit(lat: float, lon: float) -> float:
        """Per-BDE forward axis. Tier 1: nearest THREAT-AA polyline
        segment tangent (the doctrinal corridor the unit's astride).
        Tier 2: bearing toward nearest BLUE concentration. Tier 3:
        BDA_AXIS_AZIMUTH default."""
        if _threat_aa_segments:
            best = min(_threat_aa_segments,
                          key=lambda s: _seg_midpoint_dist2(s, lat, lon))
            return _seg_tangent_bearing(best)
        if _blue_positions:
            best = min(_blue_positions,
                          key=lambda p: (p[0] - lat) ** 2 + (p[1] - lon) ** 2)
            dN = best[0] - lat
            dE = best[1] - lon
            return (_math.degrees(_math.atan2(dE, dN)) + 360.0) % 360.0
        return _DEFAULT_AOA_DEG

    # Deterministic per-kbid jitter so same-role siblings under one BDE
    # don't form perfect straight lines on the rendered map. ±2km
    # depth, ±1km lateral, seeded by the BN's kbid hash so the position
    # is stable across refreshes.
    import hashlib as _hashlib
    def _kbid_jitter_km(kbid: str) -> tuple[float, float]:
        h = _hashlib.md5(kbid.encode("utf-8")).digest()
        # Unpack first 4 bytes as two int16; map to [-1, 1].
        a = int.from_bytes(h[0:2], "big", signed=True) / 32768.0
        b = int.from_bytes(h[2:4], "big", signed=True) / 32768.0
        return a * 2.0, b * 1.0  # ±2km along-axis, ±1km lateral jitter

    def _frontage_to_latlon(parent_lat: float, parent_lon: float,
                              forward_km: float, lateral_km: float,
                              aoa_deg: float | None = None
                              ) -> tuple[float, float]:
        # Local tangent plane:
        #   forward vector  = (Δ_north, Δ_east) = (cos ψ, sin ψ)
        #   lateral vector  = right of forward  = (-sin ψ, cos ψ)
        # km → deg using cos(lat) for longitude shrink.
        ψ = _math.radians(aoa_deg if aoa_deg is not None else _DEFAULT_AOA_DEG)
        _sa, _ca = _math.sin(ψ), _math.cos(ψ)
        dN_km = forward_km * _ca + lateral_km * (-_sa)
        dE_km = forward_km * _sa + lateral_km * (_ca)
        dlat = dN_km / _KM_PER_DEG_LAT
        km_per_deg_lon = _KM_PER_DEG_LAT * max(
            0.01, _math.cos(_math.radians(parent_lat))
        )
        dlon = dE_km / km_per_deg_lon
        return parent_lat + dlat, parent_lon + dlon

    # ---- position derivation for units with no raw primary_position ----
    # Cougaar IDXRedUnit ships ~179 of 225 BNs/CO with null data__location
    # in IDX 26 (verified 2026-06-10 night against live cougaar; the
    # equipment-fusion path was also dry — all equipment items linked to
    # those BNs ALSO have null data__location). Parent BDE is the only
    # positional signal cougaar carries. Right behavior: derive via
    # doctrinal frontage offset from the parent BDE's centroid, in this
    # order:
    #   1. Best attributed_position (HIGH-confidence cluster member).
    #   2. Average of subordinate units' primary positions (BDE rolls
    #      up to centroid of its known BNs).
    #   3. Doctrinal-frontage offset from parent's primary position
    #      using _DOCTRINAL_OFFSETS_KM[role] and AOA azimuth (replaces
    #      the earlier "co-locate on parent" fallback that produced
    #      visible rings the user — correctly — called doctrinally
    #      wrong).
    # Mark the source in `position_source` so the analyst panel /
    # collection-recommendation engine can flag derived positions and
    # request HUMINT/IIR confirmation. Units that still have no
    # position after this pass keep primary_position with null
    # lat/lon — the live-map emit drops them with a structured
    # `position_missing: true` flag rather than painting them at (0,0).
    def _is_pos_set(u: dict) -> bool:
        pp = u.get("primary_position") or {}
        la, lo = pp.get("lat"), pp.get("lon")
        if not (isinstance(la, (int, float)) and isinstance(lo, (int, float))):
            return False
        # Treat sub-meter-of-zero as null (cougaar placeholder).
        return not (abs(la) < 1e-3 and abs(lo) < 1e-3)

    # Initialize all units' position_source from their raw input.
    for kbid, u in nu_by_kbid.items():
        u["position_source"] = "raw" if _is_pos_set(u) else None

    # Pre-build the children index (parent_kg_id -> [child_kbid, ...]).
    children_of: dict[str, list[str]] = {}
    for kbid, u in nu_by_kbid.items():
        pkg = u.get("parent_kg_id")
        if pkg:
            children_of.setdefault(pkg, []).append(kbid)

    def _avg_children_pos(kbid: str) -> tuple[float | None, float | None]:
        kids = children_of.get(kbid) or []
        lats, lons = [], []
        for cid in kids:
            cu = nu_by_kbid.get(cid)
            if cu and _is_pos_set(cu):
                pp = cu["primary_position"]
                lats.append(float(pp["lat"]))
                lons.append(float(pp["lon"]))
        if lats:
            return sum(lats) / len(lats), sum(lons) / len(lons)
        return None, None

    # Multi-pass: each iteration may unlock new derivations (a BN
    # whose BDE got a parent-derived position from a sibling BN this
    # pass can use that next pass). Cap at 5 passes — the OOB tree is
    # ≤ 5 deep (Corps > Div > Bde > Bn > Co/Btry).
    for _pass in range(5):
        changed = 0
        for kbid, u in nu_by_kbid.items():
            if _is_pos_set(u):
                continue
            # Tier 1: best attributed_position.
            best_ap = None
            for ap in (u.get("attributed_positions") or []):
                la, lo = ap.get("lat"), ap.get("lon")
                if not (isinstance(la, (int, float))
                          and isinstance(lo, (int, float))):
                    continue
                if abs(la) < 1e-3 and abs(lo) < 1e-3:
                    continue
                if (best_ap is None
                        or ap.get("confidence") == "HIGH"
                        or (ap.get("score") or 0) > (best_ap.get("score") or 0)):
                    best_ap = ap
            if best_ap is not None:
                u["primary_position"] = {
                    "lat": float(best_ap["lat"]),
                    "lon": float(best_ap["lon"]),
                    "last_seen": best_ap.get("last_seen") or best_ap.get("cluster_last_seen"),
                }
                u["position_source"] = "attributed"
                changed += 1
                continue
            # Tier 2: average of subordinate units' positions.
            avg_la, avg_lo = _avg_children_pos(kbid)
            if avg_la is not None:
                u["primary_position"] = {
                    "lat": avg_la, "lon": avg_lo, "last_seen": None,
                }
                u["position_source"] = "children_avg"
                changed += 1
                continue
            # Tier 3: doctrinal-frontage offset from parent BDE.
            #   - Pick role from the unit name (RECON/FA/ADA/etc.).
            #   - Look up (forward_km, lateral_spread_km, _) from
            #     _DOCTRINAL_OFFSETS_KM.
            #   - Lateral index = sort-position of this unit among its
            #     SAME-ROLE siblings under the same BDE (deterministic
            #     by kbid). Spread the same-role siblings evenly across
            #     [-lateral_spread, +lateral_spread] km so multiple
            #     FA BNs / multiple INF BNs don't co-locate either.
            pkg = u.get("parent_kg_id")
            if pkg:
                pu = nu_by_kbid.get(pkg)
                if pu and _is_pos_set(pu):
                    pp = pu["primary_position"]
                    role = _role_of_unit(u)
                    fwd, lat_spread, _depth = _DOCTRINAL_OFFSETS_KM.get(
                        role, _DOCTRINAL_OFFSETS_KM["support"]
                    )
                    # Same-role siblings under this parent (stable order).
                    siblings = sorted(
                        [s for s in (children_of.get(pkg) or [])
                          if _role_of_unit(nu_by_kbid.get(s) or {}) == role],
                        key=lambda x: x,
                    )
                    n_sib = max(1, len(siblings))
                    try:
                        ix = siblings.index(kbid)
                    except ValueError:
                        ix = 0
                    # Evenly spread across [-lat_spread, +lat_spread]:
                    # ix=0 → -spread, ix=n-1 → +spread. For n=1 → 0.
                    if n_sib > 1:
                        lateral_km = (-lat_spread
                                       + (2 * lat_spread) * (ix / (n_sib - 1)))
                    else:
                        lateral_km = 0.0
                    # Per-parent-BDE AOA: tangent of the nearest THREAT
                    # avenue-of-approach polyline at the parent's
                    # position — the doctrinal corridor this BDE is
                    # operating along. Falls through to nearest-BLUE
                    # bearing, then to BDA_AXIS_AZIMUTH default.
                    aoa_deg = _bearing_for_unit(pp["lat"], pp["lon"])
                    # Deterministic per-kbid jitter — breaks up the
                    # straight lateral lines same-role siblings would
                    # otherwise form. ±2 km along-axis, ±1 km lateral.
                    jdepth, jlat = _kbid_jitter_km(kbid)
                    new_lat, new_lon = _frontage_to_latlon(
                        pp["lat"], pp["lon"],
                        fwd + jdepth, lateral_km + jlat,
                        aoa_deg=aoa_deg,
                    )
                    u["primary_position"] = {
                        "lat": new_lat, "lon": new_lon,
                        "last_seen": pp.get("last_seen"),
                    }
                    u["position_source"] = (
                        f"frontage:{role}@{int(round(aoa_deg))}deg"
                    )
                    changed += 1
                    continue
        if changed == 0:
            break

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # ---- raw summary for the GraphQL viewer ----
    raw_summary = {
        "n_red_units":       len(units_by_kbid),
        "n_named_units":     len(named),
        "n_unknown_stubs":   len(stubs),
        "n_red_equipment":   len(eq_by_kbid),
        "n_equipment_edges": len(edge_rows or []),
        "elapsed_ms":        elapsed_ms,
    }
    fusion_stats = {
        "n_orphan_clusters_total":        len(clusters),
        "n_orphan_clusters_high_conf":    sum(1 for a in cluster_attributions if a[3] == "HIGH"),
        "n_orphan_clusters_med_conf":     sum(1 for a in cluster_attributions if a[3] == "MED"),
        "n_orphan_clusters_unidentified": len(unidentified),
        "n_attributed_sub_positions":     n_attrib,
    }

    payload = {
        "named_units":            list(nu_by_kbid.values()),
        "unidentified_formations": unidentified,
        "snapshot_ts_ms":         int(time.time() * 1000),
        "raw_summary":            raw_summary,
        "fusion_stats":           fusion_stats,
    }

    snapshot_path = _write_oob_snapshot(payload, {
        # Don't dump the full equipment list (~92k records) into every snapshot;
        # the analyst can re-query cougaar directly via the GraphQL viewer for
        # full bodies. Keep counts + a sample of each vertex type.
        "idx_red_unit_count":          len(units_rows or []),
        "idx_red_unit_sample":         (units_rows or [])[:3],
        "equipment_count":             len(equipment_rows or []),
        "equipment_sample":            (equipment_rows or [])[:3],
        "equipment_redunit_edge_count": len(edge_rows or []),
        "equipment_redunit_edge_sample": (edge_rows or [])[:3],
    })
    payload["snapshot_path"] = snapshot_path

    log.info(
        "get_red_oob_resolved: %d named units, %d Unknown stubs, "
        "%d orphan clusters (%d HIGH + %d MED + %d unidentified), "
        "%d sub-positions attributed in %dms",
        len(named), len(stubs), len(clusters),
        fusion_stats["n_orphan_clusters_high_conf"],
        fusion_stats["n_orphan_clusters_med_conf"],
        fusion_stats["n_orphan_clusters_unidentified"],
        n_attrib, elapsed_ms,
    )
    return payload


async def get_spatial_overlay() -> dict:
    """SpatialControl items filtered to the doctrinal actionType set.

    Cached (30 s TTL) — see _TOOL_CACHE.
    """
    cached = _tool_cache_get(("get_spatial_overlay",))
    if cached is not None:
        return cached
    async with _session() as s:
        rows = await _execute_list(s, _SPATIAL_CONTROLS_ALL)
    result = {"control_measures": _transform(rows)}
    _tool_cache_put(("get_spatial_overlay",), result)
    return result


async def get_target_set(since_iso: str | None = None) -> dict:
    """IMINT-identified targets, fused by BE number.

    Source: IDXImageIirItem (target identity) + IDXImageIirReport (acquisition
    metadata). Within-INT dedup groups items by basicEncyclopediaNumber. The
    `since_iso` parameter filters underlying reports by reportTime client-side.

    Cached (30 s TTL, keyed by since_iso) — see _TOOL_CACHE.
    """
    cached = _tool_cache_get(("get_target_set", since_iso))
    if cached is not None:
        return cached
    async with _session() as s:
        items_rows, reports_rows = await asyncio.gather(
            _execute_list(s, _IMINT_ITEMS_ALL),
            _execute_list(s, _IMINT_REPORTS_ALL),
        )
    items = _transform(items_rows)
    reports = _filter_since(_transform(reports_rows), "report_time", since_iso)
    fused = _fuse_imint_items(items, reports)
    result = {"targets": [f.model_dump() for f in fused]}
    _tool_cache_put(("get_target_set", since_iso), result)
    return result


async def get_target_by_id(target_uuid: str) -> dict:
    """Single-target lookup by kbID. Resolves to a FusedObservation if the uuid
    points to an IDXImageIirItem; returns the raw transformed record otherwise.
    """
    async with _session() as s:
        response = await _execute(s, _vertex_by_kbid_query(target_uuid))
    vertex = response.get("getVertexByKbID")
    if not vertex:
        return {"target": None}

    object_type = vertex.get("objectType")
    if object_type == "World.Informations.IDXImageIirItem":
        item = IDXImageIirItem.model_validate(vertex).model_dump()
        obs = FusedObservation(
            observation_id=f"imint:{item.get('be_number') or item['uuid']}",
            name=item.get("target_name"),
            source_int="IMINT",
            source_kbids=[item["uuid"]],
            entity_key=item.get("be_number"),
            entity_name=item.get("target_name"),
            raw_records=[item],
        )
        return {"target": obs.model_dump()}

    if object_type in _MODELS:
        return {"target": _MODELS[object_type].model_validate(vertex).model_dump()}

    return {"target": None}


# ---------------------------------------------------------------------------
# Recent-intel cache + 30s poller
# ---------------------------------------------------------------------------
#
# Process-wide cache fronting get_recent_intel(). One asyncio background task
# (started from main.py FastAPI startup via start_recent_intel_poller())
# refreshes the cache every _POLLER_INTERVAL_SEC seconds. get_recent_intel()
# now reads from the cache instead of hitting Cougaar live; if the cache is
# empty on first call (poller hasn't completed its first pass yet) we do a
# synchronous fetch + populate. The since_iso filter still applies client-side
# over the cached observations.
#
# The poller exposes its delta-detection state via _LAST_SEEN_KBIDS so the
# KLIEGLIGHT auto-fire trigger (handlers.klieglight_autofire) can see which
# kbIDs are newly-arrived this poll cycle. Each poll, the trigger callback
# (registered via register_poll_callback) receives the new-kbID set + the raw
# observations and decides what to do — the cache module knows nothing about
# product-firing policy.
#
# No graceful error wrapping: poll failures are logged at WARNING with full
# traceback. The poller does NOT catch-and-continue silently — the next
# scheduled tick simply re-attempts. If the underlying transport stays dead,
# every poll surfaces it in the logs.

_POLLER_INTERVAL_SEC = 60.0
_CACHE_TTL_SEC = 120.0

# Cache holds the result of _fetch_recent_intel_raw() — a dict with
# "observations" (list of FusedObservation .model_dump()), "fetched_at"
# (monotonic timestamp), and "raw_by_kbid" (kbID -> raw record dict so the
# auto-fire trigger can identify the specific SIGINT/ELINT record without a
# second Cougaar round-trip).
_RECENT_INTEL_CACHE: dict | None = None
_LAST_SEEN_KBIDS: set[str] = set()
_POLLER_TASK: asyncio.Task | None = None
_POLL_CALLBACKS: list = []  # list of async callables: (new_kbids, raw_by_kbid, observations) -> None
_CACHE_LOCK_OWNER_LOOP_ID: int | None = None  # id(loop) that owns _CACHE_LOCK
_CACHE_LOCK: asyncio.Lock | None = None


def _cache_lock_for_loop() -> asyncio.Lock:
    """Return the asyncio.Lock pinned to the current running loop. Lazily
    rebuilds if the running loop changed (e.g. tests use a fresh loop)."""
    global _CACHE_LOCK, _CACHE_LOCK_OWNER_LOOP_ID
    loop = asyncio.get_running_loop()
    if _CACHE_LOCK is None or _CACHE_LOCK_OWNER_LOOP_ID != id(loop):
        _CACHE_LOCK = asyncio.Lock()
        _CACHE_LOCK_OWNER_LOOP_ID = id(loop)
    return _CACHE_LOCK


def register_poll_callback(cb) -> None:
    """Register an async callback fired after every successful poll.

    Callback signature:
        async def cb(new_kbids: set[str], raw_by_kbid: dict[str, dict],
                     observations: list[dict]) -> None

    `new_kbids` is the set difference between this poll's kbID set and the
    previous poll's. On the very first poll EVERY kbID is "new" — callers
    that care about that distinction can check _LAST_SEEN_KBIDS before
    registering, or skip the first invocation by checking
    cb.firstcall == True (caller's responsibility).

    Idempotent: registering the same callable twice is harmless — the
    poller iterates the list, so duplicates fire twice. Caller's choice.
    """
    _POLL_CALLBACKS.append(cb)


async def _fetch_recent_intel_raw() -> dict:
    """One uncached fetch + transform pass. Returns a dict with:
        observations: list of FusedObservation .model_dump() — the public
                      shape get_recent_intel() returns.
        raw_by_kbid:  dict mapping kbID to the raw transformed record dict.
                      Lets the auto-fire trigger identify the specific
                      SIGINT/ELINT record that drives a KLIEGLIGHT.
        all_kbids:    set of every kbID seen this pass. Used for delta
                      detection in the poll loop.
    """
    async with _session() as s:
        (
            imint_items, imint_reports,
            sigint, elint, wrangler, oob, tactical, humint,
            sensor_contact, esa, comms, imagery_reports, reccexrep,
            signal_loc_map,
        ) = await asyncio.gather(
            _execute_list(s, _IMINT_ITEMS_ALL),
            _execute_list(s, _IMINT_REPORTS_ALL),
            _execute_list(s, _SIGINT_ALL),
            _execute_list(s, _ELINT_ALL),
            _execute_list(s, _WRANGLER_ALL),
            _execute_list(s, _OOB_REPORTS_ALL),
            _execute_list(s, _TACTICAL_REPORTS_ALL),
            _execute_list(s, _HUMINT_REPORTS_ALL),
            _execute_list(s, _SENSOR_CONTACT_REPORTS_ALL),
            _execute_list(s, _ENEMY_SITUATIONAL_AWARENESS_REPORTS_ALL),
            _execute_list(s, _COMMUNICATIONS_DATA_ALL),
            _execute_list(s, _IMAGERY_REPORTS_ALL),
            _execute_list(s, _RECCEXREP_REPORTS_ALL),
            # Emitter geolocation for the SIGINT source vertices — walked from
            # the AT_*_LOCATION edge family (their own data__location is null).
            _fetch_signal_location_map(s),
        )
        # OSINT vertex is not yet hosted by the live cougaar IDX schema
        # (verified 2026-05-28 via __schema introspection — no type matches
        # osint / opensource / socialmedia / telegram). The query above
        # ships ready for the day cougaar adds it; until then a
        # TransportQueryError on the unknown vertex must NOT take down the
        # rest of the fetch. Run separately + catch defensively. The
        # event_data_fixtures._parse_osint pass below fills the gap from
        # scenario.KG['report'] rows of type=='OSINT'.
        try:
            osint = await _execute_list(s, _OSINT_REPORTS_ALL)
        except Exception as e:
            log.debug(
                "OSINT fetch from cougaar returned an error (expected until "
                "the IDXOpenSourceReport vertex is added live): %s", e,
            )
            osint = []

    imint_item_records = _transform(imint_items)
    imint_report_records = _transform(imint_reports)
    sigint_records = _transform(sigint)
    elint_records = _transform(elint)
    wrangler_records = _transform(wrangler)
    oob_records = _transform(oob)
    tactical_records = _transform(tactical)
    humint_records = _transform(humint)
    sensor_contact_records = _transform(sensor_contact)
    esa_records = _transform(esa)
    comms_records = _transform(comms)
    osint_records = _transform(osint)
    imagery_report_records = _transform(imagery_reports)
    reccexrep_records = _transform(reccexrep)

    # Backfill emitter geolocation onto the SIGINT source records from the
    # cougaar AT_*_LOCATION edges (see _fetch_signal_location_map). Live
    # IDXTacElintReport / IDXWranglerReport vertices ship data__location=null;
    # the coordinate lives on a connected IDXLocation vertex one (WRANGLER) or
    # two (TACELINT) edges away. Populating `location` here makes BOTH the
    # KLIEGLIGHT autofire and the live signal-report map layer plot the emitter
    # at its real position instead of the owning unit's MGRS fallback.
    n_edge_geo = 0
    for r in (*elint_records, *wrangler_records):
        if not r.get("location"):
            pt = signal_loc_map.get(r.get("uuid"))
            if pt:
                r["location"] = pt
                n_edge_geo += 1
    log.info(
        "recent-intel: emitter-location edges resolved geo for %d/%d SIGINT "
        "source records (TacElint+Wrangler)",
        n_edge_geo, len(elint_records) + len(wrangler_records),
    )

    observations: list[FusedObservation] = []
    observations.extend(_fuse_imint_items(imint_item_records, imint_report_records))
    observations.extend(_fuse_sigint(sigint_records))
    observations.extend(_fuse_oob(oob_records))
    observations.extend(_passthrough_fuse(tactical_records, "TACTICAL", "report_time"))
    observations.extend(_passthrough_fuse(elint_records, "ELINT", "reported_time"))
    observations.extend(_passthrough_fuse(humint_records, "HUMINT", "report_time", key_field="report_id"))
    # SPOTREP / SIGACT-class generic observations — feed INTSUM/SITREP/Threat Overlay.
    observations.extend(_passthrough_fuse(sensor_contact_records, "SENSOR_CONTACT", "observation_time", key_field="unit_key"))
    # ENSIT-class enemy situation reports — feed Threat Overlay/SITEMP/Threat Model.
    observations.extend(_passthrough_fuse(esa_records, "ENSIT", "enemy_observation_time", key_field="enemy_unit_master_key"))
    # COMINT comms data — augments SIGSUM. No timestamp/location on this vertex
    # in the public schema, so passthrough uses uuid as the key and no time field.
    observations.extend(_passthrough_fuse(comms_records, "COMINT", time_field=None, key_field="uuid"))
    # OSINT per-event reports — feed OSIR (1:1 per row) + INTSUM/CCA/OED upstream.
    observations.extend(_passthrough_fuse(osint_records, "OSINT", "acquisition_time", key_field="report_id"))

    raw_by_kbid: dict[str, dict] = {}
    # Stamp the source-type onto each raw record so the auto-fire trigger
    # can route IDXSignalOfInterestReport vs IDXTacElintReport without a
    # second lookup. Same kbID never appears in two of these buckets — they
    # are disjoint domain types.
    for r in sigint_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXSignalOfInterestReport", **r}
    for r in elint_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXTacElintReport", **r}
    # WRANGLER → IDXWranglerReport per cougaar sim staff. Stamped with the
    # same _objectType pattern so klieglight_autofire._should_fire admits
    # them under the SIGINT-class gate (once that gate's allow-list
    # includes IDXWranglerReport).
    for r in wrangler_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXWranglerReport", **r}
    for r in tactical_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXTacticalReport", **r}
    for r in oob_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXEnemyOrderOfBattleReport", **r}
    for r in humint_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXHumintReport", **r}
    for r in imint_item_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXImageIirItem", **r}
    for r in imint_report_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXImageIirReport", **r}
    for r in sensor_contact_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXSensorContactReport", **r}
    for r in esa_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXEnemySituationalAwarenessReport", **r}
    for r in comms_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXCommunicationsData", **r}
    for r in osint_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXOpenSourceReport", **r}
    # Imagery reports — GRVTY populates data__imageryUrl with the BDA
    # image file location; downstream BDA / per-target products fetch
    # via utils.url_fetcher.fetch_bytes (protocol-agnostic).
    for r in imagery_report_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXImageryReport", **r}
    # RECCEXREP — BDA assessment grade (target_status) + narrative
    # (gentext) + target reference (ben / item_name / item_number) +
    # mission context (platform / timing). BDA template joins this with
    # IDXImageryReport for the visual evidence.
    for r in reccexrep_records:
        raw_by_kbid[r["uuid"]] = {"_objectType": "IDXReccexrepReport", **r}

    # ---- Augment with rich event_data fixture rows ------------------------
    # Cougaar's live IDX vertices come back sparse (IDXTacElintReport carries
    # only uuid + ref_id + force_threat_designator + is_simulated today). The
    # IBL event_data USMTF dump preserved the rich SIGINT bodies — emitter
    # callsign, ELNOT, frequency, PRF, lat/lon, intercept window — and those
    # landed in scenario.KG['report'] as TACELINT/WRANGLER/EMTREW/TACREP_COMEW
    # entries. Parse those into IDXTacElintReport / IDXSignalOfInterestReport /
    # IDXCommunicationsData-shaped records so the autofire's _should_fire
    # picks them up + the writer sees actual signal data instead of an empty
    # shell. Fixture entries get deterministic synthetic UUIDs so the poller
    # delta logic doesn't re-fire them every tick.
    try:
        from utils import scenario as _scenario  # type: ignore
        from utils.event_data_fixtures import build_fixture_raw_records
        fixture_records = build_fixture_raw_records(_scenario.KG.get("report") or [])
    except Exception as e:
        log.warning("event_data fixture augmentation failed: %s", e, exc_info=True)
        fixture_records = {}
    for kbid, rec in fixture_records.items():
        # Live cougaar takes precedence — if the same kbID happens to exist
        # (shouldn't, fixture UUIDs are in a distinct namespace), don't
        # clobber.
        raw_by_kbid.setdefault(kbid, rec)

    return {
        "observations": [o.model_dump() for o in observations],
        "raw_by_kbid": raw_by_kbid,
        "all_kbids": set(raw_by_kbid.keys()),
    }


async def get_recent_intel(since_iso: str | None = None) -> dict:
    """Multi-INT report stream, within-INT-fused, optionally filtered by time.

    Returns `{"recent_intel": [FusedObservation, ...]}` covering IMINT
    (IDXImageIirReport via items), SIGINT (IDXSignalOfInterestReport), ELINT
    (IDXTacElintReport), OOB (IDXEnemyOrderOfBattleReport with unit refs),
    Tactical (IDXTacticalReport), and HUMINT (IDXHumintReport).

    Cache-backed: reads from the process-wide cache populated by the 30s
    poller (start_recent_intel_poller() at FastAPI startup). If the cache
    is empty (first call before the poller's first pass) does a synchronous
    fetch + populates. The `since_iso` filter still applies client-side
    over the cached observations.

    OOB records without a unitKey (scaffold rows with activity='RECON' and
    null bodies) are dropped — they carry no entity reference and aren't
    real observations.
    """
    global _RECENT_INTEL_CACHE
    lock = _cache_lock_for_loop()
    async with lock:
        now = time.monotonic()
        cache = _RECENT_INTEL_CACHE
        cache_stale = (
            cache is None
            or (now - cache.get("fetched_at", 0.0)) > _CACHE_TTL_SEC
        )
        if cache_stale:
            fresh = await _fetch_recent_intel_raw()
            _RECENT_INTEL_CACHE = {
                **fresh,
                "fetched_at": time.monotonic(),
            }
            cache = _RECENT_INTEL_CACHE

    observations = cache["observations"]
    if since_iso:
        # Filter by the FusedObservation.reported_time field. Same semantic
        # as the previous _filter_since call — but applied to the already-
        # fused output, not the raw per-INT records.
        observations = [o for o in observations
                         if (o.get("reported_time") or "") >= since_iso]
    return {"recent_intel": observations}


async def _poll_loop() -> None:
    """The single background poller task.

    Runs forever (until cancelled by stop_recent_intel_poller). Each tick:
      1. Fetch via _fetch_recent_intel_raw (one Cougaar round-trip).
      2. Update the cache atomically under _CACHE_LOCK.
      3. Compute the delta against _LAST_SEEN_KBIDS.
      4. Fire every registered callback with (new_kbids, raw_by_kbid,
         observations). Callbacks are async and run sequentially so a slow
         callback delays the next one but never the next poll tick — the
         poll tick is fire-then-await-callbacks, then sleep.
      5. Update _LAST_SEEN_KBIDS for the next tick.

    No graceful error wrapping: failures are logged and the poller
    continues to the next tick. The cache is NOT updated on failure, so
    get_recent_intel() callers continue to see the prior good snapshot
    (or hit a fresh fetch themselves if the cache is empty).
    """
    global _RECENT_INTEL_CACHE, _LAST_SEEN_KBIDS
    log.info("recent-intel poller starting (interval=%.1fs)", _POLLER_INTERVAL_SEC)
    while True:
        new_kbids: set[str] = set()
        prior_seen: set[str] = set(_LAST_SEEN_KBIDS)
        raw_by_kbid: dict = {}
        observations: list = []
        fetch_ok = False
        try:
            fresh = await _fetch_recent_intel_raw()
            lock = _cache_lock_for_loop()
            async with lock:
                _RECENT_INTEL_CACHE = {
                    **fresh,
                    "fetched_at": time.monotonic(),
                }
            new_kbids = fresh["all_kbids"] - _LAST_SEEN_KBIDS
            _LAST_SEEN_KBIDS = set(fresh["all_kbids"])
            raw_by_kbid = fresh["raw_by_kbid"]
            observations = fresh["observations"]
            fetch_ok = True
            log.info(
                "recent-intel poll OK: total=%d new=%d (first_poll=%s)",
                len(fresh["all_kbids"]), len(new_kbids),
                not prior_seen,
            )
        except asyncio.CancelledError:
            log.info("recent-intel poller cancelled")
            raise
        except Exception:
            log.exception(
                "recent-intel poll failed; firing callbacks with empty "
                "delta so consumers (master-map OOB sync) still run and "
                "can fall back to scenario preload"
            )

        # Fire callbacks UNCONDITIONALLY on every tick after the first —
        # even when the cougaar fetch failed. The "first poll = everything
        # is new" stampede gate (prior_seen empty) still applies to keep
        # KLIEGLIGHT autofire from re-firing the historical SIGINT stack
        # on every restart, but on_poll_sync_oob and other consumers that
        # don't depend on raw_by_kbid get to run with an empty delta —
        # their internal tool_get_oob call already falls back from
        # cougaar IDXRedUnit to scenario.KG.unit_red when cougaar is
        # unreachable / stale, so the master-map OOB layer stays warm
        # from scenario preload during cougaar outages instead of going
        # empty until the next successful poll.
        if prior_seen or not fetch_ok:
            for cb in list(_POLL_CALLBACKS):
                try:
                    await cb(new_kbids, raw_by_kbid, observations)
                except Exception:
                    log.exception("recent-intel poll callback raised")
        await asyncio.sleep(_POLLER_INTERVAL_SEC)


def start_recent_intel_poller() -> asyncio.Task:
    """Spawn the background poller as an asyncio.Task on the current loop.

    Called from main.py's FastAPI startup hook. Idempotent: if a task is
    already running on this loop, returns it instead of creating a second.
    """
    global _POLLER_TASK
    if _POLLER_TASK is not None and not _POLLER_TASK.done():
        return _POLLER_TASK
    _POLLER_TASK = asyncio.create_task(_poll_loop(), name="recent_intel_poller")
    return _POLLER_TASK


async def stop_recent_intel_poller() -> None:
    """Cancel the background poller task. Called from FastAPI shutdown."""
    global _POLLER_TASK
    if _POLLER_TASK is None:
        return
    task = _POLLER_TASK
    _POLLER_TASK = None
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def _cache_diag() -> dict:
    """Diagnostic snapshot for the /api/diag/recent_intel_cache endpoint.

    Cheap synchronous read of the cache state — no lock acquisition, no
    Cougaar hit. Used by verification tests to confirm the poller is
    populating the cache without exposing the cache dict itself.
    """
    cache = _RECENT_INTEL_CACHE
    return {
        "cache_warm": cache is not None,
        "fetched_at_monotonic": (cache or {}).get("fetched_at"),
        "fetched_age_sec": (time.monotonic() - cache["fetched_at"]) if cache else None,
        "total_kbids": len(cache.get("all_kbids", ())) if cache else 0,
        "last_seen_kbids_count": len(_LAST_SEEN_KBIDS),
        "poller_running": _POLLER_TASK is not None and not _POLLER_TASK.done(),
        "callbacks_registered": len(_POLL_CALLBACKS),
    }


# ---------------------------------------------------------------------------
# Parsed GraphQL queries
# ---------------------------------------------------------------------------

# Filter key uses double-underscore form (`data__forceType`). The schema
# advertises colon form (`data:forceType`) via getDomainPropertyKeyDefinitions,
# but the live server returns 0 rows on the colon form — verified 2026-05-14.

# Cougaar's GraphQL server doesn't accept variables via the standard {query,variables}
# POST body — verified 2026-05-15. Workaround: inline literal values into the query
# string. Force values are constrained to {"RED_FORCE","BLUE_FORCE"} so injection-safe.

def _units_query(force_value: str) -> GraphQLRequest:
    return gql(f"""
    {{
      getVerticesOfDomainTypeWithSpecifiedPropertyValues(
        fullyQualifiedDomainType: "World.Organizations.Unit"
        propertyKeyValuePairs: {{ propertyKey: "data__forceType", propertyValue: ["{force_value}"] }}
      ) {{
        kbID objectType conceptName
        ... on worldOrganizationsUnit {{
          data__ID data__name data__unitLongName data__unitShortName data__sidc
          data__description data__personnelStatus
          data__forceType {{ literal }}
          data__echelon {{ literal }}
          data__unitStatus {{ literal }}
          link__unitSuperiorID
          data__lastUpdatedDateTime {{ instant }}
          data__location {{ latitude longitude altitude }}
        }}
      }}
    }}
    """)


def _equipment_query(force_value: str) -> GraphQLRequest:
    # Equipment.data__forceType is populated; filter directly. The edge-based form
    # (getFilteredDomainVerticesByEdge with an inline EdgeFilter) trips a Java
    # cast bug server-side as of 2026-05-15.
    return gql(f"""
    {{
      getVerticesOfDomainTypeWithSpecifiedPropertyValues(
        fullyQualifiedDomainType: "World.Materiels.Equipment"
        propertyKeyValuePairs: {{ propertyKey: "data__forceType", propertyValue: ["{force_value}"] }}
      ) {{
        kbID objectType
        ... on worldMaterielsEquipment {{
          data__ID data__name data__nsn data__nomenclature
          data__forceType {{ literal }}
          link__unitID
          data__location {{ latitude longitude altitude }}
          data__locationLastUpdate {{ instant }}
        }}
      }}
    }}
    """)

_SPATIAL_CONTROLS_ALL = gql(
    """
    query GetAllSpatialControls {
      getVerticesByDomainType(
        fullyQualifiedDomainType: "World.Informations.SpatialControl"
      ) {
        kbID objectType
        ... on worldInformationsRegionSpatialControl   { data__ID data__name data__Description data__color data__source data__width data__actionType { literal } data__regionStatus { literal } data__EffectiveStartDate { instant } data__EffectiveEndDate { instant } data__coordinates { latitude longitude altitude } }
        ... on worldInformationsLinearSpatialControl   { data__ID data__name data__Description data__color data__source data__width data__actionType { literal } data__regionStatus { literal } data__EffectiveStartDate { instant } data__EffectiveEndDate { instant } data__coordinates { latitude longitude altitude } }
        ... on worldInformationsBoundarySpatialControl { data__ID data__name data__Description data__color data__source data__width data__actionType { literal } data__regionStatus { literal } data__EffectiveStartDate { instant } data__EffectiveEndDate { instant } data__coordinates { latitude longitude altitude } }
        ... on worldInformationsNOGOSpatialControl     { data__ID data__name data__Description data__color data__source data__width data__actionType { literal } data__regionStatus { literal } data__EffectiveStartDate { instant } data__EffectiveEndDate { instant } data__coordinates { latitude longitude altitude } }
        ... on worldInformationsSLOWGOSpatialControl   { data__ID data__name data__Description data__color data__source data__width data__actionType { literal } data__regionStatus { literal } data__EffectiveStartDate { instant } data__EffectiveEndDate { instant } data__coordinates { latitude longitude altitude } }
        ... on worldInformationsCorridorSpatialControl { data__ID data__name data__Description data__color data__source data__width data__actionType { literal } data__regionStatus { literal } data__EffectiveStartDate { instant } data__EffectiveEndDate { instant } data__coordinates { latitude longitude altitude } }
        ... on worldInformationsNamedAreaOfInterest    { data__ID data__name data__Description data__color data__source data__width data__actionType { literal } data__regionStatus { literal } data__EffectiveStartDate { instant } data__EffectiveEndDate { instant } data__coordinates { latitude longitude altitude } }
      }
    }
    """
)

_UUID_SAFE_CHARS = set("0123456789abcdefABCDEF-")


def _vertex_by_kbid_query(kbid: str) -> GraphQLRequest:
    if not all(c in _UUID_SAFE_CHARS for c in kbid):
        raise ValueError(f"kbID contains non-UUID characters: {kbid!r}")
    return gql(f"""
    {{
      getVertexByKbID(kbID: "{kbid}") {{
        kbID objectType
        ... on worldInformationsIDXImageIirItem {{
          data__ID data__name data__targetName data__basicEncyclopediaNumber data__country
          data__itemNumber data__targetFunctionCode data__statusActivitySummary
          data__nationalTaskingIndicator data__erref data__suffix
        }}
        ... on worldPlansTarget {{
          data__ID data__name
        }}
      }}
    }}
    """)

_IMINT_ITEMS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXImageIirItem") {
        kbID objectType
        ... on worldInformationsIDXImageIirItem {
          data__ID data__name data__targetName data__basicEncyclopediaNumber data__country
          data__itemNumber data__targetFunctionCode data__statusActivitySummary
          data__nationalTaskingIndicator data__erref data__suffix
        }
      }
    }
    """
)

_IMINT_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXImageIirReport") {
        kbID objectType
        ... on worldInformationsIDXImageIirReport {
          data__ID data__name data__serial data__itemType data__highlights
          data__imageryMissionNumber data__projectIdentifierCode
          data__reportTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_SIGINT_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXSignalOfInterestReport") {
        kbID objectType
        ... on worldInformationsIDXSignalOfInterestReport {
          data__ID data__name data__signalId data__emitterName data__platformOrSensorId
          data__startTime { instant }
          data__endTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_ELINT_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXTacElintReport") {
        kbID objectType
        ... on worldInformationsIDXTacElintReport {
          data__ID data__name data__eventDescription data__eventEntityCount
          data__forceThreatDesignator data__isSimulated
          data__reportedTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

# BDA RECCEXREP — doctrinal BDA-feeding report. GRVTY populates
# target_status (the assessment grade), gentext (narrative), ben + item
# fields (target reference), and mission fields (platform + timing).
# Joined to IDXImageryReport on the BDA side so the analyst sees the
# RECCEXREP score + the supporting image in one artifact.
_RECCEXREP_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXReccexrepReport") {
        kbID objectType
        ... on worldInformationsIDXReccexrepReport {
          data__ID data__name
          data__missionType data__missionCode data__missionPlatformOrType
          data__targetType data__targetStatus
          data__itemName data__itemNumber data__ben
          data__gentext data__timeOnTarget
          data__reportTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)


# BDA imagery reports — IDXImageryReport carries imagery_url (GRVTY-
# populated). Pulled in every poll so BDA / per-target products can join
# imagery by fusion_observation_ref_id and embed the actual image in the
# artifact (see utils/url_fetcher for the protocol-agnostic fetch).
_IMAGERY_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXImageryReport") {
        kbID objectType
        ... on worldInformationsIDXImageryReport {
          data__ID data__name
          data__imageryUrl data__imageryType data__imageNumber
          data__imageryMissionNumber data__sensorPosition
          data__projectIdentifierCode data__expansionItem
          data__universalReferenceGrid data__indexNumber
          data__fusionObservationRefID
          data__imageTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)


# WRANGLER reports — second SIGINT-class vertex per cougaar sim staff.
# Field set per __type introspection (2026-06-10) against the live
# 10.10.23.134:8200 instance.
_WRANGLER_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXWranglerReport") {
        kbID objectType
        ... on worldInformationsIDXWranglerReport {
          data__ID data__name
          data__interceptCaseNumber data__signalCaseNumber
          data__emitterFunction data__modulationType data__scanType
          data__fusionRefID
          data__doiTimeUp { instant }
          data__doiTimeDown { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_OOB_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXEnemyOrderOfBattleReport") {
        kbID objectType
        ... on worldInformationsIDXEnemyOrderOfBattleReport {
          data__ID data__name data__unitKey data__enemyUnit data__activity data__event
          data__observationTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_TACTICAL_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXTacticalReport") {
        kbID objectType
        ... on worldInformationsIDXTacticalReport {
          data__ID data__name data__forceThreatDesignator data__emitterEw data__isSimulated
          data__reportTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_HUMINT_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXHumintReport") {
        kbID objectType
        ... on worldInformationsIDXHumintReport {
          data__ID data__name data__reportType data__reportId data__summary data__rawText
          data__sourceType data__sourceName data__sourceChannel data__sourceAccountOrOrg
          data__reliabilityTier data__deceptionRisk data__biasIndicators data__assessmentRationale
          data__locationText
          data__reportTime { instant }
          data__timeBucket { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_OSINT_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXOpenSourceReport") {
        kbID objectType
        ... on worldInformationsIDXOpenSourceReport {
          data__ID data__name data__reportId data__subject data__countryRegion
          data__sourceType data__sourceName data__sourceChannel
          data__sourceAccountOrOrg data__sourceUrl
          data__sourceReliabilityIcd203 data__sourceReliabilityRationale
          data__informationCredibility data__informationCredibilityRationale
          data__summary data__bodyText data__bodyTextOriginalLanguage
          data__translationNote data__mediaDescription
          data__locationMgrs data__locationNamedLandmark data__tacticalMarkings
          data__pirAlignment data__contextualNote data__crossCueingRecommendation
          data__publicationTime { instant }
          data__acquisitionTime { instant }
          data__captureTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)


_SENSOR_CONTACT_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXSensorContactReport") {
        kbID objectType
        ... on worldInformationsIDXSensorContactReport {
          data__ID data__name data__enemyUnit data__unitKey data__activity
          data__observationTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_ENEMY_SITUATIONAL_AWARENESS_REPORTS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXEnemySituationalAwarenessReport") {
        kbID objectType
        ... on worldInformationsIDXEnemySituationalAwarenessReport {
          data__ID data__name data__unitDesignation data__extendedUnitNumber
          data__enemyUnitMasterKey data__enemyUnitClassification data__enemyParentUnitDesignator
          data__parentMasterKey data__parentExtendedUnitNumber data__parentUnitEchelon
          data__parentUnitType data__parentHonorificName data__honorificName
          data__echelon data__organizationType data__functionalRole
          data__operationalStatus data__enemyIntelEvalStage data__activityType
          data__condition data__unitStrength data__equipmentPercent
          data__nationality data__symbolId data__databaseSystemName data__databaseKey
          data__enemyObservationTime { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)

_COMMUNICATIONS_DATA_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Informations.IDXCommunicationsData") {
        kbID objectType
        ... on worldInformationsIDXCommunicationsData {
          data__ID data__name data__bandwidth data__ewCallsign data__frequencyRaw
        }
      }
    }
    """
)


# IDX 26 RED OOB vertex — purpose-built for the IDX 26 Donovian forces.
# As of 2026-05-20 the live KB has zero IDXRedUnit rows; ingestion populates
# this vertex from the OPORD/OOB parse. Until then the assemblers' RED OOB
# path returns an empty list and the intel-agent tool layer falls through
# to the scenario fixture. Never falls through to World.Organizations.Unit
# whose currently-loaded rows are NTC (California) scenario.
_IDX_RED_UNITS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Organizations.IDXRedUnit") {
        kbID objectType
        ... on worldOrganizationsIDXRedUnit {
          data__ID data__name data__unitSuperiorRefID
          data__unitDesignator data__extendedUnitNumber data__honorificName
          data__echelon data__organizationType
          data__parentUnitDesignator data__parentExtendedUnitNumber
          data__parentHonorificName data__parentEchelon
          data__parentOrganizationType
          data__affiliation data__disposition data__currentActivity
          data__composition data__strengthPctManned
          data__engagementRange data__detectionRange
          data__lastIntelStage data__descriptionNarrative data__additionalInfo
          data__symbolicIdentifier
          data__lastUpdated { instant }
          data__location { latitude longitude altitude }
        }
      }
    }
    """
)


# === Resolved-OOB queries (§ 12) =============================================
# All RED Equipment vertices with their kg-sourced fields. data__name is the
# field that's actually populated on RED equipment (data__nomenclature is the
# BLUE-side field).
_RED_EQUIPMENT_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Materiels.Equipment") {
        kbID objectType
        ... on worldMaterielsEquipment {
          data__ID data__name data__nomenclature data__description
          data__forceType { literal }
          data__location { latitude longitude altitude }
          data__locationLastUpdate { instant }
          link__redUnitID link__unitID
        }
      }
    }
    """
)

# Every Equipment_RedUnit edge instance. ~95k rows in IDX 26.
_EQUIPMENT_REDUNIT_EDGES = gql(
    """
    {
      getEdgesOfType(
        relationshipType: "Equipment_RedUnit:World.Materiels.Equipment>World.Organizations.IDXRedUnit"
      ) {
        ... on Edge {
          source { kbID }
          target { kbID }
        }
      }
    }
    """
)


# --- SIGINT emitter-location edges (KLIEGLIGHT map plotting) -----------------
# Live IDXTacElintReport / IDXWranglerReport vertices ship data__location=null;
# the emitter coordinate lives on a connected IDXLocation vertex reached via an
# AT_*_LOCATION edge (verified against live cougaar 2026-06 at 10.10.23.134:8200:
# WRANGLER one-hop, TACELINT two-hop through IDXEmitterLocations). getEdgesOfType
# is the reliable API here — getEdgesFromVertex returns inconsistent location
# kbIDs. Mirrors handlers.bda_autofire's AT_RECCEXREP_LOCATION walk.
_AT_WRANGLER_LOCATION_EDGES = gql(
    """
    {
      getEdgesOfType(
        relationshipType: "AT_WRANGLER_LOCATION:World.Informations.IDXWranglerReport>World.Environments.IDXLocation"
      ) {
        ... on Edge { source { kbID } target { kbID } }
      }
    }
    """
)

_AT_EMITTER_LOCATION_EDGES = gql(
    """
    {
      getEdgesOfType(
        relationshipType: "AT_EMITTER_LOCATION:World.Informations.IDXTacElintReport>World.Environments.IDXEmitterLocations"
      ) {
        ... on Edge { source { kbID } target { kbID } }
      }
    }
    """
)

_AT_EMITTER_LOCATIONS_EDGES = gql(
    """
    {
      getEdgesOfType(
        relationshipType: "AT_EMITTER_LOCATIONS:World.Environments.IDXEmitterLocations>World.Environments.IDXLocation"
      ) {
        ... on Edge { source { kbID } target { kbID } }
      }
    }
    """
)

_IDX_LOCATIONS_ALL = gql(
    """
    {
      getVerticesByDomainType(fullyQualifiedDomainType: "World.Environments.IDXLocation") {
        kbID
        ... on worldEnvironmentsIDXLocation {
          data__location { latitude longitude }
        }
      }
    }
    """
)


def _edges_of_type_gql(relationship_type: str):
    """getEdgesOfType template returning source+target kbIDs for one
    relationship type (the format cougaar answers reliably for the
    AT_*/HAS_*/IS_* edge families)."""
    return gql(
        '{ getEdgesOfType(relationshipType: "%s") { ... on Edge { '
        "source { kbID } target { kbID } } } }" % relationship_type
    )


# Operation-fallback location path. When a TacElint/Wrangler has NO direct
# emitter/wrangler location edge, its coordinate is still reachable: the report
# shares a ReportHeader with the IDXTacticalReport for the same intercept, and
# that tactical report's ground operation carries the geolocation. Verified live
# against cougaar 2026-06 (recovered every TacElint that AT_EMITTER_LOCATION
# missed). Chain:
#   TacElint/Wrangler --HAS_*_REPORT_HEADER--> IDXReportHeader
#        <--HAS_TACTICAL_REPORT_HEADER-- IDXTacticalReport
#        --IS_GROUND_OPERATION--> IDXGroundOperation
#        --AT_OPERATION_LOCATION--> IDXLocation
_AT_OPERATION_LOCATION_EDGES = _edges_of_type_gql(
    "AT_OPERATION_LOCATION:World.Plans.IDXGroundOperation>World.Environments.IDXLocation")
_IS_GROUND_OPERATION_EDGES = _edges_of_type_gql(
    "IS_GROUND_OPERATION:World.Informations.IDXTacticalReport>World.Plans.IDXGroundOperation")
_HAS_TACTICAL_REPORT_HEADER_EDGES = _edges_of_type_gql(
    "HAS_TACTICAL_REPORT_HEADER:World.Informations.IDXTacticalReport>World.Informations.IDXReportHeader")
_HAS_TAC_ELINT_REPORT_HEADER_EDGES = _edges_of_type_gql(
    "HAS_TAC_ELINT_REPORT_HEADER:World.Informations.IDXTacElintReport>World.Informations.IDXReportHeader")
_HAS_WRANGLER_REPORT_HEADER_EDGES = _edges_of_type_gql(
    "HAS_WRANGLER_REPORT_HEADER:World.Informations.IDXWranglerReport>World.Informations.IDXReportHeader")


async def _fetch_signal_location_map(session) -> dict[str, dict]:
    """Resolve SIGINT source-report kbID -> GeoJSON Point by walking the
    cougaar AT_*_LOCATION edge family. Live IDXTacElintReport /
    IDXWranglerReport vertices ship data__location=null; the emitter
    coordinate lives on a connected IDXLocation vertex:

        WRANGLER : IDXWranglerReport --AT_WRANGLER_LOCATION--> IDXLocation
        TACELINT : IDXTacElintReport --AT_EMITTER_LOCATION--> IDXEmitterLocations
                   --AT_EMITTER_LOCATIONS--> IDXLocation      (two-hop)

    When the direct edge is absent (about half the TacElints today), the
    coordinate is recovered via the report's ground operation — the report
    shares a ReportHeader with the IDXTacticalReport for the same intercept,
    whose IDXGroundOperation carries the location (see the operation-fallback
    note above). This two-tier resolve covered every TacElint in the live graph.

    Returns {report_kbid: {"type":"Point","coordinates":[lon,lat,0]}} for every
    source report whose chain resolves to a populated lat/lon. All queries run
    concurrently; the join is in-process. Defensive: a schema rename / missing
    edge type must NOT take down the recent-intel fetch, so any failure logs
    and returns whatever resolved (callers degrade to the bearer-unit fallback)."""
    try:
        (wr_edges, em_edges, em_loc_edges, locs,
         op_loc_edges, tr_go_edges, tr_hdr_edges,
         te_hdr_edges, wr_hdr_edges) = await asyncio.gather(
            _execute_list(session, _AT_WRANGLER_LOCATION_EDGES),
            _execute_list(session, _AT_EMITTER_LOCATION_EDGES),
            _execute_list(session, _AT_EMITTER_LOCATIONS_EDGES),
            _execute_list(session, _IDX_LOCATIONS_ALL),
            _execute_list(session, _AT_OPERATION_LOCATION_EDGES),
            _execute_list(session, _IS_GROUND_OPERATION_EDGES),
            _execute_list(session, _HAS_TACTICAL_REPORT_HEADER_EDGES),
            _execute_list(session, _HAS_TAC_ELINT_REPORT_HEADER_EDGES),
            _execute_list(session, _HAS_WRANGLER_REPORT_HEADER_EDGES),
        )
    except Exception:
        log.warning("emitter-location edge fetch failed; KLIEGLIGHTs fall back "
                    "to bearer-unit MGRS this pass", exc_info=True)
        return {}

    # IDXLocation kbID -> GeoJSON Point (skip locations with null lat/lon).
    loc_point: dict[str, dict] = {}
    for loc in locs or []:
        pt = _to_geojson_point(loc.get("data__location"))
        if pt is not None:
            loc_point[loc["kbID"]] = pt
    # IDXEmitterLocations kbID -> IDXLocation kbID (TacElint second hop).
    emitter_to_loc = {
        e["source"]["kbID"]: e["target"]["kbID"] for e in (em_loc_edges or [])
    }

    out: dict[str, dict] = {}
    # --- Tier 1: direct emitter / wrangler location edge (most precise) ------
    # WRANGLER — one hop straight to IDXLocation.
    for e in wr_edges or []:
        pt = loc_point.get(e["target"]["kbID"])
        if pt is not None:
            out[e["source"]["kbID"]] = pt
    # TACELINT — report -> EmitterLocations -> IDXLocation.
    for e in em_edges or []:
        loc_kbid = emitter_to_loc.get(e["target"]["kbID"])
        pt = loc_point.get(loc_kbid) if loc_kbid else None
        if pt is not None:
            out[e["source"]["kbID"]] = pt

    # --- Tier 2: operation fallback via the shared ReportHeader --------------
    # ReportHeader kbID -> Point, derived from the tactical report that owns the
    # header and its ground operation's location.
    op_to_loc = {e["source"]["kbID"]: e["target"]["kbID"] for e in (op_loc_edges or [])}
    tr_to_op = {e["source"]["kbID"]: e["target"]["kbID"] for e in (tr_go_edges or [])}
    header_point: dict[str, dict] = {}
    for e in tr_hdr_edges or []:
        op_kbid = tr_to_op.get(e["source"]["kbID"])
        loc_kbid = op_to_loc.get(op_kbid) if op_kbid else None
        pt = loc_point.get(loc_kbid) if loc_kbid else None
        if pt is not None:
            header_point[e["target"]["kbID"]] = pt
    # Apply the header location to any TacElint/Wrangler not already resolved.
    for e in (te_hdr_edges or []) + (wr_hdr_edges or []):
        report_kbid = e["source"]["kbID"]
        if report_kbid in out:
            continue
        pt = header_point.get(e["target"]["kbID"])
        if pt is not None:
            out[report_kbid] = pt
    return out
