"""Parse scenario.KG['report'] event_data USMTF fixture rows into structured
raw records that mirror the cougaar IDX vertex shape, so the recent-intel
cache + KLIEGLIGHT autofire see rich signal bodies even when cougaar's live
IDX vertices are sparse/empty.

The live cougaar adapter pulls IDXTacElintReport / IDXSignalOfInterestReport
etc., but the production Cougaar instance currently strips most fields on
those vertices (we get just uuid + ref_id + force_threat_designator on
ELINT). The IBL event_data dump preserved the rich USMTF bodies (TACELINT
DOGEARTA emitter intercepts with frequency/PRF/lat-lon/window, WRANGLER
SIGINT signal-parameter intercepts with case-IDs + freq + coords, etc.) —
those went into `scenario.KG['report']` so the writers can fall back when
cougaar is empty.

This module:
  - Parses each fixture row's `details` into structured fields.
  - Stamps a stable synthetic UUID (deterministic from msg_serial) so the
    poller's new-kbid delta logic doesn't re-fire on every tick.
  - Maps the fixture `type` onto the doctrinal IDX vertex name (e.g.
    TACELINT → IDXTacElintReport, WRANGLER → IDXSignalOfInterestReport)
    so `_should_fire` + per-product schema wiring sees them as the right
    vertex type.
"""
from __future__ import annotations

import re
import uuid as _uuid
from typing import Any

# Lat/lon in LS:DDMMSSN(/S)DDDMMSSE(/W) format, e.g. "LS:571821N0282847E".
_LS_RE = re.compile(
    r"LS:(\d{2})(\d{2})(\d{2})([NS])(\d{3})(\d{2})(\d{2})([EW])"
)

# MGRS in UT: format, e.g. "UT:35VND7306197534" or "UT:36VVJ15008361".
_UT_RE = re.compile(r"UT:([0-9]{1,2}[A-Z]{1,3}\d+)")

# TACELINT details: "Emitter <NAME> at LS:<latlon>; freq <X>MHz; PRF <Y>PPS; intercept window <T1>-<T2>."
_TACELINT_DETAIL_RE = re.compile(
    r"Emitter\s+(?P<emitter>\S+)\s+at\s+LS:(?P<latlon>\d+[NS]\d+[EW]);\s*"
    r"freq\s+(?P<freq>[\d.]+)MHz;\s*"
    r"PRF\s+(?P<prf>[\d.]+)PPS;\s*"
    r"intercept window\s+(?P<t1>\d+Z)-(?P<t2>\d+Z)\."
)

# TACELINT subject: "ELINT — <EMITTER> ELNOT <CODE>"
_TACELINT_SUBJECT_RE = re.compile(
    r"ELINT\s+[—–-]\s+(?P<emitter>\S+)\s+ELNOT\s+(?P<elnot>\S+)"
)

# WRANGLER subject: "SIGINT signal parameters — notation <CODE> <MOD>"
_WRANGLER_SUBJECT_RE = re.compile(
    r"SIGINT signal parameters\s+[—–-]\s+notation\s+(?P<notation>\S+)\s+(?P<mod>\S+)"
)

# WRANGLER details — two shapes coexist:
#   (a) Legacy compact: "Case <ID>; freq <X>MHz <MOD>; lat/lon <DDMMSSN><DDDMMSSE>; observed <T1>-<T2>."
#   (b) 515224b USMTF:  "WRANGLER SIGINT intercept: case <ID>; signal_notation <NOT>; mod <MOD>; freq <X>MHz; lat/lon <DDMMSSN><DDDMMSSE>; observed <T1>-<T2>. ..."
# Single regex handles both — `case` is anchored before the case id, freq/mod
# order is flexible (mod may be inline or separated by a `; mod <X>;` segment),
# notation is captured if present.
_WRANGLER_DETAIL_RE = re.compile(
    r"(?:[Cc]ase)\s+(?P<case>\S+?);\s*"
    r"(?:signal_notation\s+(?P<notation>\S+);\s*)?"
    r"(?:mod\s+(?P<mod_pre>\S+);\s*)?"
    r"freq\s+(?P<freq>[\d.]+)MHz(?:\s+(?P<mod_post>\S+?))?;\s*"
    r"lat/lon\s+(?P<lat>\d+[NS])(?P<lon>\d+[EW]);\s*"
    r"observed\s+(?P<t1>\d+)-(?P<t2>\d+)"
)

# EMTREW details: "EMTREW intercept at UT:<MGRS>; freq MHz; PD ; scan Hz ; PRF ; observed <T>."
_EMTREW_DETAIL_RE = re.compile(
    r"EMTREW intercept at\s+UT:(?P<mgrs>[0-9A-Z]+);.*?observed\s+(?P<t>\d+Z)\."
)
_EMTREW_SUBJECT_RE = re.compile(
    r"ELINT EW\s+[—–-]\s+(?P<emitter>\S+)\s+ELNOT"
)

# COMINT (TACREP_COMEW) — three shapes coexist:
#   (a) Legacy compact: "COMINT intercept at UT:<MGRS>; freq <X>MHz BW <BW>; observed <T>."
#   (b) 515224b USMTF:  "COMINT intercept: GNDOP at UT:<MGRS> obs <T> unit <ROLE> (...); ELP ...; FORCODE 38; COMEW <X>MHZ BW <BW>. ..."
#   (c) 515224b plaintext annotation: "COMINT intercept (IEWTPT plaintext): GNDOP at UT:<MGRS> obs <T> ..."
# Frequency/BW token in (b)/(c) sits under "COMEW <freq>MHZ BW <bw>", uppercase MHZ.
_COMINT_DETAIL_RE = re.compile(
    r"COMINT intercept"
    r"(?:\s*\([^)]*\))?"                              # optional "(IEWTPT plaintext)" annotation
    r"(?:\s+at|:\s*\S+\s+at)\s+"                       # "at UT:..." or ": GNDOP at UT:..."
    r"UT:(?P<mgrs>[0-9A-Z]+)[;\s]+"
    r"(?:obs\s+(?P<obs_alt>\d+Z)\s+unit\s+(?P<unit>[A-Z][A-Z\s/&\-]+?)\s*\(.*?\);\s*)?"
    r"(?:ELP\s+[^;]+;\s*)?"
    r"(?:FORCODE\s+\d+;\s*)?"
    r"(?:freq|COMEW)\s+(?P<freq>[\d.]+)\s*MHz\s+BW\s+(?P<bw>\S+?)(?:[;.]|\s*$)"
    r"(?:\s*observed\s+(?P<t>\d+Z)\.?)?",
    re.IGNORECASE,
)
_COMINT_SUBJECT_RE = re.compile(r"COMINT\s+[—–-]\s+(?P<role>UNIT\s*\([^)]+\))")

