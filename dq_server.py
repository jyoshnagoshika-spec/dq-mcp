"""
dq-mcp — a Model Context Protocol server that exposes data-quality checks as tools.

Why this exists
---------------
An LLM asked to reason about a dataset will happily describe a table it has never
profiled. This server gives it real tools instead: it can profile a table, run
not-null / uniqueness / referential-integrity checks, and read back actual numbers
before it says anything about the data.

The checks mirror the dbt test vocabulary (not_null, unique, relationships) so the
same assertions you enforce in a pipeline are available to an agent at query time.

Run it
------
    pip install "mcp[cli]" pandas pyarrow
    python dq_server.py

Register it with Claude Desktop by adding this to claude_desktop_config.json:

    {
      "mcpServers": {
        "dq": {
          "command": "python",
          "args": ["/absolute/path/to/dq_server.py"]
        }
      }
    }

Author: Jyoshna Goshika
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

# The MCP Python SDK renamed its high-level server class in 2.0:
# FastMCP (1.x)  ->  MCPServer (2.x). Support both so this runs either way.
try:
    from mcp.server.mcpserver import MCPServer as _Server  # mcp >= 2.0
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server      # mcp < 2.0

mcp = _Server("dq")

# Guard rail: refuse to load anything enormous into memory by accident.
MAX_BYTES = 512 * 1024 * 1024  # 512 MB


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def _load(path: str) -> pd.DataFrame:
    """Load a CSV or Parquet file into a DataFrame, with useful failures."""
    p = Path(path).expanduser()

    if not p.exists():
        raise FileNotFoundError(f"No file at {p}")
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(
            f"{p.name} is {p.stat().st_size / 1e6:.0f} MB, above the {MAX_BYTES / 1e6:.0f} MB limit. "
            "Point this at a sample or a partition instead."
        )

    suffix = p.suffix.lower()
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(p)
    if suffix in (".csv", ".txt"):
        return pd.read_csv(p)
    if suffix in (".tsv",):
        return pd.read_csv(p, sep="\t")
    if suffix in (".json", ".jsonl", ".ndjson"):
        return pd.read_json(p, lines=suffix in (".jsonl", ".ndjson"))

    raise ValueError(f"Unsupported file type '{suffix}'. Use csv, tsv, json, jsonl or parquet.")


def _ok(**kwargs: Any) -> str:
    return json.dumps({"status": "pass", **kwargs}, indent=2, default=str)


def _fail(**kwargs: Any) -> str:
    return json.dumps({"status": "fail", **kwargs}, indent=2, default=str)


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

@mcp.tool()
def infer_schema(path: str) -> str:
    """Return the column names, dtypes and row count for a dataset.

    Use this first. It is cheap, and it stops you guessing at column names.

    Args:
        path: Path to a csv, tsv, json, jsonl or parquet file.
    """
    df = _load(path)
    return json.dumps(
        {
            "path": path,
            "rows": int(len(df)),
            "columns": [
                {"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns
            ],
        },
        indent=2,
    )


@mcp.tool()
def profile_table(path: str, sample_values: int = 3) -> str:
    """Profile every column: null rate, distinct count, and a few example values.

    This is the check that tends to surface the real problem — a column that is
    40% null, or a 'unique' id with three distinct values across a million rows.

    Args:
        path: Path to the dataset.
        sample_values: How many example values to show per column (0 to omit).
    """
    df = _load(path)
    rows = len(df)
    out = []

    for col in df.columns:
        s = df[col]
        nulls = int(s.isna().sum())
        entry = {
            "column": str(col),
            "dtype": str(s.dtype),
            "nulls": nulls,
            "null_rate": round(nulls / rows, 4) if rows else 0.0,
            "distinct": int(s.nunique(dropna=True)),
        }
        if sample_values > 0:
            examples = s.dropna().unique()[:sample_values]
            entry["examples"] = [str(v) for v in examples]
        if pd.api.types.is_numeric_dtype(s) and rows:
            entry["min"] = float(s.min()) if nulls < rows else None
            entry["max"] = float(s.max()) if nulls < rows else None
            entry["mean"] = round(float(s.mean()), 4) if nulls < rows else None
        out.append(entry)

    return json.dumps({"path": path, "rows": rows, "columns": out}, indent=2, default=str)


@mcp.tool()
def check_not_null(path: str, columns: list[str]) -> str:
    """Assert that the given columns contain no nulls. The dbt not_null test.

    Args:
        path: Path to the dataset.
        columns: Column names that must be fully populated.
    """
    df = _load(path)
    missing = [c for c in columns if c not in df.columns]
    if missing:
        return _fail(reason="columns not found", missing=missing, available=list(map(str, df.columns)))

    failures = {}
    for c in columns:
        n = int(df[c].isna().sum())
        if n:
            failures[c] = {"null_rows": n, "null_rate": round(n / len(df), 4)}

    if failures:
        return _fail(test="not_null", path=path, failures=failures)
    return _ok(test="not_null", path=path, columns=columns, rows_checked=int(len(df)))


@mcp.tool()
def check_unique(path: str, columns: list[str]) -> str:
    """Assert uniqueness. One column checks that column; several check the composite key.

    Args:
        path: Path to the dataset.
        columns: One column, or several to test as a composite key.
    """
    df = _load(path)
    missing = [c for c in columns if c not in df.columns]
    if missing:
        return _fail(reason="columns not found", missing=missing, available=list(map(str, df.columns)))

    dupes = df[df.duplicated(subset=columns, keep=False)]
    if len(dupes):
        worst = (
            dupes.groupby(columns, dropna=False)
            .size()
            .sort_values(ascending=False)
            .head(5)
        )
        return _fail(
            test="unique",
            path=path,
            key=columns,
            duplicate_rows=int(len(dupes)),
            worst_offenders={str(k): int(v) for k, v in worst.items()},
        )
    return _ok(test="unique", path=path, key=columns, rows_checked=int(len(df)))


@mcp.tool()
def check_relationship(
    child_path: str,
    child_column: str,
    parent_path: str,
    parent_column: str,
) -> str:
    """Assert referential integrity: every child value exists in the parent.

    The dbt relationships test. Orphaned foreign keys are the failure that
    quietly corrupts a join three models downstream.

    Args:
        child_path: Dataset holding the foreign key.
        child_column: The foreign key column.
        parent_path: Dataset holding the primary key.
        parent_column: The primary key column.
    """
    child = _load(child_path)
    parent = _load(parent_path)

    if child_column not in child.columns:
        return _fail(reason=f"'{child_column}' not in child", available=list(map(str, child.columns)))
    if parent_column not in parent.columns:
        return _fail(reason=f"'{parent_column}' not in parent", available=list(map(str, parent.columns)))

    child_vals = child[child_column].dropna()
    parent_vals = set(parent[parent_column].dropna().unique())
    orphan_mask = ~child_vals.isin(parent_vals)
    orphans = child_vals[orphan_mask]

    if len(orphans):
        return _fail(
            test="relationships",
            orphan_rows=int(len(orphans)),
            orphan_rate=round(len(orphans) / len(child_vals), 4) if len(child_vals) else 0.0,
            example_orphans=[str(v) for v in orphans.unique()[:10]],
        )
    return _ok(
        test="relationships",
        rows_checked=int(len(child_vals)),
        parent_keys=int(len(parent_vals)),
    )


@mcp.tool()
def check_accepted_values(path: str, column: str, accepted: list[str]) -> str:
    """Assert that a column only contains values from an allowed set.

    Args:
        path: Path to the dataset.
        column: The column to constrain.
        accepted: The allowed values.
    """
    df = _load(path)
    if column not in df.columns:
        return _fail(reason="column not found", available=list(map(str, df.columns)))

    allowed = set(map(str, accepted))
    actual = df[column].dropna().astype(str)
    bad = actual[~actual.isin(allowed)]

    if len(bad):
        counts = bad.value_counts().head(10)
        return _fail(
            test="accepted_values",
            column=column,
            violating_rows=int(len(bad)),
            unexpected_values={str(k): int(v) for k, v in counts.items()},
        )
    return _ok(test="accepted_values", column=column, rows_checked=int(len(actual)))


@mcp.tool()
def run_suite(path: str, suite_json: str) -> str:
    """Run several checks in one call and return a combined report.

    Args:
        path: Path to the dataset.
        suite_json: JSON like
            {"not_null": ["id", "created_at"],
             "unique": [["id"], ["account_id", "as_of_date"]],
             "accepted_values": {"status": ["open", "closed"]}}
    """
    try:
        suite = json.loads(suite_json)
    except json.JSONDecodeError as e:
        return _fail(reason=f"suite_json is not valid JSON: {e}")

    results, failed = [], 0

    for col in suite.get("not_null", []):
        r = json.loads(check_not_null(path, [col]))
        failed += r["status"] == "fail"
        results.append(r)

    for key in suite.get("unique", []):
        key = key if isinstance(key, list) else [key]
        r = json.loads(check_unique(path, key))
        failed += r["status"] == "fail"
        results.append(r)

    for col, allowed in suite.get("accepted_values", {}).items():
        r = json.loads(check_accepted_values(path, col, allowed))
        failed += r["status"] == "fail"
        results.append(r)

    return json.dumps(
        {
            "path": path,
            "tests_run": len(results),
            "tests_failed": failed,
            "status": "fail" if failed else "pass",
            "results": results,
        },
        indent=2,
        default=str,
    )


# --------------------------------------------------------------------------
# resource
# --------------------------------------------------------------------------

@mcp.resource("dq://conventions")
def conventions() -> str:
    """House rules for interpreting the results of these checks."""
    return (
        "Reading a data-quality report\n"
        "=============================\n\n"
        "1. Run infer_schema before anything else. Do not guess column names.\n"
        "2. A passing test means the assertion held on the rows present. It does not\n"
        "   mean the data is correct — a fully populated column of wrong values passes\n"
        "   not_null cleanly.\n"
        "3. Null rate matters more than null count. Ten nulls in twelve rows is a\n"
        "   broken pipeline; ten in ten million is probably a late-arriving record.\n"
        "4. Orphaned foreign keys are worth escalating even at a low rate. They tend to\n"
        "   indicate a load-order problem that will get worse, not a one-off.\n"
        "5. Report the numbers you actually retrieved. If a check was not run, say it\n"
        "   was not run rather than inferring the result."
    )


if __name__ == "__main__":
    mcp.run()
