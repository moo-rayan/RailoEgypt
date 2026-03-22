-- ============================================================
-- Migration: Create profiles table + auto-create trigger
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor)
-- ============================================================

-- 1. Create profiles table
CREATE TABLE IF NOT EXISTS "EgRailway".profiles (
    id          UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email       TEXT,
    display_name TEXT,
    avatar_url  TEXT,

    -- Tracking contribution
    is_contributor      BOOLEAN NOT NULL DEFAULT FALSE,
    contribution_count  INTEGER NOT NULL DEFAULT 0,
    reputation_score    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    last_contribution_at TIMESTAMPTZ,

    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profiles_email ON "EgRailway".profiles(email);

-- 2. Auto-update updated_at on every UPDATE
CREATE OR REPLACE FUNCTION "EgRailway".update_profiles_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON "EgRailway".profiles;
CREATE TRIGGER trg_profiles_updated_at
    BEFORE UPDATE ON "EgRailway".profiles
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".update_profiles_timestamp();

-- 3. Auto-create profile on new user sign-up
CREATE OR REPLACE FUNCTION "EgRailway".handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO "EgRailway".profiles (id, email, display_name, avatar_url)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', NEW.raw_user_meta_data->>'name', ''),
        COALESCE(NEW.raw_user_meta_data->>'avatar_url', NEW.raw_user_meta_data->>'picture', '')
    )
    ON CONFLICT (id) DO UPDATE SET
        email        = EXCLUDED.email,
        display_name = EXCLUDED.display_name,
        avatar_url   = EXCLUDED.avatar_url,
        updated_at   = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION "EgRailway".handle_new_user();

-- 4. Enable Row Level Security
ALTER TABLE "EgRailway".profiles ENABLE ROW LEVEL SECURITY;

-- Users can read their own profile
CREATE POLICY "Users can view own profile"
    ON "EgRailway".profiles FOR SELECT
    USING (auth.uid() = id);

-- Users can update their own profile
CREATE POLICY "Users can update own profile"
    ON "EgRailway".profiles FOR UPDATE
    USING (auth.uid() = id);

-- Service role can do everything (for backend API)
CREATE POLICY "Service role full access"
    ON "EgRailway".profiles FOR ALL
    USING (auth.role() = 'service_role');

-- 5. Backfill: create profiles for any existing auth users
INSERT INTO "EgRailway".profiles (id, email, display_name, avatar_url)
SELECT
    id,
    email,
    COALESCE(raw_user_meta_data->>'full_name', raw_user_meta_data->>'name', ''),
    COALESCE(raw_user_meta_data->>'avatar_url', raw_user_meta_data->>'picture', '')
FROM auth.users
ON CONFLICT (id) DO NOTHING;
