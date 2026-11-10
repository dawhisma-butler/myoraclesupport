# Oracle CPU/CSPU Patch Automation (opatchbot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Linux command-line tool, `opatchbot`, that detects each Oracle CPU/CSPU release, builds a manifest of exactly this site's patches from Oracle's ARU patch services, downloads and verifies them into a per-cycle stage folder, and emails the outcome, needing a human only to refresh the MOS sign-in.

**Architecture:** One Python package with five modules behind one CLI: `aru` (XML search/metadata/download over `updates.oracle.com/Orion/Services`), `token` (bearer token from a Playwright browser session that completed MFA once), `resolve` (inventory → manifest via per-item selectors), `fetch` (resumable, SHA-256-verified staging with hardlink reuse), `monitor` (security-alerts page → cycle state machine), plus `notify`, `state`, `config`, `cli`. State is JSON files under a state directory; configuration is YAML; secrets are a 600-mode env file.

**Tech Stack:** Python 3.11, `requests`, `playwright` (Chromium), `pyotp`, `PyYAML`, `defusedxml`; tests with `pytest` and `responses`. Oracle Linux 8/9 target; developed and unit-tested on macOS.

**Spec:** `docs/superpowers/specs/2026-11-09-oracle-cpu-patch-automation-design.md`

## Global Constraints

- Python ≥ 3.11; no services other than cron/systemd timer and SMTP relay.
- Never print, log, or pass on a command line: the MOS password, TOTP seed, token, or session cookies. Token and session files are created with mode 0600; the secrets file is required to be 0600 or the CLI refuses to start.
- All ARU access goes through `opatchbot/aru.py`; no other module builds `updates.oracle.com` URLs.
- Known ids (from live metadata, 2026-11-09): platforms 233 = Microsoft Windows x64, 226 = Linux x86-64, 2000 = Generic Platform. Products: 21917 PeopleSoft PeopleTools (family), 21918 PeopleSoft Enterprise PT PeopleTools, 21858 PeopleSoft HCM (family), 21591 PeopleSoft CS (family), 9480 Oracle Database (family). Releases: 600000000204773 PeopleTools 8.61, 600000000214961 PeopleTools 8.62, 600000000101150 Oracle Database 19.0.0.0.0, 600000000153639 Oracle Database 21.0.0.0.0, 27001300090200 "PeopleSoft 9.2". FSCM family id not yet found (Task 3 discovers it).
- Cycle ids are `YYYY-MM-CPU` or `YYYY-MM-CSPU`; CPU months are 1, 4, 7, 10. Cycle window: scheduled date −7 days to +14 days.
- Stage layout: `<stage_root>/<cycle_id>/<area>/<item_name>/<file>`; areas are `peopletools`, `middleware`, `apps`, `database`.
- Cadence values: `quarterly` (database items), `monthly` (all others). An item not expected in a cycle is reported `not expected`, never `missing`.
- Cycle states: `scheduled → released → partial → fetched | failed`; `needs_human` is a run status, not a cycle state.
- Default `keep_cycles` = 3; `prune` never runs unless invoked.
- Token refresh threshold: refresh when < 10 minutes remain.

## Review Focus

1. An ARU `search` that returns HTTP 200 with an HTML sign-in page (expired token mid-run): the resolver must raise `AuthError`, not report items as `missing`. Pinned in Task 2.
2. Two patches match a selector in the window (e.g. a re-released DPK): the resolver must pick the latest `released_date` and record the runner-up numbers in `reason`. Pinned in Task 5.
3. A partially downloaded file whose on-disk size exceeds the expected size (changed file on Oracle's side): the fetcher must discard and restart, not resume past the end. Pinned in Task 7.
4. The security-alerts page lists next year's dates before this year's cycles are finished, or lists a date with no advisory link yet: the monitor must not mark a future cycle `released`. Pinned in Task 8.
5. SMTP relay unreachable: the run must still write the manifest and state, exit non-zero, and log the error, so a later `status` shows the truth. Pinned in Task 9.

---

### Task 0: Repository scaffold

**Files:**
- Create: `pyproject.toml`, `opatchbot/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `.gitignore`, `Makefile`
- Move: existing prototype scripts into `prototype/` (`mos_*.sh`, `weekly_sync.sh`, `wget*.sh`, `com.mocs.mos-patch-sync.plist`) with the old `README.md` as `prototype/README.md`

**Interfaces:**
- Produces: package name `opatchbot`, console script `opatchbot = opatchbot.cli:main`; `tests/conftest.py` exposes fixture `fixtures_dir` → `Path("tests/fixtures")`.

- [ ] **Step 1: Initialise git in `/Users/wizbot/mocs/oracle-patch` and commit the spec**

```bash
cd /Users/wizbot/mocs/oracle-patch && git init -b main && mkdir -p prototype && git mv -k mos_*.sh weekly_sync.sh wget.sh wget_40014041_win_x64.sh com.mocs.mos-patch-sync.plist README.md prototype/ 2>/dev/null || mv mos_*.sh weekly_sync.sh wget.sh wget_40014041_win_x64.sh com.mocs.mos-patch-sync.plist README.md prototype/
git add -A && git commit -m "chore: import prototype scripts and design spec"
```

- [ ] **Step 2: Write `pyproject.toml`** with `[project] name="opatchbot" version="0.1.0" requires-python=">=3.11"`, dependencies `requests>=2.31`, `playwright>=1.45`, `pyotp>=2.9`, `PyYAML>=6`, `defusedxml>=0.7`; optional `dev`: `pytest>=8`, `responses>=0.25`; `[project.scripts] opatchbot = "opatchbot.cli:main"`.

- [ ] **Step 3: Write `.gitignore`** containing `state/`, `secrets.env`, `*.egg-info/`, `.venv/`, `__pycache__/`, `tests/fixtures/live/`.

- [ ] **Step 4: Write the failing smoke test** `tests/test_package.py`:

```python
def test_version():
    import opatchbot
    assert opatchbot.__version__ == "0.1.0"
```

- [ ] **Step 5: Run it to verify it fails**: `python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]' && pytest -q` → FAIL: no attribute `__version__`.

