"""Running many checks at once, proposing a suite, and exporting the result.

`run_suite` calls the functions in `dq.core` directly. The previous version
called the MCP-decorated tools, which happens to work on SDK 1.x and breaks on
2.x where the decorator returns a wrapper rather than the function — the kind of
bug that only shows up on somebody else's machine.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Sequence

import pandas as pd

from . import core

SuiteSpec = dict[str, Any]


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------

def run_suite(
    df: pd.DataFrame,
    spec: SuiteSpec,
    name: str = "dataset",
    resolve_parent: Callable[[str], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    """Run every check in `spec` against `df` and return a combined report.

    `resolve_parent` is how relationship checks get hold of the parent table. The
    MCP server passes a file loader; the browser passes a lookup over datasets
    already in memory. If it is None, relationship checks report as errors rather
    than being silently skipped.
    """
    results: list[dict[str, Any]] = []

    for col in spec.get("not_null", []) or []:
        results.append(core.check_not_null(df, [col] if isinstance(col, str) else list(col)))

    for key in spec.get("unique", []) or []:
        results.append(core.check_unique(df, [key] if isinstance(key, str) else list(key)))

    for col, allowed in (spec.get("accepted_values") or {}).items():
        results.append(core.check_accepted_values(df, col, allowed))

    for col, bounds in (spec.get("range") or {}).items():
        bounds = bounds or {}
        results.append(
            core.check_range(df, col, bounds.get("min"), bounds.get("max"))
        )

    for col, opts in (spec.get("freshness") or {}).items():
        opts = opts or {}
        results.append(
            core.check_freshness(
                df, col, float(opts.get("max_age_hours", 24)), opts.get("now")
            )
        )

    for rel in spec.get("relationships", []) or []:
        results.append(_relationship(df, rel, resolve_parent))

    return summarise(results, name=name)


def _relationship(
    df: pd.DataFrame,
    rel: dict[str, Any],
    resolve_parent: Callable[[str], pd.DataFrame] | None,
) -> dict[str, Any]:
    column = rel.get("column")
    parent = rel.get("parent")
    parent_column = rel.get("parent_column", column)

    if not column or not parent:
        return core._error(
            "relationships",
            "A relationship needs both 'column' and 'parent'.",
            target=dict(rel),
        )
    if resolve_parent is None:
        return core._error(
            "relationships",
            "No way to load the parent dataset in this context.",
            target=dict(rel),
        )

    try:
        parent_df = resolve_parent(parent)
    except Exception as exc:
        return core._error(
            "relationships", f"Could not load parent '{parent}': {exc}", target=dict(rel)
        )

    result = core.check_relationship(df, column, parent_df, parent_column)
    result["target"]["parent"] = parent
    return result


def summarise(results: Sequence[dict[str, Any]], name: str = "dataset") -> dict[str, Any]:
    """Wrap a list of check results in a report with counts and an overall status."""
    failed = sum(r["status"] == "fail" for r in results)
    errored = sum(r["status"] == "error" for r in results)
    status = "fail" if failed else ("error" if errored else "pass")
    return {
        "dataset": name,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "tests_run": len(results),
        "tests_passed": sum(r["status"] == "pass" for r in results),
        "tests_failed": failed,
        "tests_errored": errored,
        "status": status,
        "results": list(results),
    }


# --------------------------------------------------------------------------
# proposing a suite
# --------------------------------------------------------------------------

ID_HINTS = ("id", "key", "code", "number", "no")
TIME_HINTS = ("date", "time", "_at", "ts", "timestamp")


def suggest_suite(df: pd.DataFrame, max_category_values: int = 12) -> dict[str, Any]:
    """Propose a starting suite from what the data already looks like.

    This encodes the first pass an engineer does by hand on an unfamiliar table:
    columns that are fully populated today should stay that way, an id that is
    currently unique is meant to be a key, a short string column is a status
    enum, a positive amount should not go negative, a timestamp column is worth
    a freshness bound. Every proposal carries the reason, because a suggested
    test nobody can justify is one that gets deleted at the first false alarm.
    """
    rows = len(df)
    spec: dict[str, Any] = {
        "not_null": [],
        "unique": [],
        "accepted_values": {},
        "range": {},
        "freshness": {},
    }
    reasons: list[dict[str, str]] = []

    if not rows:
        return {"suite": {}, "reasons": [], "note": "Empty dataset — nothing to propose."}

    for col in df.columns:
        name = str(col)
        lower = name.lower()
        s = df[col]
        nulls = int(s.isna().sum())
        populated = s.dropna()
        distinct = int(populated.nunique())

        if nulls == 0:
            spec["not_null"].append(name)
            reasons.append({"test": f"not_null({name})", "why": "no nulls in the sample"})

        looks_like_id = any(lower.endswith(h) or lower == h for h in ID_HINTS)
        if looks_like_id and distinct == rows and nulls == 0:
            spec["unique"].append([name])
            reasons.append(
                {"test": f"unique({name})", "why": f"every one of {rows} values is distinct"}
            )

        is_text = not (
            pd.api.types.is_numeric_dtype(s)
            or pd.api.types.is_bool_dtype(s)
            or pd.api.types.is_datetime64_any_dtype(s)
        )
        if is_text and 1 < distinct <= max_category_values and not looks_like_id:
            values = sorted({str(v) for v in populated.unique()})
            spec["accepted_values"][name] = values
            reasons.append(
                {
                    "test": f"accepted_values({name})",
                    "why": f"only {distinct} distinct values, so it reads as a status column",
                }
            )

        if pd.api.types.is_numeric_dtype(s) and len(populated):
            low = float(populated.min())
            if low >= 0 and not looks_like_id:
                spec["range"][name] = {"min": 0}
                reasons.append(
                    {"test": f"range({name} >= 0)", "why": "never negative in the sample"}
                )

        if any(h in lower for h in TIME_HINTS):
            parsed = pd.to_datetime(s, errors="coerce", utc=True)
            if parsed.notna().any():
                spec["freshness"][name] = {"max_age_hours": 24}
                reasons.append(
                    {
                        "test": f"freshness({name})",
                        "why": "parses as a timestamp, so staleness is worth bounding",
                    }
                )

    spec = {k: v for k, v in spec.items() if v}
    return {
        "suite": spec,
        "reasons": reasons,
        "note": (
            "These are proposals from one sample, not rules. Read each reason and "
            "delete the ones that describe an accident rather than an invariant."
        ),
    }


# --------------------------------------------------------------------------
# exporting
# --------------------------------------------------------------------------

def to_run_results(report: dict[str, Any]) -> dict[str, Any]:
    """Render a report in the shape of dbt's run_results.json.

    Lets an existing dbt-aware CI step read these results without learning a new
    format.
    """
    results = []
    for i, r in enumerate(report.get("results", [])):
        target = r.get("target", {})
        parts = [str(v) for v in target.values() if isinstance(v, (str, int, float))]
        unique_id = f"test.dq.{r['test']}_{'_'.join(parts) or i}".replace(" ", "_")
        results.append(
            {
                "unique_id": unique_id,
                "status": {"pass": "pass", "fail": "fail", "error": "error"}[r["status"]],
                "failures": len(r.get("failing_rows", [])) or r.get("detail", {}).get("violating_rows", 0),
                "message": r.get("message", ""),
                "execution_time": 0,
                "adapter_response": {},
                "thread_id": "Thread-1",
                "timing": [],
            }
        )

    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v5.json",
            "generated_at": report.get("generated_at"),
            "invocation_id": None,
            "env": {"generated_by": "dq-mcp"},
        },
        "results": results,
        "elapsed_time": 0,
        "args": {"which": "test", "dataset": report.get("dataset")},
    }
