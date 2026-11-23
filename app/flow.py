"""Flow evaluation for the drainage direction map.

The map is a functional graph: every ``direction`` cell has exactly one
successor, while ``outfall``, ``missing`` cells and the area outside the
rectangle terminate a path.  Following successors from any non-outfall cell
therefore ends in exactly one of four ways:

* it reaches an **outfall** -> *supplied*, recording that outfall;
* it steps **outside the rectangle** -> isolated, reporting the last in-bounds
  cell;
* it reaches a **missing** cell -> isolated, reporting the missing cell and the
  cell that points into it (``stop``); a missing cell itself is isolated with
  itself reported and no ``stop``;
* it **re-enters a cell of its own path** -> isolated, reporting the whole
  cycle.

Terminal facts (outfall, missing cell, last in-bounds cell, cycle set) are
identical for every cell on the same path, so a result can be inherited by the
whole path.

Only affected cells are evaluated.  A cached result for an unaffected
successor is a valid shortcut: a cell that drains through an affected cell is
itself affected, so the cache cannot be stale along the path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from .grid import DIRECTIONS, Cell, canonical_cycle

Outcome = str
Reason = str


@dataclass(frozen=True, slots=True)
class FlowResult:
    """Classification of one non-outfall cell."""

    outcome: Outcome  # "supplied" | "isolated"
    reason: Reason | None = None
    outfall: Cell | None = None
    missing: Cell | None = None
    stop: Cell | None = None
    last: Cell | None = None
    cycle: tuple[Cell, ...] = field(default_factory=tuple)

    @classmethod
    def supplied(cls, outfall: Cell) -> "FlowResult":
        return cls(outcome="supplied", outfall=outfall)

    @classmethod
    def at_missing(cls, missing: Cell, stop: Cell | None) -> "FlowResult":
        return cls(outcome="isolated", reason="missing", missing=missing, stop=stop)

    @classmethod
    def out_of_bounds(cls, last: Cell) -> "FlowResult":
        return cls(outcome="isolated", reason="out_of_bounds", last=last)

    @classmethod
    def in_cycle(cls, cycle: Sequence[Cell]) -> "FlowResult":
        return cls(outcome="isolated", reason="cycle", cycle=tuple(cycle))

    # -- persistence -------------------------------------------------------
    def detail(self) -> dict | None:
        if self.outcome == "supplied":
            return None
        if self.reason == "missing":
            return {
                "missing": list(self.missing) if self.missing else None,
                "stop": list(self.stop) if self.stop else None,
            }
        if self.reason == "out_of_bounds":
            return {"last": list(self.last) if self.last else None}
        if self.reason == "cycle":
            return {"cycle": [list(c) for c in self.cycle]}
        raise ValueError(f"unclassified result {self!r}")  # pragma: no cover

    @classmethod
    def from_record(
        cls,
        outcome: str,
        reason: str | None,
        outfall: Cell | None,
        detail: dict | None,
    ) -> "FlowResult":
        if outcome == "supplied":
            if outfall is None:  # pragma: no cover - guarded by the schema
                raise ValueError("supplied result without an outfall")
            return cls.supplied(outfall)
        detail = detail or {}
        if reason == "missing":
            missing = detail.get("missing")
            if missing is None:  # pragma: no cover - guarded by the schema
                raise ValueError("missing result without a missing cell")
            return cls.at_missing((int(missing[0]), int(missing[1])), _opt_cell(detail.get("stop")))
        if reason == "out_of_bounds":
            last = detail.get("last")
            if last is None:  # pragma: no cover - guarded by the schema
                raise ValueError("out_of_bounds result without a last cell")
            return cls.out_of_bounds((int(last[0]), int(last[1])))
        if reason == "cycle":
            cycle = tuple((int(c[0]), int(c[1])) for c in detail["cycle"])
            return cls.in_cycle(cycle)
        raise ValueError(f"unknown stored result outcome={outcome!r} reason={reason!r}")


def cell_json(cell: Cell | None) -> dict | None:
    return None if cell is None else {"row": cell[0], "col": cell[1]}


def _opt_cell(value: Sequence[int] | None) -> Cell | None:
    return None if value is None else (int(value[0]), int(value[1]))


def evaluate_cells(
    affected: Iterable[Cell],
    states: Mapping[Cell, tuple[str, str | None]],
    rows: int,
    cols: int,
    lookup_state: Callable[[Cell], tuple[str, str | None]],
    lookup_result: Callable[[Cell], FlowResult],
) -> dict[Cell, FlowResult]:
    """Classify every cell in ``affected`` (outfalls must not be included).

    ``states`` holds the *current* ``(kind, direction)`` of each affected cell.
    ``lookup_state`` resolves a successor that is not affected (an outfall, a
    missing cell, or an unchanged direction cell) and ``lookup_result``
    resolves the cached result of an unchanged direction cell.
    """
    affected_set = set(affected)
    results: dict[Cell, FlowResult] = {}

    def assign(path: list[Cell], base: FlowResult) -> None:
        for cell in path:
            results[cell] = base

    for start in affected_set:
        if start in results:
            continue
        path: list[Cell] = []
        positions: dict[Cell, int] = {}
        current = start

        while True:
            if current in positions:
                # Re-entered a cell of the path currently being walked.
                loop = canonical_cycle(path[positions[current]:])
                assign(path, FlowResult.in_cycle(loop))
                break

            kind, direction = states[current]

            if kind == "outfall":  # pragma: no cover - outfalls are never affected
                raise RuntimeError("outfall cell must not be evaluated")
            if kind == "missing":
                # The missing cell itself has no "cell pointing into it"; the
                # path that reached it does, and is the one just walked.
                results[current] = FlowResult.at_missing(current, None)
                if path:
                    assign(path, FlowResult.at_missing(current, path[-1]))
                break
            if direction is None:  # pragma: no cover - guarded by the schema
                raise ValueError(f"direction cell {current} without a direction")

            if current in results:
                # Already classified as part of another affected path.
                assign(path, results[current])
                break

            positions[current] = len(path)
            path.append(current)
            dr, dc = DIRECTIONS[direction]
            target = (current[0] + dr, current[1] + dc)
            if not (0 <= target[0] < rows and 0 <= target[1] < cols):
                assign(path, FlowResult.out_of_bounds(current))
                break
            if target in affected_set:
                current = target
                continue

            # Unchanged cell: resolve it directly (or via its cached result)
            # instead of walking the rest of the path.
            kind, _ = lookup_state(target)
            if kind == "outfall":
                assign(path, FlowResult.supplied(target))
            elif kind == "missing":
                assign(path, FlowResult.at_missing(target, current))
            else:
                assign(path, lookup_result(target))
            break

    unclassified = affected_set - results.keys()
    if unclassified:  # pragma: no cover - internal invariant
        raise RuntimeError(f"evaluation left cells unclassified: {sorted(unclassified)[:5]}")
    return results
