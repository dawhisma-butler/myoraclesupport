"""opatchbot command line."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .aru import AruClient
from .config import load_config, load_secrets
from .notify import Notifier
from .run import Services, heartbeat, monitor_once, prune, run_cycle
from .state import StateStore
from .token import BrowserSession, SessionExpired, TokenStore, make_token_provider

COMMANDS = ("login", "token", "status", "monitor", "resolve", "fetch", "run", "heartbeat", "prune")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="opatchbot", description="Oracle CPU/CSPU patch monitor and stager")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", default=os.environ.get("OPATCHBOT_CONFIG", "/etc/opatchbot/config.yaml"))
    ap.add_argument("--secrets", default=os.environ.get("OPATCHBOT_SECRETS", "/etc/opatchbot/secrets.env"))
    # --notify/--today are accepted before or after the subcommand. The subcommand copies use SUPPRESS so
    # they do not overwrite a value given before the subcommand (argparse subparser defaults win otherwise).
    ap.add_argument("--notify", choices=["smtp", "stdout"], default=None, help="override notify mode")
    ap.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD) for testing")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--notify", choices=["smtp", "stdout"], default=argparse.SUPPRESS)
    common.add_argument("--today", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", parents=[common], help="sign in to Oracle once (TOTP seed or typed code) and save the session")
    sub.add_parser("token", parents=[common], help="mint a download token from the saved session")
    sub.add_parser("status", parents=[common], help="show cycle states")
    sub.add_parser("monitor", parents=[common], help="daily: detect releases and run due cycles")
    for name in ("resolve", "fetch", "run"):
        p = sub.add_parser(name, parents=[common], help=f"{name} one cycle"); p.add_argument("cycle_id")
    sub.add_parser("heartbeat", parents=[common], help="send the monthly alive email")
    p = sub.add_parser("prune", parents=[common], help="remove old cycle folders beyond keep_cycles"); p.add_argument("--yes", action="store_true")
    return ap


def build_services(args) -> Services:
    cfg = load_config(Path(args.config))
    if args.notify:
        cfg.notify.mode = args.notify
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    logdir = cfg.state_dir / "log"; logdir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logdir / "opatchbot.log", maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger(); root.setLevel(logging.INFO); root.addHandler(handler)
    err = logging.StreamHandler(sys.stderr); err.setLevel(logging.WARNING); root.addHandler(err)
    store = StateStore(cfg.state_dir)
    token_store = TokenStore(cfg.state_dir / "token.txt")
    browser = BrowserSession(cfg.state_dir / "session.json")
    client = AruClient(make_token_provider(token_store, browser))
    return Services(cfg=cfg, store=store, client=client, notifier=Notifier(cfg.notify), browser=browser,
                    token_store=token_store, secrets_path=Path(args.secrets))


def main(argv: list[str] | None = None, services: Services | None = None) -> int:
    args = build_parser().parse_args(argv)
    svc = services or build_services(args)
    today = date.fromisoformat(args.today) if args.today else date.today()

    if args.cmd == "login":
        secrets = load_secrets(svc.secrets_path)
        svc.browser.login(secrets, code_prompt=lambda: input("Code from authenticator app: "))
        svc.token_store.write(svc.browser.fetch_token())
        print("login ok; token expires", svc.token_store.expires_at().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
        return 0
    if args.cmd == "token":
        try:
            svc.token_store.write(svc.browser.fetch_token())
        except SessionExpired as e:
            print(f"session expired: {e}", file=sys.stderr); return 2
        print("token expires", svc.token_store.expires_at().astimezone().strftime("%Y-%m-%d %H:%M %Z")); return 0
    if args.cmd == "status":
        records = svc.store.load()
        if svc.token_store is not None and svc.token_store.expires_at():
            print("token expires:", svc.token_store.expires_at().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
        print(f"{'cycle':<14}{'scheduled':<12}{'status':<10}{'last run':<18}last result")
        for r in sorted(records.values(), key=lambda r: r.scheduled):
            lr = r.last_run.astimezone().strftime("%Y-%m-%d %H:%M") if r.last_run else "-"
            print(f"{r.cycle_id:<14}{r.scheduled.isoformat():<12}{r.status:<10}{lr:<18}{r.last_result or ''}")
        return 0
    if args.cmd == "monitor":
        return monitor_once(svc, today)
    if args.cmd == "resolve":  # review only: no state change, never touches manifest.json
        from .resolve import resolve_cycle
        from .run import carry_verified, peek_record
        rec = peek_record(svc, args.cycle_id)
        m = resolve_cycle(args.cycle_id, rec.scheduled, svc.cfg, svc.client)
        out = svc.cfg.stage_root / args.cycle_id; out.mkdir(parents=True, exist_ok=True)
        carry_verified(m, out / "manifest.json")
        m.to_json(out / "manifest.review.json"); (out / "MANIFEST.review.md").write_text(m.to_markdown())
        print(m.to_markdown()); return 0
    if args.cmd in ("fetch", "run"):
        return run_cycle(args.cycle_id, svc, today)
    if args.cmd == "heartbeat":
        return heartbeat(svc, today)
    if args.cmd == "prune":
        victims = prune(svc, svc.cfg.keep_cycles, apply=args.yes)
        print(("removed: " if args.yes else "would remove (add --yes): ") + (", ".join(victims) or "nothing"))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
