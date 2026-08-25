"""API-key auth for the external API (/api/ext/v1).

Keys live in the EXT_API_KEYS env var (comma-separated; EXT_API_KEY also accepted
for a single key) and are sent by callers in the X-API-Key header. Deliberately
separate from the session auth the UI uses: ext keys are server-to-server
credentials that can be rotated without touching user accounts.

Fails closed: if no keys are configured, every ext request gets a 503 so a
missing env var can never mean an open API.
"""
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

# auto_error=False so we control the error shape (and 401 vs 403) ourselves;
# declaring the scheme is what puts the Authorize button on /api/ext/docs.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="External API key")


def _configured_keys() -> list[str]:
    raw = os.environ.get("EXT_API_KEYS", "") or os.environ.get("EXT_API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


async def require_api_key(api_key: str | None = Depends(_api_key_header)) -> str:
    keys = _configured_keys()
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External API is not configured (set EXT_API_KEYS)",
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    if not any(secrets.compare_digest(api_key, k) for k in keys):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )
    return api_key
