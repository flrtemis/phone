# phone

A communication terminal for a Linux host, stripped to its functional core:

* **voice** — PJSIP (`pjsua`) speaking SIP over TLS with mandatory SRTP, launched
  by a wrapper that refuses to run in a degraded configuration.
* **text** — a dependency-free Python daemon that receives SMS webhooks (or polls
  a gateway) and writes each message to a plain text file, plus a CLI that reads
  those files.
* **firewall** — a default-deny `nftables`/`iptables` ruleset that allows only
  your pinned provider addresses, with no "allow HTTPS to anywhere" hole.

No graphical stack, no mobile OS, no database, no vendor SDK, no telemetry, and
no Python packages beyond the standard library.

```
                         +-------------------------------------------+
                         |            local Linux host               |
                         |                                           |
   +---------------+     |   +-----------------------------+         |
   | SIP provider  | TLS |   | pjsua  (sip/phone.sh)       |         |
   | registrar +   |<=====>|   - TLS signalling  :5061   |         |
   | media servers | SRTP|   |   - SRTP media 4000-4010    |         |
   +---------------+     |   +-----------------------------+         |
                         |                                           |
   +---------------+     |   +-----------------------------+         |
   | SMS gateway   |HTTPS|   | sms.daemon  / sms.poll      |         |
   |  (webhook or  |====>|   |   - parse, validate, spool  |         |
   |   poll API)   |     |   +--------------+--------------+         |
   +---------------+     |                  |                        |
                         |                  v                        |
                         |        ~/sms/incoming/*.txt               |
                         |        (plain text, mode 0600)            |
                         |                  ^                        |
                         |        phone sms list|read|tail|grep      |
                         |                                           |
                         |   +-----------------------------+         |
                         |   | nftables: default deny      |         |
                         |   |  - pins every allowed peer  |         |
                         |   |  - drops IPv6, pins DNS     |         |
                         |   +-----------------------------+         |
                         +-------------------------------------------+
```

The loopback interface is the only place the SMS receiver listens, so a hosted
gateway reaches it through a WireGuard or SSH tunnel rather than by exposing it
(`docs/PROVIDERS.md`).

## Quickstart: text, in about a minute

Nothing below needs a phone number, a provider, root, or network access.

```bash
cd phone
./bin/phone smsd &            # or: python3 -m sms.daemon

curl -sS -X POST http://127.0.0.1:8080/sms/incoming \
     -H 'Content-Type: application/json' \
     -d '{"from":"+15550109999","text":"hello from the terminal"}'

./bin/phone sms list
./bin/phone sms read latest
./bin/phone sms tail --follow          # stays open; new files appear as they land
./bin/phone sms verify                 # re-hash every file against its digest
./bin/phone sms grep -i hello
```

A message becomes one file, and that file is the message:

```
$ cat ~/sms/incoming/20261005-164501_15550109999_261054e106c0.txt
PHONE-SPOOL: 1
ID: 261054e106c0
FROM: +15550109999
DATE: 2026-10-05T12:00:00+00:00
SPOOLED: 2026-10-05T16:45:01.884417+00:00
SOURCE: webhook
TRANSPORT: sms
BYTES: 25
SHA256: 261054e106c0...
BODY:
hello from the terminal
```

Full grammar, filename policy, and guarantees: `docs/FORMAT.md`.

> The commonly posted reading command
> `tail -n +0 -f ~/sms/incoming/*.txt | grep -E '^FROM|^DATE|^BODY|==>'`
> does not work: the shell expands the glob once at start-up, so messages that
> arrive later are never opened, and `tail`'s `==>` separators interleave with
> the message text and corrupt anything downstream. `phone sms tail --follow`
> watches the directory for *new filenames* instead.

## Quickstart: voice

```bash
sudo install/build-pjsip.sh     # builds pjproject with TLS + SRTP, then verifies it
./bin/phone sip --init          # renders the config, generates a client certificate
$EDITOR ~/.config/phone/sip.conf   # registrar, username, password
./bin/phone sip --check         # refuses to start if anything is missing or degraded
./bin/phone sip                # register and wait for calls
./bin/phone sip -- 'sip:+15550100...@sip.example.net;transport=tls'
```

`phone sip --check` is the useful part. It fails, with an explanation, if:

* the config or the TLS private key is readable by anyone but you;
* template placeholders remain, or the password is empty;
* the installed `pjsua` was built without TLS or without SRTP;
* `--use-tls`, `--no-udp`, `--no-tcp`, `--use-srtp 2`, `--tls-ca-file` or
  `--tls-verify-server` is missing;
* the log level would print SIP messages (which is where SDES key material
  lives);
