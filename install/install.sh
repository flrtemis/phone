#!/usr/bin/env bash
# Install phone into PREFIX and link the `phone` command into PATH.
#
#   sudo install/install.sh                     # /opt/phone, /usr/local/bin
#   sudo PREFIX=/srv/phone install/install.sh
#   install/install.sh --user                   # no root: ~/.local, no systemd
#   install/install.sh --with-systemd --user phoneuser
#
# It does not touch your firewall, does not create a resolver, and does not
# start any service without being asked.

set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-/opt/phone}"
BINDIR="${BINDIR:-/usr/local/bin}"
WITH_SYSTEMD=0
SERVICE_USER=""
USER_MODE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)   PREFIX="${2:?}"; shift 2 ;;
        --bindir)   BINDIR="${2:?}"; shift 2 ;;
        --with-systemd) WITH_SYSTEMD=1; shift ;;
        --user)     USER_MODE=1; shift ;;
        --user-name|--user-service) SERVICE_USER="${2:?}"; shift 2 ;;
        -h|--help)  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

info() { printf '[*] %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ "$USER_MODE" -eq 1 ]; then
    PREFIX="${PREFIX:-$HOME/.local/share/phone}"
    BINDIR="${BINDIR:-$HOME/.local/bin}"
    WITH_SYSTEMD=0
fi

if [ "$USER_MODE" -eq 0 ]; then
    [ "$(id -u)" -eq 0 ] || die "run with sudo, or use --user for a home-directory install"
fi

info "installing $REPO_DIR -> $PREFIX"
mkdir -p "$PREFIX" "$BINDIR"
cp -a "$REPO_DIR/bin" "$REPO_DIR/sms" "$REPO_DIR/sip" "$REPO_DIR/voice" \
      "$REPO_DIR/firewall" \
      "$REPO_DIR/docs" "$REPO_DIR/systemd" "$REPO_DIR/install" \
      "$REPO_DIR/README.md" "$REPO_DIR/Makefile" "$PREFIX/"
cp -a "$REPO_DIR/tests" "$PREFIX/" 2>/dev/null || true

ln -sf "$PREFIX/bin/phone" "$BINDIR/phone"
info "linked $BINDIR/phone"

# Config directory for the SMS daemon and (optionally) the SIP client.
if [ "$USER_MODE" -eq 1 ]; then
    CONF_DIR="$HOME/.config/phone"
else
    CONF_DIR="/etc/phone"
fi
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
if [ ! -f "$CONF_DIR/sms.conf" ]; then
    cp "$PREFIX/sms/sms.conf.example" "$CONF_DIR/sms.conf"
    chmod 600 "$CONF_DIR/sms.conf"
    info "wrote $CONF_DIR/sms.conf (defaults; edit to taste)"
else
    info "kept existing $CONF_DIR/sms.conf"
fi

# The spool, owner-only.
SPOOL="${SPOOL_DIR:-$HOME/sms/incoming}"
mkdir -p "$SPOOL"
chmod 700 "$SPOOL"
info "spool directory: $SPOOL"

if [ "$WITH_SYSTEMD" -eq 1 ] && [ -d /etc/systemd/system ]; then
    info "installing systemd units"
    for unit in phone-smsd.service phone-sms-poll.service; do
        install -m 0644 "$PREFIX/systemd/$unit" "/etc/systemd/system/$unit"
        if [ -n "$SERVICE_USER" ]; then
            sed -i "s/^User=.*/User=$SERVICE_USER/;s/^Group=.*/Group=$SERVICE_USER/;s#^ReadWritePaths=.*#ReadWritePaths=$HOME/sms#" \
                "/etc/systemd/system/$unit"
        fi
        info "  /etc/systemd/system/$unit"
    done
    systemctl daemon-reload 2>/dev/null || true
    info "review the units (especially User= and ReadWritePaths=), then:"
    info "  systemctl enable --now phone-smsd"
fi

info "done. Try:"
info "  phone doctor"
info "  phone smsd &"
info "  phone sms list"
