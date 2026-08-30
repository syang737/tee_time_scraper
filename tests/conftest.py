import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402


@pytest.fixture(autouse=True)
def no_request_delay(monkeypatch):
    """Politeness spacing is for the real API; it only slows tests down."""
    monkeypatch.setattr(config, "REQUEST_DELAY_SECONDS", 0.0)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """A fresh SQLite database per test."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return config.DB_PATH
