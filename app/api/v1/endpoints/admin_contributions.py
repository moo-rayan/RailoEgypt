"""
Admin endpoints for contribution rewards and redemption requests.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.core.admin_auth import AdminUser, get_admin_or_legacy_key, require_fulladmin
from app.services.audit_service import audit
from app.services.contribution_reward_service import (
    InvalidRewardRedemptionTransition,
    RewardCatalogItemNotFound,
    list_reward_contributors,
    list_reward_redemptions,
    update_reward_redemption_status,
)

router = APIRouter(prefix="/admin/contributions", tags=["Admin Contributions"])


class RewardRedemptionStatusBody(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected|fulfilled|cancelled)$")
    admin_note: str = Field("", max_length=1000)


@router.get("/contributors", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_reward_contributors(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    search: str = Query("", max_length=120),
    sort_by: str = Query(
        "points",
        pattern="^(points|balance|reserved|redeemed|contributions|distance|last)$",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
):
    return await list_reward_contributors(
        page=page,
        limit=limit,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get("/redemptions", dependencies=[Depends(get_admin_or_legacy_key)])
async def get_reward_redemptions(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    status_filter: str = Query(
        "all",
        alias="status",
        pattern="^(all|pending|approved|rejected|fulfilled|cancelled)$",
    ),
    search: str = Query("", max_length=120),
):
    return await list_reward_redemptions(
        page=page,
        limit=limit,
        status_filter=status_filter,
        search=search,
    )


@router.post("/redemptions/{request_id}/status")
async def set_reward_redemption_status(
    request_id: str,
    body: RewardRedemptionStatusBody,
    request: Request,
    admin: AdminUser = Depends(require_fulladmin),
):
    try:
        redemption = await update_reward_redemption_status(
            request_id=request_id,
            admin_user_id=admin.user_id,
            status_value=body.status,
            admin_note=body.admin_note,
        )
    except RewardCatalogItemNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reward redemption request not found",
        ) from exc
    except InvalidRewardRedemptionTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    audit.log_admin_action(
        request,
        action="update_reward_redemption",
        user_id=admin.user_id,
        metadata={
            "request_id": request_id,
            "status": body.status,
            "target_user": redemption.get("user_id"),
            "points_required": redemption.get("points_required"),
            "reward_key": redemption.get("reward_key"),
        },
    )
    return {"ok": True, "redemption": redemption}
