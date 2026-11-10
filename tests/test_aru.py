import hashlib
import re

import pytest
import responses

from opatchbot.aru import AruClient, AuthError, PatchFile, looks_like_signin, parse_search_xml

DL = "https://updates.oracle.com/Orion/Services/download/f.zip?aru=1&patch_file=f.zip"


def test_parse_search_fixture(fixtures_dir):
    patches = parse_search_xml((fixtures_dir / "search_pt861_233.xml").read_text())
    assert len(patches) == 28
    p = next(x for x in patches if x.number == "40014041")
    assert p.abstract == "PT 8.61.24 PRODUCT PATCH DPK" and p.platform_id == 233
    assert p.release_id == 600000000204773 and p.release_name == "8.61.0"
    assert {f.name for f in p.files} == {f"PEOPLETOOLS-WIN-8.61.24_{i}of4.zip" for i in (1, 2, 3, 4)}
    assert p.released.isoformat().startswith("2026-10-19T02:05:09")


def test_patch_detail_has_sha256(fixtures_dir):
    patches = parse_search_xml((fixtures_dir / "patch_40014041.xml").read_text())
    p = next(x for x in patches if x.platform_id == 233)
    f = next(f for f in p.files if f.name.endswith("_1of4.zip"))
    assert f.size == 125432125
    assert f.sha256 == "700f9e028c75a86e9195bf05c3fc21fe2b3a67979966685ed7ddf429921ea32e"
    assert f.url.startswith("https://updates.oracle.com/Orion/Services/download/")


def test_signin_page_raises_auth_error(fixtures_dir):
    html = (fixtures_dir / "signin.html").read_text()
    assert looks_like_signin(html)
    with responses.RequestsMock() as rs:
        rs.get(re.compile(r"https://updates\.oracle\.com/Orion/Services/search.*"), body=html, status=200)
        with pytest.raises(AuthError):
            AruClient(lambda: "t").search(product=21917)


@responses.activate
def test_download_resumes_and_verifies(tmp_path):
    body = b"x" * 1000
    dest = tmp_path / "f.zip"
    dest.write_bytes(body[:400])
    responses.add(responses.GET, DL, body=body[400:], status=206,
                  headers={"Content-Range": "bytes 400-999/1000"})
    f = PatchFile("f.zip", 1000, DL, hashlib.sha256(body).hexdigest())
    assert AruClient(lambda: "t").download(f, dest) == dest
    assert dest.stat().st_size == 1000 and dest.read_bytes() == body
    req = responses.calls[0].request
    assert req.headers["Range"] == "bytes=400-" and req.headers["Authorization"] == "Bearer t"


@responses.activate
def test_download_restarts_when_partial_is_oversized(tmp_path):
    body = b"y" * 1000
    dest = tmp_path / "f.zip"
    dest.write_bytes(b"x" * 2000)
    responses.add(responses.GET, DL, body=body, status=200)
    f = PatchFile("f.zip", 1000, DL, hashlib.sha256(body).hexdigest())
    AruClient(lambda: "t").download(f, dest)
    assert "Range" not in responses.calls[0].request.headers and dest.read_bytes() == body


def test_parse_psu_bundle_label(fixtures_dir):
    patches = parse_search_xml((fixtures_dir / "search_db19_226.xml").read_text())
    ru = [p for p in patches if p.abstract.startswith("DATABASE RELEASE UPDATE 19.")]
    assert ru and all(p.psu_bundle.endswith(" DBRU") for p in ru)
    ojvm = next(p for p in patches if p.abstract.startswith("OJVM RELEASE UPDATE 19."))
    assert ojvm.psu_bundle.endswith(" OJVMRU") and ojvm.platform_id == 226
    jdk = next(p for p in patches if p.abstract.startswith("JDK BUNDLE PATCH 19."))
    assert jdk.files[0].name.endswith("_Linux-x86-64.zip")


@responses.activate
def test_download_restarts_when_partial_equals_size_but_hash_differs(tmp_path):
    body = b"y" * 1000
    dest = tmp_path / "f.zip"
    dest.write_bytes(b"x" * 1000)  # full size, wrong content
    responses.add(responses.GET, DL, body=body, status=200)
    f = PatchFile("f.zip", 1000, DL, hashlib.sha256(body).hexdigest())
    AruClient(lambda: "t").download(f, dest)
    assert "Range" not in responses.calls[0].request.headers and dest.read_bytes() == body
