# MOS patch automation (PeopleTools) — headless Linux server

Scripts that find and download the newest PeopleTools product patch from My Oracle Support
with curl only (no browser on the server), using Oracle's ARU XML services at `https://updates.oracle.com/Orion/Services/`
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

## Headless servers: how the token gets to the server

The servers have no GUI and no browser; everything on them is command line and curl.
Oracle's sign-in (login.oracle.com / Oracle Cloud identity, with MFA) is a JavaScript application,
so neither curl nor a text browser (lynx, w3m, elinks) can complete it. The only verified
curl-only route is therefore:

1. A person signs in to My Oracle Support **in a browser on any other device** (workstation,
   laptop, phone) and opens https://updates.oracle.com/Orion/GenToken/get_token.
2. They hand that token (valid **60 minutes**) to the server with `mos_set_token.sh`.
3. `mos_set_token.sh` saves it and immediately starts the sync in the background, all with curl.

Downloads resume, so if a large download outlives the token, install a new token and the next run
continues where it stopped and re-verifies the SHA-256 checksums.

Whether fully unattended (scheduled) runs are possible with curl alone depends on what Oracle allows
for your account; `mos_curl_login_probe.sh` tests that (see "Can it run unattended?").

## Linux deployment layout

| what | where |
|---|---|
| application (these scripts) | `/opt/app/patch` |
| patch downloads | `/opt/data2/download/patch` |
| owner / runtime user | `oracle` (home `/home/oracle`) |
| secrets directory (mode 0700) | `/home/oracle/.mos` |
| log | `/opt/app/patch/logs/mos-patch-sync.log` |

All scripts default to these paths; override with `APP_HOME`, `MOS_DIR`, `MOS_OUTDIR`, `MOS_LOG_DIR`.

Contents of `/home/oracle/.mos`:

| file | purpose |
|---|---|
| `user` | MOS username (email) |
| `token.txt` | current 60-minute download token (0600), written by `mos_set_token.sh` |
| `password` | optional, 0600: only useful if the probe shows Oracle accepts a password login. The job refuses the file if group/other can read it. |

Requirements (all command line): `bash`, `curl`, `python3`, `flock`, `setsid` (util-linux),
`sha256sum` (coreutils), `file`. No browser, X11 or desktop packages.

## Files

| file | purpose |
|---|---|
| `mos_set_token.sh` | Install a token pasted (or piped) in, check its expiry, start the sync in the background. |
| `weekly_sync.sh` | The sync job: checks for a valid token (or tries the password grant), downloads, logs. Uses `flock` so runs never overlap. |
| `mos_patch_sync.sh` | Resolve newest `PT 8.61.xx PRODUCT PATCH DPK` for a platform, download (resumable), verify SHA-256. `--list`, `--dry-run`. |
| `mos_token_probe.sh` | Inspect a token file: expiry, lifetime, endpoint status codes. Never prints the token. |
| `mos_curl_login_probe.sh` | Tests whether curl-only login (classic SSO Basic auth, OAuth2 device code) works for your account. |
| `mos_mint_token.sh` | Tests/uses the OAuth2 password grant (refused by Oracle as of 2026-11-09). |
| `install_linux.sh` | Root installer: directories with correct ownership, scripts, systemd units. |
| `systemd/` | Optional weekly timer; only useful once unattended login works. |
| `wget_40014041_win_x64.sh` | Oracle-generated wget.sh for patch 40014041 (reference; also token based). |

## Install

```bash
sudo bash install_linux.sh
sudo -iu oracle
echo 'you@butler.edu' > /home/oracle/.mos/user
```

## Run a download

On any device with a browser: sign in to My Oracle Support, open
https://updates.oracle.com/Orion/GenToken/get_token and copy the token. Then either

```bash
# on the server, as oracle: paste the token when prompted (it is not shown)
/opt/app/patch/mos_set_token.sh
```

or, from a workstation that has the token saved in a file, in one command:

```bash
ssh oracle@SERVER /opt/app/patch/mos_set_token.sh < token.txt
```

Follow progress:

```bash
tail -f /opt/app/patch/logs/mos-patch-sync.log
```

Other useful commands (as oracle, while the token is valid):

```bash
bash /opt/app/patch/mos_token_probe.sh            # remaining lifetime + endpoint checks
bash /opt/app/patch/mos_patch_sync.sh --list      # candidate patches
bash /opt/app/patch/mos_patch_sync.sh --dry-run   # what would be downloaded
/opt/app/patch/mos_set_token.sh --foreground      # run the sync in this terminal instead
```

## Can it run unattended?

Only if Oracle lets this account log in without a browser. Test once, as oracle on the server:

```bash
bash /opt/app/patch/mos_curl_login_probe.sh
bash /opt/app/patch/mos_mint_token.sh
```

They print status codes and Oracle error codes only, never the password or a token. Outcomes:

* **Classic SSO Basic auth WORKS**: curl can log in with username/password and keep session
  cookies (how Oracle's older wget.sh scripts worked). `mos_patch_sync.sh` then needs a cookie mode
  added; after that the systemd timer can run weekly. Expect this to fail if the account requires MFA.
* **Device code WORKS**: you would approve a short code on your phone once and the server could
  then refresh tokens itself; this needs a small addition to the scripts.
* **Password grant WORKS** (`mos_mint_token.sh` prints "token issued"): put the password in
  `/home/oracle/.mos/password` (`chmod 600`) and enable the timer; `weekly_sync.sh` already uses it.
* **Nothing works** (expected today): keep the hand-off above. The run takes one paste per patch cycle.

To enable the schedule once one of the above works:

```bash
sudo systemctl enable --now mos-patch-sync.timer      # Mondays 06:00 as oracle
```

or in oracle's crontab: `0 6 * * 1 /bin/bash /opt/app/patch/weekly_sync.sh`.
Without unattended login, a scheduled run just logs `NEED TOKEN` and exits 2.

## Notes

* `mos_patch_sync.sh` parses Oracle's XML with Python's stdlib `xml.etree`; the source is Oracle over
  TLS, but if `defusedxml` is installed it would be the stricter choice.
* Download resume uses `curl -C -`; re-running skips files whose size already matches and re-verifies them.
