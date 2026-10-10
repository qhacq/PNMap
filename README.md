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

Alternatives si les dépendances sont déjà installées (`pip install -r requirements.txt`) :
`uvicorn app.main:app --port 8000` ou `fastapi run app/main.py`.

Le script crée un environnement virtuel `.venv`, installe les dépendances (Python 3.10 ou plus),
démarre le serveur et ouvre <http://127.0.0.1:8000/>.

Les kilomètres s'affichent dès l'ouverture : l'application part des positions approximatives des
communes livrées dans `data/communes.json`, puis les affine en arrière-plan avec Nominatim (environ 40 s
au premier lancement, une requête par seconde) et calcule la version optimale. La progression s'affiche
en haut à droite. La version optimale est calculée dans la foulée (une quinzaine de secondes) et devient alors
cliquable. Une équipe sans localisation ne bloque rien : elle est signalée et compte 0 km tant qu'on
n'a pas indiqué sa salle dans l'onglet Géocodage.

L'écran principal reste simple : un tableau compare, pour chaque poule et au total, les km des
rencontres aller-retour des trois répartitions (poules actuelles, version optimale, Ma version), avec
l'écart par rapport aux poules actuelles. Le lien « rencontres » d'une poule détaille chaque paire
d'équipes (distance et km des deux rencontres). Viennent ensuite la carte et les trois poules avec
les km de chaque équipe.
Le reste est rangé dans « Outils avancés ».

## Déploiement sur Vercel

L'application fonctionne aussi sur Vercel (projet relié au dépôt GitHub). Elle détecte
automatiquement l'environnement serverless et s'y adapte :

