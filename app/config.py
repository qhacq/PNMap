"""Paramètres de l'application (surchargeables par variables d'environnement)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    clubs_csv: Path = field(default_factory=lambda: Path(_env("PNMAP_CLUBS_CSV", str(BASE_DIR / "data" / "clubs.csv"))))
    communes_file: Path = field(default_factory=lambda: Path(_env("PNMAP_COMMUNES", str(BASE_DIR / "data" / "communes.json"))))
    corrections_file: Path = field(default_factory=lambda: Path(_env("PNMAP_CORRECTIONS", str(BASE_DIR / "data" / "corrections.json"))))
    cache_dir: Path = field(default_factory=lambda: Path(_env("PNMAP_CACHE_DIR", str(BASE_DIR / "cache"))))
    nominatim_url: str = field(default_factory=lambda: _env("PNMAP_NOMINATIM_URL", "https://nominatim.openstreetmap.org"))
    osrm_url: str = field(default_factory=lambda: _env("PNMAP_OSRM_URL", "https://router.project-osrm.org"))
    user_agent: str = field(default_factory=lambda: _env(
        "PNMAP_USER_AGENT",
        "PNMap/1.0 (repartition des poules Pre-Nationale Masculine Grand Est; https://github.com/qhacq/PNMap)",
    ))
    # Politique d'usage Nominatim : 1 requête par seconde maximum.
    nominatim_min_interval: float = 1.05
    tile_url: str = field(default_factory=lambda: _env("PNMAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"))
    default_time_limit: float = 30.0
    solver_workers: int = 8

    @property
    def geocode_cache(self) -> Path:
        return self.cache_dir / "geocode.json"

    @property
    def matrix_cache(self) -> Path:
        return self.cache_dir / "matrix.json"

    @property
    def optimal_file(self) -> Path:
        return self.cache_dir / "optimal.json"

    @property
    def tiles_dir(self) -> Path:
        return self.cache_dir / "tiles"

    @property
    def ui_state_file(self) -> Path:
        return self.cache_dir / "ui_state.json"


# Emprise approximative de la région Grand Est (lon min, lat max, lon max, lat min)
# utilisée pour éviter les homonymes hors région lors du géocodage.
GRAND_EST_VIEWBOX = (3.3, 50.25, 8.3, 47.35)

# Repli quand OSRM est indisponible.
HAVERSINE_FACTOR = 1.3
FALLBACK_SPEED_KMH = 80.0
