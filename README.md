# opatchbot — Oracle CPU/CSPU patch monitor and stager

`opatchbot` watches Oracle's security-alerts page for each Critical Patch Update (CPU, quarterly) and
Critical Security Patch Update (CSPU, monthly), works out exactly which patches this site needs
(PeopleTools, PeopleTools middleware, PeopleSoft HCM/FSCM/CS, Oracle Database 19c/21c), downloads
them from My Oracle Support with SHA-256 verification into a per-cycle stage folder, and emails
the result. With `MOS_TOTP_SEED` set it signs in to Oracle by itself whenever needed; a human is needed only
when the Oracle password or authenticator enrollment changes (the run then emails "needs human").

Design: `docs/superpowers/specs/2026-11-09-oracle-cpu-patch-automation-design.md`.
Prototype scripts and the authentication findings that led here: `prototype/README.md`.

## How it works

| step | command | what happens |
|---|---|---|
| sign in | `opatchbot login` | Headless Chromium signs in to login.oracle.com with `MOS_USER`/`MOS_PASS` and the authenticator code (generated from `MOS_TOTP_SEED`, or typed). Saves the session to `secrets_dir/session.json`. Run once at setup; the weekly `opatchbot-login.timer` repeats it so the session never goes stale, and with `auto_login: true` any command that needs a token signs in again by itself (once per run) if Oracle has expired the session. |
| mint a token | `opatchbot token` | Uses the saved session to open Oracle's token page and store a 1-hour download token. Runs automatically whenever a token is needed. |
| daily | `opatchbot monitor` | Parses https://www.oracle.com/security-alerts/, marks cycles released, runs due cycles once per day each until their window closes (even after everything visible is fetched, because Oracle adds CPU-dated patchsets for up to two weeks), emails late/needs-human notices. |
| one cycle | `opatchbot run 2026-10-CPU [--type DB,MW,PT,AP]` | Resolve the manifest from Oracle's ARU patch services, download with resume, verify, hardlink files already staged in earlier cycles, write `MANIFEST.md`, email. With `--type`, only those patch types (see below). |
| what to search | `opatchbot list`, `add`, `del`, `discover` | Show and maintain the inventory (software versions) in `config.yaml`; Oracle ids are looked up once at `add` time and pinned. |
| look | `opatchbot status` | Cycle table and token expiry. `opatchbot resolve <cycle> [--type ...]` searches and prints the manifest without downloading or changing state (writes `manifest.review.json`/`MANIFEST.review.md` only). |
| housekeeping | `opatchbot prune --yes`, `opatchbot heartbeat` | Remove cycle folders beyond `keep_cycles` (never partial ones); monthly "alive" email. |

Stage layout: `<stage_root>/<YYYY-MM-CPU|CSPU>/<area>/<item>/<file>` with `MANIFEST.md` and `manifest.json` per cycle.

## Server layout (Oracle Linux 8/9)

| what | where |
|---|---|
| application home: venv, `config.yaml`, `state/` (cycles.json, `log/opatchbot.log`), `src/` checkout | `/opt/app/patch` |
| downloaded patches (`stage_root`) | `/opt/data2/download/patch/<YYYY-MM-CPU\|CSPU>/<area>/<item>/` |
| runs as | `oracle` (home `/home/oracle`) |
| `secrets.env`, `session.json`, `token.txt` (`secrets_dir`, dir 0700, files 0600) | `/home/oracle/.mos` |
| headless Chromium (no desktop, no display) | `/home/oracle/.cache/ms-playwright` |
| command | `/usr/local/bin/opatchbot`, a wrapper that clears `LD_LIBRARY_PATH` and runs `/opt/app/patch/venv/bin/opatchbot` |

`opatchbot` reads `/opt/app/patch/config.yaml` and `/home/oracle/.mos/secrets.env` by default (override with
`--config`/`--secrets` or `OPATCHBOT_CONFIG`/`OPATCHBOT_SECRETS`). It refuses to run as root, so every file it
writes stays owned by `oracle`.

## 10-minute setup

