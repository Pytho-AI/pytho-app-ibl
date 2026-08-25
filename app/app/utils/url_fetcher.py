"""Protocol-agnostic URL → bytes fetcher.

GRVTY's BDA imagery URLs (IDXImageryReport.imagery_url) could land as
any scheme — sim staff didn't commit to one when they added the field
2026-06-10. This utility dispatches by URL scheme so the BDA chain
doesn't need a per-protocol branch.

Schemes handled:
  http, https     — via httpx (already a pytho-app dependency)
  file            — local filesystem read
  ftp             — via urllib.request (stdlib)
  s3              — via boto3 if installed, otherwise URLFetchError
  smb             — via smbprotocol if installed, otherwise smbclient
                     subprocess; falls back to URLFetchError when
                     neither is available. URL form:
                     smb://[user[:pass]@]host/share/path/to/file
  (no scheme)     — treated as local file path (covers raw paths
                     like /mnt/grvty/img_001.jpg or smb mounts)

Returns the file bytes. Raises URLFetchError on any failure (network,
auth, file-not-found, unsupported scheme) so the caller can log + skip
without crashing the chain.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen as _urlopen

import httpx

log = logging.getLogger(__name__)


# Default timeout (s) for any network fetch. Image files for BDA are
# typically 100kB-2MB; 30s is generous for a hotel/SCIF network.
DEFAULT_TIMEOUT_SEC = 30.0


class URLFetchError(Exception):
    """Raised when fetch_bytes can't deliver bytes for any reason —
    network failure, auth failure, file-not-found, unsupported scheme.
    Caller logs + continues."""


def fetch_bytes(url: str, timeout: float = DEFAULT_TIMEOUT_SEC) -> bytes:
    """Fetch the resource at `url` as bytes, dispatching by URL scheme.

    Raises URLFetchError on any failure. Never returns empty bytes for
    a successful fetch (a zero-byte file at the URL is still a
    URLFetchError — the caller wouldn't want to embed a 0-byte image).
    """
    if not url or not isinstance(url, str):
        raise URLFetchError(f"fetch_bytes: invalid url {url!r}")
    url = url.strip()
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    try:
        if scheme in ("http", "https"):
            data = _fetch_http(url, timeout)
        elif scheme == "file":
            # urlparse on file:///path -> path is parsed.path
            data = _read_local(parsed.path)
        elif scheme == "" or (len(scheme) == 1 and url[1:2] == ":"):
            # No scheme or single-letter (Windows drive — treat as path).
            data = _read_local(url)
        elif scheme == "ftp":
            data = _fetch_ftp(url, timeout)
        elif scheme in ("s3", "s3a"):
            data = _fetch_s3(url, timeout)
        elif scheme == "smb":
            data = _fetch_smb(url, timeout)
        else:
            # Last-ditch: hand to urllib which speaks several niche
            # schemes (gopher, etc.). Anything weirder errors out as
            # URLFetchError below.
            data = _fetch_urllib(url, timeout)
    except URLFetchError:
        raise
    except Exception as e:
        raise URLFetchError(f"fetch_bytes({url!r}) failed: {e}") from e

    if not data:
        raise URLFetchError(f"fetch_bytes({url!r}) returned 0 bytes")
    return data


def _fetch_http(url: str, timeout: float) -> bytes:
    # verify=False intentional — internal cougaar / GRVTY hosts use
    # self-signed certs in the SCIF + hotel networks. Cougaar adapter
    # already does the same (cougaar_adapters._build_client transport).
    with httpx.Client(timeout=timeout, verify=False, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


def _read_local(path: str) -> bytes:
    p = Path(path)
    if not p.exists():
        raise URLFetchError(f"local file not found: {path}")
    if not p.is_file():
        raise URLFetchError(f"local path is not a file: {path}")
    return p.read_bytes()


def _fetch_ftp(url: str, timeout: float) -> bytes:
    # urllib speaks ftp natively; gives us anonymous-FTP for free.
    with _urlopen(url, timeout=timeout) as r:
        return r.read()


def _fetch_s3(url: str, timeout: float) -> bytes:
    try:
        import boto3  # type: ignore
    except ImportError as e:
        raise URLFetchError(
            f"s3 URL {url!r} requires boto3 (not installed)"
        ) from e
    parsed = urlparse(url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not (bucket and key):
        raise URLFetchError(f"s3 URL {url!r}: missing bucket or key")
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def _fetch_smb(url: str, timeout: float) -> bytes:
    """SMB (Windows file share) fetch. Tries smbprotocol pure-Python
    first; falls back to the `smbclient` command-line tool. Tries
    anonymous/guest by default — when GRVTY needs auth, set
    SMB_USERNAME / SMB_PASSWORD / SMB_DOMAIN env vars.

    URL form: smb://[user[:pass]@]host/share/path/to/file.jpg

    No prior knowledge of GRVTY's actual SMB layout — best-effort
    until they ship the first URL tomorrow."""
    import os
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise URLFetchError(f"smb URL {url!r}: missing host")
    user = parsed.username or os.environ.get("SMB_USERNAME") or ""
    pwd = parsed.password or os.environ.get("SMB_PASSWORD") or ""
    domain = os.environ.get("SMB_DOMAIN") or ""
    path_parts = [p for p in (parsed.path or "").split("/") if p]
    if len(path_parts) < 2:
        raise URLFetchError(
            f"smb URL {url!r}: need share + path (got {path_parts!r})"
        )
    share = path_parts[0]
    remote_path = "/" + "/".join(path_parts[1:])

    # Path A: smbprotocol (pip install smbprotocol).
    try:
        import smbclient as _smbc  # type: ignore
        if user:
            _smbc.register_session(host, username=user, password=pwd, port=445)
        smb_path = f"\\\\{host}\\{share}\\" + "\\".join(path_parts[1:])
        with _smbc.open_file(smb_path, mode="rb") as f:
            return f.read()
    except ImportError:
        pass
    except Exception as e:
        log.debug("url_fetcher: smbprotocol path failed for %s: %s", url, e)

    # Path B: smbclient command-line.
    import subprocess, tempfile, shutil
    if not shutil.which("smbclient"):
        raise URLFetchError(
            f"smb URL {url!r}: neither smbprotocol nor smbclient available"
        )
    auth = f"{domain + chr(92) if domain else ''}{user}%{pwd}" if user else "%"
    with tempfile.NamedTemporaryFile(prefix="bda_smb_", delete=False) as tf:
        local = tf.name
    try:
        cmd = [
            "smbclient", f"//{host}/{share}",
            "-U", auth,
            "-c", f"get \"{remote_path.lstrip('/')}\" \"{local}\"",
        ]
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if p.returncode != 0:
            raise URLFetchError(
                f"smbclient {url!r} failed: {p.stderr.decode('utf-8', 'replace')[:240]}"
            )
        with open(local, "rb") as f:
            return f.read()
    finally:
        try: os.unlink(local)
        except Exception: pass


def _fetch_urllib(url: str, timeout: float) -> bytes:
    with _urlopen(url, timeout=timeout) as r:
        return r.read()
