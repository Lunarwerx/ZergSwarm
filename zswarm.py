#!/usr/bin/env python
"""Entry point: python zswarm.py <command>. A clone is the install."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from zswarm.cli import main  # noqa: E402
sys.exit(main())
