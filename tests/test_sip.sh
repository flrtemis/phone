#!/usr/bin/env bash
# Tests for sip/pjsua.conf.example and sip/phone.sh.
#
# The launcher's whole job is to refuse to start in a degraded configuration,
# so every test here is really "does it refuse?". A stub pjsua stands in for
# the real binary, which means these run anywhere, with no SIP provider.

set -uo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1" "${3:-}"; fi; }

export HOME="$TMP/home"
mkdir -p "$HOME"

# ---- a stub pjsua ---------------------------------------------------------
FAKE_BIN="$TMP/bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/pjsua" <<'STUB'
#!/usr/bin/env bash
# Stub: reports the options the real binary would, and records invocations.
case "${1:-}" in
    --help)
        cat <<'HELP'
Usage: pjsua [options]
SIP account options:
  --use-srtp=N    Use SRTP
  --srtp-secure=N SRTP secure signalling
  --srtp-keying=N Keying method priority
TLS options:
  --use-tls       Enable TLS transport
  --tls-ca-file   CA file
  --tls-cert-file Certificate
  --tls-privkey-file Key
  --tls-verify-server Verify server certificate
Transport options:
  --local-port=port
  --no-udp
  --no-tcp
HELP
        exit 0 ;;
    --version) echo "pjsua 2.15.1-stub"; exit 0 ;;
esac
printf '%s\n' "$*" >> "${STUB_LOG:-/dev/null}"
exit 0
STUB
chmod +x "$FAKE_BIN/pjsua"
export PHONE_PJSUA="$FAKE_BIN/pjsua"

printf 'sip/pjsua.conf.example\n'

# ---- the shipped config must be lint-clean --------------------------------
CONF="$REPO_DIR/sip/pjsua.conf.example"

# Every option pjsua actually has, from the pjsua CLI reference. Anything in
# the template that is not on this list is a typo that would abort start-up.
KNOWN_OPTIONS="config-file log-level app-log-level log-file color no-color \
use-tls tls-ca-file tls-cert-file tls-privkey-file tls-password tls-verify-server \
tls-verify-client tls-neg-timeout tls-srv-name \
use-srtp srtp-secure srtp-keying \
registrar id contact contact-params proxy reg-timeout realm username password \
next-cred publish use-100rel auto-update-nat next-account \
local-port ip-addr bound-addr no-tcp no-udp nameserver outbound stun-srv set-qos ipv6 \
use-ice ice-no-host ice-no-rtcp rtp-port rx-drop-pct tx-drop-pct use-turn turn-srv \
turn-tcp turn-user turn-passwd \
add-codec dis-codec clock-rate snd-clock-rate stereo null-audio play-file play-tone \
auto-play auto-loop auto-conf rec-file auto-rec quality ptime no-vad ec-tail ec-opt \
ilbc-mode capture-dev playback-dev capture-lat playback-lat snd-auto-close no-tones jb-max-size \
add-buddy auto-answer max-calls thread-cnt duration norefersub use-compact-form force-lr \
accept-redirect mwi max-calls no-force-lr"

unknown=""
while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in
        --*) ;;
        *) unknown="$unknown
  not an option: $line"; continue ;;
    esac
    name="${line%% *}"
    name="${name%%=*}"
    name="${name#--}"
    found=0
    for candidate in $KNOWN_OPTIONS; do
        [ "$name" = "$candidate" ] && { found=1; break; }
    done
    [ "$found" -eq 1 ] || unknown="$unknown
  unknown option: --$name"
done < "$CONF"
check "every option in the template is a real pjsua option" '[ -z "$unknown" ]' "$unknown"

for required in "use-tls" "no-udp" "no-tcp" "use-srtp 2" "tls-ca-file" "tls-verify-server" "tls-privkey-file" "log-level 3"; do
    check "template sets '$required'" "grep -qE '^--$(echo "$required" | sed 's/ /[ =]/;s/\([a-z-]*\) [0-9]/\1[ =]/')' '$CONF'" "expected: --$required"
done
check "template omits the non-existent --sip-border" '! grep -q -- "--sip-border" "$CONF"'
check "template does not point pjsua at a STUN server (metadata leak)" \
    '! grep -v "^#" "$CONF" | grep -q -- "--stun-srv"'

# local port 5060 puts TLS on 5061: pjsua binds TLS on local_port + 1
local_port="$(grep -E '^--local-port' "$CONF" | awk '{print $2}')"
check "template uses local port 5060 so TLS lands on 5061" '[ "$local_port" = "5060" ]' "got: $local_port"
check "the port+1 rule is documented in the template" 'grep -q "TCP port+1" "$CONF"'

# ---- launcher: --init -----------------------------------------------------
printf 'sip/phone.sh\n'
LOG="$TMP/stub.log"
export STUB_LOG="$LOG"

cd "$REPO_DIR"
if ./sip/phone.sh --init >"$TMP/init.log" 2>&1; then
    ok "--init renders the template"
else
    bad "--init renders the template" "$(tail -2 "$TMP/init.log")"
fi

CONF_PATH="$HOME/.config/phone/sip.conf"
check "--init writes the config" '[ -f "$CONF_PATH" ]'
check "--init sets mode 600 on a file holding the SIP password" '[ "$(stat -c %a "$CONF_PATH")" = "600" ]'
check "--init expands @HOME@ to the real home" '! grep -q "@HOME@" "$CONF_PATH" && grep -q "$HOME/.config/phone/tls" "$CONF_PATH"'
check "--init generated a client certificate" '[ -f "$HOME/.config/phone/tls/client.pem" ] && [ "$(stat -c %a "$HOME/.config/phone/tls/client.key")" = "600" ]'

