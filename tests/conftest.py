import datetime as dt
import json
from pathlib import Path

import pytest

from intheknow.context import ContextEngine

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
TODAY = dt.date(2026, 11, 1)
SETUP = {
    "salon_aurora": dict(name="Salon Aurora", vertical="salon", locale="ro", currency="RON", country="RO"),
    "kine_saone": dict(name="Cabinet Kiné Saône", vertical="clinic", locale="fr", currency="EUR", country="FR"),
    "kaffeehaus": dict(name="Kaffeehaus Mitte", vertical="cafe", locale="de", currency="EUR", country="DE"),
}


def truth(name: str) -> dict:
    return json.loads((DEMO / f"{name}.truth.json").read_text())


def loaded(tmp_path, name: str) -> ContextEngine:
    e = ContextEngine(tmp_path / f"{name}.db", TODAY)
    e.call_view("setup_business", SETUP[name])
    e.call_view("add_data", {"paths": [str(DEMO / name)]})
    return e


@pytest.fixture(scope="session")
def engines(tmp_path_factory):
    base = tmp_path_factory.mktemp("dbs")
    return {name: loaded(base, name) for name in SETUP}
