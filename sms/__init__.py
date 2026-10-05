"""phone.sms - a text pipeline with no UI, no database, and no vendor SDK.

Three pieces, one shared format:

  sms.spool   the on-disk format and the only durable interface (plain text)
  sms.daemon  an HTTP webhook receiver for push-style gateways
  sms.poll    a puller for gateways that only expose "give me messages since X"
  sms.cli     the reader: list, read, tail, grep, verify, stats, purge

Everything is Python standard library. Nothing leaves the host.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
