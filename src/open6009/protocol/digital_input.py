"""Captured, input-only USB-6009 digital-port transaction plan.

This codec is restricted to the two Base-15 read-only port captures. It has no
direction or write operation and performs no USB I/O.
"""

from dataclasses import dataclass

_DISCOVERY = bytes.fromhex("0000000c0008010000000000")
_DISCOVERY_REPLY = bytes.fromhex("0000001c0018010000000002020000000132c0000100000000020000")
_CONFIGURE = bytes.fromhex("0000001400100112021000000004000000040000")
_OK = bytes.fromhex("0000000c0008010000000002")
_READ_BASE = bytes.fromhex("00000010000c010e0210000000030000")
_READ_REPLY = bytes.fromhex("00000010000c0100000000020003ff00")


@dataclass(frozen=True, slots=True)
class DigitalInputPortPlan:
    port: int
    frames: tuple[bytes, bytes, bytes]
    replies: tuple[bytes, bytes, bytes]


def captured_digital_input_port_plan(port: int) -> DigitalInputPortPlan:
    """Build the exact captured read-only plan for port 0 or port 1."""

    if port not in (0, 1):
        raise ValueError("USB-6009 has only captured digital ports 0 and 1")
    read = _READ_BASE[:-2] + bytes((port, 0))
    return DigitalInputPortPlan(port, (_DISCOVERY, _CONFIGURE, read), (_DISCOVERY_REPLY, _OK, _READ_REPLY))


def decode_captured_port_reply(port: int, reply: bytes) -> int:
    """Decode the one-byte port state after strict structural validation.

    NI's reference captures happened to read every pulled-up input HIGH and
    therefore ended in ``...ff00``. Hardware reads with driven inputs vary
    only the penultimate state byte (for example ``...fe00`` when P0.0 is
    LOW). All envelope, status, selector, and reserved bytes remain fixed.
    Port 1 is masked to its documented four-bit width.
    """

    if port not in (0, 1):
        raise ValueError("port must be 0 or 1")
    if (len(reply) != len(_READ_REPLY)
            or reply[:-2] != _READ_REPLY[:-2]
            or reply[-1] != _READ_REPLY[-1]):
        raise ValueError("unexpected captured digital-input reply")
    return reply[-2] & (0xFF if port == 0 else 0x0F)
