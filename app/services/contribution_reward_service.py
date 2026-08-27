import json
import logging
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.database import AsyncSessionFactory

logger = logging.getLogger(__name__)

POINTS_PER_KM = 2.0
MIN_REWARDED_DISTANCE_M = 250.0
CAIRO_TZ = ZoneInfo("Africa/Cairo")

REWARD_CATALOG: list[dict[str, Any]] = [
    {
        "key": "topup_5",
        "title_ar": "فكة 5 جنيه",
        "title_en": "EGP 5 top-up",
        "points_required": 550,
    },
    {
        "key": "topup_10",
        "title_ar": "شحن 10 جنيه",
        "title_en": "EGP 10 top-up",
        "points_required": 1100,
    },
    {
        "key": "topup_15",
        "title_ar": "شحن 15 جنيه",
        "title_en": "EGP 15 top-up",
        "points_required": 1650,
    },
    {
        "key": "topup_25",
        "title_ar": "شحن 25 جنيه",
        "title_en": "EGP 25 top-up",
        "points_required": 2750,
    },
    {
        "key": "topup_50",
        "title_ar": "شحن 50 جنيه",
        "title_en": "EGP 50 top-up",
        "points_required": 5500,
    },
]


class ContributionRewardPersistenceError(RuntimeError):
    """Raised when a reward contribution could not be persisted."""


class RewardCatalogItemNotFound(ValueError):
    """Raised when a client requests an unknown reward catalog item."""


class InsufficientRewardPoints(ValueError):
    """Raised when a user does not have enough redeemable points."""


class InvalidRewardTargetPhone(ValueError):
    """Raised when a reward redemption phone number is invalid."""


class InvalidRewardRedemptionTransition(ValueError):
    """Raised when an admin requests an invalid redemption status change."""


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


def _safe_uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


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


def _catalog_item_by_key(reward_key: str) -> dict[str, Any]:
    key = (reward_key or "").strip()
    for item in REWARD_CATALOG:
        if item["key"] == key:
            return item
    raise RewardCatalogItemNotFound("Unknown reward item")


def _normalize_reward_phone(target_phone: str) -> str:
    translation = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    phone = (target_phone or "").translate(translation).strip()
    phone = re.sub(r"[^\d+]", "", phone)
    if phone.startswith("+20"):
        phone = "0" + phone[3:]
    elif phone.startswith("20") and len(phone) == 12:
        phone = "0" + phone[2:]

    if not re.fullmatch(r"01\d{9}", phone):
        raise InvalidRewardTargetPhone("Invalid Egyptian mobile phone number")
    return phone


