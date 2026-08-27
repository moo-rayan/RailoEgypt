-- ============================================================
-- Migration: Reward redemption requests
-- Purpose:
--   Let users request redeeming contribution reward points while
--   keeping all point movements enforced server-side.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE "EgRailway".profiles
    ADD COLUMN IF NOT EXISTS reward_points_reserved INTEGER NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_profiles_reward_points_reserved_nonnegative'
    ) THEN
        ALTER TABLE "EgRailway".profiles
            ADD CONSTRAINT chk_profiles_reward_points_reserved_nonnegative
            CHECK (reward_points_reserved >= 0) NOT VALID;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS "EgRailway".reward_redemption_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES "EgRailway".profiles(id) ON DELETE CASCADE,
    reward_key          VARCHAR(80) NOT NULL,
    reward_title_ar     TEXT NOT NULL,
    reward_title_en     TEXT NOT NULL,
    points_required     INTEGER NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    user_note           TEXT NOT NULL DEFAULT '',
    admin_note          TEXT NOT NULL DEFAULT '',
    reviewed_by         UUID REFERENCES "EgRailway".profiles(id) ON DELETE SET NULL,
    reviewed_at         TIMESTAMPTZ,
    fulfilled_at        TIMESTAMPTZ,
    request_metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_reward_redemption_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'fulfilled', 'cancelled')),
    CONSTRAINT chk_reward_redemption_points_positive
        CHECK (points_required > 0)
);

CREATE INDEX IF NOT EXISTS idx_reward_redemption_user_created
    ON "EgRailway".reward_redemption_requests(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reward_redemption_status_created
    ON "EgRailway".reward_redemption_requests(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reward_redemption_reward_key
    ON "EgRailway".reward_redemption_requests(reward_key);

CREATE OR REPLACE FUNCTION "EgRailway".update_reward_redemption_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reward_redemption_updated_at
    ON "EgRailway".reward_redemption_requests;

CREATE TRIGGER trg_reward_redemption_updated_at
    BEFORE UPDATE ON "EgRailway".reward_redemption_requests
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".update_reward_redemption_timestamp();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON TABLE "EgRailway".reward_redemption_requests FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON TABLE "EgRailway".reward_redemption_requests FROM authenticated';
    END IF;
END $$;

ALTER TABLE "EgRailway".reward_redemption_requests ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view own reward redemptions"
    ON "EgRailway".reward_redemption_requests;
CREATE POLICY "Users can view own reward redemptions"
    ON "EgRailway".reward_redemption_requests
    FOR SELECT
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service role full access reward redemptions"
    ON "EgRailway".reward_redemption_requests;
CREATE POLICY "Service role full access reward redemptions"
    ON "EgRailway".reward_redemption_requests
    FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
