import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_authenticated_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeatureVoteRequest(BaseModel):
    feature_key: str = Field(..., min_length=1, max_length=80)
    vote_value: str = Field(..., min_length=1, max_length=80)
    target_type: str = Field("global", max_length=50)
    target_id: str = Field("", max_length=120)
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = Field("mobile_app", max_length=40)

    @field_validator("feature_key", "vote_value", "target_type", "source")
    @classmethod
    def _required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value cannot be blank")
        return cleaned

    @field_validator("target_id")
    @classmethod
    def _optional_text(cls, value: str) -> str:
        return value.strip()


@router.post("/votes", status_code=status.HTTP_201_CREATED)
async def submit_feature_vote(
    body: FeatureVoteRequest,
    user_id: str = Depends(require_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await db.execute(
            text(
                """
                INSERT INTO "EgRailway".feature_votes
                    (
                        user_id,
                        feature_key,
                        vote_value,
                        target_type,
                        target_id,
                        context_data,
                        client_metadata,
                        source
                    )
                VALUES
                    (
                        CAST(:user_id AS UUID),
                        :feature_key,
                        :vote_value,
                        :target_type,
                        :target_id,
                        CAST(:context_data AS JSONB),
                        CAST(:client_metadata AS JSONB),
                        :source
                    )
                ON CONFLICT (user_id, feature_key, target_type, target_id)
                DO UPDATE SET
                    vote_value = EXCLUDED.vote_value,
                    context_data = EXCLUDED.context_data,
                    client_metadata = EXCLUDED.client_metadata,
                    source = EXCLUDED.source,
                    updated_at = now()
                RETURNING id, feature_key, vote_value, target_type, target_id,
                          created_at, updated_at
                """
            ),
            {
                "user_id": user_id,
                "feature_key": body.feature_key,
                "vote_value": body.vote_value,
                "target_type": body.target_type,
                "target_id": body.target_id,
                "context_data": json.dumps(body.context, ensure_ascii=False),
                "client_metadata": json.dumps(body.metadata, ensure_ascii=False),
                "source": body.source,
            },
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Vote was not saved",
            )

        await db.commit()
        return {
            "ok": True,
            "id": row["id"],
            "feature_key": row["feature_key"],
            "vote_value": row["vote_value"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to submit feature vote: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit vote",
        ) from exc
