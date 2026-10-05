#!/usr/bin/env bash
# phone firewall -- deny everything, then allow exactly what keeps you reachable.
#
# Design rules, in order of importance:
#
#  1. FAIL CLOSED ON PURPOSE. Outbound is default-deny, so there is no rule
#     that says "allow HTTPS to anywhere" (the usual way telemetry, crash
#     reporters, update pings and NTP fingerprinting stay alive). Every peer
#     you talk to is pinned: SIP provider, SMTP... sorry, SMS gateway,
#     resolver. If you do not pin a peer, we will not invent an open rule.
#
#  2. IPv6 IS DROPPED. A v4-only rule set plus a live v6 stack is a leak: a
#     process can reach the network over v6 without ever touching your rules.
#     The ruleset drops all IPv6 except loopback unless --allow-ipv6.
#
#  3. DNS IS PINNED TOO. Allowing port 53 to "any" is how metadata escapes:
#     every lookup is a beacon naming the service you are about to contact.
#     Only your resolver (default 127.0.0.1, e.g. unbound/DoT) is allowed.
#
#  4. VOICE MEDIA MUST ACTUALLY WORK. The pjsua default media window is UDP
#     4000-4010; a rule set that opens 10000-20000 while pjsua sends from 4000
#     black-holes all audio. The default here matches pjsua, and the SIP
#     launcher warns if --rtp-port disagrees.
#
#  5. YOU CANNOT LOCK YOURSELF OUT. Rules are backed up before they are
#     applied, and --panic-timeout arms a self-rollback you cancel with
#     `phone firewall confirm`.
#
# Usage:
#   phone firewall --sip-host sip.example.com --sms-host sms.example.com --yes
#   phone firewall --dry-run --sip-host 203.0.113.9      # print, change nothing
#   phone firewall --status | --unblock | --rollback | --confirm
#
# Requires root for anything that touches the kernel.

set -euo pipefail

SELF="${BASH_SOURCE[0]}"
STATE_DIR="/etc/phone"
STATE_FILE="$STATE_DIR/firewall.state"
BACKUP_DIR="$STATE_DIR/backups"
CONFIRM_FILE="/run/phone-firewall.confirmed"
TABLE="phone"

MODE="apply"
ENGINE="auto"
DRY_RUN=0
ASSUME_YES=0
ALLOW_IPV6=0
ALLOW_DHCP=1
LOG_DROPS=0
PANIC=0
SIP_PORT=5061
SMS_PORT=443
RTP_MIN=4000
RTP_MAX=4010
WG_PORT=""
SIP_HOSTS=()
SMS_HOSTS=()
RESOLVERS=()

usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run|--print) DRY_RUN=1; shift ;;
        --yes|-y)          ASSUME_YES=1; shift ;;
        --status)          MODE="status"; shift ;;
        --unblock|--panic-off) MODE="unblock"; shift ;;
        --rollback)        MODE="rollback"; shift ;;
        --confirm)         MODE="confirm"; shift ;;
        --engine)          ENGINE="${2:?}"; shift 2 ;;
        --sip-host)        SIP_HOSTS+=("${2:?}"); shift 2 ;;
        --sms-host)        SMS_HOSTS+=("${2:?}"); shift 2 ;;
        --resolver)        RESOLVERS+=("${2:?}"); shift 2 ;;
        --sip-port)        SIP_PORT="${2:?}"; shift 2 ;;
        --sms-port)        SMS_PORT="${2:?}"; shift 2 ;;
        --rtp-range)       RTP_MIN="${2%%-*}"; RTP_MAX="${2##*-}"; shift 2 ;;
        --wg-port)         WG_PORT="${2:?}"; shift 2 ;;
        --panic-timeout)   PANIC="${2:?}"; shift 2 ;;
        --allow-ipv6)      ALLOW_IPV6=1; shift ;;
        --allow-dhcp)      ALLOW_DHCP=1; shift ;;
        --no-dhcp)         ALLOW_DHCP=0; shift ;;
        --log-drops)       LOG_DROPS=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

