"""Plain-text email notifications via an unauthenticated relay, or stdout for testing."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import NotifyConfig
from .fetch import FetchResult
from .manifest import Manifest
from .state import CycleRecord

log = logging.getLogger(__name__)
PREFIX = "[opatchbot]"


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    def send(self, subject: str, body: str) -> bool:
        if self.cfg.mode == "stdout":
            print(f"=== {subject} ===\n{body}")
            return True
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.cfg.sender, ", ".join(self.cfg.recipients)
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as s:
                s.send_message(msg)
            return True
        except (OSError, smtplib.SMTPException) as e:
            log.error("email send failed: %s", e)
            return False


def _outcome(manifest: Manifest, result: FetchResult) -> str:
    if result.auth_failed:
        return "needs human"
    missing = any(e.status == "missing" for e in manifest.entries)
    return "partial" if (result.failed or missing or not manifest.all_verified()) else "fetched"


def _summary(manifest: Manifest, result: FetchResult) -> str:
    lines = [f"Cycle {manifest.cycle_id} ({manifest.kind}), scheduled {manifest.scheduled.isoformat()}.",
             f"Verified now: {result.verified}; already present: {result.skipped}; failed: {len(result.failed)}.", ""]
    if result.failed:
        lines += ["Failures:"] + [f"  - {name}: {err}" for name, err in result.failed] + [""]
    missing = [e for e in manifest.entries if e.status == "missing"]
    if missing:
        lines += ["Not found yet (will retry daily until the window closes):"] + [f"  - {e.item}: {e.reason}" for e in missing] + [""]
    lines += ["Manifest:", "", manifest.to_markdown()]
    return "\n".join(lines)


def msg_first_pass(manifest: Manifest, result: FetchResult) -> tuple[str, str]:
    return f"{PREFIX} {manifest.cycle_id}: {_outcome(manifest, result)}", "First pass for this cycle.\n\n" + _summary(manifest, result)


def msg_changed(manifest: Manifest, result: FetchResult, newly_verified: list[str]) -> tuple[str, str]:
    body = "Progress since the last run. Newly verified:\n" + "".join(f"  - {n}\n" for n in newly_verified) + "\n" + _summary(manifest, result)
    return f"{PREFIX} {manifest.cycle_id}: {_outcome(manifest, result)}", body


def msg_window_closed(manifest: Manifest, result: FetchResult) -> tuple[str, str]:
    return f"{PREFIX} {manifest.cycle_id}: {_outcome(manifest, result)} (window closed)", "Cycle window closed; final state.\n\n" + _summary(manifest, result)


def msg_needs_human(cycle_id: str, command: str) -> tuple[str, str]:
    body = (f"The Oracle sign-in session has expired, so {cycle_id} cannot be fetched unattended.\n\n"
            f"On the staging server run:\n\n    {command}\n\nThe next scheduled run will resume automatically.")
    return f"{PREFIX} {cycle_id}: needs human", body


def msg_late(cycle: CycleRecord) -> tuple[str, str]:
    body = (f"{cycle.cycle_id} was scheduled for {cycle.scheduled.isoformat()} but no advisory has appeared on "
            f"https://www.oracle.com/security-alerts/ yet. Either Oracle is late or the page format changed; "
            f"check the page and `opatchbot status`.")
    return f"{PREFIX} {cycle.cycle_id}: no release detected", body


def msg_heartbeat(next_cycle: CycleRecord | None) -> tuple[str, str]:
    nxt = f"next expected cycle {next_cycle.cycle_id} on {next_cycle.scheduled.isoformat()}" if next_cycle else "no upcoming cycle known"
    return f"{PREFIX} heartbeat", f"opatchbot is alive; {nxt}."
