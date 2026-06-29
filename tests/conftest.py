import pathspec
import pytest

from cerebro.config import Config, DEFAULT_IGNORES
from cerebro import db


def make_config(root):
    spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_IGNORES)
    return Config(root=root, db_path=root / ".cerebro" / "brain.db", spec=spec)


@pytest.fixture
def project(tmp_path):
    """A Config + connection rooted at an isolated tmp dir (no env/git leakage)."""
    config = make_config(tmp_path)
    conn = db.connect(config.db_path)
    return config, conn
