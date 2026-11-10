#!/usr/bin/env bash
# Install opatchbot on Oracle Linux 8/9 (run as root from the repo root). Idempotent.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
id -u opatchbot >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/opatchbot --shell /bin/bash opatchbot
install -d -o opatchbot -g opatchbot -m 0750 /var/lib/opatchbot /stage/oracle-patches
install -d -o root -g opatchbot -m 0750 /etc/opatchbot
# Python 3.11+
if ! command -v python3.11 >/dev/null 2>&1 && ! command -v python3.12 >/dev/null 2>&1; then
  if grep -q 'release 8' /etc/oracle-release 2>/dev/null; then dnf -y module enable python311 || true; fi
  dnf -y install python3.11 python3.11-pip || dnf -y install python3.12 python3.12-pip
fi
PY="$(command -v python3.12 || command -v python3.11)"
install -d -o opatchbot -g opatchbot /opt/opatchbot
sudo -u opatchbot "$PY" -m venv /opt/opatchbot/venv
sudo -u opatchbot /opt/opatchbot/venv/bin/pip install --upgrade pip
sudo -u opatchbot /opt/opatchbot/venv/bin/pip install "$REPO"
# Headless Chromium for the token service. Playwright's bundled build needs these libs on RHEL-family systems.
dnf -y install alsa-lib atk at-spi2-atk at-spi2-core cups-libs libdrm libgbm libxkbcommon libXcomposite libXdamage \
  libXrandr libXfixes libXext libX11 libxcb mesa-libgbm nss nspr pango cairo dbus-libs expat fontconfig freetype \
  glib2 gtk3 xorg-x11-fonts-Type1 liberation-fonts || true
sudo -u opatchbot /opt/opatchbot/venv/bin/playwright install chromium
# config + secrets templates
[ -f /etc/opatchbot/config.yaml ] || install -o root -g opatchbot -m 0640 "$REPO/config.example.yaml" /etc/opatchbot/config.yaml
[ -f /etc/opatchbot/secrets.env ] || install -o opatchbot -g opatchbot -m 0600 "$REPO/secrets.env.example" /etc/opatchbot/secrets.env
# timers
install -m 0644 "$REPO"/deploy/opatchbot.service "$REPO"/deploy/opatchbot.timer "$REPO"/deploy/opatchbot-heartbeat.service "$REPO"/deploy/opatchbot-heartbeat.timer "$REPO"/deploy/opatchbot-login.service "$REPO"/deploy/opatchbot-login.timer /etc/systemd/system/
systemctl daemon-reload
cat <<MSG

Installed. Next steps (as root unless noted):
  1. vi /etc/opatchbot/config.yaml      # relay host, recipients, stage_root, inventory
  2. vi /etc/opatchbot/secrets.env      # MOS_USER, MOS_PASS, MOS_TOTP_SEED (keep mode 0600)
  3. sudo -u opatchbot OPATCHBOT_CONFIG=/etc/opatchbot/config.yaml OPATCHBOT_SECRETS=/etc/opatchbot/secrets.env /opt/opatchbot/venv/bin/opatchbot login
  4. sudo -u opatchbot OPATCHBOT_CONFIG=/etc/opatchbot/config.yaml /opt/opatchbot/venv/bin/opatchbot token
  5. sudo -u opatchbot OPATCHBOT_CONFIG=/etc/opatchbot/config.yaml /opt/opatchbot/venv/bin/opatchbot resolve 2026-10-CPU
  6. systemctl enable --now opatchbot.timer opatchbot-heartbeat.timer opatchbot-login.timer
MSG
