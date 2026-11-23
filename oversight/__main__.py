"""Entry point for `python -m oversight <command>`."""

import sys

from oversight.cli import main

if __name__ == "__main__":
    sys.exit(main())
