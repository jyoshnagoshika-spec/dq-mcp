# dq-mcp

An MCP server that gives a language model real data-quality tools instead of guesses.

Ask an LLM about a dataset it cannot inspect and it will describe the table it
*expects* to see. This server closes that gap: it exposes profiling and assertion
tools over the Model Context Protocol, so an agent has to go and look before it
says anything about your data.

The checks deliberately mirror the dbt test vocabulary — `not_null`, `unique`,
`relationships`, `accepted_values` — so the assertions you already enforce in a
pipeline are available to an agent at query time, under the same names.

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
| `run_suite` | Runs several checks in one call, returns a combined report |

There is also a `dq://conventions` resource holding the rules for reading a
report — most usefully, that a passing test means the assertion held, not that
the data is correct.

Reads CSV, TSV, JSON, JSONL and Parquet. Files above 512 MB are refused rather
than silently loaded into memory.

---

## Quickstart

```bash
git clone https://github.com/jyoshnagoshika-spec/dq-mcp.git
cd dq-mcp

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
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

## Try it on the included fixtures

`fixtures/` contains 900 orders and 200 customers with four deliberate quality
problems planted in them. Ask Claude:

> Profile `fixtures/orders.csv`, then check that `order_id` is unique, that
> `customer_id` is never null, and that every `customer_id` exists in
> `fixtures/customers.csv`.

It should find all four:

| Problem | Tool that catches it | Result |
| --- | --- | --- |
| 5 duplicated `order_id` values | `check_unique` | `ORD-00013` appears 3 times |
| 5 null `customer_id` values | `check_not_null` | 0.56% null rate |
| 3 orphaned foreign keys | `check_relationship` | `CUST-9991`, `CUST-9992`, `CUST-9993` |
| 2 rows with status `pending_review` | `check_accepted_values` | outside the allowed set |

---

## Example output

```
> check_unique(path="fixtures/orders.csv", columns=["order_id"])

{
  "status": "fail",
  "test": "unique",
  "key": ["order_id"],
  "duplicate_rows": 5,
  "worst_offenders": { "ORD-00013": 3, "ORD-00301": 2 }
}
```

Failures name the worst offenders, because "this column is not unique" is not
actionable and "`ORD-00013` appears three times" is.

---

## Compatibility

The MCP Python SDK renamed its high-level server class in 2.0 (`FastMCP` became
`MCPServer`). `dq_server.py` imports whichever is present, so it runs on both
1.x and 2.x without changes.

## Roadmap

- Warehouse-backed checks (Redshift, Snowflake) rather than files only
- Freshness assertions against a timestamp column
- Emit results in the dbt `run_results.json` shape for CI

## Licence

MIT — see [LICENSE](LICENSE).
