#!/usr/bin/env bash
#
# One-time server setup for the observe-only Polymarket monitor.
# Run as root on a fresh Debian/Ubuntu host:
#
#     sudo bash deploy/install.sh
#
# Idempotent: safe to re-run after pulling new code.

set -euo pipefail

APP_DIR=/opt/polly
DATA_DIR=/var/lib/polly
CONF_DIR=/etc/polly
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash deploy/install.sh" >&2
    exit 1
fi

echo "==> Checking Python"
PY=$(command -v python3)
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Python 3.10+ required, found {sys.version.split()[0]}")
print(f"    {sys.version.split()[0]} OK")
EOF

echo "==> Creating service user 'polly'"
id -u polly &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin polly

echo "==> Granting journal read access (for the dashboard System tab)"
# Without this the dashboard renders an empty log pane: journalctl shows a
# user only their own unit logs unless they are in systemd-journal.
getent group systemd-journal >/dev/null && usermod -aG systemd-journal polly

echo "==> Creating directories"
mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR"

echo "==> Syncing application code to $APP_DIR"
# The venv is deliberately excluded: it is platform-specific and is built
# fresh below. The database is excluded so a re-run never clobbers history.
if command -v rsync &>/dev/null; then
    rsync -a --delete \
        --exclude 'venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
        --exclude '.idea/' --exclude '*.db' --exclude '*.db-wal' \
        --exclude '*.db-shm' --exclude '.env' --exclude 'recordings/' \
        --exclude 'polymarket_report*/' --exclude 'get-pip.py' \
        "$SRC_DIR"/ "$APP_DIR"/
else
    echo "    rsync not found; install it or copy the code manually" >&2
    exit 1
fi

echo "==> Building virtualenv"
"$PY" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/deploy/requirements-server.txt"
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/deploy/requirements-dash.txt"
"$APP_DIR/venv/bin/python" -c "import requests, websockets, flask, waitress; print('    deps OK')"

echo "==> Installing config"
if [[ ! -f "$CONF_DIR/polly.env" ]]; then
    install -m 640 "$APP_DIR/deploy/polly.env.example" "$CONF_DIR/polly.env"
    echo "    wrote $CONF_DIR/polly.env (review it before starting)"
else
    echo "    $CONF_DIR/polly.env exists, keeping your values"
    # An env file written before a feature existed is missing that
    # feature's keys, and the unit that reads them fails at start rather
    # than falling back. Append only what is absent; never touch a key
    # that already has a value.
    added=()
    for key in VERDICT_RETENTION_SCANS POLLY_DASH_HOST POLLY_DASH_PORT \
               POLLY_DASH_USER POLLY_DASH_PASSWORD_HASH POLLY_DASH_SECRET_KEY \
               LIVE_RECORD LIVE_RECORD_MIN_EDGE LIVE_TICK_MIN_INTERVAL_MS \
               TICK_RETENTION_DAYS; do
        if ! grep -qE "^${key}=" "$CONF_DIR/polly.env"; then
            added+=("$key")
        fi
    done
    if (( ${#added[@]} )); then
        {
            echo ""
            echo "# --- added by install.sh on $(date +%F) ---"
            for key in "${added[@]}"; do
                case "$key" in
                    VERDICT_RETENTION_SCANS) echo "VERDICT_RETENTION_SCANS=96" ;;
                    POLLY_DASH_HOST)         echo "POLLY_DASH_HOST=0.0.0.0" ;;
                    POLLY_DASH_PORT)         echo "POLLY_DASH_PORT=8971" ;;
                    POLLY_DASH_USER)         echo "POLLY_DASH_USER=analyst" ;;
                    LIVE_RECORD)             echo "LIVE_RECORD=1" ;;
                    LIVE_RECORD_MIN_EDGE)    echo "LIVE_RECORD_MIN_EDGE=-0.02" ;;
                    LIVE_TICK_MIN_INTERVAL_MS) echo "LIVE_TICK_MIN_INTERVAL_MS=1000" ;;
                    TICK_RETENTION_DAYS)     echo "TICK_RETENTION_DAYS=7" ;;
                    *)                       echo "${key}=" ;;
                esac
            done
        } >> "$CONF_DIR/polly.env"
        echo "    added ${#added[@]} new setting(s): ${added[*]}"
    fi
fi
chown root:polly "$CONF_DIR/polly.env"

echo "==> Setting ownership"
chown -R root:root "$APP_DIR"          # code is read-only to the service
chown -R polly:polly "$DATA_DIR"       # only the database is writable
chmod 750 "$DATA_DIR"

echo "==> Installing systemd units"
install -m 644 "$APP_DIR/deploy/polly-monitor.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/polly-live.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/polly-dash.service" /etc/systemd/system/
systemctl daemon-reload

cat <<'EOF'

==> Done.

Verify the resolved configuration before starting anything:

    sudo -u polly bash -c 'set -a; . /etc/polly/polly.env; set +a; \
        /opt/polly/venv/bin/python /opt/polly/config.py'

Then start the monitor:

    sudo systemctl enable --now polly-monitor
    sudo journalctl -u polly-monitor -f

The live engine is optional and separate:

    sudo systemctl enable --now polly-live

The dashboard needs a password before it will let anyone in:

    sudo -u polly /opt/polly/venv/bin/python /opt/polly/dashboard.py --hash-password
    sudo nano /etc/polly/polly.env      # paste the two lines it prints
    sudo systemctl enable --now polly-dash

EOF
