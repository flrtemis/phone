# Calling the agent from anywhere

At home, the agent is free to reach: a SIP app on your LAN dials extension `600`,
Asterisk hands the audio to the model, and nothing leaves your network. Away from
home, the LAN is gone and the question changes. This document answers it, starting
with the part that has no workaround.

---

## 1. Why an app cannot put your carrier number on the agent

The short version: **your mobile number does not belong to your phone. It belongs
to your SIM, and incoming calls for it are delivered by your carrier's network.**

Concretely, when someone dials your number:

1. The call is routed by the PSTN to whichever carrier owns the number range.
2. That carrier's IMS core looks up where the SIM currently is — this is the
   same machinery as VoLTE, and the registration is authenticated with keys
   inside the SIM.
3. The call is delivered over the radio to that handset. If the phone is off, it
   goes to voicemail *at the carrier*.

There is no API, no permission, and no app that can insert itself into step 2.
An app can answer a call that has already arrived (that is just a telephony API),
and an app can *place* a call that shows your number (the carrier stamps the
caller ID because the call comes from your SIM). What an app cannot do is make
your number ring somewhere other than the SIM that owns it.

So "install a companion app and the agent can reach me on my real number" is not
possible. Coming at it from the other direction does not help either: no
application can register a SIP endpoint *as* your carrier number, because the
number is not an account you hold — it is a range the carrier owns.

Two things that *are* possible, both of which this project supports:

| # | Path | Money | Your carrier number | Rings |
| --- | --- | --- | --- | --- |
| A | **Data path** — the app or browser opens a socket to your server | free | not involved | in the app |
| B | **PSTN path** — a rented DID bridges the call into Asterisk | ~$1–3/mo + ~$0.005–0.02/min | genuinely used | your phone's normal ringer |

The third thing people imagine — forwarding your mobile number to the agent —
is just path B with an extra forwarding charge: your carrier forwards to the DID,
the carrier bills you for the forwarded leg, and inbound calls to *your* number
then ring the agent instead of you. Useful as an answering machine, expensive as
a conversation.

## 2. Path A: the remote client (this is `phone voice web`)

```
  your phone, away from home
   +-----------------------------------+
   |  browser  or  companion app       |
   |  mic -> PCM frames -> WebSocket   |
   +----------------+------------------+
                    |  wss:// (or ws:// inside a VPN)
                    v
   +-----------------------------------+        +-----------------------+
   |  phone voice web                  |        |  Asterisk             |
   |   /            browser client     |        |  (optional, for the   |
   |   /ws          audio bridge  <----+--------+   PSTN path and for   |
   |   /api/call-me ring your phone    |        |   extension 600)      |
   |   /api/ask     text prompt        |        +-----------------------+
   |   /api/note    brief the agent    |
   +-----------------------------------+
```

```bash
# on the machine at home
phone voice web                       # 127.0.0.1:8443, prints a tokenised URL
phone voice web --host 0.0.0.0 --allow-remote --cert ~/.config/phone/tls/web.pem --key ...
```

Open the printed URL, press **Call the agent**, and talk. The page is a single
self-contained HTML file — no CDN, no framework, no external anything, because a
client that phones home would defeat the point of the project.

What the remote client can do:

* **Talk to the model**, with the same pipeline the phone line uses: the same
  VAD, the same whisper, the same Ollama, the same barge-in, the same transcript.
* **Ring your own phone** (`Ring my phone`, or `POST /api/call-me`): the server
  asks Asterisk to call your number and puts the agent on that leg, so you get a
  normal phone call from your own agent. This needs the PSTN path below.
* **Leave a note** (`POST /api/note`): the agent reads it at the start of the next
  call. "I'm driving, keep it under a sentence." A remote client cannot watch a
  terminal, so this is how you brief it.
* **Read the last transcript** (`GET /api/transcript`), so you can see what was
  said without shelling in.

### The two things that will bite you

**Microphone access requires HTTPS.** `getUserMedia` is refused in an insecure
context, and `http://192.168.1.20:8443` is an insecure context (only
`localhost` is exempt). A native app has no such rule — it can use `ws://` inside
a tunnel — which is a genuine reason to build the companion app even though the
browser client works. Options, cheapest first:

* **Native app over a tunnel** (path A, no TLS certificates to manage).
* **A tunnel with TLS**: Tailscale, Cloudflare Tunnel, or WireGuard with a
  self-signed certificate you install as a CA on the phone.
* **Your own certificate**: see `sip/gen-certs.sh`, and the trust note below.

**Do not port-forward this to the internet.** It carries live audio and a
dialling API. Put it behind WireGuard or Tailscale, bind it to loopback or the
VPN interface, and keep the token. `phone voice web --host 0.0.0.0` requires an
explicit `--allow-remote` *and* a token for exactly this reason.

