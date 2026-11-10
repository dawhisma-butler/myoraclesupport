"""opatchbot add / del / list / discover: Oracle id lookup, pre-write check, and config.yaml line edits."""
import shutil
from datetime import date
from pathlib import Path

import pytest

from opatchbot import cli, inventory as inv
from opatchbot.aru import parse_search_xml
from opatchbot.config import load_config
from opatchbot.monitor import aru_fallback_released
from opatchbot.notify import Notifier
from opatchbot.run import Services
from opatchbot.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 10)
RELEASES = [(600000000101150, "Oracle Database 19.0.0.0.0"), (600000000153639, "Oracle Database 21.0.0.0.0"),
            (600000000999999, "Oracle Database 23.0.0.0.0"), (600000000204773, "PeopleSoft PeopleTools 8.61"),
            (600000000214961, "PeopleSoft PeopleTools 8.62"), (27001300090200, "PeopleSoft 9.2"),
            (11111, "Oracle Database 12.2.0.1.0")]
PLATFORMS = [(226, "Linux x86-64"), (233, "Microsoft Windows x64 (64-bit)"), (2000, "Generic Platform"), (46, "Linux x86")]
PRODUCTS = [(9480, "Oracle Database Family"), (21917, "PeopleSoft PeopleTools Family")]


class CatalogueClient:
    """metadata() from the lists above; search() from captured live ARU responses."""
    def __init__(self, fx):
        self.fx = fx
        self.searches = []

    def metadata(self, table):
        return {"aru_releases": RELEASES, "aru_platforms": PLATFORMS, "aru_products": PRODUCTS}[table]

    def search(self, **p):
        self.searches.append(p)
        if "bug" in p:
            name = f"patch_{p['bug']}.xml"
        else:
            name = {(9480, 600000000101150): "search_db19_226.xml", (9480, 600000000153639): "search_db21_226.xml",
                    (21917, 600000000204773): "search_pt861_233.xml", (21858, 27001300090200): "search_hcm_92_233.xml",
                    (21714, 27001300090200): "search_fscm_92_233.xml"}.get((p.get("product"), p.get("release")))
        if name is None:
            return []
        return parse_search_xml((self.fx / name).read_text(encoding="utf-8"))


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.example.yaml", p)
    return p


@pytest.fixture
def svc(cfgfile, fixtures_dir, tmp_path):
    cfg = load_config(cfgfile)
    cfg.state_dir = tmp_path / "state"
    return Services(cfg=cfg, store=StateStore(cfg.state_dir), client=CatalogueClient(fixtures_dir),
                    notifier=Notifier(cfg.notify), config_path=cfgfile)


def test_release_lookup_rules():
    assert inv.release_candidates("DB", ["19c"], RELEASES) == [(600000000101150, "Oracle Database 19.0.0.0.0")]
    assert inv.release_candidates("DB", ["26ai"], RELEASES) == [(600000000999999, "Oracle Database 23.0.0.0.0")]
    with26 = RELEASES + [(5, "Oracle AI Database 26.0.0.0.0")]
    assert inv.release_candidates("DB", ["26ai"], with26) == [(5, "Oracle AI Database 26.0.0.0.0")]
    assert inv.release_candidates("PT", ["8.62"], RELEASES) == [(600000000214961, "PeopleSoft PeopleTools 8.62")]
    assert inv.release_candidates("AP", ["HCM", "9.2"], RELEASES) == [(27001300090200, "PeopleSoft 9.2")]


def test_ambiguous_release_and_platform_are_refused(fixtures_dir):
    c = CatalogueClient(fixtures_dir)
    c.metadata = lambda t: (RELEASES + [(7, "Oracle Database 19.0.0.0.0")]) if t == "aru_releases" else PLATFORMS
    with pytest.raises(inv.InventoryError, match="several Oracle releases match"):
        inv.lookup(c, "DB", ["19"])
    assert inv.lookup(c, "DB", ["19"], release_id=7).release_id == 7
    with pytest.raises(inv.InventoryError, match="ambiguous"):
        inv.find_platform(CatalogueClient(fixtures_dir), "Linux")
    assert inv.find_platform(CatalogueClient(fixtures_dir), "Linux x86-64") == (226, "Linux x86-64")


def test_del_then_add_db19_restores_identical_lines(cfgfile, fixtures_dir):
    original = cfgfile.read_text()
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["DB", "19"]))
    text = cfgfile.read_text()
    assert "db19-" not in text and "# Database, Linux x86-64 (live discovery" in text  # notes kept
    plan = inv.plan_add(CatalogueClient(fixtures_dir), cfgfile, "DB", ["19"], TODAY)
    assert plan.missing == [] and "DATABASE RELEASE UPDATE 19.33" in plan.checks["db19-ru"]
    inv.apply(cfgfile, plan)
    new = cfgfile.read_text()
    for line in original.splitlines():
        if "{name: db19-" in line:
            assert line in new  # generated line identical to the hand-written one
    assert "# added 2026-11-10 by `opatchbot add DB 19`: Oracle Database 19.0.0.0.0" in new
    assert len(list(cfgfile.parent.glob("config.yaml.bak-*"))) >= 1