- [ ] **Step 6: Implement** `opatchbot/__init__.py` with `__version__ = "0.1.0"`; `Makefile` targets `test` (`pytest -q`) and `lint` (`python -m compileall opatchbot`).

- [ ] **Step 7: Run tests to verify they pass**: `pytest -q` → `1 passed`.

- [ ] **Step 8: Commit**: `git add -A && git commit -m "chore: package scaffold"`.

---

### Task 1: Configuration and inventory schema

**Files:**
- Create: `opatchbot/config.py`, `config.example.yaml`, `secrets.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass InventoryItem(name: str, area: str, selector: str, cadence: str, platform_id: int, product_id: int, release_id: int | None, params: dict)`; `selector` ∈ {`pt_product_dpk`, `pt_mw_dpk`, `app_release_patchset`, `db_release_update`, `db_ojvm`, `db_jdk`, `opatch`}.
  - `@dataclass Config(stage_root: Path, state_dir: Path, keep_cycles: int, window_before_days: int, window_after_days: int, notify: NotifyConfig, inventory: list[InventoryItem], cpu_months: tuple[int,...] = (1,4,7,10))`
  - `@dataclass NotifyConfig(smtp_host: str, smtp_port: int, sender: str, recipients: list[str], mode: str)` with `mode` ∈ {`smtp`, `stdout`}.
  - `@dataclass Secrets(mos_user: str, mos_pass: str, totp_seed: str | None)`
  - `load_config(path: Path) -> Config`; `load_secrets(path: Path) -> Secrets` (raises `PermissionError` if mode is not 0600).
  - `cycle_kind(month: int, cfg: Config) -> str` returning `"CPU"` or `"CSPU"`.

- [ ] **Step 1: Write failing tests** in `tests/test_config.py`:

```python
def test_example_config_loads(tmp_path):
    cfg = load_config(Path("config.example.yaml"))
    names = {i.name for i in cfg.inventory}
    assert {"pt-8.61", "pt-8.62", "ptmw-8.61", "ptmw-8.62", "hcm-9.2", "fscm-9.2", "cs-9.2",
            "db19-ru", "db19-ojvm", "db19-jdk", "db19-opatch",
            "db21-ru", "db21-ojvm", "db21-jdk", "db21-opatch"} <= names
    assert all(i.cadence == "quarterly" for i in cfg.inventory if i.area == "database")
    assert cfg.keep_cycles == 3 and cfg.window_after_days == 14

def test_cycle_kind(): 
    cfg = load_config(Path("config.example.yaml"))
    assert [cycle_kind(m, cfg) for m in (1, 2, 4, 11)] == ["CPU", "CSPU", "CPU", "CSPU"]

def test_secrets_requires_0600(tmp_path):
    p = tmp_path / "secrets.env"; p.write_text("MOS_USER=a\nMOS_PASS=b\n"); p.chmod(0o644)
    with pytest.raises(PermissionError): load_secrets(p)
    p.chmod(0o600); assert load_secrets(p).totp_seed is None
```

- [ ] **Step 2: Run to verify failure**: `pytest tests/test_config.py -q` → ImportError.

- [ ] **Step 3: Write `config.example.yaml`** with `stage_root: /stage/oracle-patches`, `state_dir: /var/lib/opatchbot`, `keep_cycles: 3`, `window_before_days: 7`, `window_after_days: 14`, `notify: {mode: smtp, smtp_host: RELAY.butler.edu, smtp_port: 25, sender: opatchbot@butler.edu, recipients: [dawhisma@butler.edu]}` and the 15 inventory items using the Global Constraints ids (PeopleTools items: product 21917, release per version, platform 233, `params: {file_prefix: "PEOPLETOOLS-WIN-8.61"}` etc.; middleware: `params: {abstract_regex: "^PT-MW-DPK"}`; apps: product = family id (HCM 21858, CS 21591, FSCM `null` with a comment "set by Task 3"), release 27001300090200, platform 233, `params: {app_code: HCM}`; database: product 9480, release 19/21 base id, platform 226; opatch: `params: {bug: 6880880}`). `secrets.env.example` lists `MOS_USER=`, `MOS_PASS=`, `MOS_TOTP_SEED=`.

- [ ] **Step 4: Implement `opatchbot/config.py`** per the Interfaces block (YAML via `yaml.safe_load`; secrets parsed as `KEY=VALUE` lines).

- [ ] **Step 5: Run tests**: `pytest tests/test_config.py -q` → 3 passed.

- [ ] **Step 6: Commit**: `git add -A && git commit -m "feat: config and inventory schema"`.

---

### Task 2: ARU client (search, metadata, patch detail, download)

**Files:**
- Create: `opatchbot/aru.py`, `tests/fixtures/search_pt861_233.xml`, `tests/fixtures/patch_40014041.xml`, `tests/fixtures/platforms.xml`, `tests/fixtures/signin.html`
- Test: `tests/test_aru.py`

