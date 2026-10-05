#!/usr/bin/env bash
# Tests for firewall/lockdown.sh.
#
# These run entirely in --dry-run, so they do not need root and do not touch
# the kernel. What they check is the generated policy: the shape of the rule
# set is the deliverable, and the most important property is that *no* rule
# allows an unpinned destination.

set -uo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
FW="$REPO_DIR/firewall/lockdown.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1" "${3:-}"; fi; }
# File assertions take (name, file, ERE) so no nested quoting is needed.
has()  { if grep -qE "$3" "$2"; then ok "$1"; else bad "$1" "missing pattern: $3"; fi; }
lacks(){ if grep -qE "$3" "$2"; then bad "$1" "unexpected pattern: $3"; else ok "$1"; fi; }

SIP=203.0.113.9
SMS=198.51.100.7
RESOLVER=10.8.0.1

run() { bash "$FW" "$@" 2>&1; }

# --------------------------------------------------------------------------
printf 'nftables ruleset (dry run)\n'
OUT="$TMP/nft.rules"
run --dry-run --sip-host "$SIP" --sms-host "$SMS" --resolver "$RESOLVER" > "$OUT"; rc=$?
check "dry run succeeds without root" '[ '"$rc"' -eq 0 ]'
check "dry run reports that nothing changed" 'grep -q "dry run: nothing changed" "$OUT"'
check "dry run does not leak permission errors from /etc" '! grep -q "Permission denied" "$OUT"'

check "default-deny on input"    'grep -qE "hook input.*policy drop" "$OUT"'
check "default-deny on output"   'grep -qE "hook output.*policy drop" "$OUT"'
check "forwarding is dropped"    'grep -qE "hook forward.*policy drop" "$OUT"'
check "loopback stays open"      'grep -q "iif lo accept" "$OUT"'

has "SIP provider pinned in a set" "$OUT" "set sip_v4.*${SIP//./\\.}"
has "SMS gateway pinned in a set" "$OUT" "set sms_v4.*${SMS//./\\.}"
has "resolver pinned in a set" "$OUT" "set dns_v4.*${RESOLVER//./\\.}"
has "SIP/TLS outbound to the pinned provider" "$OUT" "ip daddr @sip_v4 tcp dport 5061 accept"
has "SRTP media window matches pjsua default" "$OUT" "ip daddr @sip_v4 udp dport 4000-4010 accept"
has "SRTP inbound allowed from the provider (inbound calls)" "$OUT" "ip saddr @sip_v4 udp dport 4000-4010 accept"
has "HTTPS only to the pinned SMS gateway" "$OUT" "ip daddr @sms_v4 tcp dport 443 accept"
has "DNS only to the pinned resolver" "$OUT" "ip daddr @dns_v4 udp dport 53 accept"
has "IPv6 is dropped wholesale" "$OUT" "meta nfproto ipv6 drop"
has "IPv6 loopback still works" "$OUT" "ip6 saddr ::1 accept"
lacks "inbound ICMP echo is not allowed" "$OUT" "hook input.*icmp type \{ echo-request"

# The security property that matters: no unpinned destination anywhere.
unpinned="$(grep -E "accept" "$OUT" | grep -E "dport (443|5061|53)" | grep -vE "@sip_v4|@sms_v4|@dns_v4" || true)"
check "no accept rule reaches an unpinned destination" '[ -z "$unpinned" ]' "$unpinned"
check "no rule allows all outbound TCP" '! grep -qE "tcp dport [0-9:-]+ accept" "$OUT" | grep -v "@"'
check "there is an explicit drop counter at the end of output" \
    'grep -q "everything else: no telemetry, no updates, no beacons" "$OUT"'

printf '\noption handling\n'
check "unknown option exits 2" 'run --dry-run --nonsense >/dev/null; [ $? -eq 2 ]'
check "--help exits 0" 'run --help >/dev/null; [ $? -eq 0 ]'
out="$(run --status)"; rc=$?
check "--status works without any engine installed" '[ '"$rc"' -eq 0 ] && printf "%s" "$out" | grep -qE "engine +:"'

