# phone voice for Android

A client for the agent in `../voice/`, over the same protocol the browser page
speaks. It exists for the things a browser cannot do on a phone:

* **`ws://` inside a VPN works.** A browser refuses microphone access over plain
  HTTP (only `localhost` is exempt); a native app has no such rule, so
  Tailscale/WireGuard users can skip TLS certificates entirely.
* **The call survives the screen going off**, via a foreground service with a
  visible notification for exactly as long as the microphone is live.
* **Handset audio.** `VOICE_COMMUNICATION` capture turns on the platform's echo
  canceller — without it the agent hears itself through your speaker.

## Building it

```bash
cd android
gradle assembleDebug            # or: open this directory in Android Studio
adb install app/build/outputs/apk/debug/app-debug.apk
```

Requirements: Android SDK 34, JDK 17. No Gradle wrapper is committed (a wrapper
JAR is a binary blob in a repository that otherwise has none); use your own
Gradle 8.5+, or let Android Studio supply one.

## Pointing it at your server

1. On the machine running the agent:

   ```bash
   phone voice web --host 0.0.0.0 --allow-remote --token "$(openssl rand -hex 24)"
   ```

   It prints a URL with the token in the fragment. The app takes the same two
   values: **server** (`wss://host:8443`) and **token**.

2. Or hand them over with a link, which is what a QR code in a terminal would
   encode:

   ```
   phonevoice://connect?url=http://100.101.102.103:8443&token=...
   ```

3. Inside a VPN, plain `http://` is fine — uncomment the `domain-config` block in
   `res/xml/network_security_config.xml` for your tunnel's address range. Outside
   one, use `wss://` with a certificate the device trusts (a tailnet with a
   public certificate is the least work; see `../docs/REMOTE.md` §6).

## What it does not do

* **It cannot use your carrier number.** No application can: inbound calls to a
  mobile number are delivered by the carrier's IMS to the SIM that owns it.
  Calling the agent on your real number means a rented DID bridged into your
  Asterisk — `../docs/REMOTE.md` §1 and §3 — or a SIP client like Linphone
  registering to your own Asterisk over a VPN.
* **It does not record in the background.** Audio flows only while the call screen
  is open, only to your server, and the notification is the receipt.
* **It has not been built or run.** This file and the sources under it were
  written in an environment with no Android SDK and no device, so treat the first
  build as a real step rather than a formality. The protocol it speaks is tested
  on the server side (`tests/test_voice_web.py`), which is where a bug would
  actually hurt: a wrong frame size or a missing `hello` shows up there as a
  failing test, not as a mystery in the app.

## Layout

```
app/src/main/java/dev/phone/voice/
  Protocol.kt      the wire format and the settings that address a server
  AudioEngine.kt   AudioRecord/AudioTrack, echo cancellation, playback jitter buffer
  VoiceSession.kt  the WebSocket, the conversation, barge-in on the client side
  AgentApi.kt      /api/call-me, /api/note, /api/ask, /api/transcript
  CallService.kt   the foreground service that keeps a call alive
  MainActivity.kt  one Compose screen
app/src/main/res/  strings, theme, an icon drawn as vector paths (no binaries)
```
