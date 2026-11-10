"""Download manifest files into the stage layout: resumable, SHA-256 verified, hardlink reuse."""
from __future__ import annotations

import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .aru import AruClient, AruError, AuthError, PatchFile, sha256_of
from .config import Config
from .manifest import Manifest, ManifestEntry

log = logging.getLogger(__name__)
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class FetchResult:
    cycle_id: str
    verified: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: int = 0
    auth_failed: bool = False


def _safe(name: str) -> bool:
    return bool(SAFE_NAME.match(name)) and name not in (".", "..")


def stage_path(cfg: Config, cycle_id: str, entry: ManifestEntry, file: PatchFile) -> Path:
    p = (cfg.stage_root / cycle_id / entry.area / entry.item / file.name)
    root = cfg.stage_root.resolve()
    if not _safe(file.name) or not str(p.resolve()).startswith(str(root) + os.sep):
        raise AruError(f"unsafe file name {file.name!r}")
    return p


def find_prior_copy(cfg: Config, cycle_id: str, file: PatchFile) -> Path | None:
    """A verified copy of the same file (name + sha256) in an earlier cycle folder."""
    if not cfg.stage_root.exists() or not file.sha256:
        return None
    for d in sorted(cfg.stage_root.iterdir(), reverse=True):
        mpath = d / "manifest.json"
        if d.name == cycle_id or not mpath.exists():
            continue
        try:
            m = Manifest.from_json(mpath)
        except Exception:
            continue
        for e in m.entries:
            for f in e.files:
                if f.name == file.name and f.sha256 == file.sha256 and f.name in e.verified:
                    cand = d / e.area / e.item / f.name
                    if cand.exists() and cand.stat().st_size == file.size:
                        return cand
    return None


def _already_verified(dest: Path, file: PatchFile, entry: ManifestEntry) -> bool:
    if not dest.exists() or dest.stat().st_size != file.size:
        return False
    if file.name in entry.verified:
        return True
    return bool(file.sha256) and sha256_of(dest) == file.sha256.lower()


def _fetch_one(cfg: Config, cycle_id: str, entry: ManifestEntry, file: PatchFile, client: AruClient) -> tuple[str, str | None]:
    """Returns (status, error) with status in {'verified','skipped','failed','auth'}."""
    try:
        dest = stage_path(cfg, cycle_id, entry, file)
    except AruError as e:
        return "failed", str(e)
    try:
        if _already_verified(dest, file, entry):
            return "skipped", None
        prior = find_prior_copy(cfg, cycle_id, file)
        if prior is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.unlink(missing_ok=True)
            try:
                os.link(prior, dest)
            except OSError:
                shutil.copy2(prior, dest)
            return "verified", None
        if dest.exists() and dest.stat().st_size >= file.size:  # exists but not verified: start over
            dest.unlink()
        client.download(file, dest, resume=True)
        if file.sha256 and sha256_of(dest) != file.sha256.lower():
            dest.unlink(missing_ok=True)
            return "failed", f"{file.name}: SHA-256 mismatch after download"
        if dest.stat().st_size != file.size:
            return "failed", f"{file.name}: size {dest.stat().st_size} != {file.size}"
        return "verified", None
    except AuthError as e:
        return "auth", str(e)
    except (AruError, OSError) as e:
        return "failed", f"{file.name}: {e}"


def fetch_manifest(manifest: Manifest, cfg: Config, client: AruClient, parallel: int = 2) -> FetchResult:
    result = FetchResult(manifest.cycle_id)
    jobs = [(e, f) for e in manifest.entries if e.status == "resolved" for f in e.files]
    stop = {"auth": False}

    def run(job):
        e, f = job
        if stop["auth"]:
            return e, f, "failed", f"{f.name}: skipped (auth failed earlier)"
        status, err = _fetch_one(cfg, manifest.cycle_id, e, f, client)
        if status == "auth":
            stop["auth"] = True
        return e, f, status, err

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        for e, f, status, err in ex.map(run, jobs):
            if status in ("verified", "skipped"):
                if f.name not in e.verified:
                    e.verified.append(f.name)
                if status == "verified":
                    result.verified += 1
                else:
                    result.skipped += 1
            elif status == "auth":
                result.auth_failed = True
                result.failed.append((f.name, err or "auth failed"))
            else:
                result.failed.append((f.name, err or "failed"))
                log.warning("%s", err)

    _move_superseded(cfg, manifest)
    out = cfg.stage_root / manifest.cycle_id
    out.mkdir(parents=True, exist_ok=True)
    manifest.to_json(out / "manifest.json")
    (out / "MANIFEST.md").write_text(manifest.to_markdown(), encoding="utf-8")
    return result


def _move_superseded(cfg: Config, manifest: Manifest) -> None:
    """Files in an item folder that are not part of the item's current patch go to <item>/superseded/."""
    for e in manifest.entries:
        if e.status != "resolved":
            continue
        item_dir = cfg.stage_root / manifest.cycle_id / e.area / e.item
        if not item_dir.is_dir():
            continue
        current = {f.name for f in e.files} | {f.name + ".part" for f in e.files}
        for child in item_dir.iterdir():
            if child.is_file() and child.name not in current:
                sup = item_dir / "superseded"
                sup.mkdir(exist_ok=True)
                child.replace(sup / child.name)
                log.info("%s: superseded %s moved aside", e.item, child.name)
