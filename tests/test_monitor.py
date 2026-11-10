# Fixture: https://www.oracle.com/security-alerts/ captured 2026-11-09. Two sections, each with a sentence
# "The next four dates are: <d Month YYYY> x4" and a table of released cycles whose first cell links the advisory
# ("Critical Patch Update - October 2026" -> /security-alerts/cpuoct2026.html). Release day itself is not listed
# per row, so released cycles get the third Tuesday of their month as scheduled date.
from datetime import date

from opatchbot.monitor import ScheduledCycle, detect, late_cycles, parse_alerts_page, third_tuesday
from opatchbot.state import CycleRecord, StateStore

TODAY = date(2026, 11, 9)


def test_third_tuesday():
    assert third_tuesday(2026, 10) == date(2026, 10, 20) and third_tuesday(2027, 1) == date(2027, 1, 19)


def test_parse_alerts_fixture(fixtures_dir):
    cycles = parse_alerts_page((fixtures_dir / "security_alerts.html").read_text(), TODAY)
    ids = {c.cycle_id: c for c in cycles}
    assert ids["2026-10-CPU"].advisory_url == "https://www.oracle.com/security-alerts/cpuoct2026.html"
    assert ids["2026-10-CPU"].scheduled == date(2026, 10, 20) and ids["2026-10-CPU"].kind == "CPU"
    assert ids["2026-09-CSPU"].advisory_url.endswith("cspusep2026.html")
    assert ids["2026-11-CSPU"].scheduled == date(2026, 11, 17) and ids["2026-11-CSPU"].advisory_url is None
    assert ids["2027-01-CPU"].scheduled == date(2027, 1, 19) and ids["2027-04-CPU"].scheduled == date(2027, 4, 20)
    assert all(c.advisory_url is None for c in cycles if c.scheduled > TODAY)


def test_detect_does_not_release_future(tmp_path):
    store = StateStore(tmp_path)
    future = ScheduledCycle("2027-01-CPU", "CPU", date(2027, 1, 19), "https://www.oracle.com/security-alerts/cpujan2027.html")
    assert detect([future], store, TODAY) == [] and store.load()["2027-01-CPU"].status == "scheduled"


def test_detect_releases_once_and_records_url(tmp_path):
    store = StateStore(tmp_path)
    c = ScheduledCycle("2026-10-CPU", "CPU", date(2026, 10, 20), None)
    assert detect([c], store, date(2026, 10, 20)) == []  # scheduled day, no advisory yet
    c2 = ScheduledCycle("2026-10-CPU", "CPU", date(2026, 10, 20), "https://www.oracle.com/security-alerts/cpuoct2026.html")
    assert detect([c2], store, date(2026, 10, 21)) == ["2026-10-CPU"]
    rec = store.load()["2026-10-CPU"]
    assert rec.status == "released" and rec.released_seen == date(2026, 10, 21) and rec.advisory_url == c2.advisory_url
    assert detect([c2], store, date(2026, 10, 22)) == []


def test_late_cycles(tmp_path):
    store = StateStore(tmp_path)
    store.upsert(CycleRecord("2026-11-CSPU", "CSPU", date(2026, 11, 17), "scheduled", None, None, None, None, []))
    assert late_cycles(store, date(2026, 11, 19)) == []
    assert [c.cycle_id for c in late_cycles(store, date(2026, 11, 21))] == ["2026-11-CSPU"]


def test_expected_cycles_follow_third_tuesday():
    from opatchbot.monitor import expected_cycles
    got = [(c.cycle_id, c.scheduled) for c in expected_cycles(date(2026, 12, 2))]
    assert got == [("2026-12-CSPU", date(2026, 12, 15)), ("2027-01-CPU", date(2027, 1, 19))]


def test_aru_fallback_accepts_dpk_released_just_before_the_date(fixtures_dir):
    from opatchbot.aru import parse_search_xml
    from opatchbot.config import InventoryItem
    from opatchbot.monitor import aru_fallback_released
    pt = InventoryItem("pt-8.61", "peopletools", "pt_product_dpk", "monthly", 233, 21917, 600000000204773,
                       {"file_prefix": "PEOPLETOOLS-WIN-8.61"})
    client = type("C", (), {"search": lambda self, **p: parse_search_xml((fixtures_dir / "search_pt861_233.xml").read_text())})()
    # PT 8.61.24 (patch 40014041) was released 2026-10-19, one day before the 2026-10-20 CPU
    assert aru_fallback_released(client, date(2026, 10, 20), [pt]) is True
    assert aru_fallback_released(client, date(2026, 10, 20), [pt], days_before=0) is False
    assert aru_fallback_released(client, date(2026, 12, 15), [pt]) is False  # nothing that new yet
