-- Correct passing-train storage: it belongs to a specific trip stop, not to
-- every occurrence of a station globally.
ALTER TABLE "EgRailway".trip_stops
ADD COLUMN IF NOT EXISTS passing_train_numbers JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_trip_stops_passing_train_numbers_gin
ON "EgRailway".trip_stops
USING GIN (passing_train_numbers);

-- Previous approach stored station IDs on the train and made passing trains
-- appear for every trip that stopped at that station. Keep this cleanup safe
-- for deployments where migration 008 was already applied.
DROP INDEX IF EXISTS "EgRailway".idx_trains_passing_station_ids_gin;
ALTER TABLE "EgRailway".trains
DROP COLUMN IF EXISTS passing_station_ids;

COMMENT ON COLUMN "EgRailway".trip_stops.passing_train_numbers
IS 'JSON array of train numbers that pass this exact scheduled stop.';
