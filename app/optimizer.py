"""Répartition en poules par programmation par contraintes (OR-Tools CP-SAT).

Coût saison d'une équipe i : somme, sur les équipes j de sa poule, de l'aller-retour
i -> j -> i (r_ij = d_ij + d_ji). Le coût total est la somme des coûts saison de toutes
les équipes, soit 2 x la somme sur les paires d'une même poule de r_ij.

Objectif : (1 - a) * total + a * N * max_i(coût_i) + poids_stabilité * nb_changements
avec a = curseur d'équité (0 = total seul, 1 = pire cas seul).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

SCALE = 100  # précision du curseur d'équité


@dataclass
class OptimizeParams:
    pool_sizes: list[int]
    locks: dict[int, int] = field(default_factory=dict)          # équipe -> poule
    forbidden_pairs: list[tuple[int, int]] = field(default_factory=list)
    imposed_pairs: list[tuple[int, int]] = field(default_factory=list)
    clubs: list[str] | None = None                               # club de chaque équipe
    current: list[int | None] | None = None                      # poule actuelle
    stability_penalty: int = 0  # coût (mêmes unités que la matrice) par équipe qui change de poule
    fairness: float = 0.0       # 0 = minimise le total, 1 = minimise le pire cas
    time_limit: float = 30.0
    workers: int = 8
    hint: list[int] | None = None
    seed: int = 0


@dataclass
class OptimizeResult:
    status: str
    assignment: list[int] | None
    total_cost: int | None = None
    max_team_cost: int | None = None
    changes: int | None = None
    objective: float | None = None
    best_bound: float | None = None
    wall_time: float = 0.0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.assignment is not None


def team_costs(cost: list[list[int]], assignment: list[int]) -> list[int]:
    n = len(assignment)
    return [sum(cost[i][j] for j in range(n) if j != i and assignment[j] == assignment[i]) for i in range(n)]


def precheck(n: int, params: OptimizeParams) -> str | None:
    sizes = params.pool_sizes
    if any(s < 0 for s in sizes):
        return "Taille de poule négative."
    if sum(sizes) != n:
        return f"La somme des tailles de poules ({sum(sizes)}) doit être égale au nombre d'équipes ({n})."
    for t, p in params.locks.items():
        if not (0 <= t < n and 0 <= p < len(sizes)):
            return f"Verrou invalide pour l'équipe {t}."
    for a, b in params.forbidden_pairs + params.imposed_pairs:
        if not (0 <= a < n and 0 <= b < n) or a == b:
            return "Paire invalide."
    return None


def _relabel(assignment: list[int], n_pools: int) -> list[int]:
    """Renumérote les poules par ordre d'apparition (compatible avec la brisure de symétrie)."""
    mapping: dict[int, int] = {}
    for p in assignment:
        if p not in mapping:
            mapping[p] = len(mapping)
    for p in range(n_pools):
        mapping.setdefault(p, len(mapping))
    return [mapping[p] for p in assignment]


def _align_labels(assignment: list[int], reference: list[int | None], n_pools: int) -> list[int]:
    """Permute les étiquettes de poules pour maximiser le nombre d'équipes restant dans leur poule de référence."""
    if n_pools > 7:
        return assignment
    overlap = [[0] * n_pools for _ in range(n_pools)]
    for a, r in zip(assignment, reference):
        if r is not None and 0 <= r < n_pools:
            overlap[a][r] += 1
    best = max(itertools.permutations(range(n_pools)), key=lambda perm: sum(overlap[p][perm[p]] for p in range(n_pools)))
    return [best[a] for a in assignment]