**Interfaces:**
- Produces:
  - `@dataclass PatchFile(name: str, size: int, url: str, sha256: str | None)`
  - `@dataclass Patch(number: str, abstract: str, product_id: int, release_id: int, release_name: str, platform_id: int, released: datetime, updated: datetime, size: int, files: list[PatchFile])`
  - `class AuthError(Exception)`, `class AruError(Exception)`
  - `class AruClient(token_provider: Callable[[], str], session: requests.Session | None = None)` with methods `search(**params: int | str) -> list[Patch]`, `patch(number: str) -> Patch | None`, `metadata(table: str) -> list[tuple[int, str]]`, `head(url: str) -> int` (content length), `download(file: PatchFile, dest: Path, resume: bool = True) -> Path`.
  - `parse_search_xml(text: str) -> list[Patch]`, `looks_like_signin(text: str) -> bool`, `sha256_of(path: Path) -> str`.

- [ ] **Step 1: Copy fixtures**: from `/private/tmp/claude-501/-Users-wizbot-mocs/b5220453-b135-4e5e-8893-01a48f3285a6/scratchpad/aru/` copy `prp.xml` → `tests/fixtures/search_pt861_233.xml`, `bug.xml` → `tests/fixtures/patch_40014041.xml`, `platforms.xml` → `tests/fixtures/platforms.xml`, and the 1939-byte 401 body (`hcm_233.xml`) → `tests/fixtures/signin.html`.

- [ ] **Step 2: Write failing tests** in `tests/test_aru.py`:

```python
def test_parse_search_fixture(fixtures_dir):
    patches = parse_search_xml((fixtures_dir / "search_pt861_233.xml").read_text())
    assert len(patches) == 28
    p = next(x for x in patches if x.number == "40014041")
    assert p.abstract == "PT 8.61.24 PRODUCT PATCH DPK" and p.platform_id == 233
    assert {f.name for f in p.files} == {f"PEOPLETOOLS-WIN-8.61.24_{i}of4.zip" for i in (1,2,3,4)}
    assert p.released.isoformat().startswith("2026-10-19T02:05:09")

def test_patch_detail_has_sha256(fixtures_dir):
    p = next(x for x in parse_search_xml((fixtures_dir / "patch_40014041.xml").read_text()) if x.platform_id == 233)
    f = next(f for f in p.files if f.name.endswith("_1of4.zip"))
    assert f.size == 125432125 and f.sha256 == "700f9e028c75a86e9195bf05c3fc21fe2b3a67979966685ed7ddf429921ea32e"
    assert f.url.startswith("https://updates.oracle.com/Orion/Services/download/")

def test_signin_page_raises_auth_error(fixtures_dir):
    html = (fixtures_dir / "signin.html").read_text()
    assert looks_like_signin(html)
    with responses.RequestsMock() as rs:
        rs.get(re.compile(r"https://updates\.oracle\.com/Orion/Services/search.*"), body=html, status=200)
        with pytest.raises(AuthError): AruClient(lambda: "t").search(product=21917)

@responses.activate
def test_download_resumes_and_verifies(tmp_path):
    body = b"x" * 1000; dest = tmp_path / "f.zip"; dest.write_bytes(body[:400])
    responses.add(responses.GET, "https://updates.oracle.com/Orion/Services/download/f.zip?aru=1&patch_file=f.zip",
                  body=body[400:], status=206, headers={"Content-Range": "bytes 400-999/1000"})
    f = PatchFile("f.zip", 1000, "https://updates.oracle.com/Orion/Services/download/f.zip?aru=1&patch_file=f.zip", hashlib.sha256(body).hexdigest())
    assert AruClient(lambda: "t").download(f, dest) == dest and dest.stat().st_size == 1000
    assert responses.calls[0].request.headers["Range"] == "bytes=400-" and responses.calls[0].request.headers["Authorization"] == "Bearer t"
```

- [ ] **Step 3: Run to verify failure**: `pytest tests/test_aru.py -q` → ImportError.

- [ ] **Step 4: Implement `opatchbot/aru.py`** per Interfaces. Parse with `defusedxml.ElementTree`; patch abstract = text of the first `<bug><abstract>` child of `<patch>`; `looks_like_signin` = status is HTML (`<html` within first 512 bytes) or contains `signon.oracle.com`/`login.oracle.com`/`Sign in`; `search` raises `AuthError` on 401/403 or sign-in body; `download` sends `Range` when `dest` exists and is smaller than `file.size`, discards and restarts if larger, writes to `dest.with_suffix(dest.suffix + ".part")` then renames, and verifies `sha256` when present (raises `AruError` on mismatch).

- [ ] **Step 5: Run tests**: `pytest tests/test_aru.py -q` → 4 passed.

- [ ] **Step 6: Commit**: `git add -A && git commit -m "feat: ARU client with fixtures"`.

---

### Task 3: Live discovery of remaining ids and fixture capture (one-off, needs a valid token)

**Files:**
- Create: `scripts/capture_fixtures.py`, `tests/fixtures/search_hcm_92_233.xml`, `tests/fixtures/search_cs_92_233.xml`, `tests/fixtures/search_fscm_92_233.xml`, `tests/fixtures/search_db19_226.xml`, `tests/fixtures/search_db21_226.xml`, `tests/fixtures/patch_6880880.xml`
- Modify: `config.example.yaml` (FSCM family id; any corrected release ids)

**Interfaces:**
- Consumes: `AruClient` from Task 2; a token file produced by the operator (`~/.mos/token.txt`, see prototype README).
- Produces: fixtures used by Task 5 tests; the confirmed abstract patterns written into `config.example.yaml` `params`.

