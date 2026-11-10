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


@dataclass
class Secrets:
    mos_user: str
    mos_pass: str
    totp_seed: str | None = None


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    notify = NotifyConfig(**raw["notify"])
    inventory = [InventoryItem(**item) for item in raw.get("inventory", [])]
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
    )


def load_secrets(path: Path) -> Secrets:
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"{path} must be mode 0600 (is {oct(mode)})")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
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
