"""
WebSocket security: HMAC-signed tickets + Supabase JWT verification.

Flow:
  1. Client calls POST /api/v1/live/ticket  (Authorization: Bearer <supabase_jwt>)
  2. Backend verifies JWT locally (HS256) → gets user_id  [FAST ~0.1ms]
  3. Backend returns HMAC-signed ticket (valid 12h, bound to user + train + role)
  4. Client connects to ws://.../api/v1/live/{train_id}?ticket=<ticket>
  5. Backend verifies HMAC ticket on WebSocket handshake

Performance:
  Local JWT verification eliminates the remote HTTP call to Supabase /auth/v1/user
  that was the #1 bottleneck (~100-500ms per request). With in-memory caching,
  repeated tokens are verified in ~0.01ms.
"""

import hashlib
import hmac
import logging
import time
from typing import Optional

import httpx
from fastapi import Header, HTTPException
from jose import jwt, JWTError

from app.core.config import settings

logger = logging.getLogger(__name__)

_TICKET_TTL = 43200  # seconds (12 hours - enough for full train journey)


# ── Local JWT verification (fast path) ────────────────────────────────────────
#
# Supabase JWTs are signed with HS256. By verifying locally we avoid a
# network round-trip to Supabase /auth/v1/user on every request.
# An in-memory cache further speeds up repeated tokens (~0.01ms).
# Remote verification is kept as a fallback when no JWT secret is configured.
# ──────────────────────────────────────────────────────────────────────────────

_token_cache: dict[str, tuple[dict, float]] = {}
_CACHE_MAX_SIZE = 20_000
_CACHE_TTL = 300  # 5 minutes


def _cleanup_cache() -> None:
    """Remove expired entries from the token cache."""
    now = time.time()
    expired = [k for k, (_, exp) in _token_cache.items() if now >= exp]
    for k in expired:
        _token_cache.pop(k, None)


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


async def verify_supabase_token(access_token: str) -> Optional[dict]:
    """
    Verify a Supabase access token.

    Strategy (fast → slow):
      1. Local HS256 verification + in-memory cache  (~0.01-0.1ms)
      2. Remote /auth/v1/user call                   (~100-500ms)  [only if no JWT secret]

    If the JWT secret is configured and the token is expired/invalid,
    we return None immediately without wasting time on a remote call.
    """
    # ── Fast path: local JWT verification ──
    result = _verify_jwt_local(access_token)

    if isinstance(result, dict):
        return result  # valid token

    if result is _REJECTED:
        return None  # expired/invalid — no point calling remote

    # result is _NO_SECRET → fall through to remote

    # ── Slow path: remote Supabase API call (fallback) ──
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


# ── User authentication dependency ───────────────────────────────────────────


async def require_authenticated_user(
    authorization: str = Header(..., description="Bearer <supabase_jwt>"),
) -> str:
    """
    Verify Supabase JWT and return user_id.
    Use this for Flutter app endpoints that need authenticated users (not admin).
    Raises 401 if token is missing, invalid, or expired.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing access token")

    user = await verify_supabase_token(token)
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
