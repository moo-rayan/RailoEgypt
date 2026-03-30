"""
Security Audit Service — fire-and-forget logging of security events.

Events are buffered in memory and batch-inserted to the database every
few seconds via a dedicated asyncpg pool.  This design:
  • avoids prepared-statement errors with pgbouncer (transaction mode)
  • keeps audit writes from competing with the main SQLAlchemy pool
  • zero impact on request latency (append to deque ≈ 0 ms)

Usage:
    from app.services.audit_service import audit

    audit.log_rate_limit(request, ...)
    audit.log_auth_failure(request, ...)
    # or the generic:
    audit.log(event_type="custom", severity="warning", ...)
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict, deque
from typing import Optional

import asyncpg
from fastapi import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Event types ──────────────────────────────────────────────────────────────
RATE_LIMIT      = "rate_limit"
AUTH_FAILURE     = "auth_failure"
BRUTE_FORCE      = "brute_force"
BOT_DETECTED     = "bot_detected"
PATH_SCAN        = "path_scan"
SPAM             = "spam"
ATTACK           = "attack"
INVALID_INPUT    = "invalid_input"
SUSPICIOUS       = "suspicious"
ADMIN_ACTION     = "admin_action"
FORBIDDEN_ACCESS = "forbidden_access"
TOKEN_ABUSE      = "token_abuse"

# ── Severity levels ──────────────────────────────────────────────────────────
INFO     = "info"
WARNING  = "warning"
CRITICAL = "critical"

# ── Known bot / scanner patterns in User-Agent ───────────────────────────────
_BOT_SIGNATURES = [
    "sqlmap", "nikto", "nmap", "masscan", "zgrab", "gobuster",
    "dirbuster", "wfuzz", "ffuf", "nuclei", "httpx-toolkit",
    "scrapy", "python-requests", "go-http-client", "curl/",
    "wget/", "libwww-perl", "java/", "okhttp/",
    "censys", "shodan", "netcraft", "semrush", "ahrefs",
    "mj12bot", "dotbot", "petalbot", "baiduspider",
    "yandexbot", "sogou", "exabot",
    "headlesschrome", "phantomjs", "selenium",
]

# ── Suspicious path patterns (scanners probe these) ──────────────────────────
_SUSPICIOUS_PATHS = [
    "/.env", "/wp-admin", "/wp-login", "/phpmyadmin", "/admin/login",
    "/.git", "/.svn", "/config", "/backup", "/dump",
    "/shell", "/cmd", "/exec", "/eval", "/system",
    "/etc/passwd", "/proc/self", "/../", "/wp-content",
    "/xmlrpc", "/cgi-bin", "/.htaccess", "/.htpasswd",
    "/actuator", "/swagger", "/graphql", "/console",
    "/debug", "/trace", "/server-status", "/server-info",
    "/solr", "/jenkins", "/manager", "/jmx-console",
]

# ── In-memory rate tracking (per-IP counters for burst detection) ─────────
_ip_request_log: dict[str, list[float]] = defaultdict(list)
_ip_auth_failures: dict[str, list[float]] = defaultdict(list)
_ip_rate_limit_hits: dict[str, list[float]] = defaultdict(list)

# Thresholds
_BURST_WINDOW_SECONDS = 60
_BURST_THRESHOLD = 120          # >120 requests/min from single IP = suspicious
_AUTH_FAIL_THRESHOLD = 10       # >10 auth failures/min = brute force
_RATE_LIMIT_ESCALATION = 5     # >5 rate-limit hits/min = persistent abuser
_PATH_SCAN_THRESHOLD = 5        # >5 invalid paths/min = scanner

# ── Buffer & dedicated pool ──────────────────────────────────────────────────
_FLUSH_INTERVAL = 5             # seconds between batch flushes
_FLUSH_BATCH = 500              # max rows per INSERT
_MAX_BUFFER = 10_000            # hard cap on pending events

_buffer: deque[dict] = deque(maxlen=_MAX_BUFFER)
_pool: Optional[asyncpg.Pool] = None
_flush_task: Optional[asyncio.Task] = None

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I,
)


# ── SQL escaping (simple query protocol — no prepared statements) ─────────

def _q(val: Optional[str]) -> str:
    """Escape a string value for a SQL literal.  Returns NULL for None."""
    if val is None:
        return "NULL"
    return "'" + str(val).replace("'", "''") + "'"


def _qi(val) -> str:
    """Escape an integer value."""
    if val is None:
        return "NULL"
    return str(int(val))


def _quuid(val: Optional[str]) -> str:
    """Escape a UUID value."""
    if not val or not _UUID_RE.match(str(val)):
        return "NULL"
    return "'" + str(val) + "'::uuid"


def _qjsonb(val) -> str:
    """Escape a JSONB value."""
    if not val:
        return "'{}'::jsonb"
    if isinstance(val, str):
        s = val
    else:
        try:
            s = json.dumps(val, ensure_ascii=False, default=str)
        except Exception:
            s = "{}"
    return "'" + s.replace("'", "''") + "'::jsonb"


# ── Pool & flush loop ────────────────────────────────────────────────────────

def _pg_dsn() -> str:
    """Return a plain postgresql:// DSN for raw asyncpg (not SQLAlchemy)."""
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        _pool = await asyncpg.create_pool(
            dsn=_pg_dsn(),
            min_size=1,
            max_size=2,
            statement_cache_size=0,
        )
    return _pool


