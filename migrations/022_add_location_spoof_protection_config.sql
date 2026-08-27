ALTER TABLE "EgRailway".app_config
ADD COLUMN IF NOT EXISTS location_spoof_protection_enabled BOOLEAN NOT NULL DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS block_mock_locations_enabled BOOLEAN NOT NULL DEFAULT TRUE,
ADD COLUMN IF NOT EXISTS block_fake_gps_apps_enabled BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE "EgRailway".app_config
SET
    location_spoof_protection_enabled = COALESCE(location_spoof_protection_enabled, FALSE),
    block_mock_locations_enabled = COALESCE(block_mock_locations_enabled, TRUE),
    block_fake_gps_apps_enabled = COALESCE(block_fake_gps_apps_enabled, TRUE);
