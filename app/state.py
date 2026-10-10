"""État de l'application : équipes, corrections de géolocalisation, matrice, tâche de fond."""
from __future__ import annotations

import logging
import threading

from .config import Settings
from .data import Team, load_teams
from .distances import DistanceProvider, Matrix
from .geocode import Geocoder
from .storage import load_json, save_json

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, settings: Settings, geocoder: Geocoder | None = None,
                 distances: DistanceProvider | None = None):
        self.settings = settings
        self.geocoder = geocoder or Geocoder(settings.geocode_cache, settings.nominatim_url,
                                             settings.user_agent, settings.nominatim_min_interval)
        self.distances = distances or DistanceProvider(settings.matrix_cache, settings.osrm_url,
                                                       user_agent=settings.user_agent)
        self.teams: list[Team] = load_teams(settings.clubs_csv)
        self.pools: list[str] = sorted({t.current_pool for t in self.teams if t.current_pool}) or ["A", "B", "C"]
        self.corrections: dict[str, dict] = load_json(settings.corrections_file, {})
        self.matrix: Matrix | None = None
        self.matrix_error: str | None = None
        self.lock = threading.RLock()
        self.job = {"running": False, "step": "", "done": 0, "total": 0, "error": None}
        self._thread: threading.Thread | None = None
        self._apply_cached_locations()

    # -- géolocalisation ---------------------------------------------------
    def team(self, team_id: int) -> Team:
        for t in self.teams:
            if t.id == team_id:
                return t
        raise KeyError(team_id)

    def _set_location(self, t: Team, loc: dict | None, source: str) -> None:
        if loc:
            t.lat, t.lon, t.geo_label, t.geo_source = loc["lat"], loc["lon"], loc.get("label", ""), source
        else:
            t.lat = t.lon = None
            t.geo_label, t.geo_source = "", ""

    def _apply_correction(self, t: Team) -> bool:
        c = self.corrections.get(str(t.id))
        if not c:
            t.verified = False
            return False
        t.verified = bool(c.get("verified"))
        if c.get("lat") is not None:
            self._set_location(t, c, c.get("source", "carte"))
            return True
        return False

    def _apply_cached_locations(self) -> None:
        """Applique les corrections et ce qui est déjà en cache, sans appel réseau."""
        for t in self.teams:
            if self._apply_correction(t):
                continue
            self._set_location(t, self.geocoder.cached_commune(t.commune), "nominatim")

    def geocode_all(self, retry_failed: bool = False) -> None:
        todo = [t for t in self.teams if not self._apply_correction(t)]
        self.job.update(step="Géocodage des communes", done=0, total=len(todo))
        for t in todo:
            loc = self.geocoder.geocode_commune(t.commune, retry_failed=retry_failed) if t.commune else None
            with self.lock:
                self._set_location(t, loc, "nominatim")
            self.job["done"] += 1

    def save_corrections(self) -> None:
        save_json(self.settings.corrections_file, self.corrections)

    def set_team_location(self, team_id: int, lat: float, lon: float, label: str, source: str) -> Team:
        with self.lock:
            t = self.team(team_id)
            self.corrections[str(team_id)] = {"lat": lat, "lon": lon, "label": label,
                                              "source": source, "verified": True}
            self.save_corrections()
            self._apply_correction(t)
            self.matrix = None
            return t

    def validate_team(self, team_id: int) -> Team:
        with self.lock:
            t = self.team(team_id)
            if not t.located:
                raise ValueError("Équipe non localisée : saisir une adresse ou placer un point.")
            self.corrections[str(team_id)] = {"lat": t.lat, "lon": t.lon, "label": t.geo_label,
                                              "source": t.geo_source or "nominatim", "verified": True}
            self.save_corrections()
            self._apply_correction(t)
            return t

    def reset_team(self, team_id: int) -> Team:
        with self.lock:
            t = self.team(team_id)
            self.corrections.pop(str(team_id), None)
            self.save_corrections()
            t.verified = False
            self._set_location(t, self.geocoder.cached_commune(t.commune), "nominatim")
            self.matrix = None
            return t

    # -- matrice -----------------------------------------------------------
    @property
    def all_located(self) -> bool:
        return all(t.located for t in self.teams)

    def compute_matrix(self, use_osrm: bool = True) -> Matrix | None:
        if not self.all_located:
            self.matrix = None
            missing = [t.name for t in self.teams if not t.located]
            self.matrix_error = f"{len(missing)} équipe(s) sans localisation : corriger dans l'onglet Géocodage."
            return None
        self.job.update(step="Calcul de la matrice des distances (OSRM)", done=0, total=1)
        coords = [(t.lat, t.lon) for t in self.teams]
        m = self.distances.matrix(coords, use_osrm=use_osrm)
        with self.lock:
            self.matrix = m
            self.matrix_error = (f"OSRM indisponible ({self.distances.last_error}) : repli haversine x 1,3."
                                 if self.distances.last_error else None)
        self.job["done"] = 1
        return m

    # -- tâche de fond -----------------------------------------------------
    def start_job(self, geocode: bool = True, retry_failed: bool = False, use_osrm: bool = True) -> bool:
        if self.job["running"]:
            return False

        def run() -> None:
            try:
                if geocode:
                    self.geocode_all(retry_failed)
                self.compute_matrix(use_osrm)
                self.job["error"] = None
            except Exception as exc:  # affiché dans l'interface
                log.exception("Tâche de fond en échec")
                self.job["error"] = str(exc)
            finally:
                self.job.update(running=False, step="")

        self.job.update(running=True, error=None)
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return True

    def wait_job(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)
