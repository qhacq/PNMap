# PNMap : répartition des poules de Pré-Nationale Masculine Grand Est

Application web locale qui répartit les 36 équipes de PNM Grand Est en 3 poules de 12 en
minimisant les déplacements (matchs aller-retour : chaque équipe reçoit et se déplace chez
chacune des 11 autres équipes de sa poule).

- Back : Python, FastAPI, OR-Tools CP-SAT
- Front : Leaflet sur fond OpenStreetMap (Leaflet est embarqué dans `static/vendor`)
- Géocodage : Nominatim (cache local, 1 requête par seconde)
- Distances et durées routières : OSRM (API publique ou Docker), repli haversine x 1,3

## Lancement en une commande

```sh
./run.sh          # Linux / macOS
run.bat           # Windows
```

Le script crée un environnement virtuel `.venv`, installe les dépendances (Python 3.10 ou plus),
démarre le serveur et ouvre <http://127.0.0.1:8000/>.

Au premier lancement, l'application géocode les communes (environ 40 s, une requête par seconde),
puis calcule la matrice des distances. La progression s'affiche en haut à droite. Les lancements
suivants sont instantanés grâce aux caches.

## Utilisation

### Onglet Répartition
- **Carte** : un marqueur coloré par poule, étiquette avec le nom du club (case « Étiquettes »).
  Un clic sur une équipe (carte ou colonne) trace ses trajets vers les adversaires de sa poule.
- **Trois colonnes de poules** en glisser-déposer. Après chaque déplacement, recalcul immédiat :
  km cumulés de la poule, km saison par équipe, écart avec la dernière solution optimisée
  (en vert : moins de km que l'optimum, en rouge : plus).
- **Case 🔒** sur chaque équipe : verrouille l'équipe dans sa poule affichée pour la prochaine optimisation.
- **Optimiser** : relance CP-SAT en respectant verrous, paires et options. **Revenir à la poule
  actuelle** restaure la répartition du CSV. **Dernier optimum** réaffiche la dernière solution calculée.
- **Options** :
  - critère distance (km) ou durée (heures) ;
  - nombre d'équipes par poule (12 par défaut, modifiable) ;
  - équipes d'un même club dans des poules différentes ;
  - **stabilité** : pénalité (en km, ou en minutes si critère durée) par équipe qui change de poule ;
  - **équité** : curseur entre « total des km » et « pire équipe » ;
  - limite de temps (30 s par défaut) : le meilleur résultat trouvé est affiché, avec l'écart à la borne
    si l'optimalité n'est pas prouvée.
- **Paires** imposées (même poule) ou interdites (poules différentes).
- **Indicateurs** : km cumulés par poule, équipe la plus pénalisée, trajet le plus long.
- **Comparatif avant / après** : poules actuelles, répartition affichée, dernier optimum.
- **Exports** : répartition CSV, matrice des distances CSV (km ou minutes), rapport PDF
  (synthèse, poules, matrice par poule et matrice complète).

### Onglet Géocodage
Liste les équipes non géolocalisées (échec Nominatim, commune vide) et celles marquées
`a_verifier = oui`. Pour chacune :
- saisir l'**adresse de la salle** puis « Chercher » ;
- « **Placer sur la carte** » puis cliquer sur la carte (le marqueur est aussi déplaçable) ;
- « **Valider** » confirme la position actuelle ; « Réinitialiser » revient au géocodage de la commune.

Les corrections sont enregistrées dans `data/corrections.json` (à conserver) et la matrice est
recalculée automatiquement.

### Onglet Matrice
Distances (km) ou durées (minutes) aller simple, triées par poule affichée.

## Modèle d'optimisation

Variables : `x[t,p] = 1` si l'équipe `t` est dans la poule `p` (une poule par équipe).

Coût saison d'une équipe `i` : somme, sur les adversaires `j` de sa poule, de l'aller-retour
`r_ij = d(i→j) + d(j→i)`. Le total minimisé est la somme des coûts saison de toutes les équipes,
soit 2 × la somme, sur les paires d'une même poule, de la distance aller-retour.

Objectif : `(1 − a) × total + a × N × max_i(coût_i) + pénalité_stabilité × nb_changements`,
avec `a` le curseur d'équité (0 = total seul, 1 = pire cas seul) et `N` le nombre d'équipes.

Contraintes : taille exacte de chaque poule, équipes d'un même club séparées (le club est le nom
FFBB sans le suffixe « - 1 », « - 2 » ; une colonne `club` facultative dans le CSV le remplace),
verrous, paires interdites ou imposées. Une contrainte de renforcement (chaque équipe de la poule
`p` y a exactement `taille − 1` partenaires) permet à CP-SAT de prouver l'optimalité en une
quinzaine de secondes sur les 36 équipes. Quand les étiquettes de poules sont interchangeables
(pas de verrou ni de stabilité), les poules trouvées sont renommées pour coller au mieux aux
poules actuelles.

