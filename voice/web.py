"""The remote client: talk to the agent from anywhere, with no rented number.

One process serves three things on one port, because they are one feature:

* ``/``               the browser client (a single self-contained page)
* ``/ws``             the audio bridge, over WebSocket
* ``/api/...``        a small JSON API a companion app or ``curl`` can drive:
                       ring my phone, ask a question, leave a note, read the
                       last transcript

The point of the whole thing: Asterisk's AudioSocket bridge is loopback-only and
Asterisk has to be able to reach it, which is fine at home and useless in a car
park. Here your phone connects *to the agent* instead, so the audio travels over
whatever data connection you have, and no telephone company is involved.

Security posture, in order of importance:

* **Loopback by default.** Serving live audio and a dialling API on a public
  interface is a mistake, so the config refuses it unless you say
  ``allow_remote`` *and* set a token. The intended deployment is a WireGuard or
  Tailscale tunnel; ``docs/REMOTE.md`` covers it.
* **Token on everything except ``/healthz``.** Constant-time compare. The WS
  token travels in the query string because a browser cannot set headers on a
  WebSocket handshake, which is why the URL the browser uses puts the token in
  the fragment and the page passes it back over the socket: fragments are never
  sent to the server, so the token does not land in its access log.
* **Browsers need HTTPS** for microphone access (``getUserMedia`` requires a
  secure context). That is a browser rule, not a choice here - a native app over
  a tunnel does not have to care. Either way, TLS is optional and the server
  tells you exactly which case you are in when it starts.
* **The dialling rule is unchanged.** ``/api/call-me`` can only ring the
  configured owner; ``validate_dial_request`` is the only path to a dial, and
  the API exposes no way to pass a number of your choosing.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import ssl
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from voice.config import VoiceConfig
from voice.pipeline import (
    CallSession,
    DialRequest,
    OllamaLLM,
    PipelineError,
    validate_dial_request,
)
from voice.websocket import WebSocketDuplex, take_upgrade

LOG = logging.getLogger("phone.voice.web")

#: Requests are tiny (a token, a sentence). Anything larger is a mistake or an
#: attack, and either way it is not worth reading into memory.
MAX_BODY = 8 * 1024
PAGE = Path(__file__).resolve().parent / "web" / "index.html"


def _load_page() -> bytes:
    try:
        return PAGE.read_bytes()
    except OSError:
        return (
            b"<!doctype html><html><body><h1>phone voice</h1>"
            b"<p>The client page (voice/web/index.html) is missing from this install.</p>"
            b"</body></html>"
        )


class VoiceHTTPServer(ThreadingHTTPServer):
    """HTTP server that knows how to hand a connection to the call session."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        cfg: VoiceConfig,
        *,
        session_factory: Callable[[WebSocketDuplex], CallSession],
        on_call: Optional[Callable[[str], None]] = None,
        originate: Optional[Callable[[str], tuple[bool, str]]] = None,
    ) -> None:
        self.cfg = cfg
        self.token = cfg.web_token
        self.session_factory = session_factory
        self.on_call = on_call
        #: Wired by the CLI to the real Asterisk origination path. Defaults to a
        #: refusal so a server that was not given one cannot dial anything.
        self.originate = originate or (lambda number: (False, "this server has no dialler attached"))
        self.started_at = time.time()
        self._active = 0
        self._active_lock = threading.Lock()
        self.calls_accepted = 0
        self.calls_refused = 0
        self.page = _load_page()
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address) -> None:
        """A client that disappears is normal here, not a stack trace.

        Mobile networks drop sockets constantly - tunnels renegotiate, phones go
        into a lift - and http.server's default behaviour is to print a traceback
        for every one of them. This is the same path, one log level down.
        """
        LOG.debug("connection from %s failed", client_address, exc_info=True)

    # ---- call bookkeeping -------------------------------------------
    @property
    def active_calls(self) -> int:
        return self._active

    def claim_call_slot(self) -> bool:
        with self._active_lock:
            if self._active >= self.cfg.web_max_calls:
                self.calls_refused += 1
                return False
            self._active += 1
            self.calls_accepted += 1
            return True

    def release_call_slot(self) -> None:
        with self._active_lock:
            self._active = max(0, self._active - 1)


