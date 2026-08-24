from pathlib import Path

import pytest

from ragra.db.connection import connect


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()
