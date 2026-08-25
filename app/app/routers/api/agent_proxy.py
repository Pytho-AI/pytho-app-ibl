"""Reverse proxy for agent service — passes all requests through to the agent."""
import os
import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import Response
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent")

AGENT_SERVICE_URL = os.environ.get("AGENT_SERVICE_URL", "http://localhost:17611")


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_agent(request: Request, path: str):
    """Forward any request to the agent service."""
    target_url = f"{AGENT_SERVICE_URL}/{path}"

    # Forward query params
    if request.url.query:
        target_url += f"?{request.url.query}"

    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length")}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=body if body else None,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                response_body = await resp.read()
                return Response(
                    content=response_body,
                    status_code=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("transfer-encoding", "content-encoding")},
                )
    except Exception as e:
        logger.error(f"[agent_proxy] Failed to proxy {request.method} /{path}: {e}")
        return Response(
            content=f'{{"error": "Agent service unreachable: {e}"}}',
            status_code=502,
            media_type="application/json",
        )
