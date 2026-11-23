"""Application services: import, supply, diagnosis and batch resurvey.

Every mutating public method runs inside exactly one ``Store.write_tx`` so the
direction map, the supply answer and the isolation reasons commit or roll back
together.  Reads run inside one ``Store.read_tx`` snapshot, so a concurrent
write can never make the three tables disagree mid-response.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Mapping

from .flow import FlowResult, cell_json, evaluate_cells
from .grid import (
    Cell,
    GridValidationError,
    parse_resurvey_token,
    parse_token,
    token_for,
)
from .store import Store


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


DEMO_GRID: list[list[str]] = [
    ["O", "O", "O", "O", "O", "O"],
    ["O", "S", "W", "W", "SE", "O"],
    ["O", "E", "N", "NW", "E", "O"],
    ["O", "?", "W", "N", "NE", "O"],
    ["O", "O", "O", "O", "O", "O"],
]

# The demo above is a 5x6 map with an outfall ring.  Inside it,
#   (1,1) S -> (2,1) E -> (2,2) N -> (1,2) W -> (1,1)
# is a 2x2 loop; (1,3), (2,3) and (3,3) drain into it, so 7 cells are
# isolated by that loop.  (3,2) drains west into the missing cell (3,1).
# Re-surveying (1,2) from W to N sends it to the outfall above and releases
# the whole upstream cone -- that is the Swagger demo.


def _grid_row(store: Store, grid_id: int) -> sqlite3.Row:
    grid = store.get_grid(grid_id)
    if grid is None:
        raise NotFoundError(f"grid {grid_id} does not exist")
    return grid


def _parse_grid(tokens) -> tuple[int, int, dict[Cell, tuple[str, str | None]]]:
    if not isinstance(tokens, (list, tuple)) or not tokens:
        raise GridValidationError("grid must be a non-empty list of rows")
    rows = len(tokens)
    width: int | None = None
    cells: dict[Cell, tuple[str, str | None]] = {}
    for r, raw_row in enumerate(tokens):
        if not isinstance(raw_row, (list, tuple)):
            raise GridValidationError(f"row {r} must be a list of cell tokens", row=r)
        if width is None:
            width = len(raw_row)
            if width == 0:
                raise GridValidationError("grid rows must not be empty", row=r)
        elif len(raw_row) != width:
            raise GridValidationError(
                f"row {r} has {len(raw_row)} cells, expected {width} "
                "(a rectangular grid is required)",
                row=r,
            )
        for c, raw in enumerate(raw_row):
            cells[(r, c)] = parse_token(raw, row=r, col=c)
    assert width is not None
    return rows, width, cells


def _load_detail(raw: str | None) -> dict:
    if not raw:
        return {}
    return json.loads(raw)


def _pair(value) -> Cell | None:
    return None if value is None else (int(value[0]), int(value[1]))


def _state_token(state: tuple[str, str | None]) -> str:
    return token_for(*state)


class GridService:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def import_grid(self, tokens, source_name: str | None = None) -> dict:
        rows, cols, cells = _parse_grid(tokens)
        outfalls = [cell for cell, (kind, _) in cells.items() if kind == "outfall"]
        if not outfalls:
            raise GridValidationError(
                "the grid has no outfall ('O') cell, so no supply can be computed"
            )
        results = self._evaluate_map(rows, cols, cells)
        with self.store.write_tx() as conn:
            grid_id = self.store.insert_grid(rows, cols, source_name, cells)
            self.store.store_results(conn, grid_id, results)
        supplied = sum(1 for result in results.values() if result.outcome == "supplied")
        missing = sum(1 for kind, _ in cells.values() if kind == "missing")
        outfall_count = len(outfalls)
        direction_count = len(cells) - outfall_count - missing
        return {
            "grid_id": grid_id,
            "source_name": source_name,
            "rows": rows,
            "cols": cols,
            "cell_count": len(cells),
            "outfall_count": outfall_count,
            "direction_count": direction_count,
            "missing_count": missing,
            "supplied_count": supplied,
            "isolated_count": direction_count - supplied,
        }

    @staticmethod
    def _evaluate_map(
        rows: int, cols: int, cells: Mapping[Cell, tuple[str, str | None]]
    ) -> dict[Cell, FlowResult]:
        # Direction cells are classified by following their flow.  Missing
        # cells are stored as isolated-by-themselves so the diagnosis join sees
        # a uniform result table; they are excluded from supply arithmetic.
        affected = [cell for cell, (kind, _) in cells.items() if kind != "outfall"]
        if not affected:
            return {}

        def state_of(cell: Cell) -> tuple[str, str | None]:
            return cells[cell]

        def no_cache(cell: Cell) -> FlowResult:  # pragma: no cover - never called
            raise RuntimeError("a fresh map cannot read cached results")

        return evaluate_cells(affected, cells, rows, cols, state_of, no_cache)

    def import_demo(self) -> dict:
        return self.import_grid(DEMO_GRID, source_name="built-in demo (cycle)")

    # ------------------------------------------------------------------
    # Grid view
    # ------------------------------------------------------------------
    def grid_view(self, grid_id: int) -> dict:
        with self.store.read_tx():
            grid = _grid_row(self.store, grid_id)
            rows, cols = grid["rows"], grid["cols"]
            states = self.store.states_for(
                grid_id, ((r, c) for r in range(rows) for c in range(cols))
            )
        tokens = [[_state_token(states[(r, c)]) for c in range(cols)] for r in range(rows)]
        return {
            "grid_id": grid_id,
            "source_name": grid["source_name"],
            "rows": rows,
            "cols": cols,
            "tokens": tokens,
        }

    # ------------------------------------------------------------------
    # Supply
    # ------------------------------------------------------------------
    def supply(self, grid_id: int) -> dict:
        with self.store.read_tx():
            grid = _grid_row(self.store, grid_id)
            outfalls = [
                (row["row"], row["col"])
                for row in self.store.conn.execute(
                    "SELECT row, col FROM cells WHERE grid_id = ? AND kind = 'outfall' "
                    "ORDER BY row, col",
                    (grid_id,),
                )
            ]
            supplied = list(
                self.store.conn.execute(
                    "SELECT row, col, outfall_row, outfall_col FROM flow_results "
                    "WHERE grid_id = ? AND outcome = 'supplied' ORDER BY row, col",
                    (grid_id,),
                )
            )
        total = grid["rows"] * grid["cols"]
        by_outfall: dict[Cell, list[Cell]] = {cell: [] for cell in outfalls}
        for row in supplied:
            by_outfall.setdefault((row["outfall_row"], row["outfall_col"]), []).append(
                (row["row"], row["col"])
            )
        direction_count = self._direction_count(grid_id)
        return {
            "grid_id": grid_id,
            "rows": grid["rows"],
            "cols": grid["cols"],
            "cell_count": total,
            "outfall_count": len(outfalls),
            "supplied_count": len(supplied),
            "isolated_count": direction_count - len(supplied),
            "outfalls": [
                {
                    "outfall": cell_json(cell),
                    "supplied_count": len(by_outfall.get(cell, [])),
                    "cells": [cell_json(c) for c in by_outfall.get(cell, [])],
                }
                for cell in outfalls
            ],
        }

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------
    def diagnosis(self, grid_id: int) -> dict:
        with self.store.read_tx():
            grid = _grid_row(self.store, grid_id)
            counts = {
                key: self.store.conn.execute(
                    f"SELECT COUNT(*) AS n FROM cells WHERE grid_id = ? AND kind = '{key}'",
                    (grid_id,),
                ).fetchone()["n"]
                for key in ("outfall", "missing", "direction")
            }
            missing = [
                (row["row"], row["col"])
                for row in self.store.conn.execute(
                    "SELECT row, col FROM cells WHERE grid_id = ? AND kind = 'missing' "
                    "ORDER BY row, col",
                    (grid_id,),
                )
            ]
            isolated = list(
                self.store.conn.execute(
                    "SELECT f.row, f.col, f.reason, f.detail, c.kind "
                    "FROM flow_results f JOIN cells c "
                    "  ON c.grid_id = f.grid_id AND c.row = f.row AND c.col = f.col "
                    "WHERE f.grid_id = ? AND f.outcome = 'isolated' "
                    "ORDER BY f.row, f.col",
                    (grid_id,),
                )
            )
        total = grid["rows"] * grid["cols"]
        groups: dict[str, dict] = {}
        isolated_direction_count = 0
        for row in isolated:
            cell = (row["row"], row["col"])
            isolated_direction_count += row["kind"] == "direction"
            detail = _load_detail(row["detail"])
            reason = row["reason"]
            if reason == "cycle":
                cycle = tuple(_pair(p) for p in detail["cycle"])
                key = "cycle:" + json.dumps(cycle)
                group = groups.setdefault(
                    key,
                    {
                        "reason": "cycle",
                        "cycle": [cell_json(c) for c in cycle],
                        "looping_cells": [cell_json(c) for c in cycle],
                        "isolated_cells": [],
                        "diagnosis": "flow loops and never reaches an outfall",
                    },
                )
                group["isolated_cells"].append(cell_json(cell))
            elif reason == "missing":
                missing_cell = _pair(detail["missing"])
                key = "missing:" + json.dumps(missing_cell)
                group = groups.setdefault(
                    key,
                    {
                        "reason": "missing",
                        "missing_cell": cell_json(missing_cell),
                        "stop_cells": [],
                        "isolated_cells": [],
                        "diagnosis": "flow stops at a missing-survey cell",
                    },
                )
                group["isolated_cells"].append(cell_json(cell))
                stop = _pair(detail.get("stop"))
                if stop is not None:
                    group["stop_cells"].append(cell_json(stop))
            elif reason == "out_of_bounds":
                last = _pair(detail["last"])
                key = "out_of_bounds:" + json.dumps(last)
                group = groups.setdefault(
                    key,
                    {
                        "reason": "out_of_bounds",
                        "last_cell": cell_json(last),
                        "isolated_cells": [],
                        "diagnosis": "flow leaves the rectangle before reaching an outfall",
                    },
                )
                group["isolated_cells"].append(cell_json(cell))
            else:  # pragma: no cover - guarded by the schema
                raise RuntimeError(f"unknown isolation reason {reason!r}")

        ordered = []
        for group in groups.values():
            group["isolated_cells"].sort(key=lambda p: (p["row"], p["col"]))
            if group["reason"] == "cycle":
                # One group == one loop; report every loop cell exactly once.
                group["looping_cells"].sort(key=lambda p: (p["row"], p["col"]))
            if group["reason"] == "missing":
                unique = {(p["row"], p["col"]): p for p in group["stop_cells"]}
                group["stop_cells"] = [unique[k] for k in sorted(unique)]
                group["encountered"] = bool(group["stop_cells"])
                # The missing cell itself is in the group but does not flow.
                group["flowing_cell_count"] = sum(
                    1
                    for p in group["isolated_cells"]
                    if (p["row"], p["col"])
                    != (group["missing_cell"]["row"], group["missing_cell"]["col"])
                )
            else:
                group["flowing_cell_count"] = len(group["isolated_cells"])
            group["cell_count"] = len(group["isolated_cells"])
            ordered.append(group)
        ordered.sort(
            key=lambda g: (
                {"cycle": 0, "missing": 1, "out_of_bounds": 2}[g["reason"]],
                json.dumps(g.get("cycle") or g.get("missing_cell") or g.get("last_cell")),
            )
        )
        encountered_missing = {
            (g["missing_cell"]["row"], g["missing_cell"]["col"])
            for g in ordered
            if g["reason"] == "missing" and g["encountered"]
        }
        return {
            "grid_id": grid_id,
            "rows": grid["rows"],
            "cols": grid["cols"],
            "summary": {
                "cell_count": total,
                "outfall_count": counts["outfall"],
                "direction_count": counts["direction"],
                "missing_cell_count": counts["missing"],
                "supplied_count": counts["direction"] - isolated_direction_count,
                "isolated_count": isolated_direction_count,
                "isolated_total_count": len(isolated),
                "encountered_missing_count": len(encountered_missing),
                "cycle_count": sum(1 for g in ordered if g["reason"] == "cycle"),
                "out_of_bounds_count": sum(
                    1 for g in ordered if g["reason"] == "out_of_bounds"
                ),
                "missing_blocked_count": sum(
                    1 for g in ordered if g["reason"] == "missing"
                ),
            },
            "missing_cells": [
                {**cell_json(cell), "encountered": cell in encountered_missing}
                for cell in missing
            ],
            "groups": ordered,
        }

    # ------------------------------------------------------------------
    # Batch resurvey
    # ------------------------------------------------------------------
    def resurvey(self, grid_id: int, updates: Iterable[Mapping]) -> dict:
        updates = list(updates)
        with self.store.write_tx() as conn:
            grid = _grid_row(self.store, grid_id)
            rows, cols = grid["rows"], grid["cols"]

            wanted: dict[Cell, tuple[str, str | None]] = {}
            for update in updates:
                try:
                    row, col = int(update["row"]), int(update["col"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise GridValidationError(
                        "each update needs integer 'row' and 'col' fields"
                    ) from exc
                kind, direction = parse_resurvey_token(update.get("value"), row=row, col=col)
                if not (0 <= row < rows and 0 <= col < cols):
                    raise GridValidationError(
                        f"cell ({row},{col}) is outside the {rows}x{cols} grid",
                        row=row,
                        col=col,
                    )
                wanted[(row, col)] = (kind, direction)

            if not wanted:
                raise GridValidationError("the resurvey batch is empty")

            before_states = self.store.states_for(grid_id, wanted)
            for cell in wanted:
                current = before_states.get(cell)
                if current is None:  # pragma: no cover - rectangular grid
                    raise NotFoundError(f"cell {cell} does not exist")
                if current[0] == "outfall":
                    raise ConflictError(
                        f"cell {cell} is a fixed outfall and cannot be re-surveyed"
                    )
            changed = {cell for cell, after in wanted.items() if before_states[cell] != after}
            changes = [(cell, before_states[cell], wanted[cell]) for cell in changed]
            if not changes:
                return {
                    "grid_id": grid_id,
                    "resurvey_id": None,
                    "changed_count": 0,
                    "affected_count": 0,
                    "recomputed_count": 0,
                    "supplied_count": self._supplied_count(grid_id),
                    "supply_transition": {
                        "before": self._outfall_counts_json(grid_id),
                        "after": self._outfall_counts_json(grid_id),
                    },
                    "changes": [],
                    "affected_cells": [],
                }

            changed = {cell for cell, _, _ in changes}
            # "Originally flowed through" = upstream cone in the *old* map.
            # Only changed cells (whose direction/état actually changed) can
            # alter other cells' paths, so this cone plus the changed cells is
            # exactly the set to re-evaluate.
            affected = self.store.upstream_closure(grid_id, changed) | changed
            stored_states = self.store.states_for(grid_id, affected)
            after_by_cell = {cell: after for cell, _, after in changes}
            states = {
                cell: after_by_cell.get(cell, stored_states[cell]) for cell in affected
            }

            def lookup_state(cell: Cell) -> tuple[str, str | None]:
                state = self.store.cell_state(grid_id, cell)
                if state is None:  # pragma: no cover - bounds are checked above
                    raise RuntimeError(f"flow left the grid at {cell} without detection")
                return state

            def lookup_result(cell: Cell) -> FlowResult:
                result = self.store.flow_result(grid_id, cell)
                if result is None:  # pragma: no cover - all direction cells have results
                    raise RuntimeError(f"no stored result for unchanged cell {cell}")
                return result

            results = evaluate_cells(
                affected, states, rows, cols, lookup_state, lookup_result
            )
            before_counts = self._outfall_counts(grid_id)
            self.store.update_cells(conn, grid_id, after_by_cell)
            self.store.store_results(conn, grid_id, results)
            resurvey_id = self.store.insert_resurvey(conn, grid_id, changes, len(affected))
            supplied_count = self._supplied_count(grid_id)
            after_counts = self._outfall_counts(grid_id)

        return {
            "grid_id": grid_id,
            "resurvey_id": resurvey_id,
            "changed_count": len(changes),
            "affected_count": len(affected),
            "recomputed_count": len(results),
            "supplied_count": supplied_count,
            "supply_transition": {
                "before": _counts_json(before_counts),
                "after": _counts_json(after_counts),
            },
            "changes": [
                {
                    "row": cell[0],
                    "col": cell[1],
                    "before": _state_token(before),
                    "after": _state_token(after),
                }
                for cell, before, after in changes
            ],
            "affected_cells": [cell_json(c) for c in sorted(affected)],
        }

    # ------------------------------------------------------------------
    def _direction_count(self, grid_id: int) -> int:
        row = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE grid_id = ? AND kind = 'direction'",
            (grid_id,),
        ).fetchone()
        return int(row["n"])

    def _supplied_count(self, grid_id: int) -> int:
        row = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM flow_results WHERE grid_id = ? AND outcome = 'supplied'",
            (grid_id,),
        ).fetchone()
        return int(row["n"])

    def _outfall_counts(self, grid_id: int) -> dict[Cell, int]:
        return {
            (row["outfall_row"], row["outfall_col"]): int(row["n"])
            for row in self.store.conn.execute(
                "SELECT outfall_row, outfall_col, COUNT(*) AS n FROM flow_results "
                "WHERE grid_id = ? AND outcome = 'supplied' "
                "GROUP BY outfall_row, outfall_col",
                (grid_id,),
            )
        }

    def _outfall_counts_json(self, grid_id: int) -> list[dict]:
        counts = self._outfall_counts(grid_id)
        return [
            {
                "outfall": cell_json(cell),
                "supplied_count": counts[cell],
            }
            for cell in sorted(counts)
        ]


def _counts_json(counts: dict[Cell, int]) -> list[dict]:
    return [
        {"outfall": cell_json(cell), "supplied_count": counts[cell]}
        for cell in sorted(counts)
    ]
