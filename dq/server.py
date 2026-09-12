"""The MCP surface.

Deliberately thin: every tool loads a dataset, calls one function in `dq.core`,
and serialises the dict. No logic lives here, so the checks an agent runs are
byte-for-byte the checks the CLI and the browser demo run.
"""

from __future__ import annotations

import json
from typing import Any

from . import core, suite
from .loaders import FrameCache, LoadError, load_path

# The MCP Python SDK renamed its high-level server class in 2.0:
# FastMCP (1.x) -> MCPServer (2.x). Support both so this runs either way.
try:  # pragma: no cover - depends on the installed SDK
    from mcp.server.mcpserver import MCPServer as _Server  # mcp >= 2.0
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server  # mcp < 2.0

mcp = _Server("dq")
_cache = FrameCache(load_path)


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _load(path: str):
    return _cache.get(path)


def _load_error(test: str, exc: Exception) -> str:
    return _dump(core._error(test, str(exc), target={"path": None}))


@mcp.tool()
def infer_schema(path: str) -> str:
    """Return column names, dtypes and row count for a dataset.

    Run this first. It is cheap, and it stops you guessing at column names.

    Args:
        path: Path to a csv, tsv, json, jsonl or parquet file.
    """
    try:
        return _dump(core.infer_schema(_load(path), name=path))
    except LoadError as exc:
        return _load_error("infer_schema", exc)


@mcp.tool()
def profile_table(path: str, sample_values: int = 3) -> str:
    """Profile every column: null rate, distinct count, examples, numeric range.

    Args:
        path: Path to the dataset.
        sample_values: How many example values to show per column (0 to omit).
    """
    try:
        return _dump(core.profile_table(_load(path), sample_values, name=path))
    except LoadError as exc:
        return _load_error("profile_table", exc)


@mcp.tool()
def check_not_null(path: str, columns: list[str]) -> str:
    """Assert the given columns contain no nulls. The dbt not_null test.

    Args:
        path: Path to the dataset.
        columns: Column names that must be fully populated.
    """
    try:
        return _dump(core.check_not_null(_load(path), columns))
    except LoadError as exc:
        return _load_error("not_null", exc)


@mcp.tool()
def check_unique(path: str, columns: list[str]) -> str:
    """Assert uniqueness. One column checks that column; several check a composite key.

    Args:
        path: Path to the dataset.
        columns: One column, or several to test as a composite key.
    """
    try:
        return _dump(core.check_unique(_load(path), columns))
    except LoadError as exc:
        return _load_error("unique", exc)


@mcp.tool()
def check_relationship(
    child_path: str,
    child_column: str,
    parent_path: str,
    parent_column: str,
) -> str:
    """Assert referential integrity: every child value exists in the parent.

    Args:
        child_path: Dataset holding the foreign key.
        child_column: The foreign key column.
        parent_path: Dataset holding the primary key.
        parent_column: The primary key column.
    """
    try:
        result = core.check_relationship(
            _load(child_path), child_column, _load(parent_path), parent_column
        )
        result["target"]["child"] = child_path
        result["target"]["parent"] = parent_path
        return _dump(result)
    except LoadError as exc:
        return _load_error("relationships", exc)


@mcp.tool()
def check_accepted_values(path: str, column: str, accepted: list[str]) -> str:
    """Assert a column only contains values from an allowed set.

    Args:
        path: Path to the dataset.
        column: The column to constrain.
        accepted: The allowed values.
    """
    try:
        return _dump(core.check_accepted_values(_load(path), column, accepted))
    except LoadError as exc:
        return _load_error("accepted_values", exc)


@mcp.tool()
def check_range(
    path: str,
    column: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> str:
    """Assert a numeric column stays between bounds.

    Catches negative quantities and impossible amounts, which pass every other
    check without complaint.

    Args:
        path: Path to the dataset.
        column: The numeric column to bound.
        min_value: Lowest allowed value, if any.
        max_value: Highest allowed value, if any.
    """
    try:
        return _dump(core.check_range(_load(path), column, min_value, max_value))
    except LoadError as exc:
        return _load_error("range", exc)


@mcp.tool()
def check_freshness(path: str, column: str, max_age_hours: float = 24) -> str:
    """Assert the newest timestamp in a column is recent enough.

    Args:
        path: Path to the dataset.
        column: The timestamp column.
        max_age_hours: How old the newest row is allowed to be.
    """
    try:
        return _dump(core.check_freshness(_load(path), column, max_age_hours))
    except LoadError as exc:
        return _load_error("freshness", exc)


@mcp.tool()
def suggest_suite(path: str) -> str:
    """Propose a starting test suite based on what the data currently looks like.

    Returns a suite you can pass straight to run_suite, plus the reason for each
    proposal. Read the reasons — a suggestion describes the sample, not a rule.

    Args:
        path: Path to the dataset.
    """
    try:
        return _dump(suite.suggest_suite(_load(path)))
    except LoadError as exc:
        return _load_error("suggest_suite", exc)


@mcp.tool()
def run_suite(path: str, suite_json: str) -> str:
    """Run several checks in one call and return a combined report.

    Args:
        path: Path to the dataset.
        suite_json: JSON like
            {"not_null": ["id", "created_at"],
             "unique": [["id"], ["account_id", "as_of_date"]],
             "accepted_values": {"status": ["open", "closed"]},
             "range": {"amount": {"min": 0}},
             "freshness": {"created_at": {"max_age_hours": 24}},
             "relationships": [{"column": "customer_id",
                                "parent": "customers.csv",
                                "parent_column": "customer_id"}]}
    """
    try:
        spec = json.loads(suite_json)
    except json.JSONDecodeError as exc:
        return _dump(core._error("suite", f"suite_json is not valid JSON: {exc}"))
    if not isinstance(spec, dict):
        return _dump(core._error("suite", "suite_json must be a JSON object."))

    try:
        df = _load(path)
    except LoadError as exc:
        return _load_error("suite", exc)

    return _dump(suite.run_suite(df, spec, name=path, resolve_parent=_load))


@mcp.tool()
def export_run_results(path: str, suite_json: str) -> str:
    """Run a suite and return the report in dbt's run_results.json shape.

    Use this when the results have to be read by CI tooling that already
    understands dbt artifacts.

    Args:
        path: Path to the dataset.
        suite_json: Same shape as run_suite.
    """
    raw = run_suite(path, suite_json)
    report = json.loads(raw)
    if "results" not in report:
        return raw
    return _dump(suite.to_run_results(report))


@mcp.resource("dq://conventions")
def conventions() -> str:
    """House rules for interpreting the results of these checks."""
    return core.CONVENTIONS


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
