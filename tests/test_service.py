"""Service level: incremental resurvey, atomicity, and DB/API consistency."""

import random

import pytest

from app.flow import evaluate_cells
from app.service import ConflictError, GridService, NotFoundError
from app.grid import GridValidationError

DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

DEMO = [
    ["O", "O", "O", "O", "O", "O"],
    ["O", "S", "W", "W", "SE", "O"],
    ["O", "E", "N", "NW", "E", "O"],
    ["O", "?", "W", "N", "NE", "O"],
    ["O", "O", "O", "O", "O", "O"],
]


def full_results(service, grid_id):
    """Recompute every direction cell from scratch, straight from the DB."""
    grid = service.store.get_grid(grid_id)
    rows, cols = grid["rows"], grid["cols"]
    states = service.store.states_for(
        grid_id, [(r, c) for r in range(rows) for c in range(cols)]
    )
    affected = [cell for cell, (kind, _) in states.items() if kind == "direction"]
    return evaluate_cells(
        affected,
        states,
        rows,
        cols,
        states.__getitem__,
        lambda cell: service.store.flow_result(grid_id, cell),
    )


def stored_results(service, grid_id):
    grid = service.store.get_grid(grid_id)
    rows, cols = grid["rows"], grid["cols"]
    states = service.store.states_for(
        grid_id, [(r, c) for r in range(rows) for c in range(cols)]
    )
    out = {}
    for r in range(rows):
        for c in range(cols):
            if states[(r, c)][0] != "direction":
                continue
            result = service.store.flow_result(grid_id, (r, c))
            if result is not None:
                out[(r, c)] = result
    return out


def test_demo_before_and_after_loop_break(service):
    info = service.import_demo()
    grid_id = info["grid_id"]
    before_supply = service.supply(grid_id)
    before = service.diagnosis(grid_id)
    assert before["summary"]["cycle_count"] == 1
    assert before["summary"]["missing_blocked_count"] == 1
    assert before_supply["supplied_count"] == 3

    response = service.resurvey(grid_id, [{"row": 1, "col": 2, "value": "N"}])
    assert response["changed_count"] == 1
    assert response["affected_count"] > 1
    assert response["supplied_count"] > before_supply["supplied_count"]

    after_supply = service.supply(grid_id)
    after = service.diagnosis(grid_id)
    assert after["summary"]["cycle_count"] == 0
    assert after_supply["supplied_count"] == response["supplied_count"]
    assert after_supply["supplied_count"] + after_supply["isolated_count"] == after["summary"][
        "direction_count"
    ]
    # The missing cell still blocks exactly the same cell.
    assert after["summary"]["missing_blocked_count"] == 1


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_incremental_resurvey_matches_full_recomputation(service, seed):
    random.seed(seed)
    for _ in range(25):
        rows, cols = random.randint(2, 6), random.randint(2, 6)
        grid = []
        for r in range(rows):
            line = []
            for c in range(cols):
                x = random.random()
                line.append("O" if x < 0.15 else "?" if x < 0.25 else random.choice(DIRECTIONS))
            grid.append(line)
        if not any(token == "O" for line in grid for token in line):
            grid[0][0] = "O"
        grid_id = service.import_grid(grid)["grid_id"]

        candidates = [
            (r, c) for r in range(rows) for c in range(cols) if grid[r][c] != "O"
        ]
        random.shuffle(candidates)
        batch = candidates[: random.randint(1, min(4, len(candidates)))]
        updates = [
            {"row": r, "col": c, "value": random.choice(DIRECTIONS + ["?"])}
            for r, c in batch
        ]
        service.resurvey(grid_id, updates)
        assert stored_results(service, grid_id) == full_results(service, grid_id)


