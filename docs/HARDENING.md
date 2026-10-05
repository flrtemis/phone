# Hardening notes, and what this does *not* protect

A tool that claims to stop all telemetry is lying. This page states the threat
model plainly: what is enforced in code, what is enforced by the ruleset, and
what no amount of local configuration can fix.

## What is actually enforced

### 1. No plaintext signalling, no plaintext media

* `sip/phone.sh` refuses to start unless the configuration contains `--use-tls`,
  `--no-udp`, `--no-tcp`, `--use-srtp 2`, `--tls-ca-file` and
  `--tls-verify-server`, and unless the installed `pjsua` was compiled with TLS
  and SRTP (checked against its own `--help` output).
* `--use-srtp 2` is `PJMEDIA_SRTP_MANDATORY`: if the peer cannot do SRTP, the
  call does not happen. `1` ("optional") is the setting that silently downgrades
  to plaintext, which is why the launcher warns on it.
* The TLS listener binds `--local-port + 1` (pjsua's rule). With the shipped
  configuration that is 5060 → 5061, the standard port, and the launcher
  verifies the arithmetic rather than assuming it.

### 2. SIP messages are never logged

pjsua defaults to log level 5, which prints full SIP messages including SDP.
With SDES key agreement, SDP *is the key material*. The shipped configuration
pins `--log-level 3`, and the launcher refuses to start at level 4 or above.

### 3. Certificate verification is real

PJLIB's OpenSSL backend does not use the system trust store; it verifies
against exactly what `--tls-ca-file` names. Omitting that file while setting
`--tls-verify-server` is the classic "verification enabled, nothing to verify
against" configuration, so both are required and the CA file must exist.
`sip/gen-certs.sh` builds an explicit bundle from the system roots plus any
provider CA you drop in as `extra-ca.pem`.

### 4. Secrets live at mode 0600 or the process refuses to start

The SIP config (which holds your registration password) and the TLS private
key are both checked. `phone sip --init` writes the config `0600`.

### 5. The ruleset has no "anywhere" rules

`firewall/lockdown.sh` sets a default-deny policy in both directions and then
allows traffic only to peers you pin by address (`--sip-host`, `--sms-host`,
`--resolver`, `--wg-port`). Concretely:

* No `allow 443 to any` rule — the usual way update pings, crash reporters and
  vendor beacons keep working after a "lockdown".
* DNS is allowed only to the resolver you name, because every lookup is a
  metadata beacon naming the service you are about to contact.
* IPv6 is dropped wholesale. A v4-only ruleset on a live v6 stack leaks around
  every rule you wrote.
* The SRTP window defaults to 4000–4010 to match pjsua's own default
  (`--rtp-port 4000`), and the SIP launcher warns when `--rtp-port` disagrees.
  A firewall that opens 10000–20000 while the phone sends from 4000 gives you a
  registered line and silent audio.
* Inbound ICMP echo is dropped (no ping-based discovery); the ICMP types TCP
  needs for PMTU still pass.
* Rules are backed up before application, validated with `nft --check` before
  loading, and `--panic-timeout` arms a rollback you cancel with
  `phone firewall confirm` — so a remote shell is not a one-way trip.

### 6. The SMS receiver parses untrusted input defensively

Loopback-only by default (public binds require an explicit opt-in **plus** a
token **plus** TLS); constant-time token comparison; a hard body-size cap;
per-client rate limiting; a strict content-type allowlist; no version banner;
sender strings sanitised before they can influence a path; content-addressed
filenames; and message bodies kept out of every log line.

## Deliberate gaps you should know about

### DHCP leaks your hostname, and the ruleset still allows DHCP

Lease renewal needs UDP 68→67. Unless your client is configured otherwise, it
sends option 12 (hostname) and option 55 (parameter request list), which is a
fingerprint — and the address lease itself is a record held by whoever runs the
network. Either use a static address and `--no-dhcp`, or disable the hostname
option (`SendHostname=no` in systemd-networkd, `dhcp-hostname` unset for
dhclient) and keep DHCP.

### DNS is metadata, even when encrypted

A local resolver (unbound, dnsmasq, systemd-resolved with DoT) is *better* than
a public one, but the resolver still learns every name you resolve, and its
upstream learns them unless you use DoT/DoH. The ruleset pins DNS to one
address precisely so this is one trust decision instead of many.

### Your provider sees plenty

TLS and SRTP protect the audio path from third parties. They do not hide the
numbers you dial, the times you call, which IPs you register from, or the
message contents from the carrier and the SIP/SMS provider that deliver them.
With SDES, the SRTP keys travel in the SDP; they are protected by the TLS
signalling channel, not by any end-to-end property. Prefer DTLS-SRTP keying
(`--srtp-keying 1`) where the provider supports it, and treat the provider as a
party you trust with metadata.

### This is a phone on the PSTN

Interoperating with the telephone network means disclosing the calling and
called numbers to carriers by design. No local configuration changes that.

### No protection against a compromised host

If something is already running as your user (or as root), it can read the
spool, the TLS key, and the SIP password. This project reduces what leaves the
machine; it is not an endpoint-security product. Full-disk encryption, a
locked-down boot chain, and not running arbitrary code remain your job.

### No update channel, by design

The ruleset blocks package updates. You will need to open them deliberately
(`phone firewall --unblock`, update, re-apply) — that is the trade being made.
A pinned, infrequently-updated machine is a real security cost; weigh it.

## Operating notes

* Keep a copy of your working ruleset: `nft list ruleset > ~/firewall.$(date +%F).nft`.
* Provider IPs change. `phone firewall --status` prints the pinned set and the
  date it was captured; re-run the lockdown command after a provider migration.
* Rotate the TLS client certificate before its expiry
  (`openssl x509 -in client.pem -noout -enddate`) and upload the new one if the
  provider pins certificates.
* The spool is plain text: `chmod 700` the directory, and think twice before
  syncing it to another machine.
