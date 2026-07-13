"""Shared test fixtures for Porto tests."""

import shutil
from pathlib import Path

import pytest

# Project root
PROJECT_ROOT = Path(__file__).parent.parent

# Seeding directory – scripts can use this as porto_home in tests
PORTO_HOME_SEED = PROJECT_ROOT / "tests" / "porto_home"


@pytest.fixture()
def porto_home(tmp_path: Path) -> Path:
    """Provide an isolated porto_home directory for each test.

    Copies the seed data from tests/porto_home/ into a fresh tmp dir so tests
    never pollute each other or the repo.
    """
    home = tmp_path / "porto_home"
    if PORTO_HOME_SEED.exists():
        shutil.copytree(PORTO_HOME_SEED, home)
    else:
        home.mkdir(parents=True)
    (home / "workflows").mkdir(exist_ok=True)
    return home
