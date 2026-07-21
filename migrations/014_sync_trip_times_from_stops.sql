-- ============================================================
-- Migration: Backfill trip departure/arrival summaries from stops
-- Purpose:
--   Some trips were created/edited with empty summary times.
--   The authoritative route timing is the first and last trip_stops rows.
-- ============================================================

CREATE OR REPLACE FUNCTION "EgRailway".tmp_trainlive_is_blank(value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT NULLIF(BTRIM(COALESCE(value, '')), '') IS NULL
        OR UPPER(BTRIM(COALESCE(value, ''))) = 'EMPTY';
$$;

CREATE OR REPLACE FUNCTION "EgRailway".tmp_trainlive_time_ar(ar_value text, en_value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN NOT "EgRailway".tmp_trainlive_is_blank(ar_value) THEN
            REPLACE(REPLACE(REPLACE(REPLACE(BTRIM(ar_value), 'AM', 'ص'), 'PM', 'م'), 'am', 'ص'), 'pm', 'م')
        ELSE REPLACE(REPLACE(REPLACE(REPLACE(BTRIM(COALESCE(en_value, '')), 'AM', 'ص'), 'PM', 'م'), 'am', 'ص'), 'pm', 'م')
    END;
$$;

CREATE OR REPLACE FUNCTION "EgRailway".tmp_trainlive_time_en(en_value text, ar_value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN NOT "EgRailway".tmp_trainlive_is_blank(en_value) THEN
            REPLACE(REPLACE(REPLACE(REPLACE(BTRIM(en_value), 'ص', 'AM'), 'م', 'PM'), 'am', 'AM'), 'pm', 'PM')
        ELSE REPLACE(REPLACE(REPLACE(REPLACE(BTRIM(COALESCE(ar_value, '')), 'ص', 'AM'), 'م', 'PM'), 'am', 'AM'), 'pm', 'PM')
    END;
$$;

CREATE OR REPLACE FUNCTION "EgRailway".tmp_trainlive_time_minutes(value text)
RETURNS integer
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    text_value text := BTRIM(COALESCE(value, ''));
    matched text[];
    hour_value integer;
    minute_value integer;
    period_value text;
BEGIN
    IF "EgRailway".tmp_trainlive_is_blank(text_value) THEN
        RETURN NULL;
    END IF;

    matched := regexp_match(text_value, '^([0-9]{1,2}):([0-9]{2})\s*(ص|م|AM|PM|am|pm)$');
    IF matched IS NULL THEN
        RETURN NULL;
    END IF;

    hour_value := matched[1]::integer;
    minute_value := matched[2]::integer;
    period_value := lower(matched[3]);

    IF period_value IN ('م', 'pm') THEN
        IF hour_value <> 12 THEN
            hour_value := hour_value + 12;
        END IF;
    ELSIF hour_value = 12 THEN
        hour_value := 0;
    END IF;

    RETURN hour_value * 60 + minute_value;
END;
$$;

CREATE OR REPLACE FUNCTION "EgRailway".tmp_trainlive_duration_ar(dep_value text, arr_value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    dep_minutes integer;
    arr_minutes integer;
    diff_minutes integer;
    hours_value integer;
    minutes_value integer;
BEGIN
    dep_minutes := "EgRailway".tmp_trainlive_time_minutes(dep_value);
    arr_minutes := "EgRailway".tmp_trainlive_time_minutes(arr_value);
    IF dep_minutes IS NULL OR arr_minutes IS NULL THEN
        RETURN '';
    END IF;

    diff_minutes := arr_minutes - dep_minutes;
    IF diff_minutes <= 0 THEN
        diff_minutes := diff_minutes + (24 * 60);
    END IF;

    hours_value := diff_minutes / 60;
    minutes_value := diff_minutes % 60;

    IF hours_value > 0 AND minutes_value > 0 THEN
        RETURN CONCAT(hours_value, ' س و ', minutes_value, ' د');
    ELSIF hours_value > 0 THEN
        RETURN CONCAT(hours_value, ' س');
    END IF;

    RETURN CONCAT(minutes_value, ' د');
END;
$$;

CREATE OR REPLACE FUNCTION "EgRailway".tmp_trainlive_duration_en(dep_value text, arr_value text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    dep_minutes integer;
    arr_minutes integer;
    diff_minutes integer;
    hours_value integer;
    minutes_value integer;
BEGIN
    dep_minutes := "EgRailway".tmp_trainlive_time_minutes(dep_value);
    arr_minutes := "EgRailway".tmp_trainlive_time_minutes(arr_value);
    IF dep_minutes IS NULL OR arr_minutes IS NULL THEN
        RETURN '';
    END IF;

    diff_minutes := arr_minutes - dep_minutes;
    IF diff_minutes <= 0 THEN
        diff_minutes := diff_minutes + (24 * 60);
    END IF;

    hours_value := diff_minutes / 60;
    minutes_value := diff_minutes % 60;

    IF hours_value > 0 AND minutes_value > 0 THEN
        RETURN CONCAT(hours_value, 'h ', minutes_value, 'm');
    ELSIF hours_value > 0 THEN
        RETURN CONCAT(hours_value, 'h');
    END IF;

    RETURN CONCAT(minutes_value, 'm');
END;
$$;

WITH first_stop AS (
    SELECT DISTINCT ON (trip_id)
        trip_id,
        station_id,
        time_ar,
        time_en
    FROM "EgRailway".trip_stops
    ORDER BY trip_id, stop_order ASC, id ASC
),
last_stop AS (
    SELECT DISTINCT ON (trip_id)
        trip_id,
        station_id,
        time_ar,
        time_en
    FROM "EgRailway".trip_stops
    ORDER BY trip_id, stop_order DESC, id DESC
),
stop_counts AS (
    SELECT trip_id, COUNT(*)::integer AS stops_count
    FROM "EgRailway".trip_stops
    GROUP BY trip_id
),
summary AS (
    SELECT
        c.trip_id,
        c.stops_count,
        fs.station_id AS from_station_id,
        ls.station_id AS to_station_id,
        "EgRailway".tmp_trainlive_time_ar(fs.time_ar, fs.time_en) AS departure_ar,
        "EgRailway".tmp_trainlive_time_en(fs.time_en, fs.time_ar) AS departure_en,
        "EgRailway".tmp_trainlive_time_ar(ls.time_ar, ls.time_en) AS arrival_ar,
        "EgRailway".tmp_trainlive_time_en(ls.time_en, ls.time_ar) AS arrival_en
    FROM stop_counts c
    JOIN first_stop fs ON fs.trip_id = c.trip_id
    JOIN last_stop ls ON ls.trip_id = c.trip_id
)
UPDATE "EgRailway".trips t
SET
    from_station_id = COALESCE(t.from_station_id, s.from_station_id),
    to_station_id = COALESCE(t.to_station_id, s.to_station_id),
    departure_ar = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(t.departure_ar) THEN s.departure_ar
        ELSE t.departure_ar
    END,
    departure_en = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(t.departure_en) THEN s.departure_en
        ELSE t.departure_en
    END,
    arrival_ar = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(t.arrival_ar) THEN s.arrival_ar
        ELSE t.arrival_ar
    END,
    arrival_en = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(t.arrival_en) THEN s.arrival_en
        ELSE t.arrival_en
    END,
    duration_ar = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(t.duration_ar) THEN
            "EgRailway".tmp_trainlive_duration_ar(
                CASE WHEN "EgRailway".tmp_trainlive_is_blank(t.departure_ar) THEN s.departure_ar ELSE t.departure_ar END,
                CASE WHEN "EgRailway".tmp_trainlive_is_blank(t.arrival_ar) THEN s.arrival_ar ELSE t.arrival_ar END
            )
        ELSE t.duration_ar
    END,
    duration_en = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(t.duration_en) THEN
            "EgRailway".tmp_trainlive_duration_en(
                CASE WHEN "EgRailway".tmp_trainlive_is_blank(t.departure_en) THEN s.departure_en ELSE t.departure_en END,
                CASE WHEN "EgRailway".tmp_trainlive_is_blank(t.arrival_en) THEN s.arrival_en ELSE t.arrival_en END
            )
        ELSE t.duration_en
    END,
    stops_count = s.stops_count
FROM summary s
WHERE t.id = s.trip_id;

WITH primary_trip AS (
    SELECT DISTINCT ON (train_number)
        train_number,
        departure_ar,
        departure_en,
        arrival_ar,
        arrival_en,
        stops_count
    FROM "EgRailway".trips
    ORDER BY train_number, id ASC
)
UPDATE "EgRailway".trains tr
SET
    departure_ar = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(tr.departure_ar) THEN p.departure_ar
        ELSE tr.departure_ar
    END,
    departure_en = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(tr.departure_en) THEN p.departure_en
        ELSE tr.departure_en
    END,
    arrival_ar = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(tr.arrival_ar) THEN p.arrival_ar
        ELSE tr.arrival_ar
    END,
    arrival_en = CASE
        WHEN "EgRailway".tmp_trainlive_is_blank(tr.arrival_en) THEN p.arrival_en
        ELSE tr.arrival_en
    END,
    stops_count = CASE
        WHEN tr.stops_count = 0 AND p.stops_count > 0 THEN p.stops_count
        ELSE tr.stops_count
    END
FROM primary_trip p
WHERE tr.train_id = p.train_number;

DROP FUNCTION IF EXISTS "EgRailway".tmp_trainlive_duration_en(text, text);
DROP FUNCTION IF EXISTS "EgRailway".tmp_trainlive_duration_ar(text, text);
DROP FUNCTION IF EXISTS "EgRailway".tmp_trainlive_time_minutes(text);
DROP FUNCTION IF EXISTS "EgRailway".tmp_trainlive_time_en(text, text);
DROP FUNCTION IF EXISTS "EgRailway".tmp_trainlive_time_ar(text, text);
DROP FUNCTION IF EXISTS "EgRailway".tmp_trainlive_is_blank(text);
