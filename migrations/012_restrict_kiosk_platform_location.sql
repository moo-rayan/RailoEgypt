-- Restrict kiosk map placement to the two supported sides.

BEGIN;

UPDATE "EgRailway".kiosks
SET platform_location = lower(btrim(platform_location))
WHERE lower(btrim(platform_location)) IN ('left', 'right');

UPDATE "EgRailway".kiosks
SET platform_location = 'right'
WHERE platform_location IS NULL
   OR lower(btrim(platform_location)) NOT IN ('left', 'right');

ALTER TABLE "EgRailway".kiosks
    ALTER COLUMN platform_location SET DEFAULT 'right',
    ALTER COLUMN platform_location SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.conname = 'chk_kiosks_platform_location_side'
          AND t.relname = 'kiosks'
          AND n.nspname = 'EgRailway'
    ) THEN
        ALTER TABLE "EgRailway".kiosks
            ADD CONSTRAINT chk_kiosks_platform_location_side
            CHECK (platform_location IN ('left', 'right'));
    END IF;
END $$;

COMMENT ON COLUMN "EgRailway".kiosks.platform_location
IS 'Map side for kiosk marker relative to the station marker: left or right.';

COMMIT;
