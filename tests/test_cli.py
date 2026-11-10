import hashlib
from datetime import date
from pathlib import Path

import pytest

from opatchbot import cli
from opatchbot.aru import AuthError, parse_search_xml
from opatchbot.config import Config, InventoryItem, NotifyConfig
from opatchbot.notify import Notifier
from opatchbot.run import Services, monitor_once, run_cycle
from opatchbot.state import CycleRecord, StateStore

PT = InventoryItem("pt-8.61", "peopletools", "pt_product_dpk", "monthly", 233, 21917, 600000000204773, {"file_prefix": "PEOPLETOOLS-WIN-8.61"})
DB = InventoryItem("db19-ru", "database", "db_release_update", "quarterly", 226, 9480, 600000000101150, {"abstract_regex": "^DATABASE RELEASE UPDATE 19\\.", "search": {"patch_type": "all"}})


class FakeClient:
    """search() serves the PT fixture (sha256 rewritten to match fake bodies); download() can fail by name."""
    def __init__(self, fixtures_dir, fail_names=(), raise_search=None):
        self.patches = parse_search_xml((fixtures_dir / "search_pt861_233.xml").read_text())
        self.body = b"z" * 100
        for p in self.patches:
            for f in p.files:
                f.size, f.sha256 = 100, hashlib.sha256(self.body).hexdigest()
        self.fail_names, self.raise_search, self.downloads = set(fail_names), raise_search, []

    def search(self, **params):
        if self.raise_search:
            raise self.raise_search
        return self.patches

    def download(self, file, dest, resume=True):
        self.downloads.append(file.name)
        if file.name in self.fail_names:
            raise OSError("boom")
        Path(dest).parent.mkdir(parents=True, exist_ok=True); Path(dest).write_bytes(self.body); return Path(dest)


@pytest.fixture
def svc(tmp_path, fixtures_dir):
    cfg = Config(tmp_path / "stage", tmp_path / "state", 3, 7, 14, NotifyConfig("h", 25, "a@b", ["c@d"], "stdout"), [PT])
    store = StateStore(cfg.state_dir)
    store.upsert(CycleRecord("2026-10-CPU", "CPU", date(2026, 10, 20), "released", "https://x", date(2026, 10, 21), None, None, []))
    return Services(cfg=cfg, store=store, client=FakeClient(fixtures_dir), notifier=Notifier(cfg.notify),
                    page_fetch=lambda: (fixtures_dir / "security_alerts.html").read_text(), browser=None, token_store=None)


def test_run_first_pass_partial_then_fetched(svc, capsys):
    svc.client.fail_names = {"PEOPLETOOLS-WIN-8.61.24_2of4.zip"}
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 1
    out = capsys.readouterr().out
    assert "2026-10-CPU: partial" in out and "First pass" in out and svc.store.load()["2026-10-CPU"].status == "partial"
    svc.client.fail_names = set()
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 22)) == 0
    out = capsys.readouterr().out
    assert "2026-10-CPU: fetched" in out and "Newly verified" in out and svc.store.load()["2026-10-CPU"].status == "fetched"
    assert (svc.cfg.stage_root / "2026-10-CPU" / "MANIFEST.md").exists()


def test_missing_expected_item_keeps_cycle_partial(svc, capsys):
    svc.cfg.inventory.append(DB)  # fake search has no database patches -> db19-ru resolves 'missing'
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 1
    out = capsys.readouterr().out
    assert "2026-10-CPU: partial" in out and "Not found yet" in out and "db19-ru" in out
    assert svc.store.load()["2026-10-CPU"].status == "partial"


def test_run_needs_human(svc, capsys):
    svc.client.raise_search = AuthError("401")
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 2
    assert "opatchbot login" in capsys.readouterr().out and svc.store.load()["2026-10-CPU"].status == "released"


def test_run_window_closed_marks_failed(svc, capsys):
    svc.client.fail_names = {"PEOPLETOOLS-WIN-8.61.24_2of4.zip"}
    assert run_cycle("2026-10-CPU", svc, date(2026, 11, 5)) == 3
    assert "(window closed)" in capsys.readouterr().out and svc.store.load()["2026-10-CPU"].status == "failed"


def test_monitor_quiet_day(svc, capsys, monkeypatch):
    svc.store.transition("2026-10-CPU", "fetched")
    calls = []
    monkeypatch.setattr("opatchbot.run.run_cycle", lambda cid, s, today: calls.append(cid) or 0)
    assert monitor_once(svc, date(2026, 11, 9)) == 0 and calls == [] and capsys.readouterr().out == ""
    assert svc.store.load()["2026-11-CSPU"].status == "scheduled"


