"""
Account Deletion API — supports both authenticated (in-app) and public (web form) requests.

Endpoints:
  - POST /account/delete-request         → Authenticated user requests deletion
  - GET  /account/delete-status           → Check current deletion status
  - POST /account/cancel-deletion         → Cancel pending deletion
  - GET  /account/delete-page             → Public HTML page (for Google Play / App Store policy)
  - POST /account/delete-page             → Public form submission
  - POST /account/process-deletions       → Admin: process expired requests (called by cron)
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import verify_supabase_token
from app.core.admin_auth import get_admin_or_legacy_key
from app.models.account_deletion_request import AccountDeletionRequest
from app.models.device_token import DeviceToken
from app.models.profile import Profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["Account Deletion"])

DELETION_GRACE_DAYS = 30


# ── Schemas ──────────────────────────────────────────────────────────────────

class DeleteRequestIn(BaseModel):
    reason: str | None = None


class DeleteRequestOut(BaseModel):
    id: str
    status: str
    requested_at: datetime
    scheduled_deletion_at: datetime
    reason: str | None = None


class DeletionStatusOut(BaseModel):
    has_pending_request: bool
    request: DeleteRequestOut | None = None
    days_remaining: int | None = None


class WebDeleteRequestIn(BaseModel):
    email: str
    reason: str | None = None


# ── Auth helper ──────────────────────────────────────────────────────────────

async def _get_user(authorization: str = Header(...)) -> dict:
    """Extract and verify user from Supabase JWT."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    user = await verify_supabase_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    uid = user.get("id", "")
    if not uid:
        raise HTTPException(status_code=401, detail="User ID not found")
    return user




# ── Authenticated Endpoints ──────────────────────────────────────────────────

