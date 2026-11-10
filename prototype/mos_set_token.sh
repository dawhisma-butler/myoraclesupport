#!/usr/bin/env bash
# mos_set_token.sh - install a My Oracle Support download token on this (headless) server and
# start the patch sync in the background. Command line only; no browser on the server.
#
# Get the token on any machine that has a browser (your workstation or phone):
#   sign in to My Oracle Support, open https://updates.oracle.com/Orion/GenToken/get_token,
#   copy the token. It is valid for 60 minutes.
#
# Then, on the server as oracle (paste the token; it is not echoed):
#   /opt/app/patch/mos_set_token.sh
# or straight from the workstation in one step (token saved in a local file):
#   ssh oracle@server /opt/app/patch/mos_set_token.sh < token.txt
#
# Options: --no-sync     only save the token
#          --foreground  run the sync in this terminal instead of the background
# Exit codes: 0 ok, 1 bad/expired token
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${APP_HOME:-/opt/app/patch}"
export MOS_DIR="${MOS_DIR:-/home/oracle/.mos}"
TOKEN_FILE="$MOS_DIR/token.txt"
SYNC=1; FG=0
for a in "$@"; do
  case "$a" in
    --no-sync) SYNC=0;; --foreground) FG=1;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) echo "Unknown arg: $a" >&2; exit 1;;
  esac
done

if [[ -t 0 ]]; then read -r -s -p "Paste MOS token (not shown), then Enter: " tok; echo
else tok="$(cat)"; fi
tok="$(printf '%s' "$tok" | tr -d '[:space:]')"
[[ "$tok" =~ ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]] || { echo "That does not look like a token (expected a JWT: three dot-separated parts)." >&2; exit 1; }

mins="$(python3 -I - "$tok" <<'PY'
import sys, json, base64, time
try:
    p = sys.argv[1].split(".")[1]; p += "=" * (-len(p) % 4)
    c = json.loads(base64.urlsafe_b64decode(p))
    print(int((c["exp"] - time.time()) // 60))
except Exception:
    print(-1)
PY
)"
(( mins > 0 )) || { echo "Token is expired or unreadable; generate a new one." >&2; exit 1; }

umask 077
mkdir -p "$MOS_DIR"; chmod 700 "$MOS_DIR"
printf '%s\n' "$tok" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"; unset tok
echo "Token saved to $TOKEN_FILE ($mins minutes left)."

(( SYNC )) || exit 0
if (( FG )); then exec bash "$HERE/weekly_sync.sh"; fi
setsid nohup bash "$HERE/weekly_sync.sh" >/dev/null 2>&1 < /dev/null &
echo "Patch sync started in the background. Follow it with:"
echo "  tail -f ${MOS_LOG_DIR:-$APP_HOME/logs}/mos-patch-sync.log"
