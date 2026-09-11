from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eta_ingest.settings import Settings  # noqa: E402
from eta_ingest.storage import EtaStore  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> EtaStore:
    return EtaStore(tmp_path / "eta.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(username="u", password="k", db_path=tmp_path / "eta.db")
