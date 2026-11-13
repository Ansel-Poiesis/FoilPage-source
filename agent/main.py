# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Direct entry point used by the console EXE build."""
import sys
from pathlib import Path

if not __package__ and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
