from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from opatchbot.aru import AuthError, Patch, PatchFile, parse_search_xml
from opatchbot.config import InventoryItem, load_config
from opatchbot.manifest import Manifest, ManifestEntry
from opatchbot.resolve import (Window, resolve_item, select_app_release_patchset, select_app_update_image,
                               select_db_abstract, select_opatch, select_pt_mw_dpk, select_pt_product_dpk, window_for)

CFG = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
ITEMS = {i.name: i for i in CFG.inventory}
OCT = Window(date(2026, 10, 14), date(2026, 11, 3))


def fx(fixtures_dir, name):
    return parse_search_xml((fixtures_dir / name).read_text())


def make_patch(number, abstract, released, files, platform=233, release_name="8.61.0", bundle=""):
    dt = datetime(*released, tzinfo=timezone.utc)
    return Patch(number, abstract, 1, 1, release_name, platform, dt, dt, 1,
                 [PatchFile(f, 1, f"https://updates.oracle.com/x/{f}", None) for f in files], bundle)


def test_window_for():
    w = window_for(date(2026, 10, 21), CFG)
    assert (w.start, w.end) == (date(2026, 10, 14), date(2026, 11, 4))


def test_pt_product_dpk_picks_newest(fixtures_dir):
    p, reason = select_pt_product_dpk(ITEMS["pt-8.61"], fx(fixtures_dir, "search_pt861_233.xml"), OCT)
    assert p.number == "40014041" and "newest" in reason


def test_pt_product_dpk_outside_window_still_newest(fixtures_dir):
    p, reason = select_pt_product_dpk(ITEMS["pt-8.61"], fx(fixtures_dir, "search_pt861_233.xml"), Window(date(2026, 12, 1), date(2026, 12, 22)))
    assert p.number == "40014041" and "released before window" in reason


def test_pt_mw_dpk(fixtures_dir):
    p, _ = select_pt_mw_dpk(ITEMS["ptmw-8.61"], fx(fixtures_dir, "search_pt861_233.xml"), OCT)
    assert p.number == "40136983"


def test_tie_breaks_on_released_and_lists_runner_up():
    a = make_patch("1", "PT 8.61.24 PRODUCT PATCH DPK", (2026, 10, 19), ["PEOPLETOOLS-WIN-8.61.24_1of4.zip"])
    b = make_patch("2", "PT 8.61.24 PRODUCT PATCH DPK", (2026, 10, 21), ["PEOPLETOOLS-WIN-8.61.24_1of4.zip"])
    p, reason = select_pt_product_dpk(ITEMS["pt-8.61"], [a, b], OCT)
    assert p.number == "2" and "also matched: 1" in reason


def test_app_release_patchset_hcm(fixtures_dir):
    p, _ = select_app_release_patchset(ITEMS["hcm-9.2"], fx(fixtures_dir, "search_hcm_92_233.xml"), OCT)
    assert p.number == "40016683" and "UPDATE IMAGE" not in p.abstract


def test_app_release_patchset_fscm(fixtures_dir):
    p, _ = select_app_release_patchset(ITEMS["fscm-9.2"], fx(fixtures_dir, "search_fscm_92_233.xml"), OCT)
    assert p.abstract == "PEOPLESOFT RELEASE PATCHSET: FSCM - 10292026"


def test_app_release_patchset_missing_outside_window(fixtures_dir):
    p, reason = select_app_release_patchset(ITEMS["hcm-9.2"], fx(fixtures_dir, "search_hcm_92_233.xml"), Window(date(2027, 1, 1), date(2027, 1, 20)))
    assert p is None and "no PEOPLESOFT RELEASE PATCHSET: HCM" in reason


CS_IMAGE = InventoryItem("cs-9.2", "apps", "app_update_image", "monthly", 233, 21591, 27001300090200,
                         {"abstract_regex": "^PEOPLESOFT CS UPDATE IMAGE .* - NATIVE OS$"})  # opt-in in config.example.yaml


def test_app_update_image_cs(fixtures_dir):
    p, reason = select_app_update_image(CS_IMAGE, fx(fixtures_dir, "search_cs_92_233.xml"), OCT)
    assert p.number == "39715828" and p.platform_id == 233 and "released before window" in reason


def test_db_selectors(fixtures_dir):
    db = fx(fixtures_dir, "search_db19_226.xml")
    ru, _ = select_db_abstract(ITEMS["db19-ru"], db, OCT)
    ojvm, _ = select_db_abstract(ITEMS["db19-ojvm"], db, OCT)
    jdk, _ = select_db_abstract(ITEMS["db19-jdk"], db, Window(date(2026, 10, 14), date(2026, 11, 4)))
    assert ru.abstract.startswith("DATABASE RELEASE UPDATE 19.33") and ru.psu_bundle.endswith(" DBRU")
    assert ojvm.number == "39803650" and ojvm.platform_id == 226
    assert jdk.number == "40192005"


def test_opatch_selects_release_and_platform(fixtures_dir):
    p, reason = select_opatch(ITEMS["db19-opatch"], fx(fixtures_dir, "patch_6880880.xml"), OCT)
    assert p.number == "6880880" and p.platform_id == 226 and p.release_name.startswith("19.")
    assert p.files[0].name.endswith("Linux-x86-64.zip")


def test_quarterly_item_not_expected_in_cspu():
    e = resolve_item(ITEMS["db19-ru"], client=None, window=Window(date(2026, 11, 10), date(2026, 12, 1)), kind="CSPU")
    assert e.status == "not_expected" and e.reason == "quarterly item; CSPU cycle"


class FakeClient:
    def __init__(self, patches=None, raise_=None):
        self.patches, self.raise_, self.calls = patches or [], raise_, []

    def search(self, **params):
        self.calls.append(params)
        if self.raise_:
            raise self.raise_
        return self.patches

    def patch(self, number):
        return self.patches[0] if self.patches else None


def test_auth_error_propagates():
    with pytest.raises(AuthError):
        resolve_item(ITEMS["pt-8.61"], FakeClient(raise_=AuthError("expired")), OCT, "CPU")


def test_resolve_item_passes_search_params(fixtures_dir):
    c = FakeClient(fx(fixtures_dir, "search_db19_226.xml"))
    e = resolve_item(ITEMS["db19-ojvm"], c, OCT, "CPU")
    assert c.calls == [{"product": 9480, "release": 600000000101150, "platform": 226, "patch_type": "all"}]
    assert e.status == "resolved" and e.patch == "39803650" and e.files[0].sha256


def test_manifest_roundtrip_and_markdown(tmp_path):
    resolved = ManifestEntry("pt-8.61", "peopletools", "resolved", "40014041", "PT 8.61.24 PRODUCT PATCH DPK", 233,
                             datetime(2026, 10, 19, tzinfo=timezone.utc), "newest PT 8.61 product DPK",
                             [PatchFile("a.zip", 10, "https://u/a.zip", "ab" * 32)], [])
    ne = ManifestEntry("db19-ru", "database", "not_expected", None, None, 226, None, "quarterly item; CSPU cycle", [], [])
    m = Manifest("2026-11-CSPU", "CSPU", date(2026, 11, 17), datetime.now(timezone.utc), [resolved, ne])
    m.to_json(tmp_path / "m.json")
    m2 = Manifest.from_json(tmp_path / "m.json")
    assert m2.entries[0].patch == "40014041" and m2.entries[0].files[0].sha256 == "ab" * 32
    md = m2.to_markdown()
    assert "| not expected |" in md and "40014041" in md
    assert [e.item for e in m2.expected()] == ["pt-8.61"] and m2.all_verified() is False