- [ ] **Step 1: Write `scripts/capture_fixtures.py`** taking `--token-file` and `--out tests/fixtures/`, which: finds the FSCM family id with `metadata("aru_products")` filtered by name containing `PeopleSoft` and (`FSCM` or `Financials` or `Supply Chain`) and prints candidates; runs `search(product=<family>, release=27001300090200, platform=233)` for HCM, CS, FSCM, `search(product=9480, release=<19 base>, platform=226)` and the 21c equivalent, `patch("6880880")`; saves each raw XML; prints for each result the 10 newest `(released, number, abstract, release_name)`.

- [ ] **Step 2: Operator mints a token** (manual; prototype `mos_browser_token.sh` or browser) and the implementer runs: `python scripts/capture_fixtures.py --token-file ~/.mos/token.txt --out tests/fixtures/` → six fixture files written and listings printed.

- [ ] **Step 3: Record findings in `config.example.yaml`**: FSCM family id; if the PeopleSoft application searches return nothing with release 27001300090200, retry without `release` and with `platform=2000`, and record whichever combination lists the `PEOPLESOFT RELEASE PATCHSET: HCM - 10282026` patch (number 40016683) in a YAML comment above the apps items; confirm the exact 19c abstracts for the October cycle (`DATABASE RELEASE UPDATE 19.29.0.0.0`-style vs date-style naming) and write the regexes into `params.abstract_regex` for `db_release_update`, `db_ojvm`, `db_jdk`.

- [ ] **Step 4: Verify fixtures parse**: `python -c "from opatchbot.aru import parse_search_xml; import glob; [print(f, len(parse_search_xml(open(f).read()))) for f in glob.glob('tests/fixtures/search_*.xml')]"` → each prints a count > 0.

- [ ] **Step 5: Commit**: `git add -A && git commit -m "chore: capture live ARU fixtures; pin FSCM id and DB abstract patterns"`.

---

### Task 4: Token store and browser token service

**Files:**
- Create: `opatchbot/token.py`
- Test: `tests/test_token.py` (unit), `tests/integration/test_token_live.py` (skipped unless `OPATCHBOT_LIVE=1`)

**Interfaces:**
- Consumes: `Secrets` from Task 1.
- Produces:
  - `decode_exp(jwt: str) -> datetime` (UTC), `extract_jwt(text: str) -> str | None` (longest 3-segment base64url match ≥ 60 chars per segment).
  - `class TokenStore(path: Path)` with `read() -> str | None`, `write(token: str) -> None` (0600), `expires_at() -> datetime | None`, `needs_refresh(min_remaining: timedelta = timedelta(minutes=10)) -> bool`.
  - `class BrowserSession(session_file: Path, headless: bool = True)` with `login(secrets: Secrets, code_prompt: Callable[[], str] | None) -> None` and `fetch_token() -> str` (raises `SessionExpired`), using Playwright Chromium with `storage_state=session_file` when present; `login` fills the login.oracle.com form, submits, and if a one-time-code field appears uses `pyotp.TOTP(secrets.totp_seed).now()` when the seed is set, else `code_prompt()`; on success saves `storage_state` to `session_file` (0600).
  - `class SessionExpired(Exception)`.
  - `make_token_provider(store: TokenStore, browser: BrowserSession) -> Callable[[], str]` returning a callable that refreshes via `browser.fetch_token()` when `store.needs_refresh()`.

- [ ] **Step 1: Write failing unit tests** in `tests/test_token.py` using a synthetic JWT built in the test (`header.payload.sig` with `{"exp": <now+3600>}` base64url):

```python
def test_decode_exp_and_needs_refresh(tmp_path):
    tok = make_jwt(exp=time.time() + 300); s = TokenStore(tmp_path / "t"); s.write(tok)
    assert (tmp_path / "t").stat().st_mode & 0o777 == 0o600
    assert s.needs_refresh() is True and s.needs_refresh(timedelta(minutes=1)) is False

def test_extract_jwt_from_dom():
    tok = make_jwt(exp=1); assert extract_jwt(f"<html><body><pre>{tok}</pre></body></html>") == tok
    assert extract_jwt("<html>Sign in to Oracle</html>") is None

def test_provider_refreshes(tmp_path):
    s = TokenStore(tmp_path / "t"); s.write(make_jwt(exp=time.time() + 60))
    fake = types.SimpleNamespace(fetch_token=lambda: make_jwt(exp=time.time() + 3600))
    assert decode_exp(make_token_provider(s, fake)()) > datetime.now(timezone.utc) + timedelta(minutes=50)
```

- [ ] **Step 2: Run to verify failure**: `pytest tests/test_token.py -q` → ImportError.

- [ ] **Step 3: Implement `opatchbot/token.py`** per Interfaces. `fetch_token` navigates to `https://updates.oracle.com/Orion/GenToken/get_token`, waits for network idle (max 30 s), reads `page.content()`; if `extract_jwt` finds nothing and the URL or content indicates `login.oracle.com`/`signon.oracle.com`/`identity.oraclecloud.com`, raise `SessionExpired`. `login` selectors are discovered live and kept as module constants with a comment naming the page they came from.

- [ ] **Step 4: Run unit tests**: `pytest tests/test_token.py -q` → 3 passed.

- [ ] **Step 5: Write the live integration test** `tests/integration/test_token_live.py`, skipped unless `OPATCHBOT_LIVE=1`: with `OPATCHBOT_SESSION_FILE` set, `BrowserSession(...).fetch_token()` returns a token whose `decode_exp` is 50–60 minutes ahead.

- [ ] **Step 6: Run `playwright install chromium`, then the live test once with a session the operator created via `opatchbot login`** (Task 10 CLI; until then, a 10-line `scripts/login_once.py` calling `BrowserSession.login`): `OPATCHBOT_LIVE=1 OPATCHBOT_SESSION_FILE=~/.mos/session.json pytest tests/integration -q` → 1 passed. Record the outcome in `docs/superpowers/plans/spike-log.md` with the date.

