"""
Seed EgRailway schema with Stations.json, Trains.json, and Trips.json.

Usage:
    python scripts/seed_db.py               # seed all tables
    python scripts/seed_db.py stations      # seed only stations
    python scripts/seed_db.py trains        # seed only trains
    python scripts/seed_db.py trips         # seed only trips

Connection:
    Reads DATABASE_URL from backend/.env
    Automatically appends sslmode=require for Supabase.

    If you get a DNS error, use the Session Pooler URL from:
    Supabase Dashboard → Project Settings → Database → Connection pooling
    Format: postgresql://postgres.[ref]:[pass]@aws-0-[region].pooler.supabase.com:5432/postgres
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR      = Path(__file__).parent.parent
DATA_DIR      = BASE_DIR.parent / "trainLiveApp/assets/data"
STATIONS_FILE = DATA_DIR / "Stations.json"
TRAINS_FILE   = DATA_DIR / "Trains.json"
TRIPS_FILE    = DATA_DIR / "Trips.json"

load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not set in .env")

def _build_params(url: str) -> dict:
    parsed = urlparse(url)
    params = dict(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
        sslmode="require",          # Supabase requires SSL
        connect_timeout=15,
    )
    return params

DB_PARAMS = _build_params(DATABASE_URL)


def seed_stations(cur: psycopg2.extensions.cursor) -> int:
    with open(STATIONS_FILE, encoding="utf-8") as f:
        stations: list[dict] = json.load(f)

    cur.execute('TRUNCATE TABLE "EgRailway"."stations" RESTART IDENTITY CASCADE')

    rows = [
        (
            s["name_ar"],
            s["name_en"],
            s.get("latitude"),
            s.get("longitude"),
            s.get("place_id"),
        )
        for s in stations
    ]

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO "EgRailway"."stations"
            (name_ar, name_en, latitude, longitude, place_id)
        VALUES %s
        """,
        rows,
        page_size=200,
    )
    return len(rows)


def seed_trains(cur: psycopg2.extensions.cursor) -> int:
    with open(TRAINS_FILE, encoding="utf-8") as f:
        data: dict = json.load(f)
    trains: list[dict] = data["trains"]

    cur.execute('TRUNCATE TABLE "EgRailway"."trains" RESTART IDENTITY CASCADE')

    rows = [
        (
            str(t["train_id"]),
            t["type_ar"],
            t["type_en"],
            t["start_station_ar"],
            t["start_station_en"],
            t["end_station_ar"],
            t["end_station_en"],
            int(t["stops_count"]),
            t.get("tNoteAr", "") or "",
            t.get("tNoteEn", "") or "",
        )
        for t in trains
    ]

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO "EgRailway"."trains"
            (train_id, type_ar, type_en,
             start_station_ar, start_station_en,
             end_station_ar,   end_station_en,
             stops_count, note_ar, note_en)
        VALUES %s
        ON CONFLICT (train_id) DO UPDATE SET
            type_ar          = EXCLUDED.type_ar,
            type_en          = EXCLUDED.type_en,
            start_station_ar = EXCLUDED.start_station_ar,
            start_station_en = EXCLUDED.start_station_en,
            end_station_ar   = EXCLUDED.end_station_ar,
            end_station_en   = EXCLUDED.end_station_en,
            stops_count      = EXCLUDED.stops_count,
            note_ar          = EXCLUDED.note_ar,
            note_en          = EXCLUDED.note_en
        """,
        rows,
        page_size=200,
    )
    return len(rows)


def _load_station_map(cur: psycopg2.extensions.cursor) -> dict[str, int]:
    """Returns {name_ar: station_id} for fast lookup."""
    cur.execute('SELECT id, name_ar FROM "EgRailway"."stations"')
    return {row[1].strip(): row[0] for row in cur.fetchall()}


def seed_trips(cur: psycopg2.extensions.cursor) -> tuple[int, int]:
    with open(TRIPS_FILE, encoding="utf-8") as f:
        data: dict = json.load(f)

    station_map = _load_station_map(cur)

    from_ar = data["from"]["name_ar"]
    from_en = data["from"]["name_en"]
    to_ar   = data["to"]["name_ar"]
    to_en   = data["to"]["name_en"]

    from_id = station_map.get(from_ar.strip())
    to_id   = station_map.get(to_ar.strip())

    trips: list[dict] = data["trips"]
    unmatched: set[str] = set()
    trip_rows, stop_rows = [], []

    for trip in trips:
        stops_count = int(trip.get("stops_count", 0))
        trip_rows.append((
            str(trip["train_number"]),
            from_id,
            to_id,
            trip.get("departure_ar", ""),
            trip.get("departure_en", ""),
            trip.get("arrival_ar",   ""),
            trip.get("arrival_en",   ""),
            trip.get("duration_ar",  ""),
            trip.get("duration_en",  ""),
            stops_count,
            json.dumps(trip.get("fares") or {}, ensure_ascii=False),
            bool(trip.get("hasFares", False)),
        ))

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO \"EgRailway\".\"trips\"
            (train_number,
             from_station_id, to_station_id,
             departure_ar, departure_en, arrival_ar, arrival_en,
             duration_ar,  duration_en,  stops_count, fares, has_fares)
        VALUES %s
        RETURNING id
        """,
        trip_rows,
        page_size=100,
    )
    inserted_ids = [row[0] for row in cur.fetchall()]

    for trip_id, trip in zip(inserted_ids, trips):
        for order, stop in enumerate(trip.get("stops", []), start=1):
            s_ar  = stop["station_ar"].strip()
            s_id  = station_map.get(s_ar)
            if s_id is None:
                unmatched.add(s_ar)
            stop_rows.append((
                trip_id, order, s_id,
                stop.get("time_ar", ""),
                stop.get("time_en", ""),
            ))

    if stop_rows:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO \"EgRailway\".\"trip_stops\"
                (trip_id, stop_order, station_id,
                 time_ar, time_en)
            VALUES %s
            """,
            stop_rows,
            page_size=500,
        )

    if unmatched:
        print(f"  ⚠  {len(unmatched)} stop names not found in stations table:")
        for name in sorted(unmatched):
            print(f"       - {name}")

    return len(inserted_ids), len(stop_rows)


TARGETS = {"stations", "trains", "trips"}


def main() -> None:
    targets = set(sys.argv[1:]) & TARGETS or TARGETS  # default: all

    print(f"Connecting to {DB_PARAMS['host']} / {DB_PARAMS['dbname']} ...")
    try:
        conn = psycopg2.connect(**DB_PARAMS)
    except psycopg2.OperationalError as e:
        print(f"\n❌ Connection failed: {e}")
        print("\n💡 Tip: If you see a DNS error, open Supabase Dashboard →")
        print("   Project Settings → Database → Connection pooling")
        print("   and copy the Session mode URL into DATABASE_URL in .env")
        sys.exit(1)

    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            if "stations" in targets:
                print("Seeding stations ...")
                n = seed_stations(cur)
                print(f"  ✔  {n} stations inserted")

            if "trains" in targets:
                print("Seeding trains ...")
                n = seed_trains(cur)
                print(f"  ✔  {n} trains inserted")

            if "trips" in targets:
                print("Seeding trips ...")
                n_trips, n_stops = seed_trips(cur)
                print(f"  ✔  {n_trips} trips + {n_stops} stops inserted")

        conn.commit()
        print("\n✅ Seed completed successfully!")

    except Exception as exc:
        conn.rollback()
        print(f"\n❌ Seed failed: {exc}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
