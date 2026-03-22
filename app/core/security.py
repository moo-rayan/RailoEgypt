"""
WebSocket security: HMAC-signed tickets + Supabase JWT verification.

Flow:
  1. Client calls POST /api/v1/live/ticket  (Authorization: Bearer <supabase_jwt>)
  2. Backend verifies JWT via Supabase Auth API → gets user_id
  3. Backend returns HMAC-signed ticket (valid 12h, bound to user + train + role)
  4. Client connects to ws://.../api/v1/live/{train_id}?ticket=<ticket>
  5. Backend verifies HMAC ticket on WebSocket handshake
"""

import hashlib
import hmac
import logging
import time
from typing import Optional

import httpx
from fastapi import Header, HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

_TICKET_TTL = 43200  # seconds (12 hours - enough for full train journey)


# ── Supabase JWT verification ────────────────────────────────────────────────

_supabase_client: Optional[httpx.AsyncClient] = None


def _get_supabase_client() -> httpx.AsyncClient:
    """Get or create a persistent httpx client for Supabase API calls."""
    global _supabase_client
    if _supabase_client is None or _supabase_client.is_closed:
        _supabase_client = httpx.AsyncClient(
            timeout=10,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _supabase_client


async def _verify_once(access_token: str) -> Optional[dict]:
    """Single attempt to verify a Supabase token."""
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
    logger.warning("Supabase token verification failed: %d", resp.status_code)
    return None


async def verify_supabase_token(access_token: str) -> Optional[dict]:
    """
    Verify a Supabase access token by calling the GoTrue /auth/v1/user endpoint.
    Returns the user dict on success, None on failure.
    Uses a persistent client with one retry on connection errors.
    """
    global _supabase_client
    for attempt in range(2):
        try:
            return await _verify_once(access_token)
        except Exception as exc:
            logger.error(
                "Supabase token verification error (attempt %d): %s",
                attempt + 1, exc,
            )
            # Close stale client and retry with a fresh connection
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