- [ ] **Step 7: Commit**: `git add -A && git commit -m "feat: token store and Playwright token service"`.

---

### Task 5: Manifest model and resolver selectors

**Files:**
- Create: `opatchbot/manifest.py`, `opatchbot/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: `Patch`, `PatchFile`, `AruClient`, `AuthError` (Task 2); `InventoryItem`, `Config` (Task 1).
- Produces:
  - `@dataclass ManifestEntry(item: str, area: str, status: str, patch: str | None, abstract: str | None, platform_id: int, released: datetime | None, reason: str, files: list[PatchFile], verified: list[str])` with `status` ∈ {`resolved`, `missing`, `not_expected`}.
  - `@dataclass Manifest(cycle_id: str, kind: str, scheduled: date, created: datetime, entries: list[ManifestEntry])` with `to_json(path)`, `from_json(path)`, `to_markdown() -> str`, `expected() -> list[ManifestEntry]`, `all_verified() -> bool`.
  - `@dataclass Window(start: date, end: date)`; `window_for(scheduled: date, cfg: Config) -> Window`.
  - `resolve_item(item: InventoryItem, client: AruClient, window: Window, kind: str) -> ManifestEntry`
  - `resolve_cycle(cycle_id: str, scheduled: date, cfg: Config, client: AruClient) -> Manifest`
  - Selector functions, each `(item, patches: list[Patch], window) -> tuple[Patch | None, str]` (choice, reason): `select_pt_product_dpk`, `select_pt_mw_dpk`, `select_app_release_patchset`, `select_db_abstract` (shared by RU/OJVM/JDK via `params.abstract_regex`), `select_opatch`.

- [ ] **Step 1: Write failing tests** in `tests/test_resolve.py` using fixtures from Tasks 2–3:

```python
def test_pt_product_dpk_picks_newest(fixtures_dir):
    patches = parse_search_xml((fixtures_dir/"search_pt861_233.xml").read_text())
    item = InventoryItem("pt-8.61","peopletools","pt_product_dpk","monthly",233,21917,600000000204773,{"file_prefix":"PEOPLETOOLS-WIN-8.61"})
    p, reason = select_pt_product_dpk(item, patches, Window(date(2026,10,14), date(2026,11,4)))
    assert p.number == "40014041" and "newest" in reason

def test_pt_product_dpk_outside_window_still_newest(fixtures_dir):
    ...same patches...; p, reason = select_pt_product_dpk(item, patches, Window(date(2026,12,1), date(2026,12,22)))
    assert p.number == "40014041" and "released before window" in reason

def test_pt_mw_dpk(fixtures_dir):
    p, _ = select_pt_mw_dpk(item_mw, patches, Window(date(2026,10,14), date(2026,11,4))); assert p.number == "40136983"

def test_tie_breaks_on_released_and_lists_runner_up():
    a = make_patch("1", "PT 8.61.24 PRODUCT PATCH DPK", released=dt(2026,10,19), files=["PEOPLETOOLS-WIN-8.61.24_1of4.zip"])
    b = make_patch("2", "PT 8.61.24 PRODUCT PATCH DPK", released=dt(2026,10,21), files=["PEOPLETOOLS-WIN-8.61.24_1of4.zip"])
    p, reason = select_pt_product_dpk(item, [a, b], Window(date(2026,10,14), date(2026,11,4)))
    assert p.number == "2" and "also matched: 1" in reason

def test_app_release_patchset(fixtures_dir):
    patches = parse_search_xml((fixtures_dir/"search_hcm_92_233.xml").read_text())
    p, _ = select_app_release_patchset(item_hcm, patches, Window(date(2026,10,14), date(2026,11,4)))
    assert p.number == "40016683"
    assert "UPDATE IMAGE" not in p.abstract

def test_quarterly_item_not_expected_in_cspu():
    e = resolve_item(item_db19_ru, client=None, window=Window(date(2026,11,10), date(2026,12,1)), kind="CSPU")
    assert e.status == "not_expected" and e.reason == "quarterly item; CSPU cycle"

def test_auth_error_propagates(fixtures_dir):
    client = FakeClient(raise_=AuthError("expired"))
    with pytest.raises(AuthError): resolve_item(item, client, Window(date(2026,10,14), date(2026,11,4)), "CPU")

def test_manifest_roundtrip_and_markdown(tmp_path):
    m = Manifest("2026-10-CPU", "CPU", date(2026,10,21), datetime.now(timezone.utc), [entry_resolved, entry_not_expected])
    m.to_json(tmp_path/"m.json"); m2 = Manifest.from_json(tmp_path/"m.json")
    assert m2.entries[0].patch == entry_resolved.patch and "| not expected |" in m2.to_markdown()
