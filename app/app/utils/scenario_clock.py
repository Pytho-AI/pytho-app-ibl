"""Scenario clock — authoritative "now" for every IDX 26 product.

Two modes selected by the `EVENT_NTP_HOST` env var:

  1. **EVENT mode** (EVENT_NTP_HOST is set, e.g. `10.0.1.4`):
     scenario_clock is a thin NTP client. Every `now()` call queries the
     organizer's authoritative NTP server (port 123 UDP) for the real
     exercise UTC time. Result cached for `NTP_CACHE_SECONDS` (default 5s)
     so each product fan-out doesn't hammer the server.

  2. **DEV / TEST mode** (EVENT_NTP_HOST unset):
     scenario_clock computes a FROZEN scenario time in-process by scanning
     scenario.KG['report'/'event'/'engagement'/'imagery'/'civil'] +
     scenario.PROJECT_DOCS + scenario.WEATHER for every parseable timestamp
     and taking max + 30s. The standalone `scripts/scenario_ntp_server.py`
     container reads this value over HTTP and republishes it via UDP/123
     for non-Python consumers — that's our "fake NTP for testing." At
     event time it's replaced by 10.0.1.4 (organizer-supplied).

Same code path in either mode — product code just calls `scenario_clock.now()`
and gets whatever the configured source returns. To swap from dev to event,
set `EVENT_NTP_HOST=10.0.1.4` in `app/.env` and restart docker-api.

Format:
  - now() → datetime (UTC, tz-aware)
  - now_iso() → '2026-06-30T06:00:30+00:00'
  - now_dtg() → '300600ZJUN26' (USMTF DDHHMMZmonYY)
  - now_unix_ms() → milliseconds since epoch
"""
from __future__ import annotations

import logging
import os
import re
import socket
import struct
import threading
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# Env-var picks the source. Empty / unset → DEV mode (in-process scan).
# At event deploy: set to '10.0.1.4' (or whatever IP the organizer publishes).
EVENT_NTP_HOST = (os.environ.get("EVENT_NTP_HOST") or "").strip()
EVENT_NTP_PORT = int(os.environ.get("EVENT_NTP_PORT", "123"))
# Cap NTP fetches at this rate; product DAG runs fan out per-field and a
# 5s cache means at most one NTP query per generation pass.
NTP_CACHE_SECONDS = int(os.environ.get("EVENT_NTP_CACHE_SECONDS", "5"))
NTP_TIMEOUT_SECONDS = float(os.environ.get("EVENT_NTP_TIMEOUT_SECONDS", "2.0"))
# NTP epoch is 1900-01-01, UNIX epoch is 1970-01-01 — 70-year delta.
_NTP_EPOCH_OFFSET = 2208988800

# DDHHMMZmonYYYY (e.g. '291821ZAPR2026') — fully qualified.
_DDHHMMZMONYYYY_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z([A-Z]{3})(\d{4})\b")
# DDHHMMZmonYY (e.g. '281200ZJUN26') — 2-digit year per FM 6-99.
_DDHHMMZMONYY_RE   = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z([A-Z]{3})(\d{2})\b")
# Bare DDHHMMZ — no month/year context (handled with scenario defaults).
_DDHHMMZ_RE        = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
# 14-digit observed time (WRANGLER 'observed YYYYMMDDHHMMSS').
_YMD_LONG_RE       = re.compile(r"\b(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\b")
# ISO 'YYYY-MM-DDTHH:MM:SSZ'.
_ISO_RE            = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z\b")

_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_MON_REV = {v: k for k, v in _MON.items()}

# 30-second buffer past the latest event so products doing "intel from last
# 24h" don't edge-miss the latest event by a clock skew.
_FREEZE_OFFSET_SECONDS = 30

# Scenario default month/year for parsing BARE DDHHMMZ DTGs when context is
# absent. Pulled from scenario.SCENARIO_MONTH_YEAR ("JUN26") at compute time.
_DEFAULT_YEAR = 2026
_DEFAULT_MONTH = 6

# Keyed by active project id — scenario data (and therefore the frozen
# scenario clock) is per project/event. EVENT-mode NTP stays global:
# one organizer wall clock per venue.
_FROZEN_DT: dict = {}
_FROZEN_SOURCE: dict = {}
# Manual-injects overlay rev the anchor was computed against — a new
# injected OPORD/FRAGO with later DTGs must advance the freeze.
_FROZEN_OVERLAY_REV: dict = {}
_FROZEN_LOCK = threading.Lock()


