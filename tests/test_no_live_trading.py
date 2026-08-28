"""CI gate: no live-trading affordance may reappear in src/.

Mirrors the test_no_unauthorized_submit_order.py idiom. safety.py is excluded
because it is the module that *names* the forbidden states in order to block them.
"""

import subprocess
from pathlib import Path

# Resolve relative to this file, not cwd — a grep against a relative "src/"
# silently matches nothing (and every gate below passes vacuously) if pytest
# is ever invoked from outside the repo root.
_SRC = Path(__file__).resolve().parent.parent / "src"
_ALLOWED = ("safety.py",)


def _grep(pattern: str) -> list[str]:
    result = subprocess.run(
        ["grep", "-rn", pattern, str(_SRC), "--include=*.py"],
        capture_output=True,
        text=True,
    )
    return [
        line
        for line in result.stdout.splitlines()
        if not any(allowed in line.split(":")[0] for allowed in _ALLOWED)
    ]


def test_grep_gate_actually_scans_files() -> None:
    """Guard against the three gates below passing vacuously. Each of them
    asserts an *empty* result, so a broken scan (wrong cwd, bad path, grep
    misbehaving) is indistinguishable from a clean pass unless something
    else proves files were actually scanned. safety.py unconditionally
    defines LiveTradingBlocked, so a truly-empty match here means the scan
    itself is broken, not that the invariant holds."""
    result = subprocess.run(
        ["grep", "-rn", "LiveTradingBlocked", str(_SRC), "--include=*.py"],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip(), (
        f"sentinel grep under {_SRC} found nothing — the gate scanned zero files"
    )


def test_no_paper_false_in_src() -> None:
    offenders = _grep("paper=False")
    assert offenders == [], (
        "live-mode adapter construction found in src/:\n" + "\n".join(offenders)
    )


def test_no_alpaca_live_in_src() -> None:
    offenders = _grep("alpaca_live")
    assert offenders == [], (
        "alpaca_live reference found in src/:\n" + "\n".join(offenders)
    )


def test_no_moomoo_adapter_import_in_src() -> None:
    offenders = _grep("MoomooAdapter")
    assert offenders == [], (
        "MoomooAdapter reference found in src/:\n" + "\n".join(offenders)
    )
