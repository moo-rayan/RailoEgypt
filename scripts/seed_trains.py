#!/usr/bin/env python3
"""
Seed script – clear old trains / trips / trip_stops and reload from JSON.

Data sources
------------
  Trains_update.json      → EgRailway.trains   (basic catalog)
  train_stops_data.json   → EgRailway.trips     (one trip per train)
                          → EgRailway.trip_stops (individual stops)

Delete order (FK-safe): trip_stops → trips → trains
Station rows are NEVER touched (they hold lat/lon coordinates).

Usage (from project root)
-------------------------
  cd backend
  python scripts/seed_trains.py
  # or
  python -m scripts.seed_trains
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# ── ensure project root is importable ────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent          # …/backend/
sys.path.insert(0, str(ROOT))

from sqlalchemy import text                            # noqa: E402
from sqlalchemy.ext.asyncio import (                  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings                   # noqa: E402

# ── JSON file paths ───────────────────────────────────────────────────────────
_ASSETS = ROOT.parent / "trainLiveApp" / "assets" / "data"
TRAINS_JSON = _ASSETS / "Trains_update.json"
STOPS_JSON  = _ASSETS / "train_stops_data.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _pick_time(departure: dict, arrival: dict, lang: str) -> str:
    """Return departure time when available, otherwise arrival time."""
    dep = (departure.get(lang) or "").strip()
    arr = (arrival.get(lang)   or "").strip()
    if dep and dep != "-":
        return dep
    if arr and arr != "-":
        return arr
    return ""


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    db_url = settings.database_url.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    ).replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://", 1
    )
    engine  = create_async_engine(db_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:

        # ── 1. Build station lookup: name → id ───────────────────────────
        log.info("Loading station lookup table …")
        rows = (
            await db.execute(
                text(
                    'SELECT id, name_ar, name_en '
                    'FROM "EgRailway".stations '
                    'WHERE is_active = true'
                )
            )
        ).fetchall()

        by_ar: dict[str, int] = {}
        by_en: dict[str, int] = {}
        for sid, name_ar, name_en in rows:
            if name_ar:
                by_ar[name_ar.strip()] = sid
            if name_en:
                by_en[name_en.strip()] = sid
        log.info("  %d active stations indexed", len(rows))

        def lookup(ar: str, en: str) -> int | None:
            return by_ar.get(ar.strip()) or by_en.get(en.strip())

        # ── 2. Delete old data (FK order) ────────────────────────────────
        log.info("Deleting old data …")
        await db.execute(text('DELETE FROM "EgRailway".trip_stops'))
        await db.execute(text('DELETE FROM "EgRailway".trips'))
        await db.execute(text('DELETE FROM "EgRailway".trains'))
        # Reset auto-increment sequences so IDs start fresh
        for seq in ("trains", "trips", "trip_stops"):
            await db.execute(
                text(f'ALTER SEQUENCE "EgRailway".{seq}_id_seq RESTART WITH 1')
            )
        await db.commit()
        log.info("  Old rows cleared and sequences reset")

        # ── 3. Insert trains from Trains_update.json ─────────────────────
        log.info("Inserting trains …")
        trains_data = json.loads(TRAINS_JSON.read_text(encoding="utf-8"))

        train_rows = [
            {
                "train_id":         t["train_id"],
                "type_ar":          t["type_ar"],
                "type_en":          t["type_en"],
                "start_station_ar": t["start_station_ar"],
                "start_station_en": t["start_station_en"],
                "end_station_ar":   t["end_station_ar"],
                "end_station_en":   t["end_station_en"],
                "stops_count":      int(t.get("stops_count", 0)),
                "note_ar":          t.get("tNoteAr", "") or "",
                "note_en":          t.get("tNoteEn", "") or "",
            }
            for t in trains_data["trains"]
        ]

        await db.execute(
            text(
                """
                INSERT INTO "EgRailway".trains
                  (train_id, type_ar, type_en,
                   start_station_ar, start_station_en,
                   end_station_ar,   end_station_en,
                   stops_count, note_ar, note_en)
                VALUES
                  (:train_id, :type_ar, :type_en,
                   :start_station_ar, :start_station_en,
                   :end_station_ar,   :end_station_en,
                   :stops_count, :note_ar, :note_en)
                """
            ),
            train_rows,
        )
        await db.commit()
        log.info("  %d trains inserted", len(train_rows))

        # ── 4. Insert trips + trip_stops from train_stops_data.json ──────
        log.info("Inserting trips and stops …")
        stops_data = json.loads(STOPS_JSON.read_text(encoding="utf-8"))

        trips_ok   = 0
        stops_ok   = 0
        misses: set[str] = set()

        for t in stops_data["trains"]:
            train_id    = t["train_id"]
            type_ar     = t["type"]["ar"]
            type_en     = t["type"]["en"]
            from_ar     = t["start_station"]["ar"]
            from_en     = t["start_station"]["en"]
            to_ar       = t["end_station"]["ar"]
            to_en       = t["end_station"]["en"]
            duration_ar = (t.get("duration") or {}).get("ar", "")
            duration_en = (t.get("duration") or {}).get("en", "")
            stops_count = int(t.get("stops_count", 0))
            stops_list  = t.get("stops", [])

            # Departure = first stop's departure; arrival = last stop's arrival
            departure_ar = departure_en = arrival_ar = arrival_en = ""
            if stops_list:
                first = stops_list[0]
                last  = stops_list[-1]
                dep   = first.get("departure_time", {})
                arr   = last.get("arrival_time",  {})
                departure_ar = dep.get("ar", "") if dep.get("ar", "-") != "-" else ""
                departure_en = dep.get("en", "") if dep.get("en", "-") != "-" else ""
                arrival_ar   = arr.get("ar", "") if arr.get("ar", "-") != "-" else ""
                arrival_en   = arr.get("en", "") if arr.get("en", "-") != "-" else ""

            from_id = lookup(from_ar, from_en)
            to_id   = lookup(to_ar,   to_en)

            if from_id is None:
                misses.add(from_ar)
            if to_id is None:
                misses.add(to_ar)

            # Insert trip row and retrieve generated id
            result = await db.execute(
                text(
                    """
                    INSERT INTO "EgRailway".trips
                      (train_number,
                       from_station_id, to_station_id,
                       departure_ar, departure_en, arrival_ar, arrival_en,
                       duration_ar,  duration_en,  stops_count)
                    VALUES
                      (:train_number,
                       :from_id, :to_id,
                       :departure_ar, :departure_en, :arrival_ar, :arrival_en,
                       :duration_ar,  :duration_en,  :stops_count)
                    RETURNING id
                    """
                ),
                {
                    "train_number": train_id,
                    "from_id":      from_id,      "to_id":        to_id,
                    "departure_ar": departure_ar, "departure_en": departure_en,
                    "arrival_ar":   arrival_ar,   "arrival_en":   arrival_en,
                    "duration_ar":  duration_ar,  "duration_en":  duration_en,
                    "stops_count":  stops_count,
                },
            )
            trip_id = result.scalar_one()
            trips_ok += 1

            # Insert all stops for this trip
            stop_rows = []
            for s in stops_list:
                s_ar  = s["station_name"]["ar"]
                s_en  = s["station_name"]["en"]
                s_id  = lookup(s_ar, s_en)
                if s_id is None:
                    misses.add(s_ar)

                dep = s.get("departure_time", {})
                arr = s.get("arrival_time",  {})
                stop_rows.append(
                    {
                        "trip_id":    trip_id,
                        "stop_order": int(s["station_order"]),
                        "station_id": s_id,
                        "time_ar":    _pick_time(dep, arr, "ar"),
                        "time_en":    _pick_time(dep, arr, "en"),
                    }
                )

            if stop_rows:
                await db.execute(
                    text(
                        """
                        INSERT INTO "EgRailway".trip_stops
                          (trip_id, stop_order, station_id,
                           time_ar, time_en)
                        VALUES
                          (:trip_id, :stop_order, :station_id,
                           :time_ar, :time_en)
                        """
                    ),
                    stop_rows,
                )
                stops_ok += len(stop_rows)

        await db.commit()
        log.info("  %d trips inserted",       trips_ok)
        log.info("  %d trip_stops inserted",  stops_ok)

        if misses:
            log.warning(
                "  %d station name(s) had no match in stations table "
                "(station_id will be NULL for those stops):",
                len(misses),
            )
            for name in sorted(misses):
                log.warning("    • %s", name)

        # ── 5. Invalidate Redis cache keys affected by new IDs ────────────
        try:
            from app.core.cache import get_redis   # noqa: E402
            r = await get_redis()
            deleted = 0
            for pattern in ("railway:route:*", "railway:stations:*"):
                cursor = 0
                while True:
                    cursor, keys = await r.scan(cursor, match=pattern, count=200)
                    if keys:
                        await r.delete(*keys)
                        deleted += len(keys)
                    if cursor == 0:
                        break
            if deleted:
                log.info("  %d Redis cache key(s) invalidated", deleted)
        except Exception as exc:
            log.warning("  Redis invalidation skipped: %s", exc)

    await engine.dispose()
    log.info("Seed complete ✓")


if __name__ == "__main__":
    asyncio.run(main())
