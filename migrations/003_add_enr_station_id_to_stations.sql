-- Add ENR external station identifier to stations.
-- This ID comes from the ENR station selector values, for example:
-- <option value="606535390819516493">EL-MINIA</option>

ALTER TABLE "EgRailway".stations
ADD COLUMN IF NOT EXISTS enr_station_id TEXT;

CREATE INDEX IF NOT EXISTS idx_stations_enr_station_id
ON "EgRailway".stations (enr_station_id);

COMMENT ON COLUMN "EgRailway".stations.enr_station_id
IS 'External ENR station identifier used when requesting live fares.';
