# Oracle CPU/CSPU patch automation — design

Date: 2026-11-09
Owner: Dave Whisman (Butler University). Author of this document: Claude, from a design conversation.

## Intent

Every month Oracle ships security patches: a quarterly Critical Patch Update (CPU) in January,
April, July and October, and a monthly Critical Security Patch Update (CSPU) in the other eight
months. Oracle publishes the dates, but the patches for all products land on that date and the
team currently learns of the release by email, then hunts through My Oracle Support (MOS) by hand
to find the right patches among many similar ones, clicks each download in a browser on a Windows
box, and copies the files to the target servers.

The goal is a system on a Linux staging server that, with no human in the loop except an
occasional re-authentication, notices each CPU/CSPU release, works out exactly which patches this
site needs, downloads and verifies them, stages them in a per-cycle folder, and tells the team
what it fetched and what it could not.

Today's exploratory work (on a Mac) already proved the hard parts: the patch search and download
API works with a one-hour OAuth token, and the token can be obtained from a browser session that
has completed MFA once. See "Prior work" below.

## Goals

1. Detect each CPU/CSPU release within a day of it appearing, from Oracle's public
   security-alerts page, not from email.
2. Produce a **manifest** per cycle: the exact patch numbers, descriptions, platforms, files,
   sizes and SHA-256 digests this site needs, written out plainly before and after download.
3. Download every manifest file over the patch API with resume and SHA-256 verification into
   `<stage>/<YYYY-MM>-<CPU|CSPU>/<product>/`.
4. Run unattended on the Linux staging server from a scheduler, with clear logs and a
   notification (email) summarizing success, partial failure, or "needs human: re-authenticate".
5. Re-authentication is rare (when Oracle expires the browser session), takes minutes, and is
   done from a terminal over SSH.

## Non-goals

