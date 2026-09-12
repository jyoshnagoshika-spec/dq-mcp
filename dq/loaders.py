"""Reading tabular files into DataFrames, with failures that say what to do next.

Kept separate from the checks so the engine can run anywhere a DataFrame can be
built — a file on disk, an upload in the browser, a query result.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

MAX_BYTES = 512 * 1024 * 1024  # refuse to pull anything enormous into memory

CSV_SUFFIXES = {".csv", ".txt"}
TSV_SUFFIXES = {".tsv"}
JSON_SUFFIXES = {".json"}
JSONL_SUFFIXES = {".jsonl", ".ndjson"}
PARQUET_SUFFIXES = {".parquet", ".pq"}

SUPPORTED = sorted(
    s.lstrip(".")
    for s in CSV_SUFFIXES | TSV_SUFFIXES | JSON_SUFFIXES | JSONL_SUFFIXES | PARQUET_SUFFIXES
)


class LoadError(Exception):
    """Raised when a dataset cannot be read. The message is meant for a human."""


def load_path(path: str) -> pd.DataFrame:
    """Load a dataset from disk. Raises LoadError with an actionable message."""
    p = Path(path).expanduser()

    if not p.exists():
        raise LoadError(f"No file at {p}")
    if p.is_dir():
        raise LoadError(f"{p} is a directory. Point this at a single file.")

    size = p.stat().st_size
    if size > MAX_BYTES:
        raise LoadError(
            f"{p.name} is {size / 1e6:.0f} MB, above the {MAX_BYTES / 1e6:.0f} MB limit. "
            "Point this at a sample or a single partition instead."
        )

    suffix = p.suffix.lower()
    try:
        if suffix in PARQUET_SUFFIXES:
            return pd.read_parquet(p)
        if suffix in CSV_SUFFIXES:
            return pd.read_csv(p)
        if suffix in TSV_SUFFIXES:
            return pd.read_csv(p, sep="\t")
        if suffix in JSONL_SUFFIXES:
            return pd.read_json(p, lines=True)
        if suffix in JSON_SUFFIXES:
            return pd.read_json(p)
    except LoadError:
        raise
    except Exception as exc:  # pandas raises a wide range of parse errors
        raise LoadError(f"Could not parse {p.name}: {exc}") from exc

    raise LoadError(
        f"Unsupported file type '{suffix or p.name}'. Supported: {', '.join(SUPPORTED)}."
    )


def load_bytes(data: bytes, filename: str) -> pd.DataFrame:
    """Load a dataset already in memory. Used by the browser demo and by tests."""
    suffix = Path(filename).suffix.lower()
    if len(data) > MAX_BYTES:
        raise LoadError(f"{filename} is above the {MAX_BYTES / 1e6:.0f} MB limit.")

    buf = io.BytesIO(data)
    try:
        if suffix in PARQUET_SUFFIXES:
            return pd.read_parquet(buf)
        if suffix in CSV_SUFFIXES:
            return pd.read_csv(buf)
        if suffix in TSV_SUFFIXES:
            return pd.read_csv(buf, sep="\t")
        if suffix in JSONL_SUFFIXES:
            return pd.read_json(buf, lines=True)
        if suffix in JSON_SUFFIXES:
            return pd.read_json(buf)
    except Exception as exc:
        raise LoadError(f"Could not parse {filename}: {exc}") from exc

    raise LoadError(
        f"Unsupported file type '{suffix or filename}'. Supported: {', '.join(SUPPORTED)}."
    )


class FrameCache:
    """Loads each path once per process.

    A suite of eight checks against one table used to read the file eight times.
    """

    def __init__(self, loader=load_path) -> None:
        self._loader = loader
        self._frames: dict[str, pd.DataFrame] = {}

    def get(self, path: str) -> pd.DataFrame:
        if path not in self._frames:
            self._frames[path] = self._loader(path)
        return self._frames[path]

    def put(self, name: str, df: pd.DataFrame) -> None:
        self._frames[name] = df

    def clear(self) -> None:
        self._frames.clear()