- le code est en lecture seule : les caches vont dans `/tmp/pnmap` (temporaire) ;
- aucune tâche de fond : à l'ouverture, les positions viennent de `data/communes.json` et des
  adresses du CSV, et les distances routières sont demandées à OSRM pendant la requête
  (quelques secondes, puis gardées en mémoire tant que l'instance vit) ;
- la version optimale est demandée par la page elle-même (20 s par défaut) ; la page reste
  utilisable pendant le calcul ;
- Ma version, l'historique, les verrous et la dernière version optimale sont enregistrés dans
  le navigateur (localStorage), pas sur le serveur ;
- `vercel.json` autorise 60 s par requête pour laisser le temps au calcul.

Sur Vercel, les corrections faites dans l'onglet Géocodage ne sont pas conservées d'une instance
à l'autre : pour une correction durable, renseigner `commune_geocodage` ou `adresse_salle` dans
`data/clubs.csv`.

## Utilisation

### Onglet Répartition

**Trois versions côte à côte**, toujours disponibles :
- **Poules actuelles** : A, B, C telles que dans `clubs.csv` ;
- **Version optimale** : résultat du solveur, calculé automatiquement au lancement (puis conservé dans
  `cache/optimal.json`), recalculable avec le bouton **Optimiser**. Elle reste la référence, avec son
  kilométrage total et son statut (« optimale prouvée » ou « meilleure trouvée », écart à la borne) ;
- **Ma version** : copie modifiable, initialisée au choix depuis les poules actuelles ou la version optimale.

Chaque carte de version indique ses km cumulés, l'écart en km et en % avec les deux autres, et les km par poule.

**Carte OpenStreetMap**
- Une couleur **et une forme** par poule (cercle bleu, carré orange, triangle vert) : palette vérifiée pour
  les daltoniens, la forme lève toute ambiguïté. Nom du club à côté de chaque marqueur (case « Noms »).
- Sélecteur de version affichée (poules actuelles / optimale / Ma version) ; un échange change
  immédiatement la couleur et la forme des marqueurs.
- Liaisons au choix : aucune, enveloppe convexe de chaque poule, ou étoile autour du barycentre.
- Survol d'un marqueur : commune, poule, kilométrage annuel, trajet le plus long. Clic : sélection de
  l'équipe et tracé de ses trajets.

**Fiche club (double-clic)** : un double-clic sur une équipe (dans sa poule ou sur la carte) ouvre sa
fiche pour changer son **nom** ou l'**adresse de sa salle**. La nouvelle adresse est géocodée, les distances
routières et la version optimale sont recalculées. Ces modifications sont enregistrées dans le navigateur
(« Revenir aux données d'origine » les annule) et reprises dans les exports. Pour les rendre définitives
pour tout le monde, les reporter dans `data/clubs.csv`.

**Modifier Ma version**
- Glisser une équipe vers une autre poule, ou utiliser son menu **⇄**. Comme chaque poule garde son
  effectif, une fenêtre propose l'**échange** avec chaque équipe de la poule d'arrivée et affiche, avant
  validation, l'impact sur le total (km et %), sur les deux équipes (avant / après) et sur les deux poules.
  Déposer l'équipe sur une carte précise présélectionne cette équipe comme partenaire.
- Panneau **Impact** : variation du total par rapport à la version optimale et aux poules actuelles (km et %),
  km de chaque poule, détail du dernier échange (avant / après), équipe la plus pénalisée, trajet le plus long.
  Les km de chaque équipe et leur écart avec la version optimale sont sur les cartes d'équipe.
- **Historique** avec Annuler / Rétablir (boutons, Ctrl+Z, Ctrl+Y ou Ctrl+Maj+Z), conservé entre deux sessions.
- **🔒** verrouille une équipe dans sa poule de Ma version : **Optimiser** optimise alors le reste, et le
  résultat remplace la version optimale de référence.
- Tableau des **10 meilleurs échanges** de deux équipes (hors équipes verrouillées) à partir de Ma version,
  avec le gain et un bouton « Appliquer ».

**Réglages de l'optimisation** : critère distance (km) ou durée (heures) ; nombre d'équipes par poule
(12 par défaut) ; équipes d'un même club séparées ; **stabilité** (pénalité en km, ou en minutes, par équipe
qui change de poule) ; **équité** (curseur entre « total des km » et « pire équipe ») ; limite de temps
(30 s par défaut) ; **paires** imposées (même poule) ou interdites.

**Exports** de la version choisie :
- CSV : poules, km par équipe, poule et km dans la version optimale, écart, synthèse par poule et écarts
  totaux avec la version optimale et les poules actuelles ;
- PDF : comparatif avec la version optimale et les poules actuelles, **carte** (fond OpenStreetMap mis en
  cache dans `cache/tiles`, ou fond neutre hors ligne), poules et km par équipe, matrices des distances ;
- matrice des distances (km) ou des durées (minutes) en CSV.

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
| `data/clubs.csv` | équipes (`id, club_ffbb, poule_actuelle, commune_geocodage, a_verifier`, et `adresse_salle` facultative : si elle est remplie, c'est elle qui est géocodée) |
| `data/communes.json` | positions approximatives des communes, utilisées tant que Nominatim n'a pas répondu |
| `data/corrections.json` | positions corrigées ou validées dans l'écran Géocodage |
| `cache/geocode.json` | cache Nominatim (les échecs sont aussi mis en cache ; « Relancer le géocodage » les retente) |
| `cache/matrix.json` | cache OSRM par paire de points (un point déplacé ne recalcule que ce qui manque) |
| `cache/optimal.json` | version optimale de référence (recalculée si la matrice change) |
| `cache/tiles/` | tuiles OpenStreetMap utilisées pour la carte du PDF |
| `cache/ui_state.json` | Ma version, historique, verrous, paires, réglages, version affichée |

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
| `PNMAP_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` (fond de carte du PDF) |
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
- `tests/test_api.py` : géocodage, OSRM et tuiles simulés (aucun accès réseau), cache, écran de
  correction, version optimale calculée au lancement et conservée, optimisation avec verrous,
  évaluation et exports (CSV avec comparatif, PDF avec carte) via l'API.

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
  pdfmap.py      carte du PDF (tuiles OSM en cache, enveloppes, marqueurs)
  state.py       état de l'application et tâche de fond
  main.py        API FastAPI
static/          interface (HTML, CSS, JS, Leaflet embarqué)
tests/
```
