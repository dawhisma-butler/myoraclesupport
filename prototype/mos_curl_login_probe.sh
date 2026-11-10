#!/usr/bin/env bash
# mos_curl_login_probe.sh - test whether this account can get download access with curl alone
# (no browser), which is what fully unattended runs on a headless server would need.
#
# Tests, printing only HTTP status codes and Oracle error codes (never credentials or tokens):
#   1. Classic SSO with HTTP Basic auth: the method Oracle's older wget.sh scripts used
#      (curl -u user against updates.oracle.com, following the login.oracle.com redirect).
#   2. OAuth2 device-code grant: the server shows a short code; you approve it once on your phone,
#      and the server receives a token. Works only if Oracle enabled it for the UPDATES_PROD client.
# (The OAuth2 password grant is tested separately by mos_mint_token.sh.)
#
# Usage: bash mos_curl_login_probe.sh [username]    (prompts for the password; or set MOS_PASS)
set -uo pipefail
MOS_DIR="${MOS_DIR:-/home/oracle/.mos}"
USER_NAME="${MOS_USER:-${1:-$(cat "$MOS_DIR/user" 2>/dev/null || true)}}"
CLIENT_ID="${MOS_CLIENT_ID:-d590f99d833d47789fb686701f021cba}"
IDCS="https://login-ext.identity.oraclecloud.com"
ARU="https://updates.oracle.com"

[[ -n "$USER_NAME" ]] || read -r -p "MOS username (email): " USER_NAME
[[ -n "${MOS_PASS:-}" ]] || { read -r -s -p "MOS password (not shown): " MOS_PASS; echo; }

umask 077
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
jar="$work/cookies.txt"; cfg="$work/cfg"

echo "== 1. Classic SSO, HTTP Basic auth =="
# Credentials go through a curl config file so they never appear in `ps`.
printf 'user = "%s:%s"\n' "$USER_NAME" "$MOS_PASS" > "$cfg"
code="$(curl -s -K "$cfg" --location-trusted -L -c "$jar" -b "$jar" --max-time 120 \
        -o "$work/sso.out" -w '%{http_code} %{url_effective}' "$ARU/Orion/Services/download" || echo "000 -")"
: > "$cfg"
echo "login request: HTTP ${code%% *}  ended at: $(sed -E 's#^[0-9]+ (https?://[^/?]+[^?]*).*#\1#' <<<"$code")"
mcode="$(curl -s -b "$jar" -L --max-time 60 -o "$work/meta.xml" -w '%{http_code}' \
         "$ARU/Orion/Services/metadata?table=aru_platforms" || echo 000)"
if [[ "$mcode" == "200" ]] && grep -q '<platform' "$work/meta.xml" 2>/dev/null; then
  echo "RESULT: WORKS. Session cookies from Basic auth give metadata access (HTTP 200)."
  echo "        Unattended runs are possible with curl only; patch sync needs a cookie mode for this."
else
  echo "RESULT: does not work (metadata HTTP $mcode). Expected if the account requires MFA."
  if grep -qiE 'mfa|verification|one-time|passcode|sms' "$work/sso.out" 2>/dev/null; then
    echo "        The response mentions a second factor (MFA)."
  fi
fi

echo
echo "== 2. OAuth2 device-code grant =="
dcode="$(curl -s --max-time 60 -o "$work/dev.json" -w '%{http_code}' \
          --data-urlencode "client_id=$CLIENT_ID" \
          --data-urlencode "scope=updates.patch_download updates.search updates.metadata" \
          "$IDCS/oauth2/v1/device" || echo 000)"
python3 -I - "$work/dev.json" "$dcode" <<'PY'
import json, sys
path, code = sys.argv[1], sys.argv[2]
try: d = json.load(open(path))
except Exception: d = {}
if code == "200" and d.get("device_code"):
    print("RESULT: WORKS. Oracle issued a device code (not used further by this probe).")
    print("        Unattended runs could use: approve once on your phone, then refresh tokens.")
else:
    print(f"RESULT: does not work (HTTP {code}: {d.get('error','no JSON')} - {d.get('error_description','')})")
PY
unset MOS_PASS
echo
echo "Nothing above contains your password or a token. Send this output to whoever maintains the job."
