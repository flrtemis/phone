#!/usr/bin/env bash
# phone sip: prove the provider's TLS endpoint is reachable and verifiable
# BEFORE pjsua is pointed at it, and diagnose media reachability.
#
# Why this exists: pjsua reports a certificate problem as a SIP registration
# timeout, which sends people hunting in the wrong place ("is my password
# wrong?") when the real cause is an incomplete CA bundle, a middlebox, or a
# provider that simply does not listen on 5061 from your address.
#
#   sip/test-tls.sh --host sip.example.com [--port 5061] [--ca FILE]
#                   [--sni NAME] [--media-host HOST] [--media-range 4000-4010]
#
# Exit status: 0 all checks passed, 1 a check failed, 2 usage error.

set -uo pipefail

HOST=""
PORT=5061
CA="${PHONE_TLS_DIR:-$HOME/.config/phone/tls}/ca-bundle.pem"
SNI=""
MEDIA_HOST=""
SIP_PORT=5061
CHECK_MEDIA=0
TIMEOUT=8

while [ $# -gt 0 ]; do
    case "$1" in
        --host)         HOST="${2:?}"; shift 2 ;;
        --port)         PORT="${2:?}"; shift 2 ;;
        --ca)           CA="${2:?}"; shift 2 ;;
        --sni)          SNI="${2:?}"; shift 2 ;;
        --media-host)   MEDIA_HOST="${2:?}"; CHECK_MEDIA=1; shift 2 ;;
        --media-range)  MEDIA_RANGE="${2:?}"; shift 2 ;;
        --timeout)      TIMEOUT="${2:?}"; shift 2 ;;
        -h|--help)      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ -n "$HOST" ] || { echo "error: --host is required (try --help)" >&2; exit 2; }
[ -f "$CA" ] || CA="/etc/ssl/certs/ca-certificates.crt"
[ -f "$CA" ] || { echo "error: no CA bundle found (looked for --ca and the system roots)" >&2; exit 2; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
info() { printf '  --   %s\n' "$1"; }

printf '\033[1msip tls probe\033[0m %s:%s\n' "$HOST" "$PORT"

command -v openssl >/dev/null 2>&1 || { echo "error: openssl is required" >&2; exit 2; }

# ---- 1. DNS ---------------------------------------------------------------
if getent ahostsv4 "$HOST" >/dev/null 2>&1; then
    addresses="$(getent ahostsv4 "$HOST" | awk '{print $1}' | sort -u | tr '\n' ' ')"
    ok "DNS resolves: ${addresses% }"
    # A provider behind multiple addresses needs them all pinned in the firewall.
    if [ "$(printf '%s' "$addresses" | wc -w)" -gt 1 ]; then
        info "pin every address in the firewall, e.g. $(printf -- '--sip-host %s ' $addresses)"
    fi
else
    bad "DNS does not resolve $HOST" "check the registrar hostname your provider gave you"
fi

# ---- 2. TCP reachability --------------------------------------------------
if command -v timeout >/dev/null 2>&1 && timeout "$TIMEOUT" bash -c "exec 3<>/dev/tcp/$HOST/$PORT" 2>/dev/null; then
    ok "TCP $PORT is reachable"
else
    bad "TCP $PORT did not connect" \
        "either the provider is not listening on this port, or your firewall/NAT blocks outbound $PORT.
       Inbound calls also need the provider to reach YOU; see --media-host below."
fi

# ---- 3. TLS handshake + certificate verification --------------------------
SNI_ARG=()
[ -n "$SNI" ] && SNI_ARG=(-servername "$SNI")
[ -n "$SNI" ] || SNI_ARG=(-servername "$HOST")

tls_out="$(timeout "$TIMEOUT" openssl s_client -connect "$HOST:$PORT" \
              -CAfile "$CA" -verify_return_error -verify 5 \
              "${SNI_ARG[@]}" -tls1_2 </dev/null 2>&1)"
tls_rc=$?

if [ "$tls_rc" -eq 0 ]; then
    ok "TLS handshake succeeded and the certificate verifies against $CA"
else
    bad "TLS handshake or verification failed (rc=$tls_rc)" "$(printf '%s' "$tls_out" | grep -m2 -E 'verify error|alert|no peer certificate|Connection' | head -2)"
    case "$tls_out" in
        *"unable to get local issuer certificate"*|*"self-signed certificate"*)
            info "fix: the CA that signed the provider's certificate is missing from $CA."
            info "     drop it in as ${PHONE_TLS_DIR:-$HOME/.config/phone/tls}/extra-ca.pem and re-run sip/gen-certs.sh" ;;
        *"no peer certificate available"*)
            info "the connection closed without presenting a certificate. Typical causes:"
            info "  - a TLS-inspecting middlebox or egress proxy on your network terminating the"
            info "    handshake (check with: openssl s_client -connect $HOST:$PORT -CAfile $CA)"
            info "  - something that is not a TLS listener answering on this port"
            info "  - the provider requires a source IP allow-list entry before it will speak TLS" ;;
        *"Connection reset"*|*"Connection refused"*)
            info "the TLS port accepted a TCP handshake but refused the TLS session; confirm the"
            info "port with your provider's documentation rather than assuming 5061." ;;
    esac
