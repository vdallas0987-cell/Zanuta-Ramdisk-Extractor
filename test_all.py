#!/usr/bin/env python3
"""
Test runner for Zanuta Ramdisk Extractor.

Runs all tests with stdlib unittest (no external dependencies).

Usage::

    python test_all.py          # run all tests
    python test_all.py -v       # verbose
    python test_all.py [-k pattern]  # filter by keyword
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.resolve()


def main() -> int:
    # Ensure the project root is on sys.path
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))

    verbosity = 2 if "-v" in sys.argv or "--verbose" in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
