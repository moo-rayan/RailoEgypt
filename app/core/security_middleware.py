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
import asyncio
import json as _json
import logging
import time
from collections import defaultdict, deque

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.cache import get_redis
from app.services.audit_service import IP_BLOCK, audit

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

# ── Adaptive IP blocking ─────────────────────────────────────────────────────
# The first few bad requests still get a cheap 404.  If an IP starts guessing
# many unrelated paths in a short window, block it temporarily before the
# request reaches routing/database/auth layers.
_SCAN_WINDOW = 60
_SCAN_BLOCK_SECONDS = 30 * 60
_SCAN_HIT_THRESHOLD = 24
_SCAN_UNIQUE_PATH_THRESHOLD = 12

_AUTH_FAIL_WINDOW = 5 * 60
_AUTH_FAIL_BLOCK_SECONDS = 15 * 60
_AUTH_FAIL_THRESHOLD = 20

_REDIS_BLOCK_PREFIX = "security:block_ip:"
_REDIS_CACHE_TTL = 3.0

_scan_hits: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
_auth_fail_hits: dict[str, deque[float]] = defaultdict(deque)
_blocked_ips: dict[str, tuple[float, str]] = {}
_redis_block_cache: dict[str, tuple[bool, float, str]] = {}
_block_log_seen: dict[str, float] = defaultdict(float)

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
        ip = _extract_request_ip(request)

        # Skip analysis for health checks and docs
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            await self._app(scope, receive, send)
            return

        is_blocked, block_reason = await _is_ip_blocked(ip)
        if is_blocked:
            await _send_blocked_response(scope, receive, send, request, block_reason)
            return

        # ── Early reject known bot/scanner noise paths ────────────────────
        # Return 404 immediately without forwarding to the app.  Repeated
        # probes from the same IP escalate to a temporary block.
        path_lower = path.lower()
        if _is_scanner_probe_path(path_lower):
            await _record_scan_probe(request, path, aggressive=True)
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
                _cleanup_security_counters(now)
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
                    dedup_key = f"{ip}:{path}"
                    now_mono = time.monotonic()
                    if now_mono - _auth_log_seen.get(dedup_key, 0) > _AUTH_LOG_WINDOW:
                        _auth_log_seen[dedup_key] = now_mono
                        audit.log_auth_failure(
                            request,
                            reason=f"Endpoint returned 401: {path}",
                        )
                    await _record_auth_failure(request, path)

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


def _is_scanner_probe_path(path_lower: str) -> bool:
    """Return True for paths that have no legitimate use in this API."""
    if any(noise in path_lower for noise in _NOISE_PATHS):
        return True

    # This is a FastAPI backend; PHP/CMS probes are pure scanner traffic.
    if path_lower.endswith(".php") or ".php/" in path_lower or ".php?" in path_lower:
        return True

    # Common API discovery/secret probes that should never be public here.
    suspicious_exact = {
        "/graphql", "/api/graphql", "/v1/graphql", "/graphql/v1", "/graphql/v2",
        "/swagger.json", "/swagger.yaml", "/swagger-ui.html", "/swagger-ui/index.html",
        "/configprops",
    }
    if path_lower in suspicious_exact:
        return True

    suspicious_fragments = (
        "/.aws/", "/.kube/", "/config/credentials", "/config/master.key",
        "/config/secrets", "/config/storage", "/config/default",
    )
    return any(fragment in path_lower for fragment in suspicious_fragments)


async def _is_ip_blocked(ip: str) -> tuple[bool, str]:
    """Check local/Redis temporary block cache."""
    if not ip or ip == "unknown":
        return False, ""

    now = time.monotonic()

    local = _blocked_ips.get(ip)
    if local:
        expires_at, reason = local
        if now < expires_at:
            return True, reason
        _blocked_ips.pop(ip, None)

    cached = _redis_block_cache.get(ip)
    if cached:
        blocked, expires_at, reason = cached
        if now < expires_at:
            return blocked, reason
        _redis_block_cache.pop(ip, None)

    try:
        r = await get_redis()
        reason = await asyncio.wait_for(
            r.get(f"{_REDIS_BLOCK_PREFIX}{ip}"),
            timeout=0.08,
        )
        blocked = bool(reason)
        _redis_block_cache[ip] = (blocked, now + _REDIS_CACHE_TTL, reason or "")
        if blocked:
            _blocked_ips[ip] = (now + _REDIS_CACHE_TTL, reason or "temporary security block")
        return blocked, reason or ""
    except Exception:
        _redis_block_cache[ip] = (False, now + 1.0, "")
        return False, ""


