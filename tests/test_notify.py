import smtplib
from datetime import date, datetime, timezone

from opatchbot.aru import PatchFile
from opatchbot.config import NotifyConfig
from opatchbot.fetch import FetchResult
from opatchbot.manifest import Manifest, ManifestEntry
from opatchbot.notify import (Notifier, msg_changed, msg_first_pass, msg_heartbeat, msg_late, msg_needs_human,
                              msg_window_closed)
from opatchbot.state import CycleRecord


def manifest():
    e = ManifestEntry("pt-8.61", "peopletools", "resolved", "40014041", "PT 8.61.24 PRODUCT PATCH DPK", 233,
                      datetime(2026, 10, 19, tzinfo=timezone.utc), "newest", [PatchFile("a.zip", 10, "https://u/a.zip", "ab" * 32)], ["a.zip"])
    return Manifest("2026-10-CPU", "CPU", date(2026, 10, 20), datetime.now(timezone.utc), [e])


def test_subjects():
    m = manifest()
    ok = FetchResult("2026-10-CPU", verified=1)
    bad = FetchResult("2026-10-CPU", verified=0, failed=[("a.zip", "a.zip: SHA-256 mismatch")])
    assert msg_first_pass(m, ok)[0] == "[opatchbot] 2026-10-CPU: fetched"
    assert msg_first_pass(m, bad)[0] == "[opatchbot] 2026-10-CPU: partial"
    assert msg_changed(m, ok, ["a.zip"])[0].startswith("[opatchbot] 2026-10-CPU: ")
    assert msg_window_closed(m, bad)[0] == "[opatchbot] 2026-10-CPU: partial (window closed)"
    assert msg_needs_human("2026-10-CPU", "opatchbot login")[0] == "[opatchbot] 2026-10-CPU: needs human"
    rec = CycleRecord("2026-11-CSPU", "CSPU", date(2026, 11, 17), "scheduled", None, None, None, None, [])
    assert msg_late(rec)[0] == "[opatchbot] 2026-11-CSPU: no release detected"
    assert msg_heartbeat(rec)[0] == "[opatchbot] heartbeat"


def test_body_contains_manifest_table_and_errors():
    m = manifest()
    bad = FetchResult("2026-10-CPU", failed=[("a.zip", "a.zip: SHA-256 mismatch")])
    body = msg_first_pass(m, bad)[1]
    assert m.to_markdown().splitlines()[3] in body and "a.zip: SHA-256 mismatch" in body


def test_needs_human_names_command():
    assert "opatchbot login" in msg_needs_human("2026-10-CPU", "opatchbot login")[1]


def test_send_failure_returns_false(monkeypatch):
    class Boom:
        def __init__(self, *a, **k): raise OSError("relay down")
    monkeypatch.setattr(smtplib, "SMTP", Boom)
    n = Notifier(NotifyConfig("relay", 25, "a@b", ["c@d"], "smtp"))
    assert n.send("s", "b") is False


def test_stdout_mode_prints(capsys):
    assert Notifier(NotifyConfig("relay", 25, "a@b", ["c@d"], "stdout")).send("[opatchbot] x", "body") is True
    assert "[opatchbot] x" in capsys.readouterr().out


def test_resolve_email_sends_review_without_downloading(tmp_path, fixtures_dir, capsys):
    from datetime import date
    from opatchbot import cli
    from opatchbot.state import StateStore
    from tests.test_cli import DB, PT, FakeClient
    from opatchbot.config import Config, NotifyConfig
    from opatchbot.notify import Notifier
    from opatchbot.run import Services
    cfg = Config(tmp_path / "stage", tmp_path / "state", 3, 7, 14, NotifyConfig("h", 25, "a@b", ["c@d"], "stdout"), [PT, DB])
    client = FakeClient(fixtures_dir)
    svc = Services(cfg=cfg, store=StateStore(cfg.state_dir), client=client, notifier=Notifier(cfg.notify))
    assert cli.main(["resolve", "2026-10-CPU", "--email"], services=svc) == 0
    out = capsys.readouterr().out
    assert "=== [opatchbot] 2026-10-CPU: available patches (1 found, 1 not found) ===" in out
    assert "pt-8.61        patch 40014041" in out and "Review only: nothing was downloaded" in out
    assert client.downloads == [] and svc.store.load() == {}
