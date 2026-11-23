#!/usr/bin/env python3
"""End-to-end demo against a running instance.

    python scripts/demo.py [--base http://localhost:8000]

Imports the built-in cycle grid, prints supply and diagnosis, then breaks the
loop with a one-cell re-survey and prints the before/after numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} -> {exc.code}: {exc.read().decode()}")


def summarize_supply(supply: dict) -> str:
    parts = [
        f"({o['outfall']['row']},{o['outfall']['col']}): {o['supplied_count']}"
        for o in supply["outfalls"]
        if o["supplied_count"]
    ]
    return ", ".join(parts) or "none"


def summarize_diagnosis(diagnosis: dict) -> str:
    lines = []
    for group in diagnosis["groups"]:
        if group["reason"] == "cycle":
            cells = " ".join(
                f"({c['row']},{c['col']})" for c in group["cycle"]
            )
            lines.append(
                f"  cycle {cells} -> {group['flowing_cell_count']} cell(s) isolated"
            )
        elif group["reason"] == "missing":
            stops = " ".join(
                f"({c['row']},{c['col']})" for c in group["stop_cells"]
            )
            lines.append(
                f"  missing ({group['missing_cell']['row']},{group['missing_cell']['col']})"
                f" met from {stops or 'nowhere'} -> "
                f"{group['flowing_cell_count']} cell(s) isolated"
            )
        else:
            lines.append(
                f"  out_of_bounds last ("
                f"{group['last_cell']['row']},{group['last_cell']['col']}) -> "
                f"{group['flowing_cell_count']} cell(s) isolated"
            )
    return "\n".join(lines) or "  no isolated cells"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.base

    created = call(base, "POST", "/grids/demo")
    grid_id = created["grid_id"]
    print(f"imported demo grid #{grid_id}: {created['rows']}x{created['cols']}")
    print(f"  supplied {created['supplied_count']}, isolated {created['isolated_count']}")

    supply = call(base, "GET", f"/grids/{grid_id}/supply")
    diagnosis = call(base, "GET", f"/grids/{grid_id}/diagnosis")
    print(f"supply before: {supply['supplied_count']} -> {summarize_supply(supply)}")
    print("diagnosis before:")
    print(summarize_diagnosis(diagnosis))

    result = call(
        base,
        "POST",
        f"/grids/{grid_id}/resurvey",
        {"updates": [{"row": 1, "col": 2, "value": "N"}]},
    )
    print(
        f"\nre-surveyed {result['changed_count']} cell(s); "
        f"re-evaluated {result['affected_count']} cell(s) "
        f"({result['recomputed_count']} results written)"
    )
    print("supply transition:")
    print(f"  before: {result['supply_transition']['before']}")
    print(f"  after:  {result['supply_transition']['after']}")

    supply = call(base, "GET", f"/grids/{grid_id}/supply")
    diagnosis = call(base, "GET", f"/grids/{grid_id}/diagnosis")
    print(f"supply after: {supply['supplied_count']} -> {summarize_supply(supply)}")
    print("diagnosis after:")
    print(summarize_diagnosis(diagnosis))


if __name__ == "__main__":
    main()
