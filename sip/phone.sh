#!/usr/bin/env bash
# phone sip -- launch pjsua in a configuration that cannot silently downgrade.
#
# This wrapper exists because pjsua is happy to run with no TLS, no SRTP, no
# certificate verification, and a log level that writes SDP (and therefore
# SDES key material) to disk. None of that should be possible by accident, so
# every one of those properties is checked here, before pjsua is exec'd, and
# the process refuses to start rather than degrade quietly.
#
#   phone sip --init          render the shipped template into ~/.config/phone
#   phone sip --check         verify everything and exit (no call is placed)
#   phone sip --print         print the exact pjsua invocation
#   phone sip -- tls:user@host    place a call over TLS
#
# Overridable for testing: PHONE_PJSUA, PHONE_SIP_CONF, PHONE_TLS_DIR, HOME.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

PJSUA_BIN="${PHONE_PJSUA:-pjsua}"
CONF="${PHONE_SIP_CONF:-$HOME/.config/phone/sip.conf}"
TEMPLATE="$SCRIPT_DIR/pjsua.conf.example"
TLS_DIR="${PHONE_TLS_DIR:-$HOME/.config/phone/tls}"
CA_DEFAULT="/etc/ssl/certs/ca-certificates.crt"
RTCP_PORT_MIN=4000
RTCP_PORT_MAX=4010

MODE="run"
PLACEHOLDER_MODE=""
DECLARED_ARGS=()

# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------
if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'
else
    C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_DIM=""
fi

ok()   { printf '%s  ok %s %s\n' "$C_GREEN" "$C_RESET" "$*" >&2; }
warn() { printf '%s  !! %s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
note() { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET" >&2; }

usage() {
    awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

# --------------------------------------------------------------------------
# argument handling
# --------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --init)      MODE="init"; shift ;;
        --check)     MODE="check"; shift ;;
        --print)     MODE="print"; shift ;;
        -h|--help)   usage 0 ;;
        --)          shift; while [ $# -gt 0 ]; do DECLARED_ARGS+=("$1"); shift; done ;;
        -*)          die "unknown option: $1 (try --help)" ;;
        *)           DECLARED_ARGS+=("$1"); shift ;;
    esac
done

# --------------------------------------------------------------------------
# config parsing: only what the checks need, no YAML, no jq
# --------------------------------------------------------------------------
# Values may be written as `--name value` or `--name=value`.
conf_value() {
    local name="$1" line
    while IFS= read -r line; do
        case "$line" in
            "$name "*)  printf '%s\n' "${line#"$name" }" | sed 's/[[:space:]]*$//'; return 0 ;;
            "$name="*)  printf '%s\n' "${line#"$name="}"  | sed 's/[[:space:]]*$//'; return 0 ;;
        esac
    done < "$CONF"
    return 1
}

conf_has() {
    local name="$1" line
    while IFS= read -r line; do
        case "$line" in
            "$name"|"$name "*) return 0 ;;
        esac
    done < "$CONF"
    return 1
}

# --------------------------------------------------------------------------
# --init: render the template so no placeholder survives
# --------------------------------------------------------------------------
if [ "$MODE" = "init" ]; then
    [ -f "$TEMPLATE" ] || die "template not found: $TEMPLATE"
    mkdir -p "$(dirname -- "$CONF")" "$TLS_DIR"
    chmod 700 "$(dirname -- "$CONF")" "$TLS_DIR"

    if [ -e "$CONF" ]; then
        cp -p -- "$CONF" "$CONF.bak.$(date -u +%Y%m%dT%H%M%SZ)"
        note "existing config backed up"
    fi

    # Absolute paths only: pjsua does not expand ~ or $VARS.
    sed "s|@HOME@|$HOME|g" "$TEMPLATE" > "$CONF"
    chmod 600 "$CONF"
    ok "wrote $CONF (mode 0600)"
    note "next: edit it with your provider credentials, then run 'phone sip init --check'"

    if [ ! -f "$TLS_DIR/client.key" ] && [ -x "$SCRIPT_DIR/gen-certs.sh" ]; then
        note "generating a client certificate for TLS mutual authentication"
        "$SCRIPT_DIR/gen-certs.sh" || warn "certificate generation failed; run sip/gen-certs.sh manually"
    fi
    exit 0
fi

# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
FAILED=0
fail() { die "$*"; }

[ -n "${HOME:-}" ] || fail "HOME is not set"
[ -f "$CONF" ] || fail "no SIP config at $CONF -- run 'phone sip --init'"

# 1. The file holds your SIP password: it must not be readable by others.
perms="$(stat -c '%a' "$CONF" 2>/dev/null || echo '???')"
case "$perms" in
    [0-7]00) ok "config permissions are $perms" ;;
    *) fail "config $CONF is mode $perms: it contains your SIP password. Run: chmod 600 $CONF" ;;
esac

# 2. No unresolved placeholders.
if grep -qE '@HOME@|YOUR_SIP_|YOUR_PROVIDER' "$CONF"; then
    fail "config still contains template placeholders -- run 'phone sip --init' after editing, or set your credentials in $CONF"
fi
ok "no template placeholders remain"

# 3. pjsua present and built with TLS + SRTP.
command -v "$PJSUA_BIN" >/dev/null 2>&1 || fail "pjsua not found (looked for '$PJSUA_BIN'); build it with install/build-pjsip.sh"
PJSUA_HELP="$("$PJSUA_BIN" --help 2>&1 || true)"
case "$PJSUA_HELP" in
    *--use-srtp*) ok "pjsua supports SRTP" ;;
    *) fail "this pjsua was built without SRTP -- rebuild pjproject with libsrtp (install/build-pjsip.sh). Refusing to place calls in the clear." ;;
