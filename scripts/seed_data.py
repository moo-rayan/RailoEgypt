"""
Seed EgRailway schema with Stations.json and Trains.json data.
Usage: python scripts/seed_data.py
"""
import asyncio
import json
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent / ".env")

DATABASE_URL: str = os.environ["DATABASE_URL"]
STATIONS_FILE = Path(__file__).parent.parent.parent / "trainLiveApp/assets/data/Stations.json"
TRAINS_FILE   = Path(__file__).parent.parent.parent / "trainLiveApp/assets/data/Trains.json"


async def seed_stations(conn: asyncpg.Connection) -> int:
    with open(STATIONS_FILE, encoding="utf-8") as f:
        stations: list[dict] = json.load(f)

    await conn.execute('DELETE FROM "EgRailway"."stations"')

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

    await conn.executemany(
        """
        INSERT INTO "EgRailway"."stations"
            (name_ar, name_en, latitude, longitude, place_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        rows,
    )
    return len(rows)


async def seed_trains(conn: asyncpg.Connection) -> int:
    with open(TRAINS_FILE, encoding="utf-8") as f:
        data: dict = json.load(f)
    trains: list[dict] = data["trains"]

    await conn.execute('DELETE FROM "EgRailway"."trains"')

    rows = [
        (
            int(t["train_id"]),
            t["type_ar"],
            t["type_en"],
            t["start_station_ar"],
            t["start_station_en"],
            t["end_station_ar"],
            t["end_station_en"],
            int(t["stops_count"]),
            t.get("tNoteAr", ""),
            t.get("tNoteEn", ""),
        )
        for t in trains
    ]

    await conn.executemany(
        """
        INSERT INTO "EgRailway"."trains"
            (train_id, type_ar, type_en,
             start_station_ar, start_station_en,
             end_station_ar, end_station_en,
             stops_count, note_ar, note_en)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
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
    )
    return len(rows)


async def main() -> None:
    print(f"Connecting to database...")
    conn = await asyncpg.connect(DATABASE_URL.replace("+asyncpg", ""))

    try:
        async with conn.transaction():
            n_stations = await seed_stations(conn)
            print(f"  ✔  Inserted {n_stations} stations")

            n_trains = await seed_trains(conn)
            print(f"  ✔  Inserted {n_trains} trains")

        print("\n✅ Seed completed successfully!")
    except Exception as e:
        print(f"\n❌ Seed failed: {e}", file=sys.stderr)
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
