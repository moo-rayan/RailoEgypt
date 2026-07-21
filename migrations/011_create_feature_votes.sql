-- Generic feature votes table.
-- Stores kiosk-interest votes now, and can be reused for future in-app votes.

CREATE TABLE IF NOT EXISTS "EgRailway".feature_votes (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES "EgRailway".profiles(id) ON DELETE CASCADE,

    feature_key     TEXT NOT NULL,
    vote_value      TEXT NOT NULL,

    -- Optional scope for votes tied to a specific entity.
    -- Examples:
    --   target_type = 'station', target_id = '123'
    --   target_type = 'train',   target_id = '979'
    --   target_type = 'global',  target_id = ''
    target_type     TEXT NOT NULL DEFAULT 'global',
    target_id       TEXT NOT NULL DEFAULT '',

    context_data    JSONB NOT NULL DEFAULT '{}'::jsonb,
    client_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source          TEXT NOT NULL DEFAULT 'mobile_app',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_feature_votes_feature_key_not_blank
        CHECK (btrim(feature_key) <> ''),
    CONSTRAINT chk_feature_votes_vote_value_not_blank
        CHECK (btrim(vote_value) <> ''),
    CONSTRAINT chk_feature_votes_target_type_not_blank
        CHECK (btrim(target_type) <> ''),
    CONSTRAINT chk_feature_votes_context_object
        CHECK (jsonb_typeof(context_data) = 'object'),
    CONSTRAINT chk_feature_votes_metadata_object
        CHECK (jsonb_typeof(client_metadata) = 'object'),
    CONSTRAINT uq_feature_votes_user_scope
        UNIQUE (user_id, feature_key, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_feature_votes_feature_key
ON "EgRailway".feature_votes (feature_key, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_feature_votes_target
ON "EgRailway".feature_votes (target_type, target_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_feature_votes_value
ON "EgRailway".feature_votes (feature_key, vote_value);

CREATE INDEX IF NOT EXISTS idx_feature_votes_context_data_gin
ON "EgRailway".feature_votes USING GIN (context_data);

CREATE OR REPLACE FUNCTION "EgRailway".update_feature_votes_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_feature_votes_updated_at ON "EgRailway".feature_votes;
CREATE TRIGGER trg_feature_votes_updated_at
    BEFORE UPDATE ON "EgRailway".feature_votes
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".update_feature_votes_timestamp();

ALTER TABLE "EgRailway".feature_votes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can view own feature votes" ON "EgRailway".feature_votes;
CREATE POLICY "Users can view own feature votes"
    ON "EgRailway".feature_votes FOR SELECT
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can insert own feature votes" ON "EgRailway".feature_votes;
CREATE POLICY "Users can insert own feature votes"
    ON "EgRailway".feature_votes FOR INSERT
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can update own feature votes" ON "EgRailway".feature_votes;
CREATE POLICY "Users can update own feature votes"
    ON "EgRailway".feature_votes FOR UPDATE
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Service role full access to feature votes" ON "EgRailway".feature_votes;
CREATE POLICY "Service role full access to feature votes"
    ON "EgRailway".feature_votes FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

COMMENT ON TABLE "EgRailway".feature_votes
IS 'Generic table for user votes/feedback about app features and scoped entities.';

COMMENT ON COLUMN "EgRailway".feature_votes.feature_key
IS 'Stable feature identifier, e.g. station_kiosk_ordering.';

COMMENT ON COLUMN "EgRailway".feature_votes.vote_value
IS 'Flexible vote value, e.g. interested, not_interested, helpful, not_helpful.';

COMMENT ON COLUMN "EgRailway".feature_votes.context_data
IS 'Optional JSON context such as station_id, kiosk_id, screen name, or experiment data.';