def _clock_project() -> str:
    from utils.project_context import active_project
    return active_project()


def _parse_timestamps_in_text(text: str) -> list[datetime]:
    """Pull every parseable timestamp from a free-text blob. Bare DDHHMMZ
    DTGs are anchored to the scenario default (MONTH/YEAR), which keeps them
    aligned with the OPORD D-day window."""
    out: list[datetime] = []
    if not isinstance(text, str) or not text:
        return out
    for m in _DDHHMMZMONYYYY_RE.finditer(text):
        dd, hh, mm, mon, yyyy = m.groups()
        mn = _MON.get(mon)
        if mn:
            try:
                out.append(datetime(int(yyyy), mn, int(dd), int(hh), int(mm), 0, tzinfo=timezone.utc))
            except ValueError:
                pass
    for m in _DDHHMMZMONYY_RE.finditer(text):
        dd, hh, mm, mon, yy = m.groups()
        mn = _MON.get(mon)
        if mn:
            try:
                out.append(datetime(2000 + int(yy), mn, int(dd), int(hh), int(mm), 0, tzinfo=timezone.utc))
            except ValueError:
                pass
    for m in _YMD_LONG_RE.finditer(text):
        y, mo, d, hh, mm, ss = m.groups()
        try:
            out.append(datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), tzinfo=timezone.utc))
        except ValueError:
            pass
    for m in _ISO_RE.finditer(text):
        y, mo, d, hh, mm, ss = m.groups()
        try:
            out.append(datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), tzinfo=timezone.utc))
        except ValueError:
            pass
    for m in _DDHHMMZ_RE.finditer(text):
        dd, hh, mm = m.groups()
        try:
            out.append(datetime(_DEFAULT_YEAR, _DEFAULT_MONTH, int(dd), int(hh), int(mm), 0, tzinfo=timezone.utc))
        except ValueError:
            pass
    return out


def _scan_scenario_for_max() -> tuple[datetime | None, str]:
    """Walk every fixture surface and return (max_timestamp, source_id) so
    callers can log what anchored the freeze."""
    from utils import scenario  # local import to break circular at module load
    global _DEFAULT_YEAR, _DEFAULT_MONTH
    smy = getattr(scenario, "SCENARIO_MONTH_YEAR", "JUN26")
    m = re.match(r"^([A-Z]{3})(\d{2})$", smy)
    if m:
        mon, yy = m.groups()
        _DEFAULT_MONTH = _MON.get(mon, _DEFAULT_MONTH)
        _DEFAULT_YEAR = 2000 + int(yy)

    candidates: list[tuple[datetime, str]] = []

    def _scan_blob(text: str, source_id: str):
        for ts in _parse_timestamps_in_text(text):
            candidates.append((ts, source_id))

    for key in ("report", "event", "engagement", "imagery", "civil",
                "target", "unit_red", "unit_blue"):
        rows = scenario.KG.get(key) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = row.get("id") or "?"
            blob = " ".join(v for v in row.values() if isinstance(v, str))
            _scan_blob(blob, f"KG.{key}:{rid}")

    weather = getattr(scenario, "WEATHER", None)
    if isinstance(weather, dict):
        _scan_blob(" ".join(str(v) for v in weather.values()), "WEATHER")

    terrain = getattr(scenario, "TERRAIN", None)
    if isinstance(terrain, dict):
        _scan_blob(" ".join(str(v) for v in terrain.values()), "TERRAIN")

    for tag, body in (getattr(scenario, "PROJECT_DOCS", None) or {}).items():
        if isinstance(body, dict):
            _scan_blob(body.get("body") or "", f"PROJECT_DOCS:{tag}")

    if not candidates:
        return (None, "")
    candidates.sort(key=lambda p: p[0])
    return candidates[-1]