def test_add_existing_is_refused(cfgfile, fixtures_dir):
    with pytest.raises(inv.InventoryError, match="already in config.yaml"):
        inv.plan_add(CatalogueClient(fixtures_dir), cfgfile, "DB", ["19"], TODAY)


def test_add_apps_and_peopletools_build_expected_items(cfgfile, fixtures_dir):
    c = CatalogueClient(fixtures_dir)
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["hcm-9.2"]))
    plan = inv.plan_add(c, cfgfile, "AP", ["HCM", "9.2"], TODAY)
    (item,) = plan.add
    assert (item.name, item.selector, item.product_id, item.release_id, item.params) == \
        ("hcm-9.2", "app_release_patchset", 21858, 27001300090200, {"app_code": "HCM"})
    assert plan.missing == []
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["PT", "8.61"]))
    (pt,) = inv.plan_add(c, cfgfile, "PT", ["8.61"], TODAY).add
    assert pt.params == {"file_prefix": "PEOPLETOOLS-WIN-8.61"} and pt.platform_id == 233


def test_del_removes_its_added_comment(cfgfile, fixtures_dir):
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["DB", "19"]))
    inv.apply(cfgfile, inv.plan_add(CatalogueClient(fixtures_dir), cfgfile, "DB", ["19"], TODAY))
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["DB", "19"]))
    assert "opatchbot add DB 19" not in cfgfile.read_text()


def test_del_single_item_and_unknown(cfgfile):
    assert inv.plan_del(cfgfile, ["db21-ojvm"]).remove == ["db21-ojvm"]
    with pytest.raises(inv.InventoryError, match="nothing in config.yaml matches"):
        inv.plan_del(cfgfile, ["DB", "18"])


def test_cli_add_needs_yes_outside_a_terminal(svc, cfgfile, capsys):
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["DB", "19"]))
    before = cfgfile.read_text()
    assert cli.main(["add", "DB", "19"], services=svc) == 1
    assert cfgfile.read_text() == before and "re-run with --yes" in capsys.readouterr().err
    assert cli.main(["add", "DB", "19", "--yes"], services=svc) == 0
    assert "db19-ru" in [i.name for i in load_config(cfgfile).inventory]


def test_cli_add_with_nothing_found_needs_force(svc, cfgfile, capsys):
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["DB", "21"]))
    assert cli.main(["add", "DB", "21", "--yes"], services=svc) == 1  # 21c OJVM: nothing found
    out = capsys.readouterr()
    assert "NOTHING FOUND" in out.out and "--force" in out.err and "db21-ru" not in cfgfile.read_text()
    assert cli.main(["add", "DB", "21", "--yes", "--force"], services=svc) == 0
    assert cli.main(["del", "db21-ojvm", "--yes"], services=svc) == 0
    names = [i.name for i in load_config(cfgfile).inventory]
    assert "db21-ru" in names and "db21-ojvm" not in names


def test_cli_add_components_subset(svc, cfgfile):
    inv.apply(cfgfile, inv.plan_del(cfgfile, ["DB", "19"]))
    assert cli.main(["add", "DB", "19", "--components", "ru,opatch", "--yes"], services=svc) == 0
    names = [i.name for i in load_config(cfgfile).inventory if i.name.startswith("db19")]
    assert names == ["db19-ru", "db19-opatch"]


def test_cli_list_and_discover(svc, capsys):
    assert cli.main(["list"], services=svc) == 0
    out = capsys.readouterr().out
    assert "DB   db19-ru" in out and "PT   pt-8.61" in out
    assert cli.main(["discover", "database", "19"], services=svc) == 0
    out = capsys.readouterr().out
    assert "600000000101150  Oracle Database 19.0.0.0.0" in out and "1 match(es)" in out


def test_fallback_detector_follows_inventory(fixtures_dir, cfgfile):
    c = CatalogueClient(fixtures_dir)
    items = load_config(cfgfile).inventory
    assert aru_fallback_released(c, date(2026, 10, 1), items) is True
    assert c.searches[-1]["release"] == 600000000204773  # first PeopleTools item (8.61)
    no_pt = [i for i in items if i.selector != "pt_product_dpk"]
    assert aru_fallback_released(c, date(2026, 10, 1), no_pt) is False
