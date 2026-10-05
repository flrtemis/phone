#!/usr/bin/env bash
# phone sip: create the client certificate pjsua presents to your provider.
#
# Most SIP-over-TLS providers either require mutual TLS or accept it, and
# pjsua's --tls-cert-file / --tls-privkey-file want a PEM certificate and a
# separate unencrypted PEM key. This generates both, plus a combined CA bundle
# if you have a provider-specific CA to add on top of the system roots.
#
#   sip/gen-certs.sh [--force] [--cn NAME] [--bits 2048]
#
# Output (mode-controlled, never world readable):
#   $PHONE_TLS_DIR/client.pem   certificate  (0644)
#   $PHONE_TLS_DIR/client.key   private key  (0600)
#   $PHONE_TLS_DIR/ca-bundle.pem  system roots [+ extra-ca.pem] (0644)

set -euo pipefail

TLS_DIR="${PHONE_TLS_DIR:-$HOME/.config/phone/tls}"
CN="${PHONE_CERT_CN:-phone-terminal}"
BITS="${PHONE_KEY_BITS:-2048}"
DAYS="${PHONE_CERT_DAYS:-3650}"
SYSTEM_CA="/etc/ssl/certs/ca-certificates.crt"
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f) FORCE=1; shift ;;
        --cn)   CN="${2:?--cn needs a value}"; shift 2 ;;
        --bits) BITS="${2:?--bits needs a value}"; shift 2 ;;
        --days) DAYS="${2:?--days needs a value}"; shift 2 ;;
        -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

command -v openssl >/dev/null 2>&1 || { echo "error: openssl not found" >&2; exit 1; }

mkdir -p "$TLS_DIR"
chmod 700 "$TLS_DIR"

KEY="$TLS_DIR/client.key"
CERT="$TLS_DIR/client.pem"
BUNDLE="$TLS_DIR/ca-bundle.pem"

for file in "$KEY" "$CERT"; do
    if [ -e "$file" ] && [ "$FORCE" -ne 1 ]; then
        echo "error: $file exists (use --force to replace; the old file will be backed up)" >&2
        exit 1
    fi
    [ -e "$file" ] && cp -p "$file" "$file.bak.$(date -u +%Y%m%dT%H%M%SZ)"
done

echo "generating a ${BITS}-bit RSA client certificate for CN=$CN (valid $DAYS days)" >&2
umask 077
openssl req -x509 -newkey "rsa:$BITS" -sha256 -days "$DAYS" -nodes \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=$CN" \
    -addext "keyUsage = digitalSignature, keyEncipherment" \
    -addext "extendedKeyUsage = clientAuth" \
    -addext "basicConstraints = critical,CA:FALSE" 2>/dev/null

chmod 600 "$KEY"
chmod 644 "$CERT"

# Trust store for --tls-ca-file. PJLIB's OpenSSL backend uses only what this
# file contains, so build it explicitly instead of relying on system defaults.
{
    if [ -f "$SYSTEM_CA" ]; then cat "$SYSTEM_CA"; else echo >&2 "warning: $SYSTEM_CA missing"; fi
    if [ -f "$TLS_DIR/extra-ca.pem" ]; then cat "$TLS_DIR/extra-ca.pem"; fi
} > "$BUNDLE"
chmod 644 "$BUNDLE"
rm -f "$BUNDLE.tmp"

# Sanity checks: the pair must match, and the certificate must be valid now.
key_pub="$(openssl x509 -in "$CERT" -noout -pubkey | openssl sha256)"
cert_pub="$(openssl rsa -in "$KEY" -pubout 2>/dev/null | openssl sha256)"
if [ "$key_pub" != "$cert_pub" ]; then
    echo "error: generated key does not match certificate" >&2
    exit 1
fi
openssl x509 -in "$CERT" -noout -checkend 86400 >/dev/null || { echo "error: certificate is not valid" >&2; exit 1; }

echo "  ok $CERT ($(openssl x509 -in "$CERT" -noout -fingerprint -sha256 | cut -d= -f2))" >&2
echo "  ok $KEY (mode 600)" >&2
echo "  ok $BUNDLE ($(grep -c 'BEGIN CERTIFICATE' "$BUNDLE") roots)" >&2
echo >&2
echo "point your config at them:" >&2
echo "  --tls-ca-file $BUNDLE" >&2
echo "  --tls-cert-file $CERT" >&2
echo "  --tls-privkey-file $KEY" >&2
echo >&2
echo "your provider may require you to upload $CERT before it trusts the client." >&2
