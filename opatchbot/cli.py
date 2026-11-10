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
from .aru import AruClient, AruError, AuthError
from .config import TYPES, ConfigError, load_config, load_secrets, parse_types
from .notify import Notifier
from .run import Services, heartbeat, monitor_once, prune, run_cycle
from .state import StateStore
from .token import BrowserSession, SessionExpired, TokenStore, fetch_token_with_relogin, make_relogin, make_token_provider

DEFAULT_CONFIG = "/opt/app/patch/config.yaml"
DEFAULT_SECRETS = "/home/oracle/.mos/secrets.env"
LOCKED = ("monitor", "run", "fetch", "add", "del")  # one at a time: they download or edit shared files
EXIT_BUSY = 5

COMMANDS = ("login", "token", "status", "monitor", "resolve", "fetch", "run", "heartbeat", "prune",
            "add", "del", "list", "discover")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="opatchbot", description="Oracle CPU/CSPU patch monitor and stager")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", default=os.environ.get("OPATCHBOT_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--secrets", default=os.environ.get("OPATCHBOT_SECRETS", DEFAULT_SECRETS))
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
    type_help = ("patch types to include, comma separated: " + ", ".join(f"{k}={v}" for k, v in TYPES.items())
                 + " (default: everything). A filtered run updates only those items and sends no email.")
    for name in ("resolve", "fetch", "run"):
        p = sub.add_parser(name, parents=[common], help=f"{name} one cycle"); p.add_argument("cycle_id")
        p.add_argument("--type", dest="types", default=None, metavar="DB,MW,PT,AP", help=type_help)
        p.add_argument("--item", dest="items", default=None, metavar="NAME[,NAME]",
                       help="only these inventory items, comma separated, as `opatchbot list` names them "
                            "(e.g. pt-8.61). Same rules as --type; with both, an item must match both.")
    sub.add_parser("heartbeat", parents=[common], help="send the monthly alive email")
    p = sub.add_parser("prune", parents=[common], help="remove old cycle folders beyond keep_cycles"); p.add_argument("--yes", action="store_true")

    p = sub.add_parser("add", parents=[common], help="add a software version to config.yaml (looks up Oracle ids once)",
                       description="Examples: add DB 26ai | add PT 8.63 | add MW 8.63 | add AP HCM 9.2. Looks up the "
                                   "Oracle release and platform ids, runs each new item's search once to show what it "
                                   "would download, then writes config.yaml after confirmation (dated backup kept).")
    p.add_argument("type", metavar="TYPE", help="DB, PT, MW or AP")
    p.add_argument("args", nargs="+", metavar="VERSION", help="version (DB/PT/MW) or application and release (AP)")
    p.add_argument("--platform", help="platform name or id (default: Linux x86-64 for DB, Microsoft Windows x64 otherwise)")
    p.add_argument("--release-id", type=int, help="Oracle release id, when the version name matches several releases")
    p.add_argument("--components", help="DB only: subset of ru,ojvm,jdk,opatch (default all)")
    p.add_argument("--product-id", type=int, help="override the Oracle product family id")
    p.add_argument("--file-prefix", help="PT only: DPK file name prefix (default PEOPLETOOLS-WIN-<version>)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--force", action="store_true", help="with --yes: write even if an item's search finds nothing now")
    p = sub.add_parser("del", parents=[common], help="remove a software version (or one item) from config.yaml",
                       description="Examples: del DB 19 | del PT 8.61 | del AP CS 9.2 | del db19-ojvm")
    p.add_argument("targets", nargs="+", metavar="TARGET", help="TYPE VERSION..., or an item name from `opatchbot list`")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sub.add_parser("list", parents=[common], help="show the configured inventory")
    p = sub.add_parser("discover", parents=[common], help="search Oracle's catalogue for release/product/platform ids")
    p.add_argument("text", nargs="+", help="words that must all appear in the name, e.g. Database 23")
    p.add_argument("--table", choices=["releases", "products", "platforms", "all"], default=None,
                   help="what to search (default: releases and products)")
    return ap


