#!/bin/bash
# Installs ilo-fan to /opt/ilo-fan with its own Python venv, creates
# /etc/ilo-fan (config + SSH key) and the systemd unit. Does not enable it.
# Run as root from the repo folder: sudo ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

install -d /opt/ilo-fan /etc/ilo-fan
chmod 700 /etc/ilo-fan
install -m 755 ilo_fan.py /opt/ilo-fan/ilo_fan.py

# paramiko 4+ dropped diffie-hellman-group14-sha1 and ssh-rsa host keys, the only
# ones iLO 4 offers, so pin 3.x.
if [ ! -x /opt/ilo-fan/venv/bin/python ]; then
  if command -v uv >/dev/null; then
    uv venv -q /opt/ilo-fan/venv
    uv pip install -q -p /opt/ilo-fan/venv/bin/python "paramiko>=3.4,<4"
  else
    python3 -m venv /opt/ilo-fan/venv
    /opt/ilo-fan/venv/bin/pip install -q "paramiko>=3.4,<4"
  fi
fi

[ -f /etc/ilo-fan/ilo-fan.toml ] || install -m 600 ilo-fan.toml.example /etc/ilo-fan/ilo-fan.toml

if [ ! -f /etc/ilo-fan/id_rsa ]; then
  # iLO 4 accepts RSA keys up to 2048 bits; PEM format keeps paramiko happy.
  ssh-keygen -q -t rsa -b 2048 -m PEM -N "" -C "ilo-fan@$(cat /etc/hostname)" -f /etc/ilo-fan/id_rsa
fi

install -m 644 ilo-fan.service /etc/systemd/system/ilo-fan.service
systemctl daemon-reload

cat <<EOF

Installed. Next:
 1. Edit /etc/ilo-fan/ilo-fan.toml (at least [ilo] host).
 2. In the iLO web page, add this public key to the iLO user named in the config
    (Administration > Security > Secure Shell Key > Authorize New Key):

$(cat /etc/ilo-fan/id_rsa.pub)

 3. /opt/ilo-fan/venv/bin/python /opt/ilo-fan/ilo_fan.py trust
    /opt/ilo-fan/venv/bin/python /opt/ilo-fan/ilo_fan.py test
    /opt/ilo-fan/venv/bin/python /opt/ilo-fan/ilo_fan.py probe
 4. systemctl enable --now ilo-fan && journalctl -fu ilo-fan

Without the patched firmware you can still watch what it would do:
    /opt/ilo-fan/venv/bin/python /opt/ilo-fan/ilo_fan.py run --dry-run
EOF
