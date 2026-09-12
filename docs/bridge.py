"""What the browser talks to.

The page runs `dq/core.py` and `dq/suite.py` unmodified under Pyodide. This file
is only the seam: it holds loaded frames in memory, hands the grid its rows, and
returns JSON. No check logic is allowed in here — if the demo could disagree
with the MCP server about what a failing row is, the demo would be worthless.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from dq import core, suite
from dq.loaders import LoadError, load_bytes

MAX_GRID_ROWS = 5000

_frames: dict[str, pd.DataFrame] = {}


def _resolve(name: str) -> pd.DataFrame:
    if name not in _frames:
        raise LoadError(f"'{name}' has not been loaded in this page.")
    return _frames[name]


def _cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if pd.isna(value):
        return ""
    return str(value)


def load(name: str, text: str) -> str:
    """Parse an uploaded or fetched file and return what the grid needs to draw."""
    try:
        df = load_bytes(text.encode("utf-8"), name)
    except LoadError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    _frames[name] = df
    shown = df.head(MAX_GRID_ROWS)

    return json.dumps(
        {
            "ok": True,
            "name": name,
            "rows": int(len(df)),
            "shown": int(len(shown)),
            "columns": [str(c) for c in df.columns],
            "dtypes": [str(df[c].dtype) for c in df.columns],
            "grid": [[_cell(v) for v in row] for row in shown.itertuples(index=False)],
        },
        default=str,
    )


def profile(name: str) -> str:
    try:
        return json.dumps(core.profile_table(_resolve(name), name=name), default=str)
    except LoadError as exc:
        return json.dumps({"error": str(exc)})


def suggest(name: str) -> str:
    try:
        return json.dumps(suite.suggest_suite(_resolve(name)), default=str)
    except LoadError as exc:
        return json.dumps({"error": str(exc)})


def run(name: str, spec_json: str) -> str:
    """Run a suite and return the report, with failing rows the grid can mark."""
    try:
        spec = json.loads(spec_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"That is not valid JSON: {exc.msg} on line {exc.lineno}."})
    if not isinstance(spec, dict):
        return json.dumps({"error": "A suite has to be a JSON object."})

    try:
        df = _resolve(name)
    except LoadError as exc:
        return json.dumps({"error": str(exc)})

    report = suite.run_suite(df, spec, name=name, resolve_parent=_resolve)
    return json.dumps(report, default=str)


def loaded() -> str:
    return json.dumps(sorted(_frames))


def conventions() -> str:
    return core.CONVENTIONS
