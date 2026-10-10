"""Chargement du fichier des clubs."""
from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_TEAM_NUMBER = re.compile(r"\s*-\s*\d+\s*$")


def club_key(team_name: str) -> str:
    """Identifiant du club : le nom FFBB sans le numéro d'équipe final (" - 1", " - 2")."""
    return re.sub(r"\s+", " ", _TEAM_NUMBER.sub("", team_name)).strip().upper()


@dataclass
class Team:
    id: int
    name: str
    club: str
    current_pool: str
    commune: str
    a_verifier: bool
    adresse: str = ""
    lat: float | None = None
    lon: float | None = None
    geo_label: str = ""
    geo_source: str = ""  # nominatim | adresse | carte | ""
    verified: bool = False

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def needs_review(self) -> bool:
        return not self.located or (self.a_verifier and not self.verified)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["located"] = self.located
        d["needs_review"] = self.needs_review
        return d


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"oui", "o", "yes", "y", "true", "1", "x"}


def load_teams(path: Path) -> list[Team]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        teams: list[Team] = []
        for row in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            if not row.get("id"):
                continue
            name = row["club_ffbb"]
            teams.append(Team(
                id=int(row["id"]),
                name=name,
                club=(row.get("club") or club_key(name)).upper(),
                current_pool=row.get("poule_actuelle", "").upper(),
                commune=row.get("commune_geocodage", ""),
                a_verifier=_truthy(row.get("a_verifier", "")),
                adresse=row.get("adresse_salle", ""),
            ))
    ids = [t.id for t in teams]
    if len(ids) != len(set(ids)):
        raise ValueError("Identifiants d'équipe en double dans le fichier des clubs")
    return teams