# Fill in credentials the way a user would.
sed -i \
    -e 's/YOUR_SIP_USERNAME/terminal7/g' \
    -e 's/YOUR_PROVIDER\.example/sip.example.net/g' \
    -e 's/sip\.YOUR_PROVIDER\.example/sip.example.net/g' \
    -e 's/YOUR_SIP_PASSWORD/CorrectHorseBatteryStaple/g' \
    "$CONF_PATH"
sed -i "s|--tls-ca-file /etc/ssl/certs/ca-certificates.crt|--tls-ca-file $HOME/.config/phone/tls/ca-bundle.pem|" "$CONF_PATH"

check "--check passes on a fully configured, hardened config" './sip/phone.sh --check'
check "--print shows the pjsua invocation and config file" './sip/phone.sh --print | grep -q -- --config-file'

# ---- launcher: every refusal ---------------------------------------------
refuse() { # name, expected-substring, mutation-command
    # Snapshot the whole config tree, not just sip.conf: a mutation that
    # loosens a key's permissions must not leak into the next case, or the
    # following tests would fail for the wrong reason.
    local name="$1" needle="$2" mutate="$3"
    local snapshot="$TMP/snapshot"
    rm -rf "$snapshot"
    cp -a "$HOME/.config/phone" "$snapshot"
    eval "$mutate"
    local out rc
    out="$(./sip/phone.sh --check 2>&1)"; rc=$?
    rm -rf "$HOME/.config/phone"
    cp -a "$snapshot" "$HOME/.config/phone"
    if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qi "$needle"; then
        ok "refuses: $name"
    else
        bad "refuses: $name" "rc=$rc out=$(printf '%s' "$out" | grep -iE '^(error|  !!)' | head -1)"
    fi
}

refuse "world-readable config" "chmod 600" "chmod 644 '$CONF_PATH'"
refuse "unresolved template placeholder" "placeholder" "sed -i 's/terminal7/YOUR_SIP_USERNAME/' '$CONF_PATH'"
refuse "missing --use-tls" "use-tls" "sed -i '/^--use-tls$/d' '$CONF_PATH'"
refuse "missing --no-udp" "no-udp" "sed -i '/^--no-udp$/d' '$CONF_PATH'"
refuse "missing --no-tcp" "no-tcp" "sed -i '/^--no-tcp$/d' '$CONF_PATH'"
refuse "missing --tls-ca-file" "tls-ca-file" "sed -i '/^--tls-ca-file/d' '$CONF_PATH'"
refuse "missing --tls-verify-server" "tls-verify-server" "sed -i '/^--tls-verify-server/d' '$CONF_PATH'"
refuse "log level 5 (would dump SDP key material)" "log level" "sed -i 's/^--log-level 3/--log-level 5/' '$CONF_PATH'"
refuse "srtp-secure 2 with sip: URIs" "sips" "sed -i 's/^--srtp-secure 1/--srtp-secure 2/' '$CONF_PATH'"
refuse "private key group-readable" "chmod 600" "chmod 644 \$(grep '^--tls-privkey-file' '$CONF_PATH' | awk '{print \$2}')"
refuse "missing certificate file" "missing file" "sed -i 's|^--tls-cert-file .*|--tls-cert-file /nonexistent/client.pem|' '$CONF_PATH'"
refuse "empty password" "password" "sed -i 's/^--password .*/--password /' '$CONF_PATH'"
refuse "no config at all" "sip.conf" "mv '$CONF_PATH' '$TMP/moved.conf'"

# restore for the next checks
[ -f "$CONF_PATH" ] || cp -p "$TMP/backup.conf" "$CONF_PATH"

# A pjsua built without SRTP must stop the whole thing.
cat > "$FAKE_BIN/pjsua-nosrtp" <<'STUB'
#!/usr/bin/env bash
[ "${1:-}" = "--help" ] && { echo "Usage: pjsua [options]"; echo "  --use-tls Enable TLS"; exit 0; }
exit 0
STUB
chmod +x "$FAKE_BIN/pjsua-nosrtp"
old_pjsua="$PHONE_PJSUA"
PHONE_PJSUA="$FAKE_BIN/pjsua-nosrtp"
out="$(./sip/phone.sh --check 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "SRTP"; then
    ok "refuses: pjsua built without SRTP support"
else
    bad "refuses: pjsua built without SRTP support" "$(printf '%s' "$out" | head -1)"
fi
PHONE_PJSUA="$old_pjsua"

# A wrong media port must warn, because the firewall opens 4000-4010.
out="$(sed 's/^--rtp-port 4000/--rtp-port 20000/' "$CONF_PATH" > "$TMP/alt.conf"; PHONE_SIP_CONF="$TMP/alt.conf" chmod 600 "$TMP/alt.conf"; PHONE_SIP_CONF="$TMP/alt.conf" ./sip/phone.sh --check 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "4000-4010"; then
    ok "warns when --rtp-port disagrees with the firewall window"
else
    bad "warns when --rtp-port disagrees with the firewall window" "$(printf '%s' "$out" | grep -i rtp | head -1)"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
