"""The checks themselves.

Every function here takes a DataFrame and returns a plain dict. No MCP, no file
paths, no printing. That is what lets the same code run as an MCP server, as a
CLI in CI, and in a browser under Pyodide without a second implementation
drifting out of sync with the first.

Result envelope, identical for every check:

    {
      "test":         "unique",
      "status":       "pass" | "fail" | "error",
      "target":       {...},        what was checked
      "rows_checked": 900,
      "failing_rows": [12, 13, 14], row positions, capped
      "detail":       {...},        numbers specific to this test
      "message":      "..."         one line, safe to show a user
    }

`status` separates two things the original version confused: "error" means the
check could not run (column missing, bad argument), "fail" means it ran and the
assertion did not hold. A missing column is not a data-quality failure, and
counting it as one hides real ones.
"""

from __future__ import annotations

import warnings
from typing import Any, Iterable, Sequence

import pandas as pd

MAX_FAILING_ROWS = 200  # enough to highlight or paginate, not enough to flood a context
MAX_EXAMPLES = 10


# --------------------------------------------------------------------------
# result helpers
# --------------------------------------------------------------------------

def _result(
    test: str,
    status: str,
    message: str,
    target: dict[str, Any] | None = None,
    rows_checked: int = 0,
    failing_rows: Sequence[int] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [int(i) for i in (failing_rows or [])]
    return {
        "test": test,
        "status": status,
        "target": target or {},
        "rows_checked": int(rows_checked),
        "failing_rows": rows[:MAX_FAILING_ROWS],
        "failing_rows_truncated": len(rows) > MAX_FAILING_ROWS,
        "detail": detail or {},
        "message": message,
    }


def _error(test: str, message: str, target: dict[str, Any] | None = None) -> dict[str, Any]:
    return _result(test, "error", message, target=target)


def _rate(n: int, total: int) -> float:
    return round(n / total, 6) if total else 0.0


def _missing_columns(df: pd.DataFrame, columns: Iterable[str]) -> list[str]:
    present = {str(c) for c in df.columns}
    return [c for c in columns if c not in present]


def _positions(mask: "pd.Series") -> list[int]:
    """Row positions (0-based, as displayed) rather than index labels."""
    return [int(i) for i in mask.to_numpy().nonzero()[0]]


# --------------------------------------------------------------------------
# describing a table
# --------------------------------------------------------------------------

def infer_schema(df: pd.DataFrame, name: str = "dataset") -> dict[str, Any]:
    """Column names, dtypes and row count. Cheap. Run it before anything else."""
    return {
        "dataset": name,
        "rows": int(len(df)),
        "columns": [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns],
    }


def profile_table(df: pd.DataFrame, sample_values: int = 3, name: str = "dataset") -> dict[str, Any]:
    """Per-column null rate, distinct count, examples and numeric range.

    The check that most often surfaces the real problem: a column that is 40%
    null, or an id with three distinct values across a million rows.
    """
    rows = len(df)
    columns = []

    for col in df.columns:
        s = df[col]
        nulls = int(s.isna().sum())
        distinct = int(s.nunique(dropna=True))
        entry: dict[str, Any] = {
            "column": str(col),
            "dtype": str(s.dtype),
            "nulls": nulls,
            "null_rate": _rate(nulls, rows),
            "distinct": distinct,
            "distinct_rate": _rate(distinct, rows - nulls),
        }

        if sample_values > 0:
            entry["examples"] = [str(v) for v in s.dropna().unique()[:sample_values]]

        populated = s.dropna()
        if len(populated) and pd.api.types.is_numeric_dtype(s):
            entry["min"] = float(populated.min())
            entry["max"] = float(populated.max())
            entry["mean"] = round(float(populated.mean()), 6)
        elif len(populated) and pd.api.types.is_datetime64_any_dtype(s):
            entry["min"] = str(populated.min())
            entry["max"] = str(populated.max())

        columns.append(entry)

    return {"dataset": name, "rows": rows, "columns": columns}


# --------------------------------------------------------------------------
# assertions — the dbt test vocabulary
# --------------------------------------------------------------------------

def check_not_null(df: pd.DataFrame, columns: Sequence[str]) -> dict[str, Any]:
    """Assert the given columns are fully populated. dbt's not_null."""
    columns = list(columns)
    if not columns:
        return _error("not_null", "No columns given.")

    missing = _missing_columns(df, columns)
    if missing:
        return _error(
            "not_null",
            f"Column(s) not in the dataset: {', '.join(missing)}.",
            target={"columns": columns, "available": [str(c) for c in df.columns]},
        )

    null_mask = df[columns].isna().any(axis=1)
    failures = {}
    for c in columns:
        n = int(df[c].isna().sum())
        if n:
            failures[c] = {"null_rows": n, "null_rate": _rate(n, len(df))}

    if failures:
        worst = max(failures.values(), key=lambda f: f["null_rate"])["null_rate"]
        return _result(
            "not_null",
            "fail",
            f"{int(null_mask.sum())} row(s) have a null in {', '.join(failures)} "
            f"(worst null rate {worst:.2%}).",
            target={"columns": columns},
            rows_checked=len(df),
            failing_rows=_positions(null_mask),
            detail={"by_column": failures},
        )

    return _result(
        "not_null",
        "pass",
        f"{', '.join(columns)} fully populated across {len(df)} row(s).",
        target={"columns": columns},
        rows_checked=len(df),
    )


def check_unique(df: pd.DataFrame, columns: Sequence[str]) -> dict[str, Any]:
    """Assert uniqueness. One column, or several as a composite key. dbt's unique."""
    columns = list(columns)
    if not columns:
        return _error("unique", "No columns given.")

    missing = _missing_columns(df, columns)
    if missing:
        return _error(
            "unique",
            f"Column(s) not in the dataset: {', '.join(missing)}.",
            target={"key": columns, "available": [str(c) for c in df.columns]},
        )

    dupe_mask = df.duplicated(subset=columns, keep=False)
    dupes = int(dupe_mask.sum())

    if dupes:
        counts = (
            df[dupe_mask]
            .groupby(columns, dropna=False)
            .size()
            .sort_values(ascending=False)
            .head(MAX_EXAMPLES)
        )
        worst = {
            (" | ".join(map(str, k)) if isinstance(k, tuple) else str(k)): int(v)
            for k, v in counts.items()
        }
        top_key, top_n = next(iter(worst.items()))
        return _result(
            "unique",
            "fail",
            f"{dupes} row(s) share a key that should be unique — "
            f"{top_key} appears {top_n} times.",
            target={"key": columns},
            rows_checked=len(df),
            failing_rows=_positions(dupe_mask),
            detail={"duplicate_rows": dupes, "worst_offenders": worst},
        )

    return _result(
        "unique",
        "pass",
        f"{' + '.join(columns)} unique across {len(df)} row(s).",
        target={"key": columns},
        rows_checked=len(df),
    )


def check_relationship(
    child: pd.DataFrame,
    child_column: str,
    parent: pd.DataFrame,
    parent_column: str,
) -> dict[str, Any]:
    """Assert every child value exists in the parent. dbt's relationships.

    Orphaned foreign keys are the failure that quietly corrupts a join three
    models downstream, so this reports the offending values, not just a count.
    """
    target = {
        "child_column": child_column,
        "parent_column": parent_column,
    }
    if child_column not in {str(c) for c in child.columns}:
        return _error(
            "relationships",
            f"'{child_column}' is not in the child dataset.",
            target={**target, "available": [str(c) for c in child.columns]},
        )
    if parent_column not in {str(c) for c in parent.columns}:
        return _error(
            "relationships",
            f"'{parent_column}' is not in the parent dataset.",
            target={**target, "available": [str(c) for c in parent.columns]},
        )

    child_vals = child[child_column]
    parent_vals = set(parent[parent_column].dropna().unique())
    orphan_mask = child_vals.notna() & ~child_vals.isin(parent_vals)
    orphans = int(orphan_mask.sum())
    checked = int(child_vals.notna().sum())

    if orphans:
        examples = [str(v) for v in child_vals[orphan_mask].unique()[:MAX_EXAMPLES]]
        return _result(
            "relationships",
            "fail",
            f"{orphans} row(s) reference a {parent_column} that does not exist — "
            f"for example {', '.join(examples[:3])}.",
            target=target,
            rows_checked=checked,
            failing_rows=_positions(orphan_mask),
            detail={
                "orphan_rows": orphans,
                "orphan_rate": _rate(orphans, checked),
                "example_orphans": examples,
                "parent_keys": len(parent_vals),
            },
        )

    return _result(
        "relationships",
        "pass",
        f"All {checked} non-null {child_column} value(s) exist in the parent.",
        target=target,
        rows_checked=checked,
        detail={"parent_keys": len(parent_vals)},
    )


def check_accepted_values(
    df: pd.DataFrame, column: str, accepted: Sequence[Any]
) -> dict[str, Any]:
    """Assert a column stays inside an allowed set. dbt's accepted_values."""
    if column not in {str(c) for c in df.columns}:
        return _error(
            "accepted_values",
            f"'{column}' is not in the dataset.",
            target={"column": column, "available": [str(c) for c in df.columns]},
        )
    if not list(accepted):
        return _error("accepted_values", "No accepted values given.", target={"column": column})

    allowed = {str(v) for v in accepted}
    as_text = df[column].astype(str)
    bad_mask = df[column].notna() & ~as_text.isin(allowed)
    bad = int(bad_mask.sum())
    checked = int(df[column].notna().sum())

    if bad:
        counts = as_text[bad_mask].value_counts().head(MAX_EXAMPLES)
        unexpected = {str(k): int(v) for k, v in counts.items()}
        return _result(
            "accepted_values",
            "fail",
            f"{bad} row(s) hold a value outside the allowed set — "
            f"{', '.join(list(unexpected)[:3])}.",
            target={"column": column, "accepted": sorted(allowed)},
            rows_checked=checked,
            failing_rows=_positions(bad_mask),
            detail={"violating_rows": bad, "unexpected_values": unexpected},
        )

    return _result(
        "accepted_values",
        "pass",
        f"{column} stays inside the allowed set across {checked} row(s).",
        target={"column": column, "accepted": sorted(allowed)},
        rows_checked=checked,
    )


def check_range(
    df: pd.DataFrame,
    column: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> dict[str, Any]:
    """Assert a numeric column stays between bounds.

    Negative quantities and impossible amounts are the commonest defect that
    passes not_null, unique and accepted_values without complaint.
    """
    target = {"column": column, "min": min_value, "max": max_value}
    if column not in {str(c) for c in df.columns}:
        return _error(
            "range",
            f"'{column}' is not in the dataset.",
            target={**target, "available": [str(c) for c in df.columns]},
        )
    if min_value is None and max_value is None:
        return _error("range", "Give at least one of min_value or max_value.", target=target)

    s = pd.to_numeric(df[column], errors="coerce")
    non_numeric = int((df[column].notna() & s.isna()).sum())
    if non_numeric == int(df[column].notna().sum()) and non_numeric:
        return _error("range", f"'{column}' holds no numeric values.", target=target)

    below = s < min_value if min_value is not None else pd.Series(False, index=s.index)
    above = s > max_value if max_value is not None else pd.Series(False, index=s.index)
    bad_mask = (below | above).fillna(False)
    bad = int(bad_mask.sum())
    checked = int(s.notna().sum())

    if bad:
        offenders = [float(v) for v in s[bad_mask].sort_values().unique()[:MAX_EXAMPLES]]
        return _result(
            "range",
            "fail",
            f"{bad} row(s) fall outside the expected range for {column} — "
            f"lowest {min(offenders)}, highest {max(offenders)}.",
            target=target,
            rows_checked=checked,
            failing_rows=_positions(bad_mask),
            detail={
                "violating_rows": bad,
                "below_min": int(below.fillna(False).sum()),
                "above_max": int(above.fillna(False).sum()),
                "example_values": offenders,
                "non_numeric_rows": non_numeric,
            },
        )

    return _result(
        "range",
        "pass",
        f"{column} stays within range across {checked} row(s).",
        target=target,
        rows_checked=checked,
        detail={"non_numeric_rows": non_numeric},
    )


def check_freshness(
    df: pd.DataFrame,
    column: str,
    max_age_hours: float,
    now: "pd.Timestamp | None" = None,
) -> dict[str, Any]:
    """Assert the newest timestamp is recent enough. dbt's source freshness.

    A table can pass every other check and still be three days stale, which is
    the failure mode nobody notices until a report is wrong.
    """
    target = {"column": column, "max_age_hours": max_age_hours}
    if column not in {str(c) for c in df.columns}:
        return _error(
            "freshness",
            f"'{column}' is not in the dataset.",
            target={**target, "available": [str(c) for c in df.columns]},
        )

    with warnings.catch_warnings():
        # a column that is not a timestamp is an expected outcome here, not news
        warnings.simplefilter("ignore", UserWarning)
        parsed = pd.to_datetime(df[column], errors="coerce", utc=True)
    if not parsed.notna().any():
        return _error("freshness", f"'{column}' holds no parseable timestamps.", target=target)

    reference = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if reference.tzinfo is None:
        reference = reference.tz_localize("UTC")

    newest = parsed.max()
    age_hours = round((reference - newest).total_seconds() / 3600, 3)
    detail = {
        "newest": str(newest),
        "oldest": str(parsed.min()),
        "age_hours": age_hours,
        "checked_against": str(reference),
        "unparseable_rows": int((df[column].notna() & parsed.isna()).sum()),
    }

    if age_hours > max_age_hours:
        return _result(
            "freshness",
            "fail",
            f"Newest {column} is {age_hours:.1f}h old, past the {max_age_hours}h limit.",
            target=target,
            rows_checked=int(parsed.notna().sum()),
            detail=detail,
        )

    return _result(
        "freshness",
        "pass",
        f"Newest {column} is {age_hours:.1f}h old, inside the {max_age_hours}h limit.",
        target=target,
        rows_checked=int(parsed.notna().sum()),
        detail=detail,
    )


CONVENTIONS = """\
Reading a data-quality report
=============================

1. Run infer_schema before anything else. Do not guess column names.
2. status has three values, not two. "error" means the check could not run and
   tells you nothing about the data. "fail" means it ran and the assertion did
   not hold. Never report an error as a clean result.
3. A passing test means the assertion held on the rows present. It does not mean
   the data is correct — a fully populated column of wrong values passes
   not_null cleanly.
4. Null rate matters more than null count. Ten nulls in twelve rows is a broken
   pipeline; ten in ten million is probably a late-arriving record.
5. Orphaned foreign keys are worth escalating even at a low rate. They usually
   indicate a load-order problem that will get worse, not a one-off.
6. failing_rows holds row positions as displayed, capped. If
   failing_rows_truncated is true, there are more than you can see.
7. Report the numbers you actually retrieved. If a check was not run, say it was
   not run rather than inferring the result.
"""
