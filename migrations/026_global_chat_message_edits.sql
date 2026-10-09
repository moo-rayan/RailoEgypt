BEGIN;

ALTER TABLE "EgRailway".global_chat_messages
    ADD COLUMN IF NOT EXISTS edited_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by_user BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN "EgRailway".global_chat_messages.deleted_by_user
    IS 'Owner-deleted message retained as an empty tombstone; admin removals stay hidden.';
COMMENT ON COLUMN "EgRailway".global_chat_messages.revision
    IS 'Server-controlled mutation version for ordering real-time and cached updates.';

-- Keep the existing service-role-only RLS policies. Clients must use the backend.
COMMIT;
