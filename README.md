# dq-mcp

[![tests](https://github.com/jyoshnagoshika-spec/dq-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/jyoshnagoshika-spec/dq-mcp/actions/workflows/tests.yml)

An MCP server that gives a language model real data-quality tools instead of guesses.

Ask an LLM about a dataset it cannot inspect and it will describe the table it
*expects* to see. This server closes that gap: it exposes profiling and assertion
tools over the Model Context Protocol, so an agent has to go and look before it
says anything about your data.

The checks deliberately mirror the dbt test vocabulary — `not_null`, `unique`,
`relationships`, `accepted_values` — so the assertions you already enforce in a
pipeline are available to an agent at query time, under the same names.

**Try it without installing anything: [jyoshnagoshika-spec.github.io/dq-mcp](https://jyoshnagoshika-spec.github.io/dq-mcp/)**
— the demo page runs this repository's own `dq/core.py` in your browser under
Pyodide, on the included fixtures or on a file you open. Nothing is uploaded.

---

## Tools

| Tool | What it does |
| --- | --- |
| `infer_schema` | Column names, dtypes, row count. Cheap; run it first. |
| `profile_table` | Per-column null rate, distinct count, examples, numeric range |
| `check_not_null` | Asserts columns are fully populated |
| `check_unique` | Asserts a single or composite key is unique |
| `check_relationship` | Asserts every foreign key exists in the parent |
| `check_accepted_values` | Asserts a column stays inside an allowed set |
| `check_range` | Asserts a number stays between bounds |
| `check_freshness` | Asserts the newest row is recent enough |
| `suggest_suite` | Proposes a starting suite from the data, with a reason for each test |
| `run_suite` | Runs several checks in one call, returns a combined report |
| `export_run_results` | Same report in dbt's `run_results.json` shape, for CI |

There is also a `dq://conventions` resource holding the rules for reading a
report — most usefully, that a passing test means the assertion held, not that
the data is correct.

Reads CSV, TSV, JSON, JSONL and Parquet. Files above 512 MB are refused rather
than silently loaded into memory.

## Reading a result

Every check returns the same envelope:

```json
{
  "test": "unique",
  "status": "fail",
  "target": { "key": ["order_id"] },
  "rows_checked": 900,
  "failing_rows": [400, 401, 402, 12, 300],
  "detail": {
    "duplicate_rows": 5,
    "worst_offenders": { "ORD-00013": 3, "ORD-00301": 2 }
  },
  "message": "5 row(s) share a key that should be unique — ORD-00013 appears 3 times."
}
```

`status` has three values, not two. **`error`** means the check could not run — a
column name that does not exist, a missing file — and says nothing about the
data. **`fail`** means it ran and the assertion did not hold. Reporting a typo as
a data-quality failure buries the real ones, so the two are kept apart
everywhere, including in the CLI's exit code.

`failing_rows` holds row positions, capped at 200, so a caller can show the
offending rows rather than just a count. Failures name the worst offenders,
because "this column is not unique" is not actionable and "`ORD-00013` appears
three times" is.

---

## Quickstart

```bash
git clone https://github.com/jyoshnagoshika-spec/dq-mcp.git
cd dq-mcp

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[mcp,dev]"
python dq_server.py                # starts on stdio; Ctrl+C to stop
```

The server speaks MCP over stdio, so running it directly just waits for a client.
Nothing will print. That is correct behaviour — connect a client to use it.

### Connect it to Claude Desktop

Add this to `claude_desktop_config.json`:

- **macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows** — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "dq": {
      "command": "/absolute/path/to/dq-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/dq-mcp/dq_server.py"]
    }
  }
}
```

Use the **absolute path to the virtual environment's Python**, not plain
`python`. Claude Desktop does not inherit your shell's PATH, so a bare `python`
will find a system interpreter without `mcp` or `pandas` installed. This is the
single most common reason the server fails to appear.

Restart Claude Desktop fully — quit it, don't just close the window.

---

## Use it in CI

An MCP server is only reachable from an agent. A pipeline needs something it can
run and get an exit code from, so the same engine has a command line:

```bash
dq schema  fixtures/orders.csv
dq profile fixtures/orders.csv
dq suggest fixtures/orders.csv > suite.json

dq suite fixtures/orders.csv --spec suite.json
dq suite fixtures/orders.csv --spec suite.json --run-results run_results.json
```

```
fixtures/orders.csv  —  7 check(s)

  [PASS]  not_null  order_id fully populated across 900 row(s).
  [FAIL]  not_null  5 row(s) have a null in customer_id (worst null rate 0.56%).
  [FAIL]  unique  5 row(s) share a key that should be unique — ORD-00013 appears 3 times.
  [FAIL]  accepted_values  2 row(s) hold a value outside the allowed set — pending_review.
  [FAIL]  range  2 row(s) fall outside the expected range for amount — lowest -240.0, highest -19.99.
  [FAIL]  freshness  Newest ordered_at is 30.0h old, past the 24.0h limit.
  [FAIL]  relationships  3 row(s) reference a customer_id that does not exist — for example CUST-9991.

1 passed, 6 failed, 0 could not run.
```

Exit `0` when everything passed, `1` when an assertion failed, `2` when a check
could not run at all.

`--run-results` writes the report in dbt's `run_results.json` shape, so CI
tooling that already understands dbt artifacts can read these results without
learning a second format.

---

## Try it on the included fixtures

`fixtures/` holds 900 orders and 200 customers with six deliberate problems
planted in them, one per check:

| Problem | Tool that catches it | Result |
| --- | --- | --- |
| 5 rows share a duplicated `order_id` | `check_unique` | `ORD-00013` appears 3 times |
| 5 null `customer_id` values | `check_not_null` | 0.56% null rate |
| 3 orphaned foreign keys | `check_relationship` | `CUST-9991`, `CUST-9992`, `CUST-9993` |
| 2 rows with status `pending_review` | `check_accepted_values` | outside the allowed set |
| 2 negative `amount` values | `check_range` | −19.99 and −240.00 |
| newest `ordered_at` is 30 hours old | `check_freshness` | fails a 24h bound, passes 48h |

Ask Claude:

> Profile `fixtures/orders.csv`, suggest a test suite for it, then run that suite
> and tell me which failures are worth escalating.

`fixtures/make_fixtures.py` regenerates both files from a fixed seed, so the
counts above are asserted in the test suite rather than being folklore in a
README.

---

## Layout

```
dq/core.py      the checks. Plain functions over a DataFrame, no I/O, no MCP.
dq/suite.py     running many checks, proposing a suite, dbt export
dq/loaders.py   reading files, size limits, load errors worth showing a user
dq/server.py    the MCP surface — thin wrappers, no logic
dq/cli.py       the terminal surface, for CI
docs/           the browser surface, which imports dq/core.py as-is
```

One engine, three surfaces. That is the reason `run_suite` calls `dq.core`
directly: the previous version called the MCP-decorated tool functions, which
works on SDK 1.x and breaks on 2.x where the decorator returns a wrapper rather
than the original function. There is a regression test asserting the suite runner
works with no MCP SDK imported at all.

## Compatibility

The MCP Python SDK renamed its high-level server class in 2.0 (`FastMCP` became
`MCPServer`). `dq/server.py` imports whichever is present, so it runs on both 1.x
and 2.x without changes. Tested on Python 3.10, 3.11 and 3.12.

## Roadmap

- Warehouse-backed checks (Redshift, Snowflake) rather than files only
- Custom SQL-style expression assertions
- A GitHub Action wrapping the CLI, so a repo can add checks with four lines of YAML

## Licence

MIT — see [LICENSE](LICENSE).
