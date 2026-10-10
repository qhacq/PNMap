"""Matrice des distances et durées routières (OSRM), avec cache et repli haversine."""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import FALLBACK_SPEED_KMH, HAVERSINE_FACTOR
from .storage import load_json, save_json

log = logging.getLogger(__name__)

Coord = tuple[float, float]  # (lat, lon)


def haversine_m(a: Coord, b: Coord) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def fallback_pair(a: Coord, b: Coord) -> tuple[float, float]:
    """Distance haversine x 1,3 et durée estimée à vitesse moyenne constante."""
    d = haversine_m(a, b) * HAVERSINE_FACTOR
    return d, d / (FALLBACK_SPEED_KMH / 3.6)


@dataclass
class Matrix:
    distance_m: list[list[float]]  # distance_m[i][j] : trajet de i vers j
    duration_s: list[list[float]]
    source: str  # osrm | haversine | mixte

    @property
    def size(self) -> int:
        return len(self.distance_m)


def _key(a: Coord, b: Coord) -> str:
    return f"{a[0]:.5f},{a[1]:.5f}>{b[0]:.5f},{b[1]:.5f}"


class DistanceProvider:
    """Calcule des matrices via l'API table d'OSRM ; met en cache chaque paire de points."""

    def __init__(self, cache_path: Path, osrm_url: str, client: httpx.Client | None = None,
                 user_agent: str = "PNMap/1.0"):
        self.cache_path = cache_path
        self.osrm_url = osrm_url.rstrip("/")
        self.client = client or httpx.Client(timeout=60.0, headers={"User-Agent": user_agent})
        self._cache: dict[str, list[float]] = load_json(cache_path, {})
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def _osrm_table(self, coords: list[Coord]) -> tuple[list[list], list[list]]:
        path = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
        resp = self.client.get(f"{self.osrm_url}/table/v1/driving/{path}",
                               params={"annotations": "distance,duration"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM : {data.get('code')} {data.get('message', '')}")
        return data["distances"], data["durations"]

    def matrix(self, coords: list[Coord], use_osrm: bool = True) -> Matrix:
        with self._lock:
            return self._matrix(coords, use_osrm)

    def _matrix(self, coords: list[Coord], use_osrm: bool) -> Matrix:
        n = len(coords)
        # Points distincts (deux équipes d'une même commune partagent un point).
        uniq = list(dict.fromkeys((round(c[0], 5), round(c[1], 5)) for c in coords))
        missing = any(_key(a, b) not in self._cache for a in uniq for b in uniq if a != b)
        self.last_error = None
        if missing and use_osrm and len(uniq) > 1:
            try:
                dist, dur = self._osrm_table(uniq)
                for i, a in enumerate(uniq):
                    for j, b in enumerate(uniq):
                        if i != j and dist[i][j] is not None and dur[i][j] is not None:
                            self._cache[_key(a, b)] = [float(dist[i][j]), float(dur[i][j])]
                save_json(self.cache_path, self._cache)
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
                self.last_error = str(exc)
                log.warning("OSRM indisponible, repli haversine x %.1f : %s", HAVERSINE_FACTOR, exc)

        distance = [[0.0] * n for _ in range(n)]
        duration = [[0.0] * n for _ in range(n)]
        sources: set[str] = set()
        rounded = [(round(c[0], 5), round(c[1], 5)) for c in coords]
        for i, a in enumerate(rounded):
            for j, b in enumerate(rounded):
                if i == j or a == b:
                    continue
                hit = self._cache.get(_key(a, b))
                if hit is not None:
                    distance[i][j], duration[i][j] = hit
                    sources.add("osrm")
                else:
                    distance[i][j], duration[i][j] = fallback_pair(a, b)
                    sources.add("haversine")
        source = "mixte" if len(sources) > 1 else (sources.pop() if sources else "osrm")
        return Matrix(distance, duration, source)
