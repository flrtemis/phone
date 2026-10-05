# The spool format

One message is one file. That is the whole design, and everything else in this
repository is downstream of it. `cat`, `grep`, `wc`, `awk`, `find`, `rsync`,
`tar` and `mail` all work on the archive without knowing anything about this
project.

## Grammar

```
message   := header-line+ "BODY:" "\n" body
header-line := KEY ": " value "\n"
KEY       := [A-Z][A-Z0-9-]{0,31}
body      := verbatim UTF-8, no trailing newline added to the value
```

A file looks like this:

```
PHONE-SPOOL: 1
ID: 261054e106c0
FROM: +15550109999
DATE: 2026-10-05T12:00:00+00:00
SPOOLED: 2026-10-05T12:00:03.884417+00:00
SOURCE: webhook
TRANSPORT: sms
PROVIDER-ID: SM-e2e-1
BYTES: 74
SHA256: 9f2c1a0b3d4e5f60...
BODY:
Hello from the terminal.
Second line, with "quotes" and an em dash — ok.
```

### Headers

| Header | Required | Meaning |
| --- | --- | --- |
| `PHONE-SPOOL` | no | format version (currently `1`) |
| `ID` | no | first 12 hex chars of `SHA256`, used in filenames and by the CLI |
| `FROM` | **yes** | sender as the provider presented it, truncated to 64 chars |
| `DATE` | **yes** | time as *stated by the sender/provider*, ISO-8601 UTC |
| `SPOOLED` | no | local arrival time, microsecond precision |
| `SOURCE` | no | `webhook`, `poll`, `import`, … |
| `TRANSPORT` | no | `sms` today; the field exists so RCS/MMS can land here later |
| `PROVIDER-ID` | no | the gateway's own message id, when it gives one |
| `BYTES` | no | size of the body in bytes (not characters) |
| `SHA256` | no | digest over `sender \x1f received_at \x1f body` |

`DATE` and `SPOOLED` are deliberately separate. Providers stamp at second
resolution, some back-fill old messages, and filesystem mtimes are coarse on
some filesystems (and change when a directory is copied). `SPOOLED` is written
once, by this system, and never changes, so "which message landed first?" has
an exact answer even when several arrive in the same second.

`SHA256` covers `sender`, `received_at` and `body` — not `SPOOLED`, so adding
arrival metadata never invalidates a digest. `phone sms verify` recomputes it
and reports any file whose content was edited after it was spooled.

### Parsing rules

Header lines are consumed until a line that is exactly `BODY:`. Everything
after that line is the body, verbatim. This is why the body may itself contain
lines that look like headers, blank lines, or the literal text `BODY:` — there
is no escaping, no quoting, and no ambiguity, because the terminator is
positional and `BODY:` may appear at most once, at the end of the header
block.

A file that does not parse raises `CorruptMessageError`; the reader skips it
and counts it under `phone sms stats` as `corrupt` rather than silently
dropping it.

## Filenames

```
20261005-164501_15550109999_261054e106c0.txt
└─ timestamp ─┘ └─ slug ──┘ └── id ───┘
```

* The timestamp is derived from `DATE` in UTC, so a lexicographic sort of the
  directory is a chronological sort of the messages.
* The slug is `FROM` reduced by `slugify()`: NFKD-normalised, ASCII-folded, any
  character outside `[A-Za-z0-9._-]` collapsed to `-`, leading dots and dashes
  stripped, truncated to 24 characters. `../../etc/passwd` becomes `etc-passwd`
  and can never escape the spool directory; two different senders that reduce
  to the same slug still get distinct files.
* The id suffix is content-derived, so two messages arriving in the same second
  from the same sender cannot overwrite each other.

## Durability and permissions

Writing a message is: create `.NAME.PID.tmp` with mode `0600` and `O_EXCL`,
write, `fsync`, `os.replace()` onto the final name, then `fsync` the directory.
A reader therefore never observes a partial message, and an acknowledged
delivery survives a power cut (unless `fsync = false`). The spool directory is
forced to `0700`.

## The journal

`journal.log` is an append-only, tab-separated ledger, one line per write:

```
ID  SPOOLED  FROM  SOURCE  BYTES  SHA256  FILENAME
```

Metadata only — the body is never written to the journal, and neither is the
journal written to any log. Its purposes are auditability (what arrived, when,
from whom) and integrity checking after the fact. Tabs and newlines in a
sender string are replaced with spaces before they reach the ledger, so a
hostile `FROM` cannot forge extra records.

## Caps

The writer refuses to add a file beyond `max_files` / `max_bytes` (defaults:
20 000 files, 256 MiB) and the daemon answers `507`. This is a deliberate
fail-closed choice: a full disk breaks the whole host, and an SMS gateway is an
unauthenticated (or weakly authenticated) writer.

## Deliberate non-goals

* No index, database, or lock file: the directory *is* the queue.
* No deletion policy: `phone sms purge --older-than N` is explicit and requires
  `--yes`.
* No encryption at rest. Use full-disk encryption; anything this program did
  would be weaker than LUKS, and a key sitting next to the spool is theatre.
* No MIME parsing, no multipart reassembly, no delivery receipts. If a provider
  concatenates a long SMS before handing it over, that is what lands here.
