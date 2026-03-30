"""
Security module: Custom App JWT + Supabase JWT verification + HMAC tickets.

Token Strategy:
  1. Client authenticates via Supabase (Google/Facebook OAuth).
  2. Client calls POST /auth/token with Supabase JWT.
  3. Backend verifies Supabase JWT → issues long-lived App JWT (30 days).
  4. Client uses App JWT for ALL subsequent API calls.
  5. When App JWT expires → client re-exchanges via Supabase token.

App JWT is verified locally with zero network calls (~0.01ms with cache).
Supabase JWT is still accepted for backwards compatibility.
"""

import hashlib
import hmac
import logging
import time
import uuid
from typing import Optional

import httpx
from fastapi import Header, HTTPException
from jose import jwt, JWTError

from app.core.config import settings

logger = logging.getLogger(__name__)

_TICKET_TTL = 43200  # seconds (12 hours - enough for full train journey)


# ── Shared token cache (used by both App JWT and Supabase JWT) ─────────────

_token_cache: dict[str, tuple[dict, float]] = {}
_CACHE_MAX_SIZE = 20_000
_CACHE_TTL = 300  # 5 minutes


def _cleanup_cache() -> None:
    """Remove expired entries from the token cache."""
    now = time.time()
    expired = [k for k, (_, exp) in _token_cache.items() if now >= exp]
    for k in expired:
        _token_cache.pop(k, None)


# ── Custom App JWT (primary, long-lived) ──────────────────────────────────────

_MIN_SECRET_LEN = 32  # minimum secret length for security


def _validate_uuid(value: str) -> bool:
    """Check if a string is a valid UUID v4."""
    try:
        uuid.UUID(value, version=4)
        return True
    except (ValueError, AttributeError):
        return False


def create_app_token(user_id: str, email: str = "",
                     user_metadata: dict | None = None) -> str:
    """
    Issue a long-lived app JWT after Supabase authentication.

    Security:
      - Rejects empty/short secrets
      - Validates user_id is a valid UUID
      - Includes: sub, email, iss, iat, exp, nbf, jti
      - Expiry controlled by APP_TOKEN_EXPIRY_HOURS env var
    """
    if not settings.app_jwt_secret or len(settings.app_jwt_secret) < _MIN_SECRET_LEN:
        raise ValueError("APP_JWT_SECRET is missing or too short (min 32 chars)")

    if not user_id or not _validate_uuid(user_id):
        raise ValueError(f"Invalid user_id: must be a valid UUID")

    now = int(time.time())
    expiry = now + (settings.app_token_expiry_hours * 3600)

    payload = {
        "sub": user_id,
        "email": email or "",
        "user_metadata": user_metadata or {},
        "iss": "trainlive",
        "iat": now,
        "nbf": now,           # not valid before this time
        "exp": expiry,
        "jti": uuid.uuid4().hex,  # unique token ID
    }
    return jwt.encode(payload, settings.app_jwt_secret, algorithm="HS256")


def verify_app_token(token: str) -> Optional[dict]:
    """
    Verify a custom app JWT. Returns user dict or None.

    Security checks:
      1. Secret must be configured (≥32 chars)
      2. Signature verification (HS256)
      3. Required claims: sub, iss, iat, exp, nbf
      4. Issuer must be "trainlive"
      5. sub must be a valid UUID
      6. iat must not be in the future (clock skew ≤60s)
      7. exp/nbf enforced by jose library
    Uses in-memory cache for repeated tokens.
    """
    if not settings.app_jwt_secret or len(settings.app_jwt_secret) < _MIN_SECRET_LEN:
        return None

    now = time.time()

    # ── Cache hit ──
    cached = _token_cache.get(token)
    if cached is not None:
        user_data, expires_at = cached
        if now < expires_at:
            return user_data
        _token_cache.pop(token, None)

    # ── Decode & verify signature + exp + nbf ──
    try:
        payload = jwt.decode(
            token,
            settings.app_jwt_secret,
            algorithms=["HS256"],
            options={
                "require_exp": True,
                "require_iat": True,
                "require_sub": True,
            },
        )
    except JWTError:
        return None

    # ── Validate issuer ──
    if payload.get("iss") != "trainlive":
        return None

    # ── Validate sub is a real UUID ──
    sub = payload.get("sub", "")
    if not sub or not _validate_uuid(sub):
        return None

    # ── Validate iat not from the future (allow 60s clock skew) ──
    iat = payload.get("iat", 0)
    if iat > now + 60:
        logger.warning("App JWT iat is in the future: %s", iat)
        return None

    user_data = {
        "id": sub,
        "email": payload.get("email", ""),
        "user_metadata": payload.get("user_metadata", {}),
    }

    # ── Cache until token expiry or TTL, whichever is sooner ──
    token_exp = payload.get("exp", 0)
    cache_until = min(token_exp, now + _CACHE_TTL) if token_exp else now + _CACHE_TTL
    _token_cache[token] = (user_data, cache_until)
    if len(_token_cache) > _CACHE_MAX_SIZE:
        _cleanup_cache()

    return user_data


# ── Supabase JWT verification (backwards compat) ─────────────────────────────


_NO_SECRET = "NO_SECRET"  # sentinel: JWT secret not configured
_REJECTED = "REJECTED"    # sentinel: token is definitively invalid/expired


