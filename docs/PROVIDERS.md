# Wiring it to real providers

This project is provider-agnostic: it speaks SIP/TLS+SRTP for voice and plain
HTTP for text. What follows is the shape of the wiring, per provider type.

## Voice: a SIP provider or your own PBX

You need three things from the provider: a registrar host, a username/password,
and confirmation that they support SIP over TLS (`;transport=tls`) with SRTP.

### Fast path: provisioning

```bash
phone sip init                                  # template + client certificate
printf '%s' "$SIP_PASSWORD" | \
  phone sip provision --provider telnyx --username 1234567 \
                      --password-stdin --did +15550109999
phone sip check && phone sip test-tls && phone sip doctor --audio
```

`sip/providers/` holds ready-made account blocks for **telnyx**, **flowroute**,
**twilio** (marked unverified: confirm the hostname and CA yourself) and
**asterisk** (your own PBX — the only option where no third party sees your
signalling or media). Each file ends with a checklist of what must be true on
the provider's side; provisioning prints it when it finishes.

Provisioning writes the config; it does not contact the provider and does not
verify your credentials. `test-tls` covers reachability and certificates;
whether the *password* is right is only knowable by registering (`phone sip`).

### Doing it by hand

1. Edit `~/.config/phone/sip.conf` (created by `phone sip init`):

   ```
   --registrar sip:sip.yourprovider.example:5061;transport=tls
   --id       sip:terminal7@sip.yourprovider.example
   --realm    sip.yourprovider.example
   --username terminal7
   --password ...
   --contact  sip:terminal7@sip.yourprovider.example:5061;transport=tls
   ```

   Note the `;transport=tls` parameter on every URI that must be encrypted.
   Without it pjsua resolves the URI for UDP, finds no UDP transport (you
   disabled it), and registration fails.

   Write the semicolon literally. **Do not escape it as `\;`** — a widely
   repeated mistake (and one this project's own first template made). pjsua's
   config reader performs no backslash unescaping, so an escaped semicolon
   survives into the URI as a literal backslash and the transport parameter is
   misparsed. The launcher now refuses such a config rather than letting you
   debug a registration timeout.

   Two related parser facts: `#` ends a line *anywhere*, so a password
   containing `#` must be written `"pa#ss"` in quotes, and each line is read
   into a 200-byte buffer, so keep URIs short. Both are checked.

2. Verify, do not assume:

   ```
   phone sip check           # refuses to start if anything is degraded
   phone sip test-tls        # DNS, TCP, handshake, certificate, SIP OPTIONS
   phone sip doctor --audio  # which mic/speakers will be used
   phone sip                 # register and wait for calls
   ```

3. Place a call:

   ```
   phone sip -- 'sip:+15550100...@sip.yourprovider.example;transport=tls'
   ```

### Audio: microphone and speakers

pjsua opens ALSA directly — no PulseAudio or PipeWire indirection — and refers
to devices by the index *it* enumerates, which is not ALSA's card,device pair.

```
phone sip doctor --audio     # /dev/snd, group membership, arecord -l, aplay -l,
                             # and (with --audio) pjsua's own index list
phone sip -- --capture-dev 3 --playback-dev 3    # try it for one run
sudo usermod -aG audio "$USER"                   # if it cannot open the device
```

If `/dev/snd` is missing you are almost certainly in a container without the
sound devices passed through (`--device /dev/snd` for docker/podman), in which
case no configuration can give pjsua a microphone: use `--null-audio` with
`--play-file`/`--rec-file` to exercise signalling, and run the real client on
the host.

Test the hardware without pjsua in the way:

```
arecord -f dat -d 3 /tmp/mic.wav && aplay /tmp/mic.wav
```

### If the provider only does SDES and not DTLS-SRTP

SDES puts the SRTP keys in the SDP, which is protected by the TLS signalling
channel rather than end-to-end. That is the common case and still far better
than plaintext RTP, but it means the TLS leg must be genuine (see the CA
requirement in `docs/HARDENING.md`). If the provider supports DTLS-SRTP, enable
`--srtp-keying 1` in the config to prefer it.

### If your provider requires registration from a known IP

Pin that address as the `--contact`/`--id` host, or run the tunnel below. Do not
disable TLS to work around NAT problems; fix the NAT (`--outbound`,
`--bound-addr`, or reach the provider over WireGuard).

### Your own PBX (Asterisk/FreeSWITCH)

Create a PJSIP endpoint with `transport=tls`, SRTP mandatory
(`media_encryption=sdes` or `dtls`), and upload the client certificate from
`sip/gen-certs.sh` if you enable client-certificate authentication
(`--tls-verify-client` on their side). Then use the PBX as the registrar above.

## Text: a webhook-capable SMS gateway

Any gateway that can POST JSON to a URL works. Point it at
`http://127.0.0.1:8080/sms/incoming` — but a loopback address is unreachable
from a hosted gateway, so in practice the gateway reaches you through a tunnel
(see below), and the receiver stays bound to loopback.

1. Configure the gateway's webhook URL and, if it supports it, an
   `Authorization: Bearer <token>` header (put the same value in
   `[auth] token` in `sms.conf`).

2. Map its field names if they are unusual:

   ```
   [fields]
   sender = From,originator,msisdn
   body   = Body,text,content
   ```

3. Start the receiver: `phone smsd` (or the systemd unit).

4. Watch it land:

   ```
   phone sms tail --follow
   ```

### Gateways that cannot push: poll instead

```
[poll]
url = https://sms.example.com/api/v1/messages?since={since}
auth = bearer:YOUR_TOKEN
json_path = data.messages
interval = 30
```

Then `phone sms-poll` (or `phone-sms-poll.service`). The poller keeps a state
file so restarts and repeated cursors never double-spool. Run *either* the
poller or the webhook receiver for a given gateway, never both.

## Getting a hosted gateway to a loopback-only receiver

The receiver must not be exposed to the internet. Bring the gateway traffic
through a tunnel instead:

### WireGuard (recommended for a hosted gateway)

```
# on this host: wg0 = 10.8.0.2, peer = 10.8.0.1
# run a tiny reverse proxy or the receiver on the tunnel address:
phone smsd --host 10.8.0.2 --token "$(cat /etc/phone/sms.token)"
```

Because that address is non-loopback, the daemon requires **both** a token and
TLS (fail closed):

```
[sms.conf]
allow_public_bind = true
token = <the shared secret>
tls_cert = /etc/phone/tls/smsd.pem
tls_key  = /etc/phone/tls/smsd.key
```

and open the tunnel in the firewall with `--wg-port 51820`. A self-signed
certificate is fine here if the gateway allows you to pin it; otherwise use a
certificate from a CA the gateway trusts.

### SSH reverse tunnel (no gateway support needed)

```
ssh -N -R 8080:127.0.0.1:8080 gateway-host
```

Keep the receiver on `127.0.0.1` and let the tunnel terminate the exposure.
Nothing is bound publicly on this machine.

### Reverse proxy

If a web server already terminates TLS on this host, proxy `/sms/incoming` to
`127.0.0.1:8080` and set `trust_proxy = true` so rate limiting keys on
`X-Forwarded-For` instead of on the proxy's address.

## Testing without a provider

```
# a webhook by hand
curl -sS -X POST http://127.0.0.1:8080/sms/incoming \
     -H 'Content-Type: application/json' \
     -d '{"from":"+15550109999","text":"hello"}'

# a SIP call with no audio device and no provider (media flows as RTP/SRTP)
phone sip -- 'sip:echo@127.0.0.1;transport=tls'
```

`--null-audio` (commented out in the shipped config) starts pjsua with no sound
card, which is useful on a server or in a test harness.
