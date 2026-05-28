-- App startup announcements shown in the mobile app.
-- A user sees the active announcement once per (announcement id + version).

CREATE TABLE IF NOT EXISTS "EgRailway".app_announcements (
    id                   BIGSERIAL PRIMARY KEY,
    version              INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    is_active            BOOLEAN NOT NULL DEFAULT FALSE,
    priority             INTEGER NOT NULL DEFAULT 0,

    title_ar             TEXT NOT NULL DEFAULT '',
    title_en             TEXT NOT NULL DEFAULT '',
    body_ar              TEXT NOT NULL DEFAULT '',
    body_en              TEXT NOT NULL DEFAULT '',
    image_url            TEXT,

    display_mode         TEXT NOT NULL DEFAULT 'dialog'
                         CHECK (display_mode IN ('dialog', 'fullscreen')),
    width_ratio          NUMERIC(3,2) NOT NULL DEFAULT 0.92
                         CHECK (width_ratio > 0 AND width_ratio <= 1),
    max_height_ratio     NUMERIC(3,2) NOT NULL DEFAULT 0.82
                         CHECK (max_height_ratio > 0 AND max_height_ratio <= 1),
    image_fit            TEXT NOT NULL DEFAULT 'cover'
                         CHECK (image_fit IN ('cover', 'contain')),

    show_action_button   BOOLEAN NOT NULL DEFAULT FALSE,
    action_text_ar       TEXT NOT NULL DEFAULT '',
    action_text_en       TEXT NOT NULL DEFAULT '',
    action_url           TEXT NOT NULL DEFAULT '',

    show_dismiss_button  BOOLEAN NOT NULL DEFAULT TRUE,
    dismiss_text_ar      TEXT NOT NULL DEFAULT 'إخفاء',
    dismiss_text_en      TEXT NOT NULL DEFAULT 'Dismiss',
    dismissible          BOOLEAN NOT NULL DEFAULT TRUE,

    start_at             TIMESTAMPTZ,
    end_at               TIMESTAMPTZ,
    created_by           UUID REFERENCES "EgRailway".profiles(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_announcements_active_window
ON "EgRailway".app_announcements (is_active, priority DESC, updated_at DESC)
WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_app_announcements_version
ON "EgRailway".app_announcements (version);

COMMENT ON TABLE "EgRailway".app_announcements
IS 'Startup announcements controlled from the dashboard and shown in the mobile app.';

COMMENT ON COLUMN "EgRailway".app_announcements.version
IS 'Increase this number to show the same announcement again to users who already dismissed it.';