* `--srtp-secure 2` is set with `sip:` URIs, which pjsua will refuse to use.

## Firewall

```bash
sudo ./bin/phone firewall \
     --sip-host sip.example.net --sms-host sms.example.net \
     --resolver 127.0.0.1 --wg-port 51820 \
     --panic-timeout 120 --yes
```

* Default-deny in, out, and forward. Loopback stays open (where the SMS daemon
  lives).
* Every permitted peer is pinned by resolved address: SIP/TLS, SRTP media, the
  SMS gateway's HTTPS, your resolver, your WireGuard port. There is no rule
  that reaches "anywhere".
* IPv6 is dropped wholesale — a v4-only ruleset on a live v6 stack leaks around
  every rule you wrote.
* `--dry-run` prints the ruleset and changes nothing (no root needed).
* `--panic-timeout N` arms a self-rollback; cancel it with
  `phone firewall confirm`, or use `--unblock` / `--rollback`.
* Rules are validated with `nft --check` and the previous ruleset is backed up
  to `/etc/phone/backups/` before anything is applied.

It refuses to invent open rules: with no `--sip-host`/`--sms-host`/`--wg-port`
it stops rather than producing a ruleset that cannot reach anything.

## What changed from the draft specification

The draft I was handed contained several things that do not work as written.
Each is fixed here, and the reason is recorded because most of these are common
in SIP lockdown guides.

| Draft | Reality | Fix |
| --- | --- | --- |
| `./configure --with-ssl --with-srtp` | `--with-srtp` is not a pjproject option (autoconf silently ignores it); libsrtp is bundled and on by default | build with the real flags and **verify the built binary** advertises `--use-srtp` and `--use-tls`; the build fails if it does not |
| `--sip-border=1` | not a pjsua option at all | removed (the real knobs are `--bound-addr` / `--ip-addr`) |
| `--local-port=5061` with `--use-tls` | pjsua binds its TLS listener on **local port + 1**, so this puts TLS on 5062, breaking both interop and the firewall rule that opens 5061 | `--local-port 5060` → TLS on the standard 5061, with the rule documented in the config and checked by the launcher |
| (no transport controls) | `--local-port` implicitly enables UDP **and** TCP, so plaintext signalling would also be listening | `--no-udp` and `--no-tcp` are required by the launcher |
| `--srtp-secure=2` with `sip:` URIs | level 2 demands secure end-to-end transport (`sips:`); with `sip:` URIs pjsua refuses to establish media at all | default is `--srtp-secure 1` (SRTP requires TLS); selecting 2 without `sips:` URIs is rejected with an explanation |
| `--tls-verify-server` without a CA file | PJLIB's OpenSSL backend does not use the system trust store — it verifies against exactly what `--tls-ca-file` names, so "verification" would have nothing to verify against | `--tls-ca-file` is mandatory and must exist; `sip/gen-certs.sh` builds an explicit bundle |
| default log level | pjsua's default (5) prints full SIP messages, i.e. SDP, i.e. SDES SRTP key material, into your logs | `--log-level 3` enforced; the launcher refuses 4+ |
| `ufw allow out to any port 5061 / 443 / 53` | three wide-open doors: DNS to any resolver is a beacon per lookup, and HTTPS to anywhere keeps telemetry alive | every peer pinned by address; DNS only to your resolver; refusals when nothing is pinned |
| `ufw allow out to any port 10000:20000` | pjsua's default media window is 4000–4010, so this opens a range the phone never uses and blocks the range it does use — registered line, silent audio | default window 4000–4010, matched to `--rtp-port`, and the launcher warns when they disagree |
| (no IPv6 rules) | a v4-only ruleset leaves the v6 stack as an unruled path out | IPv6 dropped except loopback unless `--allow-ipv6` |
| (no lockout protection) | applying default-deny OUTPUT over SSH is a one-way trip if you get one rule wrong | backup + `nft --check` before apply, `--rollback`, and `--panic-timeout` armed rollback |
| `~/{timestamp}_{sender}.txt` | collides when two messages arrive in the same second, and a sender containing `../` influences the path | sanitised, bounded slug plus a content-addressed id; the spool directory is forced to 0700 and files to 0600 |
| Flask `sms_daemon.py` bound to `127.0.0.1:8080` | a framework dependency for what is a 200-line HTTP handler; also no authentication, size limit, or rate limit | stdlib `http.server` with a strict content-type allowlist, token auth (constant-time compare), body cap, per-client rate limiting, replay suppression, and a fail-closed refusal to bind a public interface without token + TLS |
| `tail -f ~/sms/incoming/*.txt \| grep …` | glob expanded once, so later messages are never read; cross-file output interleaves | `phone sms tail --follow` (and `read`, `grep`, `export`, `verify`) |

