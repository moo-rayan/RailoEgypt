"""
In-memory real-time tracking manager.

Each train has its own "room". Contributors push GPS updates every ~30 s.
The manager aggregates positions and broadcasts to listeners.

Design goals:
  - Lightweight: plain dicts + asyncio, no external broker
  - Per-train isolation: rooms are independent
  - Smart aggregation: max route progress when multiple contributors
  - Railway proximity: ≤500 m accept, >500 m warn (3× → disconnect)
  - Train proximity: contributor must be within 1000 m of train position
  - Station detection: determines previous/next station from trip stops
"""

import asyncio
import collections
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.cache import cache_delete, cache_get, cache_set
from app.core.config import settings
from app.services.railway_service import railway_graph, _haversine

logger = logging.getLogger(__name__)

_MAX_RAIL_DISTANCE_M = 500.0   # accept if ≤ 500 m from rail
_MAX_FAR_WARNINGS    = 3       # consecutive far updates before silent disconnect
_UPDATE_COOLDOWN_S   = 25.0    # min seconds between contributor updates
_STALE_TIMEOUT_S     = 120.0   # remove contributor after 120 s silence
_MAX_TRAIN_DISTANCE_M = 1000.0  # contributor must be within this of train
_TRAIN_POS_TTL       = 3600    # Redis TTL for cached train position (seconds)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class StationInfo:
    order: int
    name_ar: str
    name_en: str
    lat: float
    lon: float
    time_ar: str = ""
    time_en: str = ""


@dataclass
class RoomEvent:
    """A single event in the room's event log."""
    timestamp: float
    event_type: str   # join, leave, kick, ban, leader_set, leader_removed, update, far_warning, silent_disconnect
    user_id: str
    detail: str = ""


@dataclass
class Contributor:
    user_id: str
    display_name: str = ""
    avatar_url: str = ""
    lat: float = 0.0
    lng: float = 0.0
    speed: float = 0.0
    bearing: float = 0.0
    last_update: float = 0.0
    far_from_rail_count: int = 0   # consecutive updates too far from railway
    # Contributor's personal trip (boarding → alighting)
    from_station_name: str = ""
    to_station_name: str = ""
    trip_distance_km: float = 0.0
    is_captain: bool = False


@dataclass
class WaitingContributor:
    """A contributor waiting for an active slot."""
    user_id: str
    display_name: str = ""
    avatar_url: str = ""
    from_station_name: str = ""
    to_station_name: str = ""
    trip_distance_km: float = 0.0
    joined_at: float = 0.0
    is_captain: bool = False


@dataclass
class TrainRoom:
    """One tracking session per train."""
    train_id: str
    # Trip metadata (set once when first contributor joins with trip context)
    trip_id: Optional[int] = None
    start_station: str = ""
    end_station: str = ""
    stations: list[StationInfo] = field(default_factory=list)

    # Participants
    contributors: dict[str, Contributor] = field(default_factory=dict)  # user_id → Contributor
    waiting_list: list[WaitingContributor] = field(default_factory=list) # sorted by trip_distance desc
    leader_id: Optional[str] = None   # if set, only this contributor's updates are used
    max_active_contributors: int = field(default_factory=lambda: settings.max_active_contributors)
    # Temporary kick block: user_id → timestamp until which they cannot rejoin
    kicked_until: dict[str, float] = field(default_factory=dict)

    # Event log (ring buffer, last 200 events)
    event_log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=200))
    # Live update feed (ring buffer, last 100 GPS updates for admin monitoring)
    update_feed: collections.deque = field(default_factory=lambda: collections.deque(maxlen=100))

    # Aggregated state
    lat: float = 0.0
    lng: float = 0.0
    speed: float = 0.0
    direction: str = ""
    status: str = "waiting"  # waiting | moving | stopped
    last_broadcast: float = 0.0
    max_progress: float = 0.0   # furthest route progress in metres
    max_lat: float = 0.0        # lat at max_progress (forward-only anchor)
    max_lng: float = 0.0        # lng at max_progress (forward-only anchor)


# ── Manager singleton ────────────────────────────────────────────────────────

