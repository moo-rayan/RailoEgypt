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

import base64
import json as _json
import logging
import time
from collections import defaultdict

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.audit_service import audit

logger = logging.getLogger(__name__)

# Paths to skip analysis (health checks, static, etc.)
_SKIP_PREFIXES = ("/api/v1/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico")

# ── Known bot/scanner noise paths ─────────────────────────────────────────────
# Requests to these paths are silently dropped (404) without logging or
# forwarding to the app.  They are universal internet noise that has zero
# relevance to this API and only pollutes the audit log + wastes resources.
_NOISE_PATHS = (
    # WordPress
    "/wp-admin", "/wp-login", "/wp-content", "/wp-includes",
    "/wordpress", "/wp-config", "/wp-cron", "/wp-json",
    "/xmlrpc.php", "/xmlrpc",
    # PHP / config probes
    "/.env", "/.git", "/.svn", "/.htaccess", "/.htpasswd",
    "/phpmyadmin", "/pma", "/myadmin", "/phpinfo",
    "/config.php", "/wp-config.php", "/configuration.php",
    "/config.json", "/config.yaml", "/config.yml",
    # CGI / shell probes
    "/cgi-bin", "/shell", "/cmd", "/exec",
    "/eval", "/system", "/etc/passwd", "/proc/self",
    # Java / enterprise probes
    "/actuator", "/console", "/debug", "/trace",
    "/server-status", "/server-info", "/solr", "/jenkins",
    "/manager", "/jmx-console",
    # Other CMS / admin
    "/admin/login", "/administrator",
    "/drupal", "/joomla", "/magento",
    "/backup", "/dump", "/database",
    # Setup/install probes
    "/setup-config", "/install", "/setup.php",
)

# Rate-limit: only log auth failures from the same IP+path once per window
_AUTH_LOG_WINDOW = 300  # 5 minutes
_auth_log_seen: dict[str, float] = defaultdict(float)  # key: "ip:path" -> last_logged

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

        # ── Early reject known bot/scanner noise paths ────────────────────
        # Return 404 immediately without logging or forwarding to the app.
        path_lower = path.lower()
        if any(noise in path_lower for noise in _NOISE_PATHS):
            response = Response("Not Found", status_code=404)
            await response(scope, receive, send)
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
                # Also clean up auth log dedup cache
                stale = [k for k, v in _auth_log_seen.items() if now - v > _AUTH_LOG_WINDOW * 2]
                for k in stale:
                    del _auth_log_seen[k]
            except Exception:
                pass

        # Intercept the response to log auth failures
        response_status = None

        async def send_wrapper(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 0)

                if response_status == 401:
                    # 401: deduplicate per IP+path (noise from bots)
                    ip = _extract_request_ip(request)
                    dedup_key = f"{ip}:{path}"
                    now_mono = time.monotonic()
                    if now_mono - _auth_log_seen.get(dedup_key, 0) > _AUTH_LOG_WINDOW:
                        _auth_log_seen[dedup_key] = now_mono
                        audit.log_auth_failure(
                            request,
                            reason=f"Endpoint returned 401: {path}",
                        )

                elif response_status == 403:
                    # 403: ALWAYS log — extract user identity from JWT
                    user_info = _extract_user_from_jwt(request)
                    user_id = user_info.get("sub") if user_info else None
                    user_email = user_info.get("email", "unknown") if user_info else "unknown"
                    user_name = ""
                    if user_info:
                        meta = user_info.get("user_metadata") or {}
                        user_name = (
                            meta.get("full_name", "")
                            or meta.get("name", "")
                            or meta.get("display_name", "")
                        )

                    desc = f"Forbidden: Endpoint returned 403: {path}"
                    if user_email and user_email != "unknown":
                        desc = f"Forbidden: {user_name or user_email} → {path}"

                    audit.log_forbidden(
                        request,
                        reason=desc,
                        user_id=user_id,
                        metadata={
                            "user_email": user_email,
                            "user_name": user_name,
                        },
                    )

            await send(message)

        await self._app(scope, receive, send_wrapper)


def _extract_request_ip(request: Request) -> str:
    """Extract real client IP (lightweight copy to avoid circular import)."""
    for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        val = request.headers.get(header)
        if val:
            return val.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _extract_user_from_jwt(request: Request) -> dict | None:
    """
    Decode JWT payload (without verification) to extract user identity.
    This is safe — we only use it for logging, not for auth decisions.
    """
    try:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:]
        # JWT = header.payload.signature — decode payload (part 1)
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        # Add padding if needed
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return _json.loads(decoded)
    except Exception:
        return None