- Installing or applying patches. Staging is the end of the pipeline.
- Copying files onward to database/application hosts (the team's existing copy/SCP step stays).
- Supporting Oracle products outside the inventory below.
- A web UI.

## Anti-goals (would make it a failure even if it "works")

- Silently downloading the wrong patch (e.g. a PUM image instead of the security patchset) with
  no way to tell. The manifest must make every choice visible and explain how it was chosen.
- Storing the MOS password or second factor in a way that another person with shell access can
  trivially read. Secrets live in a file readable only by the service user, or a vault; never in
  scripts, logs, or process arguments.
- Leaving the operator guessing whether the job is healthy. Every run ends with an explicit
  status, and a silent month is itself reported.
- Hammering Oracle. One poll per day during a release window, one download run per cycle.

## Inventory (what "ours" means)

| Area | Product / release | Platform |
|---|---|---|
| PeopleTools | 8.61 and 8.62 | Microsoft Windows x64 (ARU platform 233) |
| PeopleTools middleware | PT-MW-DPK for 8.61 and 8.62 | Windows x64 (233) |
| PeopleSoft applications | HCM 9.2, FSCM (Financials) 9.2, Campus Solutions 9.2 | Windows x64 (233) |
| Database | Oracle Database 19c and 21c | Linux x86-64 (ARU platform 226) |

Inventory is configuration, not code: a single YAML/JSON file the operator edits.

## Constraints and facts established today

- **Portal login does not authorize downloads.** support.oracle.com accepts the tenancy's Oracle
  Cloud identity, but files are served by `updates.oracle.com`, which requires either a classic
  `login.oracle.com` browser session (MFA by SMS today) or an OAuth2 bearer token.
- **Token facts.** Issued by Oracle's identity domain `login-ext.identity.oraclecloud.com`
  (client `UPDATES_PROD`, scope `search metadata conflict_checks get_inventory_upi
  conflict_resolution patch_download`, audience `updates.`). Lifetime **60 minutes**. A token
  cannot mint another token (`GenToken/get_token` returns 401 to a bearer). The OAuth2
  password grant returns `invalid_request` for this client (confidential client; secret not
  available), so tokens must come from a signed-in browser session at
  `https://updates.oracle.com/Orion/GenToken/get_token`.
- **Patch API (ARU) works with the token.** Verified endpoints, all under
  `https://updates.oracle.com/Orion/Services/`:
  - `metadata?table=aru_products|aru_releases|aru_platforms`
  - `search?product=<id>&release=<id>&platform=<id>` → list of patches with files
  - `search?bug=<patch>` → one patch with files, sizes, `download_url`, `<digest type="SHA-256">`
  - `download/<file>?aru=...&patch_file=...` (HEAD and GET; resumable)
  Known ids: product 21917 PeopleSoft PeopleTools (family), 21918 PeopleSoft Enterprise PT
  PeopleTools; release 600000000204773 = PeopleTools 8.61 (8.62 and 8.63 also present);
  platform 233 Windows x64, 226 Linux x86-64, 212 AIX.
  These endpoints are undocumented by Oracle (used by Enterprise Manager and AutoUpgrade) and
  may change; the design isolates them behind one module.
- **Release announcement page.** `https://www.oracle.com/security-alerts/` with anchors
  `#CriticalPatchUpdates` and `#CriticalSecurityPatchUpdates`. It lists scheduled dates and,
  once released, links the advisory for each cycle.
- **Per-cycle availability documents** exist on MOS (knowledge articles, e.g. the Database CPU
  availability note the operator linked, `documentId=CPU345`). They require the portal session,
  not the bearer token, so they are a verification aid for humans, not a machine source.
- **Staging host** is a Linux server (distro and tooling to be confirmed); outbound HTTPS to
  `updates.oracle.com`, `login.oracle.com`, `login-ext.identity.oraclecloud.com`,
  `www.oracle.com` required. Mac is exploratory only.
- **MFA factor** is SMS today; the operator is checking whether an authenticator app (TOTP) can
  be enrolled instead.

## Prior work to build on (Mac prototype, `~/mocs/oracle-patch/`)

- `mos_patch_sync.sh` — resolves newest "PT 8.61.xx PRODUCT PATCH DPK" for a platform via ARU,
  downloads with resume, verifies SHA-256. Proven: downloaded and verified patch 40014041
  (4 files, 6.5 GB).
- `mos_token_probe.sh` — reads a token file, prints expiry and endpoint status codes only.
- `mos_browser_token.sh` — headless Edge against a dedicated profile to fetch a token (written,
  not yet exercised).
- `mos_mint_token.sh` — password-grant attempt; documented failure (`invalid_request`).
- `README.md` — the findings above in detail.
The Linux build should port the logic, not the shell scripts, into one small Python package.

## Architecture (decided)

Language and runtime: **Python 3.10+** on the Linux staging server, standard library plus
`requests` and `playwright` (bundled Chromium) — chosen by Claude for portability and because
Playwright gives a scriptable login with a persistent storage-state file. No other services.

Four components, one CLI (`opatchbot <command>`), one config file, one state directory.

### 1. Release monitor (`opatchbot monitor`)

- Runs daily (cron/systemd timer, e.g. 07:00).
- Fetches the security-alerts page; parses the CPU and CSPU schedule tables into
  `(cycle_id, scheduled_date, advisory_url_or_none)`. `cycle_id` is `YYYY-MM-CPU` or
  `YYYY-MM-CSPU`.
- A cycle is **released** when its advisory link is present (or, if the page structure makes
  that unreliable, when the scheduled date is ≤ today and the advisory page returns 200).
- State: `state/cycles.json` records each cycle's status: `scheduled`, `released`, `fetched`,
  `failed`. On transition to `released`, the monitor invokes the fetch pipeline once.
- If a cycle's scheduled date has passed by more than N days (default 3) and no advisory is
  seen, it notifies "release expected but not detected" so a page-format change is noticed.
- Builder choice: if Oracle's page proves unparseable, fall back to the ARU feed: a daily
  product search for PeopleTools 8.61 and a check for a new `PRODUCT PATCH DPK` is an
  acceptable detector, because all products release the same day.

### 2. Token service (`opatchbot login`, `opatchbot token`)

- `login`: Playwright drives `https://updates.oracle.com/Orion/GenToken/get_token` headlessly,
  follows to login.oracle.com, fills username/password from the secrets file, submits, then
  handles the second factor:
  - SMS: prints "enter the code Oracle texted you", reads it from the terminal, submits.
  - TOTP (if enrolled): generates the code from the stored seed (`pyotp`), submits.
  On success saves Playwright `storage_state` to `state/session.json` (mode 600).
  Must be runnable over SSH with no display.
- `token`: Playwright headless with `storage_state=session.json` loads the token page and
  extracts the JWT; writes `state/token.txt` (600); prints expiry only. If redirected to a login
  page → exit 2 "session expired" (and, with TOTP, automatically re-runs `login`).
- Tokens are fetched at the start of a fetch run and refreshed when < 10 minutes remain, since
  a full cycle (≈30–40 GB) exceeds one hour.

### 3. Manifest resolver (`opatchbot resolve <cycle_id>`)

Builds `state/<cycle_id>/manifest.json` from the inventory using ARU searches. Rules
(decided; builder adjusts ids/regexes from live data):

| Inventory item | ARU query | Selection rule |
|---|---|---|
| PeopleTools 8.61 / 8.62 product patch | product 21917, release of that version, platform 233 | newest patch whose files match `PEOPLETOOLS-WIN-<ver>.*_1of` released within the cycle window |
| PT middleware DPK | same search | newest patch whose primary bug abstract matches `^PT-MW-DPK` within the window |
| HCM / FSCM / CS 9.2 security patchset | product id for each application, release 9.2, platform 233 (ids discovered from `aru_products`/`aru_releases`) | patch whose abstract matches `^PEOPLESOFT RELEASE PATCHSET: (HCM|FSCM|CS) - MMDDYYYY` with date in the cycle window. **Not** `UPDATE IMAGE` patches. |
| Database 19c / 21c | product "Oracle Database", release `19.0.0.0.0` / `21.0.0.0.0`, platform 226 | within the window: `DATABASE RELEASE UPDATE <ver>`, `OJVM RELEASE UPDATE <ver>`, and `JDK BUNDLE PATCH`; for 21c, absent entries are reported as "no release this cycle", not as failure |
| OPatch for 19c / 21c | patch 6880880 (`search?bug=6880880`), release matching the database version, platform 226 | newest file set for that release (OPatch is not cycle-dated; always take the current one, hardlink if unchanged) |

- "Cycle window" = scheduled date −7 days to +14 days (configurable).
- Each manifest entry records: inventory item, patch number, abstract, platform, release date,
  files with sizes and SHA-256, and a `reason` string ("newest PT 8.61 product DPK, released
  2026-10-19"). Items that resolve to nothing are recorded as `missing` with the query used.
- `resolve` is idempotent and safe to run by hand for review; the notification includes the
  manifest in plain text.

### 4. Fetcher and stager (`opatchbot fetch <cycle_id>`)

- For each manifest file: download to `<stage>/<cycle_id>/<area>/<product>/<file>` with resume
  (`Range`), retry, bearer header; verify SHA-256; mark `verified` in the manifest. Parallelism
  2–3 files; respects a bandwidth cap option.
- Re-runs skip verified files; partial files resume. 401/403 → refresh token once, then stop
  with `auth_failed` and notify.
- Writes `<stage>/<cycle_id>/MANIFEST.md` (human table) and `manifest.json`; updates
  `state/cycles.json` to `fetched` or `failed`.
- Optional post-step hook (shell command from config) for the team's own copy/SCP, off by default.

### Scheduling and operations

- One cron entry: `opatchbot monitor` daily. `monitor` calls `resolve` + `fetch` when a cycle
  flips to released; a second cron entry retries `fetch` for cycles in `failed` state daily for
  up to 14 days.
- Notifications: SMTP (config) with subject `[opatchbot] <cycle_id>: fetched|partial|needs
  human|no release detected`. Body = manifest table + errors + next steps. Builder may add a
  `--notify stdout` mode for testing.
- Logs: `state/log/opatchbot.log`, rotating; no secrets, no tokens.
- Secrets: `secrets.env` (600) with `MOS_USER`, `MOS_PASS`, optional `MOS_TOTP_SEED`;
  session/token files 600 in `state/`. Service runs as a dedicated user.
- Health: `opatchbot status` prints cycles, session expiry if known, last run results.

## Calls made by Claude after the builder review (operator may override)

1. **Validation spike first.** The unattended premise rests on a saved browser session still
   yielding tokens days later without MFA, which is unproven. Phase 0 is a spike: sign in once
   into the dedicated profile, then fetch a token headlessly each day for 7+ days and log the
   outcome. Goal 5 ("re-auth is rare") is a requirement only if the spike shows multi-week
   sessions; otherwise the design falls back to one SMS per cycle, triggered by a
   "release day tomorrow, please run `opatchbot login`" email the evening before.
2. **Cadence per inventory item.** Each inventory row gets `cadence: quarterly | monthly`.
   Database RU/OJVM/JDK are `quarterly`; PeopleTools, middleware and application patchsets are
   `monthly` (Claude's default from Oracle's CPU/CSPU program; operator confirms). An item that
   is not expected in a cycle is reported as `not expected`, never `missing`.
3. **Staggered arrivals.** Patches within a cycle may land on different days. After release
   detection, `resolve` + `fetch` re-run daily until the window closes (+14 days) or everything
   expected is verified. Emails: one on the first pass, one on each day something changed, one
   at window close. Cycle states: `scheduled → released → partial → fetched | failed`.
4. **PeopleTools outside the window.** The manifest always lists the newest available
   PeopleTools product DPK and middleware DPK with reason "newest available, released <date>".
   If that exact patch is already verified in an earlier cycle folder, the fetcher hardlinks it
   into the new cycle instead of downloading again.
5. **Retention.** `keep_cycles` (default 3) and an `opatchbot prune` command that removes older
   cycle folders; never runs automatically unless enabled.
6. **Heartbeat.** A monthly "alive; next expected cycle <id> on <date>" email on the 1st, so a
   dead host or cron is noticed within a month.

## Operator answers (2026-11-09)

- **Database scope:** no Grid Infrastructure or combo patches. Per database release (19c, 21c)
  stage the Release Update, OJVM Release Update, JDK bundle patch, **and the OPatch utility
  update (patch 6880880 for that release)**. All `quarterly`. No Monthly Recommended Patches.
- **Staging server:** Oracle Linux 8 or 9, outbound HTTPS available. Python 3 assumed present
  (OL8 ships 3.6 — the builder installs a 3.11 app stream or a `uv`/pyenv Python if needed).
  Playwright's bundled Chromium is not officially supported on RHEL-family: the builder first
  tries `playwright install chromium` plus the dnf dependency set (or EPEL `chromium` via
  `executable_path`); if that is not clean, `opatchbot login` runs on the operator's Mac and the
  resulting `session.json` (portable JSON) is copied to the server. `token`/`fetch` always run on
  the server. Whether Claude Code can work on the server directly: not stated; the deliverable
  is an installable package plus README either way.
- **Notifications:** SMTP relay managed by Proofpoint, unauthenticated from campus hosts
  (hostname to be provided); recipient `dawhisma@butler.edu`.
- **MOS identity:** the operator's own account, `dawhisma@butler.edu`. Known consequence: campus
  password rotation or the operator being unreachable for an SMS breaks the job until
  `opatchbot login` is re-run; the "needs human" email goes to that same address. A shared or
  service MOS account is a later improvement, not a blocker.
- **MFA factor (updated 2026-11-09, later):** the operator switched the Oracle account's second
  factor from SMS to a mobile authenticator app; login now requires a code from the app.
  `opatchbot login` therefore prompts for a 6-digit code typed from the app (interactive mode).
  Fully unattended mode requires the TOTP seed, which Oracle shows only at enrollment (QR code or
  text key). If the seed was not captured, the operator re-enrolls once to capture it and places
  it in `secrets.env` as `MOS_TOTP_SEED`; the builder verifies the app's codes are standard TOTP
  (30 s, SHA-1, 6 digits) by comparing `pyotp` output against the app before relying on it.
  Design status: **approved by the operator on 2026-11-09.**
- **Stage root:** not stated. Claude's call: `/stage/oracle-patches` (config key `stage_root`).
- **Ansible:** the team has an Ansible server. Out of scope for this build, recorded as the
  natural follow-on: (a) an Ansible role that installs/updates `opatchbot` and its cron/timer on
  the staging server; (b) a playbook that distributes a cycle's verified files from the stage
  folder to the database and application hosts (today's manual copy/SCP); (c) optionally
  scheduling via AWX instead of cron. The core tool stays standalone so it works without Ansible.

## What's left to the builder

- Discover ARU product/release ids for HCM, FSCM, CS 9.2, PeopleTools 8.62, Database 19c/21c
  from the metadata tables and pin them in config with their names for review.
- Confirm the exact abstract patterns against two past cycles (September CSPU, October CPU) and
  write them as unit-tested selectors with fixtures captured from live XML.
- Decide the security-alerts page parsing approach after fetching the page; keep the ARU-based
  detector as fallback.
- Playwright login flow details (selectors on login.oracle.com, SMS form), with a clear failure
  message if the page changes; keep the manual browser fallback documented.
- Packaging: single-directory install (`pipx` or a venv), `systemd` timer or cron, README with
  the 10-minute setup.

## Acceptance

1. On a server with no prior session, `opatchbot login` completes with one SMS (or none with
   TOTP) and `opatchbot token` then prints a valid expiry.
2. `opatchbot resolve 2026-10-CPU` produces a manifest listing, for this inventory, the known
   October patches including 40014041 and 40136983, the HCM/FSCM/CS release patchsets dated
   10-28-2026, and the 19c RU/OJVM/JDK patches for Linux x86-64, each with a reason.
3. `opatchbot fetch 2026-10-CPU` downloads and verifies all files into the stage folder; a
   re-run does no network work beyond the HEAD checks and token fetch.
4. `opatchbot monitor` on a day with no new cycle exits quietly; on the November CSPU release
   day it detects the release, fetches, and sends one email.
5. With `session.json` deleted, a run ends with status `needs human` and an email saying
   exactly which command to run.
