import logging
import math
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)

POINTS_PER_KM = 2.0
MIN_REWARDED_DISTANCE_M = 250.0
CAIRO_TZ = ZoneInfo("Africa/Cairo")

REWARD_CATALOG: list[dict[str, Any]] = [
    {"title_ar": "فكة 5 جنيه", "title_en": "EGP 5 top-up", "points_required": 550},
    {"title_ar": "شحن 10 جنيه", "title_en": "EGP 10 top-up", "points_required": 1100},
    {"title_ar": "شحن 15 جنيه", "title_en": "EGP 15 top-up", "points_required": 1650},
    {"title_ar": "شحن 25 جنيه", "title_en": "EGP 25 top-up", "points_required": 2750},
    {"title_ar": "شحن 50 جنيه", "title_en": "EGP 50 top-up", "points_required": 5500},
]


class ContributionRewardPersistenceError(RuntimeError):
    """Raised when a reward contribution could not be persisted."""


def _utc_from_ts(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _contribution_date_from_ts(timestamp: float) -> date:
    return _utc_from_ts(timestamp).astimezone(CAIRO_TZ).date()


def _format_distance(distance_km: float) -> float:
    return round(distance_km, 1)


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _as_float(value: Any) -> float:
    return float(value or 0)


def _as_int(value: Any) -> int:
    return int(value or 0)


def _points_for_distance(trusted_distance_m: float, accepted_updates_count: int) -> int:
    if (
        accepted_updates_count < 2
        or trusted_distance_m < MIN_REWARDED_DISTANCE_M
    ):
        return 0
    return math.floor((trusted_distance_m / 1000.0) * POINTS_PER_KM)


def _source_session_ids(row: Mapping[str, Any]) -> set[str]:
    raw_ids = row.get("source_session_ids") or []
    return {str(item) for item in raw_ids if item}


def _is_likely_schema_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "undefinedcolumn",
            "undefined column",
            "does not exist",
            "undefinedtable",
            "undefined table",
            "column ",
            "relation ",
        )
    )


