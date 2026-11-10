"""--type filtering: parsing, resolve, and the manual partial run's merge/no-side-effects rules."""
import json
from datetime import date

import pytest

from opatchbot import cli
from opatchbot.config import InventoryItem, parse_types
from opatchbot.run import run_cycle
from tests.test_cli import DB, PT, svc  # noqa: F401  (svc fixture)


def test_parse_types():
    assert parse_types(None) is None and parse_types("") is None
    assert parse_types("db") == {"database"}
    assert parse_types("PT, MW") == {"peopletools", "middleware"}
    assert parse_types("AP") == {"apps"}
    with pytest.raises(ValueError, match="unknown patch type 'XX'"):
        parse_types("DB,XX")


def test_inventory_area_must_be_known():
    with pytest.raises(ValueError, match="unknown area"):
        InventoryItem("x", "weblogic", "pt_mw_dpk", "monthly", 233, 1, 1, {})


def test_resolve_with_type_only_lists_those_items(svc, capsys):
    svc.cfg.inventory = [PT, DB]
    assert cli.main(["resolve", "2026-10-CPU", "--type", "DB"], services=svc) == 0
    out = capsys.readouterr().out
    assert "db19-ru" in out and "pt-8.61" not in out


def test_bad_type_is_rejected(svc, capsys):
    assert cli.main(["run", "2026-10-CPU", "--type", "ZZ"], services=svc) == 1
    assert "unknown patch type" in capsys.readouterr().err


def test_filtered_run_merges_and_leaves_cycle_state_alone(svc, capsys):
    svc.cfg.inventory = [PT, DB]
    stage = svc.cfg.stage_root / "2026-10-CPU"
    before = svc.store.load()["2026-10-CPU"]

    rc = run_cycle("2026-10-CPU", svc, date(2026, 10, 21), areas={"peopletools"})
    out = capsys.readouterr().out
    assert rc == 0 and "Filtered run (peopletools)" in out and "===" not in out  # summary printed, no email
    after = svc.store.load()["2026-10-CPU"]
    assert (after.status, after.last_run, after.notified) == (before.status, before.last_run, before.notified)
    m = json.loads((stage / "manifest.json").read_text())
    assert [e["item"] for e in m["entries"]] == ["pt-8.61"]

    # a DB-only run adds its item and keeps the PeopleTools entry and its verified files
    run_cycle("2026-10-CPU", svc, date(2026, 10, 21), areas={"database"})
    m = json.loads((stage / "manifest.json").read_text())
    by = {e["item"]: e for e in m["entries"]}
    assert set(by) == {"pt-8.61", "db19-ru"} and len(by["pt-8.61"]["verified"]) == 4

    # the full (unfiltered) run afterwards still runs and reuses the verified PeopleTools files
    svc.client.downloads.clear()
    run_cycle("2026-10-CPU", svc, date(2026, 10, 21))
    assert svc.client.downloads == []
    assert "first_pass" in svc.store.load()["2026-10-CPU"].notified
