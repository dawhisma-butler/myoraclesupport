import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from opatchbot.aru import AruError, AuthError, PatchFile
from opatchbot.config import Config, NotifyConfig
from opatchbot.fetch import fetch_manifest, find_prior_copy, stage_path
from opatchbot.manifest import Manifest, ManifestEntry

BODY = b"x" * 1000
SHA = hashlib.sha256(BODY).hexdigest()


def file1(name="PEOPLETOOLS-WIN-8.61.24_1of4.zip", size=1000, sha=SHA):
    return PatchFile(name, size, f"https://updates.oracle.com/Orion/Services/download/{name}?aru=1&patch_file={name}", sha)


def entry(files, item="pt-8.61", area="peopletools"):
    return ManifestEntry(item, area, "resolved", "40014041", "PT 8.61.24 PRODUCT PATCH DPK", 233,
                         datetime(2026, 10, 19, tzinfo=timezone.utc), "newest", files, [])


def manifest(entries, cycle="2026-10-CPU"):
    return Manifest(cycle, "CPU", date(2026, 10, 21), datetime.now(timezone.utc), entries)


@pytest.fixture
def cfg(tmp_path):
    return Config(tmp_path / "stage", tmp_path / "state", 3, 7, 14, NotifyConfig("h", 25, "a@b", ["c@d"], "stdout"), [])


class OkClient:
    def __init__(self, body=BODY):
        self.body, self.calls = body, []

    def download(self, file, dest, resume=True):
        self.calls.append((file.name, Path(dest).exists(), resume))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.body)
        return Path(dest)


class FailingClient:
    def download(self, file, dest, resume=True):
        raise AruError("network down")


class AuthFailAfter(OkClient):
    def __init__(self, n):
        super().__init__(); self.n = n

    def download(self, file, dest, resume=True):
        if len(self.calls) >= self.n:
            self.calls.append((file.name, None, None))
            raise AuthError("401")
        return super().download(file, dest, resume)


def test_stage_layout(cfg):
    assert stage_path(cfg, "2026-10-CPU", entry([file1()]), file1()) == \
        cfg.stage_root / "2026-10-CPU" / "peopletools" / "pt-8.61" / "PEOPLETOOLS-WIN-8.61.24_1of4.zip"


def test_hardlinks_prior_verified_copy(cfg):
    prior = cfg.stage_root / "2026-09-CSPU"
    old = prior / "peopletools" / "pt-8.61" / "PEOPLETOOLS-WIN-8.61.24_1of4.zip"
    old.parent.mkdir(parents=True); old.write_bytes(BODY)
    e = entry([file1()]); e.verified = [file1().name]
    manifest([e], "2026-09-CSPU").to_json(prior / "manifest.json")
    assert find_prior_copy(cfg, "2026-10-CPU", file1()) == old
    r = fetch_manifest(manifest([entry([file1()])]), cfg, FailingClient(), parallel=1)
    new = cfg.stage_root / "2026-10-CPU" / "peopletools" / "pt-8.61" / "PEOPLETOOLS-WIN-8.61.24_1of4.zip"
    assert r.verified == 1 and not r.failed and new.stat().st_ino == old.stat().st_ino


def test_oversized_partial_is_discarded_before_download(cfg):
    dest = stage_path(cfg, "2026-10-CPU", entry([file1()]), file1())
    dest.parent.mkdir(parents=True); dest.write_bytes(b"x" * 2000)
    c = OkClient()
    r = fetch_manifest(manifest([entry([file1()])]), cfg, c, parallel=1)
    assert c.calls == [(file1().name, False, True)] and r.verified == 1


def test_auth_failure_stops_and_flags(cfg):
    files = [file1("a_1of2.zip"), file1("a_2of2.zip")]
    r = fetch_manifest(manifest([entry(files)]), cfg, AuthFailAfter(1), parallel=1)
    assert r.auth_failed and r.verified == 1 and [f for f, _ in r.failed] == ["a_2of2.zip"]


def test_rerun_skips_verified_and_writes_manifest_files(cfg):
    m = manifest([entry([file1()])])
    fetch_manifest(m, cfg, OkClient(), parallel=1)
    assert (cfg.stage_root / "2026-10-CPU" / "MANIFEST.md").exists() and (cfg.stage_root / "2026-10-CPU" / "manifest.json").exists()
    r = fetch_manifest(Manifest.from_json(cfg.stage_root / "2026-10-CPU" / "manifest.json"), cfg, FailingClient(), parallel=1)
    assert r.skipped == 1 and not r.failed and r.verified == 0


def test_sha_mismatch_is_a_failure(cfg):
    r = fetch_manifest(manifest([entry([file1(sha="0" * 64)])]), cfg, OkClient(), parallel=1)
    assert r.verified == 0 and r.failed and "SHA-256" in r.failed[0][1]


def test_unsafe_file_name_is_refused(cfg):
    c = OkClient()
    r = fetch_manifest(manifest([entry([file1(name="../evil.zip")])]), cfg, c, parallel=1)
    assert not c.calls and r.failed and "unsafe" in r.failed[0][1]