def _summary_from_row(row: Any) -> dict[str, Any]:
    unseen_distance_m = _value(row, "unseen_distance_m")
    unseen_points = _value(row, "unseen_points_awarded")
    distance_m = (
        _as_float(unseen_distance_m)
        if unseen_distance_m is not None and _as_float(unseen_distance_m) > 0
        else _as_float(_value(row, "trusted_distance_m"))
    )
    distance_km = distance_m / 1000.0
    points = (
        _as_int(unseen_points)
        if unseen_points is not None and _as_int(unseen_points) > 0
        else _as_int(_value(row, "points_awarded"))
    )
    train_number = str(_value(row, "train_number", "") or "")
    started_at = _value(row, "started_at")
    ended_at = _value(row, "ended_at")
    return {
        "id": str(_value(row, "id", "")),
        "train_number": train_number,
        "distance_km": _format_distance(distance_km),
        "points_awarded": points,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
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
    Persist one finished contribution run into a daily rollup.

    Security boundary:
      - Called only from server-side tracking state.
      - Distance and points are calculated from accepted GPS updates.
      - One DB row is kept per user + train + Cairo day.
    """
    trusted_distance_m = max(0.0, float(trusted_distance_m or 0.0))
    raw_distance_m = max(0.0, float(raw_distance_m or 0.0))
    accepted_updates_count = max(0, int(accepted_updates_count or 0))
    rejected_updates_count = max(0, int(rejected_updates_count or 0))
    started_at = _utc_from_ts(started_at_ts)
    ended_at = _utc_from_ts(ended_at_ts)
    contribution_date = _contribution_date_from_ts(started_at_ts)

    try:
        async with AsyncSessionFactory() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {
                    "lock_key": (
                        f"contribution_reward:"
                        f"{user_id}:{train_number}:{contribution_date.isoformat()}"
                    )
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO "EgRailway".profiles (id)
                    VALUES (CAST(:user_id AS uuid))
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {"user_id": user_id},
            )

            existing = (
                await session.execute(
                    text(
                        """
                        SELECT
                            id::text AS id,
                            train_number,
                            trip_id,
                            from_station_name,
                            to_station_name,
                            contribution_date,
                            started_at,
                            ended_at,
                            end_reason,
                            status,
                            is_silent,
                            session_runs_count,
                            source_session_ids,
                            accepted_updates_count,
                            rejected_updates_count,
                            raw_distance_m,
                            trusted_distance_m,
                            credited_distance_m,
                            points_awarded,
                            unseen_distance_m,
                            unseen_points_awarded,
                            first_lat,
                            first_lng,
                            last_lat,
                            last_lng,
                            max_reported_speed_kmh,
                            max_rail_distance_m,
                            max_train_distance_m,
                            last_session_id,
                            last_reward_at,
                            reward_seen_at
                        FROM "EgRailway".contribution_sessions
                        WHERE user_id = CAST(:user_id AS uuid)
                          AND train_number = :train_number
                          AND contribution_date = :contribution_date
                        FOR UPDATE
                        """
                    ),
                    {
                        "user_id": user_id,
                        "train_number": train_number,
                        "contribution_date": contribution_date,
                    },
                )
            ).mappings().first()

            has_existing = existing is not None
            if has_existing and session_id in _source_session_ids(existing):
                await session.rollback()
                logger.info(
                    "Contribution run already finalized: train=%s user=%s session=%s",
                    train_number,
                    user_id[:8],
                    session_id,
                )
                return None

            previous_points = _as_int(existing["points_awarded"]) if has_existing else 0
            previous_credited_m = (
                _as_float(existing["credited_distance_m"]) if has_existing else 0.0
            )
            previous_trusted_m = (
                _as_float(existing["trusted_distance_m"]) if has_existing else 0.0
            )
            previous_accepted = (
                _as_int(existing["accepted_updates_count"]) if has_existing else 0
            )

            total_trusted_m = previous_trusted_m + trusted_distance_m
            previous_raw_m = _as_float(existing["raw_distance_m"]) if has_existing else 0.0
            total_raw_m = previous_raw_m + raw_distance_m
            total_accepted = previous_accepted + accepted_updates_count
            previous_rejected = (
                _as_int(existing["rejected_updates_count"]) if has_existing else 0
            )
            total_rejected = (
                previous_rejected + rejected_updates_count
            )
            total_points = _points_for_distance(total_trusted_m, total_accepted)
            points_delta = max(total_points - previous_points, 0)

            credited_distance_m = (
                total_trusted_m
                if total_points > 0
                else previous_credited_m
            )
            profile_distance_delta_m = max(
                credited_distance_m - previous_credited_m,
                0.0,
            )
            unseen_distance_delta_m = profile_distance_delta_m
            contribution_count_delta = (
                1 if previous_points <= 0 and total_points > 0 else 0
            )
            status = "completed" if total_points > 0 else "discarded"

            common_params = {
                "session_id": session_id,
                "user_id": user_id,
                "train_number": train_number,
                "trip_id": trip_id,
                "from_station_name": from_station_name or "",
                "to_station_name": to_station_name or "",
                "contribution_date": contribution_date,
                "started_at": started_at,
                "ended_at": ended_at,
                "end_reason": end_reason or "",
                "status": status,
                "is_silent": bool(is_silent),
                "accepted_updates_count": accepted_updates_count,
                "rejected_updates_count": rejected_updates_count,
                "raw_distance_m": round(raw_distance_m, 2),
                "trusted_distance_m": round(trusted_distance_m, 2),
                "credited_distance_m": round(credited_distance_m, 2),
                "points_rate_per_km": POINTS_PER_KM,
                "points_awarded": total_points,
                "unseen_distance_m": round(
                    (
                        _as_float(existing["unseen_distance_m"])
                        if has_existing
                        else 0.0
                    )
                    + unseen_distance_delta_m,
                    2,
                ),
                "unseen_points_awarded": (
                    (
                        _as_int(existing["unseen_points_awarded"])
                        if has_existing
                        else 0
                    )
                    + points_delta
                ),
                "first_lat": first_lat,
                "first_lng": first_lng,
                "last_lat": last_lat,
                "last_lng": last_lng,
                "max_reported_speed_kmh": round(max_reported_speed_kmh or 0.0, 2),
                "max_rail_distance_m": max_rail_distance_m,
                "max_train_distance_m": max_train_distance_m,
            }

            if has_existing:
                row = (
                    await session.execute(
                        text(
                            """
                            UPDATE "EgRailway".contribution_sessions
                            SET
                                trip_id = COALESCE(trip_id, :trip_id),
                                from_station_name = CASE
                                    WHEN from_station_name = '' THEN :from_station_name
                                    ELSE from_station_name
                                END,
                                to_station_name = CASE
                                    WHEN :to_station_name <> '' THEN :to_station_name
                                    ELSE to_station_name
                                END,
                                started_at = LEAST(started_at, :started_at),
                                ended_at = GREATEST(ended_at, :ended_at),
                                end_reason = :end_reason,
                                status = :status,
                                is_silent = is_silent AND :is_silent,
                                session_runs_count = session_runs_count + 1,
                                source_session_ids = array_append(
                                    COALESCE(source_session_ids, '{}'::uuid[]),
                                    CAST(:session_id AS uuid)
                                ),
                                accepted_updates_count = :total_accepted,
                                rejected_updates_count = :total_rejected,
                                raw_distance_m = :total_raw_m,
                                trusted_distance_m = :total_trusted_m,
                                credited_distance_m = :credited_distance_m,
                                points_rate_per_km = :points_rate_per_km,
                                points_awarded = :points_awarded,
                                unseen_distance_m = :unseen_distance_m,
                                unseen_points_awarded = :unseen_points_awarded,
                                first_lat = COALESCE(first_lat, :first_lat),
                                first_lng = COALESCE(first_lng, :first_lng),
                                last_lat = COALESCE(:last_lat, last_lat),
                                last_lng = COALESCE(:last_lng, last_lng),
                                max_reported_speed_kmh = GREATEST(
                                    max_reported_speed_kmh,
                                    :max_reported_speed_kmh
                                ),
                                max_rail_distance_m = GREATEST(
                                    max_rail_distance_m,
                                    :max_rail_distance_m
                                ),
                                max_train_distance_m = GREATEST(
                                    max_train_distance_m,
                                    :max_train_distance_m
                                ),
                                last_session_id = CAST(:session_id AS uuid),
                                last_reward_at = CASE
                                    WHEN :points_delta > 0 THEN :ended_at
                                    ELSE last_reward_at
                                END,
                                reward_seen_at = CASE
                                    WHEN :points_delta > 0 THEN NULL
                                    ELSE reward_seen_at
                                END
                            WHERE id = CAST(:id AS uuid)
                            RETURNING
                                id::text AS id,
                                train_number,
                                trusted_distance_m,
                                points_awarded,
                                unseen_distance_m,
                                unseen_points_awarded,
                                started_at,
                                ended_at
                            """
                        ),
                        {
                            **common_params,
                            "id": existing["id"],
                            "total_accepted": total_accepted,
                            "total_rejected": total_rejected,
                            "total_raw_m": round(total_raw_m, 2),
                            "total_trusted_m": round(total_trusted_m, 2),
                            "points_delta": points_delta,
                        },
                    )
                ).mappings().first()
            else:
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
                                contribution_date,
                                started_at,
                                ended_at,
                                end_reason,
                                status,
                                is_silent,
                                session_runs_count,
                                source_session_ids,
                                accepted_updates_count,
                                rejected_updates_count,
                                raw_distance_m,
                                trusted_distance_m,
                                credited_distance_m,
                                points_rate_per_km,
                                points_awarded,
                                unseen_distance_m,
                                unseen_points_awarded,
                                first_lat,
                                first_lng,
                                last_lat,
                                last_lng,
                                max_reported_speed_kmh,
                                max_rail_distance_m,
                                max_train_distance_m,
                                last_session_id,
                                last_reward_at
                            )
                            VALUES (
                                CAST(:session_id AS uuid),
                                CAST(:user_id AS uuid),
                                :train_number,
                                :trip_id,
                                :from_station_name,
                                :to_station_name,
                                :contribution_date,
                                :started_at,
                                :ended_at,
                                :end_reason,
                                :status,
                                :is_silent,
                                1,
                                ARRAY[CAST(:session_id AS uuid)],
                                :accepted_updates_count,
                                :rejected_updates_count,
                                :raw_distance_m,
                                :trusted_distance_m,
                                :credited_distance_m,
                                :points_rate_per_km,
                                :points_awarded,
                                :unseen_distance_m,
                                :unseen_points_awarded,
                                :first_lat,
                                :first_lng,
                                :last_lat,
                                :last_lng,
                                :max_reported_speed_kmh,
                                :max_rail_distance_m,
                                :max_train_distance_m,
                                CAST(:session_id AS uuid),
                                CASE WHEN :points_delta > 0 THEN :ended_at ELSE NULL END
                            )
                            RETURNING
                                id::text AS id,
                                train_number,
                                trusted_distance_m,
                                points_awarded,
                                unseen_distance_m,
                                unseen_points_awarded,
                                started_at,
                                ended_at
                            """
                        ),
                        {**common_params, "points_delta": points_delta},
                    )
                ).mappings().first()

            if points_delta > 0 or profile_distance_delta_m > 0:
                await session.execute(
                    text(
                        """
                        UPDATE "EgRailway".profiles
                        SET
                            is_contributor = TRUE,
                            contribution_count =
                                contribution_count + :contribution_count_delta,
                            total_contribution_distance_km =
                                total_contribution_distance_km
                                + :profile_distance_delta_km,
                            reward_points_balance =
                                reward_points_balance + :points_delta,
                            reward_points_lifetime =
                                reward_points_lifetime + :points_delta,
                            last_contribution_at = GREATEST(
                                COALESCE(last_contribution_at, :ended_at),
                                :ended_at
                            ),
                            updated_at = now()
                        WHERE id = CAST(:user_id AS uuid)
                        """
                    ),
                    {
                        "user_id": user_id,
                        "contribution_count_delta": contribution_count_delta,
                        "profile_distance_delta_km": round(
                            profile_distance_delta_m / 1000.0,
                            2,
                        ),
                        "points_delta": points_delta,
                        "ended_at": ended_at,
                    },
                )

            await session.commit()

        unseen_points = _as_int(row["unseen_points_awarded"]) if row is not None else 0
        if not row or unseen_points <= 0:
            logger.info(
                "Contribution rollup updated without new points: "
                "train=%s user=%s trusted_delta=%.1fm accepted_delta=%d",
                train_number,
                user_id[:8],
                trusted_distance_m,
                accepted_updates_count,
            )
            return None

        if points_delta > 0:
            logger.info(
                "Reward awarded: train=%s user=%s date=%s total_distance=%.1fkm "
                "delta_points=%d",
                train_number,
                user_id[:8],
                contribution_date.isoformat(),
                _as_float(row["trusted_distance_m"]) / 1000.0,
                points_delta,
            )
        else:
            logger.info(
                "Pending reward summary returned without new delta: "
                "train=%s user=%s unseen_points=%d",
                train_number,
                user_id[:8],
                unseen_points,
            )
        return _summary_from_row(row)
    except Exception as exc:
        if _is_likely_schema_error(exc):
            logger.error(
                "Contribution rewards schema is not ready. "
                "Run backend migrations 016_create_contribution_rewards.sql "
                "and 017_daily_contribution_reward_rollups.sql, or the latest "
                "repair migration, on the production database."
            )
        logger.exception(
            "Failed to finalize contribution reward: train=%s user=%s error=%s",
            train_number,
            user_id[:8],
            exc,
        )
        raise ContributionRewardPersistenceError(
            "Failed to persist contribution reward"
        ) from exc


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