## Données et caches

| Fichier | Rôle |
|---|---|
| `data/clubs.csv` | équipes (`id, club_ffbb, poule_actuelle, commune_geocodage, a_verifier`) |
| `data/corrections.json` | positions corrigées ou validées dans l'écran Géocodage |
| `cache/geocode.json` | cache Nominatim (les échecs sont aussi mis en cache ; « Relancer le géocodage » les retente) |
| `cache/matrix.json` | cache OSRM par paire de points (un point déplacé ne recalcule que ce qui manque) |
| `cache/ui_state.json` | dernière répartition affichée, verrous, paires, réglages, dernier optimum |

Supprimer le dossier `cache/` force un nouveau géocodage et un nouveau calcul de matrice.

## Configuration (variables d'environnement)

| Variable | Défaut |
|---|---|
| `PORT` (scripts de lancement) | `8000` |
| `PNMAP_CLUBS_CSV` | `data/clubs.csv` |
| `PNMAP_CORRECTIONS` | `data/corrections.json` |
| `PNMAP_CACHE_DIR` | `cache` |
| `PNMAP_NOMINATIM_URL` | `https://nominatim.openstreetmap.org` |
| `PNMAP_OSRM_URL` | `https://router.project-osrm.org` |
| `PNMAP_USER_AGENT` | `PNMap/1.0 (...)` : à personnaliser avec un contact, comme le demande la politique Nominatim |

## OSRM en local avec Docker (facultatif)

Le serveur public de démonstration OSRM suffit pour 36 équipes (une seule requête `table`, mise
en cache). Pour ne pas en dépendre :

```sh
mkdir osrm && cd osrm
# Extraits Geofabrik des trois anciennes régions, fusionnés avec osmium-tool
wget https://download.geofabrik.de/europe/france/alsace-latest.osm.pbf \
     https://download.geofabrik.de/europe/france/lorraine-latest.osm.pbf \
     https://download.geofabrik.de/europe/france/champagne-ardenne-latest.osm.pbf
osmium merge alsace-latest.osm.pbf lorraine-latest.osm.pbf champagne-ardenne-latest.osm.pbf -o grand-est.osm.pbf
IMG=ghcr.io/project-osrm/osrm-backend
docker run -t -v "$PWD:/data" $IMG osrm-extract -p /opt/car.lua /data/grand-est.osm.pbf
docker run -t -v "$PWD:/data" $IMG osrm-partition /data/grand-est.osrm
docker run -t -v "$PWD:/data" $IMG osrm-customize /data/grand-est.osrm
docker run -d -p 5000:5000 -v "$PWD:/data" $IMG osrm-routed --algorithm mld /data/grand-est.osrm
cd .. && PNMAP_OSRM_URL=http://localhost:5000 ./run.sh
```

Puis bouton « Recalculer la matrice (OSRM) » dans l'onglet Géocodage. Si OSRM est indisponible,
l'application bascule sur la distance haversine × 1,3 (durée estimée à 80 km/h) et l'indique
dans la barre de statut ; ces valeurs de repli ne sont pas mises en cache.

## Tests

```sh
.venv/bin/python -m pytest        # ou : python -m pytest
```

- `tests/test_optimizer.py` : jeu de 6 équipes (Strasbourg, Colmar, Mulhouse / Reims, Châlons,
  Troyes) dont la solution est connue, contrôle par énumération exhaustive, verrous, paires,
  clubs, stabilité, équité, critère durée, cas infaisables.
- `tests/test_api.py` : géocodage et OSRM simulés (aucun accès réseau), cache, écran de
  correction, optimisation, évaluation et exports via l'API.

## Structure

```
app/
  config.py      paramètres
  data.py        lecture du CSV, identification des clubs
  geocode.py     Nominatim + cache + limitation de débit
  distances.py   OSRM table + cache par paire + repli haversine
  optimizer.py   modèle CP-SAT
  metrics.py     indicateurs (km et heures par équipe et par poule)
  exports.py     CSV et PDF
  state.py       état de l'application et tâche de fond
  main.py        API FastAPI
static/          interface (HTML, CSS, JS, Leaflet embarqué)
tests/
```
