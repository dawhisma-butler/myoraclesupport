#!/usr/bin/env bash
# mos_browser_token.sh - fetch a My Oracle Support download token headlessly from a browser
# profile that is already signed in to login.oracle.com (MFA done once by a human).
#
# Uses Microsoft Edge (or Chrome/Chromium) in headless mode with a DEDICATED user-data-dir so it
# never conflicts with an interactive browser. Writes the token to a file; prints only its expiry.
#
# Usage: bash mos_browser_token.sh [token_out_file]
# Env:   MOS_DIR           secrets directory (default /home/oracle/.mos)
#        MOS_EDGE_PROFILE  user-data-dir (default $MOS_DIR/edge-profile)
#        MOS_EDGE_BIN      browser binary (default: first of microsoft-edge, microsoft-edge-stable,
#                          google-chrome, chromium-browser, chromium found on PATH)
#
# The profile uses --password-store=basic so its cookies can be decrypted by a headless run that
# has no desktop keyring (cron/systemd). The one-time sign-in MUST use the same flag (see README).
#
# Exit codes: 0 token saved; 2 session expired (got a sign-in page -> human must re-auth); 3 other failure
set -euo pipefail

MOS_DIR="${MOS_DIR:-/home/oracle/.mos}"
OUT="${1:-$MOS_DIR/token.txt}"
PROFILE="${MOS_EDGE_PROFILE:-$MOS_DIR/edge-profile}"
URL="https://updates.oracle.com/Orion/GenToken/get_token"

EDGE="${MOS_EDGE_BIN:-}"
if [[ -z "$EDGE" ]]; then
  for b in microsoft-edge microsoft-edge-stable google-chrome google-chrome-stable chromium-browser chromium; do
    if command -v "$b" >/dev/null 2>&1; then EDGE="$(command -v "$b")"; break; fi
  done
fi
[[ -n "$EDGE" && -x "$EDGE" ]] || { echo "No Edge/Chrome/Chromium binary found (set MOS_EDGE_BIN)" >&2; exit 3; }
[[ -d "$PROFILE" ]] || { echo "Profile dir missing: $PROFILE (create it by signing in once; see README)" >&2; exit 3; }
if [[ -f "$PROFILE/SingletonLock" ]] && pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null 2>&1; then
  echo "An Edge instance is already using $PROFILE; close it first." >&2; exit 3
fi

umask 077
mkdir -p "$(dirname "$OUT")"
dom="$(mktemp)"; trap 'rm -f "$dom"' EXIT

# Headless dump of the token page. The DOM is written to a temp file and never printed.
"$EDGE" --headless=new --disable-gpu --no-first-run --no-default-browser-check \
        --password-store=basic --use-mock-keychain \
        --user-data-dir="$PROFILE" --profile-directory=Default \
        --virtual-time-budget=15000 --timeout=30000 \
        --dump-dom "$URL" > "$dom" 2>/dev/null || true

# Extract a JWT (three base64url segments). Accept the longest match.
tok="$(LC_ALL=C grep -oE '[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}' "$dom" | awk '{ if (length($0) > length(best)) best=$0 } END { print best }' || true)"

if [[ -z "$tok" ]]; then
  if LC_ALL=C grep -qiE 'sign in|signin|login\.oracle\.com|identity\.oraclecloud\.com|password' "$dom"; then
    echo "SESSION EXPIRED: the profile was redirected to a sign-in page. Re-authenticate once:" >&2
    echo "  \"$EDGE\" --password-store=basic --user-data-dir=\"$PROFILE\" \"$URL\"" >&2
    exit 2
  fi
  echo "No token found in page (size $(wc -c <"$dom" | tr -d ' ') bytes)." >&2
  exit 3
fi

printf '%s\n' "$tok" > "$OUT"; chmod 600 "$OUT"
python3 -I - "$tok" <<'PY'
import sys, json, base64, datetime
p = sys.argv[1].split(".")[1]; p += "=" * (-len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
exp = datetime.datetime.fromtimestamp(c["exp"], datetime.timezone.utc)
print("token saved; expires", exp.astimezone().strftime("%Y-%m-%d %H:%M %Z"), "| amr:", c.get("amr"), "| scope ok:", "patch_download" in (c.get("scope") or ""))
PY