## Command reference

```
phone sms list [--limit N] [--since ISO] [--from S] [--json]
phone sms read {latest|<id>|<filename>} [--raw]
phone sms tail [--last N] [--follow] [--from-start]
phone sms grep PATTERN [-i] [--field all|from|body] [--json]
phone sms stats [--json]      phone sms verify [--json]
phone sms export [--format jsonl|txt|raw] [--out FILE]
phone sms purge --older-than DAYS [--yes]
phone sms config

phone smsd [--config F] [--host H] [--port N] [--token T] [--check-config]
phone sms-poll [--once] [--dry-run] [--url U] [--auth bearer:TOKEN]

phone sip [--init|--check|--print] [call...]
phone firewall [--sip-host H] [--sms-host H] [--resolver IP] [--wg-port N]
               [--rtp-range A-B] [--panic-timeout N] [--dry-run|--status|--unblock|--rollback|--confirm]
phone doctor        # preflight: what is present, what is degraded, what to fix
phone status        # what is running right now
```

`grep` exits 1 when nothing matches (so it composes in scripts). `--json` is
available wherever a script might want it. Message bodies never appear in logs,
error messages, or the journal — only metadata does.

## Security posture, and its limits

Enforced in code: TLS-only signalling, mandatory SRTP, real certificate
verification, secrets at mode 0600, no SIP message logging, loopback-only
receiving, token auth, size and rate limits, sanitised paths, atomic writes,
pinned firewall peers, dropped IPv6, pinned DNS, backups before every firewall
change.

**Not** solved by any of this: carrier and provider metadata (who you called,
when, from which IP), DHCP leases and hostname fingerprints, DNS still being
metadata even when local, and a host that is already compromised. The full
threat model, including the honest gaps, is in `docs/HARDENING.md`.

## Testing

```bash
./tests/run.sh                     # everything
./tests/run.sh python              # just the Python suites
PHONE_PYTHON=/usr/bin/python3 ./tests/run.sh
```

Four suites, ~230 assertions, none of which need root, a provider, or network
access:

| Suite | Covers |
| --- | --- |
| `tests/test_spool.py` | format round-trip, path-traversal sanitising, atomicity, permissions, caps, journal, digests |
| `tests/test_daemon.py` | field mapping, every rejection path (400/401/404/405/411/413/415/429/507), replay suppression, config safety |
| `tests/test_poll.py` | cursor handling, idempotence across restarts, retry-after-failed-write, auth, payload shapes |
| `tests/test_cli.py` | list/read/tail/grep/stats/verify/export/purge, including follow-mode seeing new files |
| `tests/test_sip.sh` | config linter against the real pjsua option list, and each launcher refusal |
| `tests/test_firewall.sh` | generated policy shape, "no unpinned destination" property, refusals |
| `tests/test_e2e.sh` | real daemon + real HTTP + real CLI, byte-identical body, UTF-8, auth |

## Layout

```
bin/phone              one command line for everything
sip/pjsua.conf.example hardened pjsua configuration (placeholders marked)
sip/phone.sh           launcher: verifies, then execs pjsua; refuses to degrade
sip/gen-certs.sh       client certificate + explicit CA bundle
sms/spool.py           the file format and atomic writer (the contract)
sms/config.py          INI + PHONE_SMS_* env + flags, fail-closed validation
sms/daemon.py          webhook receiver (stdlib HTTP)
sms/poll.py            puller with a crash-safe cursor
sms/cli.py             reader: list/read/tail/grep/stats/verify/export/purge
sms/sms.conf.example   documented configuration
firewall/lockdown.sh   default-deny ruleset, nftables or iptables
systemd/               sandboxed units for the receiver and the poller
install/build-pjsip.sh pjproject build that verifies TLS+SRTP afterwards
docs/                  FORMAT.md, HARDENING.md, PROVIDERS.md
tests/                 four suites + tests/run.sh
```

## Status

Text pipeline: implemented and covered by tests end-to-end. Voice: configuration,
launcher, certificate tooling and build script are implemented and tested against
a stub `pjsua`; the real binary is not installed in this environment, so live
registration and audio have not been exercised here. Firewall: implemented and
tested in `--dry-run` (the generated rulesets are asserted rule by rule); this
environment has no `nft`, so nothing has been loaded into a live kernel here.

No RCS, no MMS, no group messaging, no call recording, no voicemail, no GUI. If
that list is a problem, this is the wrong tool — which is rather the point.
