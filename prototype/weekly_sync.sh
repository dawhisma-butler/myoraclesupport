#!/usr/bin/env bash
# weekly_sync.sh - unattended wrapper: mint a fresh MOS token, then fetch the newest
# PeopleTools product patch for each configured release/platform.
#
# Credentials: MOS username in MOS_USER (or ~/.mos/user), password from the macOS Keychain
# item created once with:
#   security add-generic-password -a 'you@example.edu' -s 'mos' -w   # prompts for the password
#
# Logs to ~/Library/Logs/mos-patch-sync.log. Intended to be run by launchd (see com.mocs.mos-patch-sync.plist).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/mos-patch-sync.log"
OUT="${MOS_OUTDIR:-$HOME/Downloads/peopletools}"
TOKEN_FILE="$HOME/.mos/token.txt"
MOS_USER="${MOS_USER:-$(cat "$HOME/.mos/user" 2>/dev/null || true)}"

# release:platform:prefix  (233 = Windows x64, 226 = Linux x86-64)
TARGETS=( "8.61:233:PEOPLETOOLS-WIN-" )

mkdir -p "$(dirname "$LOG")" "$OUT"
exec >>"$LOG" 2>&1
echo "===== $(date '+%F %T') weekly_sync start ====="

# 1. get a fresh token.
#    Primary: headless Edge using the dedicated signed-in profile (~/.mos/edge-profile).
#    Optional: OAuth2 password grant, only if Keychain creds exist AND Oracle ever allows it.
minted=0
if [[ -d "$HOME/.mos/edge-profile" ]]; then
  if bash "$HERE/mos_browser_token.sh" "$TOKEN_FILE"; then minted=1
  else echo "browser token failed (rc=$?). If 'SESSION EXPIRED', re-run the one-time sign-in in README."; fi
fi
if (( ! minted )) && [[ -n "$MOS_USER" ]] && MOS_PASS="$(security find-generic-password -a "$MOS_USER" -s mos -w 2>/dev/null)"; then
  export MOS_USER MOS_PASS
  if bash "$HERE/mos_mint_token.sh" "$MOS_USER" "$TOKEN_FILE"; then minted=1; fi
  unset MOS_PASS
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
