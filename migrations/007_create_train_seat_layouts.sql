-- ============================================================
-- Migration: Create train_seat_layouts table
-- Stores static seat layout per train number and travel class.
-- Run this in Supabase SQL Editor.
-- ============================================================

CREATE TABLE IF NOT EXISTS "EgRailway".train_seat_layouts (
    id                  BIGSERIAL PRIMARY KEY,
    train_number        VARCHAR(20) NOT NULL,
    class_code          TEXT NOT NULL,
    class_name_ar       TEXT NOT NULL DEFAULT '',
    class_name_en       TEXT NOT NULL DEFAULT '',
    enr_train_id        TEXT NOT NULL DEFAULT '',
    coach_count         INTEGER NOT NULL DEFAULT 0,
    seat_count          INTEGER NOT NULL DEFAULT 0,
    window_seat_count   INTEGER NOT NULL DEFAULT 0,
    aisle_seat_count    INTEGER NOT NULL DEFAULT 0,
    layout_hash         TEXT NOT NULL,
    layout              JSONB NOT NULL,
    source_file         TEXT NOT NULL DEFAULT '',
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_train_seat_layouts_train
        FOREIGN KEY (train_number)
        REFERENCES "EgRailway".trains(train_id)
        ON DELETE CASCADE,

    CONSTRAINT uq_train_seat_layouts_train_class
        UNIQUE (train_number, class_code),

    CONSTRAINT chk_train_seat_layouts_counts
        CHECK (
            coach_count >= 0
            AND seat_count >= 0
            AND window_seat_count >= 0
            AND aisle_seat_count >= 0
        )
);

COMMENT ON TABLE "EgRailway".train_seat_layouts IS
    'Static seat layout per train and class, extracted from ENR seat maps without availability/sold state.';

COMMENT ON COLUMN "EgRailway".train_seat_layouts.train_number IS
    'FK to EgRailway.trains.train_id.';

COMMENT ON COLUMN "EgRailway".train_seat_layouts.class_code IS
    'ENR coach class code such as AC 1, AC 2, AC 3, GA 2, PRIMUM VIP.';

COMMENT ON COLUMN "EgRailway".train_seat_layouts.layout IS
    'JSONB payload with coaches, rows, seat coordinates, and derived window/aisle flags.';

CREATE INDEX IF NOT EXISTS idx_train_seat_layouts_train_number
    ON "EgRailway".train_seat_layouts(train_number);

CREATE INDEX IF NOT EXISTS idx_train_seat_layouts_class_code
    ON "EgRailway".train_seat_layouts(class_code);

CREATE INDEX IF NOT EXISTS idx_train_seat_layouts_layout_gin
    ON "EgRailway".train_seat_layouts
    USING GIN (layout);

CREATE OR REPLACE FUNCTION "EgRailway".update_train_seat_layouts_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_train_seat_layouts_updated_at
    ON "EgRailway".train_seat_layouts;

CREATE TRIGGER trg_train_seat_layouts_updated_at
    BEFORE UPDATE ON "EgRailway".train_seat_layouts
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".update_train_seat_layouts_timestamp();

ALTER TABLE "EgRailway".train_seat_layouts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role full access on train_seat_layouts"
    ON "EgRailway".train_seat_layouts;

CREATE POLICY "Service role full access on train_seat_layouts"
    ON "EgRailway".train_seat_layouts FOR ALL
    USING (auth.role() = 'service_role');

DROP POLICY IF EXISTS "Authenticated users can read train_seat_layouts"
    ON "EgRailway".train_seat_layouts;

CREATE POLICY "Authenticated users can read train_seat_layouts"
    ON "EgRailway".train_seat_layouts FOR SELECT
    USING (auth.role() = 'authenticated');
