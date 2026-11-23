"""FastAPI application: HTTP surface for the drainage flow map."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .grid import GridValidationError
from .service import ConflictError, DEMO_GRID, GridService, NotFoundError
from .store import Store, get_store, set_store

DB_PATH = Path(os.environ.get("DRAINAGE_DB", "/data/drainage.sqlite3"))


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ImportRequest(BaseModel):
    tokens: list[list[str]] = Field(
        ...,
        description=(
            "Rectangular grid of tokens. 'O' = outfall, '?' = missing, "
            "otherwise one of N, NE, E, SE, S, SW, W, NW. "
            "Row 0 is the top row, col 0 the left column; N is row-1."
        ),
    )
    source_name: str | None = Field(None, max_length=200)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "source_name": "demo with a cycle",
                    "tokens": DEMO_GRID,
                }
            ]
        }
    }


class ResurveyItem(BaseModel):
    row: int = Field(..., ge=0)
    col: int = Field(..., ge=0)
    value: str = Field(
        ...,
        description="One of N, NE, E, SE, S, SW, W, NW or '?' (missing). Outfalls are fixed.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"row": 1, "col": 2, "value": "N"}]
        }
    }


class ResurveyRequest(BaseModel):
    updates: list[ResurveyItem] = Field(..., min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"updates": [{"row": 1, "col": 2, "value": "N"}]}
            ]
        }
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(store: Store | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if store is not None:
            set_store(store)
        else:
            set_store(Store(DB_PATH))
        yield
        set_store(None)

    app = FastAPI(
        title="Drainage Flow Map",
        version=__version__,
        description=(
            "Import a rectangular flow-direction map, answer which cells drain "
            "to which outfall, isolate and diagnose the rest, and re-survey "
            "cells in batches with incremental re-evaluation."
        ),
        lifespan=lifespan,
    )

    def service() -> GridService:
        return GridService(get_store())

    @app.exception_handler(NotFoundError)
    async def _not_found(_request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(GridValidationError)
    async def _invalid(_request: Request, exc: GridValidationError):
        body = {"detail": exc.message}
        if exc.row is not None:
            body["row"] = exc.row
        if exc.col is not None:
            body["col"] = exc.col
        return JSONResponse(status_code=422, content=body)

    # -- meta ----------------------------------------------------------
    @app.get("/health", tags=["meta"])
    def health() -> dict:
        store = get_store()
        row = store.conn.execute("SELECT COUNT(*) AS n FROM grids").fetchone()
        return {"status": "ok", "version": __version__, "grid_count": int(row["n"])}

    @app.get("/grids", tags=["grids"], summary="List imported grids")
    def list_grids() -> dict:
        store = get_store()
        with store.read_tx():
            grids = [
                {
                    "grid_id": row["id"],
                    "rows": row["rows"],
                    "cols": row["cols"],
                    "source_name": row["source_name"],
                    "cell_count": row["cell_count"],
                    "created_at": row["created_at"],
                }
                for row in store.conn.execute("SELECT * FROM grids ORDER BY id")
            ]
        return {"grids": grids}

    @app.post("/grids", status_code=201, tags=["grids"], summary="Import a grid")
    def import_grid(body: ImportRequest) -> dict:
        return service().import_grid(body.tokens, body.source_name)

    @app.post("/grids/demo", status_code=201, tags=["grids"], summary="Import the built-in demo grid")
    def import_demo() -> dict:
        return service().import_demo()

    @app.get("/grids/{grid_id}", tags=["grids"], summary="Read a grid back as tokens")
    def grid_view(grid_id: int) -> dict:
        return service().grid_view(grid_id)

    # -- supply --------------------------------------------------------
    @app.get("/grids/{grid_id}/supply", tags=["supply"],
             summary="Cells that reach an outfall, grouped by outfall")
    def supply(grid_id: int) -> dict:
        return service().supply(grid_id)

    # -- diagnosis -----------------------------------------------------
    @app.get("/grids/{grid_id}/diagnosis", tags=["quality"],
             summary="Isolated cells and why (missing / out of bounds / cycle)")
    def diagnosis(grid_id: int) -> dict:
        return service().diagnosis(grid_id)

    # -- resurvey ------------------------------------------------------
    @app.post("/grids/{grid_id}/resurvey", tags=["resurvey"],
              summary="Apply a batch of re-surveyed cells and re-evaluate incrementally")
    def resurvey(grid_id: int, body: ResurveyRequest) -> dict:
        return service().resurvey(grid_id, [item.model_dump() for item in body.updates])

    return app


app = create_app()
