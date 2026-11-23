"""HTTP contract tests through FastAPI's TestClient."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import DEMO_GRID
from app.store import Store


@pytest.fixture()
def client(tmp_path):
    app = create_app(Store(tmp_path / "api.sqlite3"))
    with TestClient(app) as client:
        yield client


def test_health_and_empty_grid_list(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/grids").json() == {"grids": []}


def test_import_supply_diagnosis_and_resurvey_flow(client):
    created = client.post(
        "/grids", json={"tokens": DEMO_GRID, "source_name": "demo"}
    )
    assert created.status_code == 201
    grid_id = created.json()["grid_id"]

    supply = client.get(f"/grids/{grid_id}/supply").json()
    assert sum(o["supplied_count"] for o in supply["outfalls"]) == supply["supplied_count"]

    diagnosis = client.get(f"/grids/{grid_id}/diagnosis").json()
    assert diagnosis["summary"]["cycle_count"] == 1
    cycle_group = next(g for g in diagnosis["groups"] if g["reason"] == "cycle")
    assert len(cycle_group["cycle"]) == 4
    assert len(cycle_group["looping_cells"]) == 4
    missing_group = next(g for g in diagnosis["groups"] if g["reason"] == "missing")
    assert missing_group["missing_cell"] == {"row": 3, "col": 1}
    assert missing_group["stop_cells"] == [{"row": 3, "col": 2}]

    broken = client.post(
        f"/grids/{grid_id}/resurvey",
        json={"updates": [{"row": 1, "col": 2, "value": "N"}]},
    )
    assert broken.status_code == 200
    body = broken.json()
    assert body["changed_count"] == 1
    assert body["supply_transition"]["before"] != body["supply_transition"]["after"]

    after = client.get(f"/grids/{grid_id}/diagnosis").json()
    assert after["summary"]["cycle_count"] == 0
    after_supply = client.get(f"/grids/{grid_id}/supply").json()
    assert after_supply["supplied_count"] == body["supplied_count"]


def test_demo_endpoint(client):
    assert client.post("/grids/demo").status_code == 201


def test_grid_round_trip(client):
    grid_id = client.post("/grids", json={"tokens": DEMO_GRID}).json()["grid_id"]
    view = client.get(f"/grids/{grid_id}").json()
    assert view["tokens"] == DEMO_GRID


def test_error_status_codes(client):
    grid_id = client.post("/grids", json={"tokens": [["E", "O"]]}).json()["grid_id"]
    assert client.get("/grids/999/supply").status_code == 404
    assert client.post("/grids", json={"tokens": [["E", "?"]]}).status_code == 422
    assert client.post("/grids", json={"tokens": [["E", "Q"]]}).status_code == 422
    assert (
        client.post(
            f"/grids/{grid_id}/resurvey",
            json={"updates": [{"row": 0, "col": 1, "value": "N"}]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/grids/{grid_id}/resurvey",
            json={"updates": [{"row": 5, "col": 0, "value": "N"}]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/grids/{grid_id}/resurvey",
            json={"updates": [{"row": 0, "col": 0, "value": "O"}]},
        ).status_code
        == 422
    )
    assert client.post(f"/grids/{grid_id}/resurvey", json={"updates": []}).status_code == 422


def test_openapi_documents_the_resurvey_shape(client):
    spec = client.get("/openapi.json").json()
    assert "/grids/{grid_id}/resurvey" in spec["paths"]
    assert "/grids/{grid_id}/diagnosis" in spec["paths"]
    assert "/grids/{grid_id}/supply" in spec["paths"]
