"""Configuration: INI file + environment + CLI, in that precedence order.

Deliberately ``configparser`` (stdlib) rather than TOML/YAML so the daemon
runs unmodified on Python 3.8+ with zero pip packages installed. That is
the whole point of this tool: a text pipeline should not need a dependency
tree to receive a text.
"""

from __future__ import annotations

import hashlib
import os
import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATHS = (
    "~/.config/phone/sms.conf",
    "~/.phone/sms.conf",
)

# Gateways disagree about field names. Rather than patching code per
# provider, we try a list of candidate keys in order.
DEFAULT_SENDER_FIELDS = "from,From,sender,msisdn,number,originator,source_address,src"
DEFAULT_BODY_FIELDS = "text,body,message,msg,content,txt,Body"
DEFAULT_TIME_FIELDS = "timestamp,date,ts,sent_at,received_at,created_at,time"
DEFAULT_ID_FIELDS = "id,message_id,messageId,sid,uuid,guid"


class ConfigError(Exception):
    """Raised when configuration is missing or unsafe."""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    # --- webhook receiver ---
    host: str = "127.0.0.1"
    port: int = 8080
    token: str = ""
    trust_proxy: bool = False
    allow_public_bind: bool = False
    tls_cert: str = ""
    tls_key: str = ""
    path: str = "/sms/incoming"
    expose_recent: bool = True
    max_body_bytes: int = 64 * 1024
    max_requests_per_minute: int = 60
    max_burst: int = 10
    dedupe_window: int = 300      # seconds; 0 disables replay suppression
    dedupe_capacity: int = 4096

    # --- spool ---
    spool_dir: str = "~/sms/incoming"
    max_files: int = 20_000
    max_bytes: int = 256 * 1024 * 1024
    fsync: bool = True
    journal: bool = True

    # --- field mapping ---
    sender_fields: list[str] = field(default_factory=lambda: _split(DEFAULT_SENDER_FIELDS))
    body_fields: list[str] = field(default_factory=lambda: _split(DEFAULT_BODY_FIELDS))
    time_fields: list[str] = field(default_factory=lambda: _split(DEFAULT_TIME_FIELDS))
    id_fields: list[str] = field(default_factory=lambda: _split(DEFAULT_ID_FIELDS))

    # --- poller ---
    poll_url: str = ""
    poll_method: str = "GET"
    poll_interval: int = 30
    poll_auth: str = ""            # "bearer:TOKEN" | "basic:user:pass"
    poll_json_path: str = "messages"
    poll_since_param: str = "since"
    poll_extra_headers: str = ""
    state_file: str = "~/.local/state/phone/poll.state.json"

    # --- bookkeeping ---
    source_path: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def spool_path(self) -> Path:
        return Path(os.path.expanduser(self.spool_dir))

    @property
    def token_digest(self) -> str:
        if not self.token:
            return ""
        return hashlib.sha256(self.token.encode()).hexdigest()[:12]

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")

    def validate(self) -> None:
        """Refuse configurations that would quietly expose the spool."""
        # Port 0 means "any free port" to the kernel. That is legitimate for a
        # self-test or an ephemeral run, and useless for a webhook receiver a
        # provider has to reach, so it is allowed here and flagged loudly by
        # --check-config.
        if not (0 <= self.port < 65536):
            raise ConfigError(f"port out of range: {self.port}")
        if not self.path.startswith("/"):
            raise ConfigError(f"path must start with '/': {self.path}")
        if not self.is_loopback and not self.allow_public_bind:
            raise ConfigError(
                f"refusing to bind {self.host} without allow_public_bind; "
                "keep the receiver on loopback and let a WireGuard/SSH tunnel in"
            )
        if not self.is_loopback:
            if not self.token:
                raise ConfigError("a non-loopback bind requires auth_token (fail closed)")
            if not (self.tls_cert and self.tls_key):
                raise ConfigError("a non-loopback bind requires tls_cert + tls_key")
        if self.tls_cert and not self.tls_key:
            raise ConfigError("tls_cert set without tls_key")
        if Path(os.path.expanduser(self.tls_cert)).exists() is False and self.tls_cert:
            raise ConfigError(f"tls_cert not found: {self.tls_cert}")
        if self.max_body_bytes <= 0:
            raise ConfigError("max_body_bytes must be positive")
        if self.poll_url and not self.poll_url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise ConfigError("poll_url must be https:// (or loopback http for a local gateway)")

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None, *, env: bool = True) -> "Config":
        cfg = cls()
        chosen: Optional[Path] = None
        if path:
            chosen = Path(os.path.expanduser(path))
            if not chosen.is_file():
                raise ConfigError(f"config file not found: {chosen}")
        else:
            for candidate in DEFAULT_CONFIG_PATHS:
                p = Path(os.path.expanduser(candidate))
                if p.is_file():
                    chosen = p
                    break
        if chosen:
            cfg._apply_file(chosen)
            cfg.source_path = str(chosen)
        if env:
            cfg._apply_env()
        cfg.validate()
        return cfg

    def _apply_file(self, path: Path) -> None:
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except configparser.Error as exc:
            raise ConfigError(f"{path}: {exc}") from exc

        def get(section: str, option: str, default: str) -> str:
            if parser.has_option(section, option):
                return parser.get(section, option).strip()
            return default

        def getbool(section: str, option: str, default: bool) -> bool:
            if not parser.has_option(section, option):
                return default
            try:
                return parser.getboolean(section, option)
            except ValueError as exc:
                raise ConfigError(f"{path}: [{section}] {option} is not a boolean") from exc

        def getint(section: str, option: str, default: int) -> int:
            if not parser.has_option(section, option):
                return default
            try:
                return parser.getint(section, option)
            except ValueError as exc:
                raise ConfigError(f"{path}: [{section}] {option} is not an integer") from exc

        self.host = get("daemon", "host", self.host)
        self.port = getint("daemon", "port", self.port)
        self.token = get("auth", "token", self.token)
        self.trust_proxy = getbool("daemon", "trust_proxy", self.trust_proxy)
        self.allow_public_bind = getbool("daemon", "allow_public_bind", self.allow_public_bind)
        self.tls_cert = get("daemon", "tls_cert", self.tls_cert)
        self.tls_key = get("daemon", "tls_key", self.tls_key)
        self.path = get("daemon", "path", self.path)
        self.expose_recent = getbool("daemon", "expose_recent", self.expose_recent)
        self.max_body_bytes = getint("daemon", "max_body_bytes", self.max_body_bytes)
        self.max_requests_per_minute = getint("daemon", "max_requests_per_minute", self.max_requests_per_minute)
        self.max_burst = getint("daemon", "max_burst", self.max_burst)
        self.dedupe_window = getint("daemon", "dedupe_window", self.dedupe_window)
        self.dedupe_capacity = getint("daemon", "dedupe_capacity", self.dedupe_capacity)

        self.spool_dir = get("spool", "dir", self.spool_dir)
        self.max_files = getint("spool", "max_files", self.max_files)
        self.max_bytes = getint("spool", "max_bytes", self.max_bytes)
        self.fsync = getbool("spool", "fsync", self.fsync)
        self.journal = getbool("spool", "journal", self.journal)

        if parser.has_option("fields", "sender"):
            self.sender_fields = _split(get("fields", "sender", DEFAULT_SENDER_FIELDS))
        if parser.has_option("fields", "body"):
            self.body_fields = _split(get("fields", "body", DEFAULT_BODY_FIELDS))
        if parser.has_option("fields", "time"):
            self.time_fields = _split(get("fields", "time", DEFAULT_TIME_FIELDS))
        if parser.has_option("fields", "id"):
            self.id_fields = _split(get("fields", "id", DEFAULT_ID_FIELDS))

        self.poll_url = get("poll", "url", self.poll_url)
        self.poll_method = get("poll", "method", self.poll_method).upper()
        self.poll_interval = getint("poll", "interval", self.poll_interval)
        self.poll_auth = get("poll", "auth", self.poll_auth)
        self.poll_json_path = get("poll", "json_path", self.poll_json_path)
        self.poll_since_param = get("poll", "since_param", self.poll_since_param)
        self.poll_extra_headers = get("poll", "extra_headers", self.poll_extra_headers)
        self.state_file = get("poll", "state_file", self.state_file)

    def _apply_env(self) -> None:
        """PHONE_SMS_* overrides - keeps the token out of the config file if
        you prefer (e.g. read from a systemd credential)."""
        env = os.environ
        if v := env.get("PHONE_SMS_HOST"):
            self.host = v
        if v := env.get("PHONE_SMS_PORT"):
            try:
                self.port = int(v)
            except ValueError as exc:
                raise ConfigError("PHONE_SMS_PORT is not an integer") from exc
        if v := env.get("PHONE_SMS_TOKEN"):
            self.token = v
        if v := env.get("PHONE_SMS_SPOOL_DIR"):
            self.spool_dir = v
        if v := env.get("PHONE_SMS_TLS_CERT"):
            self.tls_cert = v
        if v := env.get("PHONE_SMS_TLS_KEY"):
            self.tls_key = v
        if v := env.get("PHONE_SMS_POLL_URL"):
            self.poll_url = v
        if v := env.get("PHONE_SMS_POLL_AUTH"):
            self.poll_auth = v

    # ------------------------------------------------------------------
    def render(self, *, redact: bool = True) -> str:
        """Human-readable effective config. Secrets are never printed."""
        token = "<unset>"
        if self.token:
            token = f"<set sha256:{self.token_digest}>" if redact else self.token
        auth = "unset"
        if self.poll_auth:
            auth = self.poll_auth.split(":", 1)[0] + ":<redacted>" if redact else self.poll_auth
        lines = [
            f"config file      : {self.source_path or '<defaults only>'}",
            f"listen           : {self.host}:{self.port} (tls={'on' if self.tls_cert else 'off'})",
            f"webhook path     : {self.path}",
            f"auth token       : {token}",
            f"trust proxy hdrs : {self.trust_proxy}",
            f"spool dir        : {self.spool_path}",
            f"spool caps       : {self.max_files} files / {self.max_bytes} bytes",
            f"fsync            : {self.fsync}",
            f"journal          : {self.journal}",
            f"body fields      : {', '.join(self.body_fields)}",
            f"sender fields    : {', '.join(self.sender_fields)}",
            f"id fields        : {', '.join(self.id_fields)}",
            f"poll url         : {self.poll_url or '<unset>'} ({self.poll_method}, every {self.poll_interval}s)",
            f"poll auth        : {auth}",
            f"poll state file  : {os.path.expanduser(self.state_file)}",
        ]
        return "\n".join(lines)
