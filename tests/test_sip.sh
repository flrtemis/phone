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
# Exact option names from long_options[] in
# pjsip-apps/src/pjsua/pjsua_app_config.c. Anything not in this list aborts
# pjsua at start-up, so the template must not contain it.
KNOWN_OPTIONS="config-file log-file log-level app-log-level log-append color no-color \
light-bg no-stderr help version clock-rate snd-clock-rate stereo null-audio local-port \
ip-addr bound-addr no-tcp no-udp norefersub no-supported-norefersub keep-call-on-tsx-fail \
proxy outbound registrar reg-timeout publish mwi use-100rel use-ims id contact \
contact-params contact-uri-params reg-contact-params reg-contact-uri-params auto-update-nat \
disable-stun use-compact-form accept-redirect no-force-lr realm username password aka-op \
aka-amf rereg-delay reg-use-proxy nameserver server-failover stun-srv upnp add-buddy \
offer-x-ms-msg no-presence auto-answer auto-play auto-play-hangup auto-rec auto-loop \
auto-conf play-file play-tone rec-file rtp-port use-ice ice-regular ice-trickle \
ice-max-hosts ice-no-rtcp use-turn turn-srv turn-tcp turn-tls turn-tls-ca-file \
turn-tls-cert-file turn-tls-privkey-file turn-tls-privkey-pwd turn-tls-neg-timeout \
turn-tls-cipher turn-tls-verify-server turn-user turn-passwd rtcp-mux rtcp-xr use-srtp \
srtp-secure srtp-keying add-codec dis-codec complexity quality ptime no-vad ec-tail ec-opt \
ilbc-mode rx-drop-pct tx-drop-pct next-account next-cred max-calls duration thread-cnt \
use-tls tls-ca-file tls-cert-file tls-privkey-file tls-password tls-verify-server \
tls-verify-client tls-neg-timeout tls-cipher capture-dev playback-dev capture-lat \
playback-lat stdout-refresh stdout-refresh-text stdout-no-buf snd-auto-close no-tones \
jb-max-size ipv6 set-qos no-mci use-timer timer-se timer-min-se outb-rid video text \
text-red extra-audio vcapture-dev vrender-dev play-avi auto-play-avi rec-avi rec-avi-size \
rec-avi-audio auto-rec-avi use-cli cli-telnet-port no-cli-console server-affinity custom-sdp"


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

# Fill in credentials the way a user would. Order matters: collapse the
# qualified 'sip.YOUR_PROVIDER.example' first, or the generic rule turns it
# into 'sip.sip.example.net' and later assertions silently stop matching.
sed -i \
    -e 's/YOUR_SIP_USERNAME/terminal7/g' \
    -e 's/YOUR_SIP_PASSWORD/CorrectHorseBatteryStaple/g' \
    -e 's/sip\.YOUR_PROVIDER\.example/sip.example.net/g' \
    -e 's/YOUR_PROVIDER\.example/sip.example.net/g' \
    "$CONF_PATH"
sed -i "s|--tls-ca-file /etc/ssl/certs/ca-certificates.crt|--tls-ca-file $HOME/.config/phone/tls/ca-bundle.pem|" "$CONF_PATH"

check "--check passes on a fully configured, hardened config" './sip/phone.sh --check'
check "--print shows the pjsua invocation and config file" './sip/phone.sh --print | grep -q -- --config-file'

# ---- launcher: every refusal ---------------------------------------------
# A pristine copy of the configured tree, so the parser section below can
# mutate and restore deterministically.
rm -rf "$TMP/pristine"; cp -a "$HOME/.config/phone" "$TMP/pristine"

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

printf 'parser compatibility (read_config_file() in pjsua_app_config.c)\n'
# It splits on whitespace, ends the line at '#', and does NO backslash
# unescaping. Escaping the semicolon in a URI therefore leaks a literal
# backslash into the URI and the transport parameter is misparsed.
check "no backslash-escaped semicolons in URIs" \
    '! grep -vE "^#" "$CONF" | grep -qE "\\\\;"' \
    "pjsua does not unescape backslashes: write a literal ;"
check "registrar URI carries the TLS transport parameter" \
    'grep -E "^--registrar" "$CONF" | grep -q ";transport=tls"'
check "contact URI carries the TLS transport parameter" \
    'grep -E "^--contact" "$CONF" | grep -q ";transport=tls"'
check "every config line fits pjsua's 200-byte read buffer" \
    '[ "$(awk "length(\$0) > 199" "$CONF" | wc -l)" -eq 0 ]'
check "no accidentally unquoted '#' inside a value" \
    '! grep -vE "^#" "$CONF" | grep -qE "^--[a-z-]+ +[^\"#]*#"'

# The launcher must catch each of those before pjsua does.
rm -rf "$HOME/.config/phone"; cp -a "$TMP/pristine" "$HOME/.config/phone"
check "launcher accepts the corrected template" './sip/phone.sh --check'

