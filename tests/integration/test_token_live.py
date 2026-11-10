"""Live check: a saved session mints a token. Run with OPATCHBOT_LIVE=1 OPATCHBOT_SESSION_FILE=~/.mos/session.json."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from opatchbot.token import BrowserSession, decode_exp

pytestmark = pytest.mark.skipif(os.environ.get("OPATCHBOT_LIVE") != "1", reason="live test; set OPATCHBOT_LIVE=1")


def test_saved_session_mints_token():
    sess = Path(os.path.expanduser(os.environ["OPATCHBOT_SESSION_FILE"]))
    tok = BrowserSession(sess).fetch_token()
    remaining = decode_exp(tok) - datetime.now(timezone.utc)
    # Oracle hands back the session's current token (same JWT) until it expires, then a new one.
    assert timedelta(0) < remaining <= timedelta(minutes=61)
