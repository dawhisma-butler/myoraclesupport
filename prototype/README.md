# MOS patch automation (PeopleTools) — Linux server

Scripts that find and download the newest PeopleTools product patch from My Oracle Support
without a browser, using Oracle's ARU XML services at `https://updates.oracle.com/Orion/Services/`
and a short-lived (1 hour) OAuth2 download token.

## How Oracle's download auth actually works (learned 2026-11-09)

* The portal login (support.oracle.com, via your tenancy's Oracle Cloud identity) does **not**
  authorize downloads. Downloads are served by `updates.oracle.com`, which needs either
  * a classic `login.oracle.com` SSO session in the browser (MFA, SMS), or
  * an OAuth2 access token sent as `Authorization: Bearer <token>`.
* Tokens come from Oracle's external identity domain `login-ext.identity.oraclecloud.com`
  (client `UPDATES_PROD`, scope `search metadata ... patch_download`) and live **60 minutes**.
  A token cannot mint another token. The browser page
  `https://updates.oracle.com/Orion/GenToken/get_token` issues one interactively.
* Oracle's own `wget.sh` (saved here as `wget_40014041_win_x64.sh`) is just a batch downloader
  that takes that token (`-T token.txt`).
* With a valid token these work (verified):
  * `Orion/Services/metadata?table=aru_platforms|aru_products|aru_releases`
  * `Orion/Services/search?product=21917&release=600000000204773&platform=233` (PeopleTools family, 8.61, Windows x64)
  * `Orion/Services/search?bug=<patch>` (full file list with sizes, download URLs and SHA-256)
  * `Orion/Services/download/<file>?aru=...&patch_file=...`
* IDs: product 21917 = PeopleSoft PeopleTools (family), 21918 = PeopleSoft Enterprise PT PeopleTools;
  release 600000000204773 = 8.61; platform 233 = Microsoft Windows x64, 226 = Linux x86-64, 212 = AIX.

These endpoints are not formally documented by Oracle; they are what Enterprise Manager
(`emcli search_patches`, `list_aru_*`) and AutoUpgrade use, and community tools such as getMOSPatch.

## Linux deployment layout

| what | where |
|---|---|
| application (these scripts) | `/opt/app/patch` |
| patch downloads | `/opt/data2/download/patch` |
| owner / runtime user | `oracle` (home `/home/oracle`) |
| secrets directory (mode 0700) | `/home/oracle/.mos` |
| log | `/opt/app/patch/logs/mos-patch-sync.log` |
| schedule | systemd `mos-patch-sync.timer` (Mondays 06:00) |

All scripts default to these paths; override with `APP_HOME`, `MOS_DIR`, `MOS_OUTDIR`,
`MOS_LOG_DIR`, `MOS_EDGE_BIN`, `MOS_EDGE_PROFILE` if needed.

Contents of `/home/oracle/.mos`:

| file | purpose |
|---|---|
| `user` | MOS username (email) |
| `password` | MOS password, mode 0600. Only used if Oracle ever allows the password grant (it currently does not; see Fallback). The job refuses the file if group/other can read it. |
| `token.txt` | current 1-hour download token, written by the job (0600) |
| `edge-profile/` | dedicated browser profile signed in to login.oracle.com |

Requirements: `bash`, `curl`, `python3`, `flock` (util-linux), `sha256sum` (coreutils), `file`, and
Microsoft Edge or Google Chrome/Chromium for headless token minting
(e.g. `dnf install microsoft-edge-stable` from packages.microsoft.com, or `chromium`).

## Files

| file | purpose |
|---|---|
| `mos_token_probe.sh` | Inspect a token file: expiry, lifetime, endpoint status codes. Never prints the token. |
| `mos_mint_token.sh` | Try to mint a token with username/password (OAuth2 password grant). Prompts for the password or reads `MOS_PASS`. |
| `mos_browser_token.sh` | Mint a token headlessly from the signed-in browser profile. |
| `mos_patch_sync.sh` | Resolve newest `PT 8.61.xx PRODUCT PATCH DPK` for a platform, download (resumable), verify SHA-256. `--list`, `--dry-run`. |
| `weekly_sync.sh` | Unattended wrapper: browser token (or password file) → sync. Logs to `/opt/app/patch/logs/mos-patch-sync.log`. Uses `flock` so runs never overlap. |
| `systemd/mos-patch-sync.service`, `.timer` | systemd schedule (Mondays 06:00, runs as `oracle`). |
| `install_linux.sh` | Root installer: creates directories with correct ownership, copies scripts, installs the systemd units. |
| `wget_40014041_win_x64.sh` | Oracle-generated wget.sh for patch 40014041 (reference). |

## Install

```bash
sudo bash install_linux.sh
```

This creates `/opt/app/patch`, `/opt/app/patch/logs`, `/opt/data2/download/patch` and
`/home/oracle/.mos` owned by `oracle`, copies the scripts, and installs (but does not enable) the
systemd timer.

## One-time setup (as oracle)

```bash
sudo -iu oracle
echo 'you@butler.edu' > /home/oracle/.mos/user
```

Create the dedicated browser profile and sign in once (MFA). This needs a display: connect with
X11 forwarding (`ssh -X oracle@server`, with `xorg-x11-xauth` installed on the server) or use a VNC
session. `--password-store=basic` is required so the later headless runs, which have no desktop
keyring, can read the profile's cookies:

```bash
mkdir -p /home/oracle/.mos/edge-profile && chmod 700 /home/oracle/.mos
microsoft-edge --password-store=basic --user-data-dir=/home/oracle/.mos/edge-profile --no-first-run \
  "https://updates.oracle.com/Orion/GenToken/get_token"
```

Sign in, confirm a token is displayed, then close that browser window. (Use `google-chrome` or
`chromium` in place of `microsoft-edge` if that is what is installed.) A profile created on a Mac or
Windows machine cannot be copied over: its cookies are encrypted with that machine's keystore.

Test headless minting and the whole chain:

```bash
bash /opt/app/patch/mos_browser_token.sh && bash /opt/app/patch/mos_token_probe.sh
bash /opt/app/patch/mos_patch_sync.sh --dry-run
bash /opt/app/patch/weekly_sync.sh; tail -30 /opt/app/patch/logs/mos-patch-sync.log
```

## Schedule

```bash
sudo systemctl enable --now mos-patch-sync.timer
systemctl list-timers mos-patch-sync.timer
sudo systemctl start mos-patch-sync.service      # run now
journalctl -u mos-patch-sync                       # systemd's view; details are in the log file
```

Without systemd, use oracle's crontab instead (`crontab -e` as oracle):

```
0 6 * * 1 /bin/bash /opt/app/patch/weekly_sync.sh
```

## Manual use (token from a browser elsewhere)

Sign in to the portal on any machine, open https://updates.oracle.com/Orion/GenToken/get_token,
copy the token, then on the server as oracle:

```bash
read -rs TOK && printf '%s\n' "$TOK" > /home/oracle/.mos/token.txt && chmod 600 /home/oracle/.mos/token.txt; unset TOK
bash /opt/app/patch/mos_token_probe.sh            # shows remaining lifetime
bash /opt/app/patch/mos_patch_sync.sh --list      # candidate patches
bash /opt/app/patch/mos_patch_sync.sh             # download newest to /opt/data2/download/patch
```

## Fallback if tokens cannot be minted headlessly (current state, 2026-11-09)

`mos_mint_token.sh` returns `invalid_request` for every scope: the `UPDATES_PROD` client is
confidential and the password grant needs its secret, which only Oracle's tools hold. So the
job uses a **dedicated browser profile** that stays signed in to login.oracle.com; the scheduled run
opens the token page headlessly in that profile (`mos_browser_token.sh`), saves the token, and
then runs `mos_patch_sync.sh`. A human re-authenticates only when Oracle expires that session.

In `weekly_sync.sh`, token minting falls back to `mos_mint_token.sh` (with `/home/oracle/.mos/password`)
when the browser token fails. If the log shows "SESSION EXPIRED", re-run the one-time sign-in above.

## Notes

* `mos_patch_sync.sh` parses Oracle's XML with Python's stdlib `xml.etree`; the source is Oracle over
  TLS, but if `defusedxml` is installed it would be the stricter choice.
* Download resume uses `curl -C -`; re-running skips files whose size already matches and re-verifies them.
