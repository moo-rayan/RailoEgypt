-- Prevent the same route segment from being rewarded twice for the same
-- user/train/day, even if the user stops and starts a new contribution session.

ALTER TABLE "EgRailway".contribution_sessions
    ADD COLUMN IF NOT EXISTS max_route_progress_m NUMERIC(12, 2) NOT NULL DEFAULT 0;

UPDATE "EgRailway".contribution_sessions
SET max_route_progress_m = GREATEST(
    COALESCE(max_route_progress_m, 0),
    COALESCE(trusted_distance_m, 0)
)
WHERE max_route_progress_m IS NULL
   OR max_route_progress_m < COALESCE(trusted_distance_m, 0);

ALTER TABLE "EgRailway".contribution_sessions
    ALTER COLUMN max_route_progress_m SET DEFAULT 0,
    ALTER COLUMN max_route_progress_m SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_contribution_sessions_route_progress_nonnegative'
          AND conrelid = '"EgRailway".contribution_sessions'::regclass
    ) THEN
        ALTER TABLE "EgRailway".contribution_sessions
            ADD CONSTRAINT chk_contribution_sessions_route_progress_nonnegative
            CHECK (max_route_progress_m >= 0);
    END IF;
END $$;
