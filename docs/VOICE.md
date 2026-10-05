# Putting a local language model on a telephone line

This document is about `phone voice`: a local Ollama model that can hold a spoken
conversation on a real phone line — answering your calls, and calling your phone
back — without any of it leaving your machine.

It also answers, directly, the question that started this: *can my local model
call my mobile over VoLTE?* The short answer is in the next section, and it is a
"no, but here is the thing that does work".

---

## 1. VoLTE, plainly

**VoLTE is a handset-side capability, not a server-side one.** VoLTE means your
*phone* attaches to the carrier's IMS core over a dedicated bearer, using a
SIM-based identity and carrier-provisioned credentials. The radio, the SIM, and
the carrier's IMS registration are all required. A server has none of them, so
"my Ollama model makes a VoLTE call" cannot be done — not because of a missing
library, but because the identity that makes the call is in the SIM.

What *is* possible, and what this code does:

| Piece | How the agent reaches your mobile |
| --- | --- |
| **You call the agent** | A rented DID (or a SIP app on your LAN) terminates the call on your Asterisk, which hands the audio to the model |
| **The agent calls you** | Asterisk originates a call to your mobile number over a SIP trunk; the carrier bridges it to your phone over VoLTE, exactly as for any other call |
| **You, free, on the same LAN** | A SIP app or ATA registers to your Asterisk as extension 100 and dials 600 — no telephone company involved at all |

So: **the model half is free and local. The PSTN half is rented**, because
someone has to pay the carrier that owns the mobile network at the other end.
There is no free interconnect, and anything claiming otherwise is either a trial
credit that expires or a service that is listening to your calls.

| Path | Cost | What it needs |
| --- | --- | --- |
| SIP app on the LAN → extension 600 | **£0 / $0** | Asterisk on a machine on your network, a free softphone (Linphone, Bria, PhonerLite) |
| Inbound real number → agent | ~$1–3/month + ~$0.003–0.01/min inbound | A DID from a SIP provider (Telnyx, Flowroute, Twilio) |
| Agent → your mobile | Same DID, ~$0.005–0.02/min outbound | A trunk, and your mobile number as the one allowed destination |
| Hosted competitor, for comparison | $2/month + $0.10/min | Someone else's servers, someone else's transcript |

Numbers are typical US list prices and move constantly; check your provider.
The point is the *shape*: a few dollars a month and fractions of a cent per
minute, versus a tenth of a dollar per minute for a hosted agent.

---

## 2. How it fits together

```
   your softphone / ATA            your mobile
   (extension 100, TLS+SRTP)       (over the PSTN)
             |                          |
             |  SIP over TLS :5061      |  PSTN, bridged by your trunk
             v                          v
     +------------------------------------------------+
     |                 Asterisk (chan_pjsip)          |
     |  dialplan: extension 600 -> AudioSocket()      |
     +------------------------+-----------------------+
                              |  TCP loopback: raw 8 kHz slin
                              |  (one 20 ms frame per message)
                              v
     +------------------------------------------------+
     |  phone voice serve   (this repository)         |
     |                                                |
     |   PCM --> VAD/utterance --> whisper (ASR)      |
     |             |                                  |
     |             +--> Ollama (gemma4:26b) --stream-> |
     |             |                                  |
     |             +--> Piper (TTS) --> PCM ----------+
     |                                                |
     |   JSONL transcript per call, on disk locally   |
     +------------------------------------------------+
```

Three deliberate choices in that diagram:

* **Asterisk dials the bridge, not the other way round.** The `AudioSocket()`
  dialplan application opens a TCP connection *out* to `phone voice serve`,
  which is why the bridge can sit on loopback with no authentication and still
  be reachable. It also means the bridge can restart without the PBX caring.
* **The protocol is 3 bytes of header plus PCM.** Type byte, two-byte
  *big-endian* length, payload. `0x10` is 8 kHz signed linear; `0x00` is a
  hangup; `0x03` is DTMF. That is the entire transport, and it is implemented in
  `voice/audiosocket.py` with the standard library only.
* **The model never sees a phone number.** Dialling is a separate, validated
  intent — see section 6.

### Why not just use pjsua (the `phone sip` path) for this?

