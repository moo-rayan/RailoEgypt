-- Track unique users who opened each news article.

CREATE TABLE IF NOT EXISTS "EgRailway".news_views (
    id         BIGSERIAL PRIMARY KEY,
    news_id    INTEGER NOT NULL REFERENCES "EgRailway".news(id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES "EgRailway".profiles(id) ON DELETE CASCADE,
    viewed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_news_views_news_user UNIQUE (news_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_news_views_news_id
ON "EgRailway".news_views (news_id);

CREATE INDEX IF NOT EXISTS idx_news_views_user_id
ON "EgRailway".news_views (user_id);

CREATE INDEX IF NOT EXISTS idx_news_views_viewed_at
ON "EgRailway".news_views (viewed_at);

COMMENT ON TABLE "EgRailway".news_views
IS 'One row per user per news article opened in the mobile app.';

COMMENT ON COLUMN "EgRailway".news_views.viewed_at
IS 'Last time this user opened the article details.';