die()  { echo "error: $*" >&2; exit 1; }
info() { echo "[*] $*" >&2; }
warn() { echo "[!] $*" >&2; }

[ "$SIP_PORT" -gt 0 ] 2>/dev/null || die "--sip-port must be a number"
[ "$SMS_PORT" -gt 0 ] 2>/dev/null || die "--sms-port must be a number"
[ "$RTP_MIN" -gt 0 ] 2>/dev/null || die "--rtp-range must look like 4000-4010"
[ "$RTP_MIN" -le "$RTP_MAX" ] || die "--rtp-range start is greater than its end"

# --------------------------------------------------------------------------
# peer resolution: hostnames are resolved once and pinned as addresses
# --------------------------------------------------------------------------
resolve() {
    local host="$1" out=""
    case "$host" in
        */*) printf '%s\n' "$host"; return 0 ;;                     # already CIDR
        *[0-9].[0-9]*) case "$host" in
            *[!0-9.]*) : ;; *:*) : ;; *) printf '%s\n' "$host"; return 0 ;;
        esac ;;
    esac
    command -v getent >/dev/null 2>&1 || die "getent is required to resolve $host"
    out="$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' || true)"
    [ -n "${out// /}" ] || die "could not resolve $host (no IPv4 address found)"
    printf '%s\n' "${out% }"
}

collect() {
    local -n _out="$1"; shift
    local host
    for host in "$@"; do
        # shellcheck disable=SC2207
        _out+=($(resolve "$host"))
    done
}

SIP_IPS=()
SMS_IPS=()
if [ "${#SIP_HOSTS[@]}" -gt 0 ]; then collect SIP_IPS "${SIP_HOSTS[@]}"; fi
if [ "${#SMS_HOSTS[@]}" -gt 0 ]; then collect SMS_IPS "${SMS_HOSTS[@]}"; fi
if [ "${#RESOLVERS[@]}" -eq 0 ]; then RESOLVERS=("127.0.0.1"); fi

ip_elements() {
    local first=1 ip
    for ip in "$@"; do
        if [ "$first" -eq 1 ]; then first=0; else printf ', '; fi
        printf '%s' "$ip"
    done
}

# --------------------------------------------------------------------------
# engines
# --------------------------------------------------------------------------
detect_engine() {
    # Returns the chosen engine, or an empty string when none is installed.
    # Callers decide whether that is fatal (apply) or merely informative
    # (--dry-run, --status).
    if [ "$ENGINE" != "auto" ]; then printf '%s' "$ENGINE"; return 0; fi
    if command -v nft >/dev/null 2>&1; then printf 'nft'; return 0; fi
    if command -v iptables >/dev/null 2>&1; then printf 'iptables'; return 0; fi
    printf ''
}

build_nft() {
    local ipv6_policy
    if [ "$ALLOW_IPV6" -eq 1 ]; then ipv6_policy="accept"; else ipv6_policy="drop"; fi

    cat <<EOF
# phone firewall -- generated $(date -u +%Y-%m-%dT%H:%M:%SZ)
# peers: sip=[${SIP_IPS[*]:-none}] sms=[${SMS_IPS[*]:-none}] dns=[${RESOLVERS[*]}]
destroy table inet $TABLE
table inet $TABLE {
EOF
    # Sets are only declared when they have members: nft rejects an empty
    # elements list, and an unused set is noise in `nft list`.
    if [ "${#SIP_IPS[@]}" -gt 0 ]; then
        printf '    set sip_v4 { type ipv4_addr; flags interval; elements = { %s } }\n' "$(ip_elements "${SIP_IPS[@]}")"
    fi
    if [ "${#SMS_IPS[@]}" -gt 0 ]; then
        printf '    set sms_v4 { type ipv4_addr; flags interval; elements = { %s } }\n' "$(ip_elements "${SMS_IPS[@]}")"
    fi
    printf '    set dns_v4 { type ipv4_addr; flags interval; elements = { %s } }\n' "$(ip_elements "${RESOLVERS[@]}")"
    cat <<EOF

    chain input {
        type filter hook input priority filter; policy drop;
        iif lo accept comment "loopback: sms daemon and local tooling live here"
        ct state invalid drop
        ct state established,related accept comment "replies to flows we opened"

        # IPv6 is dropped wholesale unless explicitly enabled: a v4-only rule
        # set on a live v6 stack leaks around every rule above.
        meta nfproto ipv6 ip6 saddr ::1 accept
        meta nfproto ipv6 $ipv6_policy

        # PMTU and unreachables are required for healthy TCP; echo is not.
        ip protocol icmp icmp type { destination-unreachable, time-exceeded, parameter-problem } accept
EOF
    if [ "$ALLOW_DHCP" -eq 1 ]; then
        cat <<EOF
        udp sport 67 udp dport 68 accept comment "DHCP reply (lease renewal)"
EOF
    fi
    if [ -n "$WG_PORT" ]; then
        cat <<EOF
        udp dport $WG_PORT accept comment "WireGuard - inbound tunnel for remote tooling"
EOF
    fi
    if [ "${#SIP_IPS[@]}" -gt 0 ]; then
        cat <<EOF
        ip saddr @sip_v4 tcp dport $SIP_PORT accept comment "SIP/TLS signalling from the provider"
        ip saddr @sip_v4 udp dport $RTP_MIN-$RTP_MAX accept comment "SRTP media from the provider"
EOF
    fi
    cat <<EOF
        counter drop comment "everything else"
    }

    chain output {
        type filter hook output priority filter; policy drop;
        oif lo accept comment "loopback"
        ct state established,related accept

        # DNS: only to the resolver you named. Lookups are metadata.
        ip daddr @dns_v4 udp dport 53 accept comment "DNS to approved resolver"
        ip daddr @dns_v4 tcp dport 53 accept comment "DNS over TCP to approved resolver"
EOF
    if [ "$ALLOW_DHCP" -eq 1 ]; then
        cat <<EOF
        udp sport 68 udp dport 67 accept comment "DHCP renewal (disable with --no-dhcp or a static address)"
EOF
    fi
    if [ "${#SIP_IPS[@]}" -gt 0 ]; then
        cat <<EOF
        ip daddr @sip_v4 tcp dport $SIP_PORT accept comment "SIP/TLS signalling to the provider"
        ip daddr @sip_v4 udp dport $RTP_MIN-$RTP_MAX accept comment "SRTP media to the provider"
EOF
    fi
    if [ "${#SMS_IPS[@]}" -gt 0 ]; then
        cat <<EOF
        ip daddr @sms_v4 tcp dport $SMS_PORT accept comment "SMS gateway API (HTTPS)"
EOF
    fi
    if [ -n "$WG_PORT" ]; then
        cat <<EOF
        udp dport $WG_PORT accept comment "WireGuard tunnel"
EOF
    fi
    cat <<EOF
        ip protocol icmp icmp type { echo-request, destination-unreachable, time-exceeded } accept comment "ICMP for PMTU"
EOF
    if [ "$LOG_DROPS" -eq 1 ]; then
        cat <<EOF
        log prefix "phone-drop-out " level info limit rate 5/minute
EOF
    fi
    cat <<EOF
        counter drop comment "everything else: no telemetry, no updates, no beacons"
    }

    chain forward {
        type filter hook forward priority filter; policy drop;
        counter drop comment "this host is not a router"
    }
}
EOF
}

build_iptables() {
    # iptables fallback: expand sets into individual rules. Deliberately the
    # same policy, just more verbose.
    local ipv6_policy ip
    if [ "$ALLOW_IPV6" -eq 1 ]; then ipv6_policy="ACCEPT"; else ipv6_policy="DROP"; fi

    printf 'iptables -N %s_IN 2>/dev/null || true\n' "$TABLE"
    printf 'iptables -N %s_OUT 2>/dev/null || true\n' "$TABLE"
    printf 'iptables -F %s_IN; iptables -F %s_OUT\n' "$TABLE" "$TABLE"
    printf 'iptables -A %s_IN -i lo -j ACCEPT\n' "$TABLE"
    printf 'iptables -A %s_IN -m conntrack --ctstate INVALID -j DROP\n' "$TABLE"
    printf 'iptables -A %s_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n' "$TABLE"
    printf 'iptables -A %s_IN -p icmp --icmp-type destination-unreachable -j ACCEPT\n' "$TABLE"
    printf 'iptables -A %s_IN -p icmp --icmp-type time-exceeded -j ACCEPT\n' "$TABLE"
    printf 'iptables -A %s_IN -p icmp --icmp-type parameter-problem -j ACCEPT\n' "$TABLE"
    [ "$ALLOW_DHCP" -eq 1 ] && printf 'iptables -A %s_IN -p udp --sport 67 --dport 68 -j ACCEPT\n' "$TABLE"
    [ -n "$WG_PORT" ] && printf 'iptables -A %s_IN -p udp --dport %s -j ACCEPT\n' "$TABLE" "$WG_PORT"
    for ip in "${SIP_IPS[@]:-}"; do
        [ -n "$ip" ] || continue
        printf 'iptables -A %s_IN -s %s -p tcp --dport %s -j ACCEPT\n' "$TABLE" "$ip" "$SIP_PORT"
        printf 'iptables -A %s_IN -s %s -p udp --dport %s:%s -j ACCEPT\n' "$TABLE" "$ip" "$RTP_MIN" "$RTP_MAX"
    done
    printf 'iptables -A %s_IN -j DROP\n' "$TABLE"

    printf 'iptables -A %s_OUT -o lo -j ACCEPT\n' "$TABLE"
    printf 'iptables -A %s_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT\n' "$TABLE"
    for ip in "${RESOLVERS[@]}"; do
        printf 'iptables -A %s_OUT -d %s -p udp --dport 53 -j ACCEPT\n' "$TABLE" "$ip"
        printf 'iptables -A %s_OUT -d %s -p tcp --dport 53 -j ACCEPT\n' "$TABLE" "$ip"
    done
    [ "$ALLOW_DHCP" -eq 1 ] && printf 'iptables -A %s_OUT -p udp --sport 68 --dport 67 -j ACCEPT\n' "$TABLE"
    for ip in "${SIP_IPS[@]:-}"; do
        [ -n "$ip" ] || continue
        printf 'iptables -A %s_OUT -d %s -p tcp --dport %s -j ACCEPT\n' "$TABLE" "$ip" "$SIP_PORT"
        printf 'iptables -A %s_OUT -d %s -p udp --dport %s:%s -j ACCEPT\n' "$TABLE" "$ip" "$RTP_MIN" "$RTP_MAX"
    done
    for ip in "${SMS_IPS[@]:-}"; do
        [ -n "$ip" ] || continue
        printf 'iptables -A %s_OUT -d %s -p tcp --dport %s -j ACCEPT\n' "$TABLE" "$ip" "$SMS_PORT"
    done
    [ -n "$WG_PORT" ] && printf 'iptables -A %s_OUT -p udp --dport %s -j ACCEPT\n' "$TABLE" "$WG_PORT"
    printf 'iptables -A %s_OUT -p icmp --icmp-type echo-request -j ACCEPT\n' "$TABLE"
    printf 'iptables -A %s_OUT -p icmp --icmp-type destination-unreachable -j ACCEPT\n' "$TABLE"
    printf 'iptables -A %s_OUT -j DROP\n' "$TABLE"

    printf 'iptables -C INPUT -j %s_IN 2>/dev/null || iptables -I INPUT 1 -j %s_IN\n' "$TABLE" "$TABLE"
    printf 'iptables -C OUTPUT -j %s_OUT 2>/dev/null || iptables -I OUTPUT 1 -j %s_OUT\n' "$TABLE" "$TABLE"
    printf 'iptables -P INPUT DROP\n'
    printf 'iptables -P OUTPUT DROP\n'
    printf 'iptables -P FORWARD DROP\n'
    printf 'ip6tables -P INPUT %s 2>/dev/null || true\n' "$ipv6_policy"
    printf 'ip6tables -P OUTPUT %s 2>/dev/null || true\n' "$ipv6_policy"
    printf 'ip6tables -P FORWARD %s 2>/dev/null || true\n' "$ipv6_policy"
    printf 'ip6tables -A INPUT -i lo -j ACCEPT 2>/dev/null || true\n'
    printf 'ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true\n'
}

# --------------------------------------------------------------------------
# backup / apply / rollback
# --------------------------------------------------------------------------
backup_ruleset() {
    local engine="$1" stamp
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$STATE_DIR" "$BACKUP_DIR"
    case "$engine" in
        nft)      nft list ruleset > "$BACKUP_DIR/$stamp.nft" 2>/dev/null || true ;;
        iptables) { iptables-save 2>/dev/null || true; echo '# --- ipv6 ---'; ip6tables-save 2>/dev/null || true; } > "$BACKUP_DIR/$stamp.iptables" ;;
    esac
    [ -n "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ] || warn "no backup written (nothing to save?)"
    printf '%s' "$stamp"
}

write_state() {
    local engine="$1"
    mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
    {
        echo "# phone firewall state -- written $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "engine=$engine"
        echo "sip_ips=${SIP_IPS[*]:-}"
        echo "sms_ips=${SMS_IPS[*]:-}"
        echo "resolvers=${RESOLVERS[*]}"
        echo "sip_port=$SIP_PORT"
        echo "sms_port=$SMS_PORT"
        echo "rtp_range=$RTP_MIN-$RTP_MAX"
        echo "allow_ipv6=$ALLOW_IPV6"
        echo "allow_dhcp=$ALLOW_DHCP"
        echo "wg_port=$WG_PORT"
    } > "$STATE_FILE"
    chmod 600 "$STATE_FILE"
}

apply_engine() {
    local engine="$1" ruleset="$2" backup="(nothing to back up)"

    if [ "$DRY_RUN" -eq 1 ]; then
        printf '# engine: %s\n' "$engine"
        printf '# backup would be written to %s/<timestamp>\n' "$BACKUP_DIR" >&2
        printf '# apply with: %s -f <this file>\n\n' "$engine" >&2
        printf '%s\n' "$ruleset"
        return 0
    fi
    backup="$(backup_ruleset "$engine")"

    if [ "$ASSUME_YES" -ne 1 ]; then
        warn "this will replace the running firewall with a default-deny policy."
        warn "peers: sip=[${SIP_IPS[*]:-none}] sms=[${SMS_IPS[*]:-none}] dns=[${RESOLVERS[*]}]"
        printf 'continue? [y/N] ' >&2
        read -r reply </dev/tty || reply="n"
        case "$reply" in [yY]*) : ;; *) die "aborted" ;; esac
    fi

    case "$engine" in
        nft)
            command -v nft >/dev/null 2>&1 || die "nft not installed"
            local work="/tmp/phone-firewall.$$.nft" err="/tmp/phone-firewall.$$.err"
            printf '%s\n' "$ruleset" > "$work"
            if nft --check --file "$work" 2>"$err"; then
                nft --file "$work"
            else
                cat "$err" >&2
                if grep -q '^destroy table' "$work"; then
                    # nft < 1.0.2 has no 'destroy' keyword. Fall back to a
                    # delete-then-apply: validate the ruleset first (without
                    # the destroy line), then drop the old table and load.
                    warn "'destroy' unsupported by this nft; falling back to delete-then-apply"
                    grep -v '^destroy table' "$work" > "$work.body"
                    if ! nft --check --file "$work.body" 2>"$err"; then
                        cat "$err" >&2
                        rm -f "$work" "$work.body" "$err"
                        die "ruleset failed validation; nothing was applied"
                    fi
                    nft delete table inet "$TABLE" 2>/dev/null || true
                    nft --file "$work.body"
                    rm -f "$work.body"
                else
                    rm -f "$work" "$err"
                    die "ruleset failed validation; nothing was applied"
                fi
            fi
            rm -f "$work" "$err"
            ;;
        iptables)
            command -v iptables >/dev/null 2>&1 || die "iptables not installed"
            printf '%s\n' "$ruleset" | bash
            ;;
        *) die "unknown engine: $engine" ;;
    esac

    write_state "$engine"
    info "applied ($engine). backup: $BACKUP_DIR/$backup"

    if [ "$PANIC" -gt 0 ]; then
        rm -f "$CONFIRM_FILE"
        nohup bash -c "sleep $PANIC; if [ ! -f '$CONFIRM_FILE' ]; then '$SELF' --unblock >/dev/null 2>&1; fi" >/dev/null 2>&1 &
        disown 2>/dev/null || true
        warn "panic timer armed: rules roll back in ${PANIC}s unless you run 'phone firewall confirm'"
    fi
}

do_unblock() {
    [ "$(id -u)" -eq 0 ] || die "unblock needs root"
    local engine
    engine="$(detect_engine)"
    [ -n "$engine" ] || { touch "$CONFIRM_FILE" 2>/dev/null || true; info "no firewall engine installed; nothing to unblock"; return 0; }
    case "$engine" in
        nft)
            if command -v nft >/dev/null 2>&1; then
                nft list table inet "$TABLE" >/dev/null 2>&1 && nft delete table inet "$TABLE" && info "removed table inet $TABLE"
            fi
            ;;
        iptables)
            iptables -D INPUT -j "${TABLE}_IN" 2>/dev/null || true
            iptables -D OUTPUT -j "${TABLE}_OUT" 2>/dev/null || true
            iptables -F "${TABLE}_IN" 2>/dev/null || true
            iptables -F "${TABLE}_OUT" 2>/dev/null || true
            iptables -X "${TABLE}_IN" 2>/dev/null || true
            iptables -X "${TABLE}_OUT" 2>/dev/null || true
            iptables -P INPUT ACCEPT; iptables -P OUTPUT ACCEPT; iptables -P FORWARD ACCEPT
            ip6tables -P INPUT ACCEPT 2>/dev/null || true
            ip6tables -P OUTPUT ACCEPT 2>/dev/null || true
            ip6tables -P FORWARD ACCEPT 2>/dev/null || true
            info "flushed phone chains and restored ACCEPT policies"
            ;;
    esac
    touch "$CONFIRM_FILE"
    info "network is open again (host firewall aside). Re-lock with: phone firewall --yes ..."
}

do_rollback() {
    [ "$(id -u)" -eq 0 ] || die "rollback needs root"
    [ -n "$(detect_engine)" ] || die "no firewall engine installed"
    local latest engine
    latest="$(ls -1t "$BACKUP_DIR" 2>/dev/null | head -1 || true)"
    [ -n "$latest" ] || die "no backups in $BACKUP_DIR"
    case "$latest" in
        *.nft)      engine="nft";      nft --file "$BACKUP_DIR/$latest" ;;
        *.iptables) engine="iptables"; bash "$BACKUP_DIR/$latest" ;;
        *) die "unrecognised backup: $latest" ;;
    esac
    info "restored $engine ruleset from $latest"
}

do_status() {
    local engine
    engine="$(detect_engine)"
    echo "engine      : ${engine:-none installed (nft or iptables required)}"
    if [ -f "$STATE_FILE" ]; then
        echo "state file  : $STATE_FILE"
        sed 's/^/              /' "$STATE_FILE"
    else
        echo "state file  : none (firewall never applied by this tool)"
    fi
    if [ "$engine" = "nft" ] && command -v nft >/dev/null 2>&1; then
        if nft list table inet "$TABLE" >/dev/null 2>&1; then
            echo "table       : inet $TABLE present"
            nft -a list table inet "$TABLE" 2>/dev/null | sed -n '1,40p' | sed 's/^/              /'
        else
            echo "table       : inet $TABLE absent (not locked down)"
        fi
    fi
    [ "$(id -u)" -eq 0 ] || echo "note        : run as root for a complete view"
}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
case "$MODE" in
    confirm)
        if [ -f "$STATE_FILE" ]; then
            mkdir -p "$(dirname "$CONFIRM_FILE")" 2>/dev/null || true
            touch "$CONFIRM_FILE"
            echo "panic timer cancelled; rules stay in place"
        else
            echo "nothing to confirm (firewall was never applied)"
        fi
        exit 0
        ;;
    status)   do_status; exit 0 ;;
    unblock)  do_unblock; exit 0 ;;
    rollback) do_rollback; exit 0 ;;
esac

# Refuse to invent open rules, and refuse to build a box that can reach
# nothing at all. A resolver alone is not a communication peer: with no SIP
# host, no SMS host and no tunnel, this ruleset would cut the machine off
# from everything while looking "applied".
if [ "${#SIP_IPS[@]}" -eq 0 ] && [ "${#SMS_IPS[@]}" -eq 0 ] && [ -z "$WG_PORT" ]; then
    die "no communication peer pinned: pass at least one --sip-host, --sms-host or --wg-port.
     Pinning is deliberate - an open 'allow 5061 to any' rule is exactly the
     hole this script exists to avoid. Example:
       $(basename "$SELF") --sip-host sip.yourprovider.example --sms-host sms.yourprovider.example --yes"
fi

# Anti-lockout warning: a default-deny OUTPUT policy with no DNS and no SSH
# means the box becomes unreachable from the network.
if [ "${#RESOLVERS[@]}" -gt 0 ] && [ "${RESOLVERS[0]}" = "127.0.0.1" ]; then
    warn "DNS is pinned to 127.0.0.1: you must be running a local resolver (unbound, dnsmasq, systemd-resolved)."
    warn "if you are not, name resolution stops the moment this applies."
fi
if [ "$ALLOW_DHCP" -eq 1 ]; then
    warn "DHCP renewal is allowed, which sends your hostname unless the client is configured not to. See docs/HARDENING.md."
fi
warn "outbound default-deny will stop package updates, NTP, and every unlisted service. That is the point."

# Privilege first: "run me with sudo" is the more useful error than "no nft".
[ "$DRY_RUN" -eq 1 ] || [ "$(id -u)" -eq 0 ] || die "applying firewall rules needs root (use --dry-run to preview, or run under sudo)"

ENGINE_RESOLVED="$(detect_engine)"
if [ -z "$ENGINE_RESOLVED" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "neither nft nor iptables is installed here; previewing the nft ruleset anyway"
        ENGINE_RESOLVED="nft"
    else
        die "neither nft nor iptables is installed: 'apt-get install nftables' (recommended) or iptables"
    fi
fi

case "$ENGINE_RESOLVED" in
    nft)      RULESET="$(build_nft)" ;;
    iptables) RULESET="$(build_iptables)" ;;
    *)        die "unknown engine: $ENGINE_RESOLVED" ;;
esac

apply_engine "$ENGINE_RESOLVED" "$RULESET"

if [ "$DRY_RUN" -eq 1 ]; then
    info "dry run: nothing changed"
    exit 0
fi

info "done. verify with: phone firewall --status"
info "voice needs the provider's media addresses too; if audio is one-way, add --sip-host <media-host>"
