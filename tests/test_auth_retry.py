"""401 handling: one forced token refresh + retry inside AruClient; add's fallback when the catalogue is refused."""
import pytest
import responses

from opatchbot import cli, inventory as inv
from opatchbot.aru import SERVICES, AruClient, AuthError
from tests.test_inventory import CatalogueClient, TODAY, cfgfile, svc  # noqa: F401  (fixtures)


def provider_with_refresh():
    calls = {"refresh": 0}
    def p():
        return f"tok{calls['refresh']}"
    def force_refresh():
        calls["refresh"] += 1
    p.force_refresh = force_refresh
    return p, calls


@responses.activate
def test_401_refreshes_token_once_and_retries():
    responses.add(responses.GET, f"{SERVICES}/metadata", status=401, body="Unauthorized")
    responses.add(responses.GET, f"{SERVICES}/metadata", body='<results><platform id="226">Linux x86-64</platform></results>')
    p, calls = provider_with_refresh()
    assert AruClient(p).metadata("aru_platforms") == [(226, "Linux x86-64")]
    assert calls["refresh"] == 1
    assert responses.calls[1].request.headers["Authorization"] == "Bearer tok1"


@responses.activate
def test_second_401_raises_after_one_retry():
    responses.add(responses.GET, f"{SERVICES}/metadata", status=401, body="Unauthorized")
    p, calls = provider_with_refresh()
    with pytest.raises(AuthError, match="HTTP 401"):
        AruClient(p).metadata("aru_releases")
    assert calls["refresh"] == 1 and len(responses.calls) == 2


class RefusingCatalogue(CatalogueClient):
    """Oracle refuses metadata (as seen on the server) but patch searches work."""
    def metadata(self, table):
        raise AuthError("HTTP 401 from https://updates.oracle.com/Orion/Services/metadata")

    def search(self, **p):
        if p.get("product") == 21591:  # CS family, no release given
            from opatchbot.aru import parse_search_xml
            return parse_search_xml((self.fx / "search_cs_92_233.xml").read_text(encoding="utf-8"))
        return super().search(**p)


def test_add_falls_back_to_search_when_catalogue_refused(fixtures_dir, cfgfile):
    c = RefusingCatalogue(fixtures_dir)
    lk = inv.lookup(c, "AP", ["CS", "9.2"])
    assert (lk.release_id, lk.release_name, lk.platform_id) == (27001300090200, "PeopleSoft 9.2", 233)
    plan = inv.plan_add(c, cfgfile, "AP", ["CS", "9.2"], TODAY)
    (item,) = plan.add
    assert (item.name, item.selector, item.product_id, item.release_id) == ("cs-9.2", "app_update_image", 21591, 27001300090200)
    assert "UPDATE IMAGE" in plan.checks["cs-9.2"]


def test_db_release_from_search_when_catalogue_refused(fixtures_dir, cfgfile):
    class C(RefusingCatalogue):
        def search(self, **p):
            if p.get("product") == 9480 and "release" not in p:
                p = dict(p, release=600000000101150)
            return super().search(**p)
    lk = inv.lookup(C(fixtures_dir), "DB", ["19c"])
    assert (lk.release_id, lk.release_name, lk.platform_id) == (600000000101150, "Oracle Database 19.0.0.0.0", 226)


def test_refused_request_is_a_one_line_error(svc, capsys):
    svc.client = RefusingCatalogue(svc.client.fx)
    assert cli.main(["discover", "database", "2"], services=svc) == 2
    err = capsys.readouterr().err
    assert err.startswith("Oracle refused the request: HTTP 401") and "Traceback" not in err
