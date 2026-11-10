"""Per-cycle manifest: what this site needs, how each choice was made, and what is verified."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .aru import PatchFile

STATUSES = ("resolved", "missing", "not_expected")


@dataclass
class ManifestEntry:
    item: str
    area: str
    status: str
    patch: str | None
    abstract: str | None
    platform_id: int
    released: datetime | None
    reason: str
    files: list[PatchFile] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        return self.status == "resolved" and bool(self.files) and all(f.name in self.verified for f in self.files)


@dataclass
class Manifest:
    cycle_id: str
    kind: str
    scheduled: date
    created: datetime
    entries: list[ManifestEntry] = field(default_factory=list)

    # -- persistence ---------------------------------------------------------------------------
    def to_json(self, path: Path) -> None:
        def enc(o):
            if isinstance(o, (datetime, date)):
                return o.isoformat()
            raise TypeError(type(o))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), default=enc, indent=2))
        tmp.replace(path)

    @classmethod
    def from_json(cls, path: Path) -> "Manifest":
        d = json.loads(Path(path).read_text())
        entries = []
        for e in d["entries"]:
            e["files"] = [PatchFile(**f) for f in e.get("files", [])]
            e["released"] = datetime.fromisoformat(e["released"]) if e.get("released") else None
            entries.append(ManifestEntry(**e))
        return cls(d["cycle_id"], d["kind"], date.fromisoformat(d["scheduled"]),
                   datetime.fromisoformat(d["created"]), entries)

    # -- views ---------------------------------------------------------------------------------
    def expected(self) -> list[ManifestEntry]:
        return [e for e in self.entries if e.status != "not_expected"]

    def all_verified(self) -> bool:
        exp = self.expected()
        return bool(exp) and all(e.all_verified for e in exp)

    def to_markdown(self) -> str:
        lines = [f"# {self.cycle_id} ({self.kind}) — scheduled {self.scheduled.isoformat()}",
                 f"Manifest created {self.created.strftime('%Y-%m-%d %H:%M %Z')}", "",
                 "| item | area | status | patch | description | released | files | verified | reason |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for e in self.entries:
            status = e.status.replace("_", " ")
            if e.status == "resolved":
                status = "verified" if e.all_verified else ("partial" if e.verified else "resolved")
            size = sum(f.size for f in e.files)
            lines.append("| {} | {} | {} | {} | {} | {} | {} | {}/{} | {} |".format(
                e.item, e.area, status, e.patch or "", (e.abstract or "").replace("|", "/"),
                e.released.date().isoformat() if e.released else "", f"{len(e.files)} ({size / 1e9:.1f} GB)" if e.files else "",
                len(e.verified), len(e.files), e.reason.replace("|", "/")))
        return "\n".join(lines) + "\n"
