-- ============================================================
-- Migration: Contribution rewards ledger
-- Purpose:
--   Persist trusted tracking contributions and server-awarded points.
--   Clients can only read their own data; all writes are server-side.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE "EgRailway".profiles
    ADD COLUMN IF NOT EXISTS total_contribution_distance_km NUMERIC(12, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reward_points_balance INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reward_points_lifetime INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reward_points_redeemed INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_profiles_total_contribution_distance_nonnegative'
    ) THEN
        ALTER TABLE "EgRailway".profiles
            ADD CONSTRAINT chk_profiles_total_contribution_distance_nonnegative
            CHECK (total_contribution_distance_km >= 0) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_profiles_reward_points_balance_nonnegative'
    ) THEN
        ALTER TABLE "EgRailway".profiles
            ADD CONSTRAINT chk_profiles_reward_points_balance_nonnegative
            CHECK (reward_points_balance >= 0) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_profiles_reward_points_lifetime_nonnegative'
    ) THEN
        ALTER TABLE "EgRailway".profiles
            ADD CONSTRAINT chk_profiles_reward_points_lifetime_nonnegative
            CHECK (reward_points_lifetime >= 0) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_profiles_reward_points_redeemed_nonnegative'
    ) THEN
        ALTER TABLE "EgRailway".profiles
            ADD CONSTRAINT chk_profiles_reward_points_redeemed_nonnegative
            CHECK (reward_points_redeemed >= 0) NOT VALID;
    END IF;
END $$;

-- Keep reward counters server-owned even if the profile row is user-editable.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE INSERT (
            is_contributor,
            contribution_count,
            reputation_score,
            last_contribution_at,
            total_contribution_distance_km,
            reward_points_balance,
            reward_points_lifetime,
            reward_points_redeemed
        ) ON TABLE "EgRailway".profiles FROM anon';

        EXECUTE 'REVOKE UPDATE (
            is_contributor,
            contribution_count,
            reputation_score,
            last_contribution_at,
            total_contribution_distance_km,
            reward_points_balance,
            reward_points_lifetime,
            reward_points_redeemed
        ) ON TABLE "EgRailway".profiles FROM anon';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE INSERT (
            is_contributor,
            contribution_count,
            reputation_score,
            last_contribution_at,
            total_contribution_distance_km,
            reward_points_balance,
            reward_points_lifetime,
            reward_points_redeemed
        ) ON TABLE "EgRailway".profiles FROM authenticated';

        EXECUTE 'REVOKE UPDATE (
            is_contributor,
            contribution_count,
            reputation_score,
            last_contribution_at,
            total_contribution_distance_km,
            reward_points_balance,
            reward_points_lifetime,
            reward_points_redeemed
        ) ON TABLE "EgRailway".profiles FROM authenticated';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS "EgRailway".contribution_sessions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES "EgRailway".profiles(id) ON DELETE CASCADE,
    train_number            VARCHAR(20) NOT NULL,
    trip_id                 INTEGER REFERENCES "EgRailway".trips(id) ON DELETE SET NULL,
    from_station_name       TEXT NOT NULL DEFAULT '',
    to_station_name         TEXT NOT NULL DEFAULT '',
    started_at              TIMESTAMPTZ NOT NULL,
    ended_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    end_reason              TEXT NOT NULL DEFAULT '',
    status                  TEXT NOT NULL DEFAULT 'completed',
    is_silent               BOOLEAN NOT NULL DEFAULT FALSE,

    accepted_updates_count  INTEGER NOT NULL DEFAULT 0,
    rejected_updates_count  INTEGER NOT NULL DEFAULT 0,
    raw_distance_m          NUMERIC(12, 2) NOT NULL DEFAULT 0,
    trusted_distance_m      NUMERIC(12, 2) NOT NULL DEFAULT 0,
    points_rate_per_km      NUMERIC(8, 2) NOT NULL DEFAULT 2,
    points_awarded          INTEGER NOT NULL DEFAULT 0,

    first_lat               DOUBLE PRECISION,
    first_lng               DOUBLE PRECISION,
    last_lat                DOUBLE PRECISION,
    last_lng                DOUBLE PRECISION,
    max_reported_speed_kmh  NUMERIC(8, 2) NOT NULL DEFAULT 0,
    max_rail_distance_m     NUMERIC(8, 2) NOT NULL DEFAULT 500,
    max_train_distance_m    NUMERIC(8, 2) NOT NULL DEFAULT 5000,

    reward_seen_at          TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_contribution_sessions_status
        CHECK (status IN ('completed', 'discarded')),
    CONSTRAINT chk_contribution_sessions_counts_nonnegative
        CHECK (accepted_updates_count >= 0 AND rejected_updates_count >= 0),
    CONSTRAINT chk_contribution_sessions_distances_nonnegative
        CHECK (raw_distance_m >= 0 AND trusted_distance_m >= 0),
    CONSTRAINT chk_contribution_sessions_points_nonnegative
        CHECK (points_awarded >= 0)
);

CREATE INDEX IF NOT EXISTS idx_contribution_sessions_user_created
    ON "EgRailway".contribution_sessions(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_contribution_sessions_train_created
    ON "EgRailway".contribution_sessions(train_number, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_contribution_sessions_pending_seen
    ON "EgRailway".contribution_sessions(user_id, reward_seen_at)
    WHERE points_awarded > 0 AND reward_seen_at IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON TABLE "EgRailway".contribution_sessions FROM anon';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON TABLE "EgRailway".contribution_sessions FROM authenticated';
    END IF;
END $$;

ALTER TABLE "EgRailway".contribution_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view own contribution sessions"
    ON "EgRailway".contribution_sessions;
CREATE POLICY "Users can view own contribution sessions"
    ON "EgRailway".contribution_sessions
    FOR SELECT
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service role full access contribution sessions"
    ON "EgRailway".contribution_sessions;
CREATE POLICY "Service role full access contribution sessions"
    ON "EgRailway".contribution_sessions
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
