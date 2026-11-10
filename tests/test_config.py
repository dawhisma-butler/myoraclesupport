from pathlib import Path

import pytest

from opatchbot.config import cycle_kind, load_config, load_secrets

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def test_example_config_loads():
    cfg = load_config(EXAMPLE)
    names = {i.name for i in cfg.inventory}
    assert {
        "pt-8.61", "pt-8.62", "ptmw-8.61", "ptmw-8.62",
        "hcm-9.2", "fscm-9.2",
        "db19-ru", "db19-ojvm", "db19-jdk", "db19-opatch",
        "db21-ru", "db21-ojvm", "db21-jdk", "db21-opatch",
    } <= names
    assert all(i.cadence == "quarterly" for i in cfg.inventory if i.area == "database")
    assert all(i.cadence == "monthly" for i in cfg.inventory if i.area != "database")
    assert cfg.keep_cycles == 3 and cfg.window_after_days == 14 and cfg.window_before_days == 7
    assert cfg.notify.recipients == ["dawhisma@butler.edu"] and cfg.notify.mode == "smtp"


def test_cycle_kind():
    cfg = load_config(EXAMPLE)
    assert [cycle_kind(m, cfg) for m in (1, 2, 4, 11)] == ["CPU", "CSPU", "CPU", "CSPU"]


def test_secrets_requires_0600(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("MOS_USER=a\nMOS_PASS=b\n")
    p.chmod(0o644)
    with pytest.raises(PermissionError):
        load_secrets(p)
    p.chmod(0o600)
    s = load_secrets(p)
    assert (s.mos_user, s.mos_pass, s.totp_seed) == ("a", "b", None)
