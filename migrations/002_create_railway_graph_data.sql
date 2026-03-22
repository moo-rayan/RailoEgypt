-- ============================================================
-- Migration: Create railway_graph_data table
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor)
-- ============================================================

-- 1. Create railway_graph_data table
CREATE TABLE IF NOT EXISTS "EgRailway".railway_graph_data (
    id          SERIAL PRIMARY KEY,
    version     TEXT NOT NULL DEFAULT '1.0',
    data        JSONB NOT NULL,
    node_count  INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Add comments for documentation
COMMENT ON TABLE "EgRailway".railway_graph_data IS 
    'Stores the complete railway graph structure including nodes, edges, spatial grid, and display polylines';

COMMENT ON COLUMN "EgRailway".railway_graph_data.version IS 
    'Graph version identifier';

COMMENT ON COLUMN "EgRailway".railway_graph_data.data IS 
    'Complete graph data: nodes, adj, grid, lines (same format as Redis cache)';

COMMENT ON COLUMN "EgRailway".railway_graph_data.node_count IS 
    'Total number of nodes in the graph';

-- 2. Create index on version for faster lookups
CREATE INDEX IF NOT EXISTS idx_railway_graph_data_version 
    ON "EgRailway".railway_graph_data(version);

-- 3. Create index on created_at for sorting by latest
CREATE INDEX IF NOT EXISTS idx_railway_graph_data_created_at 
    ON "EgRailway".railway_graph_data(created_at DESC);

-- 4. Auto-update updated_at on every UPDATE
CREATE OR REPLACE FUNCTION "EgRailway".update_railway_graph_data_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_railway_graph_data_updated_at ON "EgRailway".railway_graph_data;
CREATE TRIGGER trg_railway_graph_data_updated_at
    BEFORE UPDATE ON "EgRailway".railway_graph_data
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".update_railway_graph_data_timestamp();

-- 5. Enable Row Level Security (optional - for security)
ALTER TABLE "EgRailway".railway_graph_data ENABLE ROW LEVEL SECURITY;

-- Service role can do everything (for backend API)
CREATE POLICY "Service role full access on railway_graph_data"
    ON "EgRailway".railway_graph_data FOR ALL
    USING (auth.role() = 'service_role');

-- Public read access (authenticated users can read graph data)
CREATE POLICY "Authenticated users can read railway_graph_data"
    ON "EgRailway".railway_graph_data FOR SELECT
    USING (auth.role() = 'authenticated');