```

- [ ] **Step 2: Run to verify failure**: `pytest tests/test_resolve.py -q` → ImportError.

- [ ] **Step 3: Implement `manifest.py` and `resolve.py`** per Interfaces. Selection rules: `pt_product_dpk` filters patches with any file name starting with `params.file_prefix` and containing `_1of`; `pt_mw_dpk` and `db_*` filter by `re.search(params.abstract_regex, abstract)`; `app_release_patchset` filters `^PEOPLESOFT RELEASE PATCHSET: <app_code> - (\d{8})$` with the date inside the window; `opatch` uses `client.patch("6880880")` and keeps the files whose release name starts with the database major version. Window rule: for `pt_*` and `opatch`, pick newest overall and say "released before window" when outside; for others, only in-window candidates count, else `missing`. Tie-break by `released`, then `updated`; reason lists other candidate numbers as "also matched: …". `resolve_item` returns `not_expected` without calling the client when `item.cadence == "quarterly"` and `kind == "CSPU"`.

- [ ] **Step 4: Run tests**: `pytest tests/test_resolve.py -q` → 8 passed.

- [ ] **Step 5: Commit**: `git add -A && git commit -m "feat: manifest model and inventory selectors"`.

---

### Task 6: Cycle state store

**Files:**
- Create: `opatchbot/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces:
  - `@dataclass CycleRecord(cycle_id: str, kind: str, scheduled: date, status: str, advisory_url: str | None, released_seen: date | None, last_run: datetime | None, last_result: str | None, notified: list[str])`
  - `class StateStore(state_dir: Path)` with `load() -> dict[str, CycleRecord]`, `save(records)`, `upsert(record)`, `transition(cycle_id: str, new_status: str) -> CycleRecord` (raises `ValueError` on illegal transition), `cycle_dir(cycle_id) -> Path` (creates `state_dir/cycles/<cycle_id>/`).
  - Legal transitions: `scheduled→released`, `released→partial`, `released→fetched`, `partial→fetched`, `partial→failed`, `released→failed`, `failed→partial`, `failed→fetched`.

- [ ] **Step 1: Write failing tests**: `test_roundtrip_json`, `test_legal_and_illegal_transitions` (`scheduled→fetched` raises), `test_atomic_write_leaves_no_tmp` (save writes `cycles.json.tmp` then renames; no `.tmp` remains).

- [ ] **Step 2: Run to verify failure** → ImportError.

- [ ] **Step 3: Implement `opatchbot/state.py`** per Interfaces (JSON file `state_dir/cycles.json`, ISO dates).

- [ ] **Step 4: Run tests** → 3 passed.

- [ ] **Step 5: Commit**: `git commit -am "feat: cycle state store"` (after `git add -A`).

---

### Task 7: Fetcher and stager

**Files:**
- Create: `opatchbot/fetch.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `AruClient.download/head`, `AruError`, `AuthError` (Task 2); `Manifest`, `ManifestEntry` (Task 5); `Config` (Task 1).
- Produces:
  - `@dataclass FetchResult(cycle_id: str, verified: int, failed: list[tuple[str, str]], skipped: int, auth_failed: bool)`
  - `stage_path(cfg: Config, cycle_id: str, entry: ManifestEntry, file: PatchFile) -> Path`
  - `find_prior_copy(cfg: Config, cycle_id: str, file: PatchFile) -> Path | None` (same file name and sha256 recorded as verified in an earlier cycle's `manifest.json`)
  - `fetch_manifest(manifest: Manifest, cfg: Config, client: AruClient, parallel: int = 2) -> FetchResult` (writes `MANIFEST.md` and `manifest.json` into `<stage_root>/<cycle_id>/`, updating `entry.verified`).

- [ ] **Step 1: Write failing tests** in `tests/test_fetch.py` (mock client with `download` writing bytes to dest):

```python
def test_stage_layout(tmp_cfg):
    assert stage_path(tmp_cfg, "2026-10-CPU", entry_pt861, file1) == tmp_cfg.stage_root/"2026-10-CPU"/"peopletools"/"pt-8.61"/"PEOPLETOOLS-WIN-8.61.24_1of4.zip"

def test_hardlinks_prior_verified_copy(tmp_cfg):
    # prior cycle folder with verified file + manifest.json
    ...; r = fetch_manifest(m, tmp_cfg, client=FailingClient()); assert r.verified == 1 and new.stat().st_ino == old.stat().st_ino

def test_oversized_partial_restarts(tmp_cfg):
    dest.write_bytes(b"x" * 2000)  # expected size 1000
    r = fetch_manifest(m, tmp_cfg, client=RecordingClient(b"y" * 1000)); assert client.resume_flags == [False] and r.verified == 1

def test_auth_failure_stops_and_flags(tmp_cfg):
    r = fetch_manifest(m_two_files, tmp_cfg, client=AuthFailAfter(1)); assert r.auth_failed and r.verified == 1 and len(r.failed) == 1

def test_rerun_skips_verified(tmp_cfg):
    fetch_manifest(m, tmp_cfg, client=ok); r = fetch_manifest(m, tmp_cfg, client=FailingClient()); assert r.skipped == 1 and not r.failed
```

- [ ] **Step 2: Run to verify failure** → ImportError.

- [ ] **Step 3: Implement `opatchbot/fetch.py`** per Interfaces: `ThreadPoolExecutor(parallel)`; a file is skipped when `dest` exists, size matches and sha256 matches (or its name is in `entry.verified`); prior copies are hardlinked with `os.link` (fallback to copy across filesystems); `AuthError` cancels remaining submissions and sets `auth_failed`; manifest files written at the end regardless of outcome.

- [ ] **Step 4: Run tests** → 5 passed.

- [ ] **Step 5: Commit**: `git add -A && git commit -m "feat: resumable verified fetcher with hardlink reuse"`.

---

### Task 8: Release monitor

**Files:**
- Create: `opatchbot/monitor.py`, `tests/fixtures/security_alerts.html`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: `Config.cpu_months`, `cycle_kind` (Task 1); `StateStore`, `CycleRecord` (Task 6); `AruClient` (Task 2) for the fallback detector.
- Produces:
  - `@dataclass ScheduledCycle(cycle_id: str, kind: str, scheduled: date, advisory_url: str | None)`
  - `fetch_alerts_page(url: str = "https://www.oracle.com/security-alerts/") -> str`
  - `parse_alerts_page(html: str, today: date) -> list[ScheduledCycle]`
  - `detect(cycles: list[ScheduledCycle], store: StateStore, today: date) -> list[str]` (returns cycle ids newly transitioned to `released`; never transitions a cycle whose `scheduled > today`; marks `advisory_url` when present)
  - `late_cycles(store: StateStore, today: date, grace_days: int = 3) -> list[CycleRecord]`
  - `aru_fallback_released(client: AruClient, scheduled: date) -> bool` (true when a PeopleTools 8.61 Windows product DPK has `released >= scheduled`)

- [ ] **Step 1: Capture the fixture**: `curl -sL https://www.oracle.com/security-alerts/ -o tests/fixtures/security_alerts.html`; open it and note in a comment at the top of `tests/test_monitor.py` which table/anchor holds CPU dates and advisory links and which holds CSPU dates.

