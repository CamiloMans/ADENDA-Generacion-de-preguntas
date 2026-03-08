from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ["DATABASE_URL"] = f"sqlite:///{(ROOT_DIR / 'test_suite.db').as_posix()}"
os.environ["DATA_DIR"] = str(ROOT_DIR / "test_data" / "jobs")
os.environ["API_KEYS"] = "change-this-key"

from app.db.base import Base
from app.db.session import engine


@pytest.fixture(autouse=True)
def reset_database() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
