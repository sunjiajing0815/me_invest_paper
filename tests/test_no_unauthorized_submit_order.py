"""CI gate: submit_order must only appear in services/auto_trade.py and brokers/."""

import subprocess
from pathlib import Path

# Resolve relative to this file, not cwd — a grep against a relative "src/"
# silently matches nothing (and this gate passes vacuously) if pytest is
# ever invoked from outside the repo root.
_SRC = Path(__file__).resolve().parent.parent / "src"


def test_submit_order_only_in_auto_trade_and_brokers() -> None:
    result = subprocess.run(
        ["grep", "-rn", "submit_order", str(_SRC), "--include=*.py"],
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    # Guard against the gate passing vacuously: an empty result here would
    # otherwise be indistinguishable from "no offenders". submit_order() is
    # always defined in brokers/, so a truly-empty match means the scan
    # itself is broken (wrong cwd, bad path), not that the invariant holds.
    assert lines, f"grep for 'submit_order' under {_SRC} found nothing — gate scanned zero files"
    offenders = [
        line
        for line in lines
        if "services/auto_trade.py" not in line and "brokers/" not in line
    ]
    assert offenders == [], (
        "submit_order called outside allowed files:\n" + "\n".join(offenders)
    )
