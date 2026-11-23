"""Persistence-level guarantees: reverse-index lookup, snapshots, durability."""

import tempfile
from pathlib import Path

from app.service import GridService
from app.store import Store


def test_upstream_closure_uses_the_reverse_index(tmp_path):
    store = Store(tmp_path / "plan.sqlite3")
    store.conn.execute(
        "INSERT INTO grids (rows, cols, cell_count) VALUES (5, 5, 25)"
    )
    plan = store.conn.execute(
        """
        EXPLAIN QUERY PLAN
        WITH RECURSIVE seed(row, col) AS (
            SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]')
            FROM json_each(:seeds)
        ),
        up(row, col) AS (
            SELECT row, col FROM seed
            UNION
            SELECT c.row, c.col FROM cells c
            JOIN up ON c.downstream_row = up.row AND c.downstream_col = up.col
            WHERE c.grid_id = :grid_id
        )
        SELECT row, col FROM up
        """,
        {"seeds": "[[0, 1]]", "grid_id": 1},
    ).fetchall()
    text = " ".join(row["detail"] for row in plan)
    assert "idx_cells_downstream" in text
    assert "SCAN cells" not in text


def test_reads_see_one_snapshot_while_a_write_commits(tmp_path):
    path = tmp_path / "snap.sqlite3"
    writer = Store(path)
    writer_service = GridService(writer)
    grid_id = writer_service.import_demo()["grid_id"]

    reader = Store(path)
    with reader.read_tx():
        before = reader.conn.execute(
            "SELECT COUNT(*) AS n FROM flow_results WHERE grid_id = ? AND outcome = 'supplied'",
            (grid_id,),
        ).fetchone()["n"]
        writer_service.resurvey(grid_id, [{"row": 1, "col": 2, "value": "N"}])
        during = reader.conn.execute(
            "SELECT COUNT(*) AS n FROM flow_results WHERE grid_id = ? AND outcome = 'supplied'",
            (grid_id,),
        ).fetchone()["n"]
    after = reader.conn.execute(
        "SELECT COUNT(*) AS n FROM flow_results WHERE grid_id = ? AND outcome = 'supplied'",
        (grid_id,),
    ).fetchone()["n"]
    assert before == during == 3
    assert after == 10


def test_data_survives_reopening_and_audit_is_recorded(tmp_path):
    path = tmp_path / "durable.sqlite3"
    first = Store(path)
    service = GridService(first)
    grid_id = service.import_demo()["grid_id"]
    service.resurvey(grid_id, [{"row": 1, "col": 2, "value": "N"}])
    first.close()

    second = Store(path)
    service = GridService(second)
    assert service.supply(grid_id)["supplied_count"] == 10
    assert service.diagnosis(grid_id)["summary"]["cycle_count"] == 0
    audit = second.conn.execute(
        "SELECT row, col, before_direction, after_direction FROM resurvey_changes"
    ).fetchall()
    assert [tuple(a) for a in audit] == [(1, 2, "W", "N")]


def test_failed_write_leaves_database_file_queryable(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite3"
    store = Store(path)
    service = GridService(store)
    grid_id = service.import_demo()["grid_id"]
    before = service.diagnosis(grid_id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "insert_resurvey", boom)
    try:
        service.resurvey(grid_id, [{"row": 1, "col": 2, "value": "N"}])
    except RuntimeError:
        pass

    # Old results are still queryable on this connection and after reopening.
    assert service.diagnosis(grid_id) == before
    store.close()
    reopened = GridService(Store(path))
    assert reopened.diagnosis(grid_id) == before
    assert reopened.store.cell_state(grid_id, (1, 2)) == ("direction", "W")
