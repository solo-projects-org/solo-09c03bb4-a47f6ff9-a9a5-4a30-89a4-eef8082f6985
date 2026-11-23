import pytest

from app.service import GridService
from app.store import Store


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    yield store
    store.close()


@pytest.fixture()
def service(store):
    return GridService(store)
