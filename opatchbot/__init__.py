__version__ = "0.1.0"

import os as _os
import sys as _sys

_REEXEC_FLAG = "OPATCHBOT_LD_CLEARED"


def _clear_oracle_libs(environ=None, execve=None) -> None:
    """Restart once without LD_LIBRARY_PATH if Python's XML parser cannot load.

    The oracle user's profile usually puts $ORACLE_HOME/lib first on LD_LIBRARY_PATH. That directory holds an
    older libexpat, so Python 3.12's pyexpat fails with "undefined symbol:
    XML_SetBillionLaughsAttackProtectionMaximumAmplification" before any opatchbot code runs. opatchbot never
    needs Oracle's libraries, so it re-executes itself with that variable removed (once; the flag prevents loops),
    whichever way it was started (wrapper, venv path, cron, systemd).
    """
    environ = _os.environ if environ is None else environ
    execve = _os.execve if execve is None else execve
    if not environ.get("LD_LIBRARY_PATH") or environ.get(_REEXEC_FLAG):
        return
    try:
        import pyexpat  # noqa: F401
        return
    except ImportError:
        pass
    env = {k: v for k, v in environ.items() if k != "LD_LIBRARY_PATH"}
    env[_REEXEC_FLAG] = "1"
    execve(_sys.executable, [_sys.executable, *_sys.argv], env)


_clear_oracle_libs()