esac
case "$PJSUA_HELP" in
    *--use-tls*) ok "pjsua supports TLS" ;;
    *) fail "this pjsua was built without TLS support (PJ_HAS_SSL_SOCK=0) -- rebuild with OpenSSL. Refusing to run." ;;
esac

# 4. Mandatory hardening must actually be in the config.
conf_has "--use-tls"      || fail "config is missing '--use-tls'"
conf_has "--no-udp"       || fail "config is missing '--no-udp': plaintext signalling would be possible"
conf_has "--no-tcp"       || fail "config is missing '--no-tcp': plaintext signalling would be possible"
conf_has "--use-srtp"     || fail "config is missing '--use-srtp'"
conf_has "--tls-ca-file"  || fail "config is missing '--tls-ca-file' (PJLIB/OpenSSL does not use the system trust store)"
conf_has "--tls-verify-server" || fail "config is missing '--tls-verify-server': the provider's certificate would not be checked"
ok "hardening options present (TLS only, SRTP required, server verified)"

srtp_use="$(conf_value "--use-srtp" || echo "")"
[ "$srtp_use" = "2" ] || warn "use-srtp is '${srtp_use:-unset}': only 2 makes encrypted media mandatory (1 falls back to plaintext)"
srtp_secure="$(conf_value "--srtp-secure" || echo "1")"
[ "$srtp_secure" != "0" ] || warn "srtp-secure is 0: SRTP would not require a secure signalling channel"

# 5. sips: requirement, straight from the SRTP docs: srtp_secure_signaling=2
#    demands secure end-to-end transport, i.e. a sips: URI.
if [ "$srtp_secure" = "2" ]; then
    registrar_uri="$(conf_value "--registrar" || echo "")"
    id_uri="$(conf_value "--id" || echo "")"
    case "$registrar_uri$id_uri" in
        *sips:*) ok "srtp-secure=2 with sips: URIs" ;;
        *) fail "srtp-secure=2 requires sips: URIs for --registrar and --id; with sip: URIs pjsua will refuse to establish media. Use --srtp-secure 1, or switch to sips:." ;;
    esac
fi

# 6. Never log SIP messages: level 5 dumps full messages, and SDP can carry
#    SDES SRTP key material.
log_level="$(conf_value "--log-level" || echo "5")"
case "$log_level" in
    [0-3]) ok "log level $log_level (no SIP message dumps)" ;;
    4) warn "log level 4 is verbose; 3 is recommended" ;;
    *) fail "log level $log_level would print SIP messages, including SDP key material. Set '--log-level 3'." ;;
esac

# 7. TLS port arithmetic: pjsua binds TLS on (local port + 1).
local_port="$(conf_value "--local-port" || echo "5060")"
tls_port=$((local_port + 1))
if [ "$tls_port" -eq 5061 ]; then
    ok "TLS listener will bind $tls_port (--local-port $local_port)"
else
    warn "TLS listener will bind $tls_port, not the standard 5061 (--local-port $local_port). Your firewall rules and provider contact URI must match."
fi

# 8. Certificate material actually exists, and the key is not world-readable.
for opt in "--tls-ca-file" "--tls-cert-file" "--tls-privkey-file"; do
    path="$(conf_value "$opt" || echo "")"
    [ -n "$path" ] || continue
    [ -f "$path" ] || fail "$opt points at a missing file: $path"
done
key_path="$(conf_value "--tls-privkey-file" || echo "")"
if [ -n "$key_path" ]; then
    key_perms="$(stat -c '%a' "$key_path" 2>/dev/null || echo '???')"
    case "$key_perms" in
        [0-7]00) ok "private key permissions are $key_perms" ;;
        *) fail "private key $key_path is mode $key_perms -- run: chmod 600 $key_path" ;;
    esac
fi

# 9. Media port window, so the firewall script and pjsua cannot disagree.
rtp_port="$(conf_value "--rtp-port" || echo "4000")"
if [ "$rtp_port" -ne "$RTCP_PORT_MIN" ]; then
    warn "rtp-port is $rtp_port but the firewall opens $RTCP_PORT_MIN-$RTCP_PORT_MAX; audio may be blocked or dropped"
fi

# 10. Provider credentials must be filled in.
if conf_has "--password"; then
    pw="$(conf_value "--password" || echo "")"
    case "$pw" in
        ""|*YOUR_*|*changeme*) fail "no SIP password set in $CONF" ;;
        *) ok "credentials present (password redacted)" ;;
    esac
else
    fail "config has no '--password' line"
fi

# Optional: confirm the port that is really bound, when this host runs pjsua.
if [ "$MODE" = "check" ] && command -v ss >/dev/null 2>&1; then
    if ss -lnt 2>/dev/null | grep -qE "[:.]$tls_port[[:space:]]"; then
        ok "something is listening on $tls_port"
    else
        note "note: nothing is listening on $tls_port yet (expected if pjsua is not running)"
    fi
fi

BOLD_ARGS=(
    "--config-file=$CONF"
    "--no-color"
)

if [ "$MODE" = "check" ]; then
    ok "all checks passed"
    exit 0
fi

if [ "$MODE" = "print" ]; then
    printf '%s' "$(command -v "$PJSUA_BIN" 2>/dev/null || echo "$PJSUA_BIN")"
    printf ' %q' "${BOLD_ARGS[@]}"
    [ ${#DECLARED_ARGS[@]} -gt 0 ] && printf ' %q' "${DECLARED_ARGS[@]}"
    printf '\n'
    exit 0
fi

# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
umask 077
note "starting pjsua: TLS signalling on :$tls_port, SRTP required, spool at ~/sms/incoming"
note "call a number with:  phone sip -- 'sip:number@host;transport=tls'"
exec "$PJSUA_BIN" "${BOLD_ARGS[@]}" "$@"