def test_resurvey_only_recomputes_the_upstream_cone(service):
    # 20 rows x 30 cols: each row drains west; column 0 drains north to (0,0),
    # except the top row which points west straight into the outfall at (0,0).
    rows, cols = 20, 30
    grid = [
        ["O" if (r, c) == (0, 0) else ("W" if c > 0 else "N") for c in range(cols)]
        for r in range(rows)
    ]
    grid_id = service.import_grid(grid)["grid_id"]
    response = service.resurvey(grid_id, [{"row": 12, "col": 20, "value": "E"}])
    # 'W' means col-1, so upstream of (12,20) is the row segment to its east
    # (cols 21..29) plus the changed cell itself.
    expected = {(12, 20)} | {(12, c) for c in range(21, 30)}
    assert response["affected_count"] == len(expected)
    affected = {(cell["row"], cell["col"]) for cell in response["affected_cells"]}
    assert affected == expected
    assert (12, 19) not in affected


def test_resurvey_without_changes_is_a_noop(service):
    grid_id = service.import_grid([["E", "O"]])["grid_id"]
    response = service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "E"}])
    assert response["changed_count"] == 0
    assert response["resurvey_id"] is None
    assert response["affected_count"] == 0


def test_outfalls_are_fixed(service):
    grid_id = service.import_grid([["E", "O"]])["grid_id"]
    with pytest.raises(ConflictError):
        service.resurvey(grid_id, [{"row": 0, "col": 1, "value": "N"}])
    # 'O' is not a legal re-survey value at all.
    with pytest.raises(GridValidationError):
        service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "O"}])


def test_batch_is_validated_before_any_write(service):
    grid_id = service.import_grid([["E", "E", "O"]])["grid_id"]
    before = stored_results(service, grid_id)
    with pytest.raises(GridValidationError):
        service.resurvey(
            grid_id,
            [
                {"row": 0, "col": 0, "value": "?"},
                {"row": 0, "col": 1, "value": "bogus"},
            ],
        )
    # First update must not have been applied.
    assert stored_results(service, grid_id) == before
    assert service.store.cell_state(grid_id, (0, 0)) == ("direction", "E")


def test_database_failure_rolls_back_and_old_result_stays_queryable(service, monkeypatch):
    grid_id = service.import_grid(DEMO)["grid_id"]
    before_supply = service.supply(grid_id)
    before_diag = service.diagnosis(grid_id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(service.store, "insert_resurvey", boom)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        service.resurvey(grid_id, [{"row": 1, "col": 2, "value": "N"}])

    assert service.supply(grid_id) == before_supply
    assert service.diagnosis(grid_id) == before_diag
    # Direction map rolled back too.
    assert service.store.cell_state(grid_id, (1, 2)) == ("direction", "W")
    assert service.store.conn.execute("SELECT COUNT(*) AS n FROM resurveys").fetchone()[
        "n"
    ] == 0


def test_unknown_grid_raises_not_found(service):
    for call in (service.supply, service.diagnosis, service.grid_view):
        with pytest.raises(NotFoundError):
            call(999)


def test_import_requires_a_rectangular_grid_and_an_outfall(service):
    with pytest.raises(GridValidationError):
        service.import_grid([["E", "O"], ["E"]])
    with pytest.raises(GridValidationError):
        service.import_grid([["E", "N"]])
    with pytest.raises(GridValidationError):
        service.import_grid([])


def test_resurvey_to_missing_and_back(service):
    grid_id = service.import_grid([["E", "E", "O"]])["grid_id"]
    service.resurvey(grid_id, [{"row": 0, "col": 1, "value": "?"}])
    diag = service.diagnosis(grid_id)
    assert diag["summary"]["missing_blocked_count"] == 1
    assert service.supply(grid_id)["supplied_count"] == 0
    # Filling the gap back in restores supply.
    service.resurvey(grid_id, [{"row": 0, "col": 1, "value": "E"}])
    assert service.supply(grid_id)["supplied_count"] == 2


def test_grid_view_round_trips_tokens(service):
    grid_id = service.import_grid(DEMO)["grid_id"]
    view = service.grid_view(grid_id)
    assert view["tokens"] == DEMO
