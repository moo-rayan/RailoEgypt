import logging
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)

POINTS_PER_KM = 2.0
MIN_REWARDED_DISTANCE_M = 250.0

REWARD_CATALOG: list[dict[str, Any]] = [
    {"title_ar": "فكة 5 جنيه", "title_en": "EGP 5 top-up", "points_required": 550},
    {"title_ar": "شحن 10 جنيه", "title_en": "EGP 10 top-up", "points_required": 1100},
    {"title_ar": "شحن 15 جنيه", "title_en": "EGP 15 top-up", "points_required": 1650},
    {"title_ar": "شحن 25 جنيه", "title_en": "EGP 25 top-up", "points_required": 2750},
    {"title_ar": "شحن 50 جنيه", "title_en": "EGP 50 top-up", "points_required": 5500},
]


def _utc_from_ts(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _format_distance(distance_km: float) -> float:
    return round(distance_km, 1)


def _summary_from_row(row: Any) -> dict[str, Any]:
    distance_km = float(row.trusted_distance_m or 0) / 1000.0
    points = int(row.points_awarded or 0)
    train_number = str(row.train_number or "")
    return {
        "id": str(row.id),
        "train_number": train_number,
        "distance_km": _format_distance(distance_km),
        "points_awarded": points,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "message_ar": (
            "شكراً لمساهمتك ❤️\n"
            f"ساهمت في تتبع القطار لمسافة {_format_distance(distance_km)} كم\n"
            f"وتم إضافة {points} نقطة لرصيدك."
        ),
        "message_en": (
            "Thanks for contributing.\n"
            f"You helped track train {train_number} for {_format_distance(distance_km)} km.\n"
            f"{points} points were added to your balance."
        ),
    }


async def finalize_contribution_session(
    *,
    session_id: str,
    user_id: str,
    train_number: str,
    trip_id: int | None,
    from_station_name: str,
    to_station_name: str,
    started_at_ts: float,
    ended_at_ts: float,
    end_reason: str,
    is_silent: bool,
    accepted_updates_count: int,
    rejected_updates_count: int,
    raw_distance_m: float,
    trusted_distance_m: float,
    first_lat: float | None,
    first_lng: float | None,
    last_lat: float | None,
    last_lng: float | None,
    max_reported_speed_kmh: float,
    max_rail_distance_m: float,
    max_train_distance_m: float,
) -> dict[str, Any] | None:
    """
    Persist one finished contribution session.

    Security boundary:
      - This function is called only from server-side tracking state.
      - Points are calculated from trusted accepted GPS updates, never from
        client-supplied points or distance.
    """
    trusted_distance_m = max(0.0, float(trusted_distance_m or 0.0))
    raw_distance_m = max(0.0, float(raw_distance_m or 0.0))
    accepted_updates_count = max(0, int(accepted_updates_count or 0))
    rejected_updates_count = max(0, int(rejected_updates_count or 0))

    should_reward = (
        accepted_updates_count >= 2
        and trusted_distance_m >= MIN_REWARDED_DISTANCE_M
    )
    points_awarded = (
        math.floor((trusted_distance_m / 1000.0) * POINTS_PER_KM)
        if should_reward
        else 0
    )
    status = "completed" if points_awarded > 0 else "discarded"

    try:
        async with AsyncSessionFactory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO "EgRailway".contribution_sessions (
                            id,
                            user_id,
                            train_number,
                            trip_id,
                            from_station_name,
                            to_station_name,
                            started_at,
                            ended_at,
                            end_reason,
                            status,
                            is_silent,
                            accepted_updates_count,
                            rejected_updates_count,
                            raw_distance_m,
                            trusted_distance_m,
                            points_rate_per_km,
                            points_awarded,
                            first_lat,
                            first_lng,
                            last_lat,
                            last_lng,
                            max_reported_speed_kmh,
                            max_rail_distance_m,
                            max_train_distance_m
                        )
                        VALUES (
                            CAST(:session_id AS uuid),
                            CAST(:user_id AS uuid),
                            :train_number,
                            :trip_id,
                            :from_station_name,
                            :to_station_name,
                            :started_at,
                            :ended_at,
                            :end_reason,
                            :status,
                            :is_silent,
                            :accepted_updates_count,
                            :rejected_updates_count,
                            :raw_distance_m,
                            :trusted_distance_m,
                            :points_rate_per_km,
                            :points_awarded,
                            :first_lat,
                            :first_lng,
                            :last_lat,
                            :last_lng,
                            :max_reported_speed_kmh,
                            :max_rail_distance_m,
                            :max_train_distance_m
                        )
                        RETURNING
                            id::text AS id,
                            train_number,
                            trusted_distance_m,
                            points_awarded,
                            started_at,
                            ended_at
                        """
                    ),
                    {
                        "user_id": user_id,
                        "session_id": session_id,
                        "train_number": train_number,
                        "trip_id": trip_id,
                        "from_station_name": from_station_name or "",
                        "to_station_name": to_station_name or "",
                        "started_at": _utc_from_ts(started_at_ts),
                        "ended_at": _utc_from_ts(ended_at_ts),
                        "end_reason": end_reason or "",
                        "status": status,
                        "is_silent": bool(is_silent),
                        "accepted_updates_count": accepted_updates_count,
                        "rejected_updates_count": rejected_updates_count,
                        "raw_distance_m": round(raw_distance_m, 2),
                        "trusted_distance_m": round(trusted_distance_m, 2),
                        "points_rate_per_km": POINTS_PER_KM,
                        "points_awarded": points_awarded,
                        "first_lat": first_lat,
                        "first_lng": first_lng,
                        "last_lat": last_lat,
                        "last_lng": last_lng,
                        "max_reported_speed_kmh": round(max_reported_speed_kmh or 0.0, 2),
                        "max_rail_distance_m": max_rail_distance_m,
                        "max_train_distance_m": max_train_distance_m,
                    },
                )
            ).first()

            if points_awarded > 0:
                await session.execute(
                    text(
                        """
                        UPDATE "EgRailway".profiles
                        SET
                            is_contributor = TRUE,
                            contribution_count = contribution_count + 1,
                            total_contribution_distance_km =
                                total_contribution_distance_km + :trusted_distance_km,
                            reward_points_balance = reward_points_balance + :points_awarded,
                            reward_points_lifetime = reward_points_lifetime + :points_awarded,
                            last_contribution_at = :ended_at,
                            updated_at = now()
                        WHERE id = CAST(:user_id AS uuid)
                        """
                    ),
                    {
                        "user_id": user_id,
                        "trusted_distance_km": round(trusted_distance_m / 1000.0, 2),
                        "points_awarded": points_awarded,
                        "ended_at": _utc_from_ts(ended_at_ts),
                    },
                )

            await session.commit()

        if not row or points_awarded <= 0:
            logger.info(
                "Contribution discarded: train=%s user=%s trusted=%.1fm accepted=%d",
                train_number,
                user_id[:8],
                trusted_distance_m,
                accepted_updates_count,
            )
            return None

        logger.info(
            "Reward awarded: train=%s user=%s distance=%.1fkm points=%d",
            train_number,
            user_id[:8],
            trusted_distance_m / 1000.0,
            points_awarded,
        )
        return _summary_from_row(row)
    except Exception as exc:
        logger.exception(
            "Failed to finalize contribution reward: train=%s user=%s error=%s",
            train_number,
            user_id[:8],
            exc,
        )
        return None


async def get_reward_profile(user_id: str) -> dict[str, Any]:
    async with AsyncSessionFactory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        contribution_count,
                        total_contribution_distance_km,
                        reward_points_balance,
                        reward_points_lifetime,
                        reward_points_redeemed,
                        last_contribution_at
                    FROM "EgRailway".profiles
                    WHERE id = CAST(:user_id AS uuid)
                    """
                ),
                {"user_id": user_id},
            )
        ).first()

    if row is None:
        return {
            "contribution_count": 0,
            "total_contribution_distance_km": 0.0,
            "reward_points_balance": 0,
            "reward_points_lifetime": 0,
            "reward_points_redeemed": 0,
            "last_contribution_at": None,
            "reward_catalog": REWARD_CATALOG,
        }

    return {
        "contribution_count": int(row.contribution_count or 0),
        "total_contribution_distance_km": float(row.total_contribution_distance_km or 0),
        "reward_points_balance": int(row.reward_points_balance or 0),
        "reward_points_lifetime": int(row.reward_points_lifetime or 0),
        "reward_points_redeemed": int(row.reward_points_redeemed or 0),
        "last_contribution_at": (
            row.last_contribution_at.isoformat()
            if row.last_contribution_at
            else None
        ),
        "reward_catalog": REWARD_CATALOG,
    }


async def get_pending_reward_summaries(user_id: str, limit: int = 3) -> list[dict[str, Any]]:
    async with AsyncSessionFactory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text AS id,
                        train_number,
                        trusted_distance_m,
                        points_awarded,
                        started_at,
                        ended_at
                    FROM "EgRailway".contribution_sessions
                    WHERE user_id = CAST(:user_id AS uuid)
                      AND status = 'completed'
                      AND points_awarded > 0
                      AND reward_seen_at IS NULL
                    ORDER BY ended_at DESC
                    LIMIT :limit
                    """
                ),
                {"user_id": user_id, "limit": max(1, min(limit, 10))},
            )
        ).all()
    return [_summary_from_row(row) for row in rows]


async def mark_reward_seen(user_id: str, contribution_id: str) -> bool:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            text(
                """
                UPDATE "EgRailway".contribution_sessions
                SET reward_seen_at = COALESCE(reward_seen_at, now())
                WHERE id = CAST(:contribution_id AS uuid)
                  AND user_id = CAST(:user_id AS uuid)
                  AND points_awarded > 0
                """
            ),
            {"user_id": user_id, "contribution_id": contribution_id},
        )
        await session.commit()
    return bool(result.rowcount and result.rowcount > 0)
