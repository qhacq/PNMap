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
        self.corrections: dict[str, dict] = load_json(settings.corrections_file, None)
        if self.corrections is None:  # serverless : corrections livrées dans le dépôt, en lecture seule
            from .config import BASE_DIR
            self.corrections = load_json(BASE_DIR / "data" / "corrections.json", {})
        # Coordonnées approximatives livrées avec l'application : affichage immédiat, même hors ligne.
        self.seed = {k.strip().lower(): v for k, v in load_json(settings.communes_file, {}).items()
                     if not k.startswith("_") and isinstance(v, list) and len(v) == 2}
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

    def _locate_without_network(self, t: Team) -> None:
        """Correction manuelle, sinon cache Nominatim, sinon coordonnées approximatives livrées."""
        if self._apply_correction(t):
            return
        hit = self.geocoder.cached_address(t.adresse) if t.adresse else None
        if hit:
            self._set_location(t, hit, "adresse")
            return
        hit = self.geocoder.cached_commune(t.commune)
        if hit:
            self._set_location(t, hit, "nominatim")
            return
        seed = self.seed.get(t.commune.strip().lower()) if t.commune else None
        self._set_location(t, {"lat": seed[0], "lon": seed[1], "label": f"{t.commune} (position approximative)"}
                           if seed else None, "approx")

    def _apply_cached_locations(self) -> None:
        for t in self.teams:
            self._locate_without_network(t)

    def geocode_all(self, retry_failed: bool = False) -> None:
        """Interroge Nominatim pour les communes pas encore géocodées (les autres viennent du cache)."""
        todo = [t for t in self.teams if not self._apply_correction(t) and (t.commune or t.adresse)
                and (retry_failed or not (self.geocoder.cached_address(t.adresse) if t.adresse
                                          else self.geocoder.cached_commune(t.commune)))]
        self.job.update(step="Géocodage des communes (Nominatim)", done=0, total=len(todo))
        for t in todo:
            loc, source = None, "nominatim"
            if t.adresse:  # adresse de la salle fournie dans le CSV : prioritaire
                loc, source = self.geocoder.geocode_address(t.adresse, retry_failed=retry_failed), "adresse"
            if not loc and t.commune:
                loc, source = self.geocoder.geocode_commune(t.commune, retry_failed=retry_failed), "nominatim"
            with self.lock:
                if loc:
                    self._set_location(t, loc, source)
                else:
                    self._locate_without_network(t)  # garde la position approximative
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
            self._locate_without_network(t)
            self.matrix = None
            return t

    # -- matrice -----------------------------------------------------------
    @property
    def all_located(self) -> bool:
        return all(t.located for t in self.teams)

    def compute_matrix(self, use_osrm: bool = True) -> Matrix | None:
        """Matrice sur les équipes localisées ; une équipe non localisée compte 0 km (signalée)."""
        located = [i for i, t in enumerate(self.teams) if t.located]
        if len(located) < 2:
            self.matrix = None
            self.matrix_error = "Pas assez d'équipes localisées pour calculer des distances."
            return None
        self.job.update(step="Calcul des distances routières (OSRM)", done=0, total=1)
        sub = self.distances.matrix([(self.teams[i].lat, self.teams[i].lon) for i in located], use_osrm=use_osrm)
        n = len(self.teams)
        dist = [[0.0] * n for _ in range(n)]
        dur = [[0.0] * n for _ in range(n)]
        for a, i in enumerate(located):
            for b, j in enumerate(located):
                dist[i][j], dur[i][j] = sub.distance_m[a][b], sub.duration_s[a][b]
        m = Matrix(dist, dur, sub.source)
        key = hashlib.sha1(json.dumps(
            [[t.id, round(t.lat, 5), round(t.lon, 5)] if t.located else [t.id] for t in self.teams]
            + [m.source]).encode()).hexdigest()
        with self.lock:
            self.matrix = m
            self.matrix_key = key
            notes = []
            missing = [t.name for t in self.teams if not t.located]
            if missing:
                notes.append(f"Sans localisation, km non comptés : {', '.join(missing)} (onglet Géocodage).")
            if self.distances.last_error:
                notes.append(f"OSRM indisponible ({self.distances.last_error}) : distances à vol d'oiseau x 1,3.")
            self.matrix_error = " ".join(notes) or None
        self.job["done"] = 1
        return m

    def ensure_matrix(self) -> None:
        """Serverless : pas de tâche de fond, la matrice est calculée pendant la requête
        (positions connues ou approximatives + un seul appel OSRM), puis gardée en mémoire."""
        if self.matrix is None and not self.job["running"]:
            with self.lock:
                if self.matrix is None:
                    # Seules les équipes absentes des positions livrées interrogent Nominatim.
                    for t in [t for t in self.teams if not t.located and (t.commune or t.adresse)][:15]:
                        loc = self.geocoder.geocode_address(t.adresse) if t.adresse else None
                        loc = loc or (self.geocoder.geocode_commune(t.commune) if t.commune else None)
                        if loc:
                            self._set_location(t, loc, "adresse" if t.adresse else "nominatim")
                    self.compute_matrix()
                    self.job.update(step="", done=0, total=0)

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
            time_limit=min(float(req.get("time_limit") or self.settings.default_time_limit), self.settings.max_time_limit),
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

    def _auto_optimize(self) -> None:
        """(Re)calcule la version optimale si elle manque ou si la matrice a changé,
        avec les derniers réglages utilisés."""
        if self.matrix is None or (self.optimal_is_fresh and self.optimal_assignment()):
            return
        req = dict((self.optimal or {}).get("params") or {})
        sizes = req.get("pool_sizes") or {}
        if set(sizes) != set(self.pools) or sum(sizes.values()) != len(self.teams):
            req["pool_sizes"] = self.default_pool_sizes()
        if self.optimal_assignment():
            req["hint"] = {str(k): v for k, v in self.optimal_assignment().items()}
        tl = req.get("time_limit") or self.settings.default_time_limit
        self.job.update(step=f"Calcul de la version optimale (jusqu'à {tl:.0f} s)", done=0, total=0)
        try:
            self.optimize(req)
        except ValueError:
            self.optimize({"pool_sizes": self.default_pool_sizes()})

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
                    # 1) km affichés tout de suite avec les positions connues ou approximatives ;
                    # 2) géocodage Nominatim des communes manquantes ; 3) recalcul si une position a changé.
                    before = [(t.lat, t.lon) for t in self.teams]
                    if self.matrix is None:
                        self.compute_matrix(use_osrm)
                    if optimize:
                        self._auto_optimize()  # version optimale disponible sans attendre Nominatim
                    self.geocode_all(retry_failed)
                    if self.matrix is None or [(t.lat, t.lon) for t in self.teams] != before:
                        self.compute_matrix(use_osrm)
                else:
                    self.compute_matrix(use_osrm)
                if optimize:
                    self._auto_optimize()
                self.job["error"] = None
            except Exception as exc:  # affiché dans l'interface
                log.exception("Tâche de fond en échec")
                self.job["error"] = str(exc)
            finally:
                self.job.update(running=False, step="")

        self.job.update(running=True, error=None)
        if self.settings.serverless:
            # Pas de fil d'exécution durable : tout se fait pendant la requête. L'optimisation
            # (longue) est lancée par la page elle-même, via /api/optimize.
            optimize = False
            run()
            return True
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return True

    def wait_job(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)
