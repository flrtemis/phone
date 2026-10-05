#!/usr/bin/env bash
# phone sip provision -- fill in your account details without hand-editing.
#
# Writes ~/.config/phone/sip.conf from the hardened template plus a
# provider-specific account block, substituting your username, domain,
# password and DID. It never sends anything anywhere: this configures the
# client, it does not talk to the provider.
#
#   sip/provision.sh --provider telnyx --username 1234567 \
#                    --password-stdin --did +15550109999
#   sip/provision.sh --provider flowroute --username 12345678 \
#                    --password-env MY_SIP_PW --domain sip.flowroute.com
#   sip/provision.sh --show-providers
#   sip/provision.sh --list-providers
#
# Password handling: prefer --password-stdin or --password-env. `--password`
# puts the secret in your shell history and in this process's argv (visible in
# `ps`), so it is accepted but discouraged. Whatever you choose, the resulting
# config file is mode 0600 and `phone sip --check` refuses to run otherwise.
#
# Values containing `#` or spaces are written double-quoted, because pjsua's
# config reader treats an unquoted `#` as the start of a comment and would
# silently truncate your password.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/pjsua.conf.example"
PROVIDER_DIR="$SCRIPT_DIR/providers"
CONF="${PHONE_SIP_CONF:-$HOME/.config/phone/sip.conf}"

PROVIDER=""
USERNAME=""
DOMAIN=""
PASSWORD=""
DID=""
REGISTRAR=""
REALM=""
FORCE=0
PASSWORD_FROM=""