async def get_pending_reward_summaries(
    user_id: str,
    limit: int = 3,
    exclude_train_numbers: set[str] | None = None,
) -> list[dict[str, Any]]:
    exclude_train_numbers = {str(n) for n in (exclude_train_numbers or set())}
    fetch_limit = max(1, min(limit + len(exclude_train_numbers), 20))
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
                        unseen_distance_m,
                        unseen_points_awarded,
                        started_at,
                        ended_at
                    FROM "EgRailway".contribution_sessions
                    WHERE user_id = CAST(:user_id AS uuid)
                      AND status = 'completed'
                      AND unseen_points_awarded > 0
                      AND reward_seen_at IS NULL
                    ORDER BY ended_at DESC
                    LIMIT :limit
                    """
                ),
                {"user_id": user_id, "limit": fetch_limit},
            )
        ).all()
    summaries = [
        _summary_from_row(row)
        for row in rows
        if str(row.train_number or "") not in exclude_train_numbers
    ]
    return summaries[: max(1, min(limit, 10))]


async def mark_reward_seen(user_id: str, contribution_id: str) -> bool:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            text(
                """
                UPDATE "EgRailway".contribution_sessions
                SET
                    reward_seen_at = COALESCE(reward_seen_at, now()),
                    unseen_distance_m = 0,
                    unseen_points_awarded = 0
                WHERE id = CAST(:contribution_id AS uuid)
                  AND user_id = CAST(:user_id AS uuid)
                  AND unseen_points_awarded > 0
                """
            ),
            {"user_id": user_id, "contribution_id": contribution_id},
        )
        await session.commit()
    return bool(result.rowcount and result.rowcount > 0)
