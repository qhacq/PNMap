"""Tests du solveur sur un jeu de 6 équipes dont la solution est connue."""
import itertools

import pytest

from app.distances import Matrix, haversine_m
from app.metrics import integer_costs
from app.optimizer import OptimizeParams, solve, team_costs

# Deux groupes de 3 villes très éloignés : Strasbourg, Colmar, Mulhouse / Reims, Châlons, Troyes.
COORDS = [
    (48.5734, 7.7521),  # 0 Strasbourg
    (48.0794, 7.3585),  # 1 Colmar
    (47.7508, 7.3359),  # 2 Mulhouse
    (49.2583, 4.0317),  # 3 Reims
    (48.9566, 4.3631),  # 4 Châlons-en-Champagne
    (48.2973, 4.0744),  # 5 Troyes
]
EXPECTED = {frozenset({0, 1, 2}), frozenset({3, 4, 5})}


def make_matrix(coords=COORDS) -> Matrix:
    d = [[haversine_m(a, b) * 1.3 for b in coords] for a in coords]
    return Matrix(d, [[v / 22.2 for v in row] for row in d], "haversine")


@pytest.fixture
def cost():
    return integer_costs(make_matrix())


def groups(assignment):
    out = {}
    for t, p in enumerate(assignment):
        out.setdefault(p, set()).add(t)
    return {frozenset(g) for g in out.values()}


def brute_force(cost, sizes, key):
    """Énumère toutes les répartitions possibles (6 équipes) et renvoie la meilleure valeur de key."""
    n = len(cost)
    best = None
    for perm in set(itertools.permutations([p for p, s in enumerate(sizes) for _ in range(s)])):
        v = key(team_costs(cost, list(perm)))
        best = v if best is None else min(best, v)
    return best


def test_known_solution(cost):
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], time_limit=10, workers=1))
    assert res.status == "OPTIMAL"
    assert groups(res.assignment) == EXPECTED
    assert res.total_cost == brute_force(cost, [3, 3], sum)


def test_total_is_twice_pair_sum(cost):
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], time_limit=10, workers=1))
    pair_sum = sum(cost[i][j] for i in range(6) for j in range(i + 1, 6) if res.assignment[i] == res.assignment[j])
    assert res.total_cost == 2 * pair_sum


def test_pool_sizes(cost):
    res = solve(cost, OptimizeParams(pool_sizes=[2, 2, 2], time_limit=10, workers=1))
    assert res.ok
    assert sorted(res.assignment.count(p) for p in range(3)) == [2, 2, 2]
    assert res.total_cost == brute_force(cost, [2, 2, 2], sum)


def test_lock(cost):
    # Mulhouse verrouillé avec le groupe champenois (poule 1) ; Reims verrouillé en poule 1.
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], locks={2: 1, 3: 1}, time_limit=10, workers=1))
    assert res.assignment[2] == 1 and res.assignment[3] == 1
    assert res.assignment.count(1) == 3


def test_forbidden_pair(cost):
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], forbidden_pairs=[(0, 1)], time_limit=10, workers=1))
    assert res.assignment[0] != res.assignment[1]


def test_imposed_pair(cost):
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], imposed_pairs=[(0, 5)], time_limit=10, workers=1))
    assert res.assignment[0] == res.assignment[5]


def test_same_club_separated(cost):
    clubs = ["SIG", "SIG", "X", "Y", "Z", "W"]
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], clubs=clubs, time_limit=10, workers=1))
    assert res.assignment[0] != res.assignment[1]


def test_infeasible(cost):
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], forbidden_pairs=[(0, 1)], imposed_pairs=[(0, 1)],
                                     time_limit=10, workers=1))
    assert res.status == "INFEASIBLE" and res.assignment is None


def test_invalid_sizes(cost):
    res = solve(cost, OptimizeParams(pool_sizes=[3, 2]))
    assert res.status == "INVALID"


def test_stability(cost):
    current = [0, 0, 1, 1, 1, 0]  # Troyes et Mulhouse sont dans la « mauvaise » poule
    free = solve(cost, OptimizeParams(pool_sizes=[3, 3], current=current, time_limit=10, workers=1))
    assert free.changes == 2
    stuck = solve(cost, OptimizeParams(pool_sizes=[3, 3], current=current, stability_penalty=10**6,
                                       time_limit=10, workers=1))
    assert stuck.assignment == current and stuck.changes == 0


def test_fairness_minimises_worst_team():
    # Configuration où la solution « total » et la solution « pire cas » diffèrent.
    coords = [(47.84, 7.39), (49.41, 5.02), (48.74, 5.8), (49.13, 7.15), (47.73, 4.11), (49.59, 5.73)]
    cost = integer_costs(make_matrix(coords))
    total = solve(cost, OptimizeParams(pool_sizes=[3, 3], time_limit=10, workers=1))
    fair = solve(cost, OptimizeParams(pool_sizes=[3, 3], fairness=1.0, time_limit=10, workers=1))
    assert total.total_cost == brute_force(cost, [3, 3], sum)
    assert fair.max_team_cost == brute_force(cost, [3, 3], max)
    assert fair.max_team_cost < total.max_team_cost
    assert fair.total_cost > total.total_cost
    # Curseur intermédiaire : compromis entre les deux extrêmes.
    mid = solve(cost, OptimizeParams(pool_sizes=[3, 3], fairness=0.5, time_limit=10, workers=1))
    assert fair.max_team_cost <= mid.max_team_cost <= total.max_team_cost


def test_duration_metric():
    cost = integer_costs(make_matrix(), metric="duration")
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], time_limit=10, workers=1))
    assert groups(res.assignment) == EXPECTED


def test_labels_follow_current_pools(cost):
    # Sans stabilité, les étiquettes de poules sont alignées sur les poules actuelles.
    res = solve(cost, OptimizeParams(pool_sizes=[3, 3], current=[1, 1, 1, 0, 0, 0], time_limit=10, workers=1))
    assert res.assignment == [1, 1, 1, 0, 0, 0] and res.changes == 0
