import base64
import json
import time
import types
from datetime import datetime, timedelta, timezone

from opatchbot.token import TokenStore, decode_exp, extract_jwt, make_token_provider


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(exp: float) -> str:
    header = b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64u(json.dumps({"exp": int(exp), "sub": "x" * 40, "scope": "patch_download"}).encode())
    sig = b64u(b"s" * 64)
    return f"{header}.{payload}.{sig}"


def test_decode_exp_and_needs_refresh(tmp_path):
    tok = make_jwt(exp=time.time() + 300)
    s = TokenStore(tmp_path / "t")
    s.write(tok)
    assert (tmp_path / "t").stat().st_mode & 0o777 == 0o600
    assert s.read() == tok
    assert abs((s.expires_at() - datetime.now(timezone.utc)).total_seconds() - 300) < 5
    assert s.needs_refresh() is True
    assert s.needs_refresh(timedelta(minutes=1)) is False


def test_missing_token_needs_refresh(tmp_path):
    assert TokenStore(tmp_path / "none").needs_refresh() is True


def test_extract_jwt_from_dom():
    tok = make_jwt(exp=1)
    assert extract_jwt(f"<html><body><pre>{tok}</pre></body></html>") == tok
    assert extract_jwt("<html>Sign in to Oracle</html>") is None


def test_provider_refreshes(tmp_path):
    s = TokenStore(tmp_path / "t")
    s.write(make_jwt(exp=time.time() + 60))
    fake = types.SimpleNamespace(fetch_token=lambda: make_jwt(exp=time.time() + 3600))
    provider = make_token_provider(s, fake)
    assert decode_exp(provider()) > datetime.now(timezone.utc) + timedelta(minutes=50)
    assert s.read() == provider()  # refreshed token persisted and reused


def test_extract_jwt_ignores_non_download_tokens():
    header = b64u(json.dumps({"alg": "RS256"}).encode())
    decoy = f"{header}.{b64u(json.dumps({'exp': int(time.time()) + 3600, 'scope': 'urn:opc:idm:t.user.signin', 'sub': 'x' * 40}).encode())}.{b64u(b's' * 64)}"
    real = make_jwt(exp=time.time() + 3600)
    assert extract_jwt(f"<script>var t='{decoy}'</script><pre>{real}</pre>") == real
    assert extract_jwt(f"<script>var t='{decoy}'</script>") is None


def test_provider_does_not_relaunch_browser_repeatedly(tmp_path):
    s = TokenStore(tmp_path / "t")
    calls = []
    fake = types.SimpleNamespace(fetch_token=lambda: calls.append(1) or make_jwt(exp=time.time() + 120))  # still "expiring"
    provider = make_token_provider(s, fake)
    provider(); provider(); provider()
    assert calls == [1]  # one browser launch per throttle window even though needs_refresh stays true


def test_session_expired_message_strips_query():
    from opatchbot.token import safe_url
    assert safe_url("https://login.oracle.com/x?code=SECRET&state=1") == "https://login.oracle.com/x"


def test_ensure_usable_tmpdir_replaces_unexpanded_values(tmp_path):
    from opatchbot.token import ensure_usable_tmpdir
    env = {"TMPDIR": "$TMP", "TMP": str(tmp_path), "TEMP": str(tmp_path / "missing")}
    assert ensure_usable_tmpdir(env) == ["TMPDIR", "TEMP"]
    assert env["TMPDIR"] == "/tmp" and env["TEMP"] == "/tmp" and env["TMP"] == str(tmp_path)
    assert ensure_usable_tmpdir({"PATH": "/usr/bin"}) == []
