"""Regenerate the demo fixtures.

    python fixtures/make_fixtures.py

Seeded, so it produces the same two files every time and the test suite can
assert exact counts. Six defects are planted on purpose, one per check:

    order_id      5 rows share a duplicated id       check_unique
    customer_id   5 nulls                            check_not_null
    customer_id   3 keys with no matching customer   check_relationship
    status        2 rows outside the allowed set     check_accepted_values
    amount        2 negative amounts                 check_range
    ordered_at    newest row is deliberately stale   check_freshness

Everything else is ordinary-looking data, which is the point: a fixture where
every row is broken teaches you nothing about rates.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path

HERE = Path(__file__).parent

ORDERS = 900
CUSTOMERS = 200
STATUSES = ["placed", "shipped", "delivered", "cancelled", "returned"]
REGIONS = ["north", "south", "east", "west"]

# Fixed so freshness is reproducible: the newest order is 30 hours before this,
# which fails a 24-hour bound and passes a 48-hour one.
NOW = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
NEWEST = NOW - dt.timedelta(hours=30)


def build_customers(rng: random.Random) -> list[dict[str, str]]:
    first = ["Ana", "Ravi", "Mei", "Tomas", "Priya", "Jonas", "Sara", "Diego", "Nina", "Omar"]
    last = ["Vargas", "Iyer", "Chen", "Novak", "Rao", "Berg", "Haddad", "Silva", "Kaur", "Okafor"]
    rows = []
    for i in range(1, CUSTOMERS + 1):
        signup = NEWEST - dt.timedelta(days=rng.randint(40, 900))
        rows.append(
            {
                "customer_id": f"CUST-{i:04d}",
                "name": f"{rng.choice(first)} {rng.choice(last)}",
                "region": rng.choice(REGIONS),
                "signup_date": signup.date().isoformat(),
            }
        )
    return rows


def build_orders(rng: random.Random, customers: list[dict[str, str]]) -> list[dict[str, str]]:
    ids = [c["customer_id"] for c in customers]
    rows = []
    for i in range(1, ORDERS + 1):
        ordered_at = NEWEST - dt.timedelta(minutes=rng.randint(0, 60 * 24 * 21))
        rows.append(
            {
                "order_id": f"ORD-{i:05d}",
                "customer_id": rng.choice(ids),
                "status": rng.choice(STATUSES),
                "amount": f"{rng.uniform(8, 940):.2f}",
                "ordered_at": ordered_at.isoformat(),
            }
        )

    # the newest order, so freshness has a fixed answer
    rows[0]["ordered_at"] = NEWEST.isoformat()

    # unique: ORD-00013 three times, ORD-00301 twice -> 5 duplicated rows
    rows[400]["order_id"] = "ORD-00013"
    rows[401]["order_id"] = "ORD-00013"
    rows[402]["order_id"] = "ORD-00301"

    # not_null: 5 missing foreign keys
    for i in (17, 118, 259, 604, 801):
        rows[i]["customer_id"] = ""

    # relationships: 3 orphans
    for i, orphan in zip((33, 250, 707), ("CUST-9991", "CUST-9992", "CUST-9993")):
        rows[i]["customer_id"] = orphan

    # accepted_values: 2 rows outside the set
    rows[88]["status"] = "pending_review"
    rows[512]["status"] = "pending_review"

    # range: 2 negative amounts
    rows[45]["amount"] = "-19.99"
    rows[666]["amount"] = "-240.00"

    return rows


def write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path.relative_to(HERE.parent)}  ({len(rows)} rows)")


def main() -> None:
    rng = random.Random(20260115)
    customers = build_customers(rng)
    orders = build_orders(rng, customers)
    write(HERE / "customers.csv", customers)
    write(HERE / "orders.csv", orders)


if __name__ == "__main__":
    main()
