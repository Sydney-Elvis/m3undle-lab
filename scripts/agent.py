#!/usr/bin/env python3
"""Configure se-lab for this product checkout before loading its CLI."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from agent.runtime import configure

configure(repo_root=REPO_ROOT, product_name="m3undle", env_prefix="M3UNDLE")

# Product registrations must happen before agent.cli builds its parser.
import m3undle_lab.commands  # noqa: F401,E402

from agent.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user.", flush=True)
        raise SystemExit(130)