async def _block_ip(request: Request, ip: str, reason: str, seconds: int) -> None:
    """Temporarily block an IP locally and in Redis when available."""
    if not ip or ip == "unknown":
        return

    now = time.monotonic()
    _blocked_ips[ip] = (now + seconds, reason)
    _redis_block_cache[ip] = (True, now + _REDIS_CACHE_TTL, reason)

    try:
        r = await get_redis()
        await asyncio.wait_for(
            r.setex(f"{_REDIS_BLOCK_PREFIX}{ip}", seconds, reason),
            timeout=0.2,
        )
    except Exception:
        pass

    # Log one block event per IP per window; blocked requests themselves stay
    # silent to avoid turning an attack into audit-log/database pressure.
    log_key = f"{ip}:{reason}"
    if now - _block_log_seen.get(log_key, 0) > min(seconds, _AUTH_LOG_WINDOW):
        _block_log_seen[log_key] = now
        try:
            audit.log(
                event_type=IP_BLOCK,
                severity="critical",
                description=f"Temporary IP block: {reason}",
                request=request,
                status_code=404,
                metadata={
                    "block_seconds": seconds,
                    "blocked_ip": ip,
                    "reason": reason,
                },
            )
        except Exception:
            pass


async def _record_scan_probe(request: Request, path: str, aggressive: bool = False) -> None:
    """Track scanner probes and escalate repeated guesses to a temporary block."""
    ip = _extract_request_ip(request)
    if not ip or ip == "unknown":
        return

    now = time.monotonic()
    hits = _scan_hits[ip]
    cutoff = now - _SCAN_WINDOW
    while hits and hits[0][0] < cutoff:
        hits.popleft()

    hits.append((now, path[:300]))
    unique_paths = {p for _, p in hits}

    hit_threshold = 8 if aggressive else _SCAN_HIT_THRESHOLD
    unique_threshold = 6 if aggressive else _SCAN_UNIQUE_PATH_THRESHOLD

    if len(hits) >= hit_threshold or len(unique_paths) >= unique_threshold:
        reason = (
            f"path-scan detected: {len(hits)} probes, "
            f"{len(unique_paths)} unique paths in {_SCAN_WINDOW}s"
        )
        await _block_ip(request, ip, reason, _SCAN_BLOCK_SECONDS)


async def _record_auth_failure(request: Request, path: str) -> None:
    """Temporarily block repeated unauthorized hits on admin/dashboard APIs."""
    ip = _extract_request_ip(request)
    if not ip or ip == "unknown":
        return

    # Be conservative: only escalate protected admin/dashboard areas. Normal
    # app token expiry should not cause an IP ban.
    protected_prefixes = (
        "/api/v1/admin/",
        "/api/v1/live/admin/",
        "/api/v1/live/dashboard/",
        "/api/v1/support/admin/",
        "/api/v1/fares/online-stats",
        "/api/v1/admin/audit/",
    )
    if not any(path.startswith(prefix) for prefix in protected_prefixes):
        return

    now = time.monotonic()
    hits = _auth_fail_hits[ip]
    cutoff = now - _AUTH_FAIL_WINDOW
    while hits and hits[0] < cutoff:
        hits.popleft()
    hits.append(now)

    if len(hits) >= _AUTH_FAIL_THRESHOLD:
        reason = f"repeated unauthorized admin/API access: {len(hits)} failures in {_AUTH_FAIL_WINDOW}s"
        await _block_ip(request, ip, reason, _AUTH_FAIL_BLOCK_SECONDS)


async def _send_blocked_response(
    scope: Scope,
    receive: Receive,
    send: Send,
    request: Request,
    reason: str,
) -> None:
    """Return a cheap not-found response for blocked scanners."""
    # Intentionally 404, not 403: do not reveal that security rules triggered.
    response = Response("Not Found", status_code=404)
    await response(scope, receive, send)


def _cleanup_security_counters(now: float) -> None:
    """Prune local security counters to keep memory bounded."""
    scan_cutoff = now - _SCAN_WINDOW * 2
    for ip in list(_scan_hits.keys()):
        hits = _scan_hits[ip]
        while hits and hits[0][0] < scan_cutoff:
            hits.popleft()
        if not hits:
            del _scan_hits[ip]

    auth_cutoff = now - _AUTH_FAIL_WINDOW * 2
    for ip in list(_auth_fail_hits.keys()):
        hits = _auth_fail_hits[ip]
        while hits and hits[0] < auth_cutoff:
            hits.popleft()
        if not hits:
            del _auth_fail_hits[ip]

    for ip, (expires_at, _) in list(_blocked_ips.items()):
        if now >= expires_at:
            del _blocked_ips[ip]

    for ip, (_, expires_at, _) in list(_redis_block_cache.items()):
        if now >= expires_at:
            del _redis_block_cache[ip]

    for key, last_seen in list(_block_log_seen.items()):
        if now - last_seen > _SCAN_BLOCK_SECONDS * 2:
            del _block_log_seen[key]


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