---

## 3. Path B: a real phone number

Everything here is the same plumbing as `docs/VOICE.md` §5, with the difference
that the call arrives from the carrier rather than being originated by you.

1. Rent a DID from a SIP provider (Telnyx, Flowroute, Twilio — templates in
   `sip/providers/`). Roughly $1–3/month, plus per-minute.
2. Point the DID at your Asterisk (the provider's console does this; the exact
   screen differs per vendor, which is why the templates exist).
3. Add an inbound context that sends the call to the agent, and point the
   trunk's `context=` at it — the block is written out in
   `voice/trunk.conf.example`:

```
[from-trunk]
exten => <your DID>,1,NoOp(inbound call for the agent)
 same => n,Answer()
 same => n,AudioSocket(${UNIQUEID},${BRIDGE_HOST},${BRIDGE_PORT})
 same => n,Hangup()
exten => _X.,1,Hangup()
```

4. Run `phone voice serve` next to Asterisk. Now dialling your DID reaches the
   agent, from anywhere, on your normal phone app, with no app installed at all.

The cost of a 10-minute call is roughly five to twenty cents, and the audio is
narrowband 8 kHz — the same quality as any other phone call, because it *is* one.

### When you want both

They coexist. A sensible arrangement:

* `phone voice serve` — the PSTN path (inbound DID, outbound to your mobile).
* `phone voice web` — the data path (when you are on Wi-Fi, or abroad, or when
  you want the agent to read you a transcript).
* `phone voice note "flight lands at 6"` from a laptop, so whichever path you use
  next, the agent already knows.

Both write the same JSONL transcripts, so `phone voice transcript` shows one
history no matter how you called.

---

## 4. The companion app

`android/` contains a complete Android client for path A: it speaks the same
protocol as the browser page, and adds what a browser cannot give you on a phone.

What is genuinely better than the browser client:

| | Browser | Companion app |
| --- | --- | --- |
| Works over `ws://` in a VPN | no (microphone blocked without HTTPS) | **yes** |
| Keeps the call alive with the screen off | no | **yes** (foreground service) |
| Answers a real incoming phone call with the agent | no | **yes** — via the PSTN path, using an ACTION_DIAL hand-off |
| Echo cancellation tuned for handsets | whatever the browser does | `VOICE_COMMUNICATION` source, AEC/NS/AGC |
| Notes and "ring my phone" | yes | yes |

What it deliberately is **not**:

* **Not a VoIP client for your carrier number.** It cannot be, per §1. If you
  want the agent on your carrier number, that is path B (a DID) or a SIP client
  like Linphone registering to your own Asterisk over a VPN.
* **Not a background covert recorder.** It records only while the call screen is
  open and a foreground notification says so, and it never sends audio anywhere
  but your server.
* **Not prebuilt.** No APK is committed here — a signed APK depends on your
  keystore and your server's certificate. `android/README.md` has the three
  commands to build it.

---

## 5. What is verified, and what is not

Verified in this repository, by tests:

* the WebSocket layer (RFC 6455 framing, the handshake accept key from the RFC's
  own example, masking rules, fragmentation, ping/pong, size caps);
* a whole conversation over a real socket: greeting → caller speech → ASR →
  model → audio back, plus the transcript written to disk;
* the remote API's refusals: no token, wrong token, oversized body, unknown
  route, and `/api/call-me` refusing 911 and any number that is not the
  configured owner;
* the configuration's refuse-by-default posture for a public bind.

Not verified here, because this sandbox has no Android toolchain and no browser:
the Android build and the page's microphone path. The protocol between them is
tested on the server side, and the page uses no library that could not be read in
one sitting. Build it, run it, and expect to adjust the sample rate on the app's
settings screen if your device refuses 16 kHz capture.

## 6. Certificate trust for your own server

If you provide your own certificate (`--cert`/`--key`), the app and the browser
both reject it unless the phone trusts the CA that signed it. Two sane options:

* **Your own CA** (what `sip/gen-certs.sh` builds): install `ca-cert.pem` as a
  user CA on the phone (`Settings → Security → Install certificate → CA
  certificate`, on Android; `Settings → General → About → Certificate Trust
  Settings` on iOS). This is the private-CA path and works offline.
* **A tunnel that terminates TLS for you** (Tailscale, Cloudflare): then the
  certificate is a real public one, trust is already correct, and the app needs
  no configuration at all. This is what most people should do.

Never solve a trust error by disabling verification in the app. The app has no
option to do so, and `phone voice web` will not start with a certificate it
cannot read.
