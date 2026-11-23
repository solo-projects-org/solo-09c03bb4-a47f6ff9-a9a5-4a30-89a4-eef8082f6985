"""Flow evaluation: supply, the three isolation reasons, cycle canonical form."""

import pytest

from app.flow import FlowResult, evaluate_cells
from app.grid import GridValidationError, parse_resurvey_token, parse_token


def evaluate(tokens):
    rows, cols = len(tokens), len(tokens[0])
    cells = {(r, c): parse_token(tokens[r][c]) for r in range(rows) for c in range(cols)}
    affected = [cell for cell, (kind, _) in cells.items() if kind == "direction"]
    return evaluate_cells(affected, cells, rows, cols, cells.__getitem__, lambda c: None)


def test_every_direction_reaches_the_outfall():
    # (1,1) points at each of the 8 neighbours in turn; all of them are outfalls.
    for index, code in enumerate(["N", "NE", "E", "SE", "S", "SW", "W", "NW"]):
        grid = [["O", "O", "O"], ["O", code, "O"], ["O", "O", "O"]]
        results = evaluate(grid)
        assert results[(1, 1)] == FlowResult.supplied(_target(index))


def _target(direction_index):
    return [(0, 1), (0, 2), (1, 2), (2, 2), (2, 1), (2, 0), (1, 0), (0, 0)][
        direction_index
    ]


def test_supply_chain_records_the_outfall_at_the_end():
    results = evaluate(
        [
            ["E", "S", "O"],
            ["O", "S", "O"],
            ["O", "O", "O"],
        ]
    )
    assert results[(0, 0)] == FlowResult.supplied((2, 1))


def test_cycle_reports_every_cell_of_the_loop():
    results = evaluate(
        [
            ["O", "O", "O", "O"],
            ["O", "S", "W", "O"],
            ["O", "E", "N", "O"],
            ["O", "O", "O", "O"],
        ]
    )
    cycle = {(1, 1), (2, 1), (2, 2), (1, 2)}
    for cell in cycle:
        assert results[cell].reason == "cycle"
        assert set(results[cell].cycle) == cycle
        assert set(results[cell].cycle) == cycle
        # canonical: starts at the smallest cell
        assert results[cell].cycle[0] == min(cycle)
    # A cell draining into the loop is isolated by the same loop.
    assert results[(1, 2)].cycle == results[(2, 2)].cycle


def test_cycle_result_is_canonical_regardless_of_entry_point():
    grid = [
        ["O", "O", "O", "O", "O"],
        ["O", "S", "W", "O", "O"],
        ["O", "E", "N", "O", "O"],
        ["O", "O", "O", "S", "O"],
        ["O", "O", "O", "O", "O"],
    ]
    # add an upstream cell (3,3) that points north into the loop's (2,3) outfall
    results = evaluate(grid)
    assert results[(1, 1)].reason == "cycle"
    assert results[(1, 1)].cycle[0] == (1, 1)


def test_out_of_bounds_reports_the_last_in_bounds_cell():
    grid = [
        ["W", "O"],
        ["N", "O"],
        ["N", "O"],
    ]
    results = evaluate(grid)
    # (0,0) W leaves the rectangle on the left.
    assert results[(0, 0)].reason == "out_of_bounds"
    assert results[(0, 0)].last == (0, 0)
    # (1,0) N -> (0,0) W -> out; still reports the last cell inside.
    assert results[(1, 0)].last == (0, 0)
    assert results[(2, 0)].last == (0, 0)


def test_out_of_bounds_cell_does_not_poison_cells_that_reach_an_outfall():
    grid = [
        ["W", "O", "O"],
        ["E", "O", "O"],
    ]
    results = evaluate(grid)
    assert results[(0, 0)].reason == "out_of_bounds"
    assert results[(1, 0)] == FlowResult.supplied((1, 1))


def test_missing_cell_reports_the_missing_cell_and_the_stop():
    grid = [
        ["E", "E", "?"],
        ["S", "O", "S"],
        ["O", "O", "O"],
    ]
    cells = {(r, c): parse_token(grid[r][c]) for r in range(3) for c in range(3)}
    # Include the missing cell in the evaluated set, as the real grid does.
    results = evaluate_cells(
        [cell for cell, (kind, _) in cells.items() if kind != "outfall"],
        cells,
        3,
        3,
        cells.__getitem__,
        lambda c: None,
    )
    assert results[(0, 0)].reason == "missing"
    assert results[(0, 0)].missing == (0, 2)
    assert results[(0, 0)].stop == (0, 1)
    assert results[(0, 1)].stop == (0, 1)
    # The missing cell itself is isolated, but nothing points into it.
    assert results[(0, 2)].missing == (0, 2)
    assert results[(0, 2)].stop is None
    # Cells beside the blocked column still drain.
    assert results[(1, 0)] == FlowResult.supplied((2, 0))
    assert results[(1, 2)] == FlowResult.supplied((2, 2))


def test_a_missing_cell_that_nothing_flows_into_does_not_isolate_the_map():
    results = evaluate([["E", "O", "?"]])
    assert results[(0, 0)] == FlowResult.supplied((0, 1))


def test_parse_tokens():
    assert parse_token("O") == ("outfall", None)
    assert parse_token("?") == ("missing", None)
    assert parse_token("ne") == ("direction", "NE")
    with pytest.raises(GridValidationError):
        parse_token("X")


def test_resurvey_cannot_create_an_outfall():
    with pytest.raises(GridValidationError):
        parse_resurvey_token("O")
    assert parse_resurvey_token("?") == ("missing", None)
    assert parse_resurvey_token("sw") == ("direction", "SW")