`pjsua` is excellent at being a *phone* — registration, TLS, SRTP, codecs,
audio devices — and it is still what `phone sip` runs. It is a poor host for a
conversation loop: it has no way to hand you the audio of a live call for
processing, and it owns the sound card. Asterisk + AudioSocket exists precisely
to be the media bridge for external processing, so that is what the agent uses.
Both can run on the same machine; they do not share state.

---

## 3. Hardware, models, and what to expect

The examples assume an NVIDIA GPU with **24 GB or more** of VRAM, which is what
this was written against. Everything also runs on CPU, slowly.

| Component | Model | VRAM (or RAM) | Notes |
| --- | --- | --- | --- |
| ASR | `faster-whisper` `small.en` | ~1 GB | The latency sweet spot for 8 kHz speech. `medium.en` is better on names and numbers, ~2 GB, noticeably slower |
| ASR | `faster-whisper` `large-v3` | ~3 GB (int8) / ~5 GB (float16) | Only worth it if accents matter more than latency |
| LLM | `gemma4:26b` (mixture-of-experts, ~4B active) | ~15 GB at Q4 + ~1 GB context | **The right choice for a voice loop**: near-26B comprehension at roughly 4B speed |
| LLM | `gemma4:31b` (dense) | ~17.5 GB at Q4 + ~1 GB context | Fits 24 GB, better for long reasoning, slower per token — use it between calls, not on them |
| LLM | `gemma4:12b` | ~8 GB | If you want headroom for whisper and Piper on the same GPU, or a second concurrent call |
| TTS | Piper `en_US-amy-medium` | ~60 MB, CPU | ~100–300 ms for a sentence. `--length_scale` via `tts.speed` |

`phone voice doctor` reports what your machine actually has, including a VRAM
estimate for each model.

**The single most important Ollama detail.** Ollama loads *every* model at
**2048 tokens of context**, regardless of what the model supports. The system
prompt plus a dozen turns of history will silently evict the oldest turns and
the model will appear to forget what it was told two turns ago. Fix it once:

```bash
phone voice modelfile                 # writes ~/.config/phone/Modelfile
ollama create phone-voice -f ~/.config/phone/Modelfile
# then set  model = phone-voice  in ~/.config/phone/voice.conf
```

The generated Modelfile sets `num_ctx 8192`, a low temperature, `num_predict
120`, and a system prompt written for speech rather than text.

Two other Ollama knobs worth knowing:

```bash
export OLLAMA_KEEP_ALIVE=30m     # do not unload between calls; loading costs seconds
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_HOST=127.0.0.1:11434   # the default; never expose this to the network
```

Without `keep_alive`, the first turn of every call pays the model load — several
seconds of silence that sounds exactly like a broken agent.

---

## 4. Five-minute quickstart (free path)

No account, no DID, no telephone company. One machine running Asterisk, one
phone running a free SIP app.

```bash
# 1. the bridge's configuration
mkdir -p ~/.config/phone
cp voice/voice.conf.example ~/.config/phone/voice.conf
$EDITOR ~/.config/phone/voice.conf      # set [dial] owner to your mobile number

# 2. the models
ollama pull gemma4:26b
pip install faster-whisper              # ASR (pulls in numpy)
pip install piper-tts && python -m piper.download_voices en_US-amy-medium
phone voice modelfile && ollama create phone-voice -f ~/.config/phone/Modelfile

# 3. prove it before touching the phone system
phone voice doctor        # models, GPU, config, dial safety, what is missing
phone voice dial-test     # 911 and every non-owner number must be refused
phone voice demo          # the whole pipeline with no models at all
phone voice ask "say hello in six words" --timings

# 4. Asterisk
phone voice provision --dir ~/asterisk-conf
sudo apt install asterisk
sudo cp ~/asterisk-conf/{pjsip,extensions,rtp}.conf /etc/asterisk/
sudo systemctl restart asterisk
sudo asterisk -rx "pjsip show endpoints"    # your handset should register

# 5. run the agent
phone voice serve
```

On the phone: install a SIP app, register it to your Asterisk host as extension
`100` with **TLS** and the password from `pjsip.conf`, then dial **600**.

