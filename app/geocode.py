"""Géocodage Nominatim avec cache JSON local et limitation à 1 requête par seconde."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import httpx

from .config import GRAND_EST_VIEWBOX
from .storage import load_json, save_json

log = logging.getLogger(__name__)


class Geocoder:
    def __init__(self, cache_path: Path, base_url: str, user_agent: str,
                 min_interval: float = 1.05, client: httpx.Client | None = None):
        self.cache_path = cache_path
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.client = client or httpx.Client(timeout=20.0, headers={"User-Agent": user_agent})
        if client is not None:
            self.client.headers["User-Agent"] = user_agent
        self._cache: dict[str, dict] = load_json(cache_path, {})
        self._lock = threading.Lock()
        self._last_request = 0.0

    # -- accès réseau ------------------------------------------------------
    def _search(self, params: dict) -> list[dict]:
        with self._lock:  # sérialise les appels pour respecter le débit
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.client.get(f"{self.base_url}/search", params={
                    "format": "jsonv2", "limit": 1, "countrycodes": "fr",
                    "accept-language": "fr", **params,
                })
                resp.raise_for_status()
                return resp.json()
            finally:
                self._last_request = time.monotonic()

    def _cached(self, key: str, attempts: list[dict], retry_failed: bool) -> dict | None:
        hit = self._cache.get(key)
        if hit is not None and not (hit.get("failed") and retry_failed):
            return None if hit.get("failed") else hit
        result: dict = {"failed": True}
        for params in attempts:
            try:
                found = self._search(params)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Nominatim indisponible pour %s : %s", key, exc)
                return None  # erreur réseau : on ne met pas l'échec en cache
            if found:
                r = found[0]
                result = {"lat": float(r["lat"]), "lon": float(r["lon"]), "label": r.get("display_name", "")}
                break
        self._cache[key] = result
        save_json(self.cache_path, self._cache)
        return None if result.get("failed") else result

    # -- API publique ------------------------------------------------------
    def cached_commune(self, commune: str) -> dict | None:
        """Résultat en cache pour une commune, sans appel réseau."""
        hit = self._cache.get(f"commune:{commune.strip().lower()}") if commune.strip() else None
        return None if not hit or hit.get("failed") else hit

    def geocode_commune(self, commune: str, retry_failed: bool = False) -> dict | None:
        commune = commune.strip()
        if not commune:
            return None
        viewbox = ",".join(str(v) for v in GRAND_EST_VIEWBOX)
        return self._cached(f"commune:{commune.lower()}", [
            {"city": commune, "country": "France", "viewbox": viewbox, "bounded": 1},
            {"q": f"{commune}, Grand Est, France"},
        ], retry_failed)

    def geocode_address(self, address: str, retry_failed: bool = True) -> dict | None:
        address = address.strip()
        if not address:
            return None
        viewbox = ",".join(str(v) for v in GRAND_EST_VIEWBOX)
        return self._cached(f"adresse:{address.lower()}", [
            {"q": address, "viewbox": viewbox, "bounded": 1},
            {"q": address},
        ], retry_failed)
