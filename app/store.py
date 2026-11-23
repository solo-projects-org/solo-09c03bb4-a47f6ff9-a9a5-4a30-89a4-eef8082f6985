"""SQLite persistence: cells, flow results and resurvey audit.

Directions, supply answers and isolation reasons live in one SQLite file and
are written in a single transaction, so a failed write leaves the previous
state intact and still queryable.  Every read request runs inside a deferred
transaction that sees one consistent snapshot across all three tables.

The index ``cells(grid_id, downstream_row, downstream_col)`` is what turns
"which cells are upstream of a change?" into a recursive CTE that walks only
the upstream cone instead of scanning the rectangle.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .flow import FlowResult
from .grid import Cell, DIRECTIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS grids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rows        INTEGER NOT NULL CHECK (rows > 0),
    cols        INTEGER NOT NULL CHECK (cols > 0),
    source_name TEXT,
    cell_count  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS cells (
    grid_id        INTEGER NOT NULL REFERENCES grids(id) ON DELETE CASCADE,
    row            INTEGER NOT NULL,
    col            INTEGER NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('outfall','missing','direction')),
    direction      TEXT CHECK (direction IN ('N','NE','E','SE','S','SW','W','NW')),
    downstream_row INTEGER,
    downstream_col INTEGER,
    PRIMARY KEY (grid_id, row, col),
    CHECK ((kind = 'direction') = (direction IS NOT NULL)),
    CHECK ((kind = 'direction') = (downstream_row IS NOT NULL)),
    CHECK ((kind = 'direction') = (downstream_col IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_cells_downstream
    ON cells (grid_id, downstream_row, downstream_col);

CREATE TABLE IF NOT EXISTS flow_results (
    grid_id     INTEGER NOT NULL REFERENCES grids(id) ON DELETE CASCADE,
    row         INTEGER NOT NULL,
    col         INTEGER NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('supplied','isolated')),
    reason      TEXT CHECK (reason IN ('missing','out_of_bounds','cycle')),
    outfall_row INTEGER,
    outfall_col INTEGER,
    detail      TEXT,
    PRIMARY KEY (grid_id, row, col),
    CHECK ((outcome = 'isolated') = (reason IS NOT NULL)),
    CHECK ((outcome = 'supplied') = (outfall_row IS NOT NULL AND outfall_col IS NOT NULL)),
    CHECK ((outcome = 'isolated') = (outfall_row IS NULL AND outfall_col IS NULL)),
    CHECK ((outcome = 'supplied') = (detail IS NULL)),
    CHECK (detail IS NULL OR json_valid(detail)),
    CHECK (reason <> 'cycle' OR json_extract(detail, '$.cycle') IS NOT NULL),
    CHECK (reason <> 'missing' OR json_extract(detail, '$.missing') IS NOT NULL),
    CHECK (reason <> 'out_of_bounds' OR json_extract(detail, '$.last') IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_flow_results_outfall
    ON flow_results (grid_id, outfall_row, outfall_col);
CREATE INDEX IF NOT EXISTS idx_flow_results_reason
    ON flow_results (grid_id, outcome, reason);

CREATE TABLE IF NOT EXISTS resurveys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id        INTEGER NOT NULL REFERENCES grids(id) ON DELETE CASCADE,
    changed_count  INTEGER NOT NULL,
    affected_count INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS resurvey_changes (
    resurvey_id      INTEGER NOT NULL REFERENCES resurveys(id) ON DELETE CASCADE,
    row              INTEGER NOT NULL,
    col              INTEGER NOT NULL,
    before_kind      TEXT NOT NULL,
    before_direction TEXT,
    after_kind       TEXT NOT NULL,
    after_direction  TEXT,
    PRIMARY KEY (resurvey_id, row, col)
);
"""