def _redemption_from_row(row: Any) -> dict[str, Any]:
    created_at = _value(row, "created_at")
    updated_at = _value(row, "updated_at")
    reviewed_at = _value(row, "reviewed_at")
    fulfilled_at = _value(row, "fulfilled_at")
    return {
        "id": str(_value(row, "id", "")),
        "user_id": str(_value(row, "user_id", "")),
        "reward_key": str(_value(row, "reward_key", "")),
        "reward_title_ar": str(_value(row, "reward_title_ar", "")),
        "reward_title_en": str(_value(row, "reward_title_en", "")),
        "points_required": _as_int(_value(row, "points_required")),
        "target_phone": str(_value(row, "target_phone", "") or ""),
        "status": str(_value(row, "status", "")),
        "user_note": str(_value(row, "user_note", "") or ""),
        "admin_note": str(_value(row, "admin_note", "") or ""),
        "reviewed_by": (
            str(_value(row, "reviewed_by"))
            if _value(row, "reviewed_by") is not None
            else None
        ),
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
        "fulfilled_at": fulfilled_at.isoformat() if fulfilled_at else None,
        "user": {
            "email": _value(row, "email"),
            "display_name": _value(row, "display_name"),
            "avatar_url": _value(row, "avatar_url"),
            "reward_points_balance": _as_int(_value(row, "reward_points_balance")),
            "reward_points_reserved": _as_int(_value(row, "reward_points_reserved")),
            "reward_points_lifetime": _as_int(_value(row, "reward_points_lifetime")),
            "reward_points_redeemed": _as_int(_value(row, "reward_points_redeemed")),
            "contribution_count": _as_int(_value(row, "contribution_count")),
            "total_contribution_distance_km": _as_float(
                _value(row, "total_contribution_distance_km")
            ),
        },
    }


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
                            CAST(id AS text) AS id,
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
                "has_new_points": points_delta > 0,
                "last_reward_at": ended_at if points_delta > 0 else None,
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
                                started_at = LEAST(
                                    started_at,
                                    CAST(:started_at AS timestamptz)
                                ),
                                ended_at = GREATEST(
                                    ended_at,
                                    CAST(:ended_at AS timestamptz)
                                ),
                                end_reason = :end_reason,
                                status = :status,
                                is_silent = is_silent AND CAST(:is_silent AS boolean),
                                session_runs_count = session_runs_count + 1,
                                source_session_ids = array_append(
                                    COALESCE(
                                        source_session_ids,
                                        CAST(ARRAY[] AS uuid[])
                                    ),
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
                                last_reward_at = COALESCE(
                                    CAST(:last_reward_at AS timestamptz),
                                    last_reward_at
                                ),
                                reward_seen_at = CASE
                                    WHEN CAST(:has_new_points AS boolean) THEN NULL
                                    ELSE reward_seen_at
                                END
                            WHERE id = CAST(:id AS uuid)
                            RETURNING
                                CAST(id AS text) AS id,
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
                                CAST(:contribution_date AS date),
                                CAST(:started_at AS timestamptz),
                                CAST(:ended_at AS timestamptz),
                                :end_reason,
                                :status,
                                CAST(:is_silent AS boolean),
                                1,
                                CAST(ARRAY[CAST(:session_id AS uuid)] AS uuid[]),
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
                                CAST(:last_reward_at AS timestamptz)
                            )
                            RETURNING
                                CAST(id AS text) AS id,
                                train_number,
                                trusted_distance_m,
                                points_awarded,
                                unseen_distance_m,
                                unseen_points_awarded,
                                started_at,
                                ended_at
                            """
                        ),
                        common_params,
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
                                COALESCE(
                                    last_contribution_at,
                                    CAST(:ended_at AS timestamptz)
                                ),
                                CAST(:ended_at AS timestamptz)
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
                        reward_points_reserved,
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
            "reward_points_reserved": 0,
            "reward_points_lifetime": 0,
            "reward_points_redeemed": 0,
            "last_contribution_at": None,
            "reward_catalog": REWARD_CATALOG,
        }

    return {
        "contribution_count": int(row.contribution_count or 0),
        "total_contribution_distance_km": float(row.total_contribution_distance_km or 0),
        "reward_points_balance": int(row.reward_points_balance or 0),
        "reward_points_reserved": int(row.reward_points_reserved or 0),
        "reward_points_lifetime": int(row.reward_points_lifetime or 0),
        "reward_points_redeemed": int(row.reward_points_redeemed or 0),
        "last_contribution_at": (
            row.last_contribution_at.isoformat()
            if row.last_contribution_at
            else None
        ),
        "reward_catalog": REWARD_CATALOG,
    }


async def get_reward_leaderboard(
    *,
    user_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 20))

    async with AsyncSessionFactory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    WITH ranked AS (
                        SELECT
                            CAST(id AS text) AS id,
                            CASE
                                WHEN chat_anonymous = TRUE THEN
                                    COALESCE(NULLIF(chat_alias, ''), 'مساهم')
                                ELSE display_name
                            END AS display_name,
                            CASE
                                WHEN chat_anonymous = TRUE THEN ''
                                ELSE avatar_url
                            END AS avatar_url,
                            contribution_count,
                            total_contribution_distance_km,
                            reward_points_lifetime,
                            last_contribution_at,
                            ROW_NUMBER() OVER (
                                ORDER BY
                                    reward_points_lifetime DESC,
                                    total_contribution_distance_km DESC,
                                    contribution_count DESC,
                                    created_at ASC
                            ) AS rank
                        FROM "EgRailway".profiles
                        WHERE is_active = TRUE
                          AND (
                              contribution_count > 0
                              OR total_contribution_distance_km > 0
                              OR reward_points_lifetime > 0
                          )
                    )
                    SELECT *
                    FROM ranked
                    WHERE rank <= :limit
                       OR id = :user_id
                    ORDER BY rank ASC
                    """
                ),
                {
                    "limit": limit,
                    "user_id": user_id,
                },
            )
        ).mappings().all()

    items: list[dict[str, Any]] = []
    current_user: dict[str, Any] | None = None
    for row in rows:
        last_contribution_at = row["last_contribution_at"]
        item = {
            "rank": _as_int(row["rank"]),
            "user_id": str(row["id"]),
            "display_name": row["display_name"],
            "avatar_url": row["avatar_url"],
            "contribution_count": _as_int(row["contribution_count"]),
            "total_contribution_distance_km": _as_float(
                row["total_contribution_distance_km"]
            ),
            "reward_points_lifetime": _as_int(row["reward_points_lifetime"]),
            "last_contribution_at": (
                last_contribution_at.isoformat() if last_contribution_at else None
            ),
            "is_current_user": str(row["id"]) == str(user_id),
        }
        if item["rank"] <= limit:
            items.append(item)
        if item["is_current_user"]:
            current_user = item

    return {
        "items": items,
        "current_user": current_user,
        "limit": limit,
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
                        CAST(id AS text) AS id,
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


async def request_reward_redemption(
    *,
    user_id: str,
    reward_key: str,
    target_phone: str,
    user_note: str = "",
) -> dict[str, Any]:
    catalog_item = _catalog_item_by_key(reward_key)
    normalized_phone = _normalize_reward_phone(target_phone)
    points_required = int(catalog_item["points_required"])
    metadata = {
        "source": "mobile_app",
        "catalog_snapshot": catalog_item,
        "target_phone": normalized_phone,
    }

    async with AsyncSessionFactory() as session:
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
        profile = (
            await session.execute(
                text(
                    """
                    SELECT
                        reward_points_balance,
                        reward_points_reserved
                    FROM "EgRailway".profiles
                    WHERE id = CAST(:user_id AS uuid)
                    FOR UPDATE
                    """
                ),
                {"user_id": user_id},
            )
        ).mappings().first()

        if profile is None:
            raise RewardCatalogItemNotFound("Profile not found")

        current_balance = _as_int(profile["reward_points_balance"])
        if current_balance < points_required:
            raise InsufficientRewardPoints("Not enough reward points")

        update_result = await session.execute(
            text(
                """
                UPDATE "EgRailway".profiles
                SET
                    reward_points_balance =
                        reward_points_balance - :points_required,
                    reward_points_reserved =
                        reward_points_reserved + :points_required,
                    updated_at = now()
                WHERE id = CAST(:user_id AS uuid)
                  AND reward_points_balance >= :points_required
                """
            ),
            {
                "user_id": user_id,
                "points_required": points_required,
            },
        )
        if not update_result.rowcount:
            raise InsufficientRewardPoints("Not enough reward points")

        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO "EgRailway".reward_redemption_requests (
                        user_id,
                        reward_key,
                        reward_title_ar,
                        reward_title_en,
                        points_required,
                        target_phone,
                        status,
                        user_note,
                        request_metadata
                    )
                    VALUES (
                        CAST(:user_id AS uuid),
                        :reward_key,
                        :reward_title_ar,
                        :reward_title_en,
                        :points_required,
                        :target_phone,
                        'pending',
                        :user_note,
                        CAST(:request_metadata AS jsonb)
                    )
                    RETURNING
                        CAST(id AS text) AS id,
                        CAST(user_id AS text) AS user_id,
                        reward_key,
                        reward_title_ar,
                        reward_title_en,
                        points_required,
                        target_phone,
                        status,
                        user_note,
                        admin_note,
                        CAST(reviewed_by AS text) AS reviewed_by,
                        created_at,
                        updated_at,
                        reviewed_at,
                        fulfilled_at
                    """
                ),
                {
                    "user_id": user_id,
                    "reward_key": catalog_item["key"],
                    "reward_title_ar": catalog_item["title_ar"],
                    "reward_title_en": catalog_item["title_en"],
                    "points_required": points_required,
                    "target_phone": normalized_phone,
                    "user_note": (user_note or "").strip()[:500],
                    "request_metadata": json.dumps(
                        metadata,
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            )
        ).mappings().first()
        await session.commit()

    redemption = _redemption_from_row(row)
    redemption["remaining_balance"] = current_balance - points_required
    return redemption


async def list_reward_contributors(
    *,
    page: int = 1,
    limit: int = 30,
    search: str = "",
    sort_by: str = "points",
    sort_order: str = "desc",
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 30), 100))
    search = (search or "").strip()
    params: dict[str, Any] = {
        "limit": limit,
        "offset": (page - 1) * limit,
    }
    filters = [
        """
        (
            p.contribution_count > 0
            OR p.total_contribution_distance_km > 0
            OR p.reward_points_lifetime > 0
            OR p.reward_points_balance > 0
            OR p.reward_points_reserved > 0
            OR p.reward_points_redeemed > 0
        )
        """
    ]
    if search:
        filters.append(
            """
            (
                p.display_name ILIKE :q
                OR p.email ILIKE :q
                OR CAST(p.id AS text) ILIKE :q
            )
            """
        )
        params["q"] = f"%{search}%"

    sort_columns = {
        "points": "p.reward_points_lifetime",
        "balance": "p.reward_points_balance",
        "reserved": "p.reward_points_reserved",
        "redeemed": "p.reward_points_redeemed",
        "contributions": "p.contribution_count",
        "distance": "p.total_contribution_distance_km",
        "last": "p.last_contribution_at",
    }
    order_column = sort_columns.get(sort_by, sort_columns["points"])
    direction = "ASC" if sort_order == "asc" else "DESC"
    where_clause = "WHERE " + " AND ".join(filters)

    async with AsyncSessionFactory() as session:
        total = int(
            (
                await session.execute(
                    text(
                        f"""
                        SELECT COUNT(*) AS total
                        FROM "EgRailway".profiles p
                        {where_clause}
                        """
                    ),
                    params,
                )
            ).scalar_one()
            or 0
        )

        stats = (
            await session.execute(
                text(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE contribution_count > 0
                               OR reward_points_lifetime > 0
                               OR reward_points_balance > 0
                               OR reward_points_reserved > 0
                               OR reward_points_redeemed > 0
                        ) AS contributors_count,
                        COALESCE(SUM(contribution_count), 0) AS total_contributions,
                        COALESCE(SUM(total_contribution_distance_km), 0) AS total_distance_km,
                        COALESCE(SUM(reward_points_lifetime), 0) AS total_points,
                        COALESCE(SUM(reward_points_balance), 0) AS available_points,
                        COALESCE(SUM(reward_points_reserved), 0) AS reserved_points,
                        COALESCE(SUM(reward_points_redeemed), 0) AS redeemed_points
                    FROM "EgRailway".profiles
                    """
                )
            )
        ).mappings().first()

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        CAST(p.id AS text) AS id,
                        p.email,
                        p.display_name,
                        p.avatar_url,
                        p.contribution_count,
                        p.total_contribution_distance_km,
                        p.reward_points_balance,
                        p.reward_points_reserved,
                        p.reward_points_lifetime,
                        p.reward_points_redeemed,
                        p.reputation_score,
                        p.last_contribution_at,
                        p.created_at,
                        p.updated_at
                    FROM "EgRailway".profiles p
                    {where_clause}
                    ORDER BY {order_column} {direction} NULLS LAST,
                             p.contribution_count DESC,
                             p.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

    items = []
    for row in rows:
        last_contribution_at = row["last_contribution_at"]
        created_at = row["created_at"]
        updated_at = row["updated_at"]
        items.append(
            {
                "id": str(row["id"]),
                "email": row["email"],
                "display_name": row["display_name"],
                "avatar_url": row["avatar_url"],
                "contribution_count": _as_int(row["contribution_count"]),
                "total_contribution_distance_km": _as_float(
                    row["total_contribution_distance_km"]
                ),
                "reward_points_balance": _as_int(row["reward_points_balance"]),
                "reward_points_reserved": _as_int(row["reward_points_reserved"]),
                "reward_points_lifetime": _as_int(row["reward_points_lifetime"]),
                "reward_points_redeemed": _as_int(row["reward_points_redeemed"]),
                "reputation_score": _as_float(row["reputation_score"]),
                "last_contribution_at": (
                    last_contribution_at.isoformat()
                    if last_contribution_at
                    else None
                ),
                "created_at": created_at.isoformat() if created_at else None,
                "updated_at": updated_at.isoformat() if updated_at else None,
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit if total > 0 else 0,
        "stats": {
            "contributors_count": _as_int(_value(stats, "contributors_count")),
            "total_contributions": _as_int(_value(stats, "total_contributions")),
            "total_distance_km": _as_float(_value(stats, "total_distance_km")),
            "total_points": _as_int(_value(stats, "total_points")),
            "available_points": _as_int(_value(stats, "available_points")),
            "reserved_points": _as_int(_value(stats, "reserved_points")),
            "redeemed_points": _as_int(_value(stats, "redeemed_points")),
        },
    }


async def list_reward_redemptions(
    *,
    page: int = 1,
    limit: int = 30,
    status_filter: str = "all",
    search: str = "",
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 30), 100))
    search = (search or "").strip()
    status_filter = (status_filter or "all").strip()
    params: dict[str, Any] = {
        "limit": limit,
        "offset": (page - 1) * limit,
    }
    filters: list[str] = []
    if status_filter != "all":
        filters.append("r.status = :status")
        params["status"] = status_filter
    if search:
        filters.append(
            """
            (
                r.reward_title_ar ILIKE :q
                OR r.reward_title_en ILIKE :q
                OR r.reward_key ILIKE :q
                OR r.target_phone ILIKE :q
                OR CAST(r.user_id AS text) ILIKE :q
                OR p.email ILIKE :q
                OR p.display_name ILIKE :q
            )
            """
        )
        params["q"] = f"%{search}%"
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    async with AsyncSessionFactory() as session:
        total = int(
            (
                await session.execute(
                    text(
                        f"""
                        SELECT COUNT(*) AS total
                        FROM "EgRailway".reward_redemption_requests r
                        LEFT JOIN "EgRailway".profiles p ON p.id = r.user_id
                        {where_clause}
                        """
                    ),
                    params,
                )
            ).scalar_one()
            or 0
        )
        status_rows = (
            await session.execute(
                text(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM "EgRailway".reward_redemption_requests
                    GROUP BY status
                    """
                )
            )
        ).mappings().all()
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        CAST(r.id AS text) AS id,
                        CAST(r.user_id AS text) AS user_id,
                        r.reward_key,
                        r.reward_title_ar,
                        r.reward_title_en,
                        r.points_required,
                        r.target_phone,
                        r.status,
                        r.user_note,
                        r.admin_note,
                        CAST(r.reviewed_by AS text) AS reviewed_by,
                        r.reviewed_at,
                        r.fulfilled_at,
                        r.created_at,
                        r.updated_at,
                        p.email,
                        p.display_name,
                        p.avatar_url,
                        p.contribution_count,
                        p.total_contribution_distance_km,
                        p.reward_points_balance,
                        p.reward_points_reserved,
                        p.reward_points_lifetime,
                        p.reward_points_redeemed
                    FROM "EgRailway".reward_redemption_requests r
                    LEFT JOIN "EgRailway".profiles p ON p.id = r.user_id
                    {where_clause}
                    ORDER BY
                        CASE r.status
                            WHEN 'pending' THEN 0
                            WHEN 'approved' THEN 1
                            ELSE 2
                        END,
                        r.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

    status_counts = {row["status"]: _as_int(row["count"]) for row in status_rows}
    return {
        "items": [_redemption_from_row(row) for row in rows],
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit if total > 0 else 0,
        "status_counts": status_counts,
    }


async def update_reward_redemption_status(
    *,
    request_id: str,
    admin_user_id: str | None,
    status_value: str,
    admin_note: str = "",
) -> dict[str, Any]:
    next_status = (status_value or "").strip()
    if next_status not in {"approved", "rejected", "fulfilled", "cancelled"}:
        raise InvalidRewardRedemptionTransition("Unsupported redemption status")
    reviewed_by_uuid = _safe_uuid(admin_user_id)
    reviewed_by_sql = (
        "CAST(:admin_user_id AS uuid)" if reviewed_by_uuid is not None else "reviewed_by"
    )

    async with AsyncSessionFactory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        CAST(r.id AS text) AS id,
                        CAST(r.user_id AS text) AS user_id,
                        r.reward_key,
                        r.reward_title_ar,
                        r.reward_title_en,
                        r.points_required,
                        r.target_phone,
                        r.status,
                        r.user_note,
                        r.admin_note,
                        CAST(r.reviewed_by AS text) AS reviewed_by,
                        r.reviewed_at,
                        r.fulfilled_at,
                        r.created_at,
                        r.updated_at,
                        p.email,
                        p.display_name,
                        p.avatar_url,
                        p.contribution_count,
                        p.total_contribution_distance_km,
                        p.reward_points_balance,
                        p.reward_points_reserved,
                        p.reward_points_lifetime,
                        p.reward_points_redeemed
                    FROM "EgRailway".reward_redemption_requests r
                    JOIN "EgRailway".profiles p ON p.id = r.user_id
                    WHERE r.id = CAST(:request_id AS uuid)
                    FOR UPDATE OF r, p
                    """
                ),
                {"request_id": request_id},
            )
        ).mappings().first()

        if row is None:
            raise RewardCatalogItemNotFound("Redemption request not found")

        current_status = str(row["status"])
        if current_status in {"rejected", "fulfilled", "cancelled"}:
            if current_status != next_status:
                raise InvalidRewardRedemptionTransition(
                    "Final redemption status cannot be changed"
                )
        elif current_status == "approved" and next_status == "approved":
            pass
        elif current_status == "pending" and next_status == "approved":
            pass
        elif next_status in {"rejected", "cancelled"}:
            result = await session.execute(
                text(
                    """
                    UPDATE "EgRailway".profiles
                    SET
                        reward_points_reserved =
                            GREATEST(0, reward_points_reserved - :points_required),
                        reward_points_balance =
                            reward_points_balance + :points_required,
                        updated_at = now()
                    WHERE id = CAST(:user_id AS uuid)
                    """
                ),
                {
                    "user_id": row["user_id"],
                    "points_required": _as_int(row["points_required"]),
                },
            )
            if not result.rowcount:
                raise InvalidRewardRedemptionTransition(
                    "Could not refund reserved reward points"
                )
        elif next_status == "fulfilled":
            result = await session.execute(
                text(
                    """
                    UPDATE "EgRailway".profiles
                    SET
                        reward_points_reserved =
                            GREATEST(0, reward_points_reserved - :points_required),
                        reward_points_redeemed =
                            reward_points_redeemed + :points_required,
                        updated_at = now()
                    WHERE id = CAST(:user_id AS uuid)
                      AND reward_points_reserved >= :points_required
                    """
                ),
                {
                    "user_id": row["user_id"],
                    "points_required": _as_int(row["points_required"]),
                },
            )
            if not result.rowcount:
                raise InvalidRewardRedemptionTransition(
                    "User does not have enough reserved reward points"
                )
        else:
            raise InvalidRewardRedemptionTransition(
                f"Cannot change redemption from {current_status} to {next_status}"
            )

        updated = (
            await session.execute(
                text(
                    f"""
                    UPDATE "EgRailway".reward_redemption_requests r
                    SET
                        status = :next_status,
                        admin_note = :admin_note,
                        reviewed_by = {reviewed_by_sql},
                        reviewed_at = CASE
                            WHEN reviewed_at IS NULL THEN now()
                            ELSE reviewed_at
                        END,
                        fulfilled_at = CASE
                            WHEN :next_status = 'fulfilled' THEN now()
                            ELSE fulfilled_at
                        END
                    FROM "EgRailway".profiles p
                    WHERE r.id = CAST(:request_id AS uuid)
                      AND p.id = r.user_id
                    RETURNING
                        CAST(r.id AS text) AS id,
                        CAST(r.user_id AS text) AS user_id,
                        r.reward_key,
                        r.reward_title_ar,
                        r.reward_title_en,
                        r.points_required,
                        r.target_phone,
                        r.status,
                        r.user_note,
                        r.admin_note,
                        CAST(r.reviewed_by AS text) AS reviewed_by,
                        r.reviewed_at,
                        r.fulfilled_at,
                        r.created_at,
                        r.updated_at,
                        p.email,
                        p.display_name,
                        p.avatar_url,
                        p.contribution_count,
                        p.total_contribution_distance_km,
                        p.reward_points_balance,
                        p.reward_points_reserved,
                        p.reward_points_lifetime,
                        p.reward_points_redeemed
                    """
                ),
                {
                    "request_id": request_id,
                    "next_status": next_status,
                    "admin_note": (admin_note or "").strip()[:1000],
                    "admin_user_id": reviewed_by_uuid,
                },
            )
        ).mappings().first()
        await session.commit()

    return _redemption_from_row(updated)
