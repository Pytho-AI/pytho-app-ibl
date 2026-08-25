"""Anvil-IPOE external partner API — a separate FastAPI sub-app mounted at /api/ext.

Being its own app gives it its own OpenAPI schema and Swagger UI
(/api/ext/docs), so the partner-facing surface can be documented and handed
out without exposing the internal routes. Every route requires X-API-Key
(core/ext_auth).
"""
from fastapi import Depends, FastAPI

from core.ext_auth import require_api_key
from . import ingest, map as map_router, products

ext_app = FastAPI(
    title="Anvil-IPOE External API",
    version="1.0.0",
    description=(
        "Server-to-server API for partner systems to push data and map layers into "
        "Anvil-IPOE and pull finished intelligence products and overlays out.\n\n"
        "- **Ingest**: GeoJSON observations (units, tracks, events — MIL-STD-2525 "
        "`sidc` on each feature) and document files\n"
        "- **Map**: layers and features as RFC 7946 GeoJSON overlays — importable by "
        "Gaia, MapLibre, ArcGIS, or any GeoJSON-capable client\n"
        "- **Products**: generated intelligence products — statistical layer summaries, "
        "and LLM-written INTSUM narratives in which every claim traces back to its "
        "numbered source observation\n\n"
        "Authenticate every request with the `X-API-Key` header. Product generation is "
        "asynchronous where noted: poll `/v1/ingest/jobs/{job_id}` until `done`."
    ),
)

for r in (ingest.router, map_router.router, products.router):
    ext_app.include_router(r, prefix="/v1", dependencies=[Depends(require_api_key)])
