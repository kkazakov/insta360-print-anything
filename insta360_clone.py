#!/usr/bin/env python3
"""Entry point for insta360-print-anything.

The implementation lives in the ``insta360_print`` package; this file only
keeps the familiar ``python3 insta360_clone.py …`` invocation working (and is
also reachable as ``python3 -m insta360_print``).
"""

import sys

from insta360_print.cli import main

if __name__ == "__main__":
    sys.exit(main())
