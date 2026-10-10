"""Indicateurs d'une répartition (km et durées en unités réelles)."""
from __future__ import annotations

from .data import Team
from .distances import Matrix


def round_trip_km(matrix: Matrix) -> list[list[float]]:
    n = matrix.size
    d = matrix.distance_m
    return [[(d[i][j] + d[j][i]) / 1000.0 for j in range(n)] for i in range(n)]


def round_trip_h(matrix: Matrix) -> list[list[float]]:
    n = matrix.size
    t = matrix.duration_s
    return [[(t[i][j] + t[j][i]) / 3600.0 for j in range(n)] for i in range(n)]


def integer_costs(matrix: Matrix, metric: str = "distance") -> list[list[int]]:
    """Coûts aller-retour entiers pour le solveur : hectomètres ou dizaines de secondes."""
    n = matrix.size
    src, unit = (matrix.duration_s, 10.0) if metric == "duration" else (matrix.distance_m, 100.0)
    return [[0 if i == j else round((src[i][j] + src[j][i]) / unit) for j in range(n)] for i in range(n)]


def evaluate(teams: list[Team], matrix: Matrix, assignment: dict[int, str], pools: list[str]) -> dict:
    """assignment : id d'équipe -> nom de poule."""
    km = round_trip_km(matrix)
    hours = round_trip_h(matrix)
    idx = {t.id: i for i, t in enumerate(teams)}
    per_team = []
    for t in teams:
        i = idx[t.id]
        pool = assignment.get(t.id)
        mates = [u for u in teams if u.id != t.id and assignment.get(u.id) == pool and pool is not None]
        longest = max(mates, key=lambda u: matrix.distance_m[i][idx[u.id]], default=None)
        per_team.append({
            "id": t.id,
            "name": t.name,
            "pool": pool,
            "current_pool": t.current_pool,
            "moved": pool != t.current_pool,
            "km": round(sum(km[i][idx[u.id]] for u in mates), 1),
            "hours": round(sum(hours[i][idx[u.id]] for u in mates), 2),
            "longest_trip_km": round(matrix.distance_m[i][idx[longest.id]] / 1000.0, 1) if longest else 0.0,
            "longest_trip_to": longest.name if longest else None,
        })

    per_pool = []
    for p in pools:
        members = [r for r in per_team if r["pool"] == p]
        worst = max(members, key=lambda r: r["km"], default=None)
        longest = None
        for r in members:
            if longest is None or r["longest_trip_km"] > longest["km"]:
                longest = {"from": r["name"], "to": r["longest_trip_to"], "km": r["longest_trip_km"]}
        per_pool.append({
            "pool": p,
            "size": len(members),
            "km": round(sum(r["km"] for r in members), 1),
            "hours": round(sum(r["hours"] for r in members), 1),
            "avg_km": round(sum(r["km"] for r in members) / len(members), 1) if members else 0.0,
            "worst_team": {"id": worst["id"], "name": worst["name"], "km": worst["km"]} if worst else None,
            "longest_trip": longest,
        })

    worst = max(per_team, key=lambda r: r["km"], default=None)
    longest = max((p["longest_trip"] for p in per_pool if p["longest_trip"]), key=lambda x: x["km"], default=None)
    return {
        "teams": per_team,
        "pools": per_pool,
        "total_km": round(sum(r["km"] for r in per_team), 1),
        "total_hours": round(sum(r["hours"] for r in per_team), 1),
        "worst_team": {"id": worst["id"], "name": worst["name"], "pool": worst["pool"], "km": worst["km"]} if worst else None,
        "longest_trip": longest,
        "moved": sum(1 for r in per_team if r["moved"]),
        "complete": all(r["pool"] in pools for r in per_team),
    }
