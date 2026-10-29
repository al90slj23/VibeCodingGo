#!/usr/bin/env python3
"""Explicit engineering tool bootstrap; automatic updates live in the app runtime."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vibecue.engineering_install import main

if __name__ == '__main__':
    raise SystemExit(main())
