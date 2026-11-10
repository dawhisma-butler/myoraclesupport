# MOS patch automation (PeopleTools)

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

## Files

| file | purpose |
|---|---|
| `mos_token_probe.sh` | Inspect a token file: expiry, lifetime, endpoint status codes. Never prints the token. |
| `mos_mint_token.sh` | Try to mint a token with username/password (OAuth2 password grant). Prompts for the password or reads `MOS_PASS`. |
| `mos_patch_sync.sh` | Resolve newest `PT 8.61.xx PRODUCT PATCH DPK` for a platform, download (resumable), verify SHA-256. `--list`, `--dry-run`. |
| `weekly_sync.sh` | Unattended wrapper: Keychain password → mint token → sync. Logs to `~/Library/Logs/mos-patch-sync.log`. |
| `com.mocs.mos-patch-sync.plist` | launchd schedule (Mondays 06:00). Install instructions inside. |
| `wget_40014041_win_x64.sh` | Oracle-generated wget.sh for patch 40014041 (reference). |

## One-time setup for unattended runs

```bash
# 1. confirm the password grant works for your account (prompts for password)
bash mos_mint_token.sh dawhisma@butler.edu

# 2. store the password in the login Keychain and remember the username
security add-generic-password -a 'dawhisma@butler.edu' -s 'mos' -w
mkdir -p ~/.mos && chmod 700 ~/.mos && echo 'dawhisma@butler.edu' > ~/.mos/user

# 3. test the whole chain once
bash weekly_sync.sh && tail -20 ~/Library/Logs/mos-patch-sync.log

# 4. schedule
cp com.mocs.mos-patch-sync.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mocs.mos-patch-sync.plist
```

If step 1 fails with an MFA error, Oracle requires a second factor for the password grant on this
account; see "Fallback" below.

## Manual use (token from the browser)

```bash
# sign in to the portal, open https://updates.oracle.com/Orion/GenToken/get_token, save the token:
mkdir -p ~/.mos && chmod 700 ~/.mos && pbpaste | tr -d '[:space:]' > ~/.mos/token.txt && chmod 600 ~/.mos/token.txt
bash mos_token_probe.sh            # shows remaining lifetime
bash mos_patch_sync.sh --list      # candidate patches
bash mos_patch_sync.sh             # download newest to ~/Downloads/peopletools
```

## Fallback if tokens cannot be minted headlessly (current state, 2026-11-09)

`mos_mint_token.sh` returns `invalid_request` for every scope: the `UPDATES_PROD` client is
confidential and the password grant needs its secret, which only Oracle's tools hold. So the
job uses a **dedicated Edge profile** that stays signed in to login.oracle.com; the scheduled run
opens the token page headlessly in that profile (`mos_browser_token.sh`), saves the token, and
then runs `mos_patch_sync.sh`. A human re-authenticates only when Oracle expires that session.

One-time: create the profile and sign in (MFA) — run this yourself, then close that Edge window:

```bash
mkdir -p ~/.mos/edge-profile && chmod 700 ~/.mos
"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --user-data-dir="$HOME/.mos/edge-profile" --no-first-run \
  "https://updates.oracle.com/Orion/GenToken/get_token"
```

Then test headless minting and the chain:

```bash
bash mos_browser_token.sh && bash mos_token_probe.sh && bash mos_patch_sync.sh --dry-run
```

In `weekly_sync.sh`, token minting falls back to `mos_browser_token.sh` when the password grant fails.
If it exits 2 ("SESSION EXPIRED"), re-run the one-time sign-in above.

## Notes

* `mos_patch_sync.sh` parses Oracle's XML with Python's stdlib `xml.etree`; the source is Oracle over
  TLS, but if `defusedxml` is installed it would be the stricter choice.
* Download resume uses `curl -C -`; re-running skips files whose size already matches and re-verifies them.
