"""Entry point kept at the repo root so existing Claude Desktop configs keep working.

The implementation moved into the `dq` package in 0.2.0.
"""

from dq.server import main

if __name__ == "__main__":
    main()