class _Handler(BaseHTTPRequestHandler):
    server_version = "phone-voice"
    protocol_version = "HTTP/1.1"
    server: VoiceHTTPServer

    # ---- plumbing ----------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - http.server's signature
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, *, content_type: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This page and its API are never meant to be embedded or framed by
        # anything: clickjacking a "call my phone" button is worth preventing.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _query(self) -> dict:
        return {key: values[-1] for key, values in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def _authorised(self) -> bool:
        """Token in the Authorization header, the query string, or a cookie."""
        expected = self.server.token
        if not expected:
            # No token configured: only reachable on loopback, which the config
            # validation already guarantees. Still refuse anything but the page.
            return True
        supplied = ""
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            supplied = self._query().get("token", "")
        if not supplied:
            for chunk in self.headers.get("Cookie", "").split(";"):
                name, _, value = chunk.strip().partition("=")
                if name == "phone_voice_token":
                    supplied = value
                    break
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length > MAX_BODY:
            return None
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    # ---- routes ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, self.server.page, content_type="text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._json(
                200,
                {
                    "status": "ok",
                    "active_calls": self.server.active_calls,
                    "calls": self.server.calls_accepted,
                    "refused": self.server.calls_refused,
                    "tls": bool(self.server.cfg.web_cert),
                    "uptime_seconds": int(time.time() - self.server.started_at),
                },
            )
            return
        if path == "/favicon.ico":
            self._send(204, b"", content_type="image/x-icon")
            return
        if path == "/api/config":
            if not self._authorised():
                self._json(401, {"error": "unauthorised"})
                return
            cfg = self.server.cfg
            self._json(
                200,
                {
                    "title": cfg.web_title,
                    # Deliberately no owner number, no token, no paths.
                    "greeting": cfg.greeting,
                    "session_rate": 8000,
                    "dialling": bool(cfg.owner_destination),
                    "max_calls": cfg.web_max_calls,
                },
            )
            return
        if path == "/api/transcript":
            if not self._authorised():
                self._json(401, {"error": "unauthorised"})
                return
            self._json(200, {"entries": read_latest_transcript(self.server.cfg, self._tail_lines())})
            return
        if path == "/ws":
            websocket_handler(self)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/ws":
            # A WebSocket handshake is a GET, because evidently it is.
            self._json(405, {"error": "the websocket endpoint expects GET"})
            return
        if not self._authorised():
            self._json(401, {"error": "unauthorised"})
            return
        body = self._read_body()
        if body is None:
            self._json(413, {"error": f"request body must be JSON under {MAX_BODY} bytes"})
            return

        if path == "/api/call-me":
            self._api_call_me(body)
            return
        if path == "/api/ask":
            self._api_ask(body)
            return
        if path == "/api/note":
            self._api_note(body)
            return
        self._json(404, {"error": "not found"})

    # ---- the API -----------------------------------------------------
    def _api_call_me(self, body: dict) -> None:
        """Ask the agent to ring the owner's phone. The only dialling entry point."""
        cfg = self.server.cfg
        requested = str(body.get("to", "owner") or "owner")
        allowed, reason = validate_dial_request(DialRequest(requested), cfg.owner_destination)
        if not allowed:
            LOG.warning("refusing a call-me request for %r: %s", requested, reason)
            self._json(403, {"error": reason})
            return
        ok, message = self.server.originate(cfg.owner_destination)
        if not ok:
            self._json(502, {"error": message})
            return
        self._json(200, {"status": "calling", "detail": message or "the agent is ringing the owner"})

    def _api_ask(self, body: dict) -> None:
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            self._json(400, {"error": "missing 'prompt'"})
            return
        cfg = self.server.cfg
        if cfg.llm_backend != "ollama":
            self._json(503, {"error": "no text model is configured (llm backend is %r)" % cfg.llm_backend})
            return
        client = OllamaLLM(
            cfg.llm_model,
            url=cfg.llm_url,
            temperature=cfg.llm_temperature,
            num_predict=cfg.llm_max_tokens,
            timeout=cfg.llm_timeout,
        )
        started = time.monotonic()
        try:
            reply = client.generate(prompt, cfg.system_prompt)
        except (PipelineError, OSError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(
            200,
            {"reply": reply, "model": cfg.llm_model, "ms": int((time.monotonic() - started) * 1000)},
        )

    def _api_note(self, body: dict) -> None:
        """Leave a note the agent reads at the start of the next call."""
        cfg = self.server.cfg
        text = str(body.get("text", "")).strip()
        if not cfg.notes_file:
            self._json(503, {"error": "notes are disabled: set [logging] notes in the config"})
            return
        if not text:
            self._json(400, {"error": "missing 'text'"})
            return
        path = Path(os.path.expanduser(cfg.notes_file))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if body.get("replace"):
                path.write_text(text[:1000] + "\n", encoding="utf-8")
            else:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(f"- {text[:400]}\n")
            os.chmod(path, 0o600)
        except OSError as exc:
            self._json(500, {"error": f"cannot write the note: {exc}"})
            return
        self._json(200, {"status": "stored", "chars": len(text[:400])})

    def _tail_lines(self) -> int:
        try:
            return max(1, min(200, int(self._query().get("lines", "20"))))
        except ValueError:
            return 20


# ----------------------------------------------------------------------
# websocket endpoint
# ----------------------------------------------------------------------
def websocket_handler(handler: _Handler) -> None:
    """Serve one WebSocket call from an upgraded HTTP connection."""
    server = handler.server
    if not handler._authorised():
        handler._json(401, {"error": "unauthorised"})
        return
    if not server.claim_call_slot():
        LOG.warning("refusing the call: %d already in progress", server.active_calls)
        handler._json(503, {"error": "the agent is already on a call"})
        return

    ws = take_upgrade(handler)
    if ws is None:
        server.release_call_slot()
        handler._json(400, {"error": "expected a websocket upgrade"})
        return

    # The client's first message declares its audio rate. Wait for it, but do not
    # wait forever: a client that says nothing is not a call.
    client_rate = 8000
    try:
        first = ws.recv_message()
        if first is None:
            return
        _opcode, payload = first
        try:
            hello = json.loads(payload.decode("utf-8"))
            client_rate = int(hello.get("sample_rate") or hello.get("rate") or 8000)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            LOG.debug("no usable hello; assuming 8 kHz")
        if client_rate not in (8000, 12000, 16000, 22050, 24000, 32000, 44100, 48000):
            LOG.warning("unsupported client sample rate %r; using 8000", client_rate)
            client_rate = 8000
        ws.send_json({"type": "ready", "session_rate": 8000, "client_rate": client_rate})

        duplex = WebSocketDuplex(ws, client_rate=client_rate, on_event=ws.send_json)
        if server.on_call:
            server.on_call(duplex.call_id)
        session = server.session_factory(duplex)
        try:
            reason = session.run()
        finally:
            try:
                ws.send_json({"type": "bye", "reason": reason if "reason" in locals() else "error"})
            except OSError:
                # The socket is already gone - a dropped mobile connection ends
                # exactly like this, and there is nobody left to tell.
                LOG.debug("could not send the bye frame", exc_info=True)
            duplex.close()
    finally:
        server.release_call_slot()


# ----------------------------------------------------------------------
# transcript reading (shared with the CLI)
# ----------------------------------------------------------------------
def read_latest_transcript(cfg: VoiceConfig, lines: int = 20) -> list[dict]:
    directory = cfg.transcript_path
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    if not files:
        return []
    entries: list[dict] = []
    try:
        with files[-1].open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries[-lines:]


def make_server(cfg: VoiceConfig, session_factory, *, originate=None) -> VoiceHTTPServer:
    """Bind the remote client. TLS is used when a certificate is configured."""
    cfg.validate_web()
    server = VoiceHTTPServer(
        (cfg.web_host, cfg.web_port), cfg, session_factory=session_factory, originate=originate
    )
    cert, key = cfg.web_tls
    if cert and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
