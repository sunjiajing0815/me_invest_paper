#!/usr/bin/env python
"""Seed target_allocation table from per-account targets YAML.

Idempotent per broker account: skips writes if the YAML content hash matches what's
already stored for that account. Loops every active broker account, reading
data/targets/<account_ref>.yaml (the primary falls back to settings.targets_path).
Run: uv run python scripts/load_targets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from investor.config import Settings, load_targets
from investor.db import init_db, session_scope
from investor.services.accounts import list_active_accounts, resolve_primary_account_ref
from investor.services.targets import (
    load_targets_into_db,
    targets_path_for_account,
    yaml_hash,
)


def main() -> None:
    settings = Settings()
    init_db(settings.sqlite_path)
    with session_scope() as sess:
        accounts = list_active_accounts(sess)
        primary_ref = resolve_primary_account_ref(sess)
    if not accounts:
        print("load_targets: no active broker accounts — nothing to seed")
        return
    for acct in accounts:
        path = targets_path_for_account(
            settings, acct.account_ref, is_primary=(acct.account_ref == primary_ref)
        )
        if path is None:
            print(f"load_targets: no targets file for account {acct.account_ref} "
                  f"({acct.nickname}); skipping")
            continue
        targets = load_targets(path)
        h = yaml_hash(path)
        with session_scope() as sess:
            result = load_targets_into_db(
                sess, targets, h, broker_account_id=acct.account_ref, source="yaml_direct"
            )
        print(f"load_targets: account {acct.account_ref} ({acct.nickname}) → {result}")


if __name__ == "__main__":
    main()
