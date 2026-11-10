#!/usr/bin/env bash
# weekly_sync.sh - unattended wrapper: mint a fresh MOS token, then fetch the newest
# PeopleTools product patch for each configured release/platform.
#
# Linux deployment (runs as user oracle):
#   app       /opt/app/patch                (APP_HOME)
#   downloads /opt/data2/download/patch     (MOS_OUTDIR)
#   secrets   /home/oracle/.mos             (MOS_DIR, mode 0700)
#     user        MOS username (email)
#     password    MOS password, mode 0600 (only used if Oracle ever allows the password grant)
#     token.txt   current download token (written by this job)
#     edge-profile/  dedicated signed-in browser profile
#   logs      /opt/app/patch/logs/mos-patch-sync.log
#
# Intended to be run by the systemd timer in systemd/ (or cron; see README).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${APP_HOME:-/opt/app/patch}"
export MOS_DIR="${MOS_DIR:-/home/oracle/.mos}"
LOG_DIR="${MOS_LOG_DIR:-$APP_HOME/logs}"
LOG="$LOG_DIR/mos-patch-sync.log"
OUT="${MOS_OUTDIR:-/opt/data2/download/patch}"
TOKEN_FILE="$MOS_DIR/token.txt"
PASS_FILE="$MOS_DIR/password"
MOS_USER="${MOS_USER:-$(cat "$MOS_DIR/user" 2>/dev/null || true)}"

# release:platform:prefix  (233 = Windows x64, 226 = Linux x86-64)
TARGETS=( "8.61:233:PEOPLETOOLS-WIN-" )

umask 027
mkdir -p "$LOG_DIR" "$OUT"
exec >>"$LOG" 2>&1

# Prevent overlapping runs (e.g. manual run while the timer fires).
exec 9>"$LOG_DIR/.weekly_sync.lock"
if ! flock -n 9; then echo "$(date '+%F %T') another weekly_sync is running; exiting"; exit 0; fi

echo "===== $(date '+%F %T') weekly_sync start (user=$(id -un) host=$(hostname -s)) ====="
[[ "$(id -un)" == "oracle" ]] || echo "WARNING: running as $(id -un), expected oracle"

# 1. get a fresh token.
#    Primary: headless Edge/Chromium using the dedicated signed-in profile ($MOS_DIR/edge-profile).
#    Optional: OAuth2 password grant, only if a password file exists AND Oracle ever allows it.
minted=0
if [[ -d "$MOS_DIR/edge-profile" ]]; then
  if bash "$HERE/mos_browser_token.sh" "$TOKEN_FILE"; then minted=1
  else echo "browser token failed (rc=$?). If 'SESSION EXPIRED', re-run the one-time sign-in in README."; fi
fi
if (( ! minted )) && [[ -n "$MOS_USER" && -r "$PASS_FILE" ]]; then
  if [[ -n "$(find "$PASS_FILE" -perm /077 2>/dev/null)" ]]; then
    echo "REFUSING $PASS_FILE: permissions too open (chmod 600 $PASS_FILE)"
  else
    MOS_PASS="$(head -n1 "$PASS_FILE" | tr -d '\r\n')"
    export MOS_USER MOS_PASS
    if bash "$HERE/mos_mint_token.sh" "$MOS_USER" "$TOKEN_FILE"; then minted=1; fi
    unset MOS_PASS
  fi
fi
if (( ! minted )); then
  echo "TOKEN MINT FAILED - a human must re-authenticate (see README 'Fallback')."
  exit 2
fi

# 2. sync each target
rc=0
for t in "${TARGETS[@]}"; do
  IFS=: read -r rel plat prefix <<<"$t"
  echo "--- target release=$rel platform=$plat prefix=$prefix"
  bash "$HERE/mos_patch_sync.sh" -t "$TOKEN_FILE" -o "$OUT" -r "$rel" -p "$plat" -m "$prefix" || rc=$?
done
echo "===== $(date '+%F %T') weekly_sync end rc=$rc ====="
exit "$rc"
