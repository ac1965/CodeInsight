from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def c_sample_dir() -> Path:
    return FIXTURES_DIR / "c_sample"


@pytest.fixture
def python_sample_dir() -> Path:
    return FIXTURES_DIR / "python_sample"
