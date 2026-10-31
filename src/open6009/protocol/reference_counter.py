"""Counter request/reply shapes from NI Base 15 falling-edge captures.

The read-response width is observed; byte order of a *nonzero* count still
requires a pulse source for physical confirmation.
"""

from __future__ import annotations

DISCOVER = bytes.fromhex("0000000c0008010000000000")
DISCOVER_REPLY = bytes.fromhex("0000001c0018010000000002020000000132c0000100000000020000")
CONFIGURE_FALLING_UP = bytes.fromhex("00000010000c010f0220000000000000")
START = bytes.fromhex("0000000c0008010902200000")
READ = bytes.fromhex("0000000c0008010e02200000")
STOP = bytes.fromhex("0000000c0008010c02200000")
OK = bytes.fromhex("0000000c0008010000000002")
READ_PREFIX = bytes.fromhex("00000010000c010000000002")


def decode_counter_reply(reply: bytes) -> tuple[bytes, int]:
    """Return the four raw count bytes and provisional big-endian value."""

    if len(reply) != 16 or not reply.startswith(READ_PREFIX):
        raise ValueError(f"invalid counter reply: {reply.hex()}")
    raw = reply[-4:]
    return raw, int.from_bytes(raw, "big")
