"""
Auth endpoints: exchange Supabase token for long-lived App JWT.

Flow:
  1. Flutter authenticates via Supabase (Google/Facebook OAuth).
  2. Flutter calls POST /auth/token with Supabase access token.
  3. Backend verifies Supabase token → issues App JWT (long-lived).
  4. Flutter stores App JWT and uses it for ALL subsequent API calls.
  5. When App JWT expires → Flutter calls POST /auth/refresh with the
     old App JWT → Backend issues a new App JWT (NO Supabase needed).

Security:
  - /auth/token: Only accepts Supabase tokens (not App JWTs).
  - /auth/refresh: Only accepts App JWTs (not Supabase tokens).
    Allows expired tokens within a configurable grace period.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.core.security import (
    create_app_token,
    verify_app_token_for_refresh,
    verify_supabase_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


class TokenExchangeRequest(BaseModel):
    supabase_token: str

    @field_validator("supabase_token")
    @classmethod
    def token_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) < 20:
            raise ValueError("supabase_token is missing or too short")
        return v


class RefreshRequest(BaseModel):
    app_token: str

    @field_validator("app_token")
    @classmethod
    def token_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) < 20:
            raise ValueError("app_token is missing or too short")
        return v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


@router.post("/token", response_model=TokenResponse)
async def exchange_token(body: TokenExchangeRequest):
    """
    Exchange a valid Supabase JWT for a long-lived App JWT.
    The App JWT contains user_id, email, and metadata.

    Only accepts Supabase tokens — rejects App JWTs to prevent recycling.
    """
    from app.core.config import settings
    from app.core.security import verify_app_token

    # Reject if caller sends an existing App JWT (prevent token recycling)
    if verify_app_token(body.supabase_token) is not None:
        raise HTTPException(
            status_code=400,
            detail="Cannot exchange an App JWT — send a Supabase token",
        )

    # Verify the Supabase token
    user = await verify_supabase_token(body.supabase_token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired Supabase token",
        )

    user_id = user.get("id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    # Issue our own long-lived token
    try:
        app_token = create_app_token(
            user_id=user_id,
            email=user.get("email", ""),
            user_metadata=user.get("user_metadata", {}),
        )
    except ValueError as e:
        logger.error("Failed to create app token: %s", e)
        raise HTTPException(status_code=500, detail="Token generation failed")

    expires_in = settings.app_token_expiry_hours * 3600

    logger.info("App token issued for user %s (expires in %dh)",
                user_id, settings.app_token_expiry_hours)

    return TokenResponse(
        access_token=app_token,
        expires_in=expires_in,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest):
    """
    Self-refresh: exchange an existing App JWT for a new one.

    Accepts expired App JWTs within the grace period
    (default 7 days after expiry). No Supabase token needed.

    This is the PRIMARY renewal mechanism — the app never needs
    to go back to Supabase after initial login.
    """
    from app.core.config import settings

    # Verify the old App JWT (allows expired within grace period)
    user = verify_app_token_for_refresh(body.app_token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="App token invalid or expired beyond grace period",
        )

    user_id = user.get("id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    # Issue a fresh App JWT with full lifetime
    try:
        new_token = create_app_token(
            user_id=user_id,
            email=user.get("email", ""),
            user_metadata=user.get("user_metadata", {}),
        )
    except ValueError as e:
        logger.error("Failed to refresh app token: %s", e)
        raise HTTPException(status_code=500, detail="Token refresh failed")

    expires_in = settings.app_token_expiry_hours * 3600

    logger.info("App token refreshed for user %s (expires in %dh)",
                user_id, settings.app_token_expiry_hours)

    return TokenResponse(
        access_token=new_token,
        expires_in=expires_in,
    )