def _start_flusher() -> None:
    """Ensure the background flush loop is running."""
    global _flush_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _flush_task is None or _flush_task.done():
        _flush_task = loop.create_task(_flush_loop())


async def _flush_loop() -> None:
    """Periodically flush buffered events to the database."""
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        try:
            await _flush()
        except Exception as exc:
            logger.error("Audit flush error: %s", exc)


async def _flush() -> None:
    """Batch-insert all buffered events using simple query protocol."""
    if not _buffer:
        return

    batch: list[dict] = []
    while _buffer and len(batch) < _FLUSH_BATCH:
        batch.append(_buffer.popleft())
    if not batch:
        return

    values: list[str] = []
    for e in batch:
        values.append(
            f"({_q(e['event_type'])},{_q(e['severity'])},"
            f"{_q(e.get('ip_address'))},{_q(e.get('user_agent'))},"
            f"{_q(e.get('method'))},{_q(e.get('path'))},"
            f"{_qi(e.get('status_code'))},{_quuid(e.get('user_id'))},"
            f"{_q(e['description'])},{_qjsonb(e.get('metadata'))},"
            f"{_q(e.get('country_code'))})"
        )

    sql = (
        'INSERT INTO "EgRailway".audit_log '
        "(event_type,severity,ip_address,user_agent,method,"
        "path,status_code,user_id,description,metadata,country_code) VALUES "
        + ",".join(values)
    )

    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql)
    except Exception as exc:
        logger.error("Audit batch insert (%d rows) failed: %s", len(batch), exc)


# ── Helper functions ─────────────────────────────────────────────────────────

def _extract_ip(request: Request) -> str:
    """Extract the real client IP (handles proxies/CF)."""
    for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        val = request.headers.get(header)
        if val:
            return val.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _extract_country(request: Request) -> Optional[str]:
    """Extract country code from Cloudflare or similar header."""
    return request.headers.get("cf-ipcountry") or request.headers.get("x-country-code")


def _is_bot_ua(ua: str) -> bool:
    """Check if User-Agent matches known bot/scanner signatures."""
    ua_lower = ua.lower()
    return any(sig in ua_lower for sig in _BOT_SIGNATURES)


def _is_suspicious_path(path: str) -> bool:
    """Check if the request path matches known scanner probes."""
    path_lower = path.lower()
    return any(p in path_lower for p in _SUSPICIOUS_PATHS)


def _prune_window(timestamps: list[float], window: float) -> list[float]:
    """Remove entries older than the window."""
    cutoff = time.monotonic() - window
    return [t for t in timestamps if t > cutoff]


