"""Client for Oracle's ARU patch services at updates.oracle.com/Orion/Services.

All access to updates.oracle.com goes through this module. The bearer token is supplied by a
callable so it can be refreshed mid-run; it is only ever placed in a request header.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests
from defusedxml import ElementTree as ET

BASE = "https://updates.oracle.com"
SERVICES = f"{BASE}/Orion/Services"
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class AruError(Exception):
    """Non-auth failure talking to ARU (bad status, bad XML, digest mismatch)."""


class AuthError(AruError):
    """ARU rejected the token (401/403) or answered with a sign-in page."""


@dataclass
class PatchFile:
    name: str
    size: int
    url: str
    sha256: str | None = None


@dataclass
class Patch:
    number: str
    abstract: str
    product_id: int | None
    release_id: int | None
    release_name: str
    platform_id: int | None
    released: datetime
    updated: datetime
    size: int
    files: list[PatchFile] = field(default_factory=list)
    psu_bundle: str = ""


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except ValueError:
        return None


def _dt(el) -> datetime:
    if el is None:
        return datetime.fromtimestamp(0, timezone.utc)
    ms = el.get("in_epoch_ms")
    if ms and ms.isdigit():
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc)
    try:
        return datetime.strptime((el.text or "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def looks_like_signin(text: str) -> bool:
    head = text[:512].lower()
    return "<html" in head or "<!doctype html" in head or any(
        s in text for s in ("signon.oracle.com", "login.oracle.com", "identity.oraclecloud.com", "Sign in to Oracle")
    )


def parse_search_xml(text: str) -> list[Patch]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:  # type: ignore[attr-defined]
        raise AruError(f"ARU returned unparseable XML: {e}") from e
    out: list[Patch] = []
    for p in root.iter("patch"):
        bug = p.find("bug")
        abstract = (bug.findtext("abstract") or "").strip() if bug is not None else ""
        product = p.find("product")
        release = p.find("release")
        platform = p.find("platform")
        files: list[PatchFile] = []
        for f in p.iter("file"):
            url_el = f.find("download_url")
            host = (url_el.get("host") if url_el is not None else None) or BASE
            path = (url_el.text or "").strip() if url_el is not None else ""
            sha = None
            for d in f.iter("digest"):
                if (d.get("type") or "").upper() == "SHA-256":
                    sha = (d.text or "").strip().lower() or None
            files.append(PatchFile((f.findtext("name") or "").strip(), _int(f.findtext("size")) or 0, host + path, sha))
        out.append(Patch(
            number=(p.findtext("name") or "").strip(),
            abstract=abstract,
            product_id=_int(product.get("id")) if product is not None else None,
            release_id=_int(release.get("id")) if release is not None else None,
            release_name=(release.get("name") or "") if release is not None else "",
            platform_id=_int(platform.get("id")) if platform is not None else None,
            released=_dt(p.find("released_date")),
            updated=_dt(p.find("updated_date")),
            size=_int(p.findtext("size")) or 0,
            files=files,
            psu_bundle=(p.findtext("psu_bundle") or "").strip(),
        ))
    return out


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class AruClient:
    def __init__(self, token_provider: Callable[[], str], session: requests.Session | None = None, timeout: int = 120):
        self._token = token_provider
        self._s = session or requests.Session()
        self._timeout = timeout

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._token()}", "User-Agent": "opatchbot/0.1"}
        if extra:
            h.update(extra)
        return h

    def _get_text(self, url: str, params: dict | None = None) -> str:
        r = self._s.get(url, params=params, headers=self._headers(), timeout=self._timeout, allow_redirects=True)
        if r.status_code in (401, 403):
            raise AuthError(f"HTTP {r.status_code} from {url}")
        if r.status_code != 200:
            raise AruError(f"HTTP {r.status_code} from {url}")
        if looks_like_signin(r.text):
            raise AuthError(f"sign-in page returned for {url} (token expired?)")
        return r.text

    def search(self, **params: int | str) -> list[Patch]:
        return parse_search_xml(self._get_text(f"{SERVICES}/search", params))

    def patch(self, number: str) -> Patch | None:
        found = parse_search_xml(self._get_text(f"{SERVICES}/search", {"bug": number}))
        return found[0] if found else None

    def metadata(self, table: str) -> list[tuple[int, str]]:
        root = ET.fromstring(self._get_text(f"{SERVICES}/metadata", {"table": table}))
        rows: list[tuple[int, str]] = []
        for el in root.iter():
            if el.get("id") and el.tag in ("product", "release", "platform", "language"):
                i = _int(el.get("id"))
                if i is not None:
                    rows.append((i, (el.text or "").strip()))
        return rows

    def head(self, url: str) -> int:
        r = self._s.head(url, headers=self._headers(), timeout=self._timeout, allow_redirects=True)
        if r.status_code in (401, 403):
            raise AuthError(f"HTTP {r.status_code} from HEAD {url}")
        return int(r.headers.get("Content-Length") or 0)

    def download(self, file: PatchFile, dest: Path, resume: bool = True) -> Path:
        if not SAFE_NAME.match(file.name):
            raise AruError(f"refusing unsafe file name {file.name!r}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        if dest.exists() and not part.exists():
            dest.rename(part)
        have = part.stat().st_size if part.exists() else 0
        if have >= file.size or not resume:  # >= : a full-size file with a wrong hash must restart, not 416 forever
            part.unlink(missing_ok=True)
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with self._s.get(file.url, headers=self._headers(headers), timeout=self._timeout, stream=True, allow_redirects=True) as r:
            if r.status_code in (401, 403):
                raise AuthError(f"HTTP {r.status_code} downloading {file.name}")
            if have and r.status_code == 200:  # server ignored Range: start over
                have = 0
                part.unlink(missing_ok=True)
            elif r.status_code not in (200, 206):
                raise AruError(f"HTTP {r.status_code} downloading {file.name}")
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise AuthError(f"sign-in page returned while downloading {file.name}")
            with open(part, "ab" if have else "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        size = part.stat().st_size
        if file.size and size != file.size:
            raise AruError(f"{file.name}: size {size} != expected {file.size}")
        if file.sha256:
            got = sha256_of(part)
            if got != file.sha256.lower():
                part.unlink(missing_ok=True)
                raise AruError(f"{file.name}: SHA-256 mismatch")
        part.rename(dest)
        return dest
