import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("COMPINTEL_DB", os.path.join(tempfile.mkdtemp(), "import.db"))

import main  # noqa: E402
from workflow import db as wdb  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = main.app
    app.config["TESTING"] = True
    app.config["COMPINTEL_DEMO_MODE"] = True
    app.config["COMPINTEL_DB"] = str(tmp_path / "test.db")
    wdb.init_db(app)
    with app.test_client() as c:
        c.get("/app/company")  # creates session + csrf token
        yield c
    app.config["COMPINTEL_DEMO_MODE"] = True
