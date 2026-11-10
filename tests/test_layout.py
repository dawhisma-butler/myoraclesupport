"""Server layout: secrets_dir, single-instance lock, refusing root, atomic private files."""
import shutil
from pathlib import Path

from opatchbot import cli
from opatchbot.config import load_config
from opatchbot.token import TokenStore

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path, secrets_dir=True):
    text = (ROOT / "config.example.yaml").read_text()
    text = text.replace("/opt/data2/download/patch", str(tmp_path / "stage")).replace("/opt/app/patch/state", str(tmp_path / "state"))
    text = text.replace("/home/oracle/.mos", str(tmp_path / "mos")) if secrets_dir else \
        "\n".join(l for l in text.splitlines() if not l.startswith("secrets_dir:"))
    p = tmp_path / "config.yaml"; p.write_text(text)
    return p


def test_example_config_points_at_server_layout():
    cfg = load_config(ROOT / "config.example.yaml")
    assert (str(cfg.stage_root), str(cfg.state_dir), str(cfg.secret_dir)) == \
        ("/opt/data2/download/patch", "/opt/app/patch/state", "/home/oracle/.mos")
    assert (cli.DEFAULT_CONFIG, cli.DEFAULT_SECRETS) == ("/opt/app/patch/config.yaml", "/home/oracle/.mos/secrets.env")


def test_secret_files_live_in_secrets_dir(tmp_path):
    args = cli.build_parser().parse_args(["--config", str(_cfg(tmp_path)), "status"])
    svc = cli.build_services(args)
    assert svc.token_store.path == tmp_path / "mos" / "token.txt"
    assert svc.browser.session_file == tmp_path / "mos" / "session.json"
    assert (tmp_path / "mos").stat().st_mode & 0o777 == 0o700


def test_secrets_dir_defaults_to_state_dir(tmp_path):
    cfg = load_config(_cfg(tmp_path, secrets_dir=False))
    assert cfg.secret_dir == cfg.state_dir


def test_single_instance_lock(tmp_path):
    first = cli._single_instance(tmp_path)
    assert first is not None and cli._single_instance(tmp_path) is None
    first.close()
    assert cli._single_instance(tmp_path) is not None


def test_busy_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPATCHBOT_ALLOW_ROOT", "1")
    cfgp = _cfg(tmp_path)
    held = cli._single_instance(tmp_path / "state")
    assert cli.main(["--config", str(cfgp), "monitor"]) == cli.EXIT_BUSY
    assert "another opatchbot run is in progress" in capsys.readouterr().err
    held.close()


def test_refuses_root(monkeypatch, capsys):
    monkeypatch.delenv("OPATCHBOT_ALLOW_ROOT", raising=False)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    assert cli.main(["status"]) == 1
    assert "must not run as root" in capsys.readouterr().err


def test_private_files_are_replaced_atomically(tmp_path):
    s = TokenStore(tmp_path / "t.txt")
    s.write("a"); s.write("b")
    assert s.read() == "b" and (tmp_path / "t.txt").stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["t.txt"]  # no temp files left behind
