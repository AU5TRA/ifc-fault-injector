import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

#: The real models live outside the repo (hundreds of MB). Tests that need one
#: skip when it is absent, so the pure-logic suite still runs anywhere - on a
#: CI box, on a fresh clone, on a machine that never had the corpus.
DEFAULT_SOURCE_ROOT = Path(r"D:\Real World BIMs\models")


def source_root() -> Path:
    return Path(os.environ.get("IFCFAULT_SOURCE_ROOT", DEFAULT_SOURCE_ROOT))


@pytest.fixture(scope="session")
def arc_model_path() -> Path:
    path = source_root() / "dental_clinic" / "arc.ifc"
    if not path.exists():
        pytest.skip(f"no architectural source model at {path}")
    return path


@pytest.fixture(scope="session")
def str_model_path() -> Path:
    path = source_root() / "dental_clinic" / "str.ifc"
    if not path.exists():
        pytest.skip(f"no structural source model at {path}")
    return path
