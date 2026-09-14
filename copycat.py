#!/usr/bin/env python3
"""FPL Copycat - thin entry point. The logic lives in the copycat_core package.

Kept so the GitHub Actions workflow's `python copycat.py` keeps working unchanged
while the app is built around the same core. See copycat_core/__init__.py and
CLAUDE.md.
"""

import sys

from copycat_core.runner import cli

if __name__ == "__main__":
    sys.exit(cli())
