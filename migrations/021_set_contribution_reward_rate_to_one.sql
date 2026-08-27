-- ============================================================
-- Migration: Contribution reward rate
-- Purpose:
--   Change contribution rewards to 1 point per trusted kilometre.
-- ============================================================

ALTER TABLE "EgRailway".contribution_sessions
    ALTER COLUMN points_rate_per_km SET DEFAULT 1;
