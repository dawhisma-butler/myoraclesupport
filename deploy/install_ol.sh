#!/usr/bin/env bash
# Install opatchbot on Oracle Linux 8/9. Run as root from a checkout of this repository, e.g.:
#   sudo -u oracle git clone <repo url> /opt/app/patch/src     # or copy the source there
#   sudo /opt/app/patch/src/deploy/install_ol.sh
# Idempotent: re-run after `git pull` to upgrade. Never overwrites config.yaml or secrets.env.
#
# Layout (override with the variables below):
#   /opt/app/patch              app home: venv/, config.yaml, state/ (cycles.json, log/), src/
#   /opt/data2/download/patch   downloaded patches (stage_root)
#   /home/oracle/.mos           secrets.env, session.json, token.txt (0700 dir, 0600 files)
#   runs as user oracle; Chromium (headless, no desktop) under /home/oracle/.cache/ms-playwright
set -euo pipefail
APP_USER="${APP_USER:-oracle}"
APP_HOME="${APP_HOME:-/opt/app/patch}"
STAGE="${STAGE:-/opt/data2/download/patch}"
SECRETS_DIR="${SECRETS_DIR:-/home/oracle/.mos}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
id "$APP_USER" >/dev/null 2>&1 || { echo "user $APP_USER does not exist" >&2; exit 1; }
GRP="$(id -gn "$APP_USER")"
as_app() { sudo -u "$APP_USER" -H "$@"; }
as_app test -r "$REPO/pyproject.toml" || { echo "$APP_USER cannot read $REPO; clone it under $APP_HOME/src" >&2; exit 1; }

install -d -o "$APP_USER" -g "$GRP" -m 0750 "$APP_HOME" "$APP_HOME/state" "$STAGE"
install -d -o "$APP_USER" -g "$GRP" -m 0700 "$SECRETS_DIR"

# Python 3.11+
PY="$(command -v python3.12 || command -v python3.11 || true)"
if [[ -z "$PY" ]]; then
  if grep -q 'release 8' /etc/oracle-release 2>/dev/null; then dnf -y module enable python311 || true; fi
  dnf -y install python3.11 python3.11-pip || dnf -y install python3.12 python3.12-pip
  PY="$(command -v python3.12 || command -v python3.11)"
fi
[[ -x "$APP_HOME/venv/bin/python" ]] || as_app "$PY" -m venv "$APP_HOME/venv"
as_app "$APP_HOME/venv/bin/pip" install --quiet --upgrade pip
as_app "$APP_HOME/venv/bin/pip" install --quiet --upgrade "$REPO"

# Headless Chromium for the Oracle sign-in. No desktop or display is needed; these are the shared libraries
# Playwright's Chromium links against on RHEL-family systems.
dnf -y install alsa-lib atk at-spi2-atk at-spi2-core cups-libs libdrm libgbm libxkbcommon libXcomposite libXdamage \
  libXrandr libXfixes libXext libX11 libxcb mesa-libgbm nss nspr pango cairo dbus-libs expat fontconfig freetype \
  glib2 gtk3 xorg-x11-fonts-Type1 liberation-fonts || true
as_app "$APP_HOME/venv/bin/playwright" install chromium

# config + secrets templates (never overwritten)
[[ -f "$APP_HOME/config.yaml" ]] || install -o "$APP_USER" -g "$GRP" -m 0640 "$REPO/config.example.yaml" "$APP_HOME/config.yaml"
[[ -f "$SECRETS_DIR/secrets.env" ]] || install -o "$APP_USER" -g "$GRP" -m 0600 "$REPO/secrets.env.example" "$SECRETS_DIR/secrets.env"

ln -sfn "$APP_HOME/venv/bin/opatchbot" /usr/local/bin/opatchbot

# systemd timers (installed, not enabled)
install -m 0644 "$REPO"/deploy/opatchbot.service "$REPO"/deploy/opatchbot.timer \
  "$REPO"/deploy/opatchbot-heartbeat.service "$REPO"/deploy/opatchbot-heartbeat.timer \
  "$REPO"/deploy/opatchbot-login.service "$REPO"/deploy/opatchbot-login.timer /etc/systemd/system/
systemctl daemon-reload

cat <<MSG

Installed. Next steps:
  1. vi $APP_HOME/config.yaml            # notify: smtp_host, recipients
  2. vi $SECRETS_DIR/secrets.env         # MOS_USER, MOS_PASS, MOS_TOTP_SEED (keep mode 0600)
  3. sudo -u $APP_USER opatchbot login   # unattended with MOS_TOTP_SEED; prints the token expiry
  4. sudo -u $APP_USER opatchbot list    # what will be searched; adjust with opatchbot add / del
  5. sudo -u $APP_USER opatchbot resolve 2026-10-CPU      # search only; nothing downloaded
  6. systemctl enable --now opatchbot.timer opatchbot-heartbeat.timer opatchbot-login.timer
MSG