# Namespace for deterministic fixture UUIDs — never reused for live cougaar
# kbIDs (they come from cougaar's UUID space).
_FIXTURE_UUID_NS = _uuid.UUID("00000000-0000-0000-0000-fac1f1fed00d")


def _stable_uuid(serial: str) -> str:
    """Return a deterministic UUID for the given fixture serial. Same serial
    → same UUID across restarts so the poller doesn't re-fire on tick 2."""
    return str(_uuid.uuid5(_FIXTURE_UUID_NS, serial))


def _dms_to_decimal(dms: str) -> float | None:
    """Parse 'DDMMSSN', 'DDDMMSSE' style → decimal degrees. Returns None on
    parse failure."""
    if len(dms) < 5:
        return None
    sign_char = dms[-1].upper()
    if sign_char not in ("N", "S", "E", "W"):
        return None
    digits = dms[:-1]
    # latitude is 6 digits (DDMMSS); longitude is 7 (DDDMMSS).
    if len(digits) == 6:
        d, m, s = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    elif len(digits) == 7:
        d, m, s = int(digits[0:3]), int(digits[3:5]), int(digits[5:7])
    else:
        return None
    val = d + m / 60.0 + s / 3600.0
    if sign_char in ("S", "W"):
        val = -val
    return round(val, 6)


def _hhmmss_z_to_iso(token: str, fallback_year: int = 2026,
                       fallback_month: int = 6, fallback_day: int = 28) -> str:
    """Best-effort: 'HHMMSSZ' → ISO. Without a year/month/day, defaults to
    the IDX 26 exercise D-day (2026-06-28). For YYYYMMDDHHMMSS-style 14-digit
    inputs (WRANGLER's `observed` tokens), parses the full date."""
    if not token:
        return ""
    digits = token.rstrip("Z")
    if len(digits) == 14:
        return (
            f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}T"
            f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}Z"
        )
    if len(digits) == 6:
        return (
            f"{fallback_year:04d}-{fallback_month:02d}-{fallback_day:02d}T"
            f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}Z"
        )
    return token


