"""OSIR auto-fire trigger — event-driven OSINT per-event report generation.

Doctrinal anchor: per FM 2-0 §1-82 the OSINT cell's stand-alone products
are OSIR (open-source report — one per discrete event) and the Tactical
OSINT Tipper (the per-event tip-off analog of KLIEGLIGHT). This module
implements the OSIR side: one published OSIR per new IDXOpenSourceReport
vertex.

There is no doctrinal "OSINT summary" — multi-event rollups belong in
INTSUM (the multi-INT product owned by the all-source analyst), not in a
separate single-INT product. Prior periodic-rollup OSIR was deleted
2026-05-28 after customer feedback identified the shape mismatch.

This module mirrors handlers/klieglight_autofire.py 1:1 in structure;
divergences are commented inline.

Trigger policy
--------------
On each recent-intel poll, the cougaar adapter computes the kbID set
delta (this_poll - last_poll). For every NEW kbID this module inspects
the raw record and decides whether to fire:

    objectType == IDXOpenSourceReport
        Always fire — OSINT report vertices ARE the per-event analytical
        record. No HVT-style threshold gate (KLIEGLIGHT has one because
        ELINT vertices may be non-HVT routine traffic; OSINT vertices are
        already curated single events).

    Other objectTypes — explicitly NOT trigger sources. KLIEGLIGHT owns
    the SIGINT side; OSIR owns the OSINT side. No cross-INT firing.

Storage + ownership
-------------------
Identical to KLIEGLIGHT: drafts land in the `_autodraft` owner namespace
under uploads/_autodraft/<artifact_id>/. The notification flow lets any
analyst promote a draft into their own session via open_draft_in_new_session.

Concurrency
-----------
Sequential per-poll-tick (see klieglight_autofire.on_poll_callback).
Running OSIRs and KLIEGLIGHTs in parallel would saturate the writer
vLLM; the sequential model keeps the writer responsive to analyst
Generate clicks landing mid-tick.

Source: cougaar IDXOpenSourceReport vertex when live; scenario.KG['report']
type='OSINT' rows mapped via utils.event_data_fixtures._parse_osint as the
fallback until cougaar adds the vertex.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid

log = logging.getLogger(__name__)


# Persistent fired-kbid log — see klieglight_autofire for the pattern
# and memory/persistent_dedup_log_pattern.md for the rationale.
_FIRED_KBIDS_LOG_PATH = "/app/logs/osir_fired_kbids.log"


def _load_fired_kbids() -> set:
    try:
        with open(_FIRED_KBIDS_LOG_PATH, "r") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning("OSIR fired-kbid log read failed: %s", e)
        return set()


def _record_fired_kbid(kbid: str) -> None:
    if not kbid:
        return
    try:
        os.makedirs(os.path.dirname(_FIRED_KBIDS_LOG_PATH), exist_ok=True)
        with open(_FIRED_KBIDS_LOG_PATH, "a") as f:
            f.write(f"{kbid}\n")
    except Exception as e:
        log.warning("OSIR fired-kbid log append failed (key=%s): %s",
                      kbid, e)


def _fired_key(project_id: str, kbid: str) -> str:
    """Per-project dedup key for the fired log — same contract as
    klieglight_autofire._fired_key: '<project_id>:<kbid>', one OSIR per
    signal PER EVENT. Pre-unification bare-kbid log lines are rewritten
    by the box migration."""
    return f"{project_id}:{kbid}"


def _should_fire(raw_record: dict) -> bool:
    """Decide whether a single newly-arrived KG record should auto-fire an
    OSIR.

    Receives the dict shape produced by cougaar_adapters._transform —
    flattened model_dump() of the IDXOpenSourceReport pydantic model with
    `_objectType` stamped by _fetch_recent_intel_raw OR the scenario-
    fixture equivalent stamped by event_data_fixtures._parse_osint.

    Single gate: the vertex type is IDXOpenSourceReport. Unlike
    KLIEGLIGHT we do NOT additionally require a populated subset of
    fields — OSIR rows are curated single events, not raw intercepts;
    if the row exists in the KG it represents a real OSINT acquisition
    worth publishing.
    """
    return (raw_record.get("_objectType") or "") == "IDXOpenSourceReport"


def _build_autofire_context(raw_record: dict) -> str:
    """Build the prose context string passed to tool_generate_product's
    `context` arg. The DAG runner forwards this verbatim to every per-field
    LLM prompt under "ANALYST CONTEXT" so the OSIR writer fills each of
    the five sections directly from the matching fields below without
    having to guess which line maps to which section.

    The raw_record may come from either:
      - cougaar live IDX vertex (once IDXOpenSourceReport is hosted live).
      - scenario fixture (rich body authored in scenario.KG['report'] and
        parsed via utils/event_data_fixtures._parse_osint).
    """
    obj_type = raw_record.get("_objectType") or "(unknown)"
    kbid = raw_record.get("uuid") or "(no kbID)"
    src = raw_record.get("_source") or "cougaar_live"
    source_serial = raw_record.get("_source_msg_serial") or raw_record.get("_source_report_id") or ""

    lines = [
        f"EVENT-DRIVEN OSIR auto-fire on newly-arrived OSINT record.",
        f"Trigger source: {obj_type} kbID={kbid} (origin: {src}"
        + (f"; report_id={source_serial}" if source_serial else "")
        + ").",
        "",
        "PROVENANCE TAG (cite verbatim in every section's _provenance):",
        f"  {obj_type}.kbID={kbid}",
        "",
        "Fill every header and section field VERBATIM from the corresponding "
        "raw-record field below. NEVER invent a source name, channel, account, "
        "reliability rating, MGRS, or quoted text — every value traces to the "
        "raw record fields below or is left empty.",
        "",
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

    # Header chrome — REPORT NUMBER / DATE OF PUBLICATION / DATE OF
    # ACQUISITION / COUNTRY-REGION / SUBJECT
    lines.append("Header (chrome at top of doc):")
    _emit("report_number", raw_record.get("report_id"))
    _emit("date_of_publication", raw_record.get("publication_time"))
    _emit("date_of_acquisition", raw_record.get("acquisition_time"))
    _emit("country_region", raw_record.get("country_region"))
    _emit("subject", raw_record.get("subject"))
    lines.append("")

    # §1 Source Description and Reliability
    lines.append("§1 — Source Description and Reliability:")
    # Build a one-line `source` string that combines the named source +
    # channel + account so the writer can paste it verbatim into §1 line 1.
    src_name = raw_record.get("source_name") or ""
    src_channel = raw_record.get("source_channel") or ""
    src_account = raw_record.get("source_account_or_org") or ""
    src_url = raw_record.get("source_url") or ""
    pieces = [p for p in (src_name, src_channel, src_account) if p]
    one_line_source = ". ".join(pieces) if pieces else ""
    if src_url:
        one_line_source = f"{one_line_source} ({src_url})" if one_line_source else src_url
    _emit("source", one_line_source)
    _emit("source_type", raw_record.get("source_type"))
    _emit("source_reliability_icd203", raw_record.get("source_reliability_icd203"))
    _emit("source_reliability_rationale", raw_record.get("source_reliability_rationale"))
    _emit("information_credibility", raw_record.get("information_credibility"))
    _emit("information_credibility_rationale", raw_record.get("information_credibility_rationale"))
    lines.append("")

    # §2 Summary of Information
    lines.append("§2 — Summary of Information:")
    _emit("summary", raw_record.get("summary"))
    if not raw_record.get("summary"):
        lines.append(
            "  GAP RULE: if `summary` is missing, condense the §3 body_text "
            "into two-to-four neutral sentences (who/what/where/when). Do NOT "
            "add tactical interpretation here — that belongs in §5."
        )
    lines.append("")

    # §3 Details of Post / Intercepted Text
    lines.append("§3 — Details of Post / Intercepted Text:")
    _emit("translation_note", raw_record.get("translation_note"))
    _emit("body_text", raw_record.get("body_text"))
    _emit("body_text_original_language", raw_record.get("body_text_original_language"))
    _emit("media_description", raw_record.get("media_description"))
    lines.append("")

    # §4 Geospatial and Technical Metadata
    lines.append("§4 — Geospatial and Technical Metadata:")
    _emit("geolocated_mgrs", raw_record.get("location_mgrs"))
    _emit("geolocated_named_landmark", raw_record.get("location_named_landmark"))
    _emit("lat", raw_record.get("lat"))
    _emit("lon", raw_record.get("lon"))
    _emit("location", raw_record.get("location"))
    _emit("tactical_markings", raw_record.get("tactical_markings"))
    _emit("time_of_capture", raw_record.get("capture_time"))
    lines.append("")

    # §5 Collector's Comments
    lines.append("§5 — Collector's Comments:")
    _emit("requirement_alignment", raw_record.get("pir_alignment"))
    _emit("contextual_note", raw_record.get("contextual_note"))
    _emit("cross_cueing_recommendation", raw_record.get("cross_cueing_recommendation"))
    lines.append("")

    # Any remaining raw fields the writer might want — emitted verbatim
    # but excluded if already handled above. Keeps the prompt complete
    # without duplication.
    lines.append("Other raw fields (verbatim from source):")
    handled = {
        "uuid", "ref_id", "report_id", "subject", "country_region",
        "publication_time", "acquisition_time", "capture_time",
        "source_type", "source_name", "source_channel",
        "source_account_or_org", "source_url",
        "source_reliability_icd203", "source_reliability_rationale",
        "information_credibility", "information_credibility_rationale",
        "summary", "body_text", "body_text_original_language",
        "translation_note", "media_description",
        "lat", "lon", "location", "location_mgrs",
        "location_named_landmark", "tactical_markings",
        "pir_alignment", "contextual_note", "cross_cueing_recommendation",
    }
    for k, v in raw_record.items():
        if k.startswith("_") or v in (None, "", [], {}):
            continue
        if k in handled:
            continue
        _emit(k, v)

    lines.extend([
        "",
        "Rules:",
        "  - Fill each section VERBATIM from the matching fields above.",
        "  - NEVER invent a source name, channel, account, URL, reliability "
        "rating, MGRS, named landmark, tactical marking, or quoted text.",
        "  - If a field is absent from the raw record above, leave the "
        "corresponding section subfield EMPTY (the runner will surface the "
        "gap) — never fabricate a placeholder source or rating.",
        "  - Every section's _provenance MUST cite the PROVENANCE TAG above.",
        "  - This is a SINGLE-EVENT OSIR — do NOT aggregate across multiple "
        "OSINT rows; this fires on ONE detection only.",
    ])
    return "\n".join(lines)


_AUTODRAFT_USER_ID = "_autodraft"


# Hook for the notification module. Identical contract to
# klieglight_autofire.register_notification_emitter — main.py wires the
# bridge at startup via handlers/notifications.wire_osir_autofire.
_notification_emitter = None


def register_notification_emitter(emitter) -> None:
    """Wire an async callable that receives the post-fire notification payload.

        async def emitter(payload: dict) -> None

    Payload shape:
        {
          "type": "osir_autofire",
          "artifact_id": str,
          "autodraft_session_id": str,
          "kbid": str,
          "report_number": str | None,
          "source_name": str | None,
          "geolocated_mgrs": str | None,
          "location": dict | None,
          "fired_at_ms": int,
        }
    """
    global _notification_emitter
    _notification_emitter = emitter


async def _fire_one(raw_record: dict, project_id: str = "") -> dict:
    """Generate a single OSIR draft for one triggering OSINT record,
    into `project_id`'s artifact pool.

    Returns a dict describing the fire result. Raises only on programming
    errors (bad imports etc.); operational failures (DAG validation, vLLM
    timeout) are captured into the returned dict.
    """
    from handlers.intel_agent import ID_PATTERNS, State, tool_generate_product
    from handlers.klieglight_autofire import _bearer_mgrs_for_signal

    kbid = raw_record.get("uuid") or ""
    obj_type = raw_record.get("_objectType") or "(unknown)"
    autodraft_session_id = f"autodraft-{uuid.uuid4()}"
    fired_at_ms = int(time.time() * 1000)

    log.info(
        "OSIR auto-fire START: kbid=%s obj_type=%s session=%s",
        kbid, obj_type, autodraft_session_id,
    )

    # Sparse-OSINT MGRS seeding. IDXOpenSourceReport vertices today often
    # carry only the report body + cross_cueing_recommendation; the
    # geolocated_mgrs subfield falls back to the writer's '[ANALYST
    # INPUT REQUIRED]' placeholder when raw_record has no coords of its
    # own. Seed the bearer unit's MGRS so the writer fills
    # paragraph_3_event_attribution.geolocated_mgrs with a real coord
    # and the operations-map signal-report layer can plot the icon.
    if not raw_record.get("mgrs") and not raw_record.get("geolocated_mgrs") \
            and not raw_record.get("lat"):
        bearer_kg, bearer_mgrs = _bearer_mgrs_for_signal(raw_record)
        if bearer_mgrs:
            raw_record["mgrs"] = bearer_mgrs
            raw_record["geolocated_mgrs"] = bearer_mgrs
            raw_record["_geo_source"] = f"bearer_unit:{bearer_kg or 'unknown'}"
            log.info(
                "OSIR auto-fire MGRS seed: kbid=%s bearer_kg=%s mgrs=%s",
                kbid, bearer_kg, bearer_mgrs,
            )

    # Pre-build the writer context so we can scan it for legitimate
    # scenario IDs to seed seen_ids with. The OSINT row's
    # contextual_note + cross_cueing_recommendation frequently reference
    # real scenario entities by ID (civic landmarks C-005 / C-006,
    # target ids T-XXXXX, unit ids U-XX-...). The writer copies them
    # verbatim per the hard_rule "NEVER paraphrase the body_text /
    # contextual_note"; without pre-seeding, the fabrication detector at
    # intel_agent._validate_emit flags them because seen_ids is empty
    # (autofire bypasses the normal tool-call-driven harvest path).
    context_str = _build_autofire_context(raw_record)

    from utils.project_context import set_active_project
    set_active_project(project_id)
    state = State.fresh(
        autodraft_session_id,
        user_id=_AUTODRAFT_USER_ID,
        manual_product_type="OSIR",
        project_id=project_id,
    )
    seen_from_context: set = set()
    for pat in ID_PATTERNS:
        seen_from_context.update(pat.findall(context_str))
    if seen_from_context:
        state = state.add_seen(seen_from_context)
        log.info(
            "OSIR auto-fire pre-seeded seen_ids from context: %d IDs (%s)",
            len(seen_from_context),
            ", ".join(sorted(seen_from_context)[:8]),
        )

    args = {
        "product_type": "OSIR",
        "session_id": autodraft_session_id,
        "user_id": _AUTODRAFT_USER_ID,
        "project_id": project_id,
        "context": context_str,
    }

    try:
        result = await tool_generate_product(args, state, on_event=None)
    except Exception as e:
        log.exception("OSIR auto-fire failed: kbid=%s", kbid)
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
            "OSIR auto-fire DAG returned error: kbid=%s err=%s detail=%s",
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
    p4 = structured.get("paragraph_4_geospatial_and_technical_metadata") or {}
    mgrs = (p4.get("geolocated_mgrs") or "") if isinstance(p4, dict) else ""

    log.info(
        "OSIR auto-fire DONE: kbid=%s artifact_id=%s report_number=%s mgrs=%s",
        kbid, artifact_id, header.get("report_number") or "(none)",
        mgrs or "(none)",
    )

    entry = {
        "kbid": kbid,
        "obj_type": obj_type,
        "artifact_id": artifact_id,
        "autodraft_session_id": autodraft_session_id,
        "report_number": header.get("report_number") or "",
        "source_name": raw_record.get("source_name") or "",
        "geolocated_mgrs": mgrs or raw_record.get("location_mgrs") or "",
        "location": raw_record.get("location"),
        "fired_at_ms": fired_at_ms,
        "error": None,
    }

    if _notification_emitter is not None:
        try:
            await _notification_emitter({
                "type": "osir_autofire",
                "artifact_id": artifact_id,
                "autodraft_session_id": autodraft_session_id,
                "kbid": kbid,
                "report_number": header.get("report_number") or "",
                "source_name": entry["source_name"],
                "geolocated_mgrs": entry["geolocated_mgrs"],
                "location": raw_record.get("location"),
                "fired_at_ms": fired_at_ms,
            })
        except Exception:
            log.exception("OSIR notification emitter raised")

    return entry


async def _fire_one_registered(raw_record: dict, project_id: str = "") -> dict:
    """Run OSIR _fire_one through the unified product_records registry
    so the unified UI sees OSIR autofires alongside autodraft sweep
    fires. Returns the legacy entry dict for back-compat. Mirrors
    handlers/klieglight_autofire._fire_one_registered."""
    from handlers import product_records as _pr

    captured: dict[str, dict] = {}

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
        source="osir_autofire",
        product_type="OSIR",
        request_payload={
            "kbid": raw_record.get("uuid") or "",
            "obj_type": raw_record.get("_objectType") or "",
            # Persist the full raw_record so the retry dispatcher in
            # main.py can re-fire _fire_one(raw_record). Without this,
            # every manual retry of a failed OSIR chain fires
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
    """Fire an OSIR into `project_id`'s pool for every qualifying OSINT
    row in the scenario data. THE fire path since the Cougaar poll plane
    was disconnected (2026-07-09): scenario data is static, so one sweep
    at PROJECT CREATION covers every OSINT row the event carries.
    Idempotent via the persistent fired-kbid log
    ('<project_id>:<kbid>' keys). Sequential fires — parallel would
    saturate the writer LLM."""
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
            log.exception("OSIR project sweep: _fire_one raised for "
                          "kbid=%s project=%s", kbid, project_id)
            errors += 1

    log.info(
        "OSIR project sweep complete: project=%s candidates=%d "
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
    """Operator-triggered force refire — fires OSIRs for the given
    kbIDs (or every qualifying scenario OSINT row when kbids is None)
    into EVERY active project's pool, BYPASSING the fired-kbid dedup.
    Use after a schema or producer edit when you want fresh OSIRs even
    though the prior ones already landed."""
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
                log.exception("OSIR force_refire: _fire_one raised for kbid=%s", kbid)
                errors += 1

    log.info(
        "OSIR force_refire complete: candidates=%d fired=%d errors=%d",
        len(candidates), fired, errors,
    )
    return {
        "candidates": len(candidates),
        "fired": fired,
        "errors": errors,
    }