- [ ] **Step 2: Write failing tests**:

```python
def test_parse_alerts_fixture(fixtures_dir):
    cycles = parse_alerts_page((fixtures_dir/"security_alerts.html").read_text(), date(2026,11,9))
    ids = {c.cycle_id: c for c in cycles}
    assert ids["2026-10-CPU"].advisory_url and ids["2026-10-CPU"].scheduled == date(2026,10,21)  # adjust day from fixture
    assert ids["2026-11-CSPU"].kind == "CSPU"
    assert all(c.advisory_url is None for c in cycles if c.scheduled > date(2026,11,9))

def test_detect_does_not_release_future(tmp_state):
    future = ScheduledCycle("2027-01-CPU","CPU",date(2027,1,19),None)
    assert detect([future], tmp_state, date(2026,11,9)) == [] and tmp_state.load()["2027-01-CPU"].status == "scheduled"

def test_detect_releases_once(tmp_state):
    c = ScheduledCycle("2026-10-CPU","CPU",date(2026,10,21),"https://www.oracle.com/security-alerts/cpuoct2026.html")
    assert detect([c], tmp_state, date(2026,10,21)) == ["2026-10-CPU"] and detect([c], tmp_state, date(2026,10,22)) == []

def test_late_cycles(tmp_state):
    tmp_state.upsert(CycleRecord("2026-11-CSPU","CSPU",date(2026,11,17),"scheduled",None,None,None,None,[]))
    assert [c.cycle_id for c in late_cycles(tmp_state, date(2026,11,21))] == ["2026-11-CSPU"] and late_cycles(tmp_state, date(2026,11,19)) == []
```

- [ ] **Step 3: Run to verify failure** → ImportError.

- [ ] **Step 4: Implement `opatchbot/monitor.py`** per Interfaces with `html.parser`-based parsing (no new dependency); dates parsed from the page's text (e.g. "21 October 2026"); kind derived from the month via `cycle_kind`.

- [ ] **Step 5: Run tests** → 4 passed.

- [ ] **Step 6: Commit**: `git add -A && git commit -m "feat: release monitor with page parser and ARU fallback"`.

---

### Task 9: Notifications

**Files:**
- Create: `opatchbot/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: `NotifyConfig` (Task 1); `Manifest` (Task 5); `FetchResult` (Task 7); `CycleRecord` (Task 6).
- Produces:
  - `class Notifier(cfg: NotifyConfig)` with `send(subject: str, body: str) -> bool` (never raises; returns False and logs on failure; `mode == "stdout"` prints).
  - Message builders returning `(subject, body)`: `msg_first_pass(manifest, result)`, `msg_changed(manifest, result, newly_verified: list[str])`, `msg_window_closed(manifest, result)`, `msg_needs_human(cycle_id: str, command: str)`, `msg_late(cycle: CycleRecord)`, `msg_heartbeat(next_cycle: CycleRecord | None)`.
  - Subjects follow `[opatchbot] <cycle_id>: fetched | partial | needs human | no release detected | heartbeat`.

- [ ] **Step 1: Write failing tests**: `test_subjects` (each builder's subject prefix and keyword), `test_body_contains_manifest_table` (`msg_first_pass` body includes `manifest.to_markdown()` and each failed file with its error), `test_needs_human_names_command` (`"opatchbot login"` in body), `test_send_failure_returns_false` (monkeypatch `smtplib.SMTP` to raise `OSError`; `send` returns False and no exception).

- [ ] **Step 2: Run to verify failure** → ImportError.

- [ ] **Step 3: Implement `opatchbot/notify.py`** (plain-text `email.message.EmailMessage`, `smtplib.SMTP(host, port, timeout=30)`, no auth).

- [ ] **Step 4: Run tests** → 4 passed.

- [ ] **Step 5: Commit**: `git add -A && git commit -m "feat: email notifications"`.

---

### Task 10: CLI and orchestration

**Files:**
- Create: `opatchbot/cli.py`, `opatchbot/run.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `main(argv: list[str] | None = None) -> int` with subcommands: `login`, `token`, `status`, `monitor`, `resolve <cycle_id>`, `fetch <cycle_id>`, `run <cycle_id>` (= resolve + fetch + notify), `heartbeat`, `prune [--yes]`. Global options `--config PATH` (default `/etc/opatchbot/config.yaml`), `--secrets PATH` (default `/etc/opatchbot/secrets.env`), `--notify stdout`.
  - `run_cycle(cycle_id: str, cfg: Config, secrets: Secrets, store: StateStore, client: AruClient, notifier: Notifier, today: date) -> int` in `run.py`: resolves, fetches, transitions state (`released→partial|fetched`, `partial→fetched`, `failed` only when the window has closed with unverified expected files), sends exactly one of `first_pass`/`changed`/`window_closed`/`needs_human` per invocation (`changed` only if the verified set grew), returns exit code 0 ok / 1 partial / 2 needs human / 3 failed.
  - `monitor_once(cfg, secrets, store, client, notifier, today) -> int`: parse page (fallback to ARU detector on parse failure), `detect`, run newly released cycles and all `released`/`partial`/`failed` cycles still inside their window, email `msg_late` for late cycles (once per cycle, tracked in `notified`).
  - Exit codes: `token` 0 ok / 2 session expired; `login` 0 / 1.