@router.post("/delete-request", response_model=DeleteRequestOut)
async def request_deletion(
    body: DeleteRequestIn,
    user: dict = Depends(_get_user),
    db: AsyncSession = Depends(get_db),
):
    """Request account deletion (30-day grace period)."""
    user_id = user["id"]
    email = user.get("email", "")

    # Check for existing pending request
    result = await db.execute(
        select(AccountDeletionRequest)
        .where(
            AccountDeletionRequest.user_id == user_id,
            AccountDeletionRequest.status == "pending",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="A deletion request is already pending",
        )

    now = datetime.now(timezone.utc)
    scheduled = now + timedelta(days=DELETION_GRACE_DAYS)

    req = AccountDeletionRequest(
        user_id=user_id,
        email=email,
        reason=body.reason,
        status="pending",
        requested_at=now,
        scheduled_deletion_at=scheduled,
    )
    db.add(req)
    await db.flush()

    logger.info("Account deletion requested: user=%s, scheduled=%s", user_id, scheduled.isoformat())

    return DeleteRequestOut(
        id=req.id,
        status=req.status,
        requested_at=req.requested_at,
        scheduled_deletion_at=req.scheduled_deletion_at,
        reason=req.reason,
    )


@router.get("/delete-status", response_model=DeletionStatusOut)
async def get_deletion_status(
    user: dict = Depends(_get_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if user has a pending deletion request."""
    user_id = user["id"]

    result = await db.execute(
        select(AccountDeletionRequest)
        .where(
            AccountDeletionRequest.user_id == user_id,
            AccountDeletionRequest.status == "pending",
        )
    )
    req = result.scalar_one_or_none()

    if not req:
        return DeletionStatusOut(has_pending_request=False)

    now = datetime.now(timezone.utc)
    days_remaining = max(0, (req.scheduled_deletion_at.replace(tzinfo=timezone.utc) - now).days)

    return DeletionStatusOut(
        has_pending_request=True,
        request=DeleteRequestOut(
            id=req.id,
            status=req.status,
            requested_at=req.requested_at,
            scheduled_deletion_at=req.scheduled_deletion_at,
            reason=req.reason,
        ),
        days_remaining=days_remaining,
    )


@router.post("/cancel-deletion")
async def cancel_deletion(
    user: dict = Depends(_get_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a pending deletion request."""
    user_id = user["id"]

    result = await db.execute(
        select(AccountDeletionRequest)
        .where(
            AccountDeletionRequest.user_id == user_id,
            AccountDeletionRequest.status == "pending",
        )
    )
    req = result.scalar_one_or_none()

    if not req:
        raise HTTPException(status_code=404, detail="No pending deletion request found")

    req.status = "cancelled"
    req.cancelled_at = datetime.now(timezone.utc)
    await db.flush()

    logger.info("Account deletion cancelled: user=%s", user_id)
    return {"message": "Deletion request cancelled successfully"}


# ── Public Web Page (for Google Play / App Store compliance) ─────────────────

_WEB_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Delete Account - TrainLive Egypt</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            max-width: 500px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.1);
        }
        .logo {
            text-align: center;
            margin-bottom: 24px;
        }
        .logo-icon {
            width: 60px; height: 60px;
            background: linear-gradient(135deg, #1B4F72, #2E86C1);
            border-radius: 15px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            margin-bottom: 12px;
        }
        h1 {
            font-size: 24px;
            color: #1B4F72;
            text-align: center;
            margin-bottom: 8px;
        }
        .subtitle {
            color: #666;
            text-align: center;
            font-size: 14px;
            margin-bottom: 32px;
            line-height: 1.6;
        }
        .warning {
            background: #FFF3CD;
            border: 1px solid #FFE69C;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 24px;
            font-size: 14px;
            color: #664D03;
            line-height: 1.6;
        }
        .warning strong { color: #E74C3C; }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            font-weight: 600;
            color: #333;
            margin-bottom: 8px;
            font-size: 14px;
        }
        input, textarea {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e0e0e0;
            border-radius: 12px;
            font-size: 15px;
            transition: border-color 0.2s;
            font-family: inherit;
        }
        input:focus, textarea:focus {
            outline: none;
            border-color: #1B4F72;
        }
        textarea { resize: vertical; min-height: 80px; }
        .btn {
            width: 100%;
            padding: 14px;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-danger {
            background: #E74C3C;
            color: white;
        }
        .btn-danger:hover { background: #C0392B; }
        .btn-danger:disabled { background: #ccc; cursor: not-allowed; }
        .success {
            background: #D4EDDA;
            border: 1px solid #C3E6CB;
            border-radius: 12px;
            padding: 20px;
            text-align: center;
            color: #155724;
            line-height: 1.6;
        }
        .success h2 { color: #155724; margin-bottom: 8px; }
        .error-msg {
            background: #F8D7DA;
            border: 1px solid #F5C2C7;
            border-radius: 12px;
            padding: 12px;
            color: #842029;
            font-size: 14px;
            margin-bottom: 16px;
            display: none;
        }
        .info {
            font-size: 12px;
            color: #999;
            text-align: center;
            margin-top: 16px;
            line-height: 1.5;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <div class="logo-icon">🚂</div>
            <h1>Delete Your Account</h1>
            <p class="subtitle">
                TrainLive Egypt — Account Deletion Request<br>
                Your account and all associated data will be permanently deleted after 30 days.
            </p>
        </div>

        <div id="form-section">
            <div class="warning">
                <strong>⚠️ Warning:</strong> This action will schedule your account for permanent deletion.
                After 30 days, all your data including profile, search history, chat history, and preferences
                will be permanently removed. You can cancel this request within the 30-day period by logging
                into the app.
            </div>

            <div id="error-msg" class="error-msg"></div>

            <form id="delete-form" onsubmit="submitForm(event)">
                <div class="form-group">
                    <label for="email">Email Address *</label>
                    <input type="email" id="email" name="email" required
                           placeholder="Enter your account email">
                </div>
                <div class="form-group">
                    <label for="reason">Reason (optional)</label>
                    <textarea id="reason" name="reason"
                              placeholder="Tell us why you want to delete your account..."></textarea>
                </div>
                <button type="submit" class="btn btn-danger" id="submit-btn">
                    Request Account Deletion
                </button>
            </form>

            <p class="info">
                By submitting this form, you confirm that you want to delete your TrainLive Egypt account.
                A confirmation will be sent to your email address.
            </p>
        </div>

        <div id="success-section" style="display: none;">
            <div class="success">
                <h2>✅ Request Submitted</h2>
                <p>
                    Your account deletion request has been received.<br>
                    Your account will be permanently deleted after <strong>30 days</strong>.<br><br>
                    You can cancel this request by logging into the TrainLive Egypt app
                    and going to Settings.
                </p>
            </div>
        </div>
    </div>

    <script>
        async function submitForm(e) {
            e.preventDefault();
            const btn = document.getElementById('submit-btn');
            const errDiv = document.getElementById('error-msg');
            btn.disabled = true;
            btn.textContent = 'Submitting...';
            errDiv.style.display = 'none';

            const email = document.getElementById('email').value;
            const reason = document.getElementById('reason').value;

            try {
                const resp = await fetch(window.location.pathname, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, reason })
                });
                const data = await resp.json();
                if (resp.ok) {
                    document.getElementById('form-section').style.display = 'none';
                    document.getElementById('success-section').style.display = 'block';
                } else {
                    errDiv.textContent = data.detail || 'An error occurred. Please try again.';
                    errDiv.style.display = 'block';
                    btn.disabled = false;
                    btn.textContent = 'Request Account Deletion';
                }
            } catch (err) {
                errDiv.textContent = 'Network error. Please try again.';
                errDiv.style.display = 'block';
                btn.disabled = false;
                btn.textContent = 'Request Account Deletion';
            }
        }
    </script>
</body>
</html>
"""


@router.get("/delete-page", response_class=HTMLResponse)
async def delete_page():
    """Public HTML page for account deletion (Google Play / App Store compliance)."""
    return HTMLResponse(content=_WEB_PAGE_HTML)


@router.post("/delete-page")
async def submit_web_deletion(
    body: WebDeleteRequestIn,
    db: AsyncSession = Depends(get_db),
):
    """Handle public web form submission for account deletion."""
    email = body.email.strip().lower()

    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    # Find user profile by email
    result = await db.execute(
        select(Profile).where(Profile.email == email)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        raise HTTPException(
            status_code=404,
            detail="No account found with this email address",
        )

    user_id = str(profile.id)

    # Check for existing pending request
    result = await db.execute(
        select(AccountDeletionRequest)
        .where(
            AccountDeletionRequest.user_id == user_id,
            AccountDeletionRequest.status == "pending",
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="A deletion request is already pending for this account",
        )

    now = datetime.now(timezone.utc)
    scheduled = now + timedelta(days=DELETION_GRACE_DAYS)

    req = AccountDeletionRequest(
        user_id=user_id,
        email=email,
        reason=body.reason,
        status="pending",
        requested_at=now,
        scheduled_deletion_at=scheduled,
    )
    db.add(req)
    await db.flush()

    logger.info(
        "Web account deletion requested: email=%s, user=%s, scheduled=%s",
        email, profile.id, scheduled.isoformat(),
    )

    return {
        "message": "Account deletion request submitted",
        "scheduled_deletion_at": scheduled.isoformat(),
    }


# ── Admin: Process Expired Deletions (called by cron / scheduler) ────────────

@router.post("/process-deletions")
async def process_deletions(
    _=Depends(get_admin_or_legacy_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Process all pending deletion requests where grace period has expired.
    This should be called by a cron job (e.g., daily).
    """
    import httpx

    now = datetime.now(timezone.utc)

    # Find all expired pending requests
    result = await db.execute(
        select(AccountDeletionRequest)
        .where(
            AccountDeletionRequest.status == "pending",
            AccountDeletionRequest.scheduled_deletion_at <= now,
        )
    )
    expired_requests = result.scalars().all()

    if not expired_requests:
        return {"message": "No expired requests to process", "processed": 0}

    processed = 0
    errors = []

    for req in expired_requests:
        try:
            # 1. Delete user from Supabase Auth using admin API
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"{settings.supabase_url}/auth/v1/admin/users/{req.user_id}",
                    headers={
                        "Authorization": f"Bearer {settings.supabase_service_role_key}",
                        "apikey": settings.supabase_service_role_key,
                    },
                )
                if resp.status_code not in (200, 204, 404):
                    logger.error(
                        "Failed to delete Supabase user %s: %d %s",
                        req.user_id, resp.status_code, resp.text,
                    )
                    errors.append(f"Supabase delete failed for {req.user_id}: {resp.status_code}")
                    continue

            # 2. Delete device tokens
            await db.execute(
                DeviceToken.__table__.delete().where(
                    DeviceToken.user_id == req.user_id
                )
            )

            # 3. Deactivate profile (or delete)
            await db.execute(
                update(Profile)
                .where(Profile.id == req.user_id)
                .values(is_active=False, email=None, display_name="Deleted User", avatar_url=None)
            )

            # 4. Mark deletion request as completed
            req.status = "completed"
            req.completed_at = now

            processed += 1
            logger.info("Account permanently deleted: user=%s", req.user_id)

        except Exception as exc:
            logger.error("Error processing deletion for user %s: %s", req.user_id, exc)
            errors.append(f"Error for {req.user_id}: {str(exc)}")

    await db.flush()

    return {
        "message": f"Processed {processed} deletions",
        "processed": processed,
        "errors": errors if errors else None,
    }