def solve(cost: list[list[int]], params: OptimizeParams) -> OptimizeResult:
    """cost[i][j] : coût aller-retour entier (symétrique) entre i et j."""
    n = len(cost)
    n_pools = len(params.pool_sizes)
    err = precheck(n, params)
    if err:
        return OptimizeResult("INVALID", None, message=err)

    m = cp_model.CpModel()
    x = [[m.NewBoolVar(f"x_{t}_{p}") for p in range(n_pools)] for t in range(n)]
    for t in range(n):
        m.AddExactlyOne(x[t])
    for p, size in enumerate(params.pool_sizes):
        m.Add(sum(x[t][p] for t in range(n)) == size)

    # same[i][j] = 1 si i et j sont dans la même poule.
    same: dict[tuple[int, int], cp_model.IntVar] = {}
    y: dict[tuple[int, int, int], cp_model.IntVar] = {}
    for i in range(n):
        for j in range(i + 1, n):
            for p in range(n_pools):
                v = y[i, j, p] = m.NewBoolVar(f"y_{i}_{j}_{p}")
                m.AddImplication(v, x[i][p])
                m.AddImplication(v, x[j][p])
                m.AddBoolOr([x[i][p].Not(), x[j][p].Not(), v])
            s = m.NewBoolVar(f"s_{i}_{j}")
            m.Add(s == sum(y[i, j, p] for p in range(n_pools)))
            same[i, j] = s
    # Renforcement : une équipe de la poule p y a exactement (taille - 1) partenaires.
    for i in range(n):
        for p, size in enumerate(params.pool_sizes):
            m.Add(sum(y[min(i, j), max(i, j), p] for j in range(n) if j != i) == (size - 1) * x[i][p])

    def pair(i: int, j: int):
        return same[min(i, j), max(i, j)]

    # Contraintes métier.
    for t, p in params.locks.items():
        m.Add(x[t][p] == 1)
    for a, b in params.forbidden_pairs:
        m.Add(pair(a, b) == 0)
    for a, b in params.imposed_pairs:
        m.Add(pair(a, b) == 1)
    if params.clubs:
        for i in range(n):
            for j in range(i + 1, n):
                if params.clubs[i] and params.clubs[i] == params.clubs[j]:
                    m.Add(pair(i, j) == 0)

    # Coûts.
    team_cost = []
    max_pair = max((max(row) for row in cost), default=0)
    upper = max_pair * max(params.pool_sizes, default=1)
    for i in range(n):
        tc = m.NewIntVar(0, upper, f"cost_{i}")
        m.Add(tc == sum(cost[i][j] * pair(i, j) for j in range(n) if j != i))
        team_cost.append(tc)
    total = sum(2 * cost[i][j] * same[i, j] for (i, j) in same)
    worst = m.NewIntVar(0, upper, "worst")
    m.AddMaxEquality(worst, team_cost)

    changes = 0
    if params.current is not None:
        changes = sum(1 - x[t][p] for t, p in enumerate(params.current) if p is not None)

    fairness = min(max(params.fairness, 0.0), 1.0)
    w_total = round(SCALE * (1 - fairness))
    w_worst = round(SCALE * fairness)
    if w_total == 0:
        w_total = 1  # départage les solutions de même pire cas par le total
    objective = w_total * total + w_worst * n * worst
    if params.stability_penalty and params.current is not None:
        objective += SCALE * params.stability_penalty * changes
    m.Minimize(objective)

    # Brisure de symétrie : si les étiquettes de poules sont interchangeables.
    labels_free = (len(set(params.pool_sizes)) == 1 and not params.locks
                   and not (params.stability_penalty and params.current is not None))
    if labels_free and n >= 1:
        m.Add(x[0][0] == 1)
        if n >= 2 and n_pools >= 3:
            for p in range(2, n_pools):
                m.Add(x[1][p] == 0)
    hint = params.hint if params.hint and len(params.hint) == n and all(
        0 <= p < n_pools for p in params.hint) else None
    if hint and labels_free:
        hint = _relabel(hint, n_pools)
    if hint:
        for t, p in enumerate(hint):
            for q in range(n_pools):
                m.AddHint(x[t][q], 1 if q == p else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(params.time_limit))
    solver.parameters.num_workers = max(1, params.workers)
    solver.parameters.random_seed = params.seed
    status = solver.Solve(m)
    name = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        msg = {"INFEASIBLE": "Aucune répartition ne respecte toutes les contraintes "
                             "(verrous, paires, clubs, tailles de poules).",
               "UNKNOWN": "Aucune solution trouvée dans la limite de temps."}.get(name, name)
        return OptimizeResult(name, None, wall_time=solver.WallTime(), message=msg)

    assignment = [next(p for p in range(n_pools) if solver.Value(x[t][p])) for t in range(n)]
    reference = params.current if params.current is not None else params.hint
    if labels_free and reference is not None:
        assignment = _align_labels(assignment, reference, n_pools)
    costs = team_costs(cost, assignment)
    n_changes = None
    if params.current is not None:
        n_changes = sum(1 for t, p in enumerate(params.current) if p is not None and assignment[t] != p)
    return OptimizeResult(
        status=name,
        assignment=assignment,
        total_cost=sum(costs),
        max_team_cost=max(costs) if costs else 0,
        changes=n_changes,
        objective=solver.ObjectiveValue(),
        best_bound=solver.BestObjectiveBound(),
        wall_time=solver.WallTime(),
        message="Solution optimale prouvée." if status == cp_model.OPTIMAL
        else "Meilleure solution trouvée dans la limite de temps (optimalité non prouvée).",
    )
