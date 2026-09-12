"""A terminal front end for the same checks.

The point is CI. An MCP server is only reachable from an agent; a pipeline step
needs something it can run and get an exit code from. `dq suite orders.csv
--spec suite.json` exits 1 on a failing assertion and 2 when a check could not
run at all, so a broken column name does not read as a green build.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import core, suite
from .loaders import FrameCache, LoadError, load_path

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ERROR = 2

TICK = {"pass": "PASS", "fail": "FAIL", "error": " ERR"}


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _print_report(report: dict[str, Any]) -> None:
    print(f"{report['dataset']}  —  {report['tests_run']} check(s)\n")
    for r in report["results"]:
        print(f"  [{TICK[r['status']]}]  {r['test']}  {r['message']}")
    print(
        f"\n{report['tests_passed']} passed, "
        f"{report['tests_failed']} failed, "
        f"{report['tests_errored']} could not run."
    )


def _exit_code(report: dict[str, Any]) -> int:
    if report["tests_failed"]:
        return EXIT_FAILED
    if report["tests_errored"]:
        return EXIT_ERROR
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dq", description="dbt-style data quality checks over local files."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_schema = sub.add_parser("schema", help="column names, dtypes and row count")
    p_schema.add_argument("path")

    p_profile = sub.add_parser("profile", help="null rates, distinct counts, ranges")
    p_profile.add_argument("path")
    p_profile.add_argument("--samples", type=int, default=3)

    p_suggest = sub.add_parser("suggest", help="propose a suite from the data")
    p_suggest.add_argument("path")

    p_suite = sub.add_parser("suite", help="run a suite and set an exit code")
    p_suite.add_argument("path")
    p_suite.add_argument("--spec", required=True, help="path to a suite JSON file")
    p_suite.add_argument("--json", action="store_true", help="print the full report as JSON")
    p_suite.add_argument(
        "--run-results", metavar="FILE", help="also write a dbt-shaped run_results.json"
    )

    args = parser.parse_args(argv)
    cache = FrameCache(load_path)

    try:
        df = cache.get(args.path)
    except LoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.command == "schema":
        _print_json(core.infer_schema(df, name=args.path))
        return EXIT_OK

    if args.command == "profile":
        _print_json(core.profile_table(df, args.samples, name=args.path))
        return EXIT_OK

    if args.command == "suggest":
        _print_json(suite.suggest_suite(df))
        return EXIT_OK

    try:
        with open(args.spec, encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read suite spec: {exc}", file=sys.stderr)
        return EXIT_ERROR

    report = suite.run_suite(df, spec, name=args.path, resolve_parent=cache.get)

    if args.json:
        _print_json(report)
    else:
        _print_report(report)

    if args.run_results:
        with open(args.run_results, "w", encoding="utf-8") as fh:
            json.dump(suite.to_run_results(report), fh, indent=2, default=str)

    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