def _parse_tacelint(row: dict) -> dict | None:
    """TACELINT-XXXXXXX → IDXTacElintReport-shaped record."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details_m = _TACELINT_DETAIL_RE.search(row.get("details", "") or "")
    subj_m = _TACELINT_SUBJECT_RE.search(row.get("subject", "") or "")
    if not details_m:
        return None
    emitter = details_m.group("emitter") or (subj_m and subj_m.group("emitter")) or ""
    elnot = subj_m.group("elnot") if subj_m else ""
    latlon = details_m.group("latlon")
    # latlon string is "571821N0282847E" — split into lat (7 chars including hemisphere) + lon
    lat_end = next(
        (i + 1 for i, ch in enumerate(latlon) if ch in ("N", "S")), None
    )
    lat_str = latlon[:lat_end] if lat_end else latlon
    lon_str = latlon[lat_end:] if lat_end else ""
    lat = _dms_to_decimal(lat_str)
    lon = _dms_to_decimal(lon_str)
    return {
        "_objectType": "IDXTacElintReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"tacelint-{serial}"),
        "ref_id": serial,
        # ELINT emitter classification — DOGEARTA etc. are doctrinally HVT
        # threat designator class, so autofire's _is_hvt gate fires.
        "force_threat_designator": "30",
        "is_simulated": False,
        "emitter_name": emitter,
        "elnot_designator": elnot,
        "parent_threat_system": "",  # not in fixture string
        "frequency_mhz": float(details_m.group("freq")),
        "prf_pps": float(details_m.group("prf")),
        "modulation": "",
        "pulse_duration": "",
        "lat": lat,
        "lon": lon,
        "intercept_start": _hhmmss_z_to_iso(details_m.group("t1")),
        "intercept_end": _hhmmss_z_to_iso(details_m.group("t2")),
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


def _parse_wrangler(row: dict) -> dict | None:
    """WRANGLER-intcsevalXXX → IDXSignalOfInterestReport-shaped record."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details_m = _WRANGLER_DETAIL_RE.search(row.get("details", "") or "")
    subj_m = _WRANGLER_SUBJECT_RE.search(row.get("subject", "") or "")
    if not details_m:
        return None
    lat = _dms_to_decimal(details_m.group("lat"))
    lon = _dms_to_decimal(details_m.group("lon"))
    # Notation: prefer the explicit signal_notation field from the new shape,
    # fall back to the subject parse for the legacy shape.
    notation = details_m.group("notation") or (subj_m.group("notation") if subj_m else "")
    # Modulation: appears as "; mod <X>;" in the new shape, or inline "freq <F>MHz <MOD>" in legacy.
    modulation = details_m.group("mod_pre") or details_m.group("mod_post") or ""
    return {
        "_objectType": "IDXSignalOfInterestReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"wrangler-{serial}"),
        "ref_id": serial,
        "signal_id": details_m.group("case"),
        "emitter_name": notation,
        "platform_or_sensor_id": row.get("reporter", ""),
        "frequency_mhz": float(details_m.group("freq")),
        "modulation": modulation,
        "lat": lat,
        "lon": lon,
        "start_time": _hhmmss_z_to_iso(details_m.group("t1")),
        "end_time": _hhmmss_z_to_iso(details_m.group("t2")),
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


def _parse_emtrew(row: dict) -> dict | None:
    """TACREP_EMTREW → IDXTacElintReport-shaped (EW intercept, sparser body)."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details_m = _EMTREW_DETAIL_RE.search(row.get("details", "") or "")
    subj_m = _EMTREW_SUBJECT_RE.search(row.get("subject", "") or "")
    if not details_m:
        return None
    return {
        "_objectType": "IDXTacElintReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"emtrew-{serial}"),
        "ref_id": serial,
        "force_threat_designator": "30",
        "is_simulated": False,
        "emitter_name": subj_m.group("emitter") if subj_m else "",
        "elnot_designator": "",
        "mgrs": details_m.group("mgrs"),
        "frequency_mhz": None,
        "prf_pps": None,
        "modulation": "EW",
        "pulse_duration": "",
        "intercept_start": _hhmmss_z_to_iso(details_m.group("t")),
        "intercept_end": _hhmmss_z_to_iso(details_m.group("t")),
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


def _parse_comint(row: dict) -> dict | None:
    """TACREP_COMEW → IDXCommunicationsData-shaped record."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details_m = _COMINT_DETAIL_RE.search(row.get("details", "") or "")
    subj_m = _COMINT_SUBJECT_RE.search(row.get("subject", "") or "")
    if not details_m:
        return None
    # Either "observed <T>Z." (legacy tail) or "obs <T>Z unit ..." (new lead) carries the time.
    obs_token = details_m.group("t") or details_m.group("obs_alt") or ""
    # Intercepted role: subject parse covers the legacy "COMINT — UNIT (X)" subject;
    # new shape carries the same info inline as "unit <ROLE> (...)" which we capture.
    if subj_m:
        intercepted_role = subj_m.group("role")
    elif details_m.group("unit"):
        intercepted_role = f"UNIT ({details_m.group('unit').strip()})"
    else:
        intercepted_role = ""
    return {
        "_objectType": "IDXCommunicationsData",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"comint-{serial}"),
        "ref_id": serial,
        "intercepted_role": intercepted_role,
        "mgrs": details_m.group("mgrs"),
        "frequency_mhz": float(details_m.group("freq")),
        "bandwidth": details_m.group("bw"),
        "reported_time": _hhmmss_z_to_iso(obs_token),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


# ---------------------------------------------------------------------------
# ENSIT — Enemy Situation Report
# ---------------------------------------------------------------------------
# Subject pattern: "Enemy unit detection: <UNIT_DESIGNATION> (<SYMBOL_ID>)"
# Details pattern: "UIC <KEY> at <LAT>N<LON>E; role <ROLE>; echelon <ECHELON>; DTG <DTG>."
_ENSIT_SUBJECT_RE = re.compile(
    r"Enemy unit detection:\s+(?P<designation>[^(]+?)\s*\((?P<symbol>[A-Z0-9\-]+)\)"
)
# ENSIT details — two shapes:
#   (a) Legacy compact: "UIC <K> at <LAT>N<LON>E; role <R>; echelon <E>; DTG <DTG>."
#   (b) 515224b rollup:  "<N> enemy units + <M> equipment observations (exercise ...). UNITS: <DESIG> (UIC <K>, SIDC <S>) at <LAT>N<LON>E parent <PARENT>; ... EQUIPMENT: ... MSGID ENSIT/<R>/<SER>. DTG <DTG>."
# Legacy regex stays for compact rows; new regex pulls the first UNITS entry as the representative anchor.
_ENSIT_DETAILS_RE = re.compile(
    r"UIC\s+(?P<uic>\S+)\s+at\s+"
    r"(?P<lat_d>\d{2})(?P<lat_m>\d{2})(?P<lat_s>\d{2}(?:\.\d+)?)N"
    r"(?P<lon_d>\d{3})(?P<lon_m>\d{2})(?P<lon_s>\d{2}(?:\.\d+)?)E;\s*"
    r"role\s+(?P<role>\S+);\s*"
    r"echelon\s+(?P<echelon>\S+);\s*"
    r"DTG\s+(?P<dtg>\d{6,12}[A-Z][A-Z]{3}\d{4})\."
)
_ENSIT_ROLLUP_RE = re.compile(
    r"\d+\s+enemy units\s+\+\s+\d+\s+equipment observations.*?"
    r"UNITS:\s+(?P<designation>[^()]+?)\s+\(UIC\s+(?P<uic>\S+?),\s+SIDC\s+(?P<symbol>[A-Z0-9\-]+)\)\s+at\s+"
    r"(?P<lat_d>\d{2})(?P<lat_m>\d{2})(?P<lat_s>\d{2}(?:\.\d+)?)N"
    r"(?P<lon_d>\d{3})(?P<lon_m>\d{2})(?P<lon_s>\d{2}(?:\.\d+)?)E"
    r"(?:\s+parent\s+(?P<parent>.+?))?[;.]"
)
_ENSIT_ROLLUP_DTG_RE = re.compile(
    r"DTG\s+(?P<dtg>\d{6,12}[A-Z][A-Z]{3}\d{4})\.?\s*$"
)
# Echelon falls out of the designation suffix — "615 SAM PLT" → "PLT", "600 CMBTENG BDE" → "BDE".
_ENSIT_ECHELON_TOKENS = (
    "BDE", "BN", "BTY", "CO", "PLT", "SQDN", "REGT", "DIV", "ARMY", "BCT", "TM",
)


def _ddmmss_decimal_to_decimal(d: str, m: str, s: str) -> float:
    """Parse 'DDMMSS' / 'DDDMMSS' with optional fractional seconds → decimal."""
    return round(int(d) + int(m) / 60.0 + float(s) / 3600.0, 6)


def _ensit_dtg_to_iso(dtg: str) -> str:
    """ENSIT dtg comes in two shapes:
      - '161832ZAPR2026'  (DDHHMMZmmmYYYY)
      - '161832ZAPR2026.' caller strips the period
    Returns ISO8601 UTC or '' on parse failure."""
    m = re.match(r"^(\d{2})(\d{2})(\d{2})Z([A-Z]{3})(\d{4})$", dtg)
    if not m:
        return ""
    dd, hh, mm, mon, yyyy = m.groups()
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    if mon not in months:
        return ""
    return f"{yyyy}-{months[mon]:02d}-{int(dd):02d}T{int(hh):02d}:{int(mm):02d}:00Z"


def _parse_ensit(row: dict) -> dict | None:
    """ENSIT-XXXXXXX → IDXEnemySituationalAwarenessReport-shaped record. Handles
    two prose shapes: legacy "UIC <K> at <lat>N<lon>E; role <R>; echelon <E>; DTG <T>."
    and 515224b rollup "<N> enemy units + <M> equipment observations... UNITS: <DESIG>
    (UIC <K>, SIDC <S>) at <lat>N<lon>E parent <P>; ... MSGID .../<reporter>/<ser>. DTG <T>."
    The rollup carries no explicit role/echelon — we derive echelon from the unit
    designation suffix (BDE/BN/BTY/CO/PLT/...) and leave role blank."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    subject = row.get("subject", "") or ""
    details = row.get("details", "") or ""
    subj_m = _ENSIT_SUBJECT_RE.search(subject)
    det_m  = _ENSIT_DETAILS_RE.search(details)
    if subj_m and det_m:
        lat = _ddmmss_decimal_to_decimal(det_m.group("lat_d"), det_m.group("lat_m"), det_m.group("lat_s"))
        lon = _ddmmss_decimal_to_decimal(det_m.group("lon_d"), det_m.group("lon_m"), det_m.group("lon_s"))
        designation = subj_m.group("designation").strip()
        return {
            "_objectType": "IDXEnemySituationalAwarenessReport",
            "_source": "scenario_fixture",
            "_source_report_id": row.get("id"),
            "_source_msg_serial": serial,
            "uuid": _stable_uuid(f"ensit-{serial}"),
            "ref_id": serial,
            "enemy_unit_master_key": det_m.group("uic"),
            "unit_designation": designation,
            "symbol_id": subj_m.group("symbol"),
            "functional_role": det_m.group("role"),
            "echelon": det_m.group("echelon"),
            "lat": lat,
            "lon": lon,
            "enemy_observation_time": _ensit_dtg_to_iso(det_m.group("dtg")),
            "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
            "reporter": row.get("reporter", ""),
            "subject": subject,
        }
    # Rollup shape — pull the first UNITS entry as the representative anchor.
    roll_m = _ENSIT_ROLLUP_RE.search(details)
    if not roll_m:
        return None
    lat = _ddmmss_decimal_to_decimal(roll_m.group("lat_d"), roll_m.group("lat_m"), roll_m.group("lat_s"))
    lon = _ddmmss_decimal_to_decimal(roll_m.group("lon_d"), roll_m.group("lon_m"), roll_m.group("lon_s"))
    designation = roll_m.group("designation").strip()
    # Derive echelon from the designation suffix; the rollup shape doesn't
    # carry an explicit echelon field.
    echelon = ""
    designation_tokens = designation.split()
    for token in reversed(designation_tokens):
        if token in _ENSIT_ECHELON_TOKENS:
            echelon = token
            break
    dtg_m = _ENSIT_ROLLUP_DTG_RE.search(details)
    return {
        "_objectType": "IDXEnemySituationalAwarenessReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"ensit-{serial}"),
        "ref_id": serial,
        "enemy_unit_master_key": roll_m.group("uic"),
        "unit_designation": designation,
        "symbol_id": roll_m.group("symbol"),
        "functional_role": "",  # rollup shape doesn't carry an explicit role
        "echelon": echelon,
        "lat": lat,
        "lon": lon,
        "enemy_observation_time": _ensit_dtg_to_iso(dtg_m.group("dtg")) if dtg_m else "",
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": subject,
    }


# ---------------------------------------------------------------------------
# SPOTREP-family → IDXSensorContactReport
# ---------------------------------------------------------------------------
# EOBSREP: "First contact at <MGRS> (<ROLE>); obs time <DTG>; equipment sample: <LIST>."
# SENSOREP_OPIR: "First event at <MGRS> (FIRED|DETNTD); obs time <DTG>; ."
# SIGACT / INFRA-SIGACT: free-form narrative — extract MGRS if present, use details verbatim as activity.

_EOBSREP_DETAILS_RE = re.compile(
    r"First contact at\s+(?P<mgrs>[0-9]{1,2}[A-Z]{1,3}\d+)\s*\((?P<role>[A-Z]+)\);\s*"
    r"obs time\s+(?P<dtg>\d{6,12}[A-Z][A-Z]{3}\d{4});"
    r"(?:\s*equipment sample:\s*(?P<equipment>.*?))?\.?$"
)

# 515224b rollup shape: "<N> ENACTLOC (enemy )?observations by <REPORTER> in MGRS grid <GRID> area (exercise ...)"
# or "<N> ENACTLOC observations by <REPORTER> (USMTF txt, exercise ...). Per-ENACTLOC[ equipment]: <MGRS>: <eq>; <MGRS>: <eq>; ..."
# We pull the first per-ENACTLOC MGRS as the representative anchor, count as activity,
# and pull the trailing MSGID DTG (if present) for observation_time.
_EOBSREP_ROLLUP_RE = re.compile(
    r"^(?P<count>\d+)\s+ENACTLOC\s+(?:enemy\s+)?observations?\s+by\s+(?P<reporter>.+?)\s+"
    r"(?:in\s+MGRS grid\s+(?P<grid>[0-9]{1,2}[A-Z]{1,3})\s+area\s+)?"
    r"\((?:USMTF txt,\s*)?exercise[^)]*\)",
    re.IGNORECASE,
)
_EOBSREP_FIRST_PER_ENACTLOC_RE = re.compile(
    r"Per-ENACTLOC(?:\s+equipment)?:\s*(?P<mgrs>[0-9]{1,2}[A-Z]{1,3}\d+):\s*(?P<equipment>[^;]+)"
)
_EOBSREP_MSGID_DTG_RE = re.compile(
    r"MSGID\s+\S+\.\s*DTG\s+(?P<dtg>\d{6,12}[A-Z][A-Z]{3}\d{4})"
)
_SENSOREP_OPIR_DETAILS_RE = re.compile(
    r"First event at\s+(?P<mgrs>[0-9]{1,2}[A-Z]{1,3}\d+)\s*\((?P<event>FIRED|DETNTD|[A-Z]+)\);\s*"
    r"obs time\s+(?P<dtg>\d{6,12}[A-Z][A-Z]{3}\d{4});"
)
_SIGACT_MGRS_RE = re.compile(r"(?:IVO\s+)?MGRS:?\s*(?P<mgrs>[0-9]{1,2}[A-Z]{1,3}\d+)")


def _parse_eobsrep(row: dict) -> dict | None:
    """EOBSREP-XXXXXX → IDXSensorContactReport (per-report representative;
    one fixture row carries N locations but exposes only the first to the
    writer — the equipment-sample text rolls the rest). Handles two prose
    shapes: legacy "First contact at <MGRS>..." and 515224b rollup
    "<N> ENACTLOC observations by <reporter>... Per-ENACTLOC: <MGRS>: <eq>..."."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details = row.get("details", "") or ""
    det_m = _EOBSREP_DETAILS_RE.search(details)
    if det_m:
        return {
            "_objectType": "IDXSensorContactReport",
            "_source": "scenario_fixture",
            "_source_report_id": row.get("id"),
            "_source_msg_serial": serial,
            "uuid": _stable_uuid(f"eobsrep-{serial}"),
            "ref_id": serial,
            "mgrs": det_m.group("mgrs"),
            "enemy_unit": row.get("subject", ""),
            "activity": (det_m.group("role") or "").strip() + (
                f" — equipment: {det_m.group('equipment')}" if det_m.group("equipment") else ""
            ),
            "observation_time": _ensit_dtg_to_iso(det_m.group("dtg")),
            "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
            "reporter": row.get("reporter", ""),
            "subject": row.get("subject", ""),
        }
    # Rollup shape — pull representative MGRS + equipment from first Per-ENACTLOC entry.
    rollup_m = _EOBSREP_ROLLUP_RE.match(details)
    first_m = _EOBSREP_FIRST_PER_ENACTLOC_RE.search(details)
    if not (rollup_m or first_m):
        return None
    mgrs = (first_m.group("mgrs") if first_m else "") or (rollup_m.group("grid") if rollup_m else "")
    equipment = first_m.group("equipment").strip() if first_m else ""
    count = rollup_m.group("count") if rollup_m else ""
    msgid_m = _EOBSREP_MSGID_DTG_RE.search(details)
    activity_bits = []
    if count:
        activity_bits.append(f"{count} ENACTLOC observations")
    if equipment:
        activity_bits.append(f"first location equipment: {equipment}")
    return {
        "_objectType": "IDXSensorContactReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"eobsrep-{serial}"),
        "ref_id": serial,
        "mgrs": mgrs,
        "enemy_unit": row.get("subject", ""),
        "activity": " — ".join(activity_bits) if activity_bits else "ENACTLOC observations",
        "observation_time": _ensit_dtg_to_iso(msgid_m.group("dtg")) if msgid_m else "",
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


def _parse_sensorep_opir(row: dict) -> dict | None:
    """SENSOREP-OPIR-XXXXXX → IDXSensorContactReport (POO/POI fires)."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    det_m = _SENSOREP_OPIR_DETAILS_RE.search(row.get("details", "") or "")
    if not det_m:
        return None
    return {
        "_objectType": "IDXSensorContactReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"sensorep-opir-{serial}"),
        "ref_id": serial,
        "mgrs": det_m.group("mgrs"),
        "enemy_unit": row.get("subject", ""),
        "activity": f"OPIR {det_m.group('event')} event",
        "observation_time": _ensit_dtg_to_iso(det_m.group("dtg")),
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


def _parse_sigact(row: dict) -> dict | None:
    """SIGACT / INFRASTRUCTURE_SIGACT → IDXSensorContactReport. Free-form
    narrative — pass details verbatim as activity; pull MGRS if present."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details = row.get("details", "") or ""
    mgrs_m = _SIGACT_MGRS_RE.search(details)
    return {
        "_objectType": "IDXSensorContactReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"sigact-{serial}"),
        "ref_id": serial,
        "mgrs": mgrs_m.group("mgrs") if mgrs_m else "",
        "enemy_unit": "",
        "activity": details,
        "observation_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


# ---------------------------------------------------------------------------
# IIR → IDXHumintReport
# ---------------------------------------------------------------------------
# IIR rows in scenario.report:
#   id='IIR-IIR-2-980-0001-26', subject='IDP Debrief',
#   details='SUBJECT: IIR 2 980 0001 26/IIR 2 980 0001 26/<Report Title> (U) Russia',
#   _serial='IIR 2 980 0001 26'
# The narrative body is sparse in fixture; rich content arrives via the
# manual_injects folder when organizers ship HUMINT IIRs. Until then we
# surface what we have — the writer can still cite an IIR exists.

def _parse_iir(row: dict) -> dict | None:
    serial = row.get("_serial") or row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    return {
        "_objectType": "IDXHumintReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"iir-{serial}"),
        "ref_id": serial,
        "report_type": "IIR",
        "report_id": serial,
        "summary": row.get("subject", ""),
        "raw_text": row.get("details", ""),
        "source_type": "HUMINT",
        "source_name": row.get("reporter", "HUMINT IIR"),
        "reliability_tier": "",
        "report_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


def _parse_osint(row: dict) -> dict | None:
    """OSINT-XXXX scenario row → IDXOpenSourceReport-shaped record.

    The scenario.KG['report'] row already carries the per-event fields the
    OSIR writer needs (source_name, body_text, location_mgrs, pir_alignment,
    cross_cueing_recommendation, etc.); this parser passes them through to
    the vertex shape and stamps a deterministic synthetic UUID so the poller's
    new-kbid delta logic doesn't re-fire on every tick.

    Mirrors the cougaar IDXOpenSourceReport vertex 1:1 — same field names,
    so when live cougaar adds the vertex, the writer prompt path is
    unchanged.
    """
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None

    def _get(*keys, default=""):
        """First non-empty value across the candidate keys."""
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                return v
        return default

    # Geo: prefer explicit lat/lon if the fixture row provides them.
    lat = row.get("lat")
    lon = row.get("lon")
    location = None
    if lat is not None and lon is not None:
        location = {"latitude": float(lat), "longitude": float(lon)}

    return {
        "_objectType": "IDXOpenSourceReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"osint-{serial}"),
        "ref_id": serial,
        # Header
        "report_id": _get("report_id", "id"),
        "subject": _get("subject"),
        "country_region": _get("country_region"),
        "publication_time": _get("publication_dtg", "publication_time"),
        "acquisition_time": _get("acquisition_dtg", "acquisition_time", "dtg"),
        "capture_time": _get("capture_dtg", "capture_time"),
        # §1 Source description + reliability (ICD 203)
        "source_type": _get("source_type"),
        "source_name": _get("source_name"),
        "source_channel": _get("source_channel"),
        "source_account_or_org": _get("source_account_or_org"),
        "source_url": _get("source_url"),
        "source_reliability_icd203": _get("source_reliability_icd203"),
        "source_reliability_rationale": _get("source_reliability_rationale"),
        "information_credibility": _get("information_credibility"),
        "information_credibility_rationale": _get("information_credibility_rationale"),
        # §2 + §3 Body content
        "summary": _get("summary"),
        "body_text": _get("body_text", "details"),
        "body_text_original_language": _get("body_text_original_language"),
        "translation_note": _get("translation_note"),
        "media_description": _get("media_description"),
        # §4 Geospatial + technical metadata
        "lat": lat,
        "lon": lon,
        "location": location,
        "location_mgrs": _get("location_mgrs", "mgrs"),
        "location_named_landmark": _get("location_named_landmark"),
        "tactical_markings": _get("tactical_markings"),
        # §5 Collector's comments
        "pir_alignment": _get("pir_alignment"),
        "contextual_note": _get("contextual_note"),
        "cross_cueing_recommendation": _get("cross_cueing_recommendation"),
        # Bookkeeping
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
    }


# ---------------------------------------------------------------------------
# TIDE → IDXSignalOfInterestReport (SIGINT-class — TIDE cart products carry
# per-signal cuts with frequency, bandwidth, geo measurement, confidence).
# ---------------------------------------------------------------------------
# Sample: "TIDE cart <UUID> (PRODUCT, version 1.8.0, classif U, ts 2026-05-04T15:35:10.903Z); 1 product(s):
#         product <UUID> (producer Mission 1): signal_id <UUID> freq [49735000.0]Hz bw 10000.0Hz CENOT CC222;
#         measurements: geo@<ISO> lat=56.071733 lon=23.618893 alt=112.024364m, ellipse sma=... confidence=90.0%."
_TIDE_DETAILS_RE = re.compile(
    r"product\s+(?P<product_id>[0-9a-f\-]{16,})\s*\(producer\s+(?P<producer>[^)]+)\):\s*"
    r"signal_id\s+(?P<signal_id>[0-9a-f\-]{16,})\s+"
    r"freq\s+\[(?P<freq_hz>[\d.]+)\]Hz\s+"
    r"bw\s+(?P<bw_hz>[\d.]+)Hz\s+"
    r"CENOT\s+(?P<cenot>\S+);\s*"
    r"measurements:\s*geo@(?P<obs_iso>[\d\-T:.Z]+)\s+"
    r"lat=(?P<lat>-?[\d.]+)\s+lon=(?P<lon>-?[\d.]+)"
    r"(?:\s+alt=(?P<alt>-?[\d.]+)m)?"
    r"(?:.*?confidence=(?P<conf>[\d.]+)%)?",
    re.IGNORECASE | re.DOTALL,
)
_TIDE_CART_RE = re.compile(
    r"TIDE cart\s+(?P<cart_id>[0-9a-f\-]{16,})\s*\([^)]*ts\s+(?P<cart_iso>[\d\-T:.Z]+)\s*\)"
)


def _parse_tide(row: dict) -> dict | None:
    """TIDE-<UUID> → IDXSignalOfInterestReport (TIDE cart product = SIGINT cut).
    Each TIDE row carries one or more `product (...)` blocks; we surface the
    first product's signal as the representative record (frequency, bandwidth,
    CENOT notation, geo + alt + confidence). Frequency goes to MHz to match
    the SignalOfInterest field convention."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details = row.get("details", "") or ""
    det_m = _TIDE_DETAILS_RE.search(details)
    if not det_m:
        return None
    cart_m = _TIDE_CART_RE.search(details)
    freq_hz = float(det_m.group("freq_hz"))
    bw_hz = float(det_m.group("bw_hz"))
    lat = float(det_m.group("lat"))
    lon = float(det_m.group("lon"))
    alt = float(det_m.group("alt")) if det_m.group("alt") else None
    confidence = float(det_m.group("conf")) if det_m.group("conf") else None
    return {
        "_objectType": "IDXSignalOfInterestReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"tide-{serial}"),
        "ref_id": serial,
        "signal_id": det_m.group("signal_id"),
        "emitter_name": det_m.group("cenot"),  # CENOT designator is the doctrinal emitter notation
        "platform_or_sensor_id": det_m.group("producer").strip(),
        "frequency_mhz": round(freq_hz / 1_000_000.0, 6),
        "bandwidth_hz": bw_hz,
        "modulation": "",
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "altitude_m": alt,
        "confidence_pct": confidence,
        "cart_id": cart_m.group("cart_id") if cart_m else "",
        "start_time": det_m.group("obs_iso"),
        "end_time": det_m.group("obs_iso"),
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


# ---------------------------------------------------------------------------
# CMF_TACREP_COMEW → IDXCommunicationsData (IEWTPT CMF intercept package —
# COMINT-class). Distinct from TACREP_COMEW: arc-second lat/lon offsets,
# Julian day timestamp, package number in id/subject, CENOT in subject.
# ---------------------------------------------------------------------------
# CMF package — two prose shapes, both in 515224b:
#   (a) Compact: "Frequency 73605000Hz; location lat 127048sec, lon -419810sec; Julian day 106, year offset 126."
#   (b) Binary annotation: "CMF binary intercept pkg 1411: src_msg_typ ESP_SENSOREP, environ GRU,
#       provider_community SIGNT, data_categ COMNT. Loc lat 127048sec lon -419810sec (err ellipse ...);
#       freq 73605000Hz. CENOT AB456. Clk_Time 50214s, Julian day 106, year offset 1900+126=2026. ..."
# Common fields are frequency (Hz), lat/lon arc-seconds, Julian day, year offset — we accept both.
_CMF_COMEW_DETAILS_RE = re.compile(
    r"(?:"
    r"Frequency\s+(?P<freq_hz_a>[\d.]+)Hz;\s*"
    r"location\s+lat\s+(?P<lat_sec_a>-?[\d.]+)sec,\s*lon\s+(?P<lon_sec_a>-?[\d.]+)sec;\s*"
    r"Julian day\s+(?P<jday_a>\d+),\s*year offset\s+(?P<yr_offset_a>\d+)"
    r"|"
    r"Loc\s+lat\s+(?P<lat_sec_b>-?[\d.]+)sec\s+lon\s+(?P<lon_sec_b>-?[\d.]+)sec.*?"
    r"freq\s+(?P<freq_hz_b>[\d.]+)Hz.*?"
    r"Julian day\s+(?P<jday_b>\d+),\s*year offset\s+(?:1900\+)?(?P<yr_offset_b>\d+)"
    r")",
    re.DOTALL,
)
_CMF_COMEW_SUBJECT_RE = re.compile(
    r"CMF intercept pkg\s+(?P<pkg>\S+)\s*\((?P<discipline>[A-Z]+)\)\s*[—–-]\s*CENOT\s+(?P<cenot>\S+)"
)
# Binary-shape rows often have NO subject CENOT — pull from details body instead.
_CMF_COMEW_BINARY_CENOT_RE = re.compile(r"CENOT\s+(?P<cenot>\S+?)\.")
_CMF_COMEW_BINARY_PKG_RE = re.compile(r"CMF binary intercept pkg\s+(?P<pkg>\S+):")


def _parse_cmf_comew(row: dict) -> dict | None:
    """CMF-<PKG> → IDXCommunicationsData (CMF IEWTPT package). Lat/lon arrive as
    arc-second offsets — convert to decimal degrees (sec / 3600). The Julian
    day / year offset combine into an ISO date (year = 1900 + year_offset, then
    day-of-year). Bandwidth is not in the CMF prose — leave blank."""
    serial = row.get("_msg_serial") or row.get("_pkg") or row.get("id")
    if not serial:
        return None
    details = row.get("details", "") or ""
    det_m = _CMF_COMEW_DETAILS_RE.search(details)
    if not det_m:
        return None
    # Resolve fields from whichever alternation branch matched.
    freq_hz = float(det_m.group("freq_hz_a") or det_m.group("freq_hz_b"))
    lat_sec = float(det_m.group("lat_sec_a") or det_m.group("lat_sec_b"))
    lon_sec = float(det_m.group("lon_sec_a") or det_m.group("lon_sec_b"))
    jday = int(det_m.group("jday_a") or det_m.group("jday_b"))
    yr_offset = int(det_m.group("yr_offset_a") or det_m.group("yr_offset_b"))
    lat = round(lat_sec / 3600.0, 6)
    lon = round(lon_sec / 3600.0, 6)
    # Julian day + year offset → ISO date. The IEWTPT convention is
    # year = 1900 + year_offset (so 126 → 2026), day-of-year = jday.
    try:
        import datetime as _dt
        obs_iso = (_dt.date(1900 + yr_offset, 1, 1) + _dt.timedelta(days=jday - 1)).isoformat() + "T00:00:00Z"
    except Exception:
        obs_iso = ""
    # Subject parse covers compact (a) shape; binary (b) carries pkg/CENOT in details.
    subj_m = _CMF_COMEW_SUBJECT_RE.search(row.get("subject", "") or "")
    if subj_m:
        pkg = subj_m.group("pkg")
        discipline = subj_m.group("discipline")
        cenot = subj_m.group("cenot")
    else:
        bin_pkg_m = _CMF_COMEW_BINARY_PKG_RE.search(details)
        bin_cenot_m = _CMF_COMEW_BINARY_CENOT_RE.search(details)
        pkg = bin_pkg_m.group("pkg") if bin_pkg_m else (row.get("_pkg") or "")
        cenot = bin_cenot_m.group("cenot") if bin_cenot_m else ""
        # data_categ in binary shape is the doctrinal discipline ("COMNT").
        discipline_m = re.search(r"data_categ\s+(\S+)", details)
        discipline = discipline_m.group(1) if discipline_m else "COMNT"
    return {
        "_objectType": "IDXCommunicationsData",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"cmf-comew-{serial}"),
        "ref_id": serial,
        "intercepted_role": "",  # CMF package doesn't carry intercepted-unit role
        "package_number": pkg,
        "discipline": discipline,
        "ew_callsign": cenot,
        "frequency_mhz": round(freq_hz / 1_000_000.0, 6),
        "frequency_raw": f"{freq_hz}Hz",
        "bandwidth": "",
        "lat": lat,
        "lon": lon,
        "reported_time": obs_iso or _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


# ---------------------------------------------------------------------------
# IMG_IIR / IMG_RECCEXREP → IDXImageIirReport (IMINT — image intelligence).
# Items + locations + comments + MSGID/DTG trailer.
# ---------------------------------------------------------------------------
# Sample IMG_IIR: "IMG IIR (image intelligence) report: <N> target items observed.
#                  Items: item001 <NAME>; item002 <NAME>; ...
#                  Locations: GEO:<DMSS>N<DMSS>E UTM:<MGRS>; ...
#                  Comments: <TEXT>.
#                  Exercise <EX>. MSGID IIR/<reporter>/<serial>. DTG <DDHHMMZmmmYYYY>. Source: <FILE>."
# Sample IMG_RECCEXREP: same prefix shape with "RECCEXREP (reconnaissance exception report)" and "Target info:".
_IMG_ITEMS_RE = re.compile(r"Items:\s*(?P<items>.+?)(?=\.\s*Locations:)", re.DOTALL)
_IMG_LOCATIONS_RE = re.compile(
    r"Locations:\s*(?P<locations>.+?)(?=\.\s*(?:Comments|Target info|Exercise|MSGID))",
    re.DOTALL,
)
_IMG_FIRST_GEO_RE = re.compile(
    r"GEO:(?P<lat_d>\d{2})(?P<lat_m>\d{2})(?P<lat_s>\d{2}(?:\.\d+)?)N"
    r"(?P<lon_d>\d{3})(?P<lon_m>\d{2})(?P<lon_s>\d{2}(?:\.\d+)?)E"
    r"\s+UTM:(?P<mgrs>[0-9A-Z]+)"
)
_IMG_COMMENTS_RE = re.compile(r"Comments:\s*(?P<comments>.+?)(?=\.\s*(?:Exercise|MSGID))", re.DOTALL)
_IMG_TARGET_INFO_RE = re.compile(r"Target info:\s*(?P<info>.+?)(?=\.\s*(?:Exercise|MSGID))", re.DOTALL)
_IMG_MSGID_RE = re.compile(r"MSGID\s+(?P<msgid>\S+?)\.\s*DTG\s+(?P<dtg>\d{6,12}[A-Z][A-Z]{3}\d{4})")
_IMG_EXERCISE_RE = re.compile(r"Exercise\s+(?P<ex>[^.]+?)\.\s*MSGID")
_IMG_COUNT_RE = re.compile(r"(?P<count>\d+)\s+target items")


def _parse_img_report(row: dict, *, kind: str) -> dict | None:
    """Shared IMG_IIR / IMG_RECCEXREP parser → IDXImageIirReport. kind is "IIR"
    or "RECCEXREP" — drives report_type and the comments-vs-target-info choice.
    Pulls the first GEO/UTM pair as the representative anchor; rolls items +
    comments into highlights for writer narration."""
    serial = row.get("_msg_serial") or row.get("id")
    if not serial:
        return None
    details = row.get("details", "") or ""
    geo_m = _IMG_FIRST_GEO_RE.search(details)
    if not geo_m:
        return None
    items_m = _IMG_ITEMS_RE.search(details)
    comments_m = _IMG_COMMENTS_RE.search(details) if kind == "IIR" else _IMG_TARGET_INFO_RE.search(details)
    msgid_m = _IMG_MSGID_RE.search(details)
    ex_m = _IMG_EXERCISE_RE.search(details)
    count_m = _IMG_COUNT_RE.search(details)
    lat = _ddmmss_decimal_to_decimal(geo_m.group("lat_d"), geo_m.group("lat_m"), geo_m.group("lat_s"))
    lon = _ddmmss_decimal_to_decimal(geo_m.group("lon_d"), geo_m.group("lon_m"), geo_m.group("lon_s"))
    items_text = (items_m.group("items").strip() if items_m else "") or ""
    comments_text = (comments_m.group("comments" if kind == "IIR" else "info").strip()
                     if comments_m else "")
    highlights_bits = []
    if count_m:
        highlights_bits.append(f"{count_m.group('count')} target items")
    if items_text:
        highlights_bits.append(f"items: {items_text}")
    if comments_text:
        label = "comments" if kind == "IIR" else "target_info"
        highlights_bits.append(f"{label}: {comments_text}")
    return {
        "_objectType": "IDXImageIirReport",
        "_source": "scenario_fixture",
        "_source_report_id": row.get("id"),
        "_source_msg_serial": serial,
        "uuid": _stable_uuid(f"img-{kind.lower()}-{serial}"),
        "ref_id": serial,
        "serial": serial,
        "item_type": kind,
        "report_type": kind,
        "imagery_mission_number": msgid_m.group("msgid") if msgid_m else "",
        "project_identifier_code": ex_m.group("ex").strip() if ex_m else "",
        "highlights": " — ".join(highlights_bits),
        "mgrs": geo_m.group("mgrs"),
        "lat": lat,
        "lon": lon,
        "report_time": _ensit_dtg_to_iso(msgid_m.group("dtg")) if msgid_m else "",
        "reported_time": _hhmmss_z_to_iso(row.get("dtg", "")),
        "reporter": row.get("reporter", ""),
        "subject": row.get("subject", ""),
    }


def _parse_img_iir(row: dict) -> dict | None:
    """IMG-IIR-<serial> → IDXImageIirReport."""
    return _parse_img_report(row, kind="IIR")


def _parse_img_recceexrep(row: dict) -> dict | None:
    """IMG-RECCEX-<serial> → IDXImageIirReport (RECCEXREP variant — Target info
    instead of Comments, otherwise same shape)."""
    return _parse_img_report(row, kind="RECCEXREP")


_TYPE_PARSERS = {
    "TACELINT": _parse_tacelint,
    "WRANGLER": _parse_wrangler,
    "TACREP_EMTREW": _parse_emtrew,
    "TACREP_COMEW": _parse_comint,
    "CMF_TACREP_COMEW": _parse_cmf_comew,
    "ENSIT": _parse_ensit,
    "EOBSREP": _parse_eobsrep,
    "SENSOREP_OPIR": _parse_sensorep_opir,
    "SIGACT": _parse_sigact,
    "INFRASTRUCTURE_SIGACT": _parse_sigact,
    "IIR": _parse_iir,
    "OSINT": _parse_osint,
    "TIDE": _parse_tide,
    "IMG_IIR": _parse_img_iir,
    "IMG_RECCEXREP": _parse_img_recceexrep,
}


def build_fixture_raw_records(reports: list[dict]) -> dict[str, dict]:
    """Parse the scenario.KG['report'] list into a dict of synthetic kbID →
    structured raw record matching the cougaar IDX vertex shape. Caller
    merges this into `raw_by_kbid` after the cougaar fetch.

    Returns an empty dict if `reports` is empty / None. Rows that don't
    match any parser are silently skipped — the caller still has the
    original fixture row in scenario.KG['report'] for the writer's
    fallback narration.
    """
    out: dict[str, dict] = {}
    if not reports:
        return out
    for row in reports:
        if not isinstance(row, dict):
            continue
        parser = _TYPE_PARSERS.get(row.get("type"))
        if parser is None:
            continue
        try:
            rec = parser(row)
        except Exception:  # never break the cache build over one malformed row
            continue
        if rec and rec.get("uuid"):
            out[rec["uuid"]] = rec
    return out