def refreeze() -> datetime:
    """Recompute the frozen scenario clock by re-scanning scenario fixtures
    + PROJECT_DOCS. Call this after manual_injects watcher reloads a new
    OPORD / FRAGO so the freeze advances when the analyst drops in new docs.
    No-op in EVENT mode — there's no freeze to recompute when we're pulling
    from a live NTP source.

    Returns the new frozen datetime (or the cached external time in EVENT mode)."""
    if EVENT_NTP_HOST:
        # In EVENT mode the source of truth is the organizer's NTP — we
        # still expose refreeze() for callers but it just flushes our
        # cache so the next now() re-queries.
        with _NTP_CACHE_LOCK:
            globals()["_NTP_CACHED_AT_REAL"] = 0.0
        return _query_ntp_cached()
    pid = _clock_project()
    with _FROZEN_LOCK:
        max_ts, source = _scan_scenario_for_max()
        if max_ts is None:
            # No timestamps anywhere — emergency fallback to OPORD D-day per
            # scenario.WEATHER.current_dtg, or real now as last resort.
            from utils import scenario
            cur = (getattr(scenario, "WEATHER", {}) or {}).get("current_dtg")
            m = _DDHHMMZMONYY_RE.search(cur or "") if isinstance(cur, str) else None
            if m:
                dd, hh, mm, mon, yy = m.groups()
                max_ts = datetime(2000 + int(yy), _MON.get(mon, _DEFAULT_MONTH),
                                   int(dd), int(hh), int(mm), 0, tzinfo=timezone.utc)
                source = "WEATHER.current_dtg"
            else:
                max_ts = datetime.now(timezone.utc)
                source = "host_wallclock"
        from utils import project_scenario as _ps
        _FROZEN_DT[pid] = max_ts + timedelta(seconds=_FREEZE_OFFSET_SECONDS)
        _FROZEN_SOURCE[pid] = source
        _FROZEN_OVERLAY_REV[pid] = _ps._OVERLAY_REV
        log.info("scenario_clock frozen to %s (project=%s anchor=%s + %ds buffer)",
                 _FROZEN_DT[pid].isoformat(), pid, source, _FREEZE_OFFSET_SECONDS)
        return _FROZEN_DT[pid]


# ----------------------------------------------------------------------------
# EVENT mode — NTP client. Queries EVENT_NTP_HOST:123 (e.g. 10.0.1.4) for the
# organizer's authoritative time. Same wire format as our scenario_ntp_server
# so dev <-> event swap is just an env var change.
# ----------------------------------------------------------------------------
_NTP_CACHE_LOCK = threading.Lock()
_NTP_CACHED_DT: datetime | None = None
_NTP_CACHED_AT_REAL: float = 0.0


def _dev_scan_now() -> datetime:
    """DEV-mode scenario-time computation factored out of refreeze() so
    EVENT mode can fall back to it when EVENT_NTP_HOST is unreachable.
    Returns max(timestamp anywhere in scenario / event_data / OPORD /
    PROJECT_DOCS) + the same _FREEZE_OFFSET_SECONDS buffer DEV uses.
    Final fallback: scenario.WEATHER.current_dtg, then host wall-clock."""
    max_ts, _source = _scan_scenario_for_max()
    if max_ts is None:
        from utils import scenario
        cur = (getattr(scenario, "WEATHER", {}) or {}).get("current_dtg")
        m = _DDHHMMZMONYY_RE.search(cur or "") if isinstance(cur, str) else None
        if m:
            dd, hh, mm, mon, yy = m.groups()
            max_ts = datetime(2000 + int(yy), _MON.get(mon, _DEFAULT_MONTH),
                                int(dd), int(hh), int(mm), 0, tzinfo=timezone.utc)
        else:
            max_ts = datetime.now(timezone.utc)
    return max_ts + timedelta(seconds=_FREEZE_OFFSET_SECONDS)


