#!/usr/bin/env python3
"""sms.daemon - receive SMS over HTTP, write it to a plain text file, exit.

There is no database, no ORM, no message broker, no web framework, and no
template engine here. A message arrives over HTTP, gets validated, gets
written to a file with ``os.replace``, and the file is the message. That is
the entire pipeline.

Threat posture (all of it enforced in code, not in documentation):

  * binds loopback by default and *refuses to start* on a public interface
    unless you explicitly opt in with TLS + a token (fail closed)
  * optional shared-secret auth, compared in constant time
  * hard cap on request body size, per-client token-bucket rate limiting
  * strict content-type allowlist; no method other than POST on the spool
    route; no directory listings; no version banner
  * the sender string is sanitised by ``spool.slugify`` before it can touch
    a path, and every filename is content-addressed
  * message bodies are never logged

Run:
    python3 -m sms.daemon --config ~/.config/phone/sms.conf
    python3 -m sms.daemon --check-config
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):  # allow `python3 sms/daemon.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sms import __version__
from sms.config import Config, ConfigError
from sms.spool import (
    Message,
    Spool,
    SpoolError,
    SpoolFullError,
    iso,
    parse_iso,
    utcnow,
)

LOG = logging.getLogger("phone.sms.daemon")

MAX_CONTENT_LENGTH = 64 * 1024  # absolute ceiling; config can only lower it


# ----------------------------------------------------------------------
# payload extraction
# ----------------------------------------------------------------------
def first_present(data: dict[str, Any], keys: list[str]) -> Optional[Any]:
    """Return the first non-empty value among ``keys`` (case-insensitive)."""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def normalise_time(value: Any) -> str:
    """Accept ISO-8601, epoch seconds, epoch milliseconds, or nothing."""
    if value in (None, ""):
        return iso(utcnow())
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return iso(datetime.fromtimestamp(number, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return iso(utcnow())
    if isinstance(value, str):
        return iso(parse_iso(value))
    return iso(utcnow())


def extract(payload: Any, cfg: Config, raw_text: str = "") -> Message:
    """Map an arbitrary gateway payload onto a :class:`Message`.

    Accepts a dict, a list containing one dict, or a bare string.
    """
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if isinstance(payload, str):
        payload = {"text": payload}

    if not isinstance(payload, dict):
        raise SpoolError("payload must be a JSON object, a list, or plain text")

    body = first_present(payload, cfg.body_fields)
    if body is None and raw_text:
        body = raw_text
    if body is None:
        raise SpoolError("no message body found in payload")
    if isinstance(body, (dict, list)):
        raise SpoolError("message body must be a string")
    body = str(body)
    if "\x00" in body:
        body = body.replace("\x00", "")
    if not body.strip():
        raise SpoolError("empty message body")

    sender = first_present(payload, cfg.sender_fields)
    sender = "unknown" if sender is None else str(sender)
    sender = " ".join(sender.split())[:64] or "unknown"

    provider_id = first_present(payload, cfg.id_fields)
    provider_id = "" if provider_id is None else str(provider_id)[:64]

    return Message(
        sender=sender,
        body=body,
        received_at=normalise_time(first_present(payload, cfg.time_fields)),
        source="webhook",
        provider_id=provider_id,
    )


# ----------------------------------------------------------------------
# rate limiting
# ----------------------------------------------------------------------
class RateLimiter:
    """Per-client token bucket. Cheap, bounded, no dependency."""

    def __init__(self, per_minute: int, burst: int, max_clients: int = 1024) -> None:
        self.per_minute = max(1, per_minute)
        self.burst = max(1, burst)
        self.max_clients = max_clients
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        window = 60.0
        with self._lock:
            hits = self._hits.get(client)
            if hits is None:
                if len(self._hits) >= self.max_clients:
                    self._hits.popitem(last=False)
                hits = self._hits[client] = deque(maxlen=self.burst)
            else:
                self._hits.move_to_end(client)
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= self.burst:
                return False
            hits.append(now)
            return True


class Deduper:
    """Gateways retry webhooks. Suppress exact replays in a bounded window."""

    def __init__(self, window: float = 300.0, capacity: int = 4096) -> None:
        self.window = window
        self.capacity = capacity
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        if self.window <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            for old_key, stamp in list(self._seen.items()):
                if now - stamp > self.window:
                    self._seen.pop(old_key, None)
                else:
                    break
            if key in self._seen:
                self._seen.move_to_end(key)
                return True
            self._seen[key] = now
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
            return False


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
class SmsHandler(BaseHTTPRequestHandler):
    server_version = "phone-sms"
    sys_version = ""  # do not advertise the interpreter
    protocol_version = "HTTP/1.1"
    timeout = 15

    # injected by make_server()
    cfg: Config
    spool: Spool
    limiter: RateLimiter
    deduper: Deduper

    # ---- plumbing ----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.info("%s - %s", self._client_id(), fmt % args)

    def _client_id(self) -> str:
        if self.cfg.trust_proxy:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()[:64]
        return self.client_address[0]

    def _send(self, status: int, payload: Any, extra: Optional[dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorised(self) -> bool:
        import hmac

        if not self.cfg.token:
            return True
        presented = self.headers.get("X-Phone-Token", "")
        auth = self.headers.get("Authorization", "")
        if not presented and auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        return hmac.compare_digest(presented, self.cfg.token)

    def _read_body(self) -> Optional[bytes]:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._send(411, {"status": "error", "message": "chunked bodies are not accepted"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"status": "error", "message": "bad Content-Length"})
            return None
        if length <= 0:
            return b""
        ceiling = min(self.cfg.max_body_bytes, MAX_CONTENT_LENGTH)
        if length > ceiling:
            self._send(413, {"status": "error", "message": f"body larger than {ceiling} bytes"})
            return None
        try:
            return self.rfile.read(length)
        except (OSError, ValueError):
            self._send(400, {"status": "error", "message": "truncated body"})
            return None

    # ---- routes ------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != self.cfg.path.rstrip("/"):
            self._send(404, {"status": "error", "message": "no such endpoint"})
            return
        if not self._authorised():
            self._send(401, {"status": "error", "message": "unauthorised"})
            return
        if not self.limiter.allow(self._client_id()):
            self._send(
                429,
                {"status": "error", "message": "rate limited"},
                extra={"Retry-After": "60"},
            )
            return

        raw = self._read_body()
        if raw is None:
            return
        if not raw:
            self._send(400, {"status": "error", "message": "empty request body"})
            return

        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        text = raw.decode("utf-8", "replace")
        try:
            if ctype in ("application/json", "text/json", "application/x-json") or (
                not ctype and text.lstrip()[:1] in "{[\""
            ):
                payload = json.loads(text)
            elif ctype in ("application/x-www-form-urlencoded", "multipart/form-data"):
                payload = {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}
            elif ctype in ("text/plain", "", "text/csv"):
                payload = {"text": text}
            else:
                self._send(415, {"status": "error", "message": f"unsupported content-type {ctype!r}"})
                return
        except json.JSONDecodeError as exc:
            self._send(400, {"status": "error", "message": f"invalid JSON: {exc.msg}"})
            return

        try:
            message = extract(payload, self.cfg, raw_text=text if ctype == "text/plain" else "")
        except SpoolError as exc:
            self._send(400, {"status": "error", "message": str(exc)})
            return

        dedupe_key = message.provider_id or message.sha256
        if self.deduper.seen(dedupe_key):
            LOG.info("duplicate suppressed from %s (%d bytes)", message.sender, message.body_bytes)
            self._send(200, {"status": "duplicate", "id": message.id})
            return

        try:
            path_written = self.spool.write(message)
        except SpoolFullError as exc:
            LOG.error("spool full: %s", exc)
            self._send(507, {"status": "error", "message": "spool capacity reached"})
            return
        except SpoolError as exc:
            self._send(400, {"status": "error", "message": str(exc)})
            return
        except OSError as exc:
            LOG.error("write failed: %s", exc)
            self._send(500, {"status": "error", "message": "spool write failed"})
            return

        # Metadata only. The body never reaches the log.
        LOG.info(
            "spooled %s from %s (%d bytes) -> %s",
            message.id,
            message.sender,
            message.body_bytes,
            path_written.name,
        )
        self._send(
            201,
            {
                "status": "spooled",
                "id": message.id,
                "file": path_written.name,
                "bytes": message.body_bytes,
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/healthz":
            files = 0
            total = 0
            try:
                with os.scandir(self.spool.dir) as it:
                    for entry in it:
                        if entry.name.endswith(".txt"):
                            files += 1
                            total += entry.stat().st_size
            except OSError:
                pass
            self._send(
                200,
                {
                    "status": "ok",
                    "version": __version__,
                    "spool": {"files": files, "bytes": total, "dir": str(self.spool.dir)},
                    "auth": bool(self.cfg.token),
                },
            )
            return

        if path == "/sms/recent":
            if not self._authorised():
                self._send(401, {"status": "error", "message": "unauthorised"})
                return
            if not self.cfg.expose_recent:
                self._send(403, {"status": "error", "message": "recent listing disabled"})
                return
            try:
                limit = max(1, min(int(query.get("limit", ["20"])[0]), 500))
            except ValueError:
                limit = 20
            messages = []
            for msg, file_path in self._recent_pairs(limit):
                messages.append(
                    {
                        "id": msg.id,
                        "from": msg.sender,
                        "date": msg.received_at,
                        "file": file_path.name,
                        "body": msg.body,
                    }
                )
            self._send(200, {"status": "ok", "count": len(messages), "messages": messages})
            return

        if path == "/":
            self._send(200, {"status": "ok", "service": "phone-sms", "version": __version__})
            return

        self._send(404, {"status": "error", "message": "no such endpoint"})

    def _recent_pairs(self, limit: int):
        """Newest first by local arrival time, so two messages in the same
        second still come back in the order they actually landed."""
        rows = []
        for path in self.spool.paths():
            try:
                message = Message.load(path)
            except (SpoolError, OSError):
                continue
            rows.append((message.arrival, path.name, message, path))
        rows.sort(key=lambda row: (row[0], row[1]))
        for _arrival, _name, message, path in reversed(rows[-limit:]):
            yield message, path

    def _method_not_allowed(self) -> None:
        self._send(405, {"status": "error", "message": "method not allowed"}, extra={"Allow": "POST, GET"})

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_DELETE = do_PATCH = do_PUT

    def do_HEAD(self) -> None:  # noqa: N802
        """A HEAD response must carry no body - answer with headers only."""
        self.send_response(405)
        self.send_header("Allow", "POST, GET")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


class SmsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def make_server(cfg: Config, spool: Optional[Spool] = None) -> SmsHTTPServer:
    """Build (but do not start) the HTTP server. Used by tests too."""
    spool = spool or Spool(
        cfg.spool_path,
        max_files=cfg.max_files,
        max_bytes=cfg.max_bytes,
        fsync=cfg.fsync,
        journal=cfg.journal,
    )
    spool.ensure()

    handler = type(
        "BoundSmsHandler",
        (SmsHandler,),
        {
            "cfg": cfg,
            "spool": spool,
            "limiter": RateLimiter(cfg.max_requests_per_minute, cfg.max_burst),
            "deduper": Deduper(
                window=float(getattr(cfg, "dedupe_window", 300)),
                capacity=int(getattr(cfg, "dedupe_capacity", 4096)),
            ),
        },
    )

    server = SmsHTTPServer((cfg.host, cfg.port), handler)
    server.spool = spool  # type: ignore[attr-defined]

    if cfg.tls_cert and cfg.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.options |= getattr(ssl, "OP_NO_COMPRESSION", 0)
        context.load_cert_chain(certfile=cfg.tls_cert, keyfile=cfg.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        LOG.info("TLS enabled (min TLS 1.2) with %s", cfg.tls_cert)

    return server


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone-smsd",
        description="Minimal SMS webhook receiver: HTTP in, plain text files out.",
    )
    parser.add_argument("--config", help="path to sms.conf")
    parser.add_argument("--host", help="override bind address (loopback only unless configured)")
    parser.add_argument("--port", type=int, help="override bind port")
    parser.add_argument("--spool-dir", help="override spool directory")
    parser.add_argument("--token", help="override shared-secret token")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration, print it with secrets redacted, and exit",
    )
    parser.add_argument("--version", action="version", version=f"phone-smsd {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.spool_dir:
        cfg.spool_dir = args.spool_dir
    if args.token:
        cfg.token = args.token
    try:
        cfg.validate()
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    if args.check_config:
        print(cfg.render())
        if cfg.port == 0:
            print(
                "warning: port 0 asks the kernel for an arbitrary free port. That is fine for a\n"
                "         self-test, but a provider webhook needs a fixed port: set [daemon] port."
            )
        print("configuration OK")
        return 0

    try:
        server = make_server(cfg)
    except OSError as exc:
        LOG.error("cannot bind %s:%s (%s)", cfg.host, cfg.port, exc)
        return 1

    scheme = "https" if cfg.tls_cert else "http"
    LOG.info("spooling to %s", cfg.spool_path)
    LOG.info("listening on %s://%s:%s%s", scheme, cfg.host, cfg.port, cfg.path)
    if not cfg.is_loopback:
        LOG.warning("bound to a NON-loopback interface: %s", cfg.host)

    stop = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        LOG.info("signal %s: shutting down", signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
