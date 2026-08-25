-- Global chat: persistent messages, love reactions, and feature settings.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS "EgRailway".global_chat_messages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID REFERENCES "EgRailway".profiles(id) ON DELETE SET NULL,
    user_name           VARCHAR(50) NOT NULL DEFAULT 'مجهول',
    user_avatar         TEXT NOT NULL DEFAULT '',
    text                TEXT NOT NULL,
    message_type        VARCHAR(20) NOT NULL DEFAULT 'normal',
    is_admin            BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
    reply_to_message_id UUID REFERENCES "EgRailway".global_chat_messages(id) ON DELETE SET NULL,
    reply_to_user_name  VARCHAR(50),
    reply_to_text       TEXT,
    deleted_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_global_chat_text_not_blank
        CHECK (btrim(text) <> ''),
    CONSTRAINT chk_global_chat_message_type
        CHECK (message_type IN ('normal', 'admin'))
);

CREATE TABLE IF NOT EXISTS "EgRailway".global_chat_reactions (
    id              BIGSERIAL PRIMARY KEY,
    message_id      UUID NOT NULL REFERENCES "EgRailway".global_chat_messages(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES "EgRailway".profiles(id) ON DELETE CASCADE,
    reaction_type   VARCHAR(20) NOT NULL DEFAULT 'love',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_global_chat_reaction_type
        CHECK (reaction_type IN ('love')),
    CONSTRAINT uq_global_chat_reactions_message_user_type
        UNIQUE (message_id, user_id, reaction_type)
);

CREATE TABLE IF NOT EXISTS "EgRailway".global_chat_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_global_chat_settings_value_object
        CHECK (jsonb_typeof(value) = 'object')
);

INSERT INTO "EgRailway".global_chat_settings (key, value)
VALUES ('enabled', '{"enabled": true}'::jsonb)
ON CONFLICT (key) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_global_chat_messages_created_at
ON "EgRailway".global_chat_messages (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_global_chat_messages_user_id
ON "EgRailway".global_chat_messages (user_id);

CREATE INDEX IF NOT EXISTS idx_global_chat_messages_visible
ON "EgRailway".global_chat_messages (is_deleted, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_global_chat_reactions_message_id
ON "EgRailway".global_chat_reactions (message_id);

CREATE INDEX IF NOT EXISTS idx_global_chat_reactions_user_id
ON "EgRailway".global_chat_reactions (user_id);

CREATE OR REPLACE FUNCTION "EgRailway".update_global_chat_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_global_chat_messages_updated_at ON "EgRailway".global_chat_messages;
CREATE TRIGGER trg_global_chat_messages_updated_at
    BEFORE UPDATE ON "EgRailway".global_chat_messages
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".update_global_chat_timestamp();

DROP TRIGGER IF EXISTS trg_global_chat_settings_updated_at ON "EgRailway".global_chat_settings;
CREATE TRIGGER trg_global_chat_settings_updated_at
    BEFORE UPDATE ON "EgRailway".global_chat_settings
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".update_global_chat_timestamp();

ALTER TABLE "EgRailway".global_chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE "EgRailway".global_chat_reactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE "EgRailway".global_chat_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role full access to global chat messages"
ON "EgRailway".global_chat_messages;
CREATE POLICY "Service role full access to global chat messages"
    ON "EgRailway".global_chat_messages FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

DROP POLICY IF EXISTS "Service role full access to global chat reactions"
ON "EgRailway".global_chat_reactions;
CREATE POLICY "Service role full access to global chat reactions"
    ON "EgRailway".global_chat_reactions FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

DROP POLICY IF EXISTS "Service role full access to global chat settings"
ON "EgRailway".global_chat_settings;
CREATE POLICY "Service role full access to global chat settings"
    ON "EgRailway".global_chat_settings FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

COMMENT ON TABLE "EgRailway".global_chat_messages
IS 'Persistent public/global chat messages shared by all authenticated users.';

COMMENT ON TABLE "EgRailway".global_chat_reactions
IS 'Per-user reactions for global chat messages. Currently supports love only.';

COMMENT ON TABLE "EgRailway".global_chat_settings
IS 'Small key-value settings for global chat, e.g. enabled/disabled.';

COMMIT;
