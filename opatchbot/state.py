"""Cycle state: one JSON file, atomic writes, explicit transition table."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

LEGAL = {
    ("scheduled", "released"), ("released", "partial"), ("released", "fetched"), ("released", "failed"),
    ("partial", "fetched"), ("partial", "failed"), ("failed", "partial"), ("failed", "fetched"),
    ("fetched", "partial"), ("fetched", "failed"),  # a newer patch appeared later in the window
}


@dataclass
class CycleRecord:
    cycle_id: str
    kind: str
    scheduled: date
    status: str
    advisory_url: str | None
    released_seen: date | None
    last_run: datetime | None
    last_result: str | None
    notified: list[str] = field(default_factory=list)


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "cycles.json"

    def load(self) -> dict[str, CycleRecord]:
        if not self.path.exists():
            return {}
        out: dict[str, CycleRecord] = {}
        for cid, d in json.loads(self.path.read_text(encoding="utf-8")).items():
            d["scheduled"] = date.fromisoformat(d["scheduled"])
            d["released_seen"] = date.fromisoformat(d["released_seen"]) if d.get("released_seen") else None
            d["last_run"] = datetime.fromisoformat(d["last_run"]) if d.get("last_run") else None
            out[cid] = CycleRecord(**d)
        return out

    def save(self, records: dict[str, CycleRecord]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        def enc(o):
            if isinstance(o, (datetime, date)):
                return o.isoformat()
            raise TypeError(type(o))
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in sorted(records.items())}, default=enc, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def upsert(self, record: CycleRecord) -> None:
        records = self.load()
        records[record.cycle_id] = record
        self.save(records)

    def transition(self, cycle_id: str, new_status: str) -> CycleRecord:
        records = self.load()
        rec = records[cycle_id]
        if (rec.status, new_status) not in LEGAL:
            raise ValueError(f"{cycle_id}: illegal transition {rec.status} -> {new_status}")
        rec.status = new_status
        self.save(records)
        return rec

    def cycle_dir(self, cycle_id: str) -> Path:
        d = self.state_dir / "cycles" / cycle_id
        d.mkdir(parents=True, exist_ok=True)
        return d
