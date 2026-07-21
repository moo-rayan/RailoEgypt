import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import get_admin_or_legacy_key
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


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


@router.get("/admin/votes", dependencies=[Depends(get_admin_or_legacy_key)])
async def list_feature_votes(
    q: str | None = Query(None, min_length=1),
    feature_key: str | None = Query(None, min_length=1),
    vote_value: str | None = Query(None, min_length=1),
    target_type: str | None = Query(None, min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    filters: list[str] = []
    params: dict[str, Any] = {}

    if feature_key:
        filters.append("fv.feature_key = :feature_key")
        params["feature_key"] = feature_key.strip()
    if vote_value:
        filters.append("fv.vote_value = :vote_value")
        params["vote_value"] = vote_value.strip()
    if target_type:
        filters.append("fv.target_type = :target_type")
        params["target_type"] = target_type.strip()
    if q:
        params["q"] = f"%{q.strip()}%"
        filters.append(
            """
            (
                fv.feature_key ILIKE :q
                OR fv.vote_value ILIKE :q
                OR fv.target_type ILIKE :q
                OR fv.target_id ILIKE :q
                OR fv.user_id::text ILIKE :q
                OR p.email ILIKE :q
                OR p.display_name ILIKE :q
                OR fv.context_data::text ILIKE :q
                OR fv.client_metadata::text ILIKE :q
            )
            """
        )

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    page_params = {
        **params,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }

    try:
        total_result = await db.execute(
            text(
                f"""
                SELECT COUNT(*) AS total
                FROM "EgRailway".feature_votes fv
                LEFT JOIN "EgRailway".profiles p ON p.id = fv.user_id
                {where_clause}
                """
            ),
            params,
        )
        total = int(total_result.scalar_one() or 0)

        summary_result = await db.execute(
            text(
                f"""
                SELECT fv.vote_value, COUNT(*) AS count
                FROM "EgRailway".feature_votes fv
                LEFT JOIN "EgRailway".profiles p ON p.id = fv.user_id
                {where_clause}
                GROUP BY fv.vote_value
                ORDER BY count DESC, fv.vote_value ASC
                """
            ),
            params,
        )

        result = await db.execute(
            text(
                f"""
                SELECT
                    fv.id,
                    fv.user_id::text AS user_id,
                    p.email,
                    p.display_name,
                    fv.feature_key,
                    fv.vote_value,
                    fv.target_type,
                    fv.target_id,
                    fv.context_data,
                    fv.client_metadata,
                    fv.source,
                    fv.created_at,
                    fv.updated_at
                FROM "EgRailway".feature_votes fv
                LEFT JOIN "EgRailway".profiles p ON p.id = fv.user_id
                {where_clause}
                ORDER BY fv.updated_at DESC, fv.id DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            page_params,
        )

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "summary": [
                {"vote_value": row["vote_value"], "count": row["count"]}
                for row in summary_result.mappings().all()
            ],
            "items": [
                {
                    **dict(row),
                    "context_data": _json_safe(row["context_data"]),
                    "client_metadata": _json_safe(row["client_metadata"]),
                }
                for row in result.mappings().all()
            ],
        }
    except Exception as exc:
        logger.error("Failed to list feature votes: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load votes",
        ) from exc
