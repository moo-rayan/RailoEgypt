import logging
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


def _utc_from_ts(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _contribution_date_from_ts(timestamp: float) -> date:
    return _utc_from_ts(timestamp).astimezone(CAIRO_TZ).date()


def _format_distance(distance_km: float) -> float:
    return round(distance_km, 1)


def _summary_from_row(row: Any) -> dict[str, Any]:
    unseen_distance_m = getattr(row, "unseen_distance_m", None)
    unseen_points = getattr(row, "unseen_points_awarded", None)
    distance_m = (
        float(unseen_distance_m)
        if unseen_distance_m is not None and float(unseen_distance_m or 0) > 0
        else float(row.trusted_distance_m or 0)
    )
    distance_km = distance_m / 1000.0
    points = (
        int(unseen_points)
        if unseen_points is not None and int(unseen_points or 0) > 0
        else int(row.points_awarded or 0)
    )
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

    try:
        async with AsyncSessionFactory() as session:
            contribution_date = _contribution_date_from_ts(started_at_ts)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {
                    "lock_key": (
                        f"contribution_reward:"
                        f"{user_id}:{train_number}:{contribution_date.isoformat()}"
                    )
                },
            )

            row = (
                await session.execute(
                    text(
                        """
                        WITH incoming AS (
                            SELECT
                                CAST(:session_id AS uuid) AS session_id,
                                CAST(:user_id AS uuid) AS user_id,
                                :train_number AS train_number,
                                :trip_id AS trip_id,
                                :from_station_name AS from_station_name,
                                :to_station_name AS to_station_name,
                                :contribution_date AS contribution_date,
                                :started_at AS started_at,
                                :ended_at AS ended_at,
                                :end_reason AS end_reason,
                                :is_silent AS is_silent,
                                :accepted_updates_count AS accepted_updates_count,
                                :rejected_updates_count AS rejected_updates_count,
                                :raw_distance_m AS raw_distance_m,
                                :trusted_distance_m AS trusted_distance_m,
                                :points_rate_per_km AS points_rate_per_km,
                                :first_lat AS first_lat,
                                :first_lng AS first_lng,
                                :last_lat AS last_lat,
                                :last_lng AS last_lng,
                                :max_reported_speed_kmh AS max_reported_speed_kmh,
                                :max_rail_distance_m AS max_rail_distance_m,
                                :max_train_distance_m AS max_train_distance_m
                        ),
                        existing AS (
                            SELECT
                                points_awarded,
                                trusted_distance_m,
                                credited_distance_m,
                                source_session_ids
                            FROM "EgRailway".contribution_sessions
                            WHERE user_id = CAST(:user_id AS uuid)
                              AND train_number = :train_number
                              AND contribution_date = :contribution_date
                            FOR UPDATE
                        ),
                        prepared AS (
                            SELECT
                                incoming.*,
                                COALESCE(
                                    (SELECT points_awarded FROM existing),
                                    0
                                ) AS previous_points_awarded,
                                COALESCE(
                                    (SELECT trusted_distance_m FROM existing),
                                    0
                                ) AS previous_trusted_distance_m,
                                COALESCE(
                                    (SELECT credited_distance_m FROM existing),
                                    0
                                ) AS previous_credited_distance_m,
                                EXISTS (
                                    SELECT 1
                                    FROM existing
                                    WHERE incoming.session_id = ANY(source_session_ids)
                                ) AS duplicate_session
                            FROM incoming
                        ),
                        upserted AS (
                            INSERT INTO "EgRailway".contribution_sessions (
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
                            SELECT
                                user_id,
                                train_number,
                                trip_id,
                                from_station_name,
                                to_station_name,
                                contribution_date,
                                started_at,
                                ended_at,
                                end_reason,
                                CASE
                                    WHEN accepted_updates_count >= 2
                                     AND trusted_distance_m >= :min_rewarded_distance_m
                                     AND FLOOR((trusted_distance_m / 1000.0) * :points_per_km)::integer > 0
                                        THEN 'completed'
                                    ELSE 'discarded'
                                END,
                                is_silent,
                                1,
                                ARRAY[session_id],
                                accepted_updates_count,
                                rejected_updates_count,
                                raw_distance_m,
                                trusted_distance_m,
                                CASE
                                    WHEN accepted_updates_count >= 2
                                     AND trusted_distance_m >= :min_rewarded_distance_m
                                     AND FLOOR((trusted_distance_m / 1000.0) * :points_per_km)::integer > 0
                                        THEN trusted_distance_m
                                    ELSE 0
                                END,
                                points_rate_per_km,
                                CASE
                                    WHEN accepted_updates_count >= 2
                                     AND trusted_distance_m >= :min_rewarded_distance_m
                                        THEN FLOOR((trusted_distance_m / 1000.0) * :points_per_km)::integer
                                    ELSE 0
                                END,
                                CASE
                                    WHEN accepted_updates_count >= 2
                                     AND trusted_distance_m >= :min_rewarded_distance_m
                                     AND FLOOR((trusted_distance_m / 1000.0) * :points_per_km)::integer > 0
                                        THEN trusted_distance_m
                                    ELSE 0
                                END,
                                CASE
                                    WHEN accepted_updates_count >= 2
                                     AND trusted_distance_m >= :min_rewarded_distance_m
                                        THEN FLOOR((trusted_distance_m / 1000.0) * :points_per_km)::integer
                                    ELSE 0
                                END,
                                first_lat,
                                first_lng,
                                last_lat,
                                last_lng,
                                max_reported_speed_kmh,
                                max_rail_distance_m,
                                max_train_distance_m,
                                session_id,
                                CASE
                                    WHEN accepted_updates_count >= 2
                                     AND trusted_distance_m >= :min_rewarded_distance_m
                                     AND FLOOR((trusted_distance_m / 1000.0) * :points_per_km)::integer > 0
                                        THEN ended_at
                                    ELSE NULL
                                END
                            FROM prepared
                            ON CONFLICT (user_id, train_number, contribution_date)
                            DO UPDATE SET
                                trip_id = COALESCE(
                                    "EgRailway".contribution_sessions.trip_id,
                                    EXCLUDED.trip_id
                                ),
                                from_station_name = CASE
                                    WHEN "EgRailway".contribution_sessions.from_station_name = ''
                                        THEN EXCLUDED.from_station_name
                                    ELSE "EgRailway".contribution_sessions.from_station_name
                                END,
                                to_station_name = CASE
                                    WHEN EXCLUDED.to_station_name <> ''
                                        THEN EXCLUDED.to_station_name
                                    ELSE "EgRailway".contribution_sessions.to_station_name
                                END,
                                started_at = LEAST(
                                    "EgRailway".contribution_sessions.started_at,
                                    EXCLUDED.started_at
                                ),
                                ended_at = GREATEST(
                                    "EgRailway".contribution_sessions.ended_at,
                                    EXCLUDED.ended_at
                                ),
                                end_reason = EXCLUDED.end_reason,
                                is_silent = (
                                    "EgRailway".contribution_sessions.is_silent
                                    AND EXCLUDED.is_silent
                                ),
                                session_runs_count =
                                    "EgRailway".contribution_sessions.session_runs_count
                                    + CASE
                                        WHEN EXCLUDED.last_session_id = ANY(
                                            "EgRailway".contribution_sessions.source_session_ids
                                        ) THEN 0
                                        ELSE 1
                                    END,
                                accepted_updates_count =
                                    "EgRailway".contribution_sessions.accepted_updates_count
                                    + CASE
                                        WHEN EXCLUDED.last_session_id = ANY(
                                            "EgRailway".contribution_sessions.source_session_ids
                                        ) THEN 0
                                        ELSE EXCLUDED.accepted_updates_count
                                    END,
                                rejected_updates_count =
                                    "EgRailway".contribution_sessions.rejected_updates_count
                                    + CASE
                                        WHEN EXCLUDED.last_session_id = ANY(
                                            "EgRailway".contribution_sessions.source_session_ids
                                        ) THEN 0
                                        ELSE EXCLUDED.rejected_updates_count
                                    END,
                                raw_distance_m =
                                    "EgRailway".contribution_sessions.raw_distance_m
                                    + CASE
                                        WHEN EXCLUDED.last_session_id = ANY(
                                            "EgRailway".contribution_sessions.source_session_ids
                                        ) THEN 0
                                        ELSE EXCLUDED.raw_distance_m
                                    END,
                                trusted_distance_m =
                                    "EgRailway".contribution_sessions.trusted_distance_m
                                    + CASE
                                        WHEN EXCLUDED.last_session_id = ANY(
                                            "EgRailway".contribution_sessions.source_session_ids
                                        ) THEN 0
                                        ELSE EXCLUDED.trusted_distance_m
                                    END,
                                credited_distance_m = CASE
                                    WHEN (
                                        CASE
                                            WHEN (
                                                "EgRailway".contribution_sessions.accepted_updates_count
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.accepted_updates_count
                                                END
                                            ) >= 2
                                             AND (
                                                "EgRailway".contribution_sessions.trusted_distance_m
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.trusted_distance_m
                                                END
                                            ) >= :min_rewarded_distance_m
                                                THEN FLOOR((
                                                    "EgRailway".contribution_sessions.trusted_distance_m
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.trusted_distance_m
                                                    END
                                                ) / 1000.0 * :points_per_km)::integer
                                            ELSE 0
                                        END
                                    ) > "EgRailway".contribution_sessions.points_awarded
                                        THEN (
                                            "EgRailway".contribution_sessions.trusted_distance_m
                                            + CASE
                                                WHEN EXCLUDED.last_session_id = ANY(
                                                    "EgRailway".contribution_sessions.source_session_ids
                                                ) THEN 0
                                                ELSE EXCLUDED.trusted_distance_m
                                            END
                                        )
                                    ELSE "EgRailway".contribution_sessions.credited_distance_m
                                END,
                                points_rate_per_km = EXCLUDED.points_rate_per_km,
                                points_awarded = CASE
                                    WHEN (
                                        "EgRailway".contribution_sessions.accepted_updates_count
                                        + CASE
                                            WHEN EXCLUDED.last_session_id = ANY(
                                                "EgRailway".contribution_sessions.source_session_ids
                                            ) THEN 0
                                            ELSE EXCLUDED.accepted_updates_count
                                        END
                                    ) >= 2
                                     AND (
                                        "EgRailway".contribution_sessions.trusted_distance_m
                                        + CASE
                                            WHEN EXCLUDED.last_session_id = ANY(
                                                "EgRailway".contribution_sessions.source_session_ids
                                            ) THEN 0
                                            ELSE EXCLUDED.trusted_distance_m
                                        END
                                    ) >= :min_rewarded_distance_m
                                        THEN FLOOR((
                                            "EgRailway".contribution_sessions.trusted_distance_m
                                            + CASE
                                                WHEN EXCLUDED.last_session_id = ANY(
                                                    "EgRailway".contribution_sessions.source_session_ids
                                                ) THEN 0
                                                ELSE EXCLUDED.trusted_distance_m
                                            END
                                        ) / 1000.0 * :points_per_km)::integer
                                    ELSE 0
                                END,
                                unseen_points_awarded =
                                    "EgRailway".contribution_sessions.unseen_points_awarded
                                    + GREATEST(
                                        (
                                            CASE
                                                WHEN (
                                                    "EgRailway".contribution_sessions.accepted_updates_count
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.accepted_updates_count
                                                    END
                                                ) >= 2
                                                 AND (
                                                    "EgRailway".contribution_sessions.trusted_distance_m
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.trusted_distance_m
                                                    END
                                                ) >= :min_rewarded_distance_m
                                                    THEN FLOOR((
                                                        "EgRailway".contribution_sessions.trusted_distance_m
                                                        + CASE
                                                            WHEN EXCLUDED.last_session_id = ANY(
                                                                "EgRailway".contribution_sessions.source_session_ids
                                                            ) THEN 0
                                                            ELSE EXCLUDED.trusted_distance_m
                                                        END
                                                    ) / 1000.0 * :points_per_km)::integer
                                                ELSE 0
                                            END
                                        ) - "EgRailway".contribution_sessions.points_awarded,
                                        0
                                    ),
                                unseen_distance_m =
                                    "EgRailway".contribution_sessions.unseen_distance_m
                                    + CASE
                                        WHEN (
                                            CASE
                                                WHEN (
                                                    "EgRailway".contribution_sessions.accepted_updates_count
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.accepted_updates_count
                                                    END
                                                ) >= 2
                                                 AND (
                                                    "EgRailway".contribution_sessions.trusted_distance_m
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.trusted_distance_m
                                                    END
                                                ) >= :min_rewarded_distance_m
                                                    THEN FLOOR((
                                                        "EgRailway".contribution_sessions.trusted_distance_m
                                                        + CASE
                                                            WHEN EXCLUDED.last_session_id = ANY(
                                                                "EgRailway".contribution_sessions.source_session_ids
                                                            ) THEN 0
                                                            ELSE EXCLUDED.trusted_distance_m
                                                        END
                                                    ) / 1000.0 * :points_per_km)::integer
                                                ELSE 0
                                            END
                                        ) > "EgRailway".contribution_sessions.points_awarded
                                            THEN (
                                                "EgRailway".contribution_sessions.trusted_distance_m
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.trusted_distance_m
                                                END
                                            ) - "EgRailway".contribution_sessions.credited_distance_m
                                        ELSE 0
                                    END,
                                first_lat = COALESCE(
                                    "EgRailway".contribution_sessions.first_lat,
                                    EXCLUDED.first_lat
                                ),
                                first_lng = COALESCE(
                                    "EgRailway".contribution_sessions.first_lng,
                                    EXCLUDED.first_lng
                                ),
                                last_lat = CASE
                                    WHEN EXCLUDED.last_session_id = ANY(
                                        "EgRailway".contribution_sessions.source_session_ids
                                    ) THEN "EgRailway".contribution_sessions.last_lat
                                    ELSE COALESCE(EXCLUDED.last_lat, "EgRailway".contribution_sessions.last_lat)
                                END,
                                last_lng = CASE
                                    WHEN EXCLUDED.last_session_id = ANY(
                                        "EgRailway".contribution_sessions.source_session_ids
                                    ) THEN "EgRailway".contribution_sessions.last_lng
                                    ELSE COALESCE(EXCLUDED.last_lng, "EgRailway".contribution_sessions.last_lng)
                                END,
                                max_reported_speed_kmh = GREATEST(
                                    "EgRailway".contribution_sessions.max_reported_speed_kmh,
                                    EXCLUDED.max_reported_speed_kmh
                                ),
                                max_rail_distance_m = GREATEST(
                                    "EgRailway".contribution_sessions.max_rail_distance_m,
                                    EXCLUDED.max_rail_distance_m
                                ),
                                max_train_distance_m = GREATEST(
                                    "EgRailway".contribution_sessions.max_train_distance_m,
                                    EXCLUDED.max_train_distance_m
                                ),
                                last_session_id = CASE
                                    WHEN EXCLUDED.last_session_id = ANY(
                                        "EgRailway".contribution_sessions.source_session_ids
                                    ) THEN "EgRailway".contribution_sessions.last_session_id
                                    ELSE EXCLUDED.last_session_id
                                END,
                                last_reward_at = CASE
                                    WHEN (
                                        CASE
                                            WHEN (
                                                "EgRailway".contribution_sessions.accepted_updates_count
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.accepted_updates_count
                                                END
                                            ) >= 2
                                             AND (
                                                "EgRailway".contribution_sessions.trusted_distance_m
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.trusted_distance_m
                                                END
                                            ) >= :min_rewarded_distance_m
                                                THEN FLOOR((
                                                    "EgRailway".contribution_sessions.trusted_distance_m
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.trusted_distance_m
                                                    END
                                                ) / 1000.0 * :points_per_km)::integer
                                            ELSE 0
                                        END
                                    ) > "EgRailway".contribution_sessions.points_awarded
                                        THEN EXCLUDED.ended_at
                                    ELSE "EgRailway".contribution_sessions.last_reward_at
                                END,
                                reward_seen_at = CASE
                                    WHEN (
                                        CASE
                                            WHEN (
                                                "EgRailway".contribution_sessions.accepted_updates_count
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.accepted_updates_count
                                                END
                                            ) >= 2
                                             AND (
                                                "EgRailway".contribution_sessions.trusted_distance_m
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.trusted_distance_m
                                                END
                                            ) >= :min_rewarded_distance_m
                                                THEN FLOOR((
                                                    "EgRailway".contribution_sessions.trusted_distance_m
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.trusted_distance_m
                                                    END
                                                ) / 1000.0 * :points_per_km)::integer
                                            ELSE 0
                                        END
                                    ) > "EgRailway".contribution_sessions.points_awarded
                                        THEN NULL
                                    ELSE "EgRailway".contribution_sessions.reward_seen_at
                                END,
                                source_session_ids = CASE
                                    WHEN EXCLUDED.last_session_id = ANY(
                                        "EgRailway".contribution_sessions.source_session_ids
                                    ) THEN "EgRailway".contribution_sessions.source_session_ids
                                    ELSE array_append(
                                        "EgRailway".contribution_sessions.source_session_ids,
                                        EXCLUDED.last_session_id
                                    )
                                END,
                                status = CASE
                                    WHEN (
                                        CASE
                                            WHEN (
                                                "EgRailway".contribution_sessions.accepted_updates_count
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.accepted_updates_count
                                                END
                                            ) >= 2
                                             AND (
                                                "EgRailway".contribution_sessions.trusted_distance_m
                                                + CASE
                                                    WHEN EXCLUDED.last_session_id = ANY(
                                                        "EgRailway".contribution_sessions.source_session_ids
                                                    ) THEN 0
                                                    ELSE EXCLUDED.trusted_distance_m
                                                END
                                            ) >= :min_rewarded_distance_m
                                                THEN FLOOR((
                                                    "EgRailway".contribution_sessions.trusted_distance_m
                                                    + CASE
                                                        WHEN EXCLUDED.last_session_id = ANY(
                                                            "EgRailway".contribution_sessions.source_session_ids
                                                        ) THEN 0
                                                        ELSE EXCLUDED.trusted_distance_m
                                                    END
                                                ) / 1000.0 * :points_per_km)::integer
                                            ELSE 0
                                        END
                                    ) > 0
                                        THEN 'completed'
                                    ELSE 'discarded'
                                END
                            RETURNING
                                id::text AS id,
                                train_number,
                                trusted_distance_m,
                                credited_distance_m,
                                points_awarded,
                                unseen_distance_m,
                                unseen_points_awarded,
                                started_at,
                                ended_at
                        ),
                        metrics AS (
                            SELECT
                                upserted.*,
                                prepared.previous_points_awarded,
                                prepared.previous_trusted_distance_m,
                                prepared.previous_credited_distance_m,
                                prepared.duplicate_session,
                                GREATEST(
                                    upserted.points_awarded
                                    - prepared.previous_points_awarded,
                                    0
                                ) AS points_delta,
                                CASE
                                    WHEN upserted.points_awarded
                                         > prepared.previous_points_awarded
                                        THEN GREATEST(
                                            upserted.credited_distance_m
                                            - prepared.previous_credited_distance_m,
                                            0
                                        )
                                    ELSE 0
                                END AS profile_distance_delta_m,
                                CASE
                                    WHEN prepared.previous_points_awarded <= 0
                                     AND upserted.points_awarded > 0
                                        THEN 1
                                    ELSE 0
                                END AS contribution_count_delta
                            FROM upserted
                            CROSS JOIN prepared
                        ),
                        profile_update AS (
                            UPDATE "EgRailway".profiles
                            SET
                                is_contributor = TRUE,
                                contribution_count =
                                    contribution_count + metrics.contribution_count_delta,
                                total_contribution_distance_km =
                                    total_contribution_distance_km
                                    + ROUND(metrics.profile_distance_delta_m / 1000.0, 2),
                                reward_points_balance =
                                    reward_points_balance + metrics.points_delta,
                                reward_points_lifetime =
                                    reward_points_lifetime + metrics.points_delta,
                                last_contribution_at = GREATEST(
                                    COALESCE(last_contribution_at, metrics.ended_at),
                                    metrics.ended_at
                                ),
                                updated_at = now()
                            FROM metrics
                            WHERE "EgRailway".profiles.id = CAST(:user_id AS uuid)
                              AND metrics.points_delta > 0
                            RETURNING "EgRailway".profiles.id
                        )
                        SELECT
                            id,
                            train_number,
                            trusted_distance_m,
                            points_awarded,
                            unseen_distance_m,
                            unseen_points_awarded,
                            started_at,
                            ended_at,
                            points_delta,
                            duplicate_session
                        FROM metrics
                        """
                    ),
                    {
                        "user_id": user_id,
                        "session_id": session_id,
                        "train_number": train_number,
                        "trip_id": trip_id,
                        "from_station_name": from_station_name or "",
                        "to_station_name": to_station_name or "",
                        "contribution_date": contribution_date,
                        "started_at": _utc_from_ts(started_at_ts),
                        "ended_at": _utc_from_ts(ended_at_ts),
                        "end_reason": end_reason or "",
                        "is_silent": bool(is_silent),
                        "accepted_updates_count": accepted_updates_count,
                        "rejected_updates_count": rejected_updates_count,
                        "raw_distance_m": round(raw_distance_m, 2),
                        "trusted_distance_m": round(trusted_distance_m, 2),
                        "points_rate_per_km": POINTS_PER_KM,
                        "points_per_km": POINTS_PER_KM,
                        "min_rewarded_distance_m": MIN_REWARDED_DISTANCE_M,
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

            await session.commit()

        points_delta = int(getattr(row, "points_delta", 0) or 0) if row else 0
        if not row or points_delta <= 0:
            logger.info(
                "Contribution discarded: train=%s user=%s trusted=%.1fm accepted=%d",
                train_number,
                user_id[:8],
                trusted_distance_m,
                accepted_updates_count,
            )
            return None

        logger.info(
            "Reward awarded: train=%s user=%s total_distance=%.1fkm delta_points=%d",
            train_number,
            user_id[:8],
            float(row.trusted_distance_m or 0) / 1000.0,
            points_delta,
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