```bash
sudo -u oracle git clone <this repo> /opt/app/patch/src
sudo /opt/app/patch/src/deploy/install_ol.sh   # dirs, venv, Chromium + libraries, templates, timers
sudo -u oracle vi /opt/app/patch/config.yaml   # notify: smtp_host, recipients
sudo -u oracle vi /home/oracle/.mos/secrets.env   # MOS_USER, MOS_PASS, MOS_TOTP_SEED ; keep 0600
sudo -u oracle opatchbot login                 # unattended with MOS_TOTP_SEED; prints the token expiry
sudo -u oracle opatchbot list                  # what will be searched
sudo -u oracle opatchbot resolve 2026-10-CPU   # search only, nothing downloaded
sudo -u oracle opatchbot run 2026-10-CPU --notify stdout
sudo systemctl enable --now opatchbot.timer opatchbot-heartbeat.timer opatchbot-login.timer
```

Upgrade: `cd /opt/app/patch/src && sudo -u oracle git pull && sudo deploy/install_ol.sh`.

Capturing the authenticator seed: during enrollment choose "Can't scan? / enter key manually" and store that
text key as `MOS_TOTP_SEED`. Verify once that `/opt/app/patch/venv/bin/python -c "import pyotp;print(pyotp.TOTP('<seed>').now())"`
matches the phone app.

## Choosing what to download

### Patch types (`--type`)

| code | area | items |
|---|---|---|
| `DB` | database | Release Update, OJVM Release Update, JDK bundle patch, OPatch, per database version |
| `MW` | middleware | PT-MW-DPK (WebLogic, Tuxedo, JDK bundle for PeopleSoft), per PeopleTools version |
| `PT` | peopletools | PeopleTools product patch DPK, per PeopleTools version |
| `AP` | apps | PeopleSoft HCM / FSCM release patchsets; CS update image (opt-in) |

No `--type` means everything. `--item pt-8.61` (names from `opatchbot list`, comma separated) narrows a
`resolve`/`run` to single items, e.g. `opatchbot run 2026-10-CPU --item pt-8.61`. `opatchbot resolve 2026-10-CPU --type DB` searches database patches only;
`opatchbot run 2026-10-CPU --type PT,MW` downloads only PeopleTools and middleware. A filtered run updates only
those items in the cycle's `manifest.json`, prints its summary instead of emailing, and leaves the cycle status and
the daily monitor alone, so it never makes a cycle look complete. The daily `monitor` always covers every type.

### Software versions (`add`, `del`, `list`, `discover`)

The inventory in `config.yaml` lists every software version to patch, with Oracle's product, release and platform
ids pinned. The ids are looked up once, when a version is added, so every `monitor` and `run` searches for
exactly the same software.

```bash
opatchbot list                      # what is configured
opatchbot add DB 26ai               # database: adds ru, ojvm, jdk, opatch (--components ru,jdk for fewer)
opatchbot add PT 8.63               # PeopleTools product DPK
opatchbot add MW 8.63               # middleware DPK for that PeopleTools version
opatchbot add AP HCM 9.2            # application release patchsets (HCM, FSCM; CS = update image)
opatchbot del DB 19                 # after the 19c upgrade: removes db19-ru, -ojvm, -jdk, -opatch
opatchbot del db21-ojvm             # remove a single item
opatchbot discover database 23      # search Oracle's release/product names (--table platforms|all)
```

`add` looks up the release and platform ids (database on Linux x86-64, PeopleSoft on Windows x64 unless
`--platform` says otherwise), then runs each new item's real search and shows what it would download today,
e.g. `db26ai-ru -> patch 3xxxxxxx: DATABASE RELEASE UPDATE 23.26...`. Nothing is written until you confirm.
If several Oracle releases match, they are listed and you pick one with `--release-id`; nothing is guessed.
Oracle AI Database 26ai is version 23.26, so `add DB 26ai` tries release "26" and then "23". If an item's search
finds nothing now, it is flagged; with `--yes` (scripts) that refuses unless `--force` is added.

`add`/`del` change only the inventory lines involved (every comment is kept), save a dated backup
(`config.yaml.bak-YYYYmmdd-HHMMSS`), check the result loads, and replace the file atomically. Hand edits work too,
as long as each inventory item stays on one line.

## Testing email

```bash
opatchbot test-email                 # to notify.recipients
opatchbot test-email --to someone@butler.edu
```

Sends one message and prints each step: DNS lookup of `smtp_host`, TCP connect to `smtp_port`, the relay's greeting,
`MAIL FROM`, each `RCPT TO`, and `DATA`, with the relay's reply codes. Exit 0 when the relay accepted the message,
6 otherwise. A failure at DNS means a wrong `smtp_host`; at connect, a firewall; at `RCPT TO` (e.g. 550 relaying
denied), the relay does not accept mail from this host or sender.

## What each email means

