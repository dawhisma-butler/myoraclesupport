"""Orchestration: run one cycle (resolve → fetch → state → notify) and the daily monitor pass."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .aru import AruClient, AuthError
from .config import Config
from .fetch import FetchResult, fetch_manifest, write_manifest
from .manifest import Manifest
from .monitor import (aru_fallback_released, detect, fetch_alerts_page, late_cycles, parse_alerts_page,
                      third_tuesday)
from .notify import (Notifier, summary, msg_changed, msg_first_pass, msg_heartbeat, msg_late, msg_needs_human,
                     msg_window_closed)
from .resolve import resolve_cycle
from .state import LEGAL, CycleRecord, StateStore
from .token import SessionExpired

log = logging.getLogger(__name__)
LOGIN_HINT = "opatchbot login"
EXIT = {"fetched": 0, "partial": 1, "needs_human": 2, "failed": 3, "error": 4, "email_failed": 6}


@dataclass
class Services:
    cfg: Config
    store: StateStore
    client: AruClient
    notifier: Notifier
    page_fetch: Callable[[], str] = fetch_alerts_page
    browser: Any = None
    token_store: Any = None
    secrets_path: Any = None
    config_path: Any = None


def peek_record(svc: Services, cycle_id: str) -> CycleRecord:
    """Existing record, or an in-memory one (NOT saved) for ad-hoc commands such as `resolve`."""
    rec = svc.store.load().get(cycle_id)
    if rec:
        return rec
    year, month, kind = cycle_id.split("-")
    return CycleRecord(cycle_id, kind, third_tuesday(int(year), int(month)), "released", None, None, None, None, [])


def _ensure_record(svc: Services, cycle_id: str) -> CycleRecord:
    rec = peek_record(svc, cycle_id)
    if cycle_id not in svc.store.load():
        svc.store.upsert(rec)
    return rec


def carry_verified(new: Manifest, prior_path: Path) -> tuple[set[str], dict[str, str | None]]:
    """Copy verified file names from the previous manifest of this cycle (same patch only).
    Returns (verified set as 'item/file', previous picks as {item: patch})."""
    before: set[str] = set()
    picks: dict[str, str | None] = {}
    if not prior_path.exists():
        return before, picks
    try:
        prior = Manifest.from_json(prior_path)
    except Exception:
        return before, picks
    by_item = {e.item: e for e in prior.entries}
    for e in new.entries:
        p = by_item.get(e.item)
        if p is None:
            continue
        picks[e.item] = p.patch
        if p.patch == e.patch:
            e.verified = [n for n in p.verified if any(f.name == n for f in e.files)]
            before.update(f"{e.item}/{n}" for n in e.verified)
    return before, picks


def _refresh_token_or_raise(svc: Services) -> None:
    """After a 401: force a new token. If the saved session has expired and auto_login is on, the token
    provider signs in again unattended (password + TOTP seed) once and retries; otherwise SessionExpired
    propagates and the operator gets a needs-human email."""
    provider = getattr(svc.client, "_token", None)
    if callable(getattr(provider, "force_refresh", None)):
        provider.force_refresh()  # type: ignore[union-attr]
        return
    if svc.browser is None or svc.token_store is None:
        raise SessionExpired("no browser session configured")
    from .token import fetch_token_with_relogin, make_relogin
    relogin = make_relogin(svc.browser, svc.secrets_path, getattr(svc.cfg, "auto_login", True))
    svc.token_store.write(fetch_token_with_relogin(svc.browser, relogin))


def _resolve_and_fetch(svc: Services, cycle_id: str, scheduled: date, stage: Path, areas: set[str] | None = None,
                       items: set[str] | None = None) -> tuple[Manifest, FetchResult, set[str], dict]:
    manifest = resolve_cycle(cycle_id, scheduled, svc.cfg, svc.client, areas, items)
    before, prev_picks = carry_verified(manifest, stage / "manifest.json")
    result = fetch_manifest(manifest, svc.cfg, svc.client, write=areas is None and items is None)
    if result.auth_failed:
        raise AuthError("token rejected during download")
    return manifest, result, before, prev_picks


def run_filtered(cycle_id: str, svc: Services, areas: set[str] | None, items: set[str] | None = None) -> int:
    """Manual `run --type ... / --item ...`: resolve and fetch only the selected items, merge them into the cycle's
    manifest.json, print a summary. Cycle status, the monitor's once-per-day marker and email are left alone,
    so a partial run never looks like a completed cycle. 0 all selected items verified, 1 not all, 2 needs
    human, 4 error."""
    rec = peek_record(svc, cycle_id)
    stage = svc.cfg.stage_root / cycle_id
    try:
        try:
            manifest, result, _, _ = _resolve_and_fetch(svc, cycle_id, rec.scheduled, stage, areas, items)
        except AuthError as e:
            log.warning("%s: %s; refreshing token and retrying once", cycle_id, e)
            _refresh_token_or_raise(svc)
            manifest, result, _, _ = _resolve_and_fetch(svc, cycle_id, rec.scheduled, stage, areas, items)
    except (AuthError, SessionExpired) as e:
        log.error("%s: %s", cycle_id, e)
        print(f"needs human: {e}\nRun: {LOGIN_HINT}")
        return EXIT["needs_human"]
    except Exception as e:
        log.exception("%s: filtered run failed: %s", cycle_id, e)
        print(f"error: {type(e).__name__}: {e}")
        return EXIT["error"]
    prior = None
    if (stage / "manifest.json").exists():
        try:
            prior = Manifest.from_json(stage / "manifest.json")
        except Exception:
            prior = None
    write_manifest(svc.cfg, manifest.merged_into(prior))
    label = ", ".join(sorted(areas or set()) + sorted(items or set()))
    print(f"Filtered run ({label}); cycle status and email unchanged.\n")
    print(summary(manifest, result))
    complete = manifest.all_verified() and not any(e.status == "missing" for e in manifest.entries)
    return 0 if complete else 1


def run_cycle(cycle_id: str, svc: Services, today: date, areas: set[str] | None = None,
              items: set[str] | None = None) -> int:
    """0 fetched, 1 partial, 2 needs human, 3 failed (window closed with gaps), 4 error, 6 email failed.
    With `areas` (--type) or `items` (--item) this is a manual partial run: see run_filtered."""
    if areas or items:
        return run_filtered(cycle_id, svc, areas, items)
    rec = _ensure_record(svc, cycle_id)
    stage = svc.cfg.stage_root / cycle_id
    try:
        try:
            manifest, result, before, prev_picks = _resolve_and_fetch(svc, cycle_id, rec.scheduled, stage)
        except AuthError as e:
            log.warning("%s: %s; refreshing token and retrying once", cycle_id, e)
            _refresh_token_or_raise(svc)
            manifest, result, before, prev_picks = _resolve_and_fetch(svc, cycle_id, rec.scheduled, stage)
    except (AuthError, SessionExpired) as e:
        log.error("%s: %s", cycle_id, e)
        records = svc.store.load(); rec = records[cycle_id]
        rec.last_run, rec.last_result = datetime.now(timezone.utc), f"needs human: {e}"
        svc.store.save(records)
        svc.notifier.send(*msg_needs_human(cycle_id, LOGIN_HINT))
        return EXIT["needs_human"]
    except Exception as e:  # transient network/ARU/browser failure: record, report, retry tomorrow
        log.exception("%s: run failed: %s", cycle_id, e)
        records = svc.store.load(); rec = records[cycle_id]
        rec.last_run, rec.last_result = datetime.now(timezone.utc), f"error: {type(e).__name__}: {str(e)[:200]}"
        svc.store.save(records)
        return EXIT["error"]

    after = {f"{e.item}/{n}" for e in manifest.entries for n in e.verified}
    picks = {e.item: e.patch for e in manifest.entries}
    picks_changed = any(prev_picks.get(k) is not None and prev_picks.get(k) != v for k, v in picks.items())
    complete = manifest.all_verified() and not any(e.status == "missing" for e in manifest.entries)
    window_end = rec.scheduled + timedelta(days=svc.cfg.window_after_days)
    closed = today > window_end

    records = svc.store.load(); rec = records[cycle_id]
    new_status = "fetched" if complete else ("failed" if closed else "partial")
    if rec.status != new_status and (rec.status, new_status) in LEGAL:
        rec.status = new_status
    rec.last_run = datetime.now(timezone.utc)
    rec.last_result = f"{result.verified} verified, {result.skipped} present, {len(result.failed)} failed"

    sent_ok = True
    if closed and "window_closed" not in rec.notified:
        sent_ok = svc.notifier.send(*msg_window_closed(manifest, result))
        if sent_ok:
            rec.notified.append("window_closed")
    elif "first_pass" not in rec.notified:
        sent_ok = svc.notifier.send(*msg_first_pass(manifest, result))
        if sent_ok:
            rec.notified.append("first_pass")
    elif (after - before) or picks_changed:
        changed = sorted(after - before) or [f"{k}: {prev_picks.get(k)} -> {v}" for k, v in picks.items() if prev_picks.get(k) != v]
        sent_ok = svc.notifier.send(*msg_changed(manifest, result, changed))
    rc = EXIT[new_status]
    if not sent_ok:
        rec.last_result += "; email failed"
        rc = rc or EXIT["email_failed"]
    svc.store.save(records)
    return rc


def monitor_once(svc: Services, today: date) -> int:
    try:
        cycles = parse_alerts_page(svc.page_fetch(), today)
        if not cycles:
            raise ValueError("no cycles parsed from security-alerts page")
        newly = detect(cycles, svc.store, today)
    except Exception as e:  # page unreachable or format changed: ARU fallback for due cycles
        log.warning("security-alerts page unusable (%s); using ARU fallback", e)
        newly = []
        records = svc.store.load()
        for rec in records.values():
            if rec.status == "scheduled" and rec.scheduled <= today:
                try:
                    if aru_fallback_released(svc.client, rec.scheduled, svc.cfg.inventory):
                        rec.status, rec.released_seen = "released", today
                        newly.append(rec.cycle_id)
                except Exception as e2:
                    log.warning("ARU fallback failed for %s: %s", rec.cycle_id, e2)
        svc.store.save(records)

    worst = 0
    records = svc.store.load()
    for rec in sorted(records.values(), key=lambda r: r.scheduled):
        if rec.status not in ("released", "partial", "failed", "fetched"):
            continue
        window_end = rec.scheduled + timedelta(days=svc.cfg.window_after_days)
        if rec.cycle_id in newly and today > window_end:
            rec.status, rec.last_result = "failed", "missed: first seen after window closed"
            svc.store.upsert(rec)
            continue
        if today > window_end and "window_closed" in rec.notified:
            continue  # window closed and final email sent; nothing more to do
        if today > window_end + timedelta(days=3):
            continue  # long closed (e.g. historical); never ran, nothing to report
        if rec.last_run and rec.last_run.date() == today:
            continue  # at most one run per cycle per day
        try:
            worst = max(worst, run_cycle(rec.cycle_id, svc, today))
        except Exception as e:  # belt and braces: one cycle must not stop the others
            log.exception("%s: unexpected failure: %s", rec.cycle_id, e)
            worst = max(worst, EXIT["error"])

    records = svc.store.load()
    for rec in late_cycles(svc.store, today):
        if "late" not in records[rec.cycle_id].notified and svc.notifier.send(*msg_late(rec)):
            records[rec.cycle_id].notified.append("late")
    svc.store.save(records)
    return worst


def heartbeat(svc: Services, today: date) -> int:
    upcoming = sorted((r for r in svc.store.load().values() if r.scheduled >= today), key=lambda r: r.scheduled)
    return 0 if svc.notifier.send(*msg_heartbeat(upcoming[0] if upcoming else None)) else 1


def prune(svc: Services, keep: int, apply: bool) -> list[str]:
    import re
    records = svc.store.load()
    dirs = sorted((d for d in svc.cfg.stage_root.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-(CPU|CSPU)", d.name)),
                  key=lambda d: d.name) if svc.cfg.stage_root.exists() else []
    removable = [d for d in dirs if records.get(d.name) is None or records[d.name].status in ("fetched", "failed")]
    victims = removable[:-keep] if keep else removable
    victims = [d for d in victims if d.name != dirs[-1].name] if dirs else []
    if apply:
        import shutil
        for d in victims:
            shutil.rmtree(d)
    return [d.name for d in victims]
