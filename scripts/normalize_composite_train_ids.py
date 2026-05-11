"""Normalize composite train IDs that block ENR fare imports.

Examples:
    1205-1206 -> 1205
    1208-1209 -> 1208

The script only touches composite IDs whose first part appears in the given fare
JSON files and does not already exist as a standalone train ID.
Default mode is dry-run. Pass --apply to write changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import AsyncSessionFactory  # noqa: E402


def _load_train_numbers(paths: list[Path]) -> Counter[str]:
    numbers: Counter[str] = Counter()
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for item in data.get("fares", []):
            train_number = str(item.get("train_number", "")).strip()
            if train_number:
                numbers[train_number] += 1
    return numbers


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json",
        action="append",
        default=[],
        help="Fare JSON file. Can be passed more than once.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    json_paths = [Path(path) for path in args.json] or [
        ROOT / "enr_extracted_fares.json",
        ROOT / "enr_extracted_fares2.json",
    ]
    fare_train_numbers = _load_train_numbers(json_paths)

    async with AsyncSessionFactory() as session:
        train_rows = (
            await session.execute(
                text(
                    'SELECT * FROM "EgRailway".trains '
                    "ORDER BY train_id"
                )
            )
        ).mappings().all()
        train_ids = {str(row["train_id"]) for row in train_rows}
        composite_rows = [
            row for row in train_rows if "-" in str(row["train_id"])
        ]

        mappings: list[tuple[str, str]] = []
        for row in composite_rows:
            old_id = str(row["train_id"])
            new_id = old_id.split("-", 1)[0]
            if new_id in fare_train_numbers and new_id not in train_ids:
                mappings.append((old_id, new_id))

        print(f"mode={'apply' if args.apply else 'dry-run'}")
        print(f"candidate_mappings={len(mappings)}")
        for old_id, new_id in mappings:
            trips_count = (
                await session.execute(
                    text(
                        'SELECT COUNT(*) FROM "EgRailway".trips '
                        "WHERE train_number = :old_id"
                    ),
                    {"old_id": old_id},
                )
            ).scalar_one()
            fares_count = (
                await session.execute(
                    text(
                        'SELECT COUNT(*) FROM "EgRailway".trip_fares '
                        "WHERE train_number = :old_id"
                    ),
                    {"old_id": old_id},
                )
            ).scalar_one()
            print(
                f"{old_id}->{new_id} "
                f"fare_records_waiting={fare_train_numbers[new_id]} "
                f"trips={trips_count} trip_fares={fares_count}"
            )

        if not args.apply or not mappings:
            return

        for old_id, new_id in mappings:
            # Create the standalone train row first, so FK updates are valid.
            await session.execute(
                text(
                    'INSERT INTO "EgRailway".trains '
                    "(train_id, type_ar, type_en, start_station_ar, "
                    "start_station_en, end_station_ar, end_station_en, "
                    "stops_count, departure_ar, departure_en, arrival_ar, "
                    "arrival_en, note_ar, note_en, is_active, created_at, updated_at) "
                    "SELECT :new_id, type_ar, type_en, start_station_ar, "
                    "start_station_en, end_station_ar, end_station_en, "
                    "stops_count, departure_ar, departure_en, arrival_ar, "
                    "arrival_en, note_ar, note_en, is_active, created_at, now() "
                    'FROM "EgRailway".trains WHERE train_id = :old_id'
                ),
                {"old_id": old_id, "new_id": new_id},
            )
            await session.execute(
                text(
                    'UPDATE "EgRailway".trips '
                    "SET train_number = :new_id "
                    "WHERE train_number = :old_id"
                ),
                {"old_id": old_id, "new_id": new_id},
            )
            await session.execute(
                text(
                    'UPDATE "EgRailway".trip_fares '
                    "SET train_number = :new_id, updated_at = now() "
                    "WHERE train_number = :old_id"
                ),
                {"old_id": old_id, "new_id": new_id},
            )
            await session.execute(
                text(
                    'DELETE FROM "EgRailway".trains '
                    "WHERE train_id = :old_id"
                ),
                {"old_id": old_id},
            )

        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
