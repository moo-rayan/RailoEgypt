import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.endpoints import data_bundle
from app.models.train_seat_layout import TrainSeatLayout


def layout_row(train_number="185", class_code="BUFFET", seats=12):
    name_ar = "عربة البوفيه" if class_code == "BUFFET" else "ثانية مكيفة"
    name_en = "Buffet car" if class_code == "BUFFET" else "AC Second"
    layout = data_bundle._build_manual_seat_layout(
        train_number=train_number, class_code=class_code,
        class_name_ar=name_ar, class_name_en=name_en,
        coach_count=1, seats_per_coach=seats,
    )
    return TrainSeatLayout(
        train_number=train_number, class_code=class_code,
        class_name_ar=name_ar, class_name_en=name_en,
        coach_count=1, seat_count=seats,
        window_seat_count=layout["window_seat_count"],
        aisle_seat_count=layout["aisle_seat_count"],
        layout=layout, layout_hash=data_bundle._seat_layout_hash(layout),
    )


def single_result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


class BuffetLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_preserves_type_small_count_and_only_target_train(self):
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[single_result(object()), single_result(None)]),
            add=Mock(), commit=AsyncMock(), refresh=AsyncMock(),
        )

        async def refresh(row):
            row.id = 123

        db.refresh.side_effect = refresh
        payload = data_bundle.SeatLayoutAdminCreateRequest(
            train_number="185", class_code="BUFFET", class_name_ar="عربة البوفيه",
            class_name_en="Buffet car", coach_count=1, seats_per_coach=12,
        )
        with patch.object(data_bundle, "_build_seat_layouts_version_info", new=AsyncMock(return_value={"version": "new"})):
            result = await data_bundle.create_admin_seat_layout(payload, db)

        db.add.assert_called_once()
        row = db.add.call_args.args[0]
        self.assertEqual(row.train_number, "185")
        self.assertEqual(row.class_code, "BUFFET")
        self.assertEqual(row.class_name_ar, "عربة البوفيه")
        self.assertEqual(row.seat_count, 12)
        self.assertEqual(result["layout"]["layout"]["class"]["code"], "BUFFET")
        self.assertEqual(len(row.layout["coaches"][0]["seats"]), 12)
        db.commit.assert_awaited_once()

    async def test_existing_buffet_is_not_overwritten_or_duplicated(self):
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[single_result(object()), single_result(layout_row())]),
            add=Mock(), commit=AsyncMock(),
        )
        payload = data_bundle.SeatLayoutAdminCreateRequest(
            train_number="185", class_code="BUFFET", class_name_ar="عربة البوفيه",
        )
        with self.assertRaises(HTTPException) as caught:
            await data_bundle.create_admin_seat_layout(payload, db)
        self.assertEqual(caught.exception.status_code, 409)
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_unknown_train_cannot_receive_a_layout(self):
        db = SimpleNamespace(execute=AsyncMock(return_value=single_result(None)), add=Mock())
        payload = data_bundle.SeatLayoutAdminCreateRequest(
            train_number="missing", class_code="BUFFET", class_name_ar="عربة البوفيه",
        )
        with self.assertRaises(HTTPException) as caught:
            await data_bundle.create_admin_seat_layout(payload, db)
        self.assertEqual(caught.exception.status_code, 404)
        db.add.assert_not_called()

    async def test_offline_payload_keeps_buffet_independent_and_changes_version(self):
        rows = [layout_row(class_code="AC 2", seats=48), layout_row(train_number="110", class_code="AC 2", seats=48)]
        db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
        )))
        before = await data_bundle._build_seat_layouts_payload(db)
        rows.append(layout_row())
        after = await data_bundle._build_seat_layouts_payload(db)
        self.assertNotEqual(before["version"], after["version"])
        self.assertEqual([layout["c"] for layout in after["layouts"]["185"]], ["AC 2", "BUFFET"])
        self.assertEqual([layout["c"] for layout in after["layouts"]["110"]], ["AC 2"])
        buffet = after["layouts"]["185"][1]
        self.assertEqual((buffet["a"], buffet["e"], buffet["sc"]), ("عربة البوفيه", "Buffet car", 12))
        self.assertEqual([seat[0] for seat in buffet["ch"][0]["s"]], [str(n) for n in range(1, 13)])
        self.assertEqual([seat[5] for seat in buffet["ch"][0]["s"]], [0] * 4 + [1] * 4 + [0] * 4)

    def test_invalid_seat_counts_rejected_by_api_schema(self):
        for count in (0, -1, 121, 2.5):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                data_bundle.SeatLayoutAdminCreateRequest(
                    train_number="185", class_code="BUFFET", class_name_ar="عربة البوفيه", seats_per_coach=count,
                )

    def test_creation_still_requires_admin(self):
        route = next(route for route in data_bundle.router.routes if route.endpoint is data_bundle.create_admin_seat_layout)
        self.assertIn(data_bundle.require_admin, [dependency.call for dependency in route.dependant.dependencies])
