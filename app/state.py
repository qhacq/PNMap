"""État de l'application : équipes, corrections de géolocalisation, matrice, tâche de fond."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

from .config import Settings
from .data import Team, load_teams
from .distances import DistanceProvider, Matrix
from .geocode import Geocoder
from .metrics import evaluate, integer_costs
from .pdfmap import TileFetcher
from .optimizer import OptimizeParams, solve
from .storage import load_json, save_json

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, settings: Settings, geocoder: Geocoder | None = None,
                 distances: DistanceProvider | None = None, tiles: TileFetcher | None = None):
        self.settings = settings
        self.geocoder = geocoder or Geocoder(settings.geocode_cache, settings.nominatim_url,
                                             settings.user_agent, settings.nominatim_min_interval)
        self.distances = distances or DistanceProvider(settings.matrix_cache, settings.osrm_url,
                                                       user_agent=settings.user_agent)
        self.tiles = tiles or TileFetcher(settings.tiles_dir, settings.tile_url, settings.user_agent)
        self.teams: list[Team] = load_teams(settings.clubs_csv)
        self.pools: list[str] = sorted({t.current_pool for t in self.teams if t.current_pool}) or ["A", "B", "C"]
        self.corrections: dict[str, dict] = load_json(settings.corrections_file, {})
        self.matrix: Matrix | None = None
        self.matrix_error: str | None = None
        self.matrix_key: str | None = None
        # Version optimale de référence (résultat du solveur), conservée sur disque.
        self.optimal: dict | None = load_json(settings.optimal_file, None)
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
        key = hashlib.sha1(json.dumps(
            [[t.id, round(t.lat, 5), round(t.lon, 5)] for t in self.teams] + [m.source]).encode()).hexdigest()
        with self.lock:
            self.matrix = m
            self.matrix_key = key
            self.matrix_error = (f"OSRM indisponible ({self.distances.last_error}) : repli haversine x 1,3."
                                 if self.distances.last_error else None)
        self.job["done"] = 1
        return m

    # -- tâche de fond -----------------------------------------------------
    # -- optimisation ------------------------------------------------------
    def default_pool_sizes(self) -> dict[str, int]:
        n, k = len(self.teams), len(self.pools)
        return {p: n // k + (1 if i < n % k else 0) for i, p in enumerate(self.pools)}

    @property
    def optimal_is_fresh(self) -> bool:
        return bool(self.optimal and self.matrix_key and self.optimal.get("matrix_key") == self.matrix_key)

    def optimize(self, req: dict) -> dict:
        """Lance CP-SAT. req : pool_sizes, metric, locks, forbidden_pairs, imposed_pairs,
        separate_clubs, stability_weight, fairness, time_limit. En cas de succès, le résultat
        devient la version optimale de référence."""
        m = self.matrix
        if m is None:
            raise RuntimeError(self.matrix_error or "Matrice des distances pas encore disponible.")
        pools = self.pools
        pidx = {p: k for k, p in enumerate(pools)}
        tidx = {t.id: i for i, t in enumerate(self.teams)}
        sizes = req.get("pool_sizes") or self.default_pool_sizes()
        if set(sizes) != set(pools):
            raise ValueError(f"Tailles attendues pour les poules {pools}")
        try:
            locks = {tidx[int(t)]: pidx[p] for t, p in (req.get("locks") or {}).items()}
            forbidden = [(tidx[int(a)], tidx[int(b)]) for a, b in req.get("forbidden_pairs") or []]
            imposed = [(tidx[int(a)], tidx[int(b)]) for a, b in req.get("imposed_pairs") or []]
        except KeyError as exc:
            raise ValueError(f"Équipe ou poule inconnue : {exc}")
        metric = req.get("metric") or "distance"
        current = [pidx.get(t.current_pool) for t in self.teams]
        hint = current if all(c is not None for c in current) else None
        if req.get("hint"):
            h = {int(k): v for k, v in req["hint"].items()}
            if set(h) == set(tidx) and all(v in pidx for v in h.values()):
                hint = [pidx[h[t.id]] for t in self.teams]
        unit = 6 if metric == "duration" else 10  # minutes -> 10 s ; km -> 100 m
        params = OptimizeParams(
            pool_sizes=[int(sizes[p]) for p in pools],
            locks=locks,
            forbidden_pairs=forbidden,
            imposed_pairs=imposed,
            clubs=[t.club for t in self.teams] if req.get("separate_clubs", True) else None,
            current=current,
            stability_penalty=round(max(0.0, float(req.get("stability_weight") or 0)) * unit),
            fairness=float(req.get("fairness") or 0.0),
            time_limit=float(req.get("time_limit") or self.settings.default_time_limit),
            workers=self.settings.solver_workers,
            hint=hint,
        )
        res = solve(integer_costs(m, metric), params)
        out = {"status": res.status, "message": res.message, "wall_time": round(res.wall_time, 2),
               "objective": res.objective, "best_bound": res.best_bound}
        if res.ok:
            assignment = {t.id: pools[res.assignment[i]] for i, t in enumerate(self.teams)}
            metrics = evaluate(self.teams, m, assignment, pools)
            gap = round(max(0.0, (res.objective - res.best_bound) / res.objective), 4) if res.objective else 0.0
            stored_req = {k: v for k, v in req.items() if k != "hint"}
            stored_req["pool_sizes"] = {p: int(sizes[p]) for p in pools}
            self.optimal = {
                "assignment": {str(k): v for k, v in assignment.items()},
                "status": res.status,
                "proven": res.status == "OPTIMAL",
                "message": res.message,
                "gap": gap,
                "wall_time": out["wall_time"],
                "total_km": metrics["total_km"],
                "params": stored_req,
                "matrix_key": self.matrix_key,
                "matrix_source": m.source,
                "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_json(self.settings.optimal_file, self.optimal)
            out.update(assignment=assignment, metrics=metrics, gap=gap, optimal=self.optimal)
        return out

    def optimal_assignment(self) -> dict[int, str] | None:
        if not self.optimal:
            return None
        a = {int(k): v for k, v in self.optimal["assignment"].items()}
        ids = {t.id for t in self.teams}
        return a if set(a) == ids and all(v in self.pools for v in a.values()) else None

    def start_job(self, geocode: bool = True, retry_failed: bool = False, use_osrm: bool = True,
                  optimize: bool = True) -> bool:
        if self.job["running"]:
            return False

        def run() -> None:
            try:
                if geocode:
                    self.geocode_all(retry_failed)
                self.compute_matrix(use_osrm)
                if optimize and self.matrix is not None and not (self.optimal_is_fresh and self.optimal_assignment()):
                    # Version optimale calculée au lancement (ou après un changement de matrice),
                    # avec les derniers réglages utilisés.
                    req = dict((self.optimal or {}).get("params") or {})
                    if set((req.get("pool_sizes") or {})) != set(self.pools) or \
                            sum((req.get("pool_sizes") or {}).values()) != len(self.teams):
                        req["pool_sizes"] = self.default_pool_sizes()
                    tl = req.get("time_limit") or self.settings.default_time_limit
                    self.job.update(step=f"Optimisation initiale (jusqu'à {tl:.0f} s)", done=0, total=0)
                    try:
                        self.optimize(req)
                    except ValueError:
                        self.optimize({"pool_sizes": self.default_pool_sizes()})
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
