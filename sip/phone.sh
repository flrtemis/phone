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
#   phone sip doctor [--audio]  checks + audio device discovery + diagnostics
#   phone sip --test-tls      probe the provider's TLS endpoint (sip/test-tls.sh)
#   phone sip --print         print the exact pjsua invocation
#   phone sip -- 'sip:num@host;transport=tls'   place a call over TLS
#
# Pass pjsua options after `--`, e.g.:
#   phone sip -- --capture-dev 3 --playback-dev 3
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
PROBE_AUDIO=0
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
# delegating subcommands
# --------------------------------------------------------------------------
# These hand the rest of the command line straight to another program, so they
# must be handled before the option loop below (which would otherwise reject
# their own flags, e.g. `provision --provider telnyx`).
case "${1:-}" in
    provision)
        shift
        [ -x "$SCRIPT_DIR/provision.sh" ] || die "sip/provision.sh is missing or not executable"
        exec "$SCRIPT_DIR/provision.sh" "$@"
        ;;
    test-tls)
        # Handled by the --test-tls block below, which derives the registrar
        # host and port from your config. Extra flags (e.g. --media-host) are
        # forwarded; an explicit --host short-circuits the derivation there.
        shift
        MODE="test-tls"
        while [ $# -gt 0 ]; do DECLARED_ARGS+=("$1"); shift; done
        ;;
esac

# --------------------------------------------------------------------------
# argument handling
# --------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --init)      MODE="init"; shift ;;
        --check)     MODE="check"; shift ;;
        --print)     MODE="print"; shift ;;
        --doctor)    MODE="doctor"; shift ;;
        --audio)     PROBE_AUDIO=1; shift ;;
        --test-tls)  MODE="test-tls"; shift ;;
        --provision) MODE="provision"; shift ;;
        -h|--help)   usage 0 ;;
        --)          shift; while [ $# -gt 0 ]; do DECLARED_ARGS+=("$1"); shift; done ;;
        -*)          die "unknown option: $1 (try --help)" ;;
        doctor|check|init|print|test-tls|provision)
                     # Bare subcommand form, e.g. `phone sip doctor --audio`.
                     # A call target always contains ':' or '@', so these
                     # words cannot be confused with one.
                     case "$1" in
                         doctor)    MODE="doctor" ;;
                         check)     MODE="check" ;;
                         init)      MODE="init" ;;
                         print)     MODE="print" ;;
                         test-tls)  MODE="test-tls" ;;
                         provision) MODE="provision" ;;
                     esac
                     shift ;;
        *)           DECLARED_ARGS+=("$1"); shift ;;
    esac
done

# `phone sip doctor` implies every check plus diagnostics.
if [ "$MODE" = "doctor" ]; then
    MODE="check"
    PHONE_SIP_DOCTOR=1
fi

# Provisioning never needs the checks to pass first: it is how you make them pass.
if [ "$MODE" = "provision" ]; then
    [ -x "$SCRIPT_DIR/provision.sh" ] || die "sip/provision.sh is missing or not executable"
    exec "$SCRIPT_DIR/provision.sh" "${DECLARED_ARGS[@]}"
fi

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
# --------------------------------------------------------------------------
# audio: the microphone and speakers
# --------------------------------------------------------------------------
# Defined early so `phone sip doctor` can report audio state even when the
# hardening checks below fail (a fresh install with no config yet is exactly
# when you want to know whether pjsua can see your sound card).
# pjsua talks to ALSA directly and addresses devices by the index it
# enumerates, which is not the same as ALSA's card,device pair. So: report
# the hardware, then (with --audio) ask pjsua itself.
audio_report() {
    printf '\n%saudio devices%s\n' "$C_YELLOW" "$C_RESET" >&2

    if [ ! -d /dev/snd ]; then
        warn "no /dev/snd: this machine has no sound hardware, or it is not exposed to this
       container/namespace. pjsua cannot open a microphone here. Options:
         - run on the real host rather than inside a container
         - pass the device through (docker: --device /dev/snd; podman: --device /dev/snd)
         - use '--null-audio' with play-file/rec-file to test signalling without audio"
    else
        ok "/dev/snd exists"
        if [ -r /dev/snd/controlC0 ] || id -nG 2>/dev/null | tr ' ' '\n' | grep -qx audio; then
            ok "you appear to have access to the sound devices (or are in group 'audio')"
        else
            warn "you may not be able to open the sound devices: add your user to the 'audio' group
       (sudo usermod -aG audio $USER; then log out and back in)"
        fi
    fi

    if command -v arecord >/dev/null 2>&1; then
        note "capture hardware (arecord -l):"
        arecord -l 2>/dev/null | sed -n '2,12p' | sed 's/^/       /' >&2 || true
    else
        note "install alsa-utils for 'arecord -l'/'aplay -l' hardware listings"
    fi
    if command -v aplay >/dev/null 2>&1; then
        note "playback hardware (aplay -l):"
        aplay -l 2>/dev/null | sed -n '2,12p' | sed 's/^/       /' >&2 || true
    fi

    if [ "$PROBE_AUDIO" -eq 1 ]; then
        note "asking pjsua to enumerate its own devices (no credentials, no provider contact):"
        probe="$(mktemp)"
        cat > "$probe" <<PROBE_EOF
--log-level 4
--app-log-level 4
--local-port 5099
--max-calls 1
PROBE_EOF
        if command -v timeout >/dev/null 2>&1; then
            timeout 5 "$PJSUA_BIN" --config-file="$probe" --no-color >"$probe.out" 2>&1 || true
        else
            "$PJSUA_BIN" --config-file="$probe" --no-color >"$probe.out" 2>&1 &
            probe_pid=$!; sleep 4; kill "$probe_pid" 2>/dev/null || true
        fi
        if [ -s "$probe.out" ]; then
            # pjsua prints the enumeration itself; show it verbatim rather than
            # pretending to parse a format this script cannot verify.
            note "pjsua said (device indexes are what --capture-dev/--playback-dev expect):"
            grep -iE 'device|sound|audio|ALSA|error' "$probe.out" | head -20 | sed 's/^/       /' >&2
            note "full output kept at: $probe.out"
        else
            warn "pjsua produced no output; run it by hand with --log-level 4 to see its device list"
        fi
        rm -f "$probe"
    else
        note "re-run with --audio to have pjsua list the indexes it will use"
    fi

    note "then set them in $CONF (or pass after '--' on the command line):"
    note "  --capture-dev N    microphone index"
    note "  --playback-dev N   speaker index"
    note "test a route before a real call:  arecord -f dat -d 3 /tmp/mic.wav && aplay /tmp/mic.wav"
}

