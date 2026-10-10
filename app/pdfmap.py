"""Carte statique pour le PDF : fond de tuiles OpenStreetMap (cache local) et marqueurs par poule."""
from __future__ import annotations

import io
import logging
import math
from pathlib import Path

import httpx
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Flowable

log = logging.getLogger(__name__)

TILE = 256
# Palette catégorielle validée (daltonisme) + forme par poule : codage redondant.
POOL_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
POOL_SHAPES = ["circle", "square", "triangle", "diamond", "circle", "square"]


def _px(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = TILE * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


class TileFetcher:
    """Télécharge les tuiles OSM avec un User-Agent explicite et les met en cache sur disque."""

    def __init__(self, cache_dir: Path, url: str, user_agent: str, client: httpx.Client | None = None):
        self.cache_dir = cache_dir
        self.url = url
        self.client = client or httpx.Client(timeout=10.0, headers={"User-Agent": user_agent})
        self.offline = False

    def tile(self, z: int, x: int, y: int) -> Image.Image | None:
        path = self.cache_dir / str(z) / str(x) / f"{y}.png"
        if path.exists():
            try:
                return Image.open(path).convert("RGB")
            except OSError:
                pass
        if self.offline:
            return None
        try:
            r = self.client.get(self.url.format(z=z, x=x, y=y))
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
        except (httpx.HTTPError, OSError) as exc:
            log.warning("Tuiles OSM indisponibles (%s) : carte du PDF sans fond", exc)
            self.offline = True  # pas de nouvelle tentative pour ce rendu
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        return img


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


class MapFlowable(Flowable):
    """points : liste de (lat, lon, poule, libellé)."""

    def __init__(self, points, pools, width, height, tiles: TileFetcher | None = None):
        super().__init__()
        self.points, self.pools, self.width, self.height, self.tiles = points, pools, width, height, tiles
        self._prepare()

    def _prepare(self) -> None:
        lats = [p[0] for p in self.points]
        lons = [p[1] for p in self.points]
        target_w, target_h = 1600, 1600 * self.height / self.width
        z = 6
        for zz in range(6, 12):
            x0, y1 = _px(min(lats), min(lons), zz)
            x1, y0 = _px(max(lats), max(lons), zz)
            if (x1 - x0) * 1.12 <= target_w and (y1 - y0) * 1.12 <= target_h:
                z = zz
        x0, y1 = _px(min(lats), min(lons), z)
        x1, y0 = _px(max(lats), max(lons), z)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # Fenêtre au ratio du cadre, avec une marge.
        w = max(x1 - x0, (y1 - y0) * self.width / self.height) * 1.12 + 40
        h = w * self.height / self.width
        self.z, self.box = z, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        self.background = self._compose() if self.tiles else None

    def _compose(self) -> Image.Image | None:
        bx0, by0, bx1, by1 = self.box
        tx0, ty0, tx1, ty1 = int(bx0 // TILE), int(by0 // TILE), int(bx1 // TILE), int(by1 // TILE)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 80:
            return None
        canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (238, 238, 238))
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                img = self.tiles.tile(self.z, tx, ty)
                if img is None:
                    return None
                canvas.paste(img, ((tx - tx0) * TILE, (ty - ty0) * TILE))
        left, top = bx0 - tx0 * TILE, by0 - ty0 * TILE
        crop = canvas.crop((int(left), int(top), int(left + bx1 - bx0), int(top + by1 - by0)))
        # Fond éclairci pour que les marqueurs ressortent.
        return Image.blend(crop, Image.new("RGB", crop.size, (255, 255, 255)), 0.35)

    def _xy(self, lat: float, lon: float) -> tuple[float, float]:
        px, py = _px(lat, lon, self.z)
        bx0, by0, bx1, by1 = self.box
        return (px - bx0) / (bx1 - bx0) * self.width, (1 - (py - by0) / (by1 - by0)) * self.height

    def _shape(self, x: float, y: float, k: int, r: float) -> None:
        c = self.canv
        c.setFillColor(colors.HexColor(POOL_COLORS[k % len(POOL_COLORS)]))
        c.setStrokeColor(colors.white)
        c.setLineWidth(1)
        shape = POOL_SHAPES[k % len(POOL_SHAPES)]
        if shape == "square":
            c.rect(x - r, y - r, 2 * r, 2 * r, stroke=1, fill=1)
            return
        if shape == "circle":
            c.circle(x, y, r, stroke=1, fill=1)
            return
        pts = ([(x, y + r * 1.2), (x - r * 1.1, y - r * 0.8), (x + r * 1.1, y - r * 0.8)] if shape == "triangle"
               else [(x, y + r * 1.3), (x + r * 1.3, y), (x, y - r * 1.3), (x - r * 1.3, y)])
        path = c.beginPath()
        path.moveTo(*pts[0])
        for q in pts[1:]:
            path.lineTo(*q)
        path.close()
        c.drawPath(path, stroke=1, fill=1)

    def wrap(self, *_):
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        c.saveState()
        p = c.beginPath()
        p.rect(0, 0, self.width, self.height)
        c.clipPath(p, stroke=0)
        if self.background is not None:
            c.drawImage(ImageReader(self.background), 0, 0, self.width, self.height)
        else:
            c.setFillColor(colors.HexColor("#f3f4f6"))
            c.rect(0, 0, self.width, self.height, stroke=0, fill=1)
        # Enveloppes convexes des poules.
        for k, pool in enumerate(self.pools):
            pts = [self._xy(lat, lon) for lat, lon, pp, _ in self.points if pp == pool]
            hull = convex_hull(pts)
            if len(hull) >= 3:
                col = colors.HexColor(POOL_COLORS[k % len(POOL_COLORS)])
                path = c.beginPath()
                path.moveTo(*hull[0])
                for q in hull[1:]:
                    path.lineTo(*q)
                path.close()
                c.setFillColor(col, alpha=0.10)
                c.setStrokeColor(col, alpha=0.8)
                c.setLineWidth(1.2)
                c.setDash(4, 3)
                c.drawPath(path, stroke=1, fill=1)
                c.setDash()
        # Marqueurs : couleur + forme par poule, numéro d'équipe.
        r = 4.2
        for lat, lon, pool, label in self.points:
            k = self.pools.index(pool) if pool in self.pools else 0
            x, y = self._xy(lat, lon)
            self._shape(x, y, k, r)
            c.setFillColor(colors.HexColor("#1d2330"))
            c.setFont("Helvetica-Bold", 5.5)
            c.drawString(x + r + 1.5, y - 2, label)
        # Légende (forme + couleur : lisible sans distinguer les couleurs).
        w = 8 + 52 * len(self.pools)
        c.setFillColor(colors.white, alpha=0.9)
        c.setStrokeColor(colors.HexColor("#9ca3af"))
        c.rect(6, 6, w, 16, stroke=1, fill=1)
        for k, pool in enumerate(self.pools):
            x = 16 + 52 * k
            self._shape(x, 14, k, 3.6)
            c.setFillColor(colors.HexColor("#1d2330"))
            c.setFont("Helvetica", 7.5)
            c.drawString(x + 7, 11.5, f"Poule {pool}")
        c.restoreState()
        c.setFont("Helvetica", 6)
        c.setFillColor(colors.HexColor("#4b5563"))
        c.drawRightString(self.width - 3, 3, "© contributeurs OpenStreetMap" if self.background is not None
                          else "Fond de carte indisponible (hors ligne)")
        c.setStrokeColor(colors.HexColor("#9ca3af"))
        c.setLineWidth(0.5)
        c.rect(0, 0, self.width, self.height, stroke=1, fill=0)
