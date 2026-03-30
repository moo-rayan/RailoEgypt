"""
Security Middleware — inspects every HTTP request for threats.

Hooks into the ASGI pipeline to:
1. Analyze requests for bot signatures, path scanning, request bursts
2. Log rate-limit violations (via custom RateLimitExceeded handler)
3. Track auth failures (401/403 responses)
4. Periodic cleanup of in-memory counters

This middleware is lightweight: analysis is fire-and-forget (async tasks)
so it adds negligible latency to requests.
"""

import logging
import time

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.audit_service import audit

logger = logging.getLogger(__name__)

# Paths to skip analysis (health checks, static, etc.)
_SKIP_PREFIXES = ("/api/v1/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico")

# Counter cleanup interval (seconds)
_CLEANUP_INTERVAL = 300  # 5 minutes
_last_cleanup = time.monotonic()


class SecurityMiddleware:
    """
    ASGI middleware that analyzes HTTP requests for security threats.
    Skips WebSocket and lifespan scopes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive, send)
        path = request.url.path

        # Skip analysis for health checks and docs
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            await self._app(scope, receive, send)
            return

        # Analyze request (fire-and-forget — non-blocking)
        try:
            audit.analyze_request(request)
        except Exception:
            pass  # Never let analysis errors break the request

        # Periodic counter cleanup
        global _last_cleanup
        now = time.monotonic()
        if now - _last_cleanup > _CLEANUP_INTERVAL:
            _last_cleanup = now
            try:
                audit.cleanup_counters()
            except Exception:
                pass

        # Intercept the response to log auth failures
        response_status = None

        async def send_wrapper(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 0)

                # Log 401/403 responses
                if response_status == 401:
                    audit.log_auth_failure(
                        request,
                        reason=f"Endpoint returned 401: {path}",
                    )
                elif response_status == 403:
                    audit.log_forbidden(
                        request,
                        reason=f"Endpoint returned 403: {path}",
                    )

            await send(message)

        await self._app(scope, receive, send_wrapper)
