"""Self-repair when $ORACLE_HOME/lib on LD_LIBRARY_PATH breaks Python's pyexpat."""
import builtins
import sys

import opatchbot


def _broken_pyexpat(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "pyexpat":
            raise ImportError("undefined symbol: XML_SetBillionLaughsAttackProtectionMaximumAmplification")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)


def test_reexecs_once_without_ld_library_path(monkeypatch):
    _broken_pyexpat(monkeypatch)
    calls = []
    env = {"LD_LIBRARY_PATH": "/opt/app/oracle/product/21.3.0/dbhome_1/lib:/lib", "PATH": "/usr/bin"}
    opatchbot._clear_oracle_libs(env, lambda exe, argv, e: calls.append((exe, argv, e)))
    (exe, argv, newenv), = calls
    assert exe == sys.executable and argv[1:] == sys.argv
    assert "LD_LIBRARY_PATH" not in newenv and newenv["PATH"] == "/usr/bin" and newenv["OPATCHBOT_LD_CLEARED"] == "1"


def test_no_reexec_when_flag_set_or_nothing_to_clear_or_pyexpat_fine(monkeypatch):
    calls = []
    opatchbot._clear_oracle_libs({"LD_LIBRARY_PATH": "/x"}, lambda *a: calls.append(a))  # pyexpat loads fine
    _broken_pyexpat(monkeypatch)
    opatchbot._clear_oracle_libs({"LD_LIBRARY_PATH": "/x", "OPATCHBOT_LD_CLEARED": "1"}, lambda *a: calls.append(a))
    opatchbot._clear_oracle_libs({"PATH": "/usr/bin"}, lambda *a: calls.append(a))
    assert calls == []
