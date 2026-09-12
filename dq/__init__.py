"""dq — dbt-style data quality checks, usable from an agent, a terminal or a browser.

The engine lives in `dq.core` and knows nothing about how it is being called.
`dq.server` exposes it over the Model Context Protocol, `dq.cli` over a terminal,
and `docs/index.html` runs the same file in the browser under Pyodide.
"""

from .core import (  # noqa: F401
    CONVENTIONS,
    check_accepted_values,
    check_freshness,
    check_not_null,
    check_range,
    check_relationship,
    check_unique,
    infer_schema,
    profile_table,
)
from .loaders import LoadError, load_bytes, load_path  # noqa: F401
from .suite import run_suite, suggest_suite, summarise, to_run_results  # noqa: F401

__version__ = "0.2.0"
