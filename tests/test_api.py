"""Tests de l'API, du géocodage et de la matrice, sans accès réseau."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.data import club_key, load_teams
from app.distances import DistanceProvider
from app.geocode import Geocoder
from app.main import create_app
from app.pdfmap import TileFetcher
from app.state import AppState

CITIES = {
    "strasbourg": (48.5734, 7.7521), "colmar": (48.0794, 7.3585), "mulhouse": (47.7508, 7.3359),
    "reims": (49.2583, 4.0317), "châlons-en-champagne": (48.9566, 4.3631), "troyes": (48.2973, 4.0744),
}
CSV = """id,club_ffbb,poule_actuelle,commune_geocodage,a_verifier
1,SIG STRASBOURG - 1,A,Strasbourg,non
2,COLMAR BASKET,B,Colmar,oui
3,FC MULHOUSE BASKET,A,Mulhouse,non
4,REIMS BASKET,B,Reims,non
5,CHALONS BC,A,Châlons-en-Champagne,non
6,SIG STRASBOURG - 2,B,,oui
"""


def fake_nominatim(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        q = (request.url.params.get("city") or request.url.params.get("q") or "").split(",")[0].strip().lower()
        if q in CITIES:
            lat, lon = CITIES[q]
            return httpx.Response(200, json=[{"lat": str(lat), "lon": str(lon), "display_name": q.title()}])
        return httpx.Response(200, json=[])
    return httpx.MockTransport(handler)


def osrm_down(request):
    return httpx.Response(503)


def png_tile():
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (256, 256), (200, 220, 240)).save(out, format="PNG")
    return out.getvalue()


def fake_tiles(calls):
    data = png_tile()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=data, headers={"Content-Type": "image/png"})
    return httpx.MockTransport(handler)


@pytest.fixture
def state(tmp_path):
    csv = tmp_path / "clubs.csv"
    csv.write_text(CSV, encoding="utf-8")
    settings = Settings(clubs_csv=csv, corrections_file=tmp_path / "corrections.json", cache_dir=tmp_path / "cache",
                        communes_file=tmp_path / "communes.json")
    calls = []
    geo = Geocoder(settings.geocode_cache, "http://nominatim.test", "PNMap-test/1.0", min_interval=0,
                   client=httpx.Client(transport=fake_nominatim(calls)))
    dist = DistanceProvider(settings.matrix_cache, "http://osrm.test", client=httpx.Client(transport=httpx.MockTransport(osrm_down)))
    tile_calls = []
    tiles = TileFetcher(settings.tiles_dir, "http://tiles.test/{z}/{x}/{y}.png", "PNMap-test/1.0",
                        client=httpx.Client(transport=fake_tiles(tile_calls)))
    s = AppState(settings, geocoder=geo, distances=dist, tiles=tiles)
    s.calls = calls
    s.tile_calls = tile_calls
    return s


@pytest.fixture
def client(state):
    app = create_app(state, autostart=False)
    with TestClient(app) as c:
        state.start_job()
        state.wait_job(10)
        yield c


def test_club_key():
    assert club_key("VANDOEUVRE BASKETBALL - 1") == "VANDOEUVRE BASKETBALL"
    assert club_key("IE - CTC T-A-A-C BASKET - ENERGIE TROYENNE") == "IE - CTC T-A-A-C BASKET - ENERGIE TROYENNE"
    assert club_key("SAINT JOSEPH - STRASBOURG - 1") == "SAINT JOSEPH - STRASBOURG"


def test_load_real_csv():
    teams = load_teams(Settings().clubs_csv)
    assert len(teams) == 36
    assert {t.current_pool for t in teams} == {"A", "B", "C"}
    assert sum(t.a_verifier for t in teams) == 8
    assert next(t for t in teams if t.id == 30).adresse.startswith("66 route de Mulhouse")


def test_geocoding_and_cache(state, client):
    s = state
    assert s.team(1).located and s.team(1).geo_source == "nominatim"
    assert not s.team(6).located  # commune vide
    assert s.calls and all(r.headers["User-Agent"] == "PNMap-test/1.0" for r in s.calls)
    n = len(s.calls)
    s.geocode_all()  # tout est en cache, échecs compris : aucun appel réseau
    assert len(s.calls) == n
    cache = json.loads(s.settings.geocode_cache.read_text(encoding="utf-8"))
    assert cache["commune:strasbourg"]["lat"] == pytest.approx(48.5734)


def test_review_list_and_correction(state, client):
    st = client.get("/api/state").json()
    review = {t["id"] for t in st["teams"] if t["needs_review"]}
    assert review == {2, 6}  # Colmar à vérifier, équipe 6 sans commune
    assert st["matrix_ready"] and "SIG STRASBOURG - 2" in st["matrix_error"]  # non bloquant, mais signalé

    r = client.post("/api/teams/6/location", json={"address": "Troyes"})
    assert r.status_code == 200 and r.json()["geo_source"] == "adresse"
    state.wait_job(10)
    assert client.post("/api/teams/2/validate").json()["verified"] is True
    st = client.get("/api/state").json()
    assert not any(t["needs_review"] for t in st["teams"])
    assert st["matrix_ready"] and st["matrix_source"] == "haversine"  # OSRM simulé en panne
    assert json.loads(state.settings.corrections_file.read_text(encoding="utf-8"))["6"]["source"] == "adresse"

    r = client.post("/api/teams/6/location", json={"lat": 48.3, "lon": 4.08})
    assert r.json()["geo_source"] == "carte"


def ready(client, state):
    client.post("/api/teams/6/location", json={"address": "Troyes"})
    state.wait_job(10)


def test_optimize_evaluate_export(state, client):
    ready(client, state)
    r = client.post("/api/optimize", json={"pool_sizes": {"A": 3, "B": 3}, "time_limit": 5})
    body = r.json()
    assert body["status"] == "OPTIMAL", body
    a = {int(k): v for k, v in body["assignment"].items()}
    # Les deux équipes de la SIG (1 et 6) sont séparées malgré la proximité géographique.
    assert a[1] != a[6]
    m = body["metrics"]
    assert m["complete"] and len(m["pools"]) == 2
    assert m["worst_team"]["km"] == max(t["km"] for t in m["teams"])

    ev = client.post("/api/evaluate", json={"assignment": body["assignment"]}).json()
    assert ev["total_km"] == m["total_km"]

    current = {t["id"]: t["current_pool"] for t in client.get("/api/state").json()["teams"]}
    cur = client.post("/api/evaluate", json={"assignment": current}).json()
    assert cur["total_km"] >= m["total_km"] and cur["moved"] == 0

    csv = client.post("/api/export/assignment.csv", json={"assignment": body["assignment"]})
    assert csv.status_code == 200 and "km_saison" in csv.text
    mx = client.post("/api/export/matrix.csv", json={"assignment": body["assignment"]})
    assert mx.text.count("\n") >= 7
    pdf = client.post("/api/export/report.pdf", json={"assignment": current, "label": "Poules actuelles"})
    assert pdf.content[:4] == b"%PDF"
    assert pdf.headers["content-disposition"].endswith('repartition_poules_actuelles.pdf"')
    assert state.tile_calls  # fond de carte OSM téléchargé...
    n = len(state.tile_calls)
    client.post("/api/export/report.pdf", json={"assignment": current})
    assert len(state.tile_calls) == n  # ...puis servi par le cache local


def test_optimize_with_lock_and_stability(state, client):
    ready(client, state)
    r = client.post("/api/optimize", json={
        "pool_sizes": {"A": 3, "B": 3}, "locks": {"4": "A"}, "separate_clubs": False,
        "forbidden_pairs": [[1, 2]], "stability_weight": 0, "time_limit": 5}).json()
    assert r["assignment"]["4"] == "A" and r["assignment"]["1"] != r["assignment"]["2"]
    r = client.post("/api/optimize", json={"pool_sizes": {"A": 3, "B": 3}, "stability_weight": 100000,
                                           "time_limit": 5}).json()
    assert r["metrics"]["moved"] == 0


def test_errors(state, client):
    ready(client, state)
    assert client.post("/api/evaluate", json={"assignment": {"99": "A"}}).status_code == 422
    r = client.post("/api/optimize", json={"pool_sizes": {"A": 4, "B": 4}}).json()
    assert r["status"] == "INVALID"


def test_osrm_used_and_cached(tmp_path):
    calls = []

    def osrm(request):
        calls.append(request)
        n = len(request.url.path.rsplit("/", 1)[1].split(";"))
        return httpx.Response(200, json={"code": "Ok",
                                         "distances": [[0 if i == j else 1000.0 * (i + j) for j in range(n)] for i in range(n)],
                                         "durations": [[0 if i == j else 60.0 for j in range(n)] for i in range(n)]})
    dp = DistanceProvider(tmp_path / "m.json", "http://osrm.test", client=httpx.Client(transport=httpx.MockTransport(osrm)))
    coords = [(48.0, 7.0), (48.1, 7.1), (48.0, 7.0), (49.0, 4.0)]  # deux équipes au même point
    m = dp.matrix(coords)
    assert m.source == "osrm" and len(calls) == 1
    assert m.distance_m[0][2] == 0 and m.distance_m[0][1] == 1000.0
    dp2 = DistanceProvider(tmp_path / "m.json", "http://osrm.test", client=httpx.Client(transport=httpx.MockTransport(osrm)))
    assert dp2.matrix(coords).distance_m == m.distance_m and len(calls) == 1  # servi par le cache


def test_optimal_computed_at_startup_and_persisted(state, client):
    first = client.get("/api/optimal").json()["optimal"]
    assert first is not None  # calculée dès le lancement, même avec une équipe non localisée
    ready(client, state)  # la localisation corrigée relance matrice + optimisation initiale
    assert client.get("/api/optimal").json()["optimal"]["matrix_key"] != first["matrix_key"]
    st = client.get("/api/state").json()
    opt = st["optimal"]
    assert st["optimal_fresh"] and opt["status"] == "OPTIMAL" and opt["proven"]
    assert opt["params"]["pool_sizes"] == {"A": 3, "B": 3}
    a = {int(k): v for k, v in opt["assignment"].items()}
    assert a[1] != a[6]
    assert json.loads(state.settings.optimal_file.read_text(encoding="utf-8"))["total_km"] == opt["total_km"]

    # Optimisation relancée avec un verrou : la référence est remplacée.
    r = client.post("/api/optimize", json={"pool_sizes": {"A": 3, "B": 3}, "locks": {"4": "A", "1": "A"},
                                           "time_limit": 5}).json()
    assert r["optimal"]["params"]["locks"] == {"4": "A", "1": "A"}
    assert client.get("/api/optimal").json()["optimal"]["assignment"]["4"] == "A"

    # Rechargement : l'optimum stocké est réutilisé tant que la matrice ne change pas.
    s2 = AppState(state.settings, geocoder=state.geocoder, distances=state.distances, tiles=state.tiles)
    s2.compute_matrix()
    assert s2.optimal_is_fresh and s2.optimal_assignment()[4] == "A"


def test_csv_compares_with_optimal(state, client):
    ready(client, state)
    current = {t["id"]: t["current_pool"] for t in client.get("/api/state").json()["teams"]}
    csv = client.post("/api/export/assignment.csv", json={"assignment": current, "label": "Poules actuelles"}).text
    assert "poule_version_optimale" in csv and "ecart vs version optimale" in csv


def test_unlocated_team_does_not_block(state, client):
    # L'équipe 6 n'a pas de commune : les km des autres s'affichent quand même.
    st = client.get("/api/state").json()
    assert st["matrix_ready"]
    assert "SIG STRASBOURG - 2" in st["matrix_error"]
    m = client.get("/api/matrix").json()
    assert all(v == 0 for v in m["km"][5])  # équipe 6 : 0 km, signalée
    current = {t["id"]: t["current_pool"] for t in st["teams"]}
    ev = client.post("/api/evaluate", json={"assignment": current}).json()
    assert ev["total_km"] > 0


def test_bundled_coordinates_used_without_network(tmp_path):
    csv = tmp_path / "clubs.csv"
    csv.write_text(CSV, encoding="utf-8")
    (tmp_path / "communes.json").write_text(json.dumps({"_note": "x", "Colmar": [48.08, 7.36], "Strasbourg": [48.57, 7.75]}), encoding="utf-8")
    settings = Settings(clubs_csv=csv, corrections_file=tmp_path / "c.json", cache_dir=tmp_path / "cache",
                        communes_file=tmp_path / "communes.json")

    def down(request):
        raise httpx.ConnectError("hors ligne")
    geo = Geocoder(settings.geocode_cache, "http://nominatim.test", "ua", min_interval=0,
                   client=httpx.Client(transport=httpx.MockTransport(down)))
    s = AppState(settings, geocoder=geo, distances=DistanceProvider(
        settings.matrix_cache, "http://osrm.test", client=httpx.Client(transport=httpx.MockTransport(osrm_down))))
    assert s.team(2).located and s.team(2).geo_source == "approx"  # avant tout appel réseau
    s.start_job(optimize=False)
    s.wait_job(10)
    assert s.team(2).geo_source == "approx" and s.matrix is not None
