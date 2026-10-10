"""Exports CSV et PDF de la répartition et de la matrice des distances."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .data import Team
from .distances import Matrix

POOL_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]


def _fmt(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def assignment_csv(teams: list[Team], metrics: dict) -> str:
    by_id = {t.id: t for t in teams}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["poule", "id", "club_ffbb", "commune", "poule_actuelle", "change_de_poule",
                "km_saison", "heures_saison", "trajet_le_plus_long_km", "vers"])
    for r in sorted(metrics["teams"], key=lambda r: (r["pool"] or "", r["id"])):
        t = by_id[r["id"]]
        w.writerow([r["pool"], t.id, t.name, t.commune, t.current_pool, "oui" if r["moved"] else "non",
                    _fmt(r["km"]), _fmt(r["hours"], 2), _fmt(r["longest_trip_km"]), r["longest_trip_to"] or ""])
    w.writerow([])
    w.writerow(["poule", "nb_equipes", "km_cumules", "heures_cumulees", "km_moyen_par_equipe",
                "equipe_la_plus_penalisee", "km", "trajet_le_plus_long", "km"])
    for p in metrics["pools"]:
        worst, longest = p["worst_team"] or {}, p["longest_trip"] or {}
        w.writerow([p["pool"], p["size"], _fmt(p["km"]), _fmt(p["hours"]), _fmt(p["avg_km"]),
                    worst.get("name", ""), _fmt(worst.get("km", 0)),
                    f"{longest.get('from', '')} > {longest.get('to', '')}", _fmt(longest.get("km", 0))])
    w.writerow(["TOTAL", len(teams), _fmt(metrics["total_km"]), _fmt(metrics["total_hours"])])
    return "﻿" + buf.getvalue()


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
               reference: dict | None = None, title: str = "Répartition Pré-Nationale Masculine Grand Est") -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm, title=title)
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    tiny_style = styles["BodyText"].clone("tiny", fontSize=7, leading=8.5)
    story = [Paragraph(title, styles["Title"]),
             Paragraph(f"Édité le {datetime.now():%d/%m/%Y %H:%M}. Matrice : {matrix.source}. "
                       "Km saison = somme des allers-retours vers chaque adversaire de la poule.", small),
             Spacer(1, 4 * mm)]
    by_id = {t.id: t for t in teams}
    pool_color = {p: _hex(POOL_COLORS[k % len(POOL_COLORS)]) for k, p in enumerate(pools)}

    # Synthèse.
    rows = [["Poule", "Équipes", "Km cumulés", "Heures", "Km moyen", "Équipe la plus pénalisée", "Trajet le plus long"]]
    for p in metrics["pools"]:
        worst, longest = p["worst_team"] or {}, p["longest_trip"] or {}
        rows.append([p["pool"], p["size"], _fmt(p["km"], 0), _fmt(p["hours"], 0), _fmt(p["avg_km"], 0),
                     Paragraph(f"{worst.get('name', '')} ({_fmt(worst.get('km', 0), 0)} km)", small),
                     Paragraph(f"{longest.get('from', '')} &gt; {longest.get('to', '')} "
                               f"({_fmt(longest.get('km', 0), 0)} km)", small)])
    rows.append(["Total", len(teams), _fmt(metrics["total_km"], 0), _fmt(metrics["total_hours"], 0), "", "", ""])
    if reference:
        rows.append(["Référence", "", _fmt(reference["total_km"], 0), _fmt(reference["total_hours"], 0),
                     "", f"Écart : {_fmt(metrics['total_km'] - reference['total_km'], 0)} km", ""])
    tbl = Table(rows, repeatRows=1, colWidths=[18 * mm, 16 * mm, 22 * mm, 16 * mm, 18 * mm, 85 * mm, 102 * mm])
    style = [("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
             ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    for k, p in enumerate(metrics["pools"], start=1):
        style.append(("TEXTCOLOR", (0, k), (0, k), pool_color.get(p["pool"], colors.black)))
    tbl.setStyle(TableStyle(style))
    story += [tbl, Spacer(1, 5 * mm)]

    # Détail par poule, côte à côte.
    cols = []
    for p in pools:
        members = sorted((r for r in metrics["teams"] if r["pool"] == p), key=lambda r: -r["km"])
        data = [[f"Poule {p}", "Commune", "Km", "Act."]]
        for r in members:
            t = by_id[r["id"]]
            data.append([Paragraph(f"{t.id}. {t.name}", tiny_style), Paragraph(t.commune, tiny_style),
                         _fmt(r["km"], 0), t.current_pool + ("*" if r["moved"] else "")])
        tb = Table(data, colWidths=[46 * mm, 22 * mm, 11 * mm, 9 * mm], repeatRows=1)
        tb.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                                ("BACKGROUND", (0, 0), (-1, 0), pool_color[p]),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        cols.append(tb)
    if cols:
        story.append(Table([cols], style=[("VALIGN", (0, 0), (-1, -1), "TOP"),
                                          ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1)]))
        story.append(Paragraph("* : équipe qui change de poule par rapport à la poule actuelle.", small))

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
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]))
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
            style.append(("TEXTCOLOR", (0, i), (1, i), c))
    tb.setStyle(TableStyle(style))
    story.append(tb)
    doc.build(story)
    return buf.getvalue()
