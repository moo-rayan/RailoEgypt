-- Free-form note displayed under a specific trip stop in the app.
-- Replaces the fixed "مرور + train number" rendering with admin-authored text.
ALTER TABLE "EgRailway".trip_stops
ADD COLUMN IF NOT EXISTS passing_note TEXT NOT NULL DEFAULT '';

UPDATE "EgRailway".trip_stops
SET passing_note = COALESCE((
  SELECT string_agg(value, '، ')
  FROM jsonb_array_elements_text(passing_train_numbers) AS value
  WHERE btrim(value) <> ''
), '')
WHERE btrim(COALESCE(passing_note, '')) = ''
  AND jsonb_typeof(passing_train_numbers) = 'array'
  AND EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(passing_train_numbers) AS value
    WHERE btrim(value) <> ''
  );

COMMENT ON COLUMN "EgRailway".trip_stops.passing_note
IS 'Free-form note displayed under this exact scheduled stop in the app.';