def test_monitor_release_day_runs_once_per_day(svc, monkeypatch, tmp_path):
    store = StateStore(tmp_path / "fresh"); svc.store = store
    calls = []
    def fake_run(cid, s, today):
        calls.append(cid); rec = s.store.load()[cid]; rec.last_run = __import__("datetime").datetime(today.year, today.month, today.day, 7, tzinfo=__import__("datetime").timezone.utc); s.store.upsert(rec); return 1
    monkeypatch.setattr("opatchbot.run.run_cycle", fake_run)
    assert monitor_once(svc, date(2026, 10, 21)) == 1 and calls == ["2026-10-CPU"]
    assert monitor_once(svc, date(2026, 10, 21)) == 0 and calls == ["2026-10-CPU"]  # same day: not re-run, nothing to report
    assert monitor_once(svc, date(2026, 10, 22)) == 1 and calls == ["2026-10-CPU", "2026-10-CPU"]  # next day: re-run


def test_monitor_late_cycle_emails_once(svc, capsys, monkeypatch):
    svc.store.transition("2026-10-CPU", "fetched")
    svc.page_fetch = lambda: "<html>no schedule here</html>"
    svc.store.upsert(CycleRecord("2026-11-CSPU", "CSPU", date(2026, 11, 17), "scheduled", None, None, None, None, []))
    monkeypatch.setattr("opatchbot.run.aru_fallback_released", lambda c, d: False)
    monitor_once(svc, date(2026, 11, 21)); monitor_once(svc, date(2026, 11, 22))
    assert capsys.readouterr().out.count("no release detected") == 1


def test_prune_keeps_n(svc, capsys):
    for cid in ("2026-05-CSPU", "2026-06-CSPU", "2026-07-CPU", "2026-08-CSPU", "2026-09-CSPU"):
        (svc.cfg.stage_root / cid).mkdir(parents=True)
        svc.store.upsert(CycleRecord(cid, "CSPU", date(2026, 6, 16), "partial" if cid == "2026-05-CSPU" else "fetched", None, None, None, None, []))
    assert cli.main(["prune", "--yes"], services=svc) == 0
    kept = sorted(p.name for p in svc.cfg.stage_root.iterdir())
    assert kept == ["2026-05-CSPU", "2026-07-CPU", "2026-08-CSPU", "2026-09-CSPU"]


def test_status_prints_cycles(svc, capsys):
    assert cli.main(["status"], services=svc) == 0
    assert "2026-10-CPU" in capsys.readouterr().out


def test_global_options_accepted_after_subcommand():
    args = cli.build_parser().parse_args(["run", "2026-10-CPU", "--notify", "stdout", "--today", "2026-10-21"])
    assert (args.cmd, args.cycle_id, args.notify, args.today) == ("run", "2026-10-CPU", "stdout", "2026-10-21")
    args = cli.build_parser().parse_args(["--notify", "stdout", "status"])
    assert (args.cmd, args.notify) == ("status", "stdout")


# ---------------------------------------------------------------- fix pass (final review) -----------------
class ReplayClient(FakeClient):
    """Serves only patches released on/before `today`, so day-by-day replays see patches appear over time."""
    def __init__(self, fixtures_dir, names):
        super().__init__(fixtures_dir)
        from opatchbot.aru import parse_search_xml as parse
        for n in names:
            self.patches += parse((fixtures_dir / n).read_text())
        for p in self.patches:
            for f in p.files:
                f.size, f.sha256 = 100, __import__("hashlib").sha256(self.body).hexdigest()
        self.today = date(2026, 10, 20)

    def search(self, **params):
        prod = params.get("product")
        return [p for p in self.patches if p.released.date() <= self.today and (prod is None or p.product_id == prod or prod == 21917)]


HCM = InventoryItem("hcm-9.2", "apps", "app_release_patchset", "monthly", 233, 21858, 27001300090200, {"app_code": "HCM"})


def test_fetched_cycle_keeps_reresolving_until_window_closes(svc, fixtures_dir, capsys):
    svc.cfg.inventory[:] = [HCM]
    svc.client = ReplayClient(fixtures_dir, ["search_hcm_92_233.xml"])
    svc.store.upsert(CycleRecord("2026-10-CPU", "CPU", date(2026, 10, 20), "released", "https://x", date(2026, 10, 21), None, None, []))
    picks = {}
    for day in range(21, 36):  # Oct 21 .. Nov 4 (window closes Nov 3)
        today = date(2026, 10, day) if day <= 31 else date(2026, 11, day - 31)
        svc.client.today = today
        monitor_once(svc, today)
        from opatchbot.manifest import Manifest
        picks[today] = Manifest.from_json(svc.cfg.stage_root / "2026-10-CPU" / "manifest.json").entries[0].patch
    out = capsys.readouterr().out
    assert picks[date(2026, 10, 26)] == "40016823"  # 10-21 patchset, verified -> cycle 'fetched' early
    assert picks[date(2026, 10, 28)] == "40016683"  # 10-28 patchset picked up although cycle was already fetched
    assert svc.store.load()["2026-10-CPU"].status == "fetched"
    assert "Progress since the last run" in out and out.count("(window closed)") == 1
    # superseded file moved aside, current file present
    item_dir = svc.cfg.stage_root / "2026-10-CPU" / "apps" / "hcm-9.2"
    assert (item_dir / "updPRJ40016683.zip").exists() and not (item_dir / "updPRJ40016823.zip").exists()
    assert (item_dir / "superseded" / "updPRJ40016823.zip").exists()


