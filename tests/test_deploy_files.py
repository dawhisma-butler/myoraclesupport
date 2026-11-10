import re
from pathlib import Path

from opatchbot.cli import COMMANDS

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_files_reference_real_commands():
    texts = {p: p.read_text() for p in [ROOT / "README.md", *sorted((ROOT / "deploy").iterdir())]}
    for path, text in texts.items():
        for sub in re.findall(r"(?:^|[/`])opatchbot (\w+)", text, re.M):  # command position only (not the service user)
            assert sub in COMMANDS, f"{path.name} references unknown subcommand {sub!r}"
    timer = (ROOT / "deploy" / "opatchbot.timer").read_text()
    assert "OnCalendar=*-*-* 07:00:00" in timer
    cron = (ROOT / "deploy" / "cron.example").read_text()
    daily = [l for l in cron.splitlines() if "opatchbot monitor" in l and not l.startswith("#")]
    monthly = [l for l in cron.splitlines() if "opatchbot heartbeat" in l and not l.startswith("#")]
    weekly = [l for l in cron.splitlines() if "opatchbot login" in l and not l.startswith("#")]
    assert len(daily) == 1 and len(monthly) == 1 and monthly[0].split()[2] == "1"
    assert len(weekly) == 1 and weekly[0].split()[4] == "0"
    assert "OnCalendar=Sun" in (ROOT / "deploy" / "opatchbot-login.timer").read_text()