cp -a "$HOME/.config/phone" "$TMP/keep"
python3 - "$CONF_PATH" <<'PYEOF'
import pathlib, re, sys
# Escape the first semicolon of the registrar URI: exactly the mistake
# a user makes when told (wrongly) that ';' must be escaped in a pjsua
# config file. pjsua does no backslash unescaping, so the URI breaks.
path = pathlib.Path(sys.argv[1])
text = path.read_text()
new, count = re.subn(r"^(--registrar .*?);", r"\1\\;", text, count=1, flags=re.M)
assert count == 1, "fixture drifted: no --registrar line to mutate"
path.write_text(new)
PYEOF
out="$(./sip/phone.sh --check 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "unescape"; then
    ok "refuses an escaped semicolon in a URI"
else
    bad "refuses an escaped semicolon in a URI" "$(printf '%s' "$out" | head -1)"
fi
rm -rf "$HOME/.config/phone"; cp -a "$TMP/keep" "$HOME/.config/phone"

sed -i 's|^--password .*|--password pass#word|' "$CONF_PATH"
out="$(./sip/phone.sh --check 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "comment"; then
    ok "refuses an unquoted '#' in the password (would be truncated)"
else
    bad "refuses an unquoted '#' in the password" "$(printf '%s' "$out" | head -1)"
fi
rm -rf "$HOME/.config/phone"; cp -a "$TMP/keep" "$HOME/.config/phone"

sed -i 's|^--password .*|--password "pass#word"|' "$CONF_PATH"
check "accepts a quoted '#' in the password" './sip/phone.sh --check'
rm -rf "$HOME/.config/phone"; cp -a "$TMP/keep" "$HOME/.config/phone"

python3 - "$CONF_PATH" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
text = p.read_text()
p.write_text(text.replace("--reg-timeout 300", "--reg-timeout 300 # " + "x" * 200))
PYEOF
out="$(./sip/phone.sh --check 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "200-byte"; then
    ok "refuses a line that would be truncated by the 200-byte buffer"
else
    bad "refuses a line longer than pjsua's read buffer" "$(printf '%s' "$out" | head -1)"
fi
rm -rf "$HOME/.config/phone"; cp -a "$TMP/keep" "$HOME/.config/phone"

printf '\nprovisioning\n'
check "the provider directory lists known providers" './sip/provision.sh --list-providers | grep -q telnyx'
check "provisioning writes a config the launcher accepts" \
    'PHONE_SIP_CONF="$TMP/prov.conf" ./sip/provision.sh --provider telnyx --username 1234567 --password-environment-does-not-exist 2>/dev/null; true'
out="$(printf 'secret\n' | PHONE_SIP_CONF="$TMP/prov.conf" ./sip/provision.sh --provider telnyx --username 1234567 --password-stdin --force 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && [ "$(stat -c %a "$TMP/prov.conf")" = "600" ]; then
    ok "provision fills in a provider block (mode 600)"
else
    bad "provision fills in a provider block" "$(printf '%s' "$out" | tail -2)"
fi
check "provisioned config carries the TLS transport parameter" 'grep -q ";transport=tls" "$TMP/prov.conf"'
check "provisioned config has no placeholders left" \
    '! grep -qE "@HOME@|YOUR_SIP_|YOUR_PROVIDER" "$TMP/prov.conf"'
check "provisioned TLS paths are absolute and expanded" \
    '! grep -q "@HOME@" "$TMP/prov.conf" && grep -q "^--tls-cert-file /" "$TMP/prov.conf"'
check "provisioned config passes the launcher's own checks" \
    'PHONE_SIP_CONF="$TMP/prov.conf" ./sip/phone.sh --check'
check "provision redacts the password in its output" \
    '! printf "%s" "$out" | grep -q "secret"'
check "provision quotes a password containing '#'" \
    'printf "pa#ss\n" | PHONE_SIP_CONF="$TMP/prov2.conf" ./sip/provision.sh --provider telnyx --username u --password-stdin --force >/dev/null 2>&1; grep -q "^--password \"pa#ss\"" "$TMP/prov2.conf"'

printf '\ndoctor\n'
doctor_out="$(./sip/phone.sh doctor 2>&1)"; doctor_rc=$?
if [ "$doctor_rc" -eq 0 ] && printf '%s' "$doctor_out" | grep -qi "audio devices"; then
    ok "doctor runs the checks and reports the audio section"
else
    bad "doctor runs the checks and reports the audio section" \
        "rc=$doctor_rc | $(printf '%s' "$doctor_out" | grep -E '^(error|  !!)' | head -2 | tr '\n' ' ')"
fi
if printf '%s' "$doctor_out" | grep -qiE "no /dev/snd|/dev/snd exists"; then
    ok "doctor reports the audio hardware state honestly"
else
    bad "doctor reports the audio hardware state honestly" "$(printf '%s' "$doctor_out" | tail -2 | tr '\n' ' ')"
fi
check "doctor exits 0 even on a machine with no sound hardware" '[ "$doctor_rc" -eq 0 ]'
check "doctor explains how to select the microphone and speakers" \
    'printf "%s" "$doctor_out" | grep -q -- "--capture-dev"'
check "doctor tells you how to probe pjsua's own device indexes" \
    'printf "%s" "$doctor_out" | grep -q -- "--audio"'
check "doctor never places a call or registers" \
    'printf "%s" "$doctor_out" | grep -qivE "^\\[.*Registration|starting pjsua"'


printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
