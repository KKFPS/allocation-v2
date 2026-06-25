#!/usr/bin/env python3
"""
CLI wrapper for the DB clone job.

Examples:
  python scripts/clone_db_window.py --site-id 10 --window-start "2026-02-11 04:00:00"
  python scripts/clone_db_window.py --site-ids 10,11,12 --window-hours 1
  python scripts/clone_db_window.py --client-id 5 --site-id 10 --vehicle-ids 101,102
  python scripts/clone_db_window.py --site-id 10 --dry-run
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.jobs.db_clone import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
