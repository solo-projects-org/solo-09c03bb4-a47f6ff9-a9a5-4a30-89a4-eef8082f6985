"""Grid primitives: coordinates, tokens, directions and cycle helpers.

Coordinate convention
---------------------
``row`` grows downwards, ``col`` grows rightwards (matrix convention).
``N`` therefore means ``row - 1`` and ``W`` means ``col - 1``.
"""

from __future__ import annotations

from typing import Literal

Cell = tuple[int, int]
CellKind = Literal["outfall", "missing", "direction"]

#: Direction code -> (row delta, col delta)
DIRECTIONS: dict[str, tuple[int, int]] = {
    "N": (-1, 0),
    "NE": (-1, 1),
    "E": (0, 1),
    "SE": (1, 1),
    "S": (1, 0),
    "SW": (1, -1),
    "W": (0, -1),
    "NW": (-1, -1),
}
DIRECTION_CODES = tuple(DIRECTIONS)

OUTFALL_TOKEN = "O"
MISSING_TOKEN = "?"

_OUTFALL_ALIASES = {"O", "OUTFALL", "OUTLET"}
_MISSING_ALIASES = {"?", "MISSING", "M", "NULL"}


class GridValidationError(ValueError):
    """Raised when an imported grid or a resurvey batch is malformed."""

    def __init__(self, message: str, *, row: int | None = None, col: int | None = None):
        super().__init__(message)
        self.message = message
        self.row = row
        self.col = col


def parse_token(raw: object, *, row: int | None = None, col: int | None = None) -> tuple[CellKind, str | None]:
    """Normalise one imported cell token to ``(kind, direction)``."""
    if not isinstance(raw, str):
        raise GridValidationError(
            f"cell token must be a string, got {type(raw).__name__}", row=row, col=col
        )
    token = raw.strip().upper()
    if token in _OUTFALL_ALIASES:
        return "outfall", None
    if token in _MISSING_ALIASES:
        return "missing", None
    if token in DIRECTIONS:
        return "direction", token
    raise GridValidationError(
        f"unknown cell token {raw!r}; expected one of "
        f"{OUTFALL_TOKEN} (outfall), {MISSING_TOKEN} (missing), "
        f"or one of {', '.join(DIRECTION_CODES)}",
        row=row,
        col=col,
    )


def parse_resurvey_token(raw: object, *, row: int | None = None, col: int | None = None) -> tuple[CellKind, str | None]:
    """Parse a value supplied to a resurvey batch.

    Only ``direction`` and ``missing`` are allowed.  Outfalls are fixed and
    cannot be created, moved or overwritten by a resurvey.
    """
    if not isinstance(raw, str):
        raise GridValidationError(
            f"resurvey value must be a string, got {type(raw).__name__}", row=row, col=col
        )
    token = raw.strip().upper()
    if token in _OUTFALL_ALIASES:
        raise GridValidationError(
            "outfalls are fixed; a re-survey cannot create or overwrite one",
            row=row,
            col=col,
        )
    if token in _MISSING_ALIASES:
        return "missing", None
    if token in DIRECTIONS:
        return "direction", token
    raise GridValidationError(
        f"invalid re-survey value {raw!r}; expected one of "
        f"{', '.join(DIRECTION_CODES)} or {MISSING_TOKEN} (missing)",
        row=row,
        col=col,
    )


def in_bounds(row: int, col: int, rows: int, cols: int) -> bool:
    return 0 <= row < rows and 0 <= col < cols


def token_for(kind: str, direction: str | None) -> str:
    if kind == "outfall":
        return OUTFALL_TOKEN
    if kind == "missing":
        return MISSING_TOKEN
    if direction is None:  # pragma: no cover - guarded by DB CHECK
        raise ValueError("direction cell without a direction")
    return direction


def canonical_cycle(cycle: list[Cell]) -> list[Cell]:
    """Rotate a flow-ordered cycle so that it starts at its smallest cell."""
    if not cycle:  # pragma: no cover - defensive
        return cycle
    start = min(range(len(cycle)), key=lambda idx: cycle[idx])
    return cycle[start:] + cycle[:start]
