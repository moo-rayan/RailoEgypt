import copy
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.v1.endpoints.seat_availability import router
from app.core.database import get_db
from app.core.security import require_authenticated_user
from app.services.seat_availability_service import (
    booking_url_for_route, earlier_boarding_stations, get_seat_availability,
    normalize_availability, route_station_ids, validate_departure_date,
)
from app.services.train_seat_layout_importer import CAIRO_TZ


def fixture():
    return [{"steps": [{
        "train": {"name": "833", "servicePoints": [{
            "id": "coach-11", "name": "11", "type": "COACH", "cost": 15000,
            "params": {"seats_count": "3", "seatCount": "2"},
            "coachClass": {"params": {"code": "GA 2", "ar": "Third fan", "en": "GA 2"}},
            "availableSeats": ["seat-1", "seat-3"],
            "places": [
                {"id": "seat-1", "number": "1", "topLeft": {"x": 20, "y": 0}, "available": False},
                {"id": "seat-2", "number": "2", "topLeft": {"x": 20, "y": 80}, "available": True},
                {"id": "seat-3", "number": "3", "topLeft": {"x": 70, "y": 0}, "available": True},
            ],
        }]},
        "fromId": 100, "toId": 200, "currency": "EGP",
    }]}]


def parse(payload):
    return normalize_availability(payload, train_number="833", from_id="100", to_id="200")


class AvailabilityParsingTests(unittest.TestCase):
    def test_coaches_sort_by_number_not_source_order_or_lexicographically(self):
        payload = fixture()
        raw = payload[0]["steps"][0]["train"]["servicePoints"][0]
        payload[0]["steps"][0]["train"]["servicePoints"] = [
            dict(copy.deepcopy(raw), id=f"coach-{name}", name=name)
            for name in ["12", "3", "9", "1", "10"]
        ]
        self.assertEqual([coach["name"] for coach in parse(payload)], ["1", "3", "9", "10", "12"])

    def test_earlier_stations_follow_stop_order_nearest_first_only(self):
        trip = SimpleNamespace(from_station_id=1, to_station_id=5, stops=[
            SimpleNamespace(station_id=4, stop_order=4),
            SimpleNamespace(station_id=2, stop_order=2),
            SimpleNamespace(station_id=1, stop_order=1),
            SimpleNamespace(station_id=3, stop_order=3),
            SimpleNamespace(station_id=5, stop_order=5),
        ])
        route = route_station_ids(trip)
        self.assertEqual(route, [1, 2, 3, 4, 5])
        stations = {value: SimpleNamespace(id=value, name_ar=str(value), name_en=str(value), enr_station_id=str(value)) for value in route}
        self.assertEqual([station["id"] for station in earlier_boarding_stations(route, 4, stations)], [3, 2, 1])
        self.assertEqual(earlier_boarding_stations(route, 1, stations), [])
        stations[2].enr_station_id = " "
        self.assertEqual([station["id"] for station in earlier_boarding_stations(route, 4, stations)], [3, 1])

    def test_booking_link_uses_the_official_station_keys_and_travel_date(self):
        payload = fixture()
        step = payload[0]["steps"][0]
        step["from"] = {"name": "ASWAN", "shortName": "ENR_819"}
        step["to"] = {"name": "CAIRO", "shortName": "ENR_1"}
        url = booking_url_for_route(payload, train_number="833", from_id="100", to_id="200", departure_date=date(2026, 10, 9))
        self.assertEqual(urlparse(url).hostname, "obs.enr.gov.eg")
        self.assertEqual(parse_qs(urlparse(url).query), {
            "from": ["ENR_819"], "to": ["ENR_1"], "trip": ["oneway"], "departure": ["2026-10-09"],
        })

    def test_booking_link_without_station_names_uses_the_base_page(self):
        url = booking_url_for_route(fixture(), train_number="833", from_id="100", to_id="200", departure_date=date(2026, 10, 9))
        self.assertEqual(urlparse(url).query, "")

    def test_seat_ids_not_numbers_or_counts_define_availability(self):
        coach = parse(fixture())[0]
        self.assertEqual(coach["available_seat_numbers"], ["1", "3"])
        self.assertEqual(coach["available_count"], 2)
        self.assertEqual(coach["unavailable_count"], 1)
        self.assertEqual(coach["price"], 150)
        self.assertTrue(coach["has_seat_map"])
        self.assertEqual(len(coach["layout"]["s"]), 3)

    def test_wrong_train_and_route_never_leak_into_results(self):
        wrong_train = fixture()[0]["steps"][0]
        wrong_train["train"]["name"] = "110"
        wrong_route = fixture()[0]["steps"][0]
        wrong_route["toId"] = 999
        self.assertEqual(parse([{"steps": [wrong_train, wrong_route]}]), [])

    def test_missing_places_does_not_invent_a_map(self):
        payload = fixture()
        payload[0]["steps"][0]["train"]["servicePoints"][0]["places"] = []
        coach = parse(payload)[0]
        self.assertFalse(coach["has_seat_map"])
        self.assertEqual(coach["available_count"], 2)
        self.assertEqual(coach["layout"]["s"], [])

    def test_partial_places_and_unknown_states_do_not_invent_a_map(self):
        payload = fixture()
        raw = payload[0]["steps"][0]["train"]["servicePoints"][0]
        raw["places"].pop()
        self.assertFalse(parse(payload)[0]["has_seat_map"])
        payload = fixture()
        raw = payload[0]["steps"][0]["train"]["servicePoints"][0]
        del raw["availableSeats"]
        for place in raw["places"]:
            del place["available"]
        self.assertFalse(parse(payload)[0]["has_seat_map"])

    def test_locked_or_sold_seat_cannot_be_green(self):
        for flag in ["locked", "sold"]:
            payload = fixture()
            payload[0]["steps"][0]["train"]["servicePoints"][0]["places"][0][flag] = True
            self.assertEqual(parse(payload)[0]["available_seat_numbers"], ["3"])

    def test_unmapped_available_seat_ids_do_not_produce_a_false_map(self):
        payload = fixture()
        payload[0]["steps"][0]["train"]["servicePoints"][0]["availableSeats"].append("unknown-seat")
        self.assertFalse(parse(payload)[0]["has_seat_map"])

    def test_all_reserved_coach_is_kept(self):
        payload = fixture()
        payload[0]["steps"][0]["train"]["servicePoints"][0]["availableSeats"] = []
        coach = parse(payload)[0]
        self.assertTrue(coach["has_seat_map"])
        self.assertEqual(coach["available_count"], 0)
        self.assertEqual(coach["unavailable_count"], 3)

    def test_directions_preserved_and_missing_directions_not_fabricated(self):
        payload = fixture()
        payload[0]["steps"][0]["train"]["servicePoints"][0]["places"][0]["params"] = {"direction": 1}
        seats = parse(payload)[0]["layout"]["s"]
        self.assertEqual(next(row[5] for row in seats if row[0] == "1"), 1)
        self.assertEqual(next(row[5] for row in seats if row[0] == "2"), -1)

    def test_multiple_coaches_and_duplicates(self):
        payload = fixture()
        coaches = payload[0]["steps"][0]["train"]["servicePoints"]
        another = copy.deepcopy(coaches[0])
        another.update(name="12")
        coaches.extend([another, copy.deepcopy(coaches[0])])
        self.assertEqual([coach["name"] for coach in parse(payload)], ["11", "12"])

    def test_date_window_including_month_rollover(self):
        today = date(2026, 10, 31)
        for delta in [0, 1, 15]:
            validate_departure_date(today + timedelta(days=delta), today)
        for delta in [-1, 16]:
            with self.assertRaises(HTTPException) as caught:
                validate_departure_date(today + timedelta(days=delta), today)
            self.assertEqual(caught.exception.status_code, 422)


class AvailabilityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mapped_route_and_query_parameters_with_mock_upstream(self):
        trip = SimpleNamespace(id=508, from_station_id=3, to_station_id=2, stops=[SimpleNamespace(station_id=1, stop_order=1)])
        start = SimpleNamespace(id=1, enr_station_id="100", name_ar="Aswan", name_en="ASWAN")
        finish = SimpleNamespace(id=2, enr_station_id="200", name_ar="Cairo", name_en="CAIRO")
        earlier = SimpleNamespace(id=3, enr_station_id="300", name_ar="Luxor", name_en="LUXOR")
        trip_result = SimpleNamespace(unique=lambda: SimpleNamespace(scalar_one_or_none=lambda: trip))
        stations_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [start, finish, earlier]))
        db = SimpleNamespace(execute=AsyncMock(side_effect=[trip_result, stations_result]), commit=AsyncMock())
        captured = []

        def upstream(request):
            self.assertEqual(db.commit.await_count, 1)
            captured.append(dict(request.url.params))
            return httpx.Response(200, json=fixture())

        original_client = httpx.AsyncClient
        with patch("app.services.seat_availability_service.httpx.AsyncClient", side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs)):
            result = await get_seat_availability(db, train_number="833", from_station_id=1, departure_date=datetime.now(CAIRO_TZ).date())
        self.assertEqual(captured[0]["skip_places_information"], "false")
        self.assertEqual(captured[0]["trainNumber"], "833")
        self.assertEqual(captured[0]["from"], "100")
        self.assertEqual(captured[0]["to"], "200")
        self.assertEqual(result["coaches"][0]["available_count"], 2)
        self.assertEqual(result["trip_id"], 508)
        self.assertEqual(result["earlier_boarding_stations"], [{"id": 3, "name_ar": "Luxor", "name_en": "LUXOR"}])


class AvailabilityEndpointTests(unittest.TestCase):
    def test_endpoint_auth_validation_and_no_store(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            self.assertEqual(client.get("/seat-availability/833", params={"departure_date": "2026-10-09"}).status_code, 422)
        app.dependency_overrides[require_authenticated_user] = lambda: "user-1"
        app.dependency_overrides[get_db] = lambda: object()
        with patch("app.api.v1.endpoints.seat_availability.get_seat_availability", new=AsyncMock(return_value={"coaches": []})), TestClient(app) as client:
            response = client.get("/seat-availability/833", params={"departure_date": "2026-10-09"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/seat-availability/nope", params={"departure_date": "2026-10-09"}).status_code, 422)
            self.assertEqual(client.get("/seat-availability/833", params={"departure_date": "bad"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