| subject | meaning | action |
|---|---|---|
| `[opatchbot] <cycle>: fetched` | every expected file verified | copy from the stage folder |
| `[opatchbot] <cycle>: partial` | some items missing or failed; retried daily until the window closes | read the manifest table; usually nothing |
| `[opatchbot] <cycle>: ... (window closed)` | final state for the cycle | handle leftovers by hand |
| `[opatchbot] <cycle>: needs human` | Oracle session expired and unattended re-login was off or failed (e.g. password changed) | fix `secrets.env`, run `opatchbot login` on the server |
| `[opatchbot] <cycle>: no release detected` | scheduled date passed with no advisory | check the security-alerts page / page format |
| `[opatchbot] heartbeat` | monthly alive signal | none |

## Inventory and selectors

Edit `inventory:` in `config.yaml`. Each item has an `area`, a `selector`, a `cadence` (`monthly` or
`quarterly`), ARU `product_id` / `release_id` / `platform_id`, and `params`. Discovery notes and the ids
in use are commented in `config.example.yaml`. Selectors:

* `pt_product_dpk` — newest `PEOPLETOOLS-WIN-<ver>...` DPK (always the newest, even if older than the window)
* `pt_mw_dpk` — newest `PT-MW-DPK`
* `app_release_patchset` — newest `PEOPLESOFT RELEASE PATCHSET: <APP> - MMDDYYYY` released inside the window (these ship weekly)
* `app_update_image` — newest `PEOPLESOFT CS UPDATE IMAGE ... - NATIVE OS`. Campus Solutions has no release patchsets (checked under the CS family and all 17 CS component products), so this ~22 GB PUM image is shipped commented out in `config.example.yaml`; enable it only if your CS patching uses the image.
* `db_release_update`, `db_ojvm`, `db_jdk` — newest matching abstract with `search: {patch_type: all}` (RUs are plain patches)
* `opatch` — current OPatch (patch 6880880) for the release prefix and platform

Quarterly items are reported as "not expected" in CSPU months.

## Operations

* Logs: `state_dir/log/opatchbot.log` (`/opt/app/patch/state/log/opatchbot.log`, rotating). Never contains tokens or passwords.
* If Chromium will not start on the server, run `opatchbot login` on a workstation with the same `secrets.env`
  and copy `session.json` to `/home/oracle/.mos/` (owner oracle, mode 0600); `token`, `monitor` and `run` only need
  that file while the session lasts.
* Token lifetime is 60 minutes; the client refreshes from the saved session when < 10 minutes remain.
* `opatchbot --today YYYY-MM-DD ...` replays a date for testing; `--notify stdout` prints emails instead of sending.
* Re-running `opatchbot run <cycle>` is safe: verified files are skipped, partial downloads resume, and files superseded by a newer pick move to `<item>/superseded/`.
* Always run `opatchbot` through `/usr/local/bin/opatchbot` (or with `env -u LD_LIBRARY_PATH`). oracle's profile
  usually puts `$ORACLE_HOME/lib` on `LD_LIBRARY_PATH`; its older libraries (e.g. libexpat) make Python fail with
  `undefined symbol: XML_SetBillionLaughsAttackProtectionMaximumAmplification`. The systemd units unset it too.
* Release detection: `monitor` reads https://www.oracle.com/security-alerts/. If that page cannot be read (e.g. Oracle's
  web filter answers 403 to this server), it tracks this month's and next month's cycles from Oracle's rule (third
  Tuesday) and detects a release through the patch service instead: a new PeopleTools product DPK for the first
  PeopleTools item in the inventory, released no earlier than `window_before_days` before the scheduled date.
* Exit codes: 0 fetched, 1 partial, 2 needs human (run `opatchbot login`), 3 failed (window closed with gaps), 4 error (transient; retried next day), 5 another run in progress, 6 email relay failed.
* `monitor`, `run`, `fetch`, `add` and `del` take a lock (`state_dir/opatchbot.lock`), so they never run at the same
  time; a second one exits 5. Session and token files are replaced atomically, so the weekly login cannot
  disturb a running download.
* A 401 from Oracle triggers one forced token refresh from the saved session and a retry before a "needs human" email is sent.
* Follow-on (not built): an Ansible role to deploy this, and a playbook to push a cycle's files to the DB/app hosts.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]' && playwright install chromium
pytest -q                                   # unit tests (fixtures captured from live ARU responses)
OPATCHBOT_LIVE=1 OPATCHBOT_SESSION_FILE=/home/oracle/.mos/session.json pytest tests/integration -q
```
