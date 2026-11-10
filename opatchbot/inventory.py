"""Inventory maintenance: `opatchbot add`, `del`, `list`, `discover`.

Oracle's internal ids (product, release, platform) are looked up ONCE, when a version is added, and pinned
in config.yaml. `monitor` and `run` only read the pinned values, so every run searches for exactly the
same software. Edits take a dated backup
first, and replace the file atomically so a run starting at the same moment never reads half a file.
Only the inventory lines being added or removed change; every comment and note in the file is kept.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import TYPES, InventoryItem
from .resolve import Window, resolve_item

CODE_FOR_AREA = {v: k for k, v in TYPES.items()}

# Product family ids (from live ARU discovery 2026-11-09). Families do not change between versions; the
# release id (which does) is what `add` looks up. Override with --product-id if Oracle ever moves one.
PRODUCTS = {
    "DB": 9480,      # Oracle Database Family
    "PT": 21917,     # PeopleSoft PeopleTools Family
    "MW": 21917,     # PT-MW-DPK is listed under PeopleTools
    "HCM": 21858,    # PeopleSoft HCM Family
    "FSCM": 21714,   # FSCM release patchsets are listed under PeopleSoft Enterprise FIN General Ledger
    "CS": 21591,     # PeopleSoft CS Family (update images only; no release patchsets)
}
APPS = ("HCM", "FSCM", "CS")
DEFAULT_PLATFORM = {"DB": "Linux x86-64", "PT": "Microsoft Windows x64", "MW": "Microsoft Windows x64",
                    "AP": "Microsoft Windows x64"}
PT_FILE_OS = {233: "WIN", 226: "LNX"}  # PEOPLETOOLS-<OS>-<ver>... ; LNX not yet seen live
DB_COMPONENTS = ("ru", "ojvm", "jdk", "opatch")
# Marketing names whose release number differs: Oracle AI Database 26ai is version 23.26.
DB_ALIASES = {"26ai": ("26", "23"), "23ai": ("23",)}


class InventoryError(Exception):
    """Bad request for add/del (unknown version, ambiguous match, duplicate, ...)."""


@dataclass
class Lookup:
    release_id: int
    release_name: str
    platform_id: int
    platform_name: str


@dataclass
class Plan:
    """What add/del will do: items to write or remove, with a check result per added item."""
    add: list[InventoryItem] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    comment: str = ""
    checks: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


# -- name helpers ------------------------------------------------------------------------------------------
def _label(version: str) -> str:
    """Version token as used in item names: '19c' -> '19', '26ai' -> '26ai', '8.62' -> '8.62'."""
    v = version.strip().lower()
    return v[:-1] if re.fullmatch(r"\d+c", v) else v


def item_names(type_code: str, args: list[str]) -> list[str]:
    """Item names `add` creates (and `del` removes) for a type and its arguments."""
    t = type_code.upper()
    if t == "DB":
        lab = _label(args[0])
        return [f"db{lab}-{c}" for c in DB_COMPONENTS]
    if t == "PT":
        return [f"pt-{args[0]}"]
    if t == "MW":
        return [f"ptmw-{args[0]}"]
    if t == "AP":
        if len(args) < 2:
            raise InventoryError("AP needs an application and a release, e.g. `AP HCM 9.2`")
        return [f"{args[0].lower()}-{args[1]}"]
    raise InventoryError(f"unknown patch type {type_code!r}; use one of {', '.join(TYPES)}")


# -- Oracle catalogue lookups ------------------------------------------------------------------------------
def find_platform(client, wanted: str) -> tuple[int, str]:
    rows = client.metadata("aru_platforms")
    if wanted.isdigit():
        for pid, name in rows:
            if pid == int(wanted):
                return pid, name
        raise InventoryError(f"no platform with id {wanted}")
    w = wanted.lower()
    exact = [(i, n) for i, n in rows if n.lower() == w]
    starts = [(i, n) for i, n in rows if n.lower().startswith(w)]
    hits = exact or starts or [(i, n) for i, n in rows if w in n.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise InventoryError(f"no platform matches {wanted!r}; try `opatchbot discover {wanted!r} --table platforms`")
    raise InventoryError(f"platform {wanted!r} is ambiguous: " + "; ".join(f"{i} {n}" for i, n in hits[:10])
                         + ". Pass --platform with the id.")


def release_candidates(type_code: str, args: list[str], rows: list[tuple[int, str]]) -> list[tuple[int, str]]:
    t = type_code.upper()
    if t == "DB":
        v = _label(args[0])
        majors = DB_ALIASES.get(v, (re.match(r"\d+", v).group(0),) if re.match(r"\d+", v) else ())
        if not majors:
            raise InventoryError(f"cannot read a database version from {args[0]!r} (e.g. 19, 21, 26ai)")
        out = []
        for major in majors:
            rx = re.compile(rf"^Oracle (?:AI )?Database {major}\.0\.0\.0\.0$")
            out += [(i, n) for i, n in rows if rx.match(n.strip())]
            if out:
                break  # first alias that matches wins (26 before 23 for 26ai)
        return out
    if t in ("PT", "MW"):
        return [(i, n) for i, n in rows if n.strip() == f"PeopleSoft PeopleTools {args[0]}"]
    if t == "AP":
        return [(i, n) for i, n in rows if n.strip() == f"PeopleSoft {args[1]}"]
    raise InventoryError(f"unknown patch type {type_code!r}")


def lookup(client, type_code: str, args: list[str], platform: str | None = None,
           release_id: int | None = None) -> Lookup:
    rows = client.metadata("aru_releases")
    if release_id is not None:
        name = next((n for i, n in rows if i == release_id), None)
        if name is None:
            raise InventoryError(f"no release with id {release_id}")
        rel = (release_id, name)
    else:
        cands = release_candidates(type_code, args, rows)
        if not cands:
            hint = {"DB": "Database", "PT": "PeopleTools", "MW": "PeopleTools", "AP": "PeopleSoft"}[type_code.upper()]
            raise InventoryError(f"no Oracle release found for {type_code} {' '.join(args)}; "
                                 f"search with `opatchbot discover {hint!r}` and pass --release-id")
        if len(cands) > 1:
            raise InventoryError("several Oracle releases match: " + "; ".join(f"{i} {n}" for i, n in cands)
                                 + ". Pass --release-id with the one you run.")
        rel = cands[0]
    pid, pname = find_platform(client, platform or DEFAULT_PLATFORM[type_code.upper()])
    return Lookup(rel[0], rel[1].strip(), pid, pname)


def discover(client, text: str, tables: tuple[str, ...] = ("releases", "products")) -> list[tuple[str, int, str]]:
    words = text.lower().split()
    out = []
    for t in tables:
        for i, n in client.metadata(f"aru_{t}"):
            if all(w in n.lower() for w in words):
                out.append((t[:-1], i, n.strip()))
    return sorted(out, key=lambda r: (r[0], r[2]))


# -- building items ----------------------------------------------------------------------------------------
def build_items(type_code: str, args: list[str], lk: Lookup, components: list[str] | None = None,
                product_id: int | None = None, file_prefix: str | None = None) -> list[InventoryItem]:
    t = type_code.upper()
    if t == "DB":
        major = re.search(r"Database (\d+)\.", lk.release_name).group(1)
        lab = _label(args[0])
        comps = components or list(DB_COMPONENTS)
        bad = [c for c in comps if c not in DB_COMPONENTS]
        if bad:
            raise InventoryError(f"unknown components {bad}; choose from {', '.join(DB_COMPONENTS)}")
        pid = product_id or PRODUCTS["DB"]
        spec = {
            "ru": ("db_release_update", {"abstract_regex": rf"^DATABASE RELEASE UPDATE {major}\.", "bundle_suffix": "DBRU",
                                         "search": {"patch_type": "all"}}),
            "ojvm": ("db_ojvm", {"abstract_regex": rf"^OJVM RELEASE UPDATE {major}\.", "bundle_suffix": "OJVMRU",
                                 "search": {"patch_type": "all"}}),
            "jdk": ("db_jdk", {"abstract_regex": rf"^JDK BUNDLE PATCH {major}\.", "search": {"patch_type": "all"}}),
            "opatch": ("opatch", {"bug": 6880880, "release_prefix": f"{major}."}),
        }
        return [InventoryItem(f"db{lab}-{c}", "database", spec[c][0], "quarterly", lk.platform_id, pid, lk.release_id,
                              spec[c][1]) for c in DB_COMPONENTS if c in comps]
    if t == "PT":
        os_tag = PT_FILE_OS.get(lk.platform_id)
        prefix = file_prefix or (f"PEOPLETOOLS-{os_tag}-{args[0]}" if os_tag else None)
        if not prefix:
            raise InventoryError(f"no known PeopleTools file prefix for platform {lk.platform_name}; pass --file-prefix")
        return [InventoryItem(f"pt-{args[0]}", "peopletools", "pt_product_dpk", "monthly", lk.platform_id,
                              product_id or PRODUCTS["PT"], lk.release_id, {"file_prefix": prefix})]
    if t == "MW":
        return [InventoryItem(f"ptmw-{args[0]}", "middleware", "pt_mw_dpk", "monthly", lk.platform_id,
                              product_id or PRODUCTS["MW"], lk.release_id, {"abstract_regex": "^PT-MW-DPK"})]
    if t == "AP":
        app = args[0].upper()
        if app not in APPS:
            raise InventoryError(f"unknown application {args[0]!r}; choose from {', '.join(APPS)}")
        name = f"{app.lower()}-{args[1]}"
        pid = product_id or PRODUCTS[app]
        if app == "CS":  # no release patchsets for CS: the NATIVE OS update image (~22 GB)
            return [InventoryItem(name, "apps", "app_update_image", "monthly", lk.platform_id, pid, lk.release_id,
                                  {"abstract_regex": "^PEOPLESOFT CS UPDATE IMAGE .* - NATIVE OS$"})]
        return [InventoryItem(name, "apps", "app_release_patchset", "monthly", lk.platform_id, pid, lk.release_id,
                              {"app_code": app})]
    raise InventoryError(f"unknown patch type {type_code!r}")


def check_items(client, items: list[InventoryItem], today: date, days: int = 120) -> tuple[dict[str, str], list[str]]:
    """Run each new item's real search now and report what it would pick, so a wrong match shows up at
    `add` time instead of on release day. Window: the last `days` days (release patchsets need one)."""
    window = Window(today - timedelta(days=days), today + timedelta(days=1))
    checks, missing = {}, []
    for item in items:
        e = resolve_item(item, client, window, "CPU")
        if e.status == "resolved":
            when = e.released.date().isoformat() if e.released else "?"
            checks[item.name] = f"patch {e.patch}: {e.abstract} (released {when}, {len(e.files)} file(s))"
        else:
            checks[item.name] = f"NOTHING FOUND: {e.reason}"
            missing.append(item.name)
    return checks, missing


# -- config.yaml editing -----------------------------------------------------------------------------------
# config.yaml keeps one inventory item per line (`  - {name: ..., ...}`). Edits are line based: removed items'
# lines are dropped and new items are inserted after the last item of the same area, so every other line
# (comments, alignment, notes) is left exactly as written. The result is validated with the normal loader
# before it replaces the file.
ITEM_LINE = re.compile(r"^(\s*)-\s*\{\s*name:\s*([^,}\s]+)")
ADDED_COMMENT = re.compile(r"^\s*# added .* by `opatchbot add ")
PLAIN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_scalar(x)}" for k, x in v.items()) + "}"
    s = str(v)
    if PLAIN.match(s) and not re.fullmatch(r"[0-9.]+|true|false|null|yes|no|on|off", s, re.I):
        return s
    return json.dumps(s)  # a JSON string is a valid YAML double-quoted scalar


def item_lines(items: list[InventoryItem], indent: str) -> list[str]:
    """One aligned flow-mapping line per item, in the same style as config.example.yaml."""
    w_name = max(len(i.name) for i in items) + 1
    w_area = max(len(i.area) for i in items) + 1
    w_sel = max(len(i.selector) for i in items) + 1
    out = []
    for i in items:
        out.append(f"{indent}- {{name: {(i.name + ','):<{w_name}} area: {(i.area + ','):<{w_area}} "
                   f"selector: {(i.selector + ','):<{w_sel}} cadence: {i.cadence}, platform_id: {i.platform_id}, "
                   f"product_id: {i.product_id}, release_id: {i.release_id}, params: {_scalar(i.params)}}}")
    return out


def _inventory_span(lines: list[str]) -> tuple[int, int]:
    """[start, end) line indexes of the inventory block (after `inventory:`)."""
    try:
        start = next(n for n, l in enumerate(lines) if re.match(r"^inventory:\s*(#.*)?$", l)) + 1
    except StopIteration:
        raise InventoryError("config.yaml has no `inventory:` section")
    end = start
    for n in range(start, len(lines)):
        l = lines[n]
        if l.strip() and not l.startswith((" ", "\t", "#")):
            break
        end = n + 1
    return start, end


def _item_rows(lines: list[str]) -> list[tuple[int, str, str]]:
    """(line index, indent, name) of every inventory item line."""
    start, end = _inventory_span(lines)
    rows = []
    for n in range(start, end):
        m = ITEM_LINE.match(lines[n])
        if m:
            rows.append((n, m.group(1), m.group(2)))
    return rows


def current_names(path: Path) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    names = [name for _, _, name in _item_rows(lines)]
    from .config import load_config
    loaded = [i.name for i in load_config(Path(path)).inventory]
    if names != loaded:
        raise InventoryError("config.yaml inventory must have one item per line (`  - {name: ..., ...}`) for "
                             "add/del to edit it; edit it by hand or restore that layout")
    return names


def render(text: str, plan: Plan) -> str:
    lines = text.splitlines()
    gone = set(plan.remove)
    rows = _item_rows(lines)
    drop = {n for n, _, name in rows if name in gone}
    lines = [l for n, l in enumerate(lines) if n not in drop]
    # drop `# added ... by opatchbot add` comments whose items are all gone
    keep = []
    for n, l in enumerate(lines):
        if ADDED_COMMENT.match(l):
            nxt = next((x for x in lines[n + 1:] if x.strip()), "")
            if not ITEM_LINE.match(nxt):
                continue
        keep.append(l)
    lines = keep
    if plan.add:
        rows = _item_rows(lines)
        area_of = {}
        for n, _, name in rows:
            m = re.search(r"\barea:\s*([A-Za-z]+)", lines[n])
            area_of[n] = m.group(1) if m else ""
        same = [n for n, _, _ in rows if area_of[n] == plan.add[0].area]
        if same:
            at = same[-1] + 1
        elif rows:
            at = rows[-1][0] + 1
        else:
            at = _inventory_span(lines)[0]
        indent = rows[0][1] if rows else "  "
        block = ([f"{indent}# {plan.comment}"] if plan.comment else []) + item_lines(plan.add, indent)
        lines[at:at] = block
    return "\n".join(lines) + "\n"


def apply(path: Path, plan: Plan, now: datetime | None = None) -> Path:
    """Write the plan into config.yaml (validated, atomic, dated backup). Returns the backup path."""
    from .config import load_config
    path = Path(path)
    before = current_names(path)
    new_text = render(path.read_text(encoding="utf-8"), plan)
    expected = [n for n in before if n not in set(plan.remove)] + [i.name for i in plan.add]
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        got = sorted(i.name for i in load_config(tmp).inventory)
        if got != sorted(expected):
            raise InventoryError(f"edit check failed: expected items {sorted(expected)}, got {got}")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    now = now or datetime.now()
    backup = path.with_name(f"{path.name}.bak-{now.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return backup


def plan_add(client, config_path: Path, type_code: str, args: list[str], today: date, *, platform: str | None = None,
             release_id: int | None = None, components: list[str] | None = None, product_id: int | None = None,
             file_prefix: str | None = None) -> Plan:
    t = type_code.upper()
    if t not in TYPES:
        raise InventoryError(f"unknown patch type {type_code!r}; use one of {', '.join(TYPES)}")
    need = 2 if t == "AP" else 1
    if len(args) != need:
        raise InventoryError({"AP": "usage: opatchbot add AP <HCM|FSCM|CS> <release>, e.g. add AP HCM 9.2"}.get(
            t, f"usage: opatchbot add {t} <version>, e.g. add {t} {'26ai' if t == 'DB' else '8.63'}"))
    existing = set(current_names(config_path))
    dupes = [n for n in item_names(t, args) if n in existing]
    if dupes:
        raise InventoryError(f"already in config.yaml: {', '.join(dupes)} (remove first with `opatchbot del`)")
    lk = lookup(client, t, args, platform, release_id)
    items = build_items(t, args, lk, components, product_id, file_prefix)
    checks, missing = check_items(client, items, today)
    comment = (f"added {today.isoformat()} by `opatchbot add {t} {' '.join(args)}`: {lk.release_name} "
               f"(release {lk.release_id}), {lk.platform_name} (platform {lk.platform_id})")
    return Plan(add=items, comment=comment, checks=checks, missing=missing)


def plan_del(config_path: Path, targets: list[str]) -> Plan:
    names = current_names(config_path)
    if len(targets) == 1 and targets[0] in names:
        return Plan(remove=[targets[0]])
    if not targets or targets[0].upper() not in TYPES:
        raise InventoryError("usage: opatchbot del <TYPE> <version>  (e.g. del DB 19, del AP HCM 9.2) "
                             "or opatchbot del <item-name>  (see `opatchbot list`)")
    wanted = item_names(targets[0], targets[1:]) if targets[1:] else []
    found = [n for n in names if n in wanted]
    if not found:
        raise InventoryError(f"nothing in config.yaml matches {' '.join(targets)} (looked for {', '.join(wanted)})")
    return Plan(remove=found)


def describe(plan: Plan) -> str:
    lines = []
    if plan.remove:
        lines += ["Remove from config.yaml:"] + [f"  - {n}" for n in plan.remove]
    if plan.add:
        lines += [f"Add to config.yaml ({plan.comment}):"]
        for item in plan.add:
            lines.append(f"  + {item.name}: {item.selector}, product {item.product_id}, release {item.release_id}, "
                         f"platform {item.platform_id}, {item.cadence}")
            if item.name in plan.checks:
                lines.append(f"      check now -> {plan.checks[item.name]}")
    return "\n".join(lines)


def list_rows(items: list[InventoryItem]) -> str:
    head = f"{'type':<5}{'name':<16}{'selector':<22}{'cadence':<11}{'product':>9}{'release':>17}{'platform':>10}"
    rows = [head, "-" * len(head)]
    for i in items:
        rows.append(f"{CODE_FOR_AREA.get(i.area, '?'):<5}{i.name:<16}{i.selector:<22}{i.cadence:<11}"
                    f"{i.product_id or '':>9}{i.release_id or '':>17}{i.platform_id:>10}")
    return "\n".join(rows)
