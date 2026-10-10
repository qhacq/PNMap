"""Exports CSV et PDF de la répartition et de la matrice des distances."""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .data import Team
from .distances import Matrix
from .pdfmap import POOL_COLORS, MapFlowable



def _fmt(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()).strip("_") or "version"


def _pct(delta: float, ref: float) -> str:
    return f"{'+' if delta > 0 else ''}{_fmt(100 * delta / ref, 1)} %" if ref else ""


def _signed(x: float, digits: int = 0) -> str:
    return ("+" if x > 0 else "") + _fmt(x, digits)


def assignment_csv(teams: list[Team], metrics: dict, optimal: dict | None = None,
                   current: dict | None = None, label: str = "Ma version") -> str:
    by_id = {t.id: t for t in teams}
    opt_team = {r["id"]: r for r in optimal["teams"]} if optimal else {}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"Répartition : {label}"])
    w.writerow(["poule", "id", "club_ffbb", "commune", "poule_actuelle", "poule_version_optimale",
                "change_de_poule", "km_saison", "km_saison_version_optimale", "ecart_km_vs_optimale",
                "heures_saison", "trajet_le_plus_long_km", "vers"])
    for r in sorted(metrics["teams"], key=lambda r: (r["pool"] or "", r["id"])):
        t = by_id[r["id"]]
        o = opt_team.get(t.id)
        w.writerow([r["pool"], t.id, t.name, t.commune, t.current_pool, o["pool"] if o else "",
                    "oui" if r["moved"] else "non", _fmt(r["km"]), _fmt(o["km"]) if o else "",
                    _signed(r["km"] - o["km"], 1) if o else "", _fmt(r["hours"], 2),
                    _fmt(r["longest_trip_km"]), r["longest_trip_to"] or ""])
    w.writerow([])
    w.writerow(["poule", "nb_equipes", f"km_cumules_{slug(label)}", "km_version_optimale", "km_poules_actuelles",
                "equipe_la_plus_penalisee", "km", "trajet_le_plus_long", "km"])
    for k, p in enumerate(metrics["pools"]):
        worst, longest = p["worst_team"] or {}, p["longest_trip"] or {}
        w.writerow([p["pool"], p["size"], _fmt(p["km"]),
                    _fmt(optimal["pools"][k]["km"]) if optimal else "", _fmt(current["pools"][k]["km"]) if current else "",
                    worst.get("name", ""), _fmt(worst.get("km", 0)),
                    f"{longest.get('from', '')} > {longest.get('to', '')}", _fmt(longest.get("km", 0))])
    w.writerow(["TOTAL", len(teams), _fmt(metrics["total_km"]),
                _fmt(optimal["total_km"]) if optimal else "", _fmt(current["total_km"]) if current else ""])
    w.writerow([])
    for name, ref in (("version optimale", optimal), ("poules actuelles", current)):
        if ref:
            d = metrics["total_km"] - ref["total_km"]
            w.writerow([f"ecart vs {name}", _signed(d, 1) + " km", _pct(d, ref["total_km"])])
    return "\ufeff" + buf.getvalue()