- [ ] **Step 1: Write failing tests** in `tests/test_cli.py` with fake client/browser injected via a `build_services` seam and `--notify stdout`:
  `test_run_first_pass_partial_then_fetched` (two invocations: first with one file failing → exit 1, state `partial`, one email containing "partial"; second with success → exit 0, state `fetched`, email contains "fetched"),
  `test_run_needs_human` (client raises `AuthError`, browser raises `SessionExpired` → exit 2, email contains "opatchbot login", state unchanged),
  `test_monitor_quiet_day` (no new cycles, nothing in window → exit 0, no email),
  `test_monitor_release_day_runs_once` (fixture page with released cycle → `run_cycle` invoked once; second call same day → not invoked),
  `test_prune_keeps_n` (5 cycle dirs, `keep_cycles=3`, `prune --yes` removes the 2 oldest and never the ones in state `partial`),
  `test_status_prints_cycles`.

- [ ] **Step 2: Run to verify failure** → ImportError.

- [ ] **Step 3: Implement `cli.py` and `run.py`** per Interfaces (`argparse`; logging to `state_dir/log/opatchbot.log` with `RotatingFileHandler(5 MB × 5)` plus stderr at WARNING; `login` prompts for the code with `input("Code from authenticator app: ")` only when no TOTP seed).

- [ ] **Step 4: Run the whole suite**: `pytest -q` → all passed.

- [ ] **Step 5: Commit**: `git add -A && git commit -m "feat: opatchbot CLI and orchestration"`.

---

### Task 11: Deployment assets and README

**Files:**
- Create: `deploy/opatchbot.service`, `deploy/opatchbot.timer`, `deploy/cron.example`, `deploy/install_ol.sh`, `README.md`
- Test: `tests/test_deploy_files.py`

**Interfaces:**
- Consumes: CLI from Task 10.

- [ ] **Step 1: Write failing test** `test_deploy_files_reference_real_commands`: every `opatchbot <sub>` token in `deploy/*` and `README.md` is a subcommand `cli.main(["--help"])` lists; `opatchbot.timer` contains `OnCalendar=*-*-* 07:00:00`; `cron.example` has one daily `opatchbot monitor` line and one monthly `opatchbot heartbeat` line on day 1.

- [ ] **Step 2: Run to verify failure** → files missing.

- [ ] **Step 3: Write the files.** `install_ol.sh`: creates user `opatchbot`, `/etc/opatchbot`, `/var/lib/opatchbot`, `/stage/oracle-patches`; installs Python 3.11 (`dnf module enable python311` on OL8, `dnf install python3.11` on OL9), venv under `/opt/opatchbot`, `pip install .`, `playwright install chromium` and `playwright install-deps || dnf install -y <RHEL chromium library list from Playwright's docs>`; prints the next steps. `README.md`: 10-minute setup (secrets file 0600, `opatchbot login`, `opatchbot token`, `opatchbot resolve 2026-10-CPU`, `opatchbot run 2026-10-CPU`, enable timer), operations (what each email means, how to re-authenticate, `status`, `prune`), the Mac-login alternative (`opatchbot login` on the Mac, `scp session.json`), and the Ansible follow-on pointer.

- [ ] **Step 4: Run tests** → passed.

- [ ] **Step 5: Commit**: `git add -A && git commit -m "docs: deployment assets and README"`.

---

### Task 12: Acceptance run against the October 2026 cycle and the Phase 0 session spike

**Files:**
- Create: `docs/superpowers/plans/spike-log.md` (if not created in Task 4), `docs/superpowers/plans/acceptance-2026-10-CPU.md`

- [ ] **Step 1: Operator step (manual)**: on the Mac, `opatchbot login --config config.example.yaml --secrets ~/.mos/secrets.env` (code typed from the authenticator app) → `session.json` created.

- [ ] **Step 2: Resolve the known cycle**: `opatchbot --config <local cfg with stage_root=~/Downloads/opatchbot-stage> resolve 2026-10-CPU` → `MANIFEST.md` lists 40014041 and 40136983 for 8.61, the 8.62 equivalents, 40016683 (HCM), the CS and FSCM release patchsets dated 10-28-2026, and the 19c RU/OJVM/JDK/OPatch for Linux x86-64, each with a reason; 21c rows resolved or "no release this cycle". Paste the table into `acceptance-2026-10-CPU.md`.

- [ ] **Step 3: Fetch with `--notify stdout`**: `opatchbot run 2026-10-CPU --notify stdout` → exit 0 and every file `verified`; re-run → `skipped` equals the file count and no downloads. Record sizes and wall time.

- [ ] **Step 4: Spike**: schedule `opatchbot token` daily for 7 days on the Mac (`launchd` or a cron line) appending `date, exit code, expiry` to `spike-log.md`. Decision rule written at the top of the log: 7/7 successes → goal 5 holds as designed; any `session expired` within 7 days → enable the "release day tomorrow" reminder email path (add `msg_reminder` to Task 9 and a `monitor` check for `scheduled == today + 1`) before go-live.

- [ ] **Step 5: Commit the logs**: `git add -A && git commit -m "docs: acceptance run and session spike log"`.