class TrackingManager:
    def __init__(self) -> None:
        self._rooms: dict[str, TrainRoom] = {}
        self._user_avatars: dict[str, str] = {}  # user_id → avatar_url
        self._user_display_names: dict[str, str] = {}  # user_id → display_name
        self._user_trip_info: dict[str, dict] = {}  # user_id → {from_station_name, to_station_name}
        self._user_captains: dict[str, bool] = {}  # user_id → is_captain

    def set_user_avatar(self, user_id: str, avatar_url: str) -> None:
        """Store user avatar for later use when contributor joins WS."""
        if avatar_url:
            self._user_avatars[user_id] = avatar_url

    def set_user_display_name(self, user_id: str, display_name: str) -> None:
        """Store user display name for later use when contributor joins WS."""
        if display_name:
            self._user_display_names[user_id] = display_name

    def set_user_trip_info(self, user_id: str, from_station_name: str, to_station_name: str) -> None:
        """Store contributor's personal trip info (boarding → alighting) at ticket time."""
        self._user_trip_info[user_id] = {
            "from_station_name": from_station_name or "",
            "to_station_name": to_station_name or "",
        }

    def set_user_captain(self, user_id: str, is_captain: bool) -> None:
        """Store whether user is a train captain."""
        self._user_captains[user_id] = is_captain

    def _log_event(self, room: TrainRoom, event_type: str, user_id: str, detail: str = "") -> None:
        """Append an event to the room's ring-buffer log."""
        room.event_log.append(RoomEvent(
            timestamp=time.time(),
            event_type=event_type,
            user_id=user_id,
            detail=detail,
        ))

    # ── Room lifecycle ────────────────────────────────────────────────────

    def get_or_create_room(self, train_id: str) -> TrainRoom:
        if train_id not in self._rooms:
            self._rooms[train_id] = TrainRoom(train_id=train_id)
            logger.info("🚂 [%s] Room created", train_id)
        return self._rooms[train_id]

    def get_room(self, train_id: str) -> Optional[TrainRoom]:
        return self._rooms.get(train_id)

    def _cleanup_room(self, train_id: str) -> None:
        room = self._rooms.get(train_id)
        if room and not room.contributors and not room.listeners and not room.waiting_list:
            del self._rooms[train_id]
            logger.info("🗑️  [%s] Room destroyed (empty)", train_id)

    # ── Set trip metadata (stations) ──────────────────────────────────────

    def set_trip_info(
        self,
        train_id: str,
        trip_id: int,
        stations: list[dict],
        start_station: str = "",
        end_station: str = "",
    ) -> None:
        room = self.get_or_create_room(train_id)
        if room.trip_id is not None:
            return  # already set
        room.trip_id = trip_id
        room.start_station = start_station
        room.end_station = end_station
        room.stations = [
            StationInfo(
                order=s["order"],
                name_ar=s["name_ar"],
                name_en=s.get("name_en", ""),
                lat=s["lat"],
                lon=s["lon"],
                time_ar=s.get("time_ar", ""),
                time_en=s.get("time_en", ""),
            )
            for s in stations
        ]
        logger.info(
            "📍 [%s] Trip info set: trip_id=%d, %d stations, %s → %s",
            train_id, trip_id, len(room.stations), start_station, end_station,
        )

    # ── Join / Leave ──────────────────────────────────────────────────────

    @staticmethod
    def _calculate_trip_distance(
        from_name: str, to_name: str, stations: list["StationInfo"],
    ) -> float:
        """
        Calculate route distance (km) between two stations along the trip.
        Uses cumulative haversine between consecutive trip stops.
        Returns 0.0 if stations not found.
        """
        if not from_name or not to_name or len(stations) < 2:
            return 0.0

        # Find station indices by name (case-insensitive strip match)
        from_idx: Optional[int] = None
        to_idx: Optional[int] = None
        for i, s in enumerate(stations):
            if s.name_ar.strip() == from_name.strip():
                from_idx = i
            if s.name_ar.strip() == to_name.strip():
                to_idx = i

        if from_idx is None or to_idx is None or from_idx >= to_idx:
            return 0.0

        # Sum haversine distances segment by segment
        total_m = 0.0
        for i in range(from_idx, to_idx):
            total_m += _haversine(
                stations[i].lon, stations[i].lat,
                stations[i + 1].lon, stations[i + 1].lat,
            )
        return total_m / 1000.0  # metres → km

    async def add_contributor(self, train_id: str, user_id: str) -> dict:
        """
        Register a contributor in a room (HTTP model — no WebSocket).
        Returns status dict:
          {"status": "active"} or {"status": "waiting", "position": N, "total": M}
          {"status": "kicked"}  if temporarily blocked

        Captain priority: a captain ALWAYS gets an active slot.
        """
        room = self.get_or_create_room(train_id)

        # Check temporary kick block
        kick_expiry = room.kicked_until.get(user_id, 0)
        if time.time() < kick_expiry:
            return {"status": "kicked", "message_ar": "تم طردك من الغرفة مؤقتاً"}

        avatar = self._user_avatars.get(user_id, "")
        name = self._user_display_names.get(user_id, "")
        is_captain = self._user_captains.get(user_id, False)
        trip_info = self._user_trip_info.get(user_id, {})
        from_name = trip_info.get("from_station_name", "")
        to_name = trip_info.get("to_station_name", "")

        # Calculate trip distance using room stations
        if from_name and to_name and room.stations:
            distance_km = self._calculate_trip_distance(from_name, to_name, room.stations)
        elif room.stations:
            # No personal stations → full trip = max distance (highest priority)
            distance_km = self._calculate_trip_distance(
                room.stations[0].name_ar, room.stations[-1].name_ar, room.stations,
            )
        else:
            distance_km = 0.0

        has_slot = len(room.contributors) < room.max_active_contributors

        # Captain priority: if room is full, demote lowest-priority non-captain
        if not has_slot and is_captain:
            # Find lowest-priority non-captain contributor
            non_captains = [
                c for c in room.contributors.values() if not c.is_captain
            ]
            if non_captains:
                # Sort by trip_distance ascending → first = shortest trip = lowest priority
                non_captains.sort(key=lambda c: c.trip_distance_km)
                demoted = non_captains[0]
                # Move demoted to waiting list
                waiting_entry = WaitingContributor(
                    user_id=demoted.user_id,
                    display_name=demoted.display_name, avatar_url=demoted.avatar_url,
                    from_station_name=demoted.from_station_name,
                    to_station_name=demoted.to_station_name,
                    trip_distance_km=demoted.trip_distance_km,
                    joined_at=time.time(),
                )
                room.waiting_list.append(waiting_entry)
                room.waiting_list.sort(key=lambda w: w.trip_distance_km, reverse=True)
                del room.contributors[demoted.user_id]
                self._log_event(
                    room, "demoted", demoted.user_id,
                    f"{demoted.display_name or demoted.user_id[:8]} نُقل لقائمة الانتظار لإفساح المجال للكابتن {name or user_id[:8]}",
                )
                logger.info(
                    "⬇️ [%s] Demoted %s to waiting (captain %s joining)",
                    train_id, demoted.user_id[:8], user_id[:8],
                )
                has_slot = True

        # Check if there's room for an active contributor
        if has_slot:
            captain_label = " �Captain" if is_captain else ""
            room.contributors[user_id] = Contributor(
                user_id=user_id, display_name=name, avatar_url=avatar,
                from_station_name=from_name, to_station_name=to_name,
                trip_distance_km=distance_km, is_captain=is_captain,
            )
            self._log_event(
                room, "join", user_id,
                f"{name or user_id[:8]}{captain_label} انضم كمساهم نشط ({from_name}→{to_name}, {distance_km:.1f}km) "
                f"[نشط: {len(room.contributors)}/{room.max_active_contributors}]",
            )
            logger.info(
                "👤+ [%s] Contributor %s joined ACTIVE%s (%.1fkm, %s→%s) [%d/%d]",
                train_id, user_id[:8], captain_label, distance_km, from_name, to_name,
                len(room.contributors), room.max_active_contributors,
            )

            # Fire admin alert (async, fire-and-forget)
            asyncio.ensure_future(self._alert_new_contribution(
                train_id, user_id, name, from_name, to_name,
            ))

            return {"status": "active"}

        # Room full (and not a captain) → add to waiting list
        waiting = WaitingContributor(
            user_id=user_id, display_name=name, avatar_url=avatar,
            from_station_name=from_name, to_station_name=to_name,
            trip_distance_km=distance_km, joined_at=time.time(),
            is_captain=is_captain,
        )
        room.waiting_list.append(waiting)
        # Sort by distance descending (longest trip first = highest priority)
        room.waiting_list.sort(key=lambda w: w.trip_distance_km, reverse=True)
        position = next(i for i, w in enumerate(room.waiting_list) if w.user_id == user_id) + 1
        self._log_event(
            room, "waiting", user_id,
            f"{name or user_id[:8]} في قائمة الانتظار #{position} ({from_name}→{to_name}, {distance_km:.1f}km) "
            f"[انتظار: {len(room.waiting_list)}]",
        )
        logger.info(
            "⏳ [%s] Contributor %s → WAITING #%d (%.1fkm, %s→%s) [waiting: %d]",
            train_id, user_id[:8], position, distance_km, from_name, to_name,
            len(room.waiting_list),
        )
        return {"status": "waiting", "position": position, "total": len(room.waiting_list)}

    async def remove_participant(self, train_id: str, user_id: str, disconnect_reason: str = "") -> None:
        room = self._rooms.get(train_id)
        if not room:
            return
        removed = False
        was_contributor = False
        was_waiting = False
        reason_text = disconnect_reason or "unknown"

        if user_id in room.contributors:
            display = room.contributors[user_id].display_name or user_id[:8]
            del room.contributors[user_id]
            removed = True
            was_contributor = True
            self._log_event(room, "leave", user_id, f"{display} — {reason_text} (remaining: {len(room.contributors)})")
            logger.info("👤- [%s] Contributor left: %s reason=%s (remaining: %d)", train_id, user_id, reason_text, len(room.contributors))

        # Check waiting list
        for i, w in enumerate(room.waiting_list):
            if w.user_id == user_id:
                room.waiting_list.pop(i)
                removed = True
                was_waiting = True
                self._log_event(room, "leave", user_id, f"{w.display_name or user_id[:8]} غادر قائمة الانتظار — {reason_text}")
                logger.info("⏳- [%s] Waiting contributor left: %s (remaining waiting: %d)", train_id, user_id[:8], len(room.waiting_list))
                break

        if removed:
            # If the removed user was the leader, clear leader
            if room.leader_id == user_id:
                room.leader_id = None
                self._log_event(room, "leader_removed", user_id, "leader disconnected, reverting to auto")
                logger.info("👑 [%s] Leader %s disconnected — reverting to auto aggregation", train_id, user_id)

            # If active contributor left and waiting list has candidates → promote
            if was_contributor and room.waiting_list:
                await self._promote_from_waiting_list(room)

            # If no contributors left, set status back to waiting
            if not room.contributors:
                room.status = "waiting"
                room.max_progress = 0.0
                room.max_lat = 0.0
                room.max_lng = 0.0
                await cache_delete(f"train_pos:{train_id}")
                logger.info("⏸️  [%s] No contributors — status → waiting, max_progress reset, Redis cache cleared", train_id)

            self._cleanup_room(train_id)

    async def _promote_from_waiting_list(self, room: TrainRoom) -> None:
        """Promote the highest-priority waiting contributor to active."""
        while room.waiting_list and len(room.contributors) < room.max_active_contributors:
            promoted = room.waiting_list.pop(0)
            name = promoted.display_name or promoted.user_id[:8]

            room.contributors[promoted.user_id] = Contributor(
                user_id=promoted.user_id,
                display_name=promoted.display_name,
                avatar_url=promoted.avatar_url,
                from_station_name=promoted.from_station_name,
                to_station_name=promoted.to_station_name,
                trip_distance_km=promoted.trip_distance_km,
            )

            self._log_event(
                room, "promoted", promoted.user_id,
                f"{name} ترقى من الانتظار إلى مساهم نشط "
                f"({promoted.from_station_name}→{promoted.to_station_name}, {promoted.trip_distance_km:.1f}km) "
                f"[نشط: {len(room.contributors)}/{room.max_active_contributors}, انتظار: {len(room.waiting_list)}]",
            )
            logger.info(
                "⬆️ [%s] Promoted %s from waiting → ACTIVE (%.1fkm) [active: %d/%d, waiting: %d]",
                room.train_id, promoted.user_id[:8], promoted.trip_distance_km,
                len(room.contributors), room.max_active_contributors, len(room.waiting_list),
            )

    # ── Admin actions ──────────────────────────────────────────────────────

    _KICK_BLOCK_SECONDS = 300  # 5 minutes block after kick

    async def kick_contributor(self, train_id: str, user_id: str, reason: str = "") -> bool:
        """Remove a contributor and block them from re-joining for 5 minutes."""
        room = self._rooms.get(train_id)
        if not room or user_id not in room.contributors:
            return False
        self._log_event(room, "kick", user_id, reason or "kicked by admin")
        # Set temporary kick block so next HTTP POST gets rejected
        room.kicked_until[user_id] = time.time() + self._KICK_BLOCK_SECONDS
        await self.remove_participant(train_id, user_id)
        logger.info("🚫 [%s] Contributor %s kicked (blocked %ds): %s",
                    train_id, user_id, self._KICK_BLOCK_SECONDS, reason)
        return True

    def set_leader(self, train_id: str, user_id: str) -> bool:
        """Set a contributor as the leader (only their updates are used)."""
        room = self._rooms.get(train_id)
        if not room or user_id not in room.contributors:
            return False
        room.leader_id = user_id
        self._log_event(room, "leader_set", user_id, "set as leader by admin")
        logger.info("👑 [%s] Leader set: %s", train_id, user_id)
        return True

    def remove_leader(self, train_id: str) -> bool:
        """Remove leader from a room (revert to auto aggregation)."""
        room = self._rooms.get(train_id)
        if not room or room.leader_id is None:
            return False
        old_leader = room.leader_id
        room.leader_id = None
        self._log_event(room, "leader_removed", old_leader, "leader removed by admin")
        logger.info("👑 [%s] Leader removed (was %s)", train_id, old_leader)
        return True

    def get_room_logs(self, train_id: str) -> list[dict]:
        """Get event log for a room."""
        room = self._rooms.get(train_id)
        if not room:
            return []
        return [
            {
                "timestamp": e.timestamp,
                "event_type": e.event_type,
                "user_id": e.user_id,
                "detail": e.detail,
            }
            for e in room.event_log
        ]

    def get_room_feed(self, train_id: str) -> list[dict]:
        """Get live GPS update feed for a room (last 100 updates)."""
        room = self._rooms.get(train_id)
        if not room:
            return []
        return list(room.update_feed)

    # ── Process contributor update ────────────────────────────────────────

    async def process_update(
        self,
        train_id: str,
        user_id: str,
        lat: float,
        lng: float,
        speed: float = 0.0,
        bearing: float = 0.0,
    ) -> dict:
        """
        Process a GPS update from a contributor. Returns a status dict.
        Validates proximity, updates room state, broadcasts to listeners.

        Distance logic:
          ≤ 500 m  → accepted, included in tracking, counter reset
          > 500 m  → skipped, warning sent (up to 3 consecutive)
          3× far   → silent disconnect
        """
        room = self._rooms.get(train_id)
        if not room:
            return {"ok": False, "error": "room_not_found"}

        contributor = room.contributors.get(user_id)
        if not contributor:
            # Check if user is in waiting list
            for i, w in enumerate(room.waiting_list):
                if w.user_id == user_id:
                    return {
                        "ok": False,
                        "error": "in_waiting_list",
                        "position": i + 1,
                        "total_waiting": len(room.waiting_list),
                        "message_ar": f"أنت في قائمة الانتظار (#{i + 1} من {len(room.waiting_list)}). سيتم ترقيتك تلقائياً.",
                        "message_en": f"You are in waiting list (#{i + 1} of {len(room.waiting_list)}). You will be promoted automatically.",
                    }
            return {"ok": False, "error": "not_a_contributor"}

        # Rate limiting
        now = time.time()
        if now - contributor.last_update < _UPDATE_COOLDOWN_S:
            wait = _UPDATE_COOLDOWN_S - (now - contributor.last_update)
            return {"ok": False, "error": "rate_limited", "wait_seconds": round(wait, 1)}

        contributor.last_update = now

        # Railway proximity check
        distance = self._distance_to_rail(lng, lat)

        # If railway graph is not ready or snap failed → accept the update
        # (don't penalize user for infrastructure issues)
        if distance is None:
            logger.debug(
                "ℹ️ [%s] User %s: railway graph unavailable, accepting update",
                train_id, user_id,
            )
            # Don't increment far_from_rail_count, just proceed
        elif distance > _MAX_RAIL_DISTANCE_M:
            # GeoJSON says far — but check trip-route fallback first
            # (covers GeoJSON data gaps using DB station coordinates)
            route_dist = self._distance_to_trip_route(lng, lat, room.stations)
            if route_dist is not None and route_dist <= _MAX_RAIL_DISTANCE_M:
                # Close to trip route → accept, GeoJSON just has a gap here
                logger.info(
                    "✅ [%s] User %s: far from GeoJSON rail (%.0fm) but "
                    "close to trip route (%.0fm) — accepted (GeoJSON gap)",
                    train_id, user_id, distance, route_dist,
                )
                contributor.far_from_rail_count = 0
                distance = route_dist  # use the closer distance for logging
            else:
                # Actually far from both sources → warn
                contributor.far_from_rail_count += 1
                dist_str = f"{distance:.0f}m"
                logger.warning(
                    "⚠️ [%s] User %s far from rail: %s (count=%d/%d)",
                    train_id, user_id, dist_str,
                    contributor.far_from_rail_count, _MAX_FAR_WARNINGS,
                )
                # After 3 consecutive far updates → silent disconnect
                if contributor.far_from_rail_count >= _MAX_FAR_WARNINGS:
                    self._log_event(room, "silent_disconnect", user_id, f"فصل تلقائي — بعيد عن السكة {_MAX_FAR_WARNINGS} مرات متتالية ({dist_str})")
                    logger.info(
                        "🚫 [%s] User %s exceeded %d far updates → silent disconnect",
                        train_id, user_id, _MAX_FAR_WARNINGS,
                    )
                    return {
                        "ok": False,
                        "error": "silent_disconnect",
                        "distance_m": distance,
                    }
                self._log_event(room, "far_warning", user_id, f"بعيد عن السكة {dist_str} ({contributor.far_from_rail_count}/{_MAX_FAR_WARNINGS})")
                room.update_feed.append({
                    "ts": now, "user_id": user_id,
                    "display_name": contributor.display_name or user_id[:8],
                    "type": "far_warning", "lat": lat, "lng": lng,
                    "speed": speed, "distance_m": distance,
                    "detail": f"بعيد عن السكة {dist_str} ({contributor.far_from_rail_count}/{_MAX_FAR_WARNINGS})",
                })
                return {
                    "ok": False,
                    "error": "too_far_warning",
                    "distance_m": distance,
                    "remaining": _MAX_FAR_WARNINGS - contributor.far_from_rail_count,
                    "message_ar": f"أنت بعيد عن السكة ({dist_str}). موقعك لا يُحتسب.",
                    "message_en": f"You are {dist_str} from the railway. Position not counted.",
                }
        else:
            # ≤ 500 m → accepted, reset warning counter
            contributor.far_from_rail_count = 0

        # ── Train proximity check (1000 m from current train position) ────
        train_lat, train_lng = room.lat, room.lng
        if train_lat == 0.0 and train_lng == 0.0:
            cached = await cache_get(f"train_pos:{train_id}")
            if cached:
                train_lat = cached.get("lat", 0.0)
                train_lng = cached.get("lng", 0.0)
        if train_lat != 0.0 or train_lng != 0.0:
            dist_to_train = _haversine(lng, lat, train_lng, train_lat)
            if dist_to_train > _MAX_TRAIN_DISTANCE_M:
                self._log_event(room, "silent_disconnect", user_id, f"فصل تلقائي — بعيد عن القطار {dist_to_train:.0f}م")
                logger.warning(
                    "🚫 [%s] User %s too far from train: %.0fm → silent disconnect",
                    train_id, user_id, dist_to_train,
                )
                return {
                    "ok": False,
                    "error": "silent_disconnect",
                    "distance_m": dist_to_train,
                    "message_ar": f"موقعك بعيد عن القطار ({dist_to_train:.0f}م).",
                }

        # Update contributor data
        contributor.lat = lat
        contributor.lng = lng
        contributor.speed = speed
        contributor.bearing = bearing

        # Log to update feed for admin monitoring
        room.update_feed.append({
            "ts": now, "user_id": user_id,
            "display_name": contributor.display_name or user_id[:8],
            "type": "gps", "lat": lat, "lng": lng,
            "speed": speed, "bearing": bearing,
            "distance_m": distance,
        })

        logger.info(
            "📡 [%s] Update from %s: (%.6f, %.6f) speed=%.1f dist_to_rail=%s",
            train_id, user_id, lat, lng, speed,
            f"{distance:.0f}m" if distance is not None else "N/A",
        )

        # Aggregate all active contributors (picks best candidate)
        self._aggregate_position(room)

        # Snap aggregated position to nearest railway segment
        self._snap_position_to_rail(room)

        # Forward-only: compare snapped position with stored max.
        # This runs AFTER snapping so we compare final positions.
        if len(room.stations) >= 2:
            current_progress = self._compute_route_progress(
                room.lat, room.lng, room.stations,
            )
            if current_progress >= room.max_progress:
                room.max_progress = current_progress
                room.max_lat = room.lat
                room.max_lng = room.lng
            else:
                # Position behind last max → keep broadcasting max position
                room.lat = room.max_lat
                room.lng = room.max_lng
                logger.info(
                    "⏩ [%s] Forward-only: keeping max position "
                    "(current=%.1fm < max=%.1fm)",
                    train_id, current_progress, room.max_progress,
                )
        else:
            logger.warning(
                "⚠️ [%s] No station data (%d stations) — forward-only unavailable",
                train_id, len(room.stations),
            )

        # Determine direction from bearing
        room.direction = self._bearing_to_direction(bearing)

        # Update status
        room.status = "moving" if speed > 2.0 else "stopped"

        # Cache full position data in Redis for HTTP polling
        if room.lat != 0.0 or room.lng != 0.0:
            await cache_set(
                f"train_pos:{room.train_id}",
                self.get_position_data(room),
                ttl=_TRAIN_POS_TTL,
            )

        room.last_broadcast = now

        return {"ok": True, "contributors": len(room.contributors)}

    # ── Railway proximity ─────────────────────────────────────────────────

    @staticmethod
    def _distance_to_rail(lon: float, lat: float) -> Optional[float]:
        """Return distance in metres to the nearest railway segment, or None."""
        if not railway_graph.is_built:
            return None
        result = railway_graph.snap_to_rail(lon, lat, search_radius=3)
        if result is None:
            return None
        return result[2]   # distance_m

    @staticmethod
    def _distance_to_trip_route(
        lon: float, lat: float, stations: list["StationInfo"],
    ) -> Optional[float]:
        """
        Fallback: distance from (lon, lat) to the nearest segment between
        consecutive trip stations.  Returns metres, or None if < 2 stations.
        Covers GeoJSON gaps by using the actual DB trip route.
        """
        if len(stations) < 2:
            return None
        best = float("inf")
        for i in range(len(stations) - 1):
            s1, s2 = stations[i], stations[i + 1]
            dx = s2.lon - s1.lon
            dy = s2.lat - s1.lat
            denom = dx * dx + dy * dy
            t = 0.0 if denom < 1e-14 else (
                ((lon - s1.lon) * dx + (lat - s1.lat) * dy) / denom
            )
            t = max(0.0, min(1.0, t))
            proj_lon = s1.lon + t * dx
            proj_lat = s1.lat + t * dy
            d = _haversine(lon, lat, proj_lon, proj_lat)
            if d < best:
                best = d
        return best

    @staticmethod
    def _snap_position_to_rail(room: "TrainRoom") -> None:
        """Snap the room's aggregated position to the nearest railway segment."""
        if not railway_graph.is_built or room.lat == 0.0:
            return
        result = railway_graph.snap_to_rail(room.lng, room.lat)
        if result is None:
            return
        snapped_lon, snapped_lat, dist = result
        if dist < 600:  # only snap if reasonably close
            room.lat = snapped_lat
            room.lng = snapped_lon

    # ── Route progress calculation ────────────────────────────────────────

    @staticmethod
    def _compute_route_progress(
        lat: float, lng: float, stations: list["StationInfo"],
    ) -> float:
        """
        Compute how far (metres) a point is along the station-to-station route.
        Projects the point onto each station segment and returns the cumulative
        distance from the first station to the closest projection.
        """
        if len(stations) < 2:
            return 0.0

        best_progress = 0.0
        best_dist = float("inf")
        cumulative = 0.0

        for i in range(len(stations) - 1):
            s1, s2 = stations[i], stations[i + 1]
            seg_len = _haversine(s1.lon, s1.lat, s2.lon, s2.lat)

            dx = s2.lon - s1.lon
            dy = s2.lat - s1.lat
            denom = dx * dx + dy * dy
            t = 0.0 if denom < 1e-14 else (
                ((lng - s1.lon) * dx + (lat - s1.lat) * dy) / denom
            )
            t = max(0.0, min(1.0, t))

            proj_lat = s1.lat + t * dy
            proj_lon = s1.lon + t * dx
            dist = _haversine(lng, lat, proj_lon, proj_lat)

            if dist < best_dist:
                best_dist = dist
                best_progress = cumulative + t * seg_len

            cumulative += seg_len

        return best_progress

    # ── Position aggregation ──────────────────────────────────────────────

    @staticmethod
    def _aggregate_position(room: TrainRoom) -> None:
        """Pick the best contributor and update room position.

        Leader mode: if leader_id is set and leader is active, use their position.
        With stations: picks contributor with maximum route progress.
        Without stations: picks the most recently updated contributor.

        NOTE: forward-only enforcement happens in process_update() AFTER
        snapping, not here.  This method just selects the best candidate.
        """
        now = time.time()
        active = [
            c for c in room.contributors.values()
            if c.last_update > 0
            and (now - c.last_update) < _STALE_TIMEOUT_S
            and (c.lat != 0.0 or c.lng != 0.0)
        ]
        if not active:
            return

        # Leader mode: use leader's position exclusively if they are active
        if room.leader_id:
            leader = next((c for c in active if c.user_id == room.leader_id), None)
            if leader:
                room.lat = leader.lat
                room.lng = leader.lng
                room.speed = leader.speed
                return
            # Leader is stale/inactive — fall through to normal aggregation

        if len(room.stations) < 2:
            # No route data → use the most recent contributor
            c = max(active, key=lambda x: x.last_update)
            room.lat = c.lat
            room.lng = c.lng
            room.speed = c.speed
            return

        # Find contributor with maximum route progress (works for 1 or N)
        best_c = active[0]
        best_progress = -1.0

        for c in active:
            progress = TrackingManager._compute_route_progress(
                c.lat, c.lng, room.stations,
            )
            if progress > best_progress:
                best_progress = progress
                best_c = c

        room.lat = best_c.lat
        room.lng = best_c.lng
        room.speed = best_c.speed

    # ── Direction helper ──────────────────────────────────────────────────

    @staticmethod
    def _bearing_to_direction(bearing: float) -> str:
        dirs = ["north", "northeast", "east", "southeast",
                "south", "southwest", "west", "northwest"]
        idx = int(((bearing + 22.5) % 360) / 45)
        return dirs[idx]

    # ── Broadcasting ──────────────────────────────────────────────────────

    @staticmethod
    def _active_contributor_count(room: TrainRoom) -> int:
        """Count contributors who have at least one accepted position."""
        return sum(
            1 for c in room.contributors.values()
            if c.lat != 0.0 or c.lng != 0.0
        )

    def _top_contributor_infos(self, room: TrainRoom, limit: int = 3) -> list[dict]:
        """Return compact info dicts for the top N active contributors (captain first).

        Keys: a=avatar_url, n=display_name, cap=is_captain (only if True).
        """
        sorted_contribs = sorted(
            room.contributors.values(),
            key=lambda c: (not c.is_captain, -c.trip_distance_km),
        )
        infos: list[dict] = []
        for c in sorted_contribs:
            if c.avatar_url:
                entry: dict = {"a": c.avatar_url, "n": c.display_name}
                if c.is_captain:
                    entry["cap"] = True
                infos.append(entry)
                if len(infos) >= limit:
                    break
        return infos

    async def cleanup_stale_contributors(self) -> int:
        """Remove contributors whose last update is older than _STALE_TIMEOUT_S.

        Called by a background task every 60 seconds.  Returns the number of
        contributors removed across all rooms.
        """
        now = time.time()
        removed_count = 0
        for train_id in list(self._rooms.keys()):
            room = self._rooms.get(train_id)
            if not room:
                continue
            stale = [
                uid for uid, c in room.contributors.items()
                if c.last_update > 0 and (now - c.last_update) > _STALE_TIMEOUT_S
            ]
            for uid in stale:
                display = room.contributors[uid].display_name or uid[:8]
                del room.contributors[uid]
                removed_count += 1
                self._log_event(room, "leave", uid, f"{display} محذوف تلقائياً (انقطع عن الإرسال)")
                logger.info("🧹 [%s] Stale contributor removed: %s", train_id, uid[:8])
                # Promote waiting contributors if a slot freed up
                if room.waiting_list:
                    await self._promote_from_waiting_list(room)
            if stale:
                if not room.contributors:
                    room.status = "waiting"
                    room.max_progress = 0.0
                    room.max_lat = 0.0
                    room.max_lng = 0.0
                    await cache_delete(f"train_pos:{train_id}")
                self._cleanup_room(train_id)
        return removed_count

    # ── Stats ───────────────────────────────────────────────────────────────────

    @property
    def active_rooms(self) -> int:
        return len(self._rooms)

    def get_position_data(self, room: "TrainRoom") -> dict:
        """Return compact position dict suitable for HTTP responses."""
        return {
            "tid": room.train_id,
            "la": round(room.lat, 6),
            "ln": round(room.lng, 6),
            "sp": round(room.speed, 1),
            "st": room.status,
            "cn": self._active_contributor_count(room),
            "dir": room.direction,
            "ss": room.start_station,
            "es": room.end_station,
            "ts": _iso_now(),
            "ci": self._top_contributor_infos(room),
        }

    def all_rooms_info(self) -> list[dict]:
        """Return detailed info for every active room (for dashboard)."""
        now = time.time()
        results = []
        for tid, room in self._rooms.items():
            contributors = []
            for uid, c in room.contributors.items():
                contributors.append({
                    "user_id": uid,
                    "display_name": c.display_name,
                    "avatar_url": c.avatar_url,
                    "lat": round(c.lat, 6),
                    "lng": round(c.lng, 6),
                    "speed": round(c.speed, 1),
                    "last_update": c.last_update,
                    "is_stale": c.last_update > 0 and (now - c.last_update) > _STALE_TIMEOUT_S,
                    "is_leader": uid == room.leader_id,
                    "is_captain": c.is_captain,
                    "from_station": c.from_station_name,
                    "to_station": c.to_station_name,
                    "trip_distance_km": round(c.trip_distance_km, 1),
                })
            contributors.sort(key=lambda x: (not x["is_captain"], not x["is_leader"], -x["trip_distance_km"]))
            waiting = []
            for w in room.waiting_list:
                waiting.append({
                    "user_id": w.user_id,
                    "display_name": w.display_name,
                    "avatar_url": w.avatar_url,
                    "from_station": w.from_station_name,
                    "to_station": w.to_station_name,
                    "trip_distance_km": round(w.trip_distance_km, 1),
                    "joined_at": w.joined_at,
                })
            results.append({
                "train_id": tid,
                "trip_id": room.trip_id,
                "status": room.status,
                "lat": round(room.lat, 6),
                "lng": round(room.lng, 6),
                "speed": round(room.speed, 1),
                "direction": room.direction,
                "start_station": room.start_station,
                "end_station": room.end_station,
                "contributors_count": len(room.contributors),
                "listeners_count": 0,  # HTTP model — listeners not tracked
                "waiting_count": len(room.waiting_list),
                "max_active_contributors": room.max_active_contributors,
                "leader_id": room.leader_id,
                "contributors": contributors,
                "waiting_list": waiting,
            })
        return results

    def room_info(self, train_id: str) -> Optional[dict]:
        room = self._rooms.get(train_id)
        if not room:
            return None
        return {
            "train_id": train_id,
            "contributors": len(room.contributors),
            "listeners": 0,
            "status": room.status,
            "lat": room.lat,
            "lng": room.lng,
        }

    async def _alert_new_contribution(
        self, train_id: str, user_id: str, display_name: str,
        from_station: str, to_station: str,
    ) -> None:
        """Fire-and-forget: create a dashboard alert for a new contribution."""
        try:
            from app.services.admin_alert_service import create_alert
            name = display_name or user_id[:8]
            route = f"{from_station} → {to_station}" if from_station and to_station else "مسار غير محدد"
            await create_alert(
                alert_type="contribution",
                title=f"مساهمة جديدة في قطار {train_id}",
                body=f"{name} بدأ المساهمة ({route})",
                metadata={
                    "train_id": train_id,
                    "user_id": user_id,
                    "display_name": name,
                    "from_station": from_station,
                    "to_station": to_station,
                },
                navigate_to=f"/admin/contributors?train={train_id}",
            )
        except Exception as exc:
            logger.error("Failed to create contribution alert: %s", exc)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Module singleton
tracking_manager = TrackingManager()
