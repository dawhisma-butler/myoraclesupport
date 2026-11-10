# opatchbot — Oracle CPU/CSPU patch monitor and stager

`opatchbot` watches Oracle's security-alerts page for each Critical Patch Update (CPU, quarterly) and
Critical Security Patch Update (CSPU, monthly), works out exactly which patches this site needs
(PeopleTools, PeopleTools middleware, PeopleSoft HCM/FSCM/CS, Oracle Database 19c/21c), downloads
them from My Oracle Support with SHA-256 verification into a per-cycle stage folder, and emails
the result. A human is needed only to re-run `opatchbot login` when Oracle expires the sign-in session.

Design: `docs/superpowers/specs/2026-11-09-oracle-cpu-patch-automation-design.md`.
Prototype scripts and the authentication findings that led here: `prototype/README.md`.

## How it works

| step | command | what happens |
|---|---|---|
| sign in | `opatchbot login` | Headless Chromium signs in to login.oracle.com with `MOS_USER`/`MOS_PASS` and the authenticator code (generated from `MOS_TOTP_SEED`, or typed). Saves the session to `state_dir/session.json`. Run once at setup; the weekly `opatchbot-login.timer` repeats it so the session never goes stale, and with `auto_login: true` the daily run also re-logs in by itself if Oracle expires the session early. |
| mint a token | `opatchbot token` | Uses the saved session to open Oracle's token page and store a 1-hour download token. Runs automatically whenever a token is needed. |
| daily | `opatchbot monitor` | Parses https://www.oracle.com/security-alerts/, marks cycles released, runs due cycles once per day each until their window closes (even after everything visible is fetched, because Oracle adds CPU-dated patchsets for up to two weeks), emails late/needs-human notices. |
| one cycle | `opatchbot run 2026-10-CPU` | Resolve the manifest from Oracle's ARU patch services, download with resume, verify, hardlink files already staged in earlier cycles, write `MANIFEST.md`, email. |
| look | `opatchbot status` | Cycle table and token expiry. `opatchbot resolve <cycle>` prints the manifest without downloading or changing state (writes `manifest.review.json`/`MANIFEST.review.md` only). |
| housekeeping | `opatchbot prune --yes`, `opatchbot heartbeat` | Remove cycle folders beyond `keep_cycles` (never partial ones); monthly "alive" email. |

Stage layout: `<stage_root>/<YYYY-MM-CPU|CSPU>/<area>/<item>/<file>` with `MANIFEST.md` and `manifest.json` per cycle.

## 10-minute setup (Oracle Linux 8/9)

```bash
git clone <this repo> /opt/opatchbot/src && cd /opt/opatchbot/src
sudo deploy/install_ol.sh                      # user, venv, Chromium, templates, timers
sudo vi /etc/opatchbot/config.yaml             # relay host, recipients, stage_root, inventory ids
sudo vi /etc/opatchbot/secrets.env             # MOS_USER, MOS_PASS, MOS_TOTP_SEED ; keep 0600
sudo -u opatchbot env OPATCHBOT_CONFIG=/etc/opatchbot/config.yaml OPATCHBOT_SECRETS=/etc/opatchbot/secrets.env \
     /opt/opatchbot/venv/bin/opatchbot login    # one time; unattended if MOS_TOTP_SEED is set
sudo -u opatchbot env OPATCHBOT_CONFIG=/etc/opatchbot/config.yaml /opt/opatchbot/venv/bin/opatchbot token
sudo -u opatchbot env OPATCHBOT_CONFIG=/etc/opatchbot/config.yaml /opt/opatchbot/venv/bin/opatchbot resolve 2026-10-CPU
sudo -u opatchbot env OPATCHBOT_CONFIG=/etc/opatchbot/config.yaml /opt/opatchbot/venv/bin/opatchbot run 2026-10-CPU --notify stdout
sudo systemctl enable --now opatchbot.timer opatchbot-heartbeat.timer opatchbot-login.timer
```

If Playwright's Chromium will not start on the server, run `opatchbot login` on a workstation with the same
`secrets.env` and copy `state_dir/session.json` (mode 0600) to the server; `token`, `monitor` and `run` only
need the session file.

Capturing the authenticator seed: during enrollment choose "Can't scan? / enter key manually" and store that
text key as `MOS_TOTP_SEED`. Verify once that `python -c "import pyotp,os;print(pyotp.TOTP(os.environ['MOS_TOTP_SEED']).now())"`
matches the phone app.

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

* Logs: `state_dir/log/opatchbot.log` (rotating). Never contains tokens or passwords.
* Token lifetime is 60 minutes; the client refreshes from the saved session when < 10 minutes remain.
* `opatchbot --today YYYY-MM-DD ...` replays a date for testing; `--notify stdout` prints emails instead of sending.
* Re-running `opatchbot run <cycle>` is safe: verified files are skipped, partial downloads resume, and files superseded by a newer pick move to `<item>/superseded/`.
* Exit codes: 0 fetched, 1 partial, 2 needs human (run `opatchbot login`), 3 failed (window closed with gaps), 4 error (transient; retried next day), 6 email relay failed.
* A 401 from Oracle triggers one forced token refresh from the saved session and a retry before a "needs human" email is sent.
* Follow-on (not built): an Ansible role to deploy this, and a playbook to push a cycle's files to the DB/app hosts.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]' && playwright install chromium
pytest -q                                   # unit tests (fixtures captured from live ARU responses)
OPATCHBOT_LIVE=1 OPATCHBOT_SESSION_FILE=~/.mos/session.json pytest tests/integration -q
```
