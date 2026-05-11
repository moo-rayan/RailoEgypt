-- Track fares that were inserted or changed from the live ENR endpoint.

ALTER TABLE "EgRailway".trip_fares
ADD COLUMN IF NOT EXISTS online_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_trip_fares_online_updated_at
ON "EgRailway".trip_fares (online_updated_at);

CREATE INDEX IF NOT EXISTS idx_trip_fares_route_online_updated_at
ON "EgRailway".trip_fares (from_station_id, to_station_id, online_updated_at);

COMMENT ON COLUMN "EgRailway".trip_fares.online_updated_at
IS 'Set when a fare row is inserted or its price is changed from the live ENR endpoint.';
