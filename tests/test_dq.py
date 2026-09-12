"""Tests for the check engine.

These run against `dq.core` rather than the MCP tools on purpose — the tools are
wrappers, and testing through a transport that needs an SDK installed would make
the suite fail for reasons that have nothing to do with the checks.

The fixtures carry six planted defects and the exact counts are asserted, so a
change in behaviour shows up as a number that moved rather than a vague failure.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from dq import cli, core, suite
from dq.loaders import LoadError, load_bytes, load_path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ORDERS = FIXTURES / "orders.csv"
CUSTOMERS = FIXTURES / "customers.csv"
STATUSES = ["placed", "shipped", "delivered", "cancelled", "returned"]


@pytest.fixture(scope="module")
def orders() -> pd.DataFrame:
    return load_path(str(ORDERS))


@pytest.fixture(scope="module")
def customers() -> pd.DataFrame:
    return load_path(str(CUSTOMERS))


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def test_loads_expected_shape(orders, customers):
    assert len(orders) == 900
    assert len(customers) == 200
    assert list(orders.columns) == [
        "order_id",
        "customer_id",
        "status",
        "amount",
        "ordered_at",
    ]


def test_missing_file_is_a_load_error():
    with pytest.raises(LoadError, match="No file at"):
        load_path("fixtures/does-not-exist.csv")


def test_unsupported_extension_names_the_alternatives():
    with pytest.raises(LoadError, match="Supported"):
        load_bytes(b"a,b\n1,2\n", "data.xlsx")


def test_load_bytes_matches_load_path(orders):
    from_bytes = load_bytes(ORDERS.read_bytes(), "orders.csv")
    pd.testing.assert_frame_equal(from_bytes, orders)


# --------------------------------------------------------------------------
# describing
# --------------------------------------------------------------------------

def test_infer_schema(orders):
    schema = core.infer_schema(orders, name="orders")
    assert schema["rows"] == 900
    assert [c["name"] for c in schema["columns"]][0] == "order_id"


def test_profile_reports_the_planted_null_rate(orders):
    profile = core.profile_table(orders)
    customer = next(c for c in profile["columns"] if c["column"] == "customer_id")
    assert customer["nulls"] == 5
    assert customer["null_rate"] == pytest.approx(5 / 900, abs=1e-6)


def test_profile_handles_an_empty_frame():
    profile = core.profile_table(pd.DataFrame({"a": pd.Series(dtype="float64")}))
    assert profile["rows"] == 0
    assert profile["columns"][0]["null_rate"] == 0.0


# --------------------------------------------------------------------------
# the planted defects
# --------------------------------------------------------------------------

def test_unique_catches_the_duplicated_ids(orders):
    r = core.check_unique(orders, ["order_id"])
    assert r["status"] == "fail"
    assert r["detail"]["duplicate_rows"] == 5
    assert r["detail"]["worst_offenders"]["ORD-00013"] == 3
    assert len(r["failing_rows"]) == 5


def test_not_null_catches_the_missing_keys(orders):
    r = core.check_not_null(orders, ["customer_id"])
    assert r["status"] == "fail"
    assert r["detail"]["by_column"]["customer_id"]["null_rows"] == 5


def test_not_null_passes_on_a_clean_column(orders):
    r = core.check_not_null(orders, ["status"])
    assert r["status"] == "pass"
    assert r["rows_checked"] == 900


def test_relationship_catches_the_orphans(orders, customers):
    r = core.check_relationship(orders, "customer_id", customers, "customer_id")
    assert r["status"] == "fail"
    assert r["detail"]["orphan_rows"] == 3
    assert set(r["detail"]["example_orphans"]) == {"CUST-9991", "CUST-9992", "CUST-9993"}
    assert r["rows_checked"] == 895  # nulls are not orphans


def test_accepted_values_catches_the_stray_status(orders):
    r = core.check_accepted_values(orders, "status", STATUSES)
    assert r["status"] == "fail"
    assert r["detail"]["unexpected_values"] == {"pending_review": 2}


def test_range_catches_the_negative_amounts(orders):
    r = core.check_range(orders, "amount", min_value=0)
    assert r["status"] == "fail"
    assert r["detail"]["violating_rows"] == 2
    assert r["detail"]["below_min"] == 2


def test_freshness_uses_the_reference_time(orders):
    now = pd.Timestamp("2026-01-15T12:00:00Z")
    stale = core.check_freshness(orders, "ordered_at", 24, now=now)
    fresh = core.check_freshness(orders, "ordered_at", 48, now=now)
    assert stale["status"] == "fail"
    assert fresh["status"] == "pass"
    assert stale["detail"]["age_hours"] == pytest.approx(30, abs=0.01)


# --------------------------------------------------------------------------
# a check that cannot run is not a check that failed
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "call",
    [
        lambda df: core.check_not_null(df, ["nope"]),
        lambda df: core.check_unique(df, ["nope"]),
        lambda df: core.check_accepted_values(df, "nope", ["a"]),
        lambda df: core.check_range(df, "nope", 0, 1),
        lambda df: core.check_freshness(df, "nope", 24),
    ],
)
def test_missing_column_reports_error_not_fail(orders, call):
    r = call(orders)
    assert r["status"] == "error"
    assert "nope" in r["message"]


def test_range_needs_a_bound(orders):
    assert core.check_range(orders, "amount")["status"] == "error"


def test_accepted_values_needs_a_set(orders):
    assert core.check_accepted_values(orders, "status", [])["status"] == "error"


def test_freshness_on_a_non_timestamp_column_errors(orders):
    assert core.check_freshness(orders, "status", 24)["status"] == "error"


def test_relationship_names_the_side_that_is_wrong(orders, customers):
    r = core.check_relationship(orders, "customer_id", customers, "nope")
    assert r["status"] == "error"
    assert "parent" in r["message"]


# --------------------------------------------------------------------------
# suites
# --------------------------------------------------------------------------

def test_run_suite_counts_every_outcome(orders, customers):
    spec = {
        "not_null": ["customer_id", "status"],
        "unique": [["order_id"]],
        "accepted_values": {"status": STATUSES},
        "range": {"amount": {"min": 0}},
        "relationships": [
            {"column": "customer_id", "parent": "customers", "parent_column": "customer_id"}
        ],
    }
    report = suite.run_suite(
        orders, spec, name="orders", resolve_parent=lambda _: customers
    )
    assert report["tests_run"] == 6
    # not_null(customer_id), unique, accepted_values, range, relationships
    assert report["tests_failed"] == 5
    assert report["tests_passed"] == 1  # not_null(status)
    assert report["status"] == "fail"


def test_run_suite_calls_the_engine_not_the_decorated_tools(orders):
    """Regression: the old run_suite invoked the MCP-decorated functions.

    That works on SDK 1.x and breaks on 2.x. The suite runner must not depend on
    an MCP SDK being installed at all.
    """
    import sys

    assert "mcp" not in sys.modules
    report = suite.run_suite(orders, {"unique": [["order_id"]]})
    assert report["tests_run"] == 1


def test_relationship_without_a_resolver_errors(orders):
    spec = {"relationships": [{"column": "customer_id", "parent": "customers.csv"}]}
    report = suite.run_suite(orders, spec)
    assert report["results"][0]["status"] == "error"
    assert report["tests_failed"] == 0


def test_suggest_suite_finds_the_key_and_the_enum(orders):
    proposed = suite.suggest_suite(orders)["suite"]
    assert ["order_id"] not in proposed.get("unique", [])  # order_id is not unique here
    assert sorted(proposed["accepted_values"]["status"]) == sorted(STATUSES + ["pending_review"])
    assert "ordered_at" in proposed["freshness"]


def test_suggest_suite_on_clean_data_proposes_a_key(customers):
    proposed = suite.suggest_suite(customers)["suite"]
    assert ["customer_id"] in proposed["unique"]
    assert "customer_id" in proposed["not_null"]


def test_suggest_suite_explains_every_proposal(customers):
    suggested = suite.suggest_suite(customers)
    assert suggested["reasons"]
    assert all(r["why"] for r in suggested["reasons"])


def test_suggest_suite_on_empty_data():
    assert suite.suggest_suite(pd.DataFrame())["suite"] == {}


def test_run_results_export_is_dbt_shaped(orders):
    report = suite.run_suite(orders, {"unique": [["order_id"]]}, name="orders")
    artifact = suite.to_run_results(report)
    assert artifact["metadata"]["dbt_schema_version"].endswith("run-results/v5.json")
    assert artifact["results"][0]["status"] == "fail"


# --------------------------------------------------------------------------
# the cli, because CI is the reason it exists
# --------------------------------------------------------------------------

def test_cli_exits_1_when_an_assertion_fails(tmp_path, capsys):
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"unique": [["order_id"]]}))
    code = cli.main(["suite", str(ORDERS), "--spec", str(spec)])
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_exits_2_when_a_check_cannot_run(tmp_path):
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"not_null": ["nope"]}))
    assert cli.main(["suite", str(ORDERS), "--spec", str(spec)]) == 2


def test_cli_exits_0_on_clean_data(tmp_path):
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"unique": [["customer_id"]], "not_null": ["name"]}))
    assert cli.main(["suite", str(CUSTOMERS), "--spec", str(spec)]) == 0


def test_cli_writes_run_results(tmp_path):
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"unique": [["order_id"]]}))
    out = tmp_path / "run_results.json"
    cli.main(["suite", str(ORDERS), "--spec", str(spec), "--run-results", str(out)])
    assert json.loads(out.read_text())["results"][0]["status"] == "fail"


def test_cli_missing_file_exits_2(capsys):
    assert cli.main(["profile", "fixtures/nope.csv"]) == 2


# --------------------------------------------------------------------------
# the fixtures are the contract the README describes
# --------------------------------------------------------------------------

def test_fixtures_are_reproducible(tmp_path):
    import subprocess, shutil, sys

    staging = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, staging)
    subprocess.run([sys.executable, str(staging / "make_fixtures.py")], check=True)
    assert (staging / "orders.csv").read_bytes() == ORDERS.read_bytes()
    assert (staging / "customers.csv").read_bytes() == CUSTOMERS.read_bytes()
