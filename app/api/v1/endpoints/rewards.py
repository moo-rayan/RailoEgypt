from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import require_authenticated_user
from app.services.contribution_reward_service import (
    InsufficientRewardPoints,
    RewardCatalogItemNotFound,
    get_pending_reward_summaries,
    get_reward_leaderboard,
    get_reward_profile,
    mark_reward_seen,
    request_reward_redemption,
)
from app.services.tracking_manager import tracking_manager

router = APIRouter(prefix="/rewards", tags=["Rewards"])


class RewardRedemptionRequestBody(BaseModel):
    reward_key: str = Field(..., min_length=1, max_length=80)
    user_note: str = Field("", max_length=500)


@router.get("/profile")
async def rewards_profile(user_id: str = Depends(require_authenticated_user)):
    return await get_reward_profile(user_id)


@router.get("/leaderboard")
async def rewards_leaderboard(
    limit: int = Query(50, ge=1, le=100),
    user_id: str = Depends(require_authenticated_user),
):
    return await get_reward_leaderboard(user_id=user_id, limit=limit)


@router.post("/redeem", status_code=status.HTTP_201_CREATED)
async def redeem_reward(
    body: RewardRedemptionRequestBody,
    user_id: str = Depends(require_authenticated_user),
):
    try:
        redemption = await request_reward_redemption(
            user_id=user_id,
            reward_key=body.reward_key,
            user_note=body.user_note,
        )
    except RewardCatalogItemNotFound as exc:
        raise HTTPException(status_code=404, detail="Reward item not found") from exc
    except InsufficientRewardPoints as exc:
        raise HTTPException(
            status_code=400,
            detail="Not enough reward points",
        ) from exc
    return {"ok": True, "redemption": redemption}


@router.get("/contributions/pending")
async def pending_contribution_rewards(
    limit: int = Query(3, ge=1, le=10),
    user_id: str = Depends(require_authenticated_user),
):
    active_trains = tracking_manager.active_contribution_train_numbers(user_id)
    return {
        "items": await get_pending_reward_summaries(
            user_id,
            limit=limit,
            exclude_train_numbers=active_trains,
        ),
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
