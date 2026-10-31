from __future__ import annotations

from typing import Protocol


class BulkTransport(Protocol):
    def write(self, endpoint: int, data: bytes, timeout_ms: int) -> int: ...

    def read(self, endpoint: int, size: int, timeout_ms: int) -> bytes: ...

    def close(self) -> None: ...