def _verify_jwt_local(access_token: str) -> str | dict:
    """
    Verify a Supabase JWT locally using the JWT secret (HS256).

    Returns:
      - dict:        valid user data
      - _NO_SECRET:  JWT secret not configured → caller should try remote
      - _REJECTED:   token is expired/invalid → caller should NOT try remote
    """
    if not settings.supabase_jwt_secret:
        return _NO_SECRET

    now = time.time()

    # ── Cache hit ──
    cached = _token_cache.get(access_token)
    if cached is not None:
        user_data, expires_at = cached
        if now < expires_at:
            return user_data
        _token_cache.pop(access_token, None)

    # ── Decode & verify ──
    try:
        payload = jwt.decode(
            access_token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except JWTError:
        return _REJECTED  # expired, bad signature, etc. → don't waste time on remote

    sub = payload.get("sub", "")
    if not sub:
        return _REJECTED

    user_data = {
        "id": sub,
        "email": payload.get("email", ""),
        "phone": payload.get("phone", ""),
        "user_metadata": payload.get("user_metadata", {}),
        "app_metadata": payload.get("app_metadata", {}),
        "role": payload.get("role", ""),
    }

    # ── Cache until token expiry or TTL, whichever is sooner ──
    token_exp = payload.get("exp", 0)
    cache_until = min(token_exp, now + _CACHE_TTL) if token_exp else now + _CACHE_TTL
    _token_cache[access_token] = (user_data, cache_until)

    # Periodic cleanup
    if len(_token_cache) > _CACHE_MAX_SIZE:
        _cleanup_cache()

    return user_data


# ── Remote Supabase verification (fallback) ──────────────────────────────────

_supabase_client: Optional[httpx.AsyncClient] = None


def _get_supabase_client() -> httpx.AsyncClient:
    """Get or create a persistent httpx client for Supabase API calls."""
    global _supabase_client
    if _supabase_client is None or _supabase_client.is_closed:
        _supabase_client = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _supabase_client


async def _verify_remote(access_token: str) -> Optional[dict]:
    """Verify token via Supabase /auth/v1/user endpoint (slow ~100-500ms)."""
    client = _get_supabase_client()
    resp = await client.get(
        f"{settings.supabase_url}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "apikey": settings.supabase_anon_key,
        },
    )
    if resp.status_code == 200:
        return resp.json()
    logger.warning("Supabase remote token verification failed: %d", resp.status_code)
    return None


async def verify_token(access_token: str) -> Optional[dict]:
    """
    Verify an access token (App JWT or Supabase JWT).

    Priority:
      1. Custom App JWT  (~0.01ms, long-lived)
      2. Supabase JWT local verification  (~0.01-0.1ms)
      3. Supabase remote /auth/v1/user  (~100-500ms, only if no secrets)
    """
    # ── 1. Try custom App JWT first (fastest, preferred) ──
    app_result = verify_app_token(access_token)
    if app_result is not None:
        return app_result

    # ── 2. Try Supabase JWT local verification ──
    result = _verify_jwt_local(access_token)

    if isinstance(result, dict):
        return result  # valid Supabase token

    if result is _REJECTED:
        return None  # expired/invalid — no point calling remote

    # result is _NO_SECRET → fall through to remote

    # ── 3. Slow path: remote Supabase API call (fallback) ──
    global _supabase_client
    for attempt in range(2):
        try:
            return await _verify_remote(access_token)
        except Exception as exc:
            logger.error(
                "Supabase token verification error (attempt %d): %s",
                attempt + 1, exc,
            )
            if _supabase_client and not _supabase_client.is_closed:
                await _supabase_client.aclose()
            _supabase_client = None
            if attempt == 0:
                continue
            return None
    return None


# Backwards-compatible alias
verify_supabase_token = verify_token


# ── User authentication dependency ───────────────────────────────────────────


async def require_authenticated_user(
    authorization: str = Header(..., description="Bearer <app_jwt or supabase_jwt>"),
) -> str:
    """
    Verify JWT (App or Supabase) and return user_id.
    Use this for Flutter app endpoints that need authenticated users (not admin).
    Raises 401 if token is missing, invalid, or expired.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing access token")

    user = await verify_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    uid = user.get("id", "")
    if not uid:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return uid


# ── HMAC ticket signing / verification ───────────────────────────────────────

def _sign(payload: str) -> str:
    """HMAC-SHA256 sign a payload string with the server secret."""
    return hmac.new(
        settings.ws_secret_key.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def create_ticket(user_id: str, train_id: str, role: str) -> str:
    """
    Create an HMAC ticket valid for 12 hours.
    Format: user_id|train_id|role|timestamp|signature
    """
    ts = str(int(time.time()))
    payload = f"{user_id}|{train_id}|{role}|{ts}"
    sig = _sign(payload)
    return f"{payload}|{sig}"


def verify_ticket(ticket: str, expected_train_id: str) -> Optional[dict]:
    """
    Verify an HMAC ticket. Returns parsed ticket data or None if invalid.
    Checks: signature validity, expiration (12h), and train_id match.
    """
    try:
        parts = ticket.split("|")
        if len(parts) != 5:
            return None

        user_id, train_id, role, ts_str, sig = parts

        # Verify train_id matches the WebSocket path
        if train_id != expected_train_id:
            logger.warning("Ticket train_id mismatch: %s vs %s", train_id, expected_train_id)
            return None

        # Verify role
        if role not in ("contributor", "listener"):
            return None

        # Verify signature
        payload = f"{user_id}|{train_id}|{role}|{ts_str}"
        expected_sig = _sign(payload)
        if not hmac.compare_digest(sig, expected_sig):
            logger.warning("Ticket signature mismatch for user %s", user_id)
            return None

        # Verify expiration
        ts = int(ts_str)
        if time.time() - ts > _TICKET_TTL:
            logger.warning("Ticket expired for user %s", user_id)
            return None

        return {"user_id": user_id, "train_id": train_id, "role": role}

    except Exception as exc:
        logger.error("Ticket verification error: %s", exc)
        return None
