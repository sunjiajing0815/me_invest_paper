"""CI gate: submit_order must only appear in services/auto_trade.py and brokers/."""

import subprocess


def test_submit_order_only_in_auto_trade_and_brokers() -> None:
    result = subprocess.run(
        ["grep", "-rn", "submit_order", "src/", "--include=*.py"],
        capture_output=True,
        text=True,
    )
    offenders = [
        line
        for line in result.stdout.splitlines()
        if "services/auto_trade.py" not in line and "brokers/" not in line
    ]
    assert offenders == [], (
        "submit_order called outside allowed files:\n" + "\n".join(offenders)
    )
