"""Release monitor: read Oracle's security-alerts page, keep the cycle state machine current."""
from __future__ import annotations

import calendar
import html as htmllib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from .aru import AruClient
from .state import CycleRecord, StateStore

log = logging.getLogger(__name__)
ALERTS_URL = "https://www.oracle.com/security-alerts/"
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
CPU_MONTHS = (1, 4, 7, 10)


@dataclass(frozen=True)
class ScheduledCycle:
    cycle_id: str
    kind: str
    scheduled: date
    advisory_url: str | None


def third_tuesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (calendar.TUESDAY - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def cycle_id_for(year: int, month: int) -> str:
    return f"{year}-{month:02d}-{'CPU' if month in CPU_MONTHS else 'CSPU'}"


def fetch_alerts_page(url: str = ALERTS_URL) -> str:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 opatchbot/0.1"}, timeout=60)
    r.raise_for_status()
    return r.text


_DATE = re.compile(r"(\d{1,2}) (January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})")
_ROW = re.compile(r'href="([^"]*/(cpu|cspu)([a-z]{3})(\d{4})\.html)"', re.I)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html)))


def parse_alerts_page(html: str, today: date) -> list[ScheduledCycle]:
    found: dict[str, ScheduledCycle] = {}
    text = _text(html)
    # 1. upcoming dates: "<program> ... The next four dates are: d Month YYYY ..." (two occurrences)
    for m in re.finditer(r"(Critical Security Patch Updates|Critical Patch Updates)[^.]{0,160}?released on the third Tuesday[^.]*\. The next \w+ dates are: ((?:\d{1,2} \w+ \d{4} ?){1,6})", text):
        for d, mon, y in _DATE.findall(m.group(2)):
            dt = date(int(y), MONTHS[mon.lower()], int(d))
            cid = cycle_id_for(dt.year, dt.month)
            found.setdefault(cid, ScheduledCycle(cid, cid.rsplit("-", 1)[1], dt, None))
    # 2. released cycles: advisory links in the tables
    for href, prog, mon3, y in _ROW.findall(html):
        month = next((i for n, i in MONTHS.items() if n.startswith(mon3.lower())), None)
        if not month:
            continue
        cid = cycle_id_for(int(y), month)
        url = href if href.startswith("http") else "https://www.oracle.com" + href
        sched = found[cid].scheduled if cid in found else third_tuesday(int(y), month)
        found[cid] = ScheduledCycle(cid, cid.rsplit("-", 1)[1], sched, url)
    # never claim a future cycle is released
    return [c if c.scheduled <= today else ScheduledCycle(c.cycle_id, c.kind, c.scheduled, None) for c in found.values()]


def detect(cycles: list[ScheduledCycle], store: StateStore, today: date) -> list[str]:
    records = store.load()
    newly: list[str] = []
    for c in cycles:
        rec = records.get(c.cycle_id)
        if rec is None:
            rec = CycleRecord(c.cycle_id, c.kind, c.scheduled, "scheduled", c.advisory_url, None, None, None, [])
            records[c.cycle_id] = rec
        if c.advisory_url:
            rec.advisory_url = c.advisory_url
        if rec.status == "scheduled" and c.advisory_url and c.scheduled <= today:
            rec.status = "released"
            rec.released_seen = today
            newly.append(c.cycle_id)
    store.save(records)
    return newly


def late_cycles(store: StateStore, today: date, grace_days: int = 3) -> list[CycleRecord]:
    return [r for r in store.load().values() if r.status == "scheduled" and today > r.scheduled + timedelta(days=grace_days)]


def aru_fallback_released(client: AruClient, scheduled: date, inventory: list | None = None) -> bool:
    """Fallback detector, used only when the security-alerts page is unusable: a PeopleTools product DPK
    released on/after the scheduled date. Uses the first PeopleTools item in the inventory (so removing a
    PeopleTools version moves the check to the next one); False when the inventory has none."""
    item = next((i for i in (inventory or []) if i.selector == "pt_product_dpk"), None)
    if item is None:
        log.warning("ARU fallback detector needs a PeopleTools item in the inventory; none configured")
        return False
    patches = client.search(product=item.product_id, release=item.release_id, platform=item.platform_id)
    prefix = item.params.get("file_prefix", "PEOPLETOOLS-")
    cutoff = datetime(scheduled.year, scheduled.month, scheduled.day, tzinfo=timezone.utc)
    return any(p.released >= cutoff and any(f.name.startswith(prefix) for f in p.files) for p in patches)