def matrix_csv(teams: list[Team], matrix: Matrix, kind: str = "distance") -> str:
    """Matrice aller simple (de la ligne vers la colonne), en km ou en minutes."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    unit = "minutes" if kind == "duration" else "km"
    w.writerow([f"{unit} (ligne -> colonne)", "club"] + [t.id for t in teams])
    for i, t in enumerate(teams):
        row = matrix.duration_s[i] if kind == "duration" else matrix.distance_m[i]
        div = 60.0 if kind == "duration" else 1000.0
        w.writerow([t.id, t.name] + [_fmt(v / div) for v in row])
    w.writerow([])
    w.writerow([f"source : {matrix.source}"])
    return "﻿" + buf.getvalue()


def _hex(c: str) -> colors.Color:
    return colors.HexColor(c)


def report_pdf(teams: list[Team], matrix: Matrix, metrics: dict, pools: list[str],
               optimal: dict | None = None, current: dict | None = None, label: str = "Ma version",
               optimal_info: dict | None = None, tiles=None,
               title: str = "Répartition Pré-Nationale Masculine Grand Est") -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm, title=f"{title} : {label}")
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    tiny_style = styles["BodyText"].clone("tiny", fontSize=7, leading=8.5)
    by_id = {t.id: t for t in teams}
    pool_color = {p: _hex(POOL_COLORS[k % len(POOL_COLORS)]) for k, p in enumerate(pools)}
    status = ""
    if optimal_info:
        status = (" Version optimale : optimalité prouvée." if optimal_info.get("proven") else
                  f" Version optimale : meilleure solution trouvée (écart à la borne {_fmt(100 * optimal_info.get('gap', 0), 1)} %).")
    story = [Paragraph(f"{title} : {label}", styles["Title"]),
             Paragraph(f"Édité le {datetime.now():%d/%m/%Y %H:%M}. Matrice : {matrix.source}. "
                       f"Km saison = somme des allers-retours vers chaque adversaire de la poule.{status}", small),
             Spacer(1, 3 * mm)]

    # Comparatif avec la version optimale et les poules actuelles.
    cols = [(label, metrics)] + [(n, m) for n, m in (("Version optimale", optimal), ("Poules actuelles", current)) if m]
    head = ["Indicateur"] + [n for n, _ in cols]
    if optimal:
        head.append("Écart vs optimale")
    if current:
        head.append("Écart vs actuelles")

    def line(name, get, digits=0, unit="km"):
        vals = [get(m) for _, m in cols]
        row = [name] + [_fmt(v, digits) if v is not None else "" for v in vals]
        for ref in (optimal, current):
            if ref:
                d = get(metrics) - get(ref)
                row.append(f"{_signed(d, digits)} {unit}" + (f" ({_pct(d, get(ref))})" if unit == "km" else ""))
        return row

    rows = [head, line("Km cumulés (total)", lambda m: m["total_km"])]
    for k, p in enumerate(pools):
        rows.append(line(f"Poule {p} (km)", lambda m, k=k: m["pools"][k]["km"]))
    rows.append(line("Heures cumulées", lambda m: m["total_hours"], 0, "h"))
    rows.append(line("Km de l'équipe la plus pénalisée", lambda m: (m["worst_team"] or {}).get("km", 0)))
    rows.append(line("Trajet le plus long (km, aller)", lambda m: (m["longest_trip"] or {}).get("km", 0)))
    rows.append(line("Équipes changeant de poule", lambda m: m["moved"], 0, ""))
    tbl = Table(rows, repeatRows=1)
    tbl.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                             ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                             ("BACKGROUND", (1, 1), (1, -1), colors.HexColor("#f5f8ff"))]))
    story += [tbl, Spacer(1, 3 * mm)]
    worst, longest = metrics["worst_team"] or {}, metrics["longest_trip"] or {}
    story.append(Paragraph(
        f"Équipe la plus pénalisée : <b>{worst.get('name', '')}</b> (poule {worst.get('pool', '')}, "
        f"{_fmt(worst.get('km', 0), 0)} km). Trajet le plus long : <b>{longest.get('from', '')}</b> &gt; "
        f"<b>{longest.get('to', '')}</b> ({_fmt(longest.get('km', 0), 0)} km).", small))

    # Carte.
    story += [PageBreak(), Paragraph(f"Carte : {label}", styles["Heading2"])]
    pool_of = {r["id"]: r["pool"] for r in metrics["teams"]}
    pts = [(t.lat, t.lon, pool_of.get(t.id), str(t.id)) for t in teams if t.located]
    if pts:
        story.append(MapFlowable(pts, pools, doc.width, doc.height - 22 * mm, tiles))
        story.append(Paragraph("Chaque poule a sa couleur et sa forme (voir légende). Numéros : identifiants "
                               "des équipes (voir tableaux). Pointillés : enveloppe de chaque poule.", small))

    # Détail par poule, côte à côte.
    story += [PageBreak(), Paragraph(f"Poules et kilométrage par équipe : {label}", styles["Heading2"])]
    opt_team = {r["id"]: r for r in optimal["teams"]} if optimal else {}
    cols_tbl = []
    for p in pools:
        members = sorted((r for r in metrics["teams"] if r["pool"] == p), key=lambda r: -r["km"])
        data = [[f"Poule {p}", "Commune", "Km", "Δ opt.", "Act."]]
        for r in members:
            t = by_id[r["id"]]
            o = opt_team.get(t.id)
            data.append([Paragraph(f"{t.id}. {t.name}", tiny_style), Paragraph(t.commune, tiny_style),
                         _fmt(r["km"], 0), _signed(r["km"] - o["km"], 0) if o else "",
                         t.current_pool + ("*" if r["moved"] else "")])
        pm = next(x for x in metrics["pools"] if x["pool"] == p)
        data.append(["Total poule", "", _fmt(pm["km"], 0), "", ""])
        tb = Table(data, colWidths=[42 * mm, 20 * mm, 11 * mm, 10 * mm, 8 * mm], repeatRows=1)
        tb.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                                ("BACKGROUND", (0, 0), (-1, 0), pool_color[p]),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827")),
                                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        cols_tbl.append(tb)
    if cols_tbl:
        story.append(Table([cols_tbl], style=[("VALIGN", (0, 0), (-1, -1), "TOP"),
                                              ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1)]))
        story.append(Paragraph("* : équipe qui change de poule par rapport à la poule actuelle. "
                               "Δ opt. : écart de kilométrage de l'équipe par rapport à la version optimale.", small))

    # Matrices par poule (km, aller simple).
    idx = {t.id: i for i, t in enumerate(teams)}
    for p in pools:
        ids = [r["id"] for r in sorted(metrics["teams"], key=lambda r: r["id"]) if r["pool"] == p]
        if not ids:
            continue
        story += [PageBreak(), Paragraph(f"Poule {p} : distances routières (km, aller simple)", styles["Heading2"])]
        data = [[""] + [str(i) for i in ids]]
        for a in ids:
            data.append([Paragraph(f"{a}. {by_id[a].name}", small)] +
                        [("" if a == b else _fmt(matrix.distance_m[idx[a]][idx[b]] / 1000, 0)) for b in ids])
        tb = Table(data, colWidths=[70 * mm] + [15 * mm] * len(ids), repeatRows=1)
        tb.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                                ("BACKGROUND", (0, 0), (-1, 0), pool_color[p]),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111827"))]))
        story.append(tb)

    # Matrice complète.
    story += [PageBreak(), Paragraph("Matrice complète des distances routières (km, aller simple)", styles["Heading2"])]
    tiny = 4.6
    data = [["", ""] + [str(t.id) for t in teams]]
    for i, t in enumerate(teams):
        data.append([str(t.id), t.name[:24], *[("" if i == j else _fmt(matrix.distance_m[i][j] / 1000, 0))
                                               for j in range(len(teams))]])
    width = (doc.width - 50 * mm) / max(1, len(teams))
    tb = Table(data, colWidths=[6 * mm, 44 * mm] + [width] * len(teams), repeatRows=1)
    style = [("FONTSIZE", (0, 0), (-1, -1), tiny), ("LEADING", (0, 0), (-1, -1), tiny + 1),
             ("GRID", (0, 0), (-1, -1), 0.2, colors.lightgrey), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
             ("TOPPADDING", (0, 0), (-1, -1), 0.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
             ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1)]
    pool_of = {r["id"]: r["pool"] for r in metrics["teams"]}
    for i, t in enumerate(teams, start=1):
        c = pool_color.get(pool_of.get(t.id))
        if c:
            style.append(("BACKGROUND", (0, i), (0, i), c))
    tb.setStyle(TableStyle(style))
    story.append(tb)
    doc.build(story)
    return buf.getvalue()