Test extension **601** first — it is an echo test, and it tells you in five
seconds whether the problem is audio (codec, jitter, echo) or the agent. If 601
sounds bad, the model is not the problem.

If you want to hear something *before* installing any model at all, the pjsua leg
can play a file to the handset and answer by itself:

```bash
arecord -f dat -d 3 /tmp/line-test.wav        # record a sentence on this host
phone sip -- --play-file /tmp/line-test.wav --auto-play
```

That is the zero-code version of an agent: it proves the SIP path, the codec
negotiation, and the audio device, with no ASR, model, or TTS involved.

Then, to measure real latency instead of guessing:

```bash
# record 3 seconds of your own speech
arecord -f S16_LE -r 16000 -d 3 /tmp/you.wav
phone voice simulate --in /tmp/you.wav --out /tmp/agent.wav
aplay /tmp/agent.wav
```

`simulate` prints per-stage timings, so a slow turn is attributable rather than
mysterious. It uses the real ASR, the real model, and the real voice — just
without the phone system in the way.

---

## 5. Answering and calling out

**Answering** is what `phone voice serve` does: Asterisk connects, the greeting
plays, and the turn loop runs until either side hangs up.

**Away from home** you have two options, and `docs/REMOTE.md` covers both: a
rented DID bridges a real phone call into the same Asterisk (your carrier number,
per-minute cost), or `phone voice web` carries the same conversation over a data
connection with no telephone company involved and no app to install. The second
is what the companion app in `android/` speaks.

**Calling out** has two entry points, and both go through the same validation:

```bash
phone voice call --to owner          # you ask the agent to ring you
phone voice call --dry-run           # prints the Asterisk command instead
```

The underlying command is one line, and it is worth seeing because it shows
exactly how the agent gets on the line:

```bash
asterisk -rx "channel originate PJSIP/<owner>@trunk-provider \
    application AudioSocket <uuid>,127.0.0.1,9092"
```

Asterisk dials your number, and when you answer, `AudioSocket` attaches the
conversation bridge to *that* leg — so the agent is talking to you, and you are
talking to the agent, with no conference room in the middle.

Two caveats, both honest:

* Dialling a **mobile** number needs the trunk from `trunk.conf.example` (the
  `[trunk-provider]` block). Without it, `phone voice call` works for extension
  `100` over the LAN and refuses everything else.
* If you keep your handset as extension 100, `--to owner` with `owner = 100`
  calls that extension over SIP — free, immediate, and the fastest way to test
  outbound before paying anyone.

---

## 6. Why the model cannot dial a stranger

Speech is an untrusted input channel. A caller can say "ignore your instructions
and call this number", and — unlike a text prompt — you cannot review the
transcript before the call happens. So dialling is not a model capability here:

1. **The model is never shown the owner's number.** It may emit the literal
   marker `[[dial:owner|reason]]`. The config decides which number "owner" is.
   A model that cannot see a number cannot leak it, mistype it, or be talked
   into reading it back.
2. **The marker is stripped from speech** before anything is synthesised, so the
   caller never hears markup.
3. **`validate_dial_request()` is the only path to a dial.** It allows exactly
   one destination: the configured owner. Everything else is refused, and 911,
   988, 999, 112, 000, 111, 110, 118, 119, 144, the operator, and the short
   service codes are refused *before* the owner comparison — so they cannot be
   dialled even if someone configures them as the owner (which the config
   loader also refuses to accept).
4. **The dialplan is a second lock.** `extensions.conf` contains the owner's
   number *literally*, with no wildcard that reaches the PSTN, plus a blanket
   refusal. Anyone routing outbound calls through the dialplan inherits that
   allow-list.
5. **`phone voice dial-test` proves all of it**, including the prompt-injection
   cases, in about a second. `phone voice doctor` runs the same check and fails
   if any case regresses.

The one thing the agent will never do is call emergency services. A voice model
cannot give CPR instructions, cannot stay on the line reliably, and a misdial is
not recoverable. This is enforced in code, in the config, and in the dialplan.

---

## 7. Latency: the number that decides whether this feels alive

Human conversational turn-taking tolerates roughly **one second** of silence.
Everything below is about staying inside that, and about knowing which stage to
blame when you do not.

