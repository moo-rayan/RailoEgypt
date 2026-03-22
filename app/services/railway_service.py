"""
RailwayGraph: builds a weighted graph from GeoJSON railway lines
and provides A* pathfinding between two geographic coordinates.

Only features with fclass='rail' are included (excludes subway, tram,
light_rail, narrow_gauge).  The graph is built once at app startup and
held as a module-level singleton.
"""

import heapq
import json
import logging
import math
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── paths & constants ────────────────────────────────────────────────────────

_GEOJSON_PATH = (
    Path(__file__).parent.parent / "data" / "Egypt-railway-lines-new.geojson"
)

_RAIL_CLASSES: frozenset[str] = frozenset({"rail"})

_PRECISION = 5        # decimal places for coordinate rounding (~1 m at equator)
_CELL_DEG  = 0.01     # spatial-grid cell size in degrees (~1.1 km at 30 °N)
_DISPLAY_EPSILON = 0.001  # Douglas-Peucker tolerance for background display lines.


# ── low-level helpers ────────────────────────────────────────────────────────

def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Return Haversine distance in **metres** between two lon/lat points."""
    R = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _node_key(lon: float, lat: float) -> str:
    """Deterministic string key for a coordinate pair (rounds to _PRECISION dp)."""
    return f"{round(lon, _PRECISION)},{round(lat, _PRECISION)}"


def _grid_cell(lon: float, lat: float) -> tuple[int, int]:
    return (int(lon / _CELL_DEG), int(lat / _CELL_DEG))


def _perp_dist(px: float, py: float,
               ax: float, ay: float,
               bx: float, by: float) -> float:
    """Perpendicular distance from point (px,py) to segment (ax,ay)→(bx,by)."""
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _simplify_line(
    pts: list[tuple[float, float]],
    epsilon: float,
) -> list[tuple[float, float]]:
    """
    Iterative Douglas-Peucker polyline simplification.
    Removes points closer than *epsilon* to the simplified line,
    preserving the overall shape.
    """
    n = len(pts)
    if n <= 2:
        return list(pts)

    keep = [False] * n
    keep[0] = keep[-1] = True

    stack = [(0, n - 1)]
    while stack:
        si, ei = stack.pop()
        ax, ay = pts[si]
        bx, by = pts[ei]
        max_d, max_i = 0.0, si
        for i in range(si + 1, ei):
            d = _perp_dist(pts[i][0], pts[i][1], ax, ay, bx, by)
            if d > max_d:
                max_d, max_i = d, i
        if max_d > epsilon:
            keep[max_i] = True
            if max_i - si > 1:
                stack.append((si, max_i))
            if ei - max_i > 1:
                stack.append((max_i, ei))

    return [pts[i] for i in range(n) if keep[i]]


# ── main graph class ─────────────────────────────────────────────────────────

class RailwayGraph:
    """
    Weighted undirected graph over Egypt's mainline railway network.

    Nodes   – unique coordinate positions (rounded to _PRECISION decimal places).
    Edges   – consecutive coordinate pairs within a GeoJSON LineString.
    Weights – Haversine distance in metres.

    Fast nearest-node lookup uses a spatial grid (O(1) average).
    Path search uses standard A* with Haversine as the admissible heuristic.
    """

    def __init__(self) -> None:
        # node_key → (lon, lat)
        self._nodes: dict[str, tuple[float, float]] = {}
        # node_key → [(neighbour_key, distance_m)]
        self._adj:   dict[str, list[tuple[str, float]]] = {}
        # spatial grid: cell → [node_key, ...]
        self._grid:  dict[tuple[int, int], list[str]] = {}
        # pre-built polylines for map display: list of [(lat, lon), ...]
        self._lines: list[list[tuple[float, float]]] = []
        # Simplified display polylines (lazy, computed on first access)
        self._display_lines: list[list[tuple[float, float]]] | None = None
        self._built = False

    # ── graph construction ───────────────────────────────────────────────────

    def build(self, path: Path = _GEOJSON_PATH) -> int:
        """
        Parse the GeoJSON file, filter railway-only features, and build the
        graph.  Also caches the raw polylines for map display.

        Returns the total number of nodes inserted.
        """
        seen_edges: set[tuple[str, str]] = set()

        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)

        for feat in data.get("features", []):
            props = feat.get("properties", {})
            if props.get("fclass") not in _RAIL_CLASSES:
                continue

            geom  = feat.get("geometry", {})
            gtype = geom.get("type", "")

            if gtype == "LineString":
                raw_lines: list[list] = [geom["coordinates"]]
            elif gtype == "MultiLineString":
                raw_lines = geom["coordinates"]
            else:
                continue

            for line in raw_lines:
                if len(line) < 2:
                    continue

                display_poly: list[tuple[float, float]] = []
                prev_key: Optional[str] = None

                for lon, lat in line:
                    k = _node_key(lon, lat)
                    display_poly.append((lat, lon))   # Flutter uses (lat, lon)

                    if k not in self._nodes:
                        self._nodes[k] = (lon, lat)
                        self._adj[k]   = []
                        cell = _grid_cell(lon, lat)
                        self._grid.setdefault(cell, []).append(k)

                    if prev_key is not None and prev_key != k:
                        edge_id = (min(prev_key, k), max(prev_key, k))
                        if edge_id not in seen_edges:
                            seen_edges.add(edge_id)
                            plon, plat = self._nodes[prev_key]
                            d = _haversine(plon, plat, lon, lat)
                            self._adj[prev_key].append((k, d))
                            self._adj[k].append((prev_key, d))

                    prev_key = k

                if display_poly:
                    self._lines.append(display_poly)

        self._built = True
        return len(self._nodes)

    # ── spatial lookup ───────────────────────────────────────────────────────

    def nearest_node(
        self,
        lon: float,
        lat: float,
        search_radius: int = 3,
    ) -> Optional[str]:
        """
        Return the graph node closest to (lon, lat) using the spatial grid.
        Searches within `search_radius` cells in each direction.
        Returns None if the graph is empty.
        """
        cx, cy = _grid_cell(lon, lat)
        best_key: Optional[str] = None
        best_dist = float("inf")

        for dx in range(-search_radius, search_radius + 1):
            for dy in range(-search_radius, search_radius + 1):
                for k in self._grid.get((cx + dx, cy + dy), []):
                    nlon, nlat = self._nodes[k]
                    d = _haversine(lon, lat, nlon, nlat)
                    if d < best_dist:
                        best_dist = d
                        best_key  = k

        return best_key

    # ── A* pathfinding ───────────────────────────────────────────────────────

    def a_star(
        self,
        from_lon: float,
        from_lat: float,
        to_lon:   float,
        to_lat:   float,
    ) -> Optional[list[tuple[float, float]]]:
        """
        Find the shortest railway path from (from_lon, from_lat) to
        (to_lon, to_lat) using A* with a Haversine heuristic.

        Returns a list of (lat, lon) tuples in travel order,
        or None if no path exists.
        """
        if not self._built:
            return None

        start = self.nearest_node(from_lon, from_lat)
        goal  = self.nearest_node(to_lon,   to_lat)

        if start is None or goal is None:
            return None

        if start == goal:
            lon, lat = self._nodes[start]
            return [(lat, lon)]

        goal_lon, goal_lat = self._nodes[goal]

        # min-heap: (f_score, node_key)
        heap: list[tuple[float, str]] = []
        heapq.heappush(heap, (0.0, start))

        g_score: dict[str, float] = {start: 0.0}
        came_from: dict[str, str] = {}
        visited: set[str]         = set()

        while heap:
            _, current = heapq.heappop(heap)

            if current in visited:
                continue
            visited.add(current)

            if current == goal:
                # Reconstruct and return the path
                path_keys: list[str] = []
                node = current
                while node in came_from:
                    path_keys.append(node)
                    node = came_from[node]
                path_keys.append(node)
                path_keys.reverse()
                return [
                    (self._nodes[n][1], self._nodes[n][0])   # (lat, lon)
                    for n in path_keys
                ]

            cur_g = g_score[current]

            for neighbour, dist in self._adj.get(current, []):
                if neighbour in visited:
                    continue
                tentative_g = cur_g + dist
                if tentative_g < g_score.get(neighbour, float("inf")):
                    g_score[neighbour]    = tentative_g
                    came_from[neighbour] = current
                    nlon, nlat = self._nodes[neighbour]
                    h = _haversine(nlon, nlat, goal_lon, goal_lat)
                    heapq.heappush(heap, (tentative_g + h, neighbour))

        return None   # no path found

    # ── persistence helpers (Redis serialisation) ────────────────────────────

    def to_dict(self) -> dict:
        """
        Serialise the built graph to a JSON-compatible dict for Redis storage.
        Grid tuple-keys are converted to "x,y" strings.
        """
        return {
            "nodes": self._nodes,
            "adj":   self._adj,
            "grid":  {f"{r},{c}": ids for (r, c), ids in self._grid.items()},
            "lines": self._lines,
        }

    def restore_from_dict(self, data: dict) -> None:
        """
        Restore graph state from a previously serialised dict (loaded from Redis).
        JSON lists are converted back to the expected Python types.
        """
        # nodes: str → (lon, lat)
        self._nodes = {k: (v[0], v[1]) for k, v in data["nodes"].items()}

        # adj: str → [(neighbour_key, distance_m)]  – lists-of-lists are fine
        self._adj = data["adj"]

        # grid: "x,y" string keys back to (int, int) tuple keys
        self._grid = {
            (int(p[0]), int(p[1])): ids
            for k, ids in data["grid"].items()
            for p in (k.split(","),)
        }

        # lines: list of [(lat, lon)] – no conversion needed
        self._lines = data["lines"]

        self._built = True

    # ── display helpers ──────────────────────────────────────────────────────

    @property
    def all_lines(self) -> list[list[tuple[float, float]]]:
        """All railway polylines as (lat, lon) pairs – full resolution."""
        return self._lines

    @property
    def display_lines(self) -> list[list[tuple[float, float]]]:
        """
        Simplified polylines for map background display.
        Douglas-Peucker reduces point count ~70-80% while preserving shape.
        Computed lazily and cached.
        """
        if self._display_lines is None:
            orig_pts = sum(len(l) for l in self._lines)
            simplified: list[list[tuple[float, float]]] = []
            for line in self._lines:
                s = _simplify_line(line, _DISPLAY_EPSILON)
                if len(s) >= 2:
                    simplified.append(s)
            self._display_lines = simplified
            simp_pts = sum(len(l) for l in simplified)
            logger.info(
                "Display lines simplified: %d→%d points (%.0f%% reduction), "
                "%d→%d polylines",
                orig_pts, simp_pts,
                (1 - simp_pts / max(orig_pts, 1)) * 100,
                len(self._lines), len(simplified),
            )
        return self._display_lines

    def snap_to_rail(
        self,
        lon: float,
        lat: float,
        search_radius: int = 2,
    ) -> Optional[tuple[float, float, float]]:
        """
        Project (lon, lat) onto the nearest railway segment.

        Returns (snapped_lon, snapped_lat, distance_m) or None if graph
        is empty.  Unlike ``nearest_node`` this interpolates between
        graph nodes so the result lies *on* an edge, not just at a vertex.
        """
        nearest = self.nearest_node(lon, lat, search_radius=search_radius)
        if nearest is None:
            return None

        best_lon, best_lat = self._nodes[nearest]
        best_dist = _haversine(lon, lat, best_lon, best_lat)

        # Check every edge connected to the nearest node (and its neighbours)
        checked: set[tuple[str, str]] = set()
        nodes_to_check = [nearest]
        # Also include neighbours so we cover adjacent segments
        for nb_key, _ in self._adj.get(nearest, []):
            nodes_to_check.append(nb_key)

        for nk in nodes_to_check:
            for nb_key, _ in self._adj.get(nk, []):
                edge_id = (min(nk, nb_key), max(nk, nb_key))
                if edge_id in checked:
                    continue
                checked.add(edge_id)

                a_lon, a_lat = self._nodes[nk]
                b_lon, b_lat = self._nodes[nb_key]

                # Project point onto segment [A, B]
                p_lon, p_lat = self._project_on_segment(
                    lon, lat, a_lon, a_lat, b_lon, b_lat,
                )
                d = _haversine(lon, lat, p_lon, p_lat)
                if d < best_dist:
                    best_dist = d
                    best_lon, best_lat = p_lon, p_lat

        return (best_lon, best_lat, best_dist)

    @staticmethod
    def _project_on_segment(
        px: float, py: float,
        ax: float, ay: float,
        bx: float, by: float,
    ) -> tuple[float, float]:
        """
        Return the closest point on segment (ax,ay)→(bx,by) to (px,py).
        Works in lon/lat space — accurate enough for short rail segments.
        """
        dx, dy = bx - ax, by - ay
        if dx == 0.0 and dy == 0.0:
            return (ax, ay)
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        return (ax + t * dx, ay + t * dy)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def is_built(self) -> bool:
        return self._built


# ── module singleton ─────────────────────────────────────────────────────────

railway_graph = RailwayGraph()