def _confirm(args, question: str) -> bool:
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("not running in a terminal; re-run with --yes to apply", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _apply_plan(svc, args, plan) -> int:
    from . import inventory
    backup = inventory.apply(Path(svc.config_path), plan)
    try:
        load_config(Path(svc.config_path))
    except Exception as e:  # never leave a config the daily run cannot read
        import shutil
        shutil.copy2(backup, svc.config_path)
        print(f"the edited config.yaml did not load ({e}); restored it from {backup}", file=sys.stderr)
        return 1
    print(f"config.yaml updated (previous version saved as {backup.name})")
    return 0


def inventory_command(svc, args, today) -> int:
    from . import inventory
    try:
        if args.cmd == "list":
            print(inventory.list_rows(svc.cfg.inventory)); return 0
        if args.cmd == "discover":
            tables = ("releases", "products", "platforms") if args.table == "all" else \
                     ((args.table,) if args.table else ("releases", "products"))
            rows = inventory.discover(svc.client, " ".join(args.text), tables)
            for kind, i, name in rows:
                print(f"{kind:<9}{i:>17}  {name}")
            print(f"{len(rows)} match(es)"); return 0
        if args.cmd == "del":
            plan = inventory.plan_del(Path(svc.config_path), args.targets)
        else:
            comps = [c.strip().lower() for c in args.components.split(",")] if args.components else None
            plan = inventory.plan_add(svc.client, Path(svc.config_path), args.type, args.args, today,
                                      platform=args.platform, release_id=args.release_id, components=comps,
                                      product_id=args.product_id, file_prefix=args.file_prefix)
    except inventory.InventoryError as e:
        print(e, file=sys.stderr); return 1
    print(inventory.describe(plan))
    if plan.missing:
        print(f"\nWARNING: no current patch found for {', '.join(plan.missing)}. The version may be new, the name may "
              "not match Oracle's, or the platform may be wrong. Check with `opatchbot discover` before relying on it.")
        if args.yes and not args.force:
            print("refusing with --yes; add --force to write anyway", file=sys.stderr); return 1
    if not _confirm(args, "Write these changes to config.yaml?"):
        print("no changes made"); return 1
    return _apply_plan(svc, args, plan)


def build_services(args) -> Services:
    cfg = load_config(Path(args.config))
    if args.notify:
        cfg.notify.mode = args.notify
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logdir = cfg.state_dir / "log"; logdir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logdir / "opatchbot.log", maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger(); root.setLevel(logging.INFO); root.addHandler(handler)
    err = logging.StreamHandler(sys.stderr); err.setLevel(logging.WARNING); root.addHandler(err)
    store = StateStore(cfg.state_dir)
    token_store = TokenStore(cfg.secret_dir / "token.txt")
    browser = BrowserSession(cfg.secret_dir / "session.json")
    relogin = make_relogin(browser, Path(args.secrets), cfg.auto_login)
    client = AruClient(make_token_provider(token_store, browser, relogin=relogin))
    return Services(cfg=cfg, store=store, client=client, notifier=Notifier(cfg.notify), browser=browser,
                    token_store=token_store, secrets_path=Path(args.secrets), config_path=Path(args.config))


def _single_instance(state_dir: Path):
    """Exclusive lock on state_dir/opatchbot.lock, or None if another locked command is running."""
    import fcntl
    state_dir.mkdir(parents=True, exist_ok=True)
    fh = open(state_dir / "opatchbot.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh  # held until the process exits


def main(argv: list[str] | None = None, services: Services | None = None) -> int:
    args = build_parser().parse_args(argv)
    if services is None and hasattr(os, "geteuid") and os.geteuid() == 0 and not os.environ.get("OPATCHBOT_ALLOW_ROOT"):
        print("opatchbot must not run as root (files it writes must stay owned by the service user). "
              "Run it as oracle, e.g. `sudo -u oracle opatchbot ...`.", file=sys.stderr)
        return 1
    try:
        svc = services or build_services(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    if services is None and args.cmd in LOCKED:
        lock = _single_instance(svc.cfg.state_dir)
        if lock is None:
            print("another opatchbot run is in progress (monitor/run/fetch/add/del); try again later", file=sys.stderr)
            return EXIT_BUSY
    today = date.fromisoformat(args.today) if args.today else date.today()
    try:
        return _dispatch(args, svc, today)
    except (AuthError, SessionExpired) as e:
        print(f"Oracle refused the request: {e}. A fresh token was already tried once; see "
              f"{svc.cfg.state_dir / 'log' / 'opatchbot.log'} for Oracle's reply. If it persists, run `opatchbot login`.",
              file=sys.stderr)
        return 2
    except AruError as e:
        print(f"Oracle patch service error: {e}", file=sys.stderr)
        return 4


def _dispatch(args, svc, today) -> int:

    if args.cmd == "login":
        try:
            secrets = load_secrets(svc.secrets_path)
        except (OSError, ValueError) as e:  # missing file, wrong mode, MOS_USER/MOS_PASS empty
            print(f"secrets error: {e}", file=sys.stderr)
            return 1
        svc.browser.login(secrets, code_prompt=lambda: input("Code from authenticator app: "))
        svc.token_store.write(svc.browser.fetch_token())
        print("login ok; token expires", svc.token_store.expires_at().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
        return 0
    if args.cmd == "token":
        try:
            relogin = make_relogin(svc.browser, svc.secrets_path, getattr(svc.cfg, "auto_login", True))
            svc.token_store.write(fetch_token_with_relogin(svc.browser, relogin))
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
    areas = items = None
    if args.cmd in ("resolve", "fetch", "run"):
        try:
            areas = parse_types(args.types)
        except ValueError as e:
            print(e, file=sys.stderr); return 1
        if args.items:
            items = {n.strip() for n in args.items.split(",") if n.strip()}
            known = {i.name for i in svc.cfg.inventory}
            unknown = sorted(items - known)
            if unknown:
                print(f"unknown item(s): {', '.join(unknown)}; `opatchbot list` shows the names", file=sys.stderr)
                return 1
    if args.cmd == "resolve":  # review only: no state change, never touches manifest.json
        from .resolve import resolve_cycle
        from .run import carry_verified, peek_record
        rec = peek_record(svc, args.cycle_id)
        m = resolve_cycle(args.cycle_id, rec.scheduled, svc.cfg, svc.client, areas, items)
        out = svc.cfg.stage_root / args.cycle_id; out.mkdir(parents=True, exist_ok=True)
        carry_verified(m, out / "manifest.json")
        m.to_json(out / "manifest.review.json"); (out / "MANIFEST.review.md").write_text(m.to_markdown(), encoding="utf-8")
        print(m.to_markdown()); return 0
    if args.cmd in ("fetch", "run"):
        return run_cycle(args.cycle_id, svc, today, areas, items)
    if args.cmd in ("add", "del", "list", "discover"):
        return inventory_command(svc, args, today)
    if args.cmd == "heartbeat":
        return heartbeat(svc, today)
    if args.cmd == "prune":
        victims = prune(svc, svc.cfg.keep_cycles, apply=args.yes)
        print(("removed: " if args.yes else "would remove (add --yes): ") + (", ".join(victims) or "nothing"))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
