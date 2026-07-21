ALTER TABLE "EgRailway".app_config
ADD COLUMN IF NOT EXISTS map_viewer_boost_enabled BOOLEAN NOT NULL DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS map_viewer_boost_min INTEGER NOT NULL DEFAULT 15,
ADD COLUMN IF NOT EXISTS map_viewer_boost_max INTEGER NOT NULL DEFAULT 30;

UPDATE "EgRailway".app_config
SET
    map_viewer_boost_min = GREATEST(0, LEAST(map_viewer_boost_min, 999)),
    map_viewer_boost_max = GREATEST(0, LEAST(map_viewer_boost_max, 999));

UPDATE "EgRailway".app_config
SET
    map_viewer_boost_min = LEAST(map_viewer_boost_min, map_viewer_boost_max),
    map_viewer_boost_max = GREATEST(map_viewer_boost_min, map_viewer_boost_max);

ALTER TABLE "EgRailway".app_config
DROP CONSTRAINT IF EXISTS chk_app_config_map_viewer_boost_range;

ALTER TABLE "EgRailway".app_config
ADD CONSTRAINT chk_app_config_map_viewer_boost_range
CHECK (
    map_viewer_boost_min >= 0
    AND map_viewer_boost_max >= map_viewer_boost_min
    AND map_viewer_boost_max <= 999
);
