"""Download-token handling: token file store and a Playwright-driven browser session.

The token is an OAuth2 JWT valid for ~60 minutes, obtainable only from a browser session that
has signed in to login.oracle.com (MFA). The session is persisted as Playwright storage state so
that later runs can mint tokens headlessly without a human.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import Secrets

log = logging.getLogger(__name__)

TOKEN_URL = "https://updates.oracle.com/Orion/GenToken/get_token"
JWT_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{60,}\.[A-Za-z0-9_-]{20,}")
SIGNIN_MARKERS = ("login.oracle.com", "signon.oracle.com", "identity.oraclecloud.com", "Sign in to Oracle")

# Selectors for login.oracle.com / signon.oracle.com (classic Oracle SSO, 2026-11). Kept generic on
# purpose: the page is a username step, a password step, then a one-time-code step.
USERNAME_SELECTORS = ["input[name='username']", "input#userid", "input[type='email']", "input[name='ssousername']", "input[type='text']"]
PASSWORD_SELECTORS = ["input[type='password']"]
OTP_SELECTORS = ["input[name*='otp' i]", "input[id*='otp' i]", "input[name*='passcode' i]", "input[id*='passcode' i]",
                 "input[name*='code' i]", "input[autocomplete='one-time-code']", "input[inputmode='numeric']"]
SUBMIT_SELECTORS = ["button[type='submit']", "input[type='submit']", "button:has-text('Next')", "button:has-text('Sign in')",
                    "button:has-text('Sign In')", "button:has-text('Verify')", "button:has-text('Continue')"]


def safe_url(url: str) -> str:
    """URL without query/fragment (OAuth redirects carry code=/state= which must not reach logs)."""
    return url.split("?", 1)[0].split("#", 1)[0]


class SessionExpired(Exception):
    """The browser session no longer signs in silently; a human must run `opatchbot login`."""


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode_exp(jwt: str) -> datetime:
    payload = json.loads(_b64url_decode(jwt.split(".")[1]))
    return datetime.fromtimestamp(int(payload["exp"]), timezone.utc)


def is_download_token(jwt: str) -> bool:
    """True for an ARU download token: scope includes patch_download (login pages embed other JWTs)."""
    try:
        payload = json.loads(_b64url_decode(jwt.split(".")[1]))
    except Exception:
        return False
    return "patch_download" in str(payload.get("scope", ""))


def extract_jwt(text: str) -> str | None:
    best = None
    for m in JWT_RE.finditer(text):
        tok = m.group(0)
        try:
            decode_exp(tok)
        except Exception:
            continue
        if not is_download_token(tok):
            continue
        if best is None or len(tok) > len(best):
            best = tok
    return best


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> str | None:
        if not self.path.exists():
            return None
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
        return None

    def write(self, token: str) -> None:
        _write_private(self.path, token.strip() + "\n")

    def expires_at(self) -> datetime | None:
        tok = self.read()
        if not tok:
            return None
        try:
            return decode_exp(tok)
        except Exception:
            return None

    def needs_refresh(self, min_remaining: timedelta = timedelta(minutes=10)) -> bool:
        exp = self.expires_at()
        return exp is None or exp - datetime.now(timezone.utc) < min_remaining


class BrowserSession:
    def __init__(self, session_file: Path, headless: bool = True, timeout_ms: int = 45_000):
        self.session_file = Path(session_file)
        self.headless = headless
        self.timeout_ms = timeout_ms

    # -- helpers -------------------------------------------------------------------------------
    def _launch(self, p, with_state: bool):
        browser = p.chromium.launch(headless=self.headless)
        kwargs = {}
        if with_state and self.session_file.exists():
            kwargs["storage_state"] = str(self.session_file)
        ctx = browser.new_context(**kwargs)
        ctx.set_default_timeout(self.timeout_ms)
        return browser, ctx


    @staticmethod
    def _content(page, attempts: int = 12) -> str:
        """self._content(page) that tolerates in-flight redirects (Oracle SSO chains several)."""
        last = None
        for _ in range(attempts):
            try:
                page.wait_for_load_state("load")
                page.wait_for_timeout(700)
                return page.content()
            except Exception as e:  # navigating; wait and retry
                last = e
                page.wait_for_timeout(800)
        raise RuntimeError(f"page never settled: {last}")

    @staticmethod
    def _first_visible(page, selectors: list[str]):
        for sel in selectors:
            loc = page.locator(sel)
            try:
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:
                continue
        return None


    def _wait_any(self, page, selectors: list[str], timeout_ms: int | None = None):
        """Poll until any selector is visible (SPA login pages render after load)."""
        import time as _t
        deadline = _t.monotonic() + (timeout_ms or self.timeout_ms) / 1000
        while _t.monotonic() < deadline:
            el = self._first_visible(page, selectors)
            if el is not None:
                return el
            try:
                if extract_jwt(page.content()):
                    return None
            except Exception:
                pass
            page.wait_for_timeout(500)
        return None


    def _generate_and_read(self, page, snap=None) -> str | None:
        """On the GenToken page: click 'Generate token' if shown, then read the token from the page
        or from a file download the page offers."""
        tok = extract_jwt(self._content(page))
        if tok:
            return tok
        btn = self._wait_any(page, ["button:has-text('Generate token')", "a:has-text('Generate token')",
                                    "button:has-text('Generate Token')"], 15_000)
        if btn is None:
            return extract_jwt(self._content(page))
        btn.click()
        page.wait_for_timeout(2000)
        for _ in range(20):
            try:
                tok = extract_jwt(page.content())
            except Exception:
                tok = None
            if tok:
                return tok
            dl = self._first_visible(page, ["a:has-text('Download')", "button:has-text('Download')",
                                            "a[download]", "button:has-text('Save')"])
            if dl is not None:
                with page.expect_download(timeout=20_000) as info:
                    dl.click()
                path = info.value.path()
                if path:
                    tok = extract_jwt(Path(path).read_text(errors="ignore"))
                    if tok:
                        return tok
            if any(m in page.url for m in SIGNIN_MARKERS):
                return None
            page.wait_for_timeout(1000)
        if snap:
            snap(page, "04-after-generate")
        return None

    def _submit(self, page) -> None:
        btn = self._first_visible(page, SUBMIT_SELECTORS)
        if btn is not None:
            btn.click()
        else:
            page.keyboard.press("Enter")
        page.wait_for_load_state("load"); page.wait_for_timeout(1500)

    def _save_state(self, ctx) -> None:
        state = ctx.storage_state()
        _write_private(self.session_file, json.dumps(state))

    # -- public --------------------------------------------------------------------------------
    def fetch_token(self) -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser, ctx = self._launch(p, with_state=True)
            try:
                page = ctx.new_page()
                page.goto(TOKEN_URL, wait_until="load")
                tok = self._generate_and_read(page)
                if tok:
                    self._save_state(ctx)  # keep refreshed cookies
                    return tok
                content = self._content(page)
                if any(m in page.url for m in SIGNIN_MARKERS) or any(m in content for m in SIGNIN_MARKERS):
                    raise SessionExpired("redirected to Oracle sign-in; run `opatchbot login`")
                raise SessionExpired(f"no token on page {safe_url(page.url)}")
            finally:
                browser.close()

    def login(self, secrets: Secrets, code_prompt: Callable[[], str] | None = None, debug_dir: Path | None = None) -> None:
        from playwright.sync_api import sync_playwright

        def snap(page, name: str) -> None:
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / f"{name}.png"))
                except Exception:
                    pass

        with sync_playwright() as p:
            browser, ctx = self._launch(p, with_state=False)
            try:
                page = ctx.new_page()
                page.goto(TOKEN_URL, wait_until="load")
                if extract_jwt(self._content(page)):
                    self._save_state(ctx)
                    return
                user = self._wait_any(page, USERNAME_SELECTORS)
                snap(page, "01-start")
                if user is None:
                    raise RuntimeError(f"no username field found at {safe_url(page.url)}")
                user.fill(secrets.mos_user)
                pw = self._first_visible(page, PASSWORD_SELECTORS)
                if pw is None:
                    self._submit(page)
                    pw = self._wait_any(page, PASSWORD_SELECTORS, 20_000)
                if pw is None:
                    snap(page, "02-no-password")
                    raise RuntimeError(f"no password field found at {safe_url(page.url)}")
                pw.fill(secrets.mos_pass)
                self._submit(page)
                page.wait_for_timeout(2000)
                # second factor
                for _ in range(2):
                    if extract_jwt(self._content(page)):
                        break
                    otp = self._wait_any(page, OTP_SELECTORS, 20_000)
                    if otp is None:
                        break
                    snap(page, "02-otp-page")
                    if secrets.totp_seed:
                        import pyotp
                        code = pyotp.TOTP(secrets.totp_seed).now()
                    elif code_prompt:
                        code = code_prompt()
                    else:
                        raise RuntimeError("second factor required but no TOTP seed and no prompt available")
                    otp.fill(code)
                    self._submit(page)
                    page.wait_for_timeout(3000)
                # some flows need a 'remember/trust this device' or consent click; try generic continue
                if not extract_jwt(self._content(page)):
                    cont = self._first_visible(page, ["button:has-text('Continue')", "button:has-text('Yes')", "button:has-text('Allow')", "button:has-text('Trust')"])
                    if cont is not None:
                        cont.click()
                        page.wait_for_load_state("load"); page.wait_for_timeout(1500)
                if TOKEN_URL.split("//")[1] not in page.url:
                    page.goto(TOKEN_URL, wait_until="load")
                snap(page, "03-after-login")
                tok = self._generate_and_read(page, snap)
                if not tok:
                    raise RuntimeError(f"login did not yield a download token; ended at {safe_url(page.url)}")
                self._save_state(ctx)
            finally:
                browser.close()


def make_token_provider(store: TokenStore, browser, min_remaining: timedelta = timedelta(minutes=10),
                        throttle: timedelta = timedelta(minutes=2)) -> Callable[[], str]:
    """Token callable for AruClient. Launches the browser only when the stored token is missing/expiring,
    at most once per `throttle` (GenToken hands back the same token until it expires), and is thread-safe.
    Call provider.force_refresh() after a 401 to bypass the throttle."""
    import threading
    lock = threading.Lock()
    last = {"at": None}

    def refresh(force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        if not force and last["at"] and now - last["at"] < throttle:
            return
        log.info("token missing/expiring; fetching one from the browser session")
        store.write(browser.fetch_token())
        last["at"] = now
        if store.needs_refresh(min_remaining):
            log.warning("fetched token expires at %s; will refresh again after expiry", store.expires_at())

    def provider() -> str:
        with lock:
            if store.needs_refresh(min_remaining) or not store.read():
                refresh()
            tok = store.read()
            if not tok:
                raise SessionExpired("no token available")
            return tok

    def force_refresh() -> None:
        with lock:
            refresh(force=True)

    provider.force_refresh = force_refresh  # type: ignore[attr-defined]
    return provider