def test_auth_error_triggers_token_refresh_and_retry(svc, capsys):
    class FlakyAuth(FakeClient):
        def __init__(self, fx): super().__init__(fx); self.n = 0
        def search(self, **params):
            self.n += 1
            if self.n == 1: raise AuthError("401")
            return self.patches
    svc.client = FlakyAuth(svc.client.patches and __import__("pathlib").Path(__file__).parent / "fixtures")
    refreshed = []
    svc.browser = type("B", (), {"fetch_token": lambda self: refreshed.append(1) or "tok"})()
    svc.token_store = type("T", (), {"write": lambda self, t: None, "expires_at": lambda self: None})()
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 0
    assert refreshed == [1] and "needs human" not in capsys.readouterr().out


def test_session_expired_on_refresh_is_needs_human(svc, capsys):
    from opatchbot.token import SessionExpired
    svc.client.raise_search = AuthError("401")
    svc.browser = type("B", (), {"fetch_token": lambda self: (_ for _ in ()).throw(SessionExpired("login page"))})()
    svc.token_store = type("T", (), {"write": lambda self, t: None, "expires_at": lambda self: None})()
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 2
    assert "opatchbot login" in capsys.readouterr().out


def test_run_cycle_survives_transient_errors(svc, capsys, monkeypatch):
    from opatchbot.aru import AruError
    svc.client.raise_search = AruError("HTTP 500")
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 4
    rec = svc.store.load()["2026-10-CPU"]
    assert rec.last_result.startswith("error:") and rec.status == "released"
    # monitor keeps going for other cycles and does not raise
    assert monitor_once(svc, date(2026, 10, 22)) == 4


def test_email_failure_is_not_marked_sent_and_exit_nonzero(svc, monkeypatch):
    monkeypatch.setattr(svc.notifier, "send", lambda s, b: False)
    rc = run_cycle("2026-10-CPU", svc, date(2026, 10, 21))
    rec = svc.store.load()["2026-10-CPU"]
    assert rc != 0 and rec.notified == [] and "email failed" in rec.last_result


def test_resolve_cli_has_no_side_effects(svc, capsys):
    run_cycle("2026-10-CPU", svc, date(2026, 10, 21))
    before = (svc.cfg.stage_root / "2026-10-CPU" / "manifest.json").read_text()
    assert cli.main(["resolve", "2026-10-CPU"], services=svc) == 0
    assert (svc.cfg.stage_root / "2026-10-CPU" / "manifest.json").read_text() == before
    assert cli.main(["resolve", "2026-11-CSPU"], services=svc) == 0
    assert "2026-11-CSPU" not in svc.store.load()


def test_expired_session_triggers_auto_login_when_enabled(svc, capsys, tmp_path):
    from opatchbot.token import SessionExpired
    secrets = tmp_path / "secrets.env"; secrets.write_text("MOS_USER=u\nMOS_PASS=p\nMOS_TOTP_SEED=JBSWY3DPEHPK3PXP\n"); secrets.chmod(0o600)
    svc.secrets_path = secrets
    svc.client.raise_search = AuthError("401")
    state = {"logged_in": False, "login_calls": []}
    class Browser:
        def fetch_token(self):
            if not state["logged_in"]:
                raise SessionExpired("login page")
            return "tok"
        def login(self, s, code_prompt=None, debug_dir=None):
            state["login_calls"].append(s.mos_user); state["logged_in"] = True
    svc.browser = Browser()
    svc.token_store = type("T", (), {"write": lambda self, t: None, "expires_at": lambda self: None})()
    class Recover(type(svc.client)):
        pass
    # after the refresh the search must succeed: clear the forced error once the browser has logged in
    orig_search = svc.client.search
    def search(**params):
        if state["logged_in"]:
            svc.client.raise_search = None
        return orig_search(**params)
    svc.client.search = search
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 0
    assert state["login_calls"] == ["u"] and "needs human" not in capsys.readouterr().out


def test_expired_session_without_auto_login_is_needs_human(svc, capsys, tmp_path):
    from opatchbot.token import SessionExpired
    svc.cfg.auto_login = False
    svc.client.raise_search = AuthError("401")
    svc.browser = type("B", (), {"fetch_token": lambda self: (_ for _ in ()).throw(SessionExpired("login page")),
                                 "login": lambda self, *a, **k: pytest.fail("login must not run when auto_login is off")})()
    svc.token_store = type("T", (), {"write": lambda self, t: None, "expires_at": lambda self: None})()
    assert run_cycle("2026-10-CPU", svc, date(2026, 10, 21)) == 2
    assert "opatchbot login" in capsys.readouterr().out