def _query_ntp_once(host: str, port: int) -> datetime:
    """Send one UDP NTP v4 request to (host, port), return the server's
    transmit timestamp as a tz-aware datetime. Raises on timeout / bad
    response so callers can decide caching vs failure."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(NTP_TIMEOUT_SECONDS)
    try:
        # NTP v4 client request: LI=0, VN=4, Mode=3 → 0x23
        req = b"\x23" + b"\x00" * 47
        sock.sendto(req, (host, port))
        data, _addr = sock.recvfrom(48)
    finally:
        sock.close()
    if len(data) < 48:
        raise RuntimeError(f"NTP response too short ({len(data)} bytes)")
    tx_secs, tx_frac = struct.unpack("!II", data[40:48])
    unix_seconds = (tx_secs - _NTP_EPOCH_OFFSET) + tx_frac / (1 << 32)
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


def _query_ntp_cached() -> datetime:
    """Query EVENT_NTP_HOST with caching. Cache window = NTP_CACHE_SECONDS.
    On NTP failure: surface last cached value (logged); if no cache yet,
    log + fall back to DEV-mode scenario scan rather than raise — the
    SCIF .env carries EVENT_NTP_HOST=10.0.1.4 (the organizer NTP) and
    dev hosts off the SCIF network can't reach it, but we still need
    docker-api lifespan to come up. EVENT mode resumes on the next
    successful poll once the host is reachable again (cache window =
    NTP_CACHE_SECONDS, so we don't hammer)."""
    global _NTP_CACHED_DT, _NTP_CACHED_AT_REAL
    now_real = time.time()
    with _NTP_CACHE_LOCK:
        if (_NTP_CACHED_DT is not None
                and (now_real - _NTP_CACHED_AT_REAL) < NTP_CACHE_SECONDS):
            return _NTP_CACHED_DT
        try:
            dt = _query_ntp_once(EVENT_NTP_HOST, EVENT_NTP_PORT)
        except Exception as e:
            if _NTP_CACHED_DT is not None:
                log.warning("EVENT_NTP_HOST %s:%d query failed (%s); serving cached %s",
                            EVENT_NTP_HOST, EVENT_NTP_PORT, e, _NTP_CACHED_DT.isoformat())
                return _NTP_CACHED_DT
            # First-query failure: fall back to DEV-mode in-process scan
            # so the container can start. Stamp the cache with the DEV
            # result + a SHORT real-time TTL so the next poll re-attempts
            # EVENT_NTP_HOST instead of staying stuck on DEV forever.
            log.warning(
                "EVENT_NTP_HOST %s:%d unreachable on first query (%s) — "
                "falling back to DEV in-process scenario-time scan. EVENT "
                "mode will resume on the next successful poll. To force "
                "EVENT mode only, unset EVENT_NTP_HOST.",
                EVENT_NTP_HOST, EVENT_NTP_PORT, e,
            )
            dev_dt = _dev_scan_now()
            # Short cache (10s) so we re-poll quickly once the SCIF
            # network is reachable, instead of waiting NTP_CACHE_SECONDS.
            _NTP_CACHED_DT = dev_dt
            _NTP_CACHED_AT_REAL = now_real - max(0.0, NTP_CACHE_SECONDS - 10.0)
            return dev_dt
        _NTP_CACHED_DT = dt
        _NTP_CACHED_AT_REAL = now_real
        return dt


def _ensure_frozen() -> datetime:
    if EVENT_NTP_HOST:
        return _query_ntp_cached()
    pid = _clock_project()
    from utils import project_scenario as _ps
    if pid not in _FROZEN_DT or _FROZEN_OVERLAY_REV.get(pid) != _ps._OVERLAY_REV:
        return refreeze()
    return _FROZEN_DT[pid]


# -----------------------------------------------------------------------------
# Public API — all consumers funnel through these.
# -----------------------------------------------------------------------------
def now() -> datetime:
    """Frozen scenario-clock datetime (UTC, tz-aware)."""
    return _ensure_frozen()


def now_iso() -> str:
    """ISO-8601 UTC: '2026-06-29T18:21:30+00:00'."""
    return _ensure_frozen().isoformat()


def now_unix_ms() -> int:
    """Milliseconds since UNIX epoch."""
    return int(_ensure_frozen().timestamp() * 1000)


def now_dtg() -> str:
    """USMTF DDHHMMZmonYY (e.g. '291821ZJUN26'). Matches the format used by
    every product schema's `message_dtg` and `period_*_dtg` header field."""
    dt = _ensure_frozen()
    mon = _MON_REV.get(dt.month, "JUN")
    return f"{dt.day:02d}{dt.hour:02d}{dt.minute:02d}Z{mon}{dt.year % 100:02d}"


def snapshot() -> dict:
    """Diagnostic snapshot — what the clock is reading and why.

    In EVENT mode source_anchor describes the NTP host queried.
    In DEV mode source_anchor names the scenario fixture row whose timestamp
    won the max-scan."""
    dt = _ensure_frozen()
    if EVENT_NTP_HOST:
        source = f"ntp://{EVENT_NTP_HOST}:{EVENT_NTP_PORT}"
        mode = "event"
    else:
        source = _FROZEN_SOURCE.get(_clock_project(), "")
        mode = "dev"
    return {
        "mode": mode,
        "iso": dt.isoformat(),
        "dtg": now_dtg(),
        "unix_ms": int(dt.timestamp() * 1000),
        "source_anchor": source,
        "event_ntp_host": EVENT_NTP_HOST or None,
        "ntp_cache_seconds": NTP_CACHE_SECONDS,
        "freeze_offset_seconds": _FREEZE_OFFSET_SECONDS,
        "default_scenario_month_year": f"{_MON_REV.get(_DEFAULT_MONTH, '?')}{_DEFAULT_YEAR % 100:02d}",
    }
