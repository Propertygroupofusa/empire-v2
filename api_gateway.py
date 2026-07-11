"""
API GATEWAY / REVERSE PROXY
===========================
Routes requests from landing pages to the appropriate microservice.
Exposes all APIs on a single port (10000) with /signals, /api, /dfy, /partners prefixes.

This solves the Railway deployment issue where all services need to be
accessible through a single public URL.
"""

import os
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("api_gateway")

app = FastAPI(title="API Gateway")

# CORS middleware for landing pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Internal service URLs
SIGNALS_API = "http://localhost:8001"
GENERATION_API = "http://localhost:8002"
DFY_SERVICE = "http://localhost:8003"
WHITE_LABEL_API = "http://localhost:8004"

GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", 10000))

async def proxy_request(service_url: str, path: str, request: Request):
    """Proxy request to the appropriate microservice"""
    try:
        # Get request body
        body = await request.body() if request.method in ["POST", "PUT", "PATCH"] else None

        # Build URL with query parameters
        url = f"{service_url}{path}"
        if request.query_params:
            url += f"?{request.query_params}"

        # Filter headers
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ["host", "content-length", "connection"]}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
            )

            # Return response with proper content type
            return StreamingResponse(
                iter([response.content]),
                status_code=response.status_code,
                headers=dict(response.headers),
            )
    except Exception as e:
        log.error(f"Proxy error for {path}: {e}")
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/health")
async def health():
    """Health check"""
    return {"status": "ok", "service": "api_gateway"}


# TRADING SIGNALS API (/signals/*)
@app.post("/signals/subscribe")
async def signals_subscribe(request: Request):
    """Proxy to trading signals API"""
    return await proxy_request(SIGNALS_API, "/subscribe", request)


@app.get("/signals/signals")
async def get_signals(request: Request):
    """Proxy to get trading signals"""
    return await proxy_request(SIGNALS_API, "/signals", request)


# CONTENT GENERATION API (/api/*)
@app.post("/api/subscribe")
async def api_subscribe(request: Request):
    """Proxy to content generation API"""
    return await proxy_request(GENERATION_API, "/subscribe", request)


@app.post("/api/webhook/stripe")
async def api_webhook_stripe(request: Request):
    """Proxy Stripe webhook to API"""
    return await proxy_request(GENERATION_API, "/webhook/stripe", request)


# DONE-FOR-YOU SERVICE (/dfy/*)
@app.post("/dfy/onboard")
async def dfy_onboard(request: Request):
    """Proxy to DFY service"""
    return await proxy_request(DFY_SERVICE, "/onboard", request)


# WHITE-LABEL PLATFORM (/partners/*)
@app.post("/partners/apply")
async def partners_apply(request: Request):
    """Proxy to white label platform"""
    return await proxy_request(WHITE_LABEL_API, "/apply", request)


@app.post("/partners/webhook/stripe")
async def partners_webhook_stripe(request: Request):
    """Proxy Stripe webhook to white label"""
    return await proxy_request(WHITE_LABEL_API, "/webhook/stripe", request)


if __name__ == "__main__":
    import uvicorn
    log.info(f"Starting API Gateway on port {GATEWAY_PORT}...")
    log.info(f"  /signals/* -> {SIGNALS_API}")
    log.info(f"  /api/* -> {GENERATION_API}")
    log.info(f"  /dfy/* -> {DFY_SERVICE}")
    log.info(f"  /partners/* -> {WHITE_LABEL_API}")

    uvicorn.run(app, host="0.0.0.0", port=GATEWAY_PORT)
