"""Configuration, inventory schema and secrets loading."""
from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SELECTORS = {
    "pt_product_dpk", "pt_mw_dpk", "app_release_patchset", "app_update_image",
    "db_release_update", "db_ojvm", "db_jdk", "opatch",
}
CADENCES = {"quarterly", "monthly"}
# Patch type codes used on the command line (--type, add, del) and the inventory area each one covers.
TYPES = {"DB": "database", "MW": "middleware", "PT": "peopletools", "AP": "apps"}
AREAS = set(TYPES.values())


def parse_types(value: str | None) -> set[str] | None:
    """'DB,PT' -> {'database', 'peopletools'}; None/'' -> None (everything)."""
    if not value:
        return None
    areas = set()
    for code in value.replace(" ", "").upper().split(","):
        if not code:
            continue
        if code not in TYPES:
            raise ValueError(f"unknown patch type {code!r}; use one or more of {', '.join(TYPES)} (comma separated)")
        areas.add(TYPES[code])
    return areas or None


@dataclass
class InventoryItem:
    name: str
    area: str
    selector: str
    cadence: str
    platform_id: int
    product_id: int | None
    release_id: int | None
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.selector not in SELECTORS:
            raise ValueError(f"{self.name}: unknown selector {self.selector!r}")
        if self.area not in AREAS:
            raise ValueError(f"{self.name}: unknown area {self.area!r} (one of {', '.join(sorted(AREAS))})")
        if self.cadence not in CADENCES:
            raise ValueError(f"{self.name}: unknown cadence {self.cadence!r}")


@dataclass
class NotifyConfig:
    smtp_host: str
    smtp_port: int
    sender: str
    recipients: list[str]
    mode: str = "smtp"


@dataclass
class Config:
    stage_root: Path
    state_dir: Path
    keep_cycles: int
    window_before_days: int
    window_after_days: int
    notify: NotifyConfig
    inventory: list[InventoryItem]
    cpu_months: tuple[int, ...] = (1, 4, 7, 10)
    auto_login: bool = True  # re-run the unattended login when the saved session has expired
    secrets_dir: Path | None = None  # session.json + token.txt; defaults to state_dir

    @property
    def secret_dir(self) -> Path:
        return self.secrets_dir or self.state_dir


@dataclass
class Secrets:
    mos_user: str
    mos_pass: str
    totp_seed: str | None = None


class ConfigError(ValueError):
    """config.yaml is missing, unreadable or invalid; the message says where and how to fix it."""


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e.strerror or e}") from e
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        hint = ""
        if "\t" in text:
            tabs = [str(n) for n, l in enumerate(text.splitlines(), 1) if "\t" in l]
            hint = (f"\nThe file contains TAB characters (line {', '.join(tabs[:10])}); YAML needs spaces. Fix with:\n"
                    f"  sed -i 's/\\t/    /g' {path}")
        raise ConfigError(f"{path}{where}: {getattr(e, 'problem', None) or e}{hint}") from e
    try:
        notify = NotifyConfig(**raw["notify"])
        inventory = [InventoryItem(**item) for item in raw.get("inventory", [])]
    except KeyError as e:
        raise ConfigError(f"{path}: missing setting {e}") from e
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from e
    return _config(raw, notify, inventory)


def _config(raw: dict, notify: NotifyConfig, inventory: list) -> Config:
    if "stage_root" not in raw or "state_dir" not in raw:
        raise ConfigError("config.yaml needs stage_root and state_dir")
    return Config(
        stage_root=Path(raw["stage_root"]),
        state_dir=Path(raw["state_dir"]),
        keep_cycles=int(raw.get("keep_cycles", 3)),
        window_before_days=int(raw.get("window_before_days", 7)),
        window_after_days=int(raw.get("window_after_days", 14)),
        notify=notify,
        inventory=inventory,
        cpu_months=tuple(raw.get("cpu_months", (1, 4, 7, 10))),
        auto_login=bool(raw.get("auto_login", True)),
        secrets_dir=Path(raw["secrets_dir"]) if raw.get("secrets_dir") else None,
    )


def load_secrets(path: Path) -> Secrets:
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"{path} must be mode 0600 (is {oct(mode)})")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    if not values.get("MOS_USER") or not values.get("MOS_PASS"):
        raise ValueError(f"{path}: MOS_USER and MOS_PASS are required")
    return Secrets(values["MOS_USER"], values["MOS_PASS"], values.get("MOS_TOTP_SEED") or None)


def cycle_kind(month: int, cfg: Config) -> str:
    return "CPU" if month in cfg.cpu_months else "CSPU"
