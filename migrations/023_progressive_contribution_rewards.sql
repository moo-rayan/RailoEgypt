-- Progressive contribution rewards.
--
-- Rewards are now credited while accepted GPS updates arrive, then the final
-- leave/cleanup step only closes the daily rollup. The active status prevents
-- pending reward popups from appearing before the contribution is closed.

ALTER TABLE "EgRailway".contribution_sessions
    DROP CONSTRAINT IF EXISTS chk_contribution_sessions_status;

ALTER TABLE "EgRailway".contribution_sessions
    ADD COLUMN IF NOT EXISTS session_progress JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE "EgRailway".contribution_sessions
SET session_progress = '{}'::jsonb
WHERE session_progress IS NULL;

ALTER TABLE "EgRailway".contribution_sessions
    ALTER COLUMN session_progress SET DEFAULT '{}'::jsonb,
    ALTER COLUMN session_progress SET NOT NULL;

-- Old rows are already final states. This keeps any accidental empty/null
-- status values readable before the new constraint touches them.
UPDATE "EgRailway".contribution_sessions
SET status = CASE
    WHEN points_awarded > 0 THEN 'completed'
    ELSE 'discarded'
END
WHERE status IS NULL OR status = '';

ALTER TABLE "EgRailway".contribution_sessions
    ADD CONSTRAINT chk_contribution_sessions_status
    CHECK (status IN ('active', 'completed', 'discarded'));

ALTER TABLE "EgRailway".contribution_sessions
    ALTER COLUMN status SET DEFAULT 'active';
