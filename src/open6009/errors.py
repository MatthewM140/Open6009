class Open6009Error(Exception):
    """Base project exception."""


class DeviceNotFoundError(Open6009Error):
    """No matching physical USB device was found."""


class UsbAccessError(Open6009Error):
    """The USB device exists but the process cannot access usbfs."""


class ProtocolError(Open6009Error):
    """A packet is malformed or violates a confirmed invariant."""


class AcquisitionOverflowError(ProtocolError):
    """The device reported NI error -200361: onboard acquisition overflow."""


class EvidenceRequiredError(Open6009Error):
    """The requested operation is disabled pending protocol evidence."""
