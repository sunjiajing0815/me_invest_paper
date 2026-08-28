"""CI gate: no live-trading affordance may reappear in src/.

Mirrors the test_no_unauthorized_submit_order.py idiom. safety.py is excluded
because it is the module that *names* the forbidden states in order to block them.
"""

import subprocess

_ALLOWED = ("safety.py",)


def _grep(pattern: str) -> list[str]:
    result = subprocess.run(
        ["grep", "-rn", pattern, "src/", "--include=*.py"],
        capture_output=True,
        text=True,
    )
    return [
        line
        for line in result.stdout.splitlines()
        if not any(allowed in line.split(":")[0] for allowed in _ALLOWED)
    ]


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
