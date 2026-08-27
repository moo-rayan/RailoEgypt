-- ============================================================
-- Migration: Reward redemption target phone
-- Purpose:
--   Store the phone number that should receive the top-up reward.
-- ============================================================

ALTER TABLE "EgRailway".reward_redemption_requests
    ADD COLUMN IF NOT EXISTS target_phone VARCHAR(32) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_reward_redemption_target_phone
    ON "EgRailway".reward_redemption_requests(target_phone);
