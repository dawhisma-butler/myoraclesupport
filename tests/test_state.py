from datetime import date, datetime, timezone

import pytest

from opatchbot.state import CycleRecord, StateStore


def rec(cycle="2026-10-CPU", status="scheduled"):
    return CycleRecord(cycle, "CPU", date(2026, 10, 21), status, None, None, None, None, [])


def test_roundtrip_json(tmp_path):
    s = StateStore(tmp_path)
    r = rec(); r.advisory_url = "https://www.oracle.com/security-alerts/cpuoct2026.html"
    r.last_run = datetime(2026, 10, 21, 7, tzinfo=timezone.utc); r.notified = ["first_pass"]
    s.upsert(r)
    loaded = StateStore(tmp_path).load()["2026-10-CPU"]
    assert loaded == r and isinstance(loaded.scheduled, date) and loaded.last_run.tzinfo is not None


def test_legal_and_illegal_transitions(tmp_path):
    s = StateStore(tmp_path); s.upsert(rec())
    assert s.transition("2026-10-CPU", "released").status == "released"
    assert s.transition("2026-10-CPU", "partial").status == "partial"
    assert s.transition("2026-10-CPU", "failed").status == "failed"
    assert s.transition("2026-10-CPU", "fetched").status == "fetched"
    s.upsert(rec("2026-11-CSPU"))
    with pytest.raises(ValueError):
        s.transition("2026-11-CSPU", "fetched")
    with pytest.raises(KeyError):
        s.transition("nope", "released")


def test_atomic_write_leaves_no_tmp_and_creates_cycle_dir(tmp_path):
    s = StateStore(tmp_path); s.upsert(rec())
    assert (tmp_path / "cycles.json").exists() and not list(tmp_path.glob("*.tmp"))
    d = s.cycle_dir("2026-10-CPU")
    assert d == tmp_path / "cycles" / "2026-10-CPU" and d.is_dir()