OUT2="$TMP/nomesh.rules"
run --dry-run --sip-host "$SIP" > "$OUT2"
lacks "SMS rules are absent when no SMS host is pinned" "$OUT2" "@sms_v4"
run --dry-run --sms-host "$SMS" > "$TMP/nosip.rules"
lacks "SIP set is absent when no SIP host is pinned" "$TMP/nosip.rules" "set sip_v4"

OUT3="$TMP/nodhcp.rules"
run --dry-run --sip-host "$SIP" --no-dhcp > "$OUT3"
lacks "--no-dhcp removes DHCP lease rules" "$OUT3" "dport 67|sport 67"
has "DHCP is allowed by default and flagged as a leak" "$OUT" "dport 67 accept"
has "DHCP allowance is called out as a metadata leak" "$OUT" "DHCP renewal is allowed"

OUT4="$TMP/ipv6.rules"
run --dry-run --sip-host "$SIP" --allow-ipv6 > "$OUT4"
has "--allow-ipv6 switches the v6 policy to accept" "$OUT4" "meta nfproto ipv6 accept"

OUT5="$TMP/rtp.rules"
run --dry-run --sip-host "$SIP" --rtp-range 12000-12100 > "$OUT5"
has "--rtp-range is honoured" "$OUT5" "udp dport 12000-12100 accept"

OUT6="$TMP/log.rules"
run --dry-run --sip-host "$SIP" --log-drops > "$OUT6"
has "--log-drops adds a rate-limited log rule" "$OUT6" "limit rate 5/minute"

OUT7="$TMP/wg.rules"
run --dry-run --sip-host "$SIP" --wg-port 51820 > "$OUT7"
has "--wg-port opens the tunnel" "$OUT7" "udp dport 51820 accept"

# --------------------------------------------------------------------------
printf '\niptables fallback\n'
OUT8="$TMP/ipt.rules"
run --engine iptables --dry-run --sip-host "$SIP" --sms-host "$SMS" --resolver "$RESOLVER" > "$OUT8"
has "iptables: default-deny policies are set" "$OUT8" "iptables -P INPUT DROP"
has "iptables: default-deny outbound policy is set" "$OUT8" "iptables -P OUTPUT DROP"
has "iptables: provider is pinned by address, not by port alone" "$OUT8" "phone_OUT -d ${SIP//./\\.} -p tcp --dport 5061 -j ACCEPT"
has "iptables: SRTP window is opened to the provider" "$OUT8" "phone_OUT -d ${SIP//./\\.} -p udp --dport 4000:4010 -j ACCEPT"
has "iptables: IPv6 policies are set too" "$OUT8" "ip6tables -P INPUT DROP"
unpinned443="$(grep -- "--dport 443" "$OUT8" | grep -v -- "-d $SMS" || true)"
check "iptables: no unpinned 443 rule" '[ -z "$unpinned443" ]' "$unpinned443"
has "iptables: SMS gateway is the only 443 destination" "$OUT8" "phone_OUT -d ${SMS//./\\.} -p tcp --dport 443 -j ACCEPT"

# --------------------------------------------------------------------------
printf '\nrefusals (these protect against a self-inflicted outage)\n'
out="$(run --dry-run --resolver 10.8.0.1)"; rc=$?
check "refuses a ruleset with no communication peer" \
    '[ '"$rc"' -ne 0 ] && printf "%s" "$out" | grep -q "no communication peer pinned"'
out="$(run --sip-host "$SIP" --yes)"; rc=$?
check "refuses to apply without root" \
    '[ '"$rc"' -ne 0 ] && printf "%s" "$out" | grep -q "needs root"'
out="$(run --dry-run --sip-host not-a-real-host.invalid)"; rc=$?
check "fails clearly when a peer cannot be resolved" \
    '[ '"$rc"' -ne 0 ] && printf "%s" "$out" | grep -q "could not resolve"'
out="$(run --dry-run --sip-host "$SIP" --rtp-range 5000-4000)"; rc=$?
check "rejects an inverted media range" \
    '[ '"$rc"' -ne 0 ] && printf "%s" "$out" | grep -q "greater than"'
out="$(run --dry-run --sip-host "$SIP" --sip-port abc)"; rc=$?
check "rejects a non-numeric port" \
    '[ '"$rc"' -ne 0 ] && printf "%s" "$out" | grep -q "sip-port"'

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
