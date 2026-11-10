#!/usr/bin/env bash
# install_linux.sh - install the MOS patch sync on a Linux server. Run as root from this directory:
#   sudo bash install_linux.sh
# Idempotent. Does NOT create secrets or sign in to Oracle; it prints those steps at the end.
set -euo pipefail

APP_USER="${APP_USER:-oracle}"
APP_HOME="${APP_HOME:-/opt/app/patch}"
OUT_DIR="${MOS_OUTDIR:-/opt/data2/download/patch}"
MOS_DIR="${MOS_DIR:-/home/oracle/.mos}"
SRC="$(cd "$(dirname "$0")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
id "$APP_USER" >/dev/null 2>&1 || { echo "User $APP_USER does not exist." >&2; exit 1; }
APP_GROUP="$(id -gn "$APP_USER")"

for cmd in curl python3 flock sha256sum file; do
  command -v "$cmd" >/dev/null 2>&1 || echo "WARNING: '$cmd' not found; install it before running the job."
done
if ! command -v microsoft-edge >/dev/null 2>&1 && ! command -v microsoft-edge-stable >/dev/null 2>&1 \
   && ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1 \
   && ! command -v chromium >/dev/null 2>&1; then
  echo "WARNING: no Edge/Chrome/Chromium found; headless token minting needs one (see README)."
fi

install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$APP_HOME" "$APP_HOME/logs"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$OUT_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0700 "$MOS_DIR"

for f in mos_browser_token.sh mos_mint_token.sh mos_patch_sync.sh mos_token_probe.sh weekly_sync.sh wget.sh; do
  install -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$SRC/$f" "$APP_HOME/$f"
done
install -o "$APP_USER" -g "$APP_GROUP" -m 0640 "$SRC/README.md" "$APP_HOME/README.md"

if [[ -d /run/systemd/system ]]; then
  install -o root -g root -m 0644 "$SRC/systemd/mos-patch-sync.service" /etc/systemd/system/
  install -o root -g root -m 0644 "$SRC/systemd/mos-patch-sync.timer"   /etc/systemd/system/
  systemctl daemon-reload
  echo "systemd units installed (timer NOT enabled yet)."
else
  echo "systemd not running; skipped timer install. Use the cron line in README instead."
fi

cat <<NEXT

Installed to $APP_HOME (owner $APP_USER:$APP_GROUP). Downloads go to $OUT_DIR.

Next steps (as $APP_USER, see README):
  1. echo 'you@butler.edu' > $MOS_DIR/user
  2. One-time browser sign-in to create $MOS_DIR/edge-profile (needs a display, e.g. ssh -X).
  3. Test: bash $APP_HOME/weekly_sync.sh; tail -30 $APP_HOME/logs/mos-patch-sync.log
  4. Schedule (as root): systemctl enable --now mos-patch-sync.timer
NEXT
