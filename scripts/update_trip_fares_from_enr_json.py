"""Update existing trip fares from an ENR extracted fares JSON file.

Default mode is a dry-run. Pass --apply to write changes.
Pass --insert-missing to insert fares that do not already exist, when both
stations are linked and the train exists.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import AsyncSessionFactory  # noqa: E402


CLASS_NAME_MAP = {
    "AC 1": "First Class AC",
    "AC 2": "Second Class AC",
    "AC 3": "Third Class AC",
    "GA 2": "Third Class (Fan)",
    "PRIMUM": "PRIMUM VIP",
    "PRIMUM VIP": "PRIMUM VIP",
    "SLEEP SINGLE": "Single Cabin",
    "SLEEP DOUBLE": "Double Cabin",
}


def _normalize_en(value: str | None) -> str:
    value = (value or "").upper().strip()
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _load_enr_station_name_map(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        return {
            _normalize_en(row["enr_name"]): row["enr_station_id"]
            for row in csv.DictReader(file)
        }


def _load_json_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    records = data.get("fares", [])
    if not isinstance(records, list):
        raise ValueError("Expected `fares` to be a list")
    return records


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json",
        default=str(ROOT / "enr_extracted_fares.json"),
        help="Path to ENR extracted fares JSON.",
    )
    parser.add_argument(
        "--stations-csv",
        default=str(ROOT / "stations_enr_sorted.csv"),
        help="Path to ENR station CSV with enr_station_id,enr_name.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write price updates. Without this flag, the script only reports changes.",
    )
    parser.add_argument(
        "--insert-missing",
        action="store_true",
        help="Insert missing fare rows when train/stations are valid.",
    )
    args = parser.parse_args()

    records = _load_json_records(Path(args.json))
    enr_name_to_id = _load_enr_station_name_map(Path(args.stations_csv))

    async with AsyncSessionFactory() as session:
        station_rows = (
            await session.execute(
                text(
                    'SELECT id, enr_station_id FROM "EgRailway".stations '
                    "WHERE enr_station_id IS NOT NULL AND enr_station_id <> ''"
                )
            )
        ).mappings()
        db_station_by_enr = {
            str(row["enr_station_id"]): int(row["id"])
            for row in station_rows
        }

        fare_rows = (
            await session.execute(
                text(
                    'SELECT id, train_number, from_station_id, to_station_id, '
                    'class_name_en, price FROM "EgRailway".trip_fares'
                )
            )
        ).mappings()
        fare_by_key = {
            (
                str(row["train_number"]),
                int(row["from_station_id"]),
                int(row["to_station_id"]),
                str(row["class_name_en"]),
            ): row
            for row in fare_rows
        }
        train_numbers = {
            str(row["train_id"])
            for row in (
                await session.execute(
                    text('SELECT train_id FROM "EgRailway".trains')
                )
            ).mappings()
        }

        stats: Counter[str] = Counter()
        updates: list[dict[str, Any]] = []
        inserts: list[dict[str, Any]] = []

        for item in records:
            from_enr_id = enr_name_to_id.get(_normalize_en(item.get("from")))
            to_enr_id = enr_name_to_id.get(_normalize_en(item.get("to")))
            from_station_id = db_station_by_enr.get(from_enr_id or "")
            to_station_id = db_station_by_enr.get(to_enr_id or "")
            class_name_en = CLASS_NAME_MAP.get(
                str(item.get("class_en", "")).strip().upper()
            )

            if not from_station_id or not to_station_id:
                stats["missing_station_link"] += 1
                continue
            if not class_name_en:
                stats["unknown_class"] += 1
                continue

            key = (
                str(item["train_number"]),
                from_station_id,
                to_station_id,
                class_name_en,
            )
            existing = fare_by_key.get(key)
            if not existing:
                if str(item["train_number"]) not in train_numbers:
                    stats["missing_train"] += 1
                    continue
                stats["missing_insertable"] += 1
                inserts.append(
                    {
                        "train_number": str(item["train_number"]),
                        "from_station_id": from_station_id,
                        "to_station_id": to_station_id,
                        "class_name_ar": str(item["class_ar"]),
                        "class_name_en": class_name_en,
                        "price": int(item["price"]),
                        "from_station": item["from"],
                        "to_station": item["to"],
                    }
                )
                continue

            old_price = int(existing["price"])
            new_price = int(item["price"])
            if old_price == new_price:
                stats["same_price"] += 1
                continue

            stats["price_updates"] += 1
            updates.append(
                {
                    "fare_id": int(existing["id"]),
                    "old_price": old_price,
                    "new_price": new_price,
                    "train_number": str(item["train_number"]),
                    "from_station": item["from"],
                    "to_station": item["to"],
                    "class_name_en": class_name_en,
                }
            )

        if args.apply and updates:
            await session.execute(
                text(
                    'UPDATE "EgRailway".trip_fares '
                    "SET price = :new_price, updated_at = now() "
                    "WHERE id = :fare_id"
                ),
                updates,
            )
        if args.apply and args.insert_missing and inserts:
            await session.execute(
                text(
                    'INSERT INTO "EgRailway".trip_fares '
                    "(train_number, from_station_id, to_station_id, "
                    "class_name_ar, class_name_en, price) "
                    "VALUES (:train_number, :from_station_id, :to_station_id, "
                    ":class_name_ar, :class_name_en, :price) "
                    "ON CONFLICT (train_number, from_station_id, to_station_id, class_name_en) "
                    "DO UPDATE SET "
                    "class_name_ar = EXCLUDED.class_name_ar, "
                    "price = EXCLUDED.price, "
                    "updated_at = now()"
                ),
                inserts,
            )
        if args.apply and (updates or (args.insert_missing and inserts)):
            await session.commit()

    print(f"mode={'apply' if args.apply else 'dry-run'}")
    print(f"json_records={len(records)}")
    for key in sorted(stats):
        print(f"{key}={stats[key]}")
    if updates:
        print("\nupdate_samples:")
        for row in updates[:20]:
            print(
                f"fare_id={row['fare_id']} "
                f"{row['train_number']} "
                f"{row['from_station']}->{row['to_station']} "
                f"{row['class_name_en']} "
                f"{row['old_price']}->{row['new_price']}"
            )
    if inserts:
        print("\ninsert_samples:")
        for row in inserts[:20]:
            print(
                f"{row['train_number']} "
                f"{row['from_station']}({row['from_station_id']})->"
                f"{row['to_station']}({row['to_station_id']}) "
                f"{row['class_name_en']} {row['price']}"
            )
    if inserts and not args.insert_missing:
        print("\nNote: pass --insert-missing with --apply to insert these rows.")


if __name__ == "__main__":
    asyncio.run(main())
