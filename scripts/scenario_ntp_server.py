"""Scenario-frozen NTP server (RFC 5905 v3/v4 compatible).

Answers every NTP query with the FROZEN scenario time fetched from the
docker-api container's `/api/scenario-clock` endpoint. Other containers /
analyst laptops point their NTP client at this host and get scenario time
instead of host wall-clock.

This is the wire-protocol companion to `utils.scenario_clock` — the same
authoritative anchor, exposed over UDP/123 so non-Python clients (geo-agent,
tileserver-gl, the analyst SPA's JS Date() interrogation, OS-level chronyd
on analyst boxes) can sync.

Design choices:
  - Stratum 1 with refid 'SCEN' (4 bytes) so clients accept our authority.
  - Frozen time refreshes from /api/scenario-clock on each request — cheap
    HTTP cost, ensures NTP responses follow refreezes triggered by
    manual_injects landing a fresh OPORD.
  - LI (leap indicator) = 0 (no warning); VN (version) = 4; Mode = server.
  - No symmetric / broadcast / multicast — request/response unicast only.

Run inside the dedicated `scenario-ntp` docker-compose service. The container
binds UDP/123 via NET_BIND_SERVICE capability (no root needed).
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import sys
import threading
import time

import urllib.request
import json

log = logging.getLogger("scenario_ntp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# NTP epoch is 1900-01-01; UNIX epoch is 1970-01-01. Offset = 70 years.
NTP_EPOCH_OFFSET_SECONDS = 2208988800

# Endpoint inside docker network. docker-api exposes the scenario clock on
# port 8000; the FastAPI route is /api/scenario-clock.
SCENARIO_CLOCK_URL = os.environ.get(
    "SCENARIO_CLOCK_URL",
    "http://docker-api:8000/api/scenario-clock",
)
SCENARIO_CLOCK_FETCH_TIMEOUT = 3
SCENARIO_CLOCK_REFRESH_SECONDS = 5  # cache hits/misses balance

_CACHE_LOCK = threading.Lock()
_CACHED_FROZEN_UNIX_SECONDS: float | None = None
_CACHED_AT_REAL_UNIX_SECONDS: float = 0.0


def _fetch_frozen_unix_seconds() -> float:
    """Hit /api/scenario-clock and return the frozen UNIX time in seconds."""
    req = urllib.request.Request(SCENARIO_CLOCK_URL)
    with urllib.request.urlopen(req, timeout=SCENARIO_CLOCK_FETCH_TIMEOUT) as resp:
        payload = json.loads(resp.read())
    unix_ms = payload.get("unix_ms")
    if not isinstance(unix_ms, (int, float)):
        raise RuntimeError(f"scenario-clock endpoint returned non-numeric unix_ms: {payload!r}")
    return float(unix_ms) / 1000.0


def _frozen_seconds_cached() -> float:
    """Return the frozen scenario time in UNIX seconds, refreshing the cache
    every SCENARIO_CLOCK_REFRESH_SECONDS to follow refreezes. On HTTP fail
    we surface the last good value rather than wall-clock — clients depend
    on us being the authority."""
    global _CACHED_FROZEN_UNIX_SECONDS, _CACHED_AT_REAL_UNIX_SECONDS
    now_real = time.time()
    with _CACHE_LOCK:
        if (_CACHED_FROZEN_UNIX_SECONDS is None
                or (now_real - _CACHED_AT_REAL_UNIX_SECONDS) > SCENARIO_CLOCK_REFRESH_SECONDS):
            try:
                _CACHED_FROZEN_UNIX_SECONDS = _fetch_frozen_unix_seconds()
                _CACHED_AT_REAL_UNIX_SECONDS = now_real
                log.debug("refreshed cached frozen=%s", _CACHED_FROZEN_UNIX_SECONDS)
            except Exception as e:
                log.error("scenario-clock fetch failed (%s); serving last cached", e)
        if _CACHED_FROZEN_UNIX_SECONDS is None:
            raise RuntimeError("no cached scenario clock yet; first fetch failed")
        return _CACHED_FROZEN_UNIX_SECONDS


def _unix_to_ntp_timestamp(unix_seconds: float) -> tuple[int, int]:
    """UNIX seconds → (NTP seconds, NTP fraction) per RFC 5905 §6 figure 4."""
    ntp_seconds_float = unix_seconds + NTP_EPOCH_OFFSET_SECONDS
    secs = int(ntp_seconds_float)
    frac = int((ntp_seconds_float - secs) * (1 << 32))
    return secs, frac


def _build_ntp_response(request_packet: bytes) -> bytes:
    """Build a stratum-1 NTP server response for the given client request.

    Layout (RFC 5905 §7.3, 48-byte fixed header):
      byte 0:        LI (2 bits) | VN (3 bits) | Mode (3 bits)
      byte 1:        Stratum
      byte 2:        Poll (signed int8)
      byte 3:        Precision (signed int8)
      bytes  4-7:    Root Delay (NTP short)
      bytes  8-11:   Root Dispersion (NTP short)
      bytes 12-15:   Reference ID (4 bytes — 'SCEN' for scenario clock)
      bytes 16-23:   Reference Timestamp (NTP timestamp, 64-bit)
      bytes 24-31:   Origin Timestamp (echo of client's Transmit Timestamp)
      bytes 32-39:   Receive Timestamp
      bytes 40-47:   Transmit Timestamp
    """
    if len(request_packet) < 48:
        raise ValueError("NTP request too short")
    # Pull client's transmit timestamp (bytes 40-47) to echo as origin.
    client_tx_secs, client_tx_frac = struct.unpack("!II", request_packet[40:48])

    frozen_unix = _frozen_seconds_cached()
    ref_secs, ref_frac = _unix_to_ntp_timestamp(frozen_unix)
    # In a frozen-clock model receive ≡ transmit ≡ reference. RFC allows
    # this; clients use the (transmit, origin) pair to compute round-trip
    # delay and offset.
    rx_secs, rx_frac = ref_secs, ref_frac
    tx_secs, tx_frac = ref_secs, ref_frac

    LI = 0
    VN = 4
    Mode = 4   # server
    li_vn_mode = (LI << 6) | (VN << 3) | Mode
    stratum = 1
    poll = 6           # 64 s, conventional
    precision = -20    # ~1 microsecond — frozen clock is exact

    root_delay = 0
    root_dispersion = 0
    # Reference ID for stratum-1: 4 ASCII chars. 'SCEN' = scenario clock.
    ref_id = b"SCEN"

    packet = bytearray(48)
    packet[0] = li_vn_mode
    packet[1] = stratum
    packet[2] = poll
    packet[3] = precision & 0xFF  # signed int8 → bit-cast to byte
    struct.pack_into("!II", packet, 4, root_delay, root_dispersion)
    packet[12:16] = ref_id
    struct.pack_into("!II", packet, 16, ref_secs, ref_frac)
    struct.pack_into("!II", packet, 24, client_tx_secs, client_tx_frac)
    struct.pack_into("!II", packet, 32, rx_secs, rx_frac)
    struct.pack_into("!II", packet, 40, tx_secs, tx_frac)
    return bytes(packet)


def serve_forever(bind_host: str = "0.0.0.0", bind_port: int = 123) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_host, bind_port))
    log.info("scenario-ntp listening on %s:%d (clock src=%s)",
             bind_host, bind_port, SCENARIO_CLOCK_URL)
    # Warm cache before serving.
    try:
        _frozen_seconds_cached()
    except Exception as e:
        log.warning("initial scenario-clock fetch failed: %s (will retry on requests)", e)

    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except OSError as e:
            log.error("recvfrom failed: %s", e)
            continue
        try:
            resp = _build_ntp_response(data)
            sock.sendto(resp, addr)
        except Exception as e:
            log.warning("dropped malformed NTP request from %s: %s", addr, e)


if __name__ == "__main__":
    port = int(os.environ.get("NTP_BIND_PORT", "123"))
    host = os.environ.get("NTP_BIND_HOST", "0.0.0.0")
    try:
        serve_forever(host, port)
    except KeyboardInterrupt:
        log.info("scenario-ntp shutting down")
        sys.exit(0)
