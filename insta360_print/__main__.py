"""Allow ``python3 -m insta360_print``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
