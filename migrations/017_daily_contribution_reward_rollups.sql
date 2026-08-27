-- ============================================================
-- Migration: Daily contribution reward rollups
-- Purpose:
--   Keep one reward row per user + train + Cairo day, and merge
--   any duplicate rows created by the previous per-session logic.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE "EgRailway".contribution_sessions
    ADD COLUMN IF NOT EXISTS contribution_date DATE,
    ADD COLUMN IF NOT EXISTS session_runs_count INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS source_session_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
    ADD COLUMN IF NOT EXISTS credited_distance_m NUMERIC(12, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unseen_distance_m NUMERIC(12, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unseen_points_awarded INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_session_id UUID,
    ADD COLUMN IF NOT EXISTS last_reward_at TIMESTAMPTZ;

UPDATE "EgRailway".contribution_sessions
SET
    contribution_date = (started_at AT TIME ZONE 'Africa/Cairo')::date,
    session_runs_count = GREATEST(session_runs_count, 1),
    source_session_ids = CASE
        WHEN cardinality(source_session_ids) > 0 THEN source_session_ids
        ELSE ARRAY[id]
    END,
    last_session_id = COALESCE(last_session_id, id),
    last_reward_at = COALESCE(last_reward_at, ended_at),
    unseen_distance_m = CASE
        WHEN reward_seen_at IS NULL AND points_awarded > 0 AND unseen_distance_m = 0
            THEN trusted_distance_m
        ELSE unseen_distance_m
    END,
    credited_distance_m = CASE
        WHEN points_awarded > 0 AND credited_distance_m = 0
            THEN trusted_distance_m
        ELSE credited_distance_m
    END,
    unseen_points_awarded = CASE
        WHEN reward_seen_at IS NULL AND points_awarded > 0 AND unseen_points_awarded = 0
            THEN points_awarded
        ELSE unseen_points_awarded
    END
WHERE contribution_date IS NULL
   OR cardinality(source_session_ids) = 0
   OR last_session_id IS NULL
   OR last_reward_at IS NULL;

ALTER TABLE "EgRailway".contribution_sessions
    ALTER COLUMN contribution_date SET NOT NULL;

WITH grouped AS (
    SELECT
        user_id,
        train_number,
        contribution_date,
        MIN(id::text)::uuid AS keep_id
    FROM "EgRailway".contribution_sessions
    GROUP BY user_id, train_number, contribution_date
    HAVING COUNT(*) > 1
),
rollup AS (
    SELECT
        g.keep_id,
        g.user_id,
        g.train_number,
        g.contribution_date,
        COALESCE(
            (ARRAY_AGG(cs.trip_id ORDER BY cs.ended_at DESC, cs.created_at DESC)
                FILTER (WHERE cs.trip_id IS NOT NULL))[1],
            NULL
        ) AS trip_id,
        COALESCE(
            (ARRAY_AGG(NULLIF(cs.from_station_name, '') ORDER BY cs.started_at, cs.created_at)
                FILTER (WHERE NULLIF(cs.from_station_name, '') IS NOT NULL))[1],
            ''
        ) AS from_station_name,
        COALESCE(
            (ARRAY_AGG(NULLIF(cs.to_station_name, '') ORDER BY cs.ended_at DESC, cs.created_at DESC)
                FILTER (WHERE NULLIF(cs.to_station_name, '') IS NOT NULL))[1],
            ''
        ) AS to_station_name,
        MIN(cs.started_at) AS started_at,
        MAX(cs.ended_at) AS ended_at,
        COALESCE(
            (ARRAY_AGG(NULLIF(cs.end_reason, '') ORDER BY cs.ended_at DESC, cs.created_at DESC)
                FILTER (WHERE NULLIF(cs.end_reason, '') IS NOT NULL))[1],
            ''
        ) AS end_reason,
        BOOL_OR(cs.is_silent) AS is_silent,
        SUM(GREATEST(cs.session_runs_count, 1))::integer AS session_runs_count,
        SUM(cs.accepted_updates_count)::integer AS accepted_updates_count,
        SUM(cs.rejected_updates_count)::integer AS rejected_updates_count,
        SUM(cs.raw_distance_m) AS raw_distance_m,
        SUM(cs.trusted_distance_m) AS trusted_distance_m,
        CASE
            WHEN SUM(cs.trusted_distance_m) > 0
                THEN SUM(CASE WHEN cs.points_awarded > 0 THEN cs.trusted_distance_m ELSE cs.credited_distance_m END)
            ELSE 0
        END AS credited_distance_m,
        CASE
            WHEN SUM(cs.accepted_updates_count) >= 2 AND SUM(cs.trusted_distance_m) >= 250
                THEN FLOOR((SUM(cs.trusted_distance_m) / 1000.0) * 1.0)::integer
            ELSE 0
        END AS points_awarded,
        SUM(cs.unseen_distance_m) AS unseen_distance_m,
        SUM(cs.unseen_points_awarded)::integer AS unseen_points_awarded,
        COALESCE(
            (ARRAY_AGG(cs.first_lat ORDER BY cs.started_at, cs.created_at)
                FILTER (WHERE cs.first_lat IS NOT NULL))[1],
            NULL
        ) AS first_lat,
        COALESCE(
            (ARRAY_AGG(cs.first_lng ORDER BY cs.started_at, cs.created_at)
                FILTER (WHERE cs.first_lng IS NOT NULL))[1],
            NULL
        ) AS first_lng,
        COALESCE(
            (ARRAY_AGG(cs.last_lat ORDER BY cs.ended_at DESC, cs.created_at DESC)
                FILTER (WHERE cs.last_lat IS NOT NULL))[1],
            NULL
        ) AS last_lat,
        COALESCE(
            (ARRAY_AGG(cs.last_lng ORDER BY cs.ended_at DESC, cs.created_at DESC)
                FILTER (WHERE cs.last_lng IS NOT NULL))[1],
            NULL
        ) AS last_lng,
        MAX(cs.max_reported_speed_kmh) AS max_reported_speed_kmh,
        MAX(cs.max_rail_distance_m) AS max_rail_distance_m,
        MAX(cs.max_train_distance_m) AS max_train_distance_m,
        (ARRAY_AGG(COALESCE(cs.last_session_id, cs.id) ORDER BY cs.ended_at DESC, cs.created_at DESC))[1]
            AS last_session_id,
        MAX(cs.last_reward_at) AS last_reward_at,
        CASE
            WHEN SUM(cs.unseen_points_awarded) > 0 THEN NULL
            ELSE MAX(cs.reward_seen_at)
        END AS reward_seen_at,
        ARRAY(
            SELECT DISTINCT sid
            FROM "EgRailway".contribution_sessions source
            CROSS JOIN LATERAL UNNEST(
                CASE
                    WHEN cardinality(source.source_session_ids) > 0
                        THEN source.source_session_ids
                    ELSE ARRAY[source.id]
                END
            ) AS sid
            WHERE source.user_id = g.user_id
              AND source.train_number = g.train_number
              AND source.contribution_date = g.contribution_date
        ) AS source_session_ids
    FROM grouped g
    JOIN "EgRailway".contribution_sessions cs
      ON cs.user_id = g.user_id
     AND cs.train_number = g.train_number
     AND cs.contribution_date = g.contribution_date
    GROUP BY g.keep_id, g.user_id, g.train_number, g.contribution_date
)
UPDATE "EgRailway".contribution_sessions target
SET
    trip_id = rollup.trip_id,
    from_station_name = rollup.from_station_name,
    to_station_name = rollup.to_station_name,
    started_at = rollup.started_at,
    ended_at = rollup.ended_at,
    end_reason = rollup.end_reason,
    status = CASE WHEN rollup.points_awarded > 0 THEN 'completed' ELSE 'discarded' END,
    is_silent = rollup.is_silent,
    session_runs_count = rollup.session_runs_count,
    accepted_updates_count = rollup.accepted_updates_count,
    rejected_updates_count = rollup.rejected_updates_count,
    raw_distance_m = rollup.raw_distance_m,
    trusted_distance_m = rollup.trusted_distance_m,
    credited_distance_m = CASE
        WHEN rollup.points_awarded > 0 THEN rollup.trusted_distance_m
        ELSE rollup.credited_distance_m
    END,
    points_awarded = rollup.points_awarded,
    unseen_distance_m = rollup.unseen_distance_m,
    unseen_points_awarded = LEAST(rollup.unseen_points_awarded, rollup.points_awarded),
    first_lat = rollup.first_lat,
    first_lng = rollup.first_lng,
    last_lat = rollup.last_lat,
    last_lng = rollup.last_lng,
    max_reported_speed_kmh = rollup.max_reported_speed_kmh,
    max_rail_distance_m = rollup.max_rail_distance_m,
    max_train_distance_m = rollup.max_train_distance_m,
    last_session_id = rollup.last_session_id,
    last_reward_at = rollup.last_reward_at,
    reward_seen_at = rollup.reward_seen_at,
    source_session_ids = rollup.source_session_ids
FROM rollup
WHERE target.id = rollup.keep_id;

WITH grouped AS (
    SELECT
        user_id,
        train_number,
        contribution_date,
        MIN(id::text)::uuid AS keep_id
    FROM "EgRailway".contribution_sessions
    GROUP BY user_id, train_number, contribution_date
    HAVING COUNT(*) > 1
)
DELETE FROM "EgRailway".contribution_sessions duplicate
USING grouped
WHERE duplicate.user_id = grouped.user_id
  AND duplicate.train_number = grouped.train_number
  AND duplicate.contribution_date = grouped.contribution_date
  AND duplicate.id <> grouped.keep_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_contribution_sessions_user_train_date
    ON "EgRailway".contribution_sessions(user_id, train_number, contribution_date);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_contribution_sessions_unseen_nonnegative'
    ) THEN
        ALTER TABLE "EgRailway".contribution_sessions
            ADD CONSTRAINT chk_contribution_sessions_unseen_nonnegative
            CHECK (
                credited_distance_m >= 0
                AND unseen_distance_m >= 0
                AND unseen_points_awarded >= 0
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_contribution_sessions_runs_nonnegative'
    ) THEN
        ALTER TABLE "EgRailway".contribution_sessions
            ADD CONSTRAINT chk_contribution_sessions_runs_nonnegative
            CHECK (session_runs_count >= 0) NOT VALID;
    END IF;
END $$;

WITH totals AS (
    SELECT
        user_id,
        COUNT(*) FILTER (WHERE points_awarded > 0)::integer AS contribution_count,
        COALESCE(
            ROUND((SUM(trusted_distance_m) FILTER (WHERE points_awarded > 0)) / 1000.0, 2),
            0
        ) AS total_contribution_distance_km,
        COALESCE(SUM(points_awarded) FILTER (WHERE points_awarded > 0), 0)::integer
            AS reward_points_lifetime,
        MAX(ended_at) FILTER (WHERE points_awarded > 0) AS last_contribution_at
    FROM "EgRailway".contribution_sessions
    GROUP BY user_id
)
UPDATE "EgRailway".profiles profile
SET
    is_contributor = totals.contribution_count > 0,
    contribution_count = totals.contribution_count,
    total_contribution_distance_km = totals.total_contribution_distance_km,
    reward_points_lifetime = totals.reward_points_lifetime,
    reward_points_balance = GREATEST(
        0,
        totals.reward_points_lifetime - profile.reward_points_redeemed
    ),
    last_contribution_at = totals.last_contribution_at,
    updated_at = now()
FROM totals
WHERE profile.id = totals.user_id;