class AuditService:
    """Centralized security audit logger."""

    def _fire(self, **kwargs) -> None:
        """Buffer an event for periodic batch insert (zero-latency)."""
        if kwargs.get("user_agent"):
            kwargs["user_agent"] = kwargs["user_agent"][:2000]
        if kwargs.get("path"):
            kwargs["path"] = kwargs["path"][:2000]
        if kwargs.get("description"):
            kwargs["description"] = kwargs["description"][:5000]
        _buffer.append(kwargs)
        _start_flusher()

    # ── Public API ────────────────────────────────────────────────────────

    def log(
        self,
        event_type: str,
        severity: str,
        description: str,
        request: Optional[Request] = None,
        status_code: Optional[int] = None,
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Generic audit log entry."""
        ip = _extract_ip(request) if request else None
        self._fire(
            event_type=event_type,
            severity=severity,
            description=description,
            ip_address=ip,
            user_agent=request.headers.get("user-agent") if request else None,
            method=request.method if request else None,
            path=str(request.url.path) if request else None,
            status_code=status_code,
            user_id=user_id,
            metadata=metadata,
            country_code=_extract_country(request) if request else None,
        )

    def log_rate_limit(self, request: Request, limit_info: str = "") -> None:
        """Log a rate-limit violation and check for persistent abuse."""
        ip = _extract_ip(request)
        now = time.monotonic()

        # Track rate-limit hits for this IP
        _ip_rate_limit_hits[ip] = _prune_window(_ip_rate_limit_hits[ip], _BURST_WINDOW_SECONDS)
        _ip_rate_limit_hits[ip].append(now)
        hit_count = len(_ip_rate_limit_hits[ip])

        severity = WARNING
        event_type = RATE_LIMIT
        desc = f"Rate limit exceeded: {limit_info}" if limit_info else "Rate limit exceeded"

        if hit_count >= _RATE_LIMIT_ESCALATION:
            severity = CRITICAL
            event_type = ATTACK
            desc = f"Persistent rate-limit abuse: {hit_count} violations in 60s"

        self._fire(
            event_type=event_type,
            severity=severity,
            description=desc,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
            method=request.method,
            path=str(request.url.path),
            status_code=429,
            metadata={"limit_info": limit_info, "hit_count": hit_count},
            country_code=_extract_country(request),
        )

    def log_auth_failure(
        self,
        request: Request,
        reason: str = "Invalid or expired token",
        user_id: Optional[str] = None,
    ) -> None:
        """Log an authentication failure and detect brute-force."""
        ip = _extract_ip(request)
        now = time.monotonic()

        _ip_auth_failures[ip] = _prune_window(_ip_auth_failures[ip], _BURST_WINDOW_SECONDS)
        _ip_auth_failures[ip].append(now)
        fail_count = len(_ip_auth_failures[ip])

        severity = WARNING
        event_type = AUTH_FAILURE

        if fail_count >= _AUTH_FAIL_THRESHOLD:
            severity = CRITICAL
            event_type = BRUTE_FORCE

        self._fire(
            event_type=event_type,
            severity=severity,
            description=f"Auth failure: {reason} (attempt #{fail_count} in 60s)",
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
            method=request.method,
            path=str(request.url.path),
            status_code=401,
            user_id=user_id,
            metadata={"reason": reason, "fail_count": fail_count},
            country_code=_extract_country(request),
        )

    def log_forbidden(
        self,
        request: Request,
        reason: str = "Access denied",
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Log a 403 Forbidden access attempt."""
        meta = {"reason": reason}
        if metadata:
            meta.update(metadata)
        self._fire(
            event_type=FORBIDDEN_ACCESS,
            severity=WARNING,
            description=f"Forbidden: {reason}",
            ip_address=_extract_ip(request),
            user_agent=request.headers.get("user-agent"),
            method=request.method,
            path=str(request.url.path),
            status_code=403,
            user_id=user_id,
            metadata=meta,
            country_code=_extract_country(request),
        )

    def log_bot_detected(self, request: Request, ua: str) -> None:
        """Log a detected bot/scanner."""
        self._fire(
            event_type=BOT_DETECTED,
            severity=WARNING,
            description=f"Bot/scanner detected: {ua[:200]}",
            ip_address=_extract_ip(request),
            user_agent=ua,
            method=request.method,
            path=str(request.url.path),
            metadata={"detected_by": "user_agent_signature"},
            country_code=_extract_country(request),
        )

    def log_path_scan(self, request: Request) -> None:
        """Log a suspected path-scan/enumeration attempt."""
        self._fire(
            event_type=PATH_SCAN,
            severity=WARNING,
            description=f"Suspicious path probe: {request.url.path}",
            ip_address=_extract_ip(request),
            user_agent=request.headers.get("user-agent"),
            method=request.method,
            path=str(request.url.path),
            status_code=404,
            metadata={"query": str(request.url.query) if request.url.query else None},
            country_code=_extract_country(request),
        )

    def log_spam(
        self,
        request: Request,
        detail: str = "Spam detected",
        metadata: Optional[dict] = None,
    ) -> None:
        """Log spam activity."""
        self._fire(
            event_type=SPAM,
            severity=WARNING,
            description=detail,
            ip_address=_extract_ip(request),
            user_agent=request.headers.get("user-agent"),
            method=request.method,
            path=str(request.url.path),
            metadata=metadata,
            country_code=_extract_country(request),
        )

    def log_suspicious(
        self,
        request: Request,
        detail: str,
        severity: str = WARNING,
        metadata: Optional[dict] = None,
    ) -> None:
        """Log generic suspicious activity."""
        self._fire(
            event_type=SUSPICIOUS,
            severity=severity,
            description=detail,
            ip_address=_extract_ip(request),
            user_agent=request.headers.get("user-agent"),
            method=request.method,
            path=str(request.url.path),
            metadata=metadata,
            country_code=_extract_country(request),
        )

    def log_admin_action(
        self,
        request: Request,
        action: str,
        user_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Log an admin action (kick, ban, config change, etc.)."""
        self._fire(
            event_type=ADMIN_ACTION,
            severity=INFO,
            description=f"Admin action: {action}",
            ip_address=_extract_ip(request),
            user_agent=request.headers.get("user-agent"),
            method=request.method,
            path=str(request.url.path),
            user_id=user_id,
            metadata=metadata,
            country_code=_extract_country(request),
        )

    # ── Request analysis (called from middleware) ─────────────────────────

    def analyze_request(self, request: Request) -> None:
        """
        Analyze an incoming request for suspicious patterns.
        Called from the security middleware on every HTTP request.
        """
        ip = _extract_ip(request)
        ua = request.headers.get("user-agent", "")
        path = request.url.path

        # 1. Bot / scanner detection via User-Agent
        if ua and _is_bot_ua(ua):
            self.log_bot_detected(request, ua)
            return

        # 2. Missing User-Agent (common in automated scanners)
        if not ua or len(ua) < 5:
            self.log_suspicious(
                request,
                detail=f"Request with missing/empty User-Agent from {ip}",
                metadata={"user_agent": ua or "(empty)"},
            )

        # 3. Suspicious path probing
        if _is_suspicious_path(path):
            self.log_path_scan(request)

        # 4. Request burst detection (per IP)
        now = time.monotonic()
        _ip_request_log[ip] = _prune_window(_ip_request_log[ip], _BURST_WINDOW_SECONDS)
        _ip_request_log[ip].append(now)
        req_count = len(_ip_request_log[ip])

        if req_count >= _BURST_THRESHOLD:
            # Only log once per escalation (every 50 requests)
            if req_count % 50 == 0:
                self._fire(
                    event_type=SPAM,
                    severity=CRITICAL,
                    description=f"Request burst: {req_count} requests in 60s from {ip}",
                    ip_address=ip,
                    user_agent=ua,
                    method=request.method,
                    path=path,
                    metadata={"request_count": req_count, "window_seconds": _BURST_WINDOW_SECONDS},
                    country_code=_extract_country(request),
                )

    def cleanup_counters(self) -> None:
        """Periodic cleanup of in-memory counters to prevent memory leaks."""
        now = time.monotonic()
        for store in (_ip_request_log, _ip_auth_failures, _ip_rate_limit_hits):
            stale_keys = [
                k for k, v in store.items()
                if not v or (now - max(v)) > _BURST_WINDOW_SECONDS * 2
            ]
            for k in stale_keys:
                del store[k]

    async def shutdown(self) -> None:
        """Flush remaining events and close the pool (call on app shutdown)."""
        try:
            await _flush()
        except Exception:
            pass
        global _pool
        if _pool is not None and not _pool._closed:
            await _pool.close()
            _pool = None


# ── Singleton ─────────────────────────────────────────────────────────────────
audit = AuditService()