| Stage | Typical (24 GB GPU) | Where the time goes |
| --- | --- | --- |
| Wait for the caller to stop | 240 ms | The VAD's hangover — deliberate; cutting sooner clips the last word |
| ASR (whisper `small.en`, 3 s utterance) | 100–300 ms | Greedy decoding (`beam_size=1`) trades a little accuracy for latency |
| LLM first token (`gemma4:26b`, warm) | 200–500 ms | Prompt processing; grows with history, which is why `history_turns` is 12 |
| TTS (Piper, one sentence) | 100–300 ms | First sentence only — playback starts while the rest synthesises |
| **Total, to first word** | **~0.7–1.4 s** | |

Design decisions that come out of that table:

* **Replies are cut at 400 characters** (`num_predict 120` in the Modelfile as
  well). A model that starts an essay cannot hold the line.
* **A 26B mixture-of-experts model beats a 31B dense one here.** Same story on
  comprehension, ~4B of active parameters worth of speed.
* **Playback is paced against the wall clock.** Dumping a three-second reply
  into the socket as fast as the CPU can write it makes Asterisk queue the
  surplus, and the delay grows with every turn.
* **Barge-in is on by default.** While the agent speaks, it keeps reading
  inbound audio and stops mid-sentence when you start talking. Audio received
  while it was speaking is kept, not discarded, so the first syllable of your
  interruption is not lost.
* **Sentence-by-sentence synthesis.** Piper runs per sentence, and the first
  sentence starts playing while the second is being made.

When it feels slow, measure, then turn one knob:

| Symptom | Knob |
| --- | --- |
| Every turn is slow, all stages fast | `history_turns` down; `num_ctx` down; check `OLLAMA_KEEP_ALIVE` |
| ASR dominates | `asr.model = small.en`; check `asr.device = cuda` (`doctor` says which) |
| TTS dominates | `tts.model` → a `low` quality Piper voice; `tts.speed` up |
| First turn of every call is slow | the model is being unloaded between calls: set `OLLAMA_KEEP_ALIVE=30m` |
| The agent talks over you | `barge_in.threshold` down (more sensitive), `barge_in.frames` down |
| The agent stops when you breathe | `barge_in.threshold` up, `barge_in.frames` up |
| It never stops listening | `conversation.silence_timeout`, `max_call_seconds` |

---

## 8. Audio quality, honestly

The bridge carries **8 kHz, 16-bit, mono** — narrowband, the same band as a
traditional phone call, because that is what Asterisk's AudioSocket hands over.
There is no wideband path here, and no amount of money changes that: the PSTN
leg is 8 kHz, and a softphone registered over the LAN can negotiate `ulaw` /
`alaw` (also 8 kHz) with this dialplan.

Practical consequences:

* Whisper is trained on 16 kHz audio and *upsamples* 8 kHz input internally. It
  works well, but plosives and fricatives are diminished: expect occasional
  confusion between "s" and "f", and read numbers back when they matter.
* **Echo cancellation belongs to the endpoint, not the server.** Asterisk's
  `AudioSocket` is a raw media tap; it does not cancel echo. A softphone or ATA
  does. On the LAN path your phone's SIP app handles it; on a mobile call the
  carrier does.
* **Use extension 601 to isolate audio problems.** It is a plain echo test. If
  601 is clean and the agent sounds choppy, the problem is ASR/TTS timing, not
  the media path.
* DTMF is sent as `rfc4733` out-of-band and is never fed to the ASR — in-band
  tones would be transcribed as digits in the middle of a sentence.

---

## 9. Privacy, and the security of the bridge

What leaves your machine when you talk to this agent, on the free path:
**nothing.** ASR, the model, and TTS are all local. There is no account, no
telemetry, no per-character API call, and the transcript is a JSONL file in
`~/voice/transcripts`.

If you add a DID, what leaves is the audio of the call itself — to your carrier,
because that is what a telephone call is.

The security properties worth stating plainly:

* The AudioSocket carries **raw, unauthenticated audio**. That is why
  `voice.conf` defaults to `127.0.0.1` and `phone voice serve` refuses any other
  address unless you set `allow_public_bind = true` *and* firewall the port to
  your PBX only. If those bytes escape onto a network, it is an open microphone.
