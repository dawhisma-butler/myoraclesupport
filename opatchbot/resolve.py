"""Turn the inventory into a per-cycle manifest using ARU searches and per-item selectors."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .aru import AruClient, Patch
from .config import Config, InventoryItem
from .manifest import Manifest, ManifestEntry


@dataclass(frozen=True)
class Window:
    start: date
    end: date

    def contains(self, dt: datetime) -> bool:
        return self.start <= dt.date() <= self.end


def window_for(scheduled: date, cfg: Config) -> Window:
    return Window(scheduled - timedelta(days=cfg.window_before_days), scheduled + timedelta(days=cfg.window_after_days))


Selector = Callable[[InventoryItem, list[Patch], Window], tuple[Patch | None, str]]


def _pick(cands: list[Patch], window: Window, label: str, newest_anyway: bool) -> tuple[Patch | None, str]:
    """Newest candidate; in-window only unless newest_anyway. Reason names runner-ups."""
    if not cands:
        return None, f"no {label} found"
    cands = sorted(cands, key=lambda p: (p.released, p.updated), reverse=True)
    inwin = [p for p in cands if window.contains(p.released)]
    pool = inwin or (cands if newest_anyway else [])
    if not pool:
        return None, f"no {label} released between {window.start} and {window.end} (newest is {cands[0].number} from {cands[0].released.date()})"
    top = pool[0]
    reason = f"newest {label}, released {top.released.date()}"
    if not inwin:
        reason += " (released before window)"
    others = [p.number for p in pool[1:] if p.number != top.number]
    if others:
        reason += f"; also matched: {', '.join(others[:5])}"
    return top, reason


def select_pt_product_dpk(item: InventoryItem, patches: list[Patch], window: Window) -> tuple[Patch | None, str]:
    prefix = item.params["file_prefix"]
    cands = [p for p in patches if any(f.name.startswith(prefix) and "_1of" in f.name for f in p.files)]
    return _pick(cands, window, f"{item.name} product patch DPK ({prefix}*)", newest_anyway=True)


def _abstract_matches(item: InventoryItem, patches: list[Patch]) -> list[Patch]:
    rx = re.compile(item.params["abstract_regex"])
    out = [p for p in patches if rx.search(p.abstract)]
    suffix = item.params.get("bundle_suffix")
    if suffix:
        out = [p for p in out if p.psu_bundle.endswith(" " + suffix)]
    return out


def select_pt_mw_dpk(item: InventoryItem, patches: list[Patch], window: Window) -> tuple[Patch | None, str]:
    return _pick(_abstract_matches(item, patches), window, f"{item.name} middleware DPK", newest_anyway=True)


def select_app_release_patchset(item: InventoryItem, patches: list[Patch], window: Window) -> tuple[Patch | None, str]:
    code = item.params["app_code"]
    rx = re.compile(rf"^PEOPLESOFT RELEASE PATCHSET: {re.escape(code)} - (\d{{8}})$")
    cands = [p for p in patches if rx.match(p.abstract)]
    return _pick(cands, window, f"PEOPLESOFT RELEASE PATCHSET: {code}", newest_anyway=False)


def select_app_update_image(item: InventoryItem, patches: list[Patch], window: Window) -> tuple[Patch | None, str]:
    cands = [p for p in _abstract_matches(item, patches) if p.platform_id == item.platform_id]
    return _pick(cands, window, f"{item.name} update image", newest_anyway=True)


def select_db_abstract(item: InventoryItem, patches: list[Patch], window: Window) -> tuple[Patch | None, str]:
    cands = [p for p in _abstract_matches(item, patches) if p.platform_id == item.platform_id]
    return _pick(cands, window, f"{item.name} ({item.params['abstract_regex']})", newest_anyway=True)


def select_opatch(item: InventoryItem, patches: list[Patch], window: Window) -> tuple[Patch | None, str]:
    prefix = item.params.get("release_prefix", "")
    cands = [p for p in patches if p.platform_id == item.platform_id and p.release_name.startswith(prefix)]
    if not cands:
        return None, f"no OPatch entry for release {prefix}* on platform {item.platform_id}"
    top = sorted(cands, key=lambda p: (p.released, p.updated), reverse=True)[0]
    return top, f"current OPatch for {top.release_name} ({top.abstract}), released {top.released.date()}"


SELECTORS: dict[str, Selector] = {
    "pt_product_dpk": select_pt_product_dpk,
    "pt_mw_dpk": select_pt_mw_dpk,
    "app_release_patchset": select_app_release_patchset,
    "app_update_image": select_app_update_image,
    "db_release_update": select_db_abstract,
    "db_ojvm": select_db_abstract,
    "db_jdk": select_db_abstract,
    "opatch": select_opatch,
}


def _entry(item: InventoryItem, status: str, reason: str, patch: Patch | None = None) -> ManifestEntry:
    return ManifestEntry(item.name, item.area, status, patch.number if patch else None, patch.abstract if patch else None,
                         item.platform_id, patch.released if patch else None, reason, list(patch.files) if patch else [], [])


def resolve_item(item: InventoryItem, client: AruClient | None, window: Window, kind: str) -> ManifestEntry:
    if item.cadence == "quarterly" and kind == "CSPU":
        return _entry(item, "not_expected", "quarterly item; CSPU cycle")
    assert client is not None
    if item.selector == "opatch":
        patches = client.search(bug=item.params["bug"])
    else:
        params: dict = {"product": item.product_id, "release": item.release_id, "platform": item.platform_id}
        params.update(item.params.get("search", {}))
        patches = client.search(**{k: v for k, v in params.items() if v is not None})
    patch, reason = SELECTORS[item.selector](item, patches, window)
    return _entry(item, "resolved" if patch else "missing", reason, patch)


def resolve_cycle(cycle_id: str, scheduled: date, cfg: Config, client: AruClient) -> Manifest:
    kind = cycle_id.rsplit("-", 1)[1]
    window = window_for(scheduled, cfg)
    entries = [resolve_item(item, client, window, kind) for item in cfg.inventory]
    return Manifest(cycle_id, kind, scheduled, datetime.now(timezone.utc), entries)
