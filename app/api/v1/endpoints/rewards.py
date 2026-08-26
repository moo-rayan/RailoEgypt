from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import require_authenticated_user
from app.services.contribution_reward_service import (
    get_pending_reward_summaries,
    get_reward_profile,
    mark_reward_seen,
)

router = APIRouter(prefix="/rewards", tags=["Rewards"])


@router.get("/profile")
async def rewards_profile(user_id: str = Depends(require_authenticated_user)):
    return await get_reward_profile(user_id)


@router.get("/contributions/pending")
async def pending_contribution_rewards(
    limit: int = Query(3, ge=1, le=10),
    user_id: str = Depends(require_authenticated_user),
):
    return {
        "items": await get_pending_reward_summaries(user_id, limit=limit),
    }


@router.post("/contributions/{contribution_id}/seen")
async def mark_contribution_reward_seen(
    contribution_id: str,
    user_id: str = Depends(require_authenticated_user),
):
    ok = await mark_reward_seen(user_id, contribution_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Contribution reward not found")
    return {"ok": True}