info() { printf '[*] %s\n' "$*" >&2; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --provider)       PROVIDER="${2:?}"; shift 2 ;;
        --username|--user) USERNAME="${2:?}"; shift 2 ;;
        --domain|--registrar-host) DOMAIN="${2:?}"; shift 2 ;;
        --registrar)      REGISTRAR="${2:?}"; shift 2 ;;
        --realm)          REALM="${2:?}"; shift 2 ;;
        --did|--number)   DID="${2:?}"; shift 2 ;;
        --password)       PASSWORD="${2:?}"; PASSWORD_FROM="flag"; shift 2 ;;
        --password-env)   PASSWORD="${!2:-}"; [ -n "$PASSWORD" ] || die "environment variable $2 is empty or unset"; PASSWORD_FROM="env:$2"; shift 2 ;;
        --password-stdin) PASSWORD="$(cat)"; PASSWORD_FROM="stdin"; shift ;;
        --conf)           CONF="${2:?}"; shift 2 ;;
        --force|-f)       FORCE=1; shift ;;
        --list-providers) ls -1 "$PROVIDER_DIR" 2>/dev/null | sed 's/\.conf$//' ; exit 0 ;;
        --show-providers)
            for f in "$PROVIDER_DIR"/*.conf; do
                [ -f "$f" ] || continue
                printf '%s\n' "$(basename "$f" .conf)"
                sed -n 's/^# \?//p' "$f" | sed -n '1,6p' | sed 's/^/    /'
                printf '\n'
            done
            exit 0 ;;
        -h|--help)        usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

# --------------------------------------------------------------------------
# Resolve provider defaults
# --------------------------------------------------------------------------
if [ -n "$PROVIDER" ]; then
    PEOF="$PROVIDER_DIR/${PROVIDER}.conf"
    [ -f "$PEOF" ] || die "no provider block for '$PROVIDER' (see: $0 --list-providers)"
    # shellcheck disable=SC1090
    . "$PEOF"   # provides PROVIDER_REGISTRAR, PROVIDER_REALM, PROVIDER_DOMAIN, PROVIDER_NOTE
    : "${PROVIDER_REGISTRAR:?provider block is missing PROVIDER_REGISTRAR}"
    [ -n "$DOMAIN" ] || DOMAIN="${PROVIDER_DOMAIN:-}"
    [ -n "$REGISTRAR" ] || REGISTRAR="$PROVIDER_REGISTRAR"
    [ -n "$REALM" ] || REALM="${PROVIDER_REALM:-*}"
fi

[ -n "$TEMPLATE" ] && [ -f "$TEMPLATE" ] || die "template not found: $TEMPLATE"
[ -n "$REGISTRAR" ] || die "no registrar: pass --provider <name> or --registrar sip:host:5061;transport=tls"
[ -n "$USERNAME" ]  || die "no username: pass --username"
[ -n "$PASSWORD" ]  || die "no password: pass --password-stdin, --password-env VAR, or --password"

# A DID is what people dial; providers usually register the account instead.
# Validate the shape rather than guessing.
if [ -n "$DID" ]; then
    case "$DID" in
        +[0-9]*) : ;;
        *) warn "DID '$DID' does not start with '+': use E.164 (+1...) or your provider may reject the caller ID" ;;
    esac
fi

# --------------------------------------------------------------------------
# Quote values the pjsua parser would otherwise mangle
# --------------------------------------------------------------------------
quote() {
    local value="$1"
    case "$value" in
        *'#'*|*' '*|*'\t'*|*'"'*) printf '"%s"' "$(printf '%s' "$value" | sed 's/"/\\"/g')" ;;
        *) printf '%s' "$value" ;;
    esac
    return 0
}

if [ "${#REGISTRAR}" -gt 190 ] || [ "${#USERNAME}" -gt 190 ] || [ "${#PASSWORD}" -gt 190 ]; then
    die "value too long: pjsua reads config lines into a 200-byte buffer"
fi

# Every URI that must go over TLS needs the transport parameter. Add it if the
# caller (or provider block) did not.
ensure_tls() {
    local uri="$1"
    case "$uri" in
        *transport=tls*|*transport=TLS*) printf '%s' "$uri" ;;
        *\;*|*\;*) printf '%s' "$uri" ;;             # already has parameters
        *) printf '%s;transport=tls' "$uri" ;;
    esac
    return 0
}

REGISTRAR="$(ensure_tls "$REGISTRAR")"
if [ -n "$DOMAIN" ]; then
    ACCOUNT_URI="sip:${USERNAME}@${DOMAIN}"
    CONTACT_URI="$(ensure_tls "sip:${USERNAME}@${DOMAIN}:5061")"
else
    ACCOUNT_URI="sip:${USERNAME}@${REGISTRAR#sip:}"
    ACCOUNT_URI="${ACCOUNT_URI%%;*}"
    CONTACT_URI="${REGISTRAR/sip:/sip:${USERNAME}@}"
fi

# --------------------------------------------------------------------------
# Build the config
# --------------------------------------------------------------------------
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# Start from the hardened template, dropping the placeholder account lines and
# inserting the real account block in their place.
awk '
    /^--registrar sip:sip\.YOUR_PROVIDER/ { print "ACCOUNT_BLOCK_MARKER"; next }
    /^--id sip:YOUR_SIP_USERNAME/          { next }
    /^--realm YOUR_PROVIDER\.example/      { next }
    /^--username YOUR_SIP_USERNAME/        { next }
    /^--password YOUR_SIP_PASSWORD/        { next }
    /^--contact sip:YOUR_SIP_USERNAME/     { next }
    { print }
' "$TEMPLATE" > "$tmp"

account="$(cat <<EOF
# --- account: filled in by sip/provision.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ) ---
# (no secrets are logged anywhere by this project; this file is mode 0600)
--registrar $(quote "$REGISTRAR")
--id $(quote "$ACCOUNT_URI")
--realm $(quote "$REALM")
--username $(quote "$USERNAME")
--password $(quote "$PASSWORD")
--contact $(quote "$CONTACT_URI")
EOF
)"

# Replace the marker line with the account block (awk to keep the multi-line
# block out of sed's escaping rules).
awk -v block="$account" '
    /^ACCOUNT_BLOCK_MARKER$/ { print block; next }
    { print }
' "$tmp" > "$tmp.new"

# Render @HOME@ exactly as `phone sip --init` does; pjsua expands neither ~ nor
# environment variables, so an unsubstituted placeholder is a broken path.
# Prefer the bundle gen-certs.sh builds (system roots + any provider CA) when
# it exists, because PJLIB's OpenSSL backend trusts only what --tls-ca-file
# names and the provider may need their own CA in that list.
USER_BUNDLE="$HOME/.config/phone/tls/ca-bundle.pem"
if [ -f "$USER_BUNDLE" ]; then
    sed -i "s|^--tls-ca-file .*|--tls-ca-file $USER_BUNDLE|" "$tmp.new"
fi
sed -i "s|@HOME@|$HOME|g" "$tmp.new"

if grep -qE '@HOME@|YOUR_SIP_|YOUR_PROVIDER' "$tmp.new"; then
    die "internal error: the generated config still contains a placeholder, which the launcher would reject. Please report this with the template you used."
fi

mkdir -p "$(dirname -- "$CONF")"
chmod 700 "$(dirname -- "$CONF")"

if [ -e "$CONF" ] && [ "$FORCE" -ne 1 ]; then
    cp -p "$CONF" "$CONF.proposed"
    chmod 600 "$CONF.proposed"
    warn "$CONF exists; wrote the new configuration to $CONF.proposed instead"
    warn "review it with:  diff -u $CONF $CONF.proposed"
    warn "then install it:  mv $CONF.proposed $CONF   (or re-run with --force)"
else
    [ -e "$CONF" ] && cp -p "$CONF" "$CONF.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$tmp.new" "$CONF"
    chmod 600 "$CONF"
    info "wrote $CONF (mode 0600)"
fi

# --------------------------------------------------------------------------
# Show what was configured, with the password redacted
# --------------------------------------------------------------------------
printf '\nregistrar : %s\n' "$REGISTRAR"
printf 'account   : %s\n' "$ACCOUNT_URI"
printf 'realm     : %s\n' "$REALM"
printf 'username  : %s\n' "$USERNAME"
printf 'password  : <set via %s, %d chars, not shown>\n' "${PASSWORD_FROM:-unknown}" "${#PASSWORD}"
printf 'contact   : %s\n' "$CONTACT_URI"
[ -n "$DID" ] && printf 'DID       : %s\n' "$DID"
[ -n "${PROVIDER_NOTE:-}" ] && printf '\nnote: %s\n' "$PROVIDER_NOTE"
if [ -n "$PROVIDER" ] && [ -f "$PROVIDER_DIR/${PROVIDER}.conf" ]; then
    printf '\nprovider checklist:\n'
    sed -n 's/^# \?//p' "$PROVIDER_DIR/${PROVIDER}.conf" | sed -n '/checklist/,$p' | tail -n +2 | sed 's/^/  /'
fi

# The config points at a client certificate and a CA bundle. --init generates
# them; provisioning must too, or the launcher (correctly) refuses to start
# because --tls-cert-file names a file that does not exist.
if [ ! -f "$HOME/.config/phone/tls/client.pem" ]; then
    if [ -x "$SCRIPT_DIR/gen-certs.sh" ]; then
        info "no client certificate yet: generating one"
        "$SCRIPT_DIR/gen-certs.sh" >&2 || warn "certificate generation failed; run sip/gen-certs.sh by hand"
    else
        warn "sip/gen-certs.sh not found: create a client certificate before starting"
    fi
fi

printf '\nnext steps:\n'
printf '  1. phone sip check                         # refuses to start if anything is degraded\n'
printf '  2. phone sip doctor --audio                # find your mic/speaker indexes\n'
printf '  3. phone sip test-tls                      # prove TLS reaches the provider\n'
printf '  4. phone sip                               # register\n'
