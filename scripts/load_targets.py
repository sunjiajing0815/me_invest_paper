#!/usr/bin/env python
"""Seed target_allocation table from targets.yaml.

Idempotent: skips writes if the YAML content hash matches what's already stored.
Run: uv run python scripts/load_targets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from investor.config import Settings, load_targets
from investor.db import init_db, session_scope
from investor.services.targets import load_targets_into_db, yaml_hash


def main() -> None:
    settings = Settings()
    init_db(settings.duckdb_path)
    h = yaml_hash(settings.targets_path)
    targets = load_targets(settings.targets_path)
    with session_scope() as sess:
        result = load_targets_into_db(sess, targets, h)
    print(f"load_targets: {result}")


if __name__ == "__main__":
    main()