DB_PATH = Path(os.environ.get("DRAINAGE_DB", "/data/drainage.sqlite3"))


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class Store:
    """Thread-safe SQLite store (one connection per thread)."""

    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        self._local = threading.local()
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    # -- connections -------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self.path)
            self._local.conn = conn
            self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            conn.executescript(SCHEMA)
            self._schema_ready = True

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction; any exception rolls the whole batch back."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    @contextmanager
    def read_tx(self) -> Iterator[sqlite3.Connection]:
        """Deferred read transaction: one snapshot of cells + results."""
        conn = self.conn
        conn.execute("BEGIN DEFERRED")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- grids -------------------------------------------------------------
    def get_grid(self, grid_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM grids WHERE id = ?", (grid_id,)).fetchone()

    def list_grids(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM grids ORDER BY id"))

    def cell_state(self, grid_id: int, cell: Cell) -> tuple[str, str | None] | None:
        row = self.conn.execute(
            "SELECT kind, direction FROM cells WHERE grid_id = ? AND row = ? AND col = ?",
            (grid_id, cell[0], cell[1]),
        ).fetchone()
        return None if row is None else (row["kind"], row["direction"])

    def states_for(self, grid_id: int, cells: Iterable[Cell]) -> dict[Cell, tuple[str, str | None]]:
        cells = list(cells)
        if not cells:
            return {}
        payload = json.dumps([[r, c] for r, c in cells])
        rows = self.conn.execute(
            "SELECT row, col, kind, direction FROM cells "
            "WHERE grid_id = ? AND (row, col) IN "
            "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') FROM json_each(?))",
            (grid_id, payload),
        )
        return {(r["row"], r["col"]): (r["kind"], r["direction"]) for r in rows}

    def flow_result(self, grid_id: int, cell: Cell) -> FlowResult | None:
        row = self.conn.execute(
            "SELECT outcome, reason, outfall_row, outfall_col, detail FROM flow_results "
            "WHERE grid_id = ? AND row = ? AND col = ?",
            (grid_id, cell[0], cell[1]),
        ).fetchone()
        return _result_from_row(row) if row else None

    # -- writes ------------------------------------------------------------
    def insert_grid(
        self,
        rows: int,
        cols: int,
        source_name: str | None,
        cells: dict[Cell, tuple[str, str | None]],
    ) -> int:
        """Insert the grid and its cells inside the caller's write transaction."""
        conn = self.conn
        cursor = conn.execute(
            "INSERT INTO grids (rows, cols, source_name, cell_count) VALUES (?, ?, ?, ?)",
            (rows, cols, source_name, len(cells)),
        )
        grid_id = int(cursor.lastrowid)
        self._insert_cells(conn, grid_id, cells)
        return grid_id

    def _insert_cells(
        self, conn: sqlite3.Connection, grid_id: int, cells: dict[Cell, tuple[str, str | None]]
    ) -> None:
        payload = []
        for (row, col), (kind, direction) in cells.items():
            if kind == "direction":
                assert direction is not None
                dr, dc = DIRECTIONS[direction]
                # The raw target is stored even when it is outside the grid:
                # no in-grid cell can ever share those coordinates, so the
                # upstream index never produces a false reverse edge.
                payload.append((grid_id, row, col, kind, direction, row + dr, col + dc))
            else:
                payload.append((grid_id, row, col, kind, None, None, None))
        conn.executemany(
            "INSERT INTO cells (grid_id, row, col, kind, direction, downstream_row, downstream_col) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )

    def update_cells(
        self, conn: sqlite3.Connection, grid_id: int, changes: dict[Cell, tuple[str, str | None]]
    ) -> None:
        payload = []
        for (row, col), (kind, direction) in changes.items():
            if kind == "direction":
                assert direction is not None
                dr, dc = DIRECTIONS[direction]
                payload.append((kind, direction, row + dr, col + dc, grid_id, row, col))
            else:
                payload.append((kind, None, None, None, grid_id, row, col))
        conn.executemany(
            "UPDATE cells SET kind = ?, direction = ?, downstream_row = ?, downstream_col = ? "
            "WHERE grid_id = ? AND row = ? AND col = ?",
            payload,
        )

    def store_results(
        self, conn: sqlite3.Connection, grid_id: int, results: dict[Cell, FlowResult]
    ) -> None:
        payload = []
        for (row, col), result in results.items():
            detail = result.detail()
            payload.append(
                (
                    grid_id,
                    row,
                    col,
                    result.outcome,
                    result.reason,
                    result.outfall[0] if result.outfall else None,
                    result.outfall[1] if result.outfall else None,
                    json.dumps(detail, separators=(",", ":")) if detail is not None else None,
                )
            )
        conn.executemany(
            "INSERT INTO flow_results (grid_id, row, col, outcome, reason, outfall_row, outfall_col, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (grid_id, row, col) DO UPDATE SET "
            "outcome = excluded.outcome, reason = excluded.reason, "
            "outfall_row = excluded.outfall_row, outfall_col = excluded.outfall_col, "
            "detail = excluded.detail",
            payload,
        )

    def upstream_closure(self, grid_id: int, seeds: Iterable[Cell]) -> set[Cell]:
        """All cells that drain into any seed (inclusive), via the reverse index."""
        seeds = list(seeds)
        if not seeds:
            return set()
        payload = json.dumps([[r, c] for r, c in seeds])
        rows = self.conn.execute(
            """
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
            {"seeds": payload, "grid_id": grid_id},
        ).fetchall()
        return {(r["row"], r["col"]) for r in rows}

    def insert_resurvey(
        self,
        conn: sqlite3.Connection,
        grid_id: int,
        changes: list[tuple[Cell, tuple[str, str | None], tuple[str, str | None]]],
        affected_count: int,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO resurveys (grid_id, changed_count, affected_count) VALUES (?, ?, ?)",
            (grid_id, len(changes), affected_count),
        )
        resurvey_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO resurvey_changes "
            "(resurvey_id, row, col, before_kind, before_direction, after_kind, after_direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (resurvey_id, cell[0], cell[1], before[0], before[1], after[0], after[1])
                for cell, before, after in changes
            ],
        )
        return resurvey_id


def _result_from_row(row: sqlite3.Row) -> FlowResult:
    outfall = (row["outfall_row"], row["outfall_col"]) if row["outfall_row"] is not None else None
    return FlowResult.from_record(
        row["outcome"],
        row["reason"],
        outfall,
        json.loads(row["detail"]) if row["detail"] else None,
    )


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store


def set_store(store: Store | None) -> None:
    """Test/lifecycle hook: replace the process-wide store."""
    global _store
    with _store_lock:
        previous, _store = _store, store
    if previous is not None and previous is not store:
        previous.close()
