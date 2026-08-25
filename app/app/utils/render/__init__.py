"""Render bridge — POSTs to geo-agent's /api/v1/render.

The map render used to live in pytho-app (`headless.py` with Playwright).
It moved to geo-agent (`geo-agent/backend/app/services/render/headless.py`)
under Task 6 of the map API unification plan. pytho-app callers now use
`render_map_dispatch` which POSTs to geo-agent and streams the JPG back.

geo-agent is a standalone service reached over its host-published port.
`GEO_API_URL` (base URL, default `http://host.docker.internal:9845`) is
the ONE knob; the render endpoint derives from it. No local fallback.
"""
from .via_geoagent import (
    RenderOptions,
    render_map_dispatch,
    render_map_dispatch_sync,
    render_map_via_geoagent,
    # Back-compat aliases; new code should use render_map_dispatch.
    render_map_to_png,
    render_map_to_png_sync,
)

__all__ = [
    "render_map_dispatch",
    "render_map_dispatch_sync",
    "render_map_via_geoagent",
    "render_map_to_png",
    "render_map_to_png_sync",
    "RenderOptions",
]
