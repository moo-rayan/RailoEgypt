-- Store station ids where a train passes without being a scheduled stop.
ALTER TABLE "EgRailway".trains
ADD COLUMN IF NOT EXISTS passing_station_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_trains_passing_station_ids_gin
ON "EgRailway".trains
USING GIN (passing_station_ids);

COMMENT ON COLUMN "EgRailway".trains.passing_station_ids
IS 'JSON array of station IDs where this train should be shown as passing through.';
