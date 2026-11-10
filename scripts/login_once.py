"""Interactive/unattended one-time login: creates the session file and a first token.

usage: python scripts/login_once.py [--secrets ~/.mos/secrets.env] [--session ~/.mos/session.json]
                                    [--token ~/.mos/token.txt] [--debug-dir DIR]
Prints only the token expiry. Never prints secrets or the token.
"""
import argparse
import os
from pathlib import Path

from opatchbot.config import load_secrets
from opatchbot.token import BrowserSession, TokenStore, decode_exp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", default="~/.mos/secrets.env")
    ap.add_argument("--session", default="~/.mos/session.json")
    ap.add_argument("--token", default="~/.mos/token.txt")
    ap.add_argument("--debug-dir", default=None)
    a = ap.parse_args()
    secrets = load_secrets(Path(os.path.expanduser(a.secrets)))
    sess = BrowserSession(Path(os.path.expanduser(a.session)))
    sess.login(secrets, code_prompt=lambda: input("Code from authenticator app: "),
               debug_dir=Path(a.debug_dir) if a.debug_dir else None)
    tok = sess.fetch_token()
    TokenStore(Path(os.path.expanduser(a.token))).write(tok)
    print("login ok; token expires", decode_exp(tok).astimezone().strftime("%Y-%m-%d %H:%M %Z"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