fi

# ---- 4. Certificate details ----------------------------------------------
# Only meaningful if the handshake actually completed: openssl prints the
# protocol it *attempted* even on failure, and reporting "negotiated TLSv1.2"
# for a failed connection is precisely the kind of reassurance this script
# exists to avoid.
if [ "$tls_rc" -eq 0 ]; then
    proto="$(printf '%s' "$tls_out" | awk '/^ *Protocol/{print $3; exit}')"
    cipher="$(printf '%s' "$tls_out" | awk '/^ *Cipher/{print $3; exit}')"
    [ -n "$proto" ] && ok "negotiated ${proto} / ${cipher:-unknown cipher}"

    subject="$(printf '%s' "$tls_out" | openssl x509 -noout -subject 2>/dev/null | sed 's/^subject=//')"
    expiry="$(printf '%s' "$tls_out" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
    [ -n "$subject" ] && info "certificate subject: $subject"
    [ -n "$expiry" ] && info "certificate expires: $expiry"

    # A certificate whose subject does not cover the host you dialled will fail
    # verification inside pjsua even if openssl -verify passed, so show the names.
    sans="$(printf '%s' "$tls_out" | openssl x509 -noout -text 2>/dev/null |
            awk '/X509v3 Subject Alternative Name/{getline; gsub(/^ +/,""); print; exit}')"
    [ -n "$sans" ] && info "subjectAltName: $sans"
fi

# ---- 5. Does the provider speak SIP over this TLS channel? ----------------
# A bare TLS port answers with an empty stream after the handshake; a real SIP
# endpoint waits for us to speak first. Sending a REQUEST (or OPTIONS) tells us
# whether anything is listening that speaks SIP, without authenticating.
if [ "$tls_rc" -eq 0 ]; then
    sip_reply="$(timeout "$TIMEOUT" openssl s_client -connect "$HOST:$PORT" -quiet \
                  "${SNI_ARG[@]}" -CAfile "$CA" 2>/dev/null <<'EOF'
OPTIONS sip:probe@localhost SIP/2.0
Via: SIP/2.0/TLS probe.invalid:1;branch=z9hG4bKprobe
Max-Forwards: 0
From: <sip:probe@probe.invalid>;tag=probe
To: <sip:probe@localhost>
Call-ID: probe@phone.invalid
CSeq: 1 OPTIONS
Content-Length: 0

EOF
)"
    case "$sip_reply" in
        *"SIP/2.0"*) ok "the endpoint answered a SIP OPTIONS probe ($(printf '%s' "$sip_reply" | head -1 | tr -d '\r'))" ;;
        "") bad "no SIP response to an unauthenticated OPTIONS probe" \
               "TLS works but the port did not answer a SIP request. Some providers only answer
       authenticated requests, so this is not proof of a problem - but if registration also
       fails, check that your account is enabled for TLS on this port." ;;
        *)  info "endpoint replied: $(printf '%s' "$sip_reply" | head -1 | tr -d '\r')" ;;
    esac
fi

# ---- 6. Media path -------------------------------------------------------
if [ "$CHECK_MEDIA" -eq 1 ]; then
    printf '\033[1mmedia path\033[0m %s %s\n' "$MEDIA_HOST" "${MEDIA_RANGE:-4000-4010}"
    if getent ahostsv4 "$MEDIA_HOST" >/dev/null 2>&1; then
        ok "$MEDIA_HOST resolves ($(getent ahostsv4 "$MEDIA_HOST" | awk '{print $1}' | sort -u | tr '\n' ' '))"
        info "pin it too, or SRTP will be dropped by your own firewall:"
        info "  phone firewall --sip-host $HOST --sip-host $MEDIA_HOST ..."
    else
        bad "$MEDIA_HOST does not resolve" "the media hostname is usually different from the registrar"
    fi

    # UDP is connectionless, so there is nothing to "connect" to: report the
    # local sockets and the firewall rule that must exist instead.
    if command -v ss >/dev/null 2>&1; then
        info "local UDP sockets in ${MEDIA_RANGE:-4000-4010} (SRTP media appears here during a call):"
        ss -lun 2>/dev/null | awk -v range="${MEDIA_RANGE:-4000-4010}" '
            BEGIN { split(range, r, "-") }
            $0 ~ /^udp|^UNCONN|^State/ { next }
            { n = split($5, a, ":"); port = a[n] + 0; if (port >= r[1] && port <= r[2]) print "       " $0 }' || true
    fi
    info "verify the matching firewall rule exists:"
    if command -v nft >/dev/null 2>&1 && nft list table inet phone >/dev/null 2>&1; then
        nft list chain inet phone output 2>/dev/null | grep -E "dport ${MEDIA_RANGE:-4000-4010}" | sed 's/^/       /' \
            || info "       no SRTP rule found in the phone output chain - audio will be blocked"
    fi
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