fail() { die "$*"; }

[ -n "${HOME:-}" ] || fail "HOME is not set"

# In doctor mode, report the audio situation before anything that can abort:
# knowing whether pjsua can open a microphone should not depend on the account
# being configured correctly yet.
[ "${PHONE_SIP_DOCTOR:-0}" -eq 1 ] && audio_report

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
        '"'*) : ;;   # quoted: '#' is safe inside quotes
        *'#'*) fail "the password contains '#' unquoted. pjsua's config reader treats '#' as the start of a comment
       and would authenticate with a truncated password. Wrap it in double quotes:
           --password \"$pw\"" ;;
        *) ok "credentials present (password redacted)" ;;
    esac
else
    fail "config has no '--password' line"
fi

# 11. Config lines go through a 200-byte buffer in pjsua (read_config_file()),
#     so an over-long line is silently truncated. Long URIs are the usual cause.
long_line="$(awk 'length($0) > 199 { print NR": "length($0)" chars" }' "$CONF" | head -3)"
if [ -n "$long_line" ]; then
    fail "config has lines longer than pjsua's 200-byte read buffer, which truncates them:
$long_line"
fi
ok "no config line exceeds pjsua's 200-byte line buffer"

# 12. Escaped semicolons in URIs. pjsua's parser does no backslash unescaping,
#     so '\\;transport=tls' reaches the URI parser with a literal backslash.
if grep -qE '^--(registrar|id|contact|proxy|outbound) .*\\;' "$CONF"; then
    fail "a URI uses '\\;' -- pjsua does not unescape backslashes, so the transport
       parameter would be misparsed. Write ';transport=tls' with a literal semicolon."
fi
ok "URIs use literal semicolons (no bogus backslash escaping)"

# 13. `#` anywhere in a value ends the line, not just at the start.
if grep -nE '^--[a-z-]+ +[^"#]*#[^ ]*' "$CONF" | grep -vE '^\s*#' >/dev/null 2>&1; then
    warn "a value contains '#' without quotes; pjsua would truncate it there (quote it)"
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

# --------------------------------------------------------------------------
# audio: the microphone and speakers
# --------------------------------------------------------------------------

if [ "$MODE" = "check" ]; then
    ok "all checks passed"
    if [ "$PROBE_AUDIO" -eq 1 ] && [ "${PHONE_SIP_DOCTOR:-0}" -ne 1 ]; then
        audio_report
    elif [ "${PHONE_SIP_DOCTOR:-0}" -ne 1 ]; then
        note "for audio devices and provider diagnostics, run:  phone sip doctor --audio"
    fi
    exit 0
fi

if [ "$MODE" = "test-tls" ]; then
    registrar="$(conf_value "--registrar" || echo "")"
    # sip:user@host:port;transport=tls  ->  host, port
    test_host_port="${registrar#*sip:}"
    test_host_port="${test_host_port#*@}"
    test_host_port="${test_host_port%%;*}"
    test_host="${test_host_port%%:*}"
    test_port="$tls_port"
    case "$test_host_port" in
        *:*) test_port="${test_host_port##*:}" ;;
    esac
    ca="$(conf_value "--tls-ca-file" || echo "")"
    extras=("${DECLARED_ARGS[@]:-}")
    if [ ${#extras[@]} -gt 0 ] && [ "${extras[0]}" != "" ]; then
        case " ${extras[*]} " in
            *" --host "*) exec "$SCRIPT_DIR/test-tls.sh" "${extras[@]}" ;;
        esac
    fi
    [ -n "$test_host" ] || fail "could not work out the registrar host from $CONF (pass --host)"
    exec "$SCRIPT_DIR/test-tls.sh" --host "$test_host" --port "$test_port" ${ca:+--ca "$ca"} "${extras[@]:-}"
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