* Signalling to the handset is **TLS only** with **SRTP mandatory** and
  `media_encryption_optimistic=no`, so a client that does not offer encryption
  is rejected rather than downgraded. There is no UDP or TCP transport in the
  generated `pjsip.conf`.
* `direct_media=no`, so the media always passes through the Asterisk on your
  machine rather than being negotiated peer-to-peer.
* Transcripts are written by the process that owns the call, with the
  permissions your umask gives them; put them on an encrypted volume if the
  conversations matter.
* Ollama's API is bound to loopback. Do not put it behind a reverse proxy;
  it has no authentication and it can execute a model.

---

## 10. Legal and ethical notes

Not legal advice, but the constraints you must know about:

* **FCC/TCPA (US):** AI-generated voices are treated as "artificial" under the
  TCPA. Calls to *other people* with a synthetic voice require consent and
  disclosure, and state law may add its own requirements. Calling *yourself* is
  not a problem. Recording calls is another matter — **Pennsylvania, where this
  project was written, is an all-party consent state**, so tell the other party
  before you record.
* **Provider AUP:** your SIP provider's acceptable-use policy governs automated
  calling, and registration (STIR/SHAKEN attestation) may be impacted. Read it
  before you dial anyone but yourself.
* **Never 911.** Enforced in code, config, and the dialplan. Do not try to
  "improve" that.
* **Do not point this at people who do not know it is an AI.** The whole design
  assumes one caller: you.

---

## 11. Troubleshooting

| Symptom | Most likely cause | What to run |
| --- | --- | --- |
| `phone voice serve` exits immediately | Port in use, or config refused (public bind, bad backend) | `phone voice doctor` |
| Asterisk says "failed to connect to AudioSocket" | The bridge is not running, or `BRIDGE_HOST`/`BRIDGE_PORT` in the dialplan disagree with the config | `phone voice serve`, then `ast log` / `asterisk -rx "dialplan show from-handset"` |
| The call connects and there is silence | TTS cannot find its voice, or the model is unreachable | `phone voice doctor`; `phone voice ask hi --timings` |
| The agent never hears you | VAD threshold too high for a quiet handset, or no audio is reaching the bridge | test 601; raise `barge_in.threshold` for noise, lower it for quiet |
| Audio is choppy, robot-like | Unpaced playback (old version) or CPU starvation; check `playback_ms` in the log | `phone voice simulate --in you.wav` to see per-stage timings |
| Answers are wrong or repetitive | `num_ctx` is still 2048, or history is being evicted | `phone voice modelfile` |
| Everything works but sounds like a 1990s phone call | It is a 1990s phone call: 8 kHz narrowband | nothing to fix; that is the medium |
| `ollama: model requires more system memory` | The model does not fit, or another model is loaded | `ollama ps`; use `gemma4:12b` or `26b` |

---

## 12. What cannot be verified from here

This project was developed in a sandbox with no sound card, no PBX, no SIP
provider route, and no GPU, and the tests are written accordingly. What that
means for you:

* **No live call has ever been placed by this code.** The protocol, the server,
  the session loop, the config generator, and the safety rules are covered by
  tests (120 of them for this feature alone, including a fake Asterisk on a real
  socket); a real call is not.
* **No audio was ever heard.** Audio levels, echo, and the VAD thresholds need
  a human on a real line — that is why the echo test at 601 and the offline
  `simulate` command exist.
* **Model latency numbers are estimates.** The design choices that bound
  latency are tested; the milliseconds are yours to measure.
* **Trunk configuration is a template.** Provider-side settings change; the
  checklist in `sip/providers/*.conf` and `voice/trunk.conf.example` is where to
  start, not a guarantee.
* **No browser has ever opened the remote client**, and the Android app has never
  been compiled - there is no Android SDK, no device and no browser in this
  environment. What *is* tested is the server side of that protocol, including a
  complete conversation driven over a real socket: a wrong frame size, a missing
  `hello`, or a token check that leaks would fail those tests. The client-side
  audio code (AudioWorklet capture, playback scheduling) is the part to expect to
  adjust first; `phone voice web` prints the URL and the token, and the page
  reports what it is doing in its own log.

Everything that *can* be tested is: `./tests/run.sh` runs the whole thing with
no root, no network, and no models installed.
