"""Unattended re-login through the real AruClient + token provider (no fake client that bypasses tokens)."""
import base64
import json
import time
from datetime import date

import pytest
import responses

from opatchbot.aru import SERVICES, AruClient
from opatchbot.config import Config, InventoryItem, NotifyConfig
from opatchbot.notify import Notifier
from opatchbot.run import Services, run_cycle
from opatchbot.state import CycleRecord, StateStore
from opatchbot.token import SessionExpired, TokenStore, make_relogin, make_token_provider

PT = InventoryItem("pt-8.61", "peopletools", "pt_product_dpk", "monthly", 233, 21917, 600000000204773,
                   {"file_prefix": "PEOPLETOOLS-WIN-8.61"})


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt() -> str:
    p = {"exp": int(time.time()) + 3600, "sub": "x" * 40, "scope": "patch_download"}
    return f"{b64u(b'{}')}.{b64u(json.dumps(p).encode())}.{b64u(b's' * 64)}"


class Browser:
    """Saved session has expired: fetch_token fails until login() runs."""
    def __init__(self, login_ok=True):
        self.logged_in, self.login_ok, self.calls = False, login_ok, []

    def fetch_token(self):
        self.calls.append("fetch_token")
        if not self.logged_in:
            raise SessionExpired("redirected to Oracle sign-in")
        return jwt()

    def login(self, secrets, **kw):
        self.calls.append("login")
        if not self.login_ok:
            raise RuntimeError("wrong password")
        self.logged_in = True


def make_svc(tmp_path, browser, auto_login=True, seed=True):
    sec = tmp_path / "secrets.env"
    sec.write_text("MOS_USER=u\nMOS_PASS=p\n" + ("MOS_TOTP_SEED=JBSWY3DPEHPK3PXP\n" if seed else ""))
    sec.chmod(0o600)
    cfg = Config(tmp_path / "stage", tmp_path / "state", 3, 7, 14, NotifyConfig("h", 25, "a@b", ["c@d"], "stdout"), [PT],
                 auto_login=auto_login)
    store = StateStore(cfg.state_dir)
    store.upsert(CycleRecord("2026-10-CPU", "CPU", date(2026, 10, 20), "released", None, None, None, None, []))
    ts = TokenStore(cfg.state_dir / "token.txt")  # no token on disk: the normal state at the start of a daily run
    client = AruClient(make_token_provider(ts, browser, relogin=make_relogin(browser, sec, auto_login)))
    return Services(cfg=cfg, store=store, client=client, notifier=Notifier(cfg.notify), browser=browser,
                    token_store=ts, secrets_path=sec)


@responses.activate
def test_expired_token_and_session_signs_in_unattended(tmp_path, fixtures_dir, capsys):
    responses.add(responses.GET, f"{SERVICES}/search", body=(fixtures_dir / "search_pt861_233.xml").read_text(encoding="utf-8"))
    b = Browser()
    svc = make_svc(tmp_path, b)
    # downloads would hit the network; stop after resolve by making download a no-op failure
    svc.client.download = lambda f, d, resume=True: (_ for _ in ()).throw(OSError("offline test"))
    rc = run_cycle("2026-10-CPU", svc, date(2026, 10, 21))
    assert b.calls[:3] == ["fetch_token", "login", "fetch_token"]
    assert rc != 2 and "needs human" not in capsys.readouterr().out


@pytest.mark.parametrize("auto_login,seed", [(False, True), (True, False)])
def test_no_unattended_login_without_auto_login_or_seed(tmp_path, capsys, auto_login, seed):
    b = Browser()
    rc = run_cycle("2026-10-CPU", make_svc(tmp_path, b, auto_login=auto_login, seed=seed), date(2026, 10, 21))
    assert rc == 2 and "login" not in b.calls and "needs human" in capsys.readouterr().out


def test_failed_login_is_attempted_once_per_process(tmp_path, capsys):
    b = Browser(login_ok=False)
    svc = make_svc(tmp_path, b)
    for _ in range(3):
        with pytest.raises(Exception):
            svc.client._token()
    assert b.calls.count("login") == 1
