"""API FastAPI et service de l'interface web."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, exports
from .config import Settings
from .metrics import evaluate
from .state import AppState
from .storage import load_json, save_json

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
log = logging.getLogger("pnmap")


class AssignmentIn(BaseModel):
    assignment: dict[int, str]


class OptimizeIn(BaseModel):
    pool_sizes: dict[str, int] | None = None
    metric: Literal["distance", "duration"] = "distance"
    locks: dict[int, str] = Field(default_factory=dict)
    forbidden_pairs: list[tuple[int, int]] = Field(default_factory=list)
    imposed_pairs: list[tuple[int, int]] = Field(default_factory=list)
    separate_clubs: bool = True
    stability_weight: float = 0.0  # km (ou minutes) de pénalité par équipe qui change de poule
    fairness: float = Field(0.0, ge=0.0, le=1.0)
    time_limit: float | None = Field(None, gt=0, le=3600)
    hint: dict[int, str] | None = None


class LocationIn(BaseModel):
    lat: float | None = None
    lon: float | None = None
    address: str | None = None


class ExportIn(AssignmentIn):
    label: str = "Ma version"
    optimal: dict[int, str] | None = None  # version optimale gardée par le navigateur (serverless)
    kind: Literal["distance", "duration"] = "distance"


def create_app(state: AppState | None = None, autostart: bool = True) -> FastAPI:
    settings = state.settings if state else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autostart and not app.state.pn.settings.serverless:
            app.state.pn.start_job()
        yield

    app = FastAPI(title="PNMap", lifespan=lifespan)
    app.state.pn = state or AppState(settings)

    def st() -> AppState:
        s = app.state.pn
        if s.settings.serverless:
            s.ensure_matrix()
        return s

    def require_matrix():
        s = st()
        if s.matrix is None:
            raise HTTPException(409, s.matrix_error or "Matrice des distances pas encore disponible.")
        return s.matrix

    def check_assignment(a: dict[int, str]) -> dict[int, str]:
        s = st()
        ids = {t.id for t in s.teams}
        unknown = set(a) - ids
        if unknown:
            raise HTTPException(422, f"Équipes inconnues : {sorted(unknown)}")
        bad = {p for p in a.values() if p not in s.pools}
        if bad:
            raise HTTPException(422, f"Poules inconnues : {sorted(bad)}")
        return a

    # -- état --------------------------------------------------------------
    @app.get("/api/state")
    def get_state():
        s = st()
        return {
            "teams": [t.to_dict() for t in s.teams],
            "pools": s.pools,
            "default_pool_sizes": s.default_pool_sizes(),
            "default_time_limit": settings.default_time_limit,
            "serverless": s.settings.serverless,
            "optimal": s.optimal,
            "optimal_fresh": s.optimal_is_fresh,
            "matrix_key": s.matrix_key,
            "matrix_ready": s.matrix is not None,
            "matrix_source": s.matrix.source if s.matrix else None,
            "matrix_error": s.matrix_error,
            "job": s.job,
        }

    @app.get("/api/status")
    def get_status():
        s = st()
        return {"job": s.job, "matrix_ready": s.matrix is not None, "optimal_fresh": s.optimal_is_fresh,
                "matrix_key": s.matrix_key, "optimal_at": (s.optimal or {}).get("computed_at"),
                "matrix_source": s.matrix.source if s.matrix else None, "matrix_error": s.matrix_error}

    @app.get("/api/ui-state")
    def get_ui_state():
        return load_json(settings.ui_state_file, {})

    @app.put("/api/ui-state")
    def put_ui_state(data: dict):
        save_json(settings.ui_state_file, data)
        return {"ok": True}

    # -- géocodage ---------------------------------------------------------
    @app.post("/api/geocode/run")
    def run_geocode(retry_failed: bool = True):
        if not st().start_job(geocode=True, retry_failed=retry_failed):
            raise HTTPException(409, "Une tâche est déjà en cours.")
        return {"started": True}

    @app.post("/api/matrix/refresh")
    def refresh_matrix():
        if not st().start_job(geocode=False):
            raise HTTPException(409, "Une tâche est déjà en cours.")
        return {"started": True}

    @app.post("/api/teams/{team_id}/location")
    def set_location(team_id: int, body: LocationIn):
        s = st()
        try:
            s.team(team_id)
        except KeyError:
            raise HTTPException(404, "Équipe inconnue")
        if body.address:
            loc = s.geocoder.geocode_address(body.address)
            if not loc:
                raise HTTPException(404, f"Adresse introuvable : {body.address}")
            t = s.set_team_location(team_id, loc["lat"], loc["lon"], loc.get("label", body.address), "adresse")
        elif body.lat is not None and body.lon is not None:
            t = s.set_team_location(team_id, body.lat, body.lon, "Point placé sur la carte", "carte")
        else:
            raise HTTPException(422, "Fournir une adresse ou des coordonnées.")
        s.start_job(geocode=False)
        return t.to_dict()

    @app.post("/api/teams/{team_id}/validate")
    def validate(team_id: int):
        try:
            return st().validate_team(team_id).to_dict()
        except KeyError:
            raise HTTPException(404, "Équipe inconnue")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.delete("/api/teams/{team_id}/location")
    def reset_location(team_id: int):
        s = st()
        try:
            t = s.reset_team(team_id)
        except KeyError:
            raise HTTPException(404, "Équipe inconnue")
        s.start_job(geocode=not t.located)
        return t.to_dict()

    # -- matrice et calculs ------------------------------------------------
    @app.get("/api/matrix")
    def get_matrix():
        m = require_matrix()
        s = st()
        return {"ids": [t.id for t in s.teams], "source": m.source,
                "km": [[round(v / 1000, 3) for v in row] for row in m.distance_m],
                "minutes": [[round(v / 60, 1) for v in row] for row in m.duration_s]}

    @app.post("/api/evaluate")
    def post_evaluate(body: AssignmentIn):
        m = require_matrix()
        s = st()
        return evaluate(s.teams, m, check_assignment(body.assignment), s.pools)

    @app.post("/api/optimize")
    def post_optimize(body: OptimizeIn):
        require_matrix()
        if body.hint:
            check_assignment(body.hint)
        try:
            return st().optimize(body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/optimal")
    def get_optimal():
        return {"optimal": st().optimal, "fresh": st().optimal_is_fresh}

    # -- exports -----------------------------------------------------------
    def attachment(content, media: str, name: str) -> Response:
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def export_context(body: ExportIn):
        m = require_matrix()
        s = st()
        metrics = evaluate(s.teams, m, check_assignment(body.assignment), s.pools)
        opt_a = check_assignment(body.optimal) if body.optimal else s.optimal_assignment()
        optimal = evaluate(s.teams, m, opt_a, s.pools) if opt_a else None
        current = evaluate(s.teams, m, {t.id: t.current_pool for t in s.teams}, s.pools)
        return s, m, metrics, optimal, current

    @app.post("/api/export/assignment.csv")
    def export_assignment(body: ExportIn):
        s, m, metrics, optimal, current = export_context(body)
        return attachment(exports.assignment_csv(s.teams, metrics, optimal, current, body.label),
                          "text/csv; charset=utf-8", f"repartition_{exports.slug(body.label)}.csv")

    @app.post("/api/export/matrix.csv")
    def export_matrix(body: ExportIn):
        m = require_matrix()
        name = "matrice_durees.csv" if body.kind == "duration" else "matrice_distances.csv"
        return attachment(exports.matrix_csv(st().teams, m, body.kind), "text/csv; charset=utf-8", name)

    @app.post("/api/export/report.pdf")
    def export_pdf(body: ExportIn):
        s, m, metrics, optimal, current = export_context(body)
        pdf = exports.report_pdf(s.teams, m, metrics, s.pools, optimal=optimal, current=current,
                                 label=body.label, optimal_info=s.optimal, tiles=s.tiles)
        return attachment(pdf, "application/pdf", f"repartition_{exports.slug(body.label)}.pdf")

    # -- interface ---------------------------------------------------------
    @app.get("/")
    def index():
        # Numéro de version dans les URL des fichiers : le navigateur ne peut pas garder
        # un ancien app.js après une mise à jour.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        tag = f"{__version__}-{int(max((STATIC_DIR / f).stat().st_mtime for f in ('app.js', 'style.css', 'index.html')))}"
        return HTMLResponse(html.replace("__VERSION__", tag), headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def no_stale_assets(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/static/app", "/static/style", "/api/")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def build() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


# Instance de niveau module : `uvicorn app.main:app` ou `fastapi run app/main.py`.
app = build()
